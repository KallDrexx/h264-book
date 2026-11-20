"""SPOILER: Chapter 10.2 reference decoder for the P / B field and frame streams of fpgen.py.

It does NOT parse the bitstream: it takes the generator's manifest (the syntax elements the generator wrote: slice header fields,
list modification commands, MMCO commands, macroblock types, ref_idx and mvd values) and does everything a decoder does after
parsing, for pictures that are frames or FIELDS:
  * PicOrderCnt of every field / frame (8.2.1, type 0), pairing of fields into frame stores (7.4.1.2.4, 3.30);
  * reference lists of P and B slices for frames and fields (fdpb.py: 8.2.4.2.x, 8.2.4.3), marking per field (8.2.5);
  * motion vector prediction inside the (field) picture, P_Skip, B_Skip / direct (spatial and temporal, co-located FIELD of
    RefPicList1[0]), implicit weights with field POCs;
  * reference sample selection from fields (alternate lines of the stored frame), the chroma vector offset between fields of
    different parity (Table 8-9), quarter-sample interpolation inside the field;
  * deblocking of field pictures (bS 3 on intra horizontal macroblock edges, vertical vector limit 2) and of frames;
  * interleaving into the frame store, output order (ascending POC inside each IDR / MMCO-5 period).
Its output is (a) the dump lines of Readme section 5 and (b) the frames.  ffmpeg decodes the same streams with identical pixels
(test_fieldrefs.py checks this for every case), which validates the generator's bitstream and this decoder at the same time.
Direct prediction only works when the co-located picture is coded like the current one (field <-> field, frame <-> frame): the
generator avoids the other cases (the frame <-> field conversions of 8.4.1.2.1 are described in the Readme but not exercised).
MUTANT switches ONE deliberately wrong rule on (MUTANTS here, fdpb.MUTANTS, fbs.MUTANTS).
"""
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import deblock_ref  # noqa: E402
import direct_ref  # noqa: E402
import dmvpred_ref  # noqa: E402
import fbs  # noqa: E402
import fdpb  # noqa: E402
import interp_ref  # noqa: E402
import intraref  # noqa: E402
import transref  # noqa: E402

MUTANT = None
MUTANTS = {
    "chroma_off_sign": "chroma vector offset between fields of different parity has the wrong sign",
    "chroma_off_none": "no chroma vector offset between fields of different parity",
    "chroma_off_same_parity": "the chroma vector offset is applied from the current parity alone, also when the reference field has the same parity",
    "col_other_parity": "temporal / spatial direct: the co-located field is the field of the OPPOSITE parity of RefPicList1[0]",
    "col_top_field": "temporal / spatial direct: the co-located field is always the top field of the frame holding RefPicList1[0]",
    "implicit_frame_poc": "implicit weights and temporal scaling use the POC of the frame (minimum of both fields) instead of the field's own POC",
    "map_col_by_frame": "MapColToList0 identifies the co-located reference by its frame only (the field's parity is ignored)",
    "pic_poc_max": "the POC of a frame picture / pair is the larger of the two field POCs",
    "output_decode_order": "frames are output in decoding order",
}
ALL_MUTANTS = {}


class Unsupported(Exception):
    pass


class StopDecoding(Exception):
    pass


def new_planes(w_mbs, h_mbs, fill=128):
    return ([[fill] * (16 * w_mbs) for _ in range(16 * h_mbs)], [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)],
            [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)])


class Ref(tuple):
    pid = None
    poc = 0
    mot = None
    par = "F"
    lt = False
    sid = -1


class Store:
    def __init__(self, sid, coded, wm, hm):
        self.id, self.coded = sid, coded
        self.planes = new_planes(wm, hm)
        self.has = {"T": False, "B": False}
        self.poc = {"T": None, "B": None}
        self.mot = {}
        self.period = 0

    def fpoc(self):
        v = [self.poc[p] for p in "TB" if self.has[p]]
        return max(v) if MUTANT == "pic_poc_max" else min(v)


class Poc0:
    """8.2.1.1, pic_order_cnt_type 0, one call per picture in decoding order."""

    def __init__(self, log2_lsb):
        self.max = 1 << log2_lsb
        self.prev_msb = self.prev_lsb = 0

    def picture(self, p):
        if p["idr"]:
            self.prev_msb = self.prev_lsb = 0
        lsb = p["lsb"]
        msb = self.prev_msb
        if lsb < self.prev_lsb and self.prev_lsb - lsb >= self.max // 2:
            msb += self.max
        elif lsb > self.prev_lsb and lsb - self.prev_lsb > self.max // 2:
            msb -= self.max
        st = p["struct"]
        top = bot = None
        if st == "F":
            top = msb + lsb
            bot = top + p["dbot"]
        elif st == "T":
            top = msb + lsb
        else:
            bot = msb + lsb
        self.pending = (msb, lsb)
        return top, bot

    def finish(self, ref, mmco5, struct, new_top):
        if not ref:
            return
        if mmco5:
            self.prev_msb = 0
            self.prev_lsb = 0 if struct == "B" else (new_top or 0)
        else:
            self.prev_msb, self.prev_lsb = self.pending


def clip1(v):
    return 0 if v < 0 else 255 if v > 255 else v


def mb_neighbours(addr, w, owner, slice_idx):
    mx, my = addr % w, addr // w

    def ok(cond, a2):
        return bool(cond) and owner.get(a2) == slice_idx
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1), ok(mx > 0 and my > 0, addr - w - 1))


def chroma_offset(cur_par, ref_par):
    """Table 8-9 / 8-10: vertical chroma vector offset (units of 1/8 chroma sample) when a field refers to a field of the other parity."""
    if cur_par == "F" or MUTANT == "chroma_off_none":
        return 0
    if MUTANT == "chroma_off_same_parity":
        return 2 if cur_par == "B" else -2
    if cur_par == ref_par:
        return 0
    off = 2 if cur_par == "B" else -2          # bottom field refers to top field: +2; top field refers to bottom field: -2
    return -off if MUTANT == "chroma_off_sign" else off


def inter_prediction(L0, L1, rec, mx, my, W, H, imp, cur_par):
    """Prediction of the whole macroblock (luma 16x16, Cb 8x8, Cr 8x8), block by block.  W, H: size of the picture being decoded
    (a field is W x H/2)."""
    pred = ([[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(8)], [[0] * 8 for _ in range(8)])
    parts = getattr(rec, "pred_parts", None) or rec.parts or [dict(x=0, y=0, w=16, h=16)]
    for p in parts:
        x, y, w, h = p["x"], p["y"], p["w"], p["h"]
        r0, m0x, m0y, r1, m1x, m1y = rec.grid[(y >> 2) * 4 + (x >> 2)]
        uses = [(0, r0, m0x, m0y)] if r0 >= 0 else []
        if r1 >= 0:
            uses.append((1, r1, m1x, m1y))
        X, Y = 16 * mx + x, 16 * my + y
        blocks = [[], [], []]
        used_refs = {}
        for lst, ri, mvx, mvy in uses:
            ref = (L0 if lst == 0 else L1)[ri]
            if ref is None:
                raise Unsupported(f"ref_idx {ri} of list {lst} names 'no reference picture'")
            used_refs[lst] = ri
            blocks[0].append((lst, interp_ref.luma_block(ref[0], W, H, X, Y, w, h, mvx, mvy)))
            cmy = mvy + chroma_offset(cur_par, ref.par)
            for pl in (1, 2):
                blocks[pl].append((lst, interp_ref.chroma_block(ref[pl], W // 2, H // 2, X // 2, Y // 2, w // 2, h // 2, mvx, cmy)))
        iw = None
        if imp is not None and len(uses) == 2:
            iw = direct_ref.implicit_weights(imp.cur_poc, imp.poc0[used_refs[0]], imp.poc1[used_refs[1]],
                                             imp.lt0[used_refs[0]], imp.lt1[used_refs[1]])
        for pl in range(3):
            bl = blocks[pl]
            if len(bl) == 1:
                blk = bl[0][1]
            elif iw is not None:
                blk = [[direct_ref.implicit_combine(a, b, iw[0], iw[1]) for a, b in zip(r0, r1)] for r0, r1 in zip(bl[0][1], bl[1][1])]
            else:
                blk = [[(a + b + 1) >> 1 for a, b in zip(r0, r1)] for r0, r1 in zip(bl[0][1], bl[1][1])]
            if pl == 0:
                for r in range(h):
                    pred[0][y + r][x:x + w] = blk[r]
            else:
                for r in range(h // 2):
                    pred[pl][y // 2 + r][x // 2:x // 2 + w // 2] = blk[r]
    return pred


class Decoder:
    def __init__(self, man, pixels=True, hook=None, strict=False):
        self.man, self.pixels, self.hook = man, pixels, hook
        sc = man["sc"]
        self.sc = sc
        self.wm, self.fhm = sc["mbw"], sc["map_h"] * 2          # frame height in macroblocks
        self.dpb = fdpb.Dpb(sc["refs"], sc["log2_fn"], MUTANT if MUTANT in fdpb.MUTANTS else None)
        self.poc = Poc0(sc["log2_poc"])
        self.stores = {}
        self.order = []                    # store ids in creation order
        self.lines = []
        self.period = 0
        self.first = None                  # the store of an unpaired first field: dict(struct, frame_num, ref, store)
        self.n_store = 0
        direct_ref.STRICT = strict

    # ------------------------------------------------------------------ one picture
    def run(self):
        direct_ref.MUTANT = MUTANT if MUTANT in direct_ref.MUTANTS else None
        fbs.MUTANT = MUTANT if MUTANT in fbs.MUTANTS else None
        interp_ref.MUTANT = None
        try:
            for n, p in enumerate(self.man["pics"]):
                self.picture(n, p)
        finally:
            direct_ref.MUTANT = fbs.MUTANT = None
            direct_ref.STRICT = False
            fbs.FIELD = False
        return self

    def picture(self, n, p):
        st = p["struct"]
        field = st != "F"
        top, bot = self.poc.picture(p)
        ref = p["ref_idc"] != 0
        # ---- pairing (7.4.1.2.4 / 3.30)
        second = False
        if self.first is not None and field and not p["idr"]:
            f = self.first
            if f["struct"] != st and f["frame_num"] == p["frame_num"] and f["ref"] == ref:
                second = True
        if second:
            store = self.stores[self.first["store"]]
            self.first = None
        else:
            store = Store(self.n_store, "P" if field else "F", self.wm, self.fhm)
            self.stores[store.id] = store
            self.order.append(store.id)
            self.n_store += 1
            if p["idr"]:
                self.period += 1
            store.period = self.period
            self.first = dict(struct=st, frame_num=p["frame_num"], ref=ref, store=store.id) if field else None
        if second:
            self.first = None
        store.has.update({q: True for q in ("TB" if not field else st)})
        for q, v in (("T", top), ("B", bot)):
            if v is not None:
                store.poc[q] = v
        fpoc = min(v for v in (top, bot) if v is not None)
        if MUTANT == "pic_poc_max" and not field:
            fpoc = max(top, bot)
        cur = dict(n=n, struct=st, frame_num=p["frame_num"], idr=p["idr"], ref=ref, second=second, store=store.id, coded=store.coded,
                   poc_t=top, poc_b=bot, poc=fpoc if not field else (top if st == "T" else bot),
                   frame_poc=min(v for v in (store.poc["T"], store.poc["B"]) if v is not None))
        if second and store.poc["T"] is not None and store.poc["B"] is not None:
            cur["frame_poc"] = min(store.poc["T"], store.poc["B"])
        self.lines.append(f"PIC {n} struct={st} frame_num={p['frame_num']} idr={int(p['idr'])} ref={int(ref)} store={store.id} "
                          f"second={int(second)} top={'-' if top is None else top} bot={'-' if bot is None else bot} "
                          f"poc={min(v for v in (top, bot) if v is not None)}")
        # ---- decode all slices into one picture (a field is decoded like a small frame)
        W = 16 * self.wm
        Hp = 16 * (self.fhm // 2 if field else self.fhm)
        wm, hm = self.wm, Hp // 16
        planes = new_planes(wm, hm)
        fbs.FIELD = field
        par = st if field else "F"
        pst = dmvpred_ref.PicState(wm, hm)
        bp = fbs.Pic(wm, hm)
        owner, intra_of = {}, {}
        qp_y, pcm_flag, adiv2, bdiv2 = [0] * (wm * hm), [False] * (wm * hm), [0] * (wm * hm), [0] * (wm * hm)
        offs = (self.man["sc"]["cqo"], self.man["sc"]["cqo"])
        for si, sl in enumerate(p["slices"]):
            self.slice(n, si, sl, p, cur, par, planes, pst, bp, owner, intra_of, qp_y, pcm_flag, adiv2, bdiv2, wm, hm, W, Hp, offs)
        grid = fbs.picture_bs(bp)
        if self.pixels:
            deblock_ref.filter_picture(planes, wm, grid, qp_y, pcm_flag, adiv2, bdiv2, offs)
            # interleave into the frame store
            if field:
                o = 0 if st == "T" else 1
                for pl in range(3):
                    for y, row in enumerate(planes[pl]):
                        store.planes[pl][2 * y + o][:] = row
            else:
                for pl in range(3):
                    for y, row in enumerate(planes[pl]):
                        store.planes[pl][y][:] = row
        store.mot["F" if not field else st] = pst
        # ---- marking
        mm = p.get("mmcos")
        res = self.dpb.finish_picture(cur, p.get("lt_flag", 0), mm)
        if res is not None:
            for q in "TB":
                if res["top" if q == "T" else "bot"] is not None:
                    store.poc[q] = res["top" if q == "T" else "bot"]
            self.period += 1
            store.period = self.period
        self.poc.finish(ref, res is not None, st, res["top"] if res else None)
        rows, mx = self.dpb.state()
        txt = ",".join(f"{i}:{m}:{fn}:{'-' if lt is None else lt}" for i, m, fn, lt in rows) or "-"
        self.lines.append(f"DPB {n} maxlt={'-' if mx is None else mx} stores={txt}")
        if self.hook is not None and (not field or second):
            self.hook(store, n)

    # ------------------------------------------------------------------ one slice
    def ref_of(self, slot, cur_par, pic_poc_override=None):
        if slot is None or slot[0] < 0:
            return None
        sid, par, kind, num = slot
        s = self.stores[sid]
        if par == "F":
            r = Ref(tuple(s.planes))
            r.poc = s.fpoc()
            r.mot = s.mot.get("F") if s.coded == "F" else None
        else:
            o = 0 if par == "T" else 1
            r = Ref(tuple(pl[o::2] for pl in s.planes))
            r.poc = s.poc[par]
            if MUTANT == "implicit_frame_poc":
                r.poc = min(v for v in (s.poc["T"], s.poc["B"]) if v is not None)
            r.mot = s.mot.get(par) if s.coded == "P" else None
        r.pid, r.par, r.lt, r.sid = (sid, par), par, kind == "L", sid
        return r

    def slice(self, n, si, sl, p, cur, par, planes, pst, bp, owner, intra_of, qp_y, pcm_flag, adiv2, bdiv2, wm, hm, W, Hp, offs):
        sc = self.sc
        stype = sl["type"]
        bp.slices[si] = fbs.SliceFacts(sl["dbf"][0], stype)
        L0 = L1 = []
        slots0 = slots1 = []
        if stype in (0, 1):
            dc = dict(cur)
            slots0, slots1 = self.dpb.lists(dc, stype == 1, sl["a0"], sl["a1"], sl["mods0"], sl["mods1"])
            if cur["struct"] != "F" and MUTANT == "implicit_frame_poc":
                pass
            L0 = [self.ref_of(x, par) for x in slots0]
            L1 = [self.ref_of(x, par) for x in slots1]
            self.lines.append(f"LIST {n} slice={si} type={'PB'[stype]} l0={','.join(map(fdpb.token, slots0)) or '-'} "
                              f"l1={','.join(map(fdpb.token, slots1)) or '-'}")
            for lst in (L0, L1):
                for r in lst:
                    if r is not None and cur["struct"] != "F" and False:
                        pass

        def facts(lst):
            return ([x.pid if x is not None else None for x in lst], [x.poc if x is not None else 0 for x in lst],
                    [x is not None and x.lt for x in lst])
        cur_poc = cur["poc"]
        if MUTANT == "implicit_frame_poc" and cur["struct"] != "F":
            cur_poc = cur["frame_poc"]
        p0_, o0_, t0_ = facts(L0)
        p1_, o1_, t1_ = facts(L1)
        mot1 = None
        if stype == 1 and L1 and L1[0] is not None:
            mot1 = L1[0].mot
            if L1[0].par != "F" and mot1 is not None:
                s1 = self.stores[L1[0].sid]
                if MUTANT == "col_other_parity":
                    mot1 = s1.mot.get("B" if L1[0].par == "T" else "T", mot1)
                elif MUTANT == "col_top_field":
                    mot1 = s1.mot.get("T", mot1)
        if stype in (0, 1):
            spatial = bool(sl["spatial"])
            sctx = direct_ref.SliceCtx(cur_poc, p0_, o0_, t0_, p1_, o1_, t1_, mot1, spatial, True, sc["bipred_idc"] == 2)
            slice_recs = []
            recs = [self.record(a, m) for a, m in zip(range(sl["first"], sl["first"] + len(sl["mbs"])), sl["mbs"])]
            res = SimpleNamespace(info=SimpleNamespace(index=si, slice_type=stype), mbs=recs)
            try:
                if MUTANT == "map_col_by_frame":
                    self._fill_by_frame(pst, res, sctx, p1_)
                else:
                    dmvpred_ref.fill_slice(pst, res, sctx)
            except direct_ref.DirectInvalid as e:
                e.pic, e.slice = n, si
                raise
            imp = sctx if (stype == 1 and sc["bipred_idc"] == 2) else None
        else:
            recs = [self.record(a, m) for a, m in zip(range(sl["first"], sl["first"] + len(sl["mbs"])), sl["mbs"])]
            res = SimpleNamespace(info=SimpleNamespace(index=si, slice_type=stype), mbs=recs)
            dmvpred_ref.fill_slice(pst, res, direct_ref.SliceCtx(cur_poc, [], [], [], [], [], [], None, False, True))
            imp = None
        for rec in res.mbs:
            a = rec.addr
            owner[a] = si
            mx, my = a % wm, a // wm
            is_intra = not rec.f["name"].startswith(("P_", "B_"))
            intra_of[a] = is_intra
            mbav = mb_neighbours(a, wm, owner, si)
            avail = (mbav[0], mbav[1], mbav[3])
            qp = sl["qp"]
            mbf = fbs.Mb(si, intra=is_intra, pcm=rec.pcm is not None, skip=rec.f["name"] in ("P_Skip", "B_Skip"))
            bp.mbs[a] = mbf
            adiv2[a], bdiv2[a] = sl["dbf"][1], sl["dbf"][2]
            pcm_flag[a] = rec.pcm is not None
            qp_y[a] = 0 if rec.pcm is not None else qp
            if not is_intra:
                grid = []
                for g in rec.grid:
                    ent = []
                    if g[0] >= 0:
                        ent.append((L0[g[0]].pid, g[1], g[2]))
                    if g[3] >= 0:
                        ent.append((L1[g[3]].pid, g[4], g[5]))
                    grid.append(tuple(ent))
                mbf.grid = grid
                mbf.refidx = [tuple(x for x in (g[0], g[3]) if x >= 0) for g in rec.grid]
            else:
                mbf.nz = [True] * 16 if rec.pcm is not None else [False] * 16
            if not self.pixels:
                continue
            if rec.pcm is not None:
                d = rec.pcm
                intraref.put(planes[0], 16 * mx, 16 * my, [list(d[16 * k:16 * k + 16]) for k in range(16)])
                for q in range(2):
                    base = 256 + 64 * q
                    intraref.put(planes[1 + q], 8 * mx, 8 * my, [list(d[base + 8 * k:base + 8 * k + 8]) for k in range(8)])
            elif is_intra:
                intraref.reconstruct_i16(planes, mx, my, avail, dict(i16_mode=2, chroma_pred=0, blocks={}), qp, offs)
            else:
                pred = inter_prediction(L0, L1, rec, mx, my, W, Hp, imp, cur["struct"] if cur["struct"] != "F" else "F")
                for k in range(16):
                    planes[0][16 * my + k][16 * mx:16 * mx + 16] = pred[0][k]
                for q in range(2):
                    for k in range(8):
                        planes[1 + q][8 * my + k][8 * mx:8 * mx + 8] = pred[1 + q][k]

    def _fill_by_frame(self, pst, res, sctx, p1_):
        orig = direct_ref.map_col_to_list0

        def by_frame(ctx, pid):
            idxs = [i for i, q in enumerate(ctx.pid0) if q is not None and pid is not None and q[0] == pid[0]]
            if not idxs:
                raise direct_ref.DirectInvalid("temporal direct: reference not in RefPicList0")
            return idxs[0]
        direct_ref.map_col_to_list0 = by_frame
        try:
            dmvpred_ref.fill_slice(pst, res, sctx)
        finally:
            direct_ref.map_col_to_list0 = orig

    @staticmethod
    def record(addr, m):
        return SimpleNamespace(addr=addr, f=dict(name=m["name"]), parts=m.get("parts", []),
                               pcm=bytes(m["samples"]) if m["name"] == "I_PCM" else None)

    # ------------------------------------------------------------------ output
    def frames(self):
        """-> [(store id, bytes)] in output order."""
        ids = list(self.order)
        if MUTANT != "output_decode_order":
            ids.sort(key=lambda i: (self.stores[i].period, self.stores[i].fpoc(), i))
        return [(i, self.frame_bytes(self.stores[i])) for i in ids]

    def frame_bytes(self, s):
        b = bytearray()
        for pl in s.planes:
            for row in pl:
                b += bytes(row)
        return bytes(b)


def decode(man, pixels=True, hook=None, strict=False):
    return Decoder(man, pixels, hook, strict).run()
