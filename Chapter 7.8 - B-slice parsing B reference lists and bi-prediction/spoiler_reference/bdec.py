#!/usr/bin/env python3
"""SPOILER: complete reference decoder for the Chapter 7.8 scope: CAVLC I, P and B pictures (frames, 8-bit 4:2:0, no 8x8 transform,
NO direct prediction) with the deblocking filter, full reference management (Chapter 7.6), explicit weighted prediction in P slices
(7.7) and in B slices (weighted_bipred_idc 1), B reference lists (8.2.4.2.3), bi-prediction, and output in display order (frames sorted
by POC inside each IDR period).  Do not read before your own decoder passes.

It is Chapter 7.7's decoder (weightdec.py) plus: poc_ref.py (picture order count), brefmgmt_ref.py (lists of B slices),
bslicehdr_ref.py (both lists' tables), bmvpred_ref.py (vector prediction per list), bsref_b.py (bS with two vectors) and the
prediction step for L0 / L1 / Bi partitions.  Streams with B_Skip / B_Direct_16x16 / B_Direct_8x8 or weighted_bipred_idc = 2 raise
Unsupported (Chapter 7.9).

usage: bdec.py <in.ts|.264> <out.yuv>        (decodes everything; --frames N stops after N output frames)
decode(..., record=True) records, for diagnosis, what each macroblock's prediction looked like: per prediction block the lists used,
ref_idx, reference picture ids, vectors and weights.  decode(..., pixels=False) skips sample work.

MUTANT switches on ONE deliberately wrong rule for the harness self-test (MUTANTS lists the decoder-level ones; the other modules have
their own MUTANTS tables, all reachable through ALL_MUTANTS).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import inter_ref as ir, yuvref  # noqa: E402
import bmvpred_ref  # noqa: E402
import brefmgmt_ref  # noqa: E402
import bslicehdr_ref  # noqa: E402
import bsref_b  # noqa: E402
import deblock_ref  # noqa: E402
import interp_ref  # noqa: E402
import intra4ref  # noqa: E402
import intraref  # noqa: E402
import poc_ref  # noqa: E402
import transref  # noqa: E402

MUTANT = None
MUTANTS = {
    "bi_round_down": "default bi-prediction averages with (p0 + p1) >> 1 (no +1)",
    "bi_only_l0": "a bi-predicted partition is predicted from list 0 only",
    "l1_ref_from_l0": "ref_idx_l1 indexes RefPicList0 (the list-1 pictures are never used)",
    "bi_w_offset_sum": "explicit bi-prediction: the offset term is (o0 + o1) >> 1 instead of (o0 + o1 + 1) >> 1",
    "bi_w_no_round": "explicit bi-prediction: the rounding term 2^logWD is missing before the shift",
    "bi_w_swapped": "explicit bi-prediction: w0 is applied to the list-1 prediction and w1 to the list-0 prediction",
    "l1_uni_weights_l0": "a list-1-only partition in a B slice uses the weights of the list-0 table",
    "nonref_b_stored": "non-reference B pictures (nal_ref_idc 0) are stored in the DPB as short-term references",
    "output_decode_order": "frames are written in decoding order",
    "output_ignores_idr": "frames are sorted by POC over the whole stream (no split at IDR pictures)",
    "poc_no_msb": "pic_order_cnt_lsb is used as the POC without the MSB part (wrong after the first wrap)",
    "p_lists_for_b": "B slices use P-style lists (descending PicNum) for list 0 and list 0 again for list 1",
}
DECODER_MUTANTS = {
    "inter_dc_prescaled": "inter luma blocks use the Intra16x16 rule: coefficient 0 is not scaled",
    "no_final_clip": "pred + residual is not clipped to 0..255 (wraps modulo 256)",
    "qp_slice_only": "inter residual dequantised with the slice QP instead of the running QP_Y",
    "chroma_qp_luma": "chroma residual dequantised with QP_Y instead of QP_C",
}
ALL_MUTANTS = {}            # every wrong rule reachable through MUTANT (decoder level, lists, vector prediction, bS); filled below
STATS = None          # a collections.Counter: coverage of the B rules (self-test)


class Unsupported(Exception):
    pass


def new_planes(w_mbs, h_mbs, fill=128):
    return ([[fill] * (16 * w_mbs) for _ in range(16 * h_mbs)], [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)],
            [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)])


def check_supported(s):
    h, p, sps = s.hdr, s.pps, s.sps
    if s.slice_type not in (0, 1, 2):
        raise Unsupported("SP / SI slices are not handled")
    if s.entropy:
        raise Unsupported("CABAC (Phase 8)")
    if h.get("field_pic_flag", 0) or (sps.get("mb_adaptive_frame_field_flag", 0) and not sps.get("frame_mbs_only_flag", 1)):
        raise Unsupported("field / MBAFF pictures (Phase 10)")
    if s.transform_8x8_mode:
        raise Unsupported("transform_8x8_mode_flag = 1 (Phase 8)")
    if s.slice_type == 1 and p.get("weighted_bipred_idc", 0) == 2:
        raise Unsupported("implicit weighted bi-prediction (weighted_bipred_idc 2, Chapter 7.9)")


class Dpb:
    """Chapter 7.6's reference management (brefmgmt_ref.Dpb) plus the decoded pictures it manages."""

    def __init__(self, sps):
        mu = MUTANT if MUTANT in brefmgmt_ref.MUTANTS else None
        self.d = brefmgmt_ref.Dpb(sps["max_num_ref_frames"], sps["log2_max_frame_num_minus4"] + 4,
                                  bool(sps.get("gaps_in_frame_num_allowed_flag", 0)), mu)
        self.pix = {}
        self.last = None

    def start_picture(self, frame_num, idr, blank):
        try:
            labels = self.d.check_frame_num(frame_num, idr)
        except brefmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        for lab in labels:
            self.pix[lab] = self.last or blank

    def lists(self, cur_fn, cur_poc, stype, a0, a1, mods0, mods1):
        """-> (RefPicList0, RefPicList1) as lists of stored pictures (None = 'no reference picture'); list 1 is [] in P slices."""
        try:
            if stype == 0:
                slots = self.d.build_list(cur_fn, a0, mods0)
                return [self.pix[sl[0]] if sl is not None else None for sl in slots], []
            if MUTANT == "p_lists_for_b":
                slots = self.d.build_list(cur_fn, a0, mods0)
                l = [self.pix[sl[0]] if sl is not None else None for sl in slots]
                first = next((x for x in l if x is not None), None)
                l = [x if x is not None else first for x in l]
                l1 = (list(l) + [first] * a1)[:a1]
                return l, l1
            s0, s1 = self.d.build_lists_b(cur_fn, cur_poc, a0, a1, mods0, mods1)
        except brefmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        return ([self.pix[sl[0]] if sl is not None else None for sl in s0],
                [self.pix[sl[0]] if sl is not None else None for sl in s1])

    def slots(self, cur_fn, cur_poc, stype, a0, a1, mods0, mods1):
        if stype == 0:
            return self.d.build_list(cur_fn, a0, mods0), []
        return self.d.build_lists_b(cur_fn, cur_poc, a0, a1, mods0, mods1)

    def mark(self, label, pic, frame_num, idr, ref_idc, lt_flag, mmcos, poc, is_b):
        if ref_idc == 0 and is_b and MUTANT == "nonref_b_stored":
            ref_idc = 1
        try:
            self.d.finish_picture(label, frame_num, idr, ref_idc, lt_flag, mmcos, poc)
        except brefmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        if ref_idc:
            self.pix[label] = pic
            self.last = pic
        live = {f.label for f in self.d.frames}
        for k in list(self.pix):
            if k not in live:
                del self.pix[k]


def mb_neighbours(addr, w, owner, slice_idx, intra, constrained):
    mx, my = addr % w, addr // w

    def ok(cond, a2):
        if not cond or owner.get(a2) != slice_idx:
            return False
        return not (constrained and intra.get(a2) is False)
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1),
            ok(mx > 0 and my > 0, addr - w - 1))


def clip1(v):
    return 0 if v < 0 else 255 if v > 255 else v


# ---------------------------------------------------------------- 8.4.2.3 weighted sample prediction
def weight1(v, w, o, log_wd):
    """Explicit weighting of a single-list prediction sample (8-448 / 8-449)."""
    t = ((v * w + (1 << (log_wd - 1))) >> log_wd) + o if log_wd >= 1 else v * w + o
    return clip1(t)


def weight2(v0, v1, w0, w1, o0, o1, log_wd):
    """Explicit weighting of a bi-predicted sample (8-450)."""
    if MUTANT == "bi_w_swapped":
        w0, w1 = w1, w0
    rnd = 0 if MUTANT == "bi_w_no_round" else (1 << log_wd)
    off = (o0 + o1) >> 1 if MUTANT == "bi_w_offset_sum" else (o0 + o1 + 1) >> 1
    return clip1(((v0 * w0 + v1 * w1 + rnd) >> (log_wd + 1)) + off)


def entry_weights(wt, lst, idx, plane):
    """-> (weight, offset, logWD) of table `lst` (0/1), entry idx, plane 0 Y / 1 Cb / 2 Cr."""
    if lst == 1 and MUTANT == "l1_uni_weights_l0":
        lst = 0
    ents = wt[f"entries{lst}"]
    e = ents[min(idx, len(ents) - 1)] if MUTANT == "l1_uni_weights_l0" else ents[idx]
    if plane == 0:
        return e["wy"], e["oy"], wt["denom_y"]
    return (e["wcb"], e["ocb"], wt["denom_c"]) if plane == 1 else (e["wcr"], e["ocr"], wt["denom_c"])


def combine(blocks, refs, wt, plane, mode_bi):
    """blocks: [(list, block)] prediction blocks of one partition and plane (1 for L0 or L1 only, 2 for Bi: list 0 first);
    refs: {list: ref_idx}.  -> predicted block (list of rows)."""
    if len(blocks) == 1:
        lst, b = blocks[0]
        if wt is None:
            return b
        w, o, lw = entry_weights(wt, lst, refs[lst], plane)
        return [[weight1(v, w, o, lw) for v in row] for row in b]
    b0, b1 = blocks[0][1], blocks[1][1]
    if wt is None:
        r = 0 if MUTANT == "bi_round_down" else 1
        return [[(x + y + r) >> 1 for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]
    w0, o0, lw = entry_weights(wt, 0, refs[0], plane)
    w1, o1, _ = entry_weights(wt, 1, refs[1], plane)
    return [[weight2(x, y, w0, w1, o0, o1, lw) for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]


def inter_prediction(L0, L1, rec, mx, my, W, H, record, wt):
    """Prediction of the whole macroblock (luma 16x16, Cb 8x8, Cr 8x8), partition by partition.  rec.grid gives, for each 4x4 block,
    (ref0, mv0x, mv0y, ref1, mv1x, mv1y).  wt = the slice's weight tables (explicit weighting) or None."""
    pred = ([[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(8)], [[0] * 8 for _ in range(8)])
    parts = rec.parts or [dict(x=0, y=0, w=16, h=16)]
    info = []
    for p in parts:
        x, y, w, h = p["x"], p["y"], p["w"], p["h"]
        r0, m0x, m0y, r1, m1x, m1y = rec.grid[(y >> 2) * 4 + (x >> 2)]
        uses = [(0, r0, m0x, m0y)] if r0 >= 0 else []
        if r1 >= 0:
            uses.append((1, r1, m1x, m1y))
        if MUTANT == "bi_only_l0" and len(uses) == 2:
            uses = uses[:1]
        X, Y = 16 * mx + x, 16 * my + y
        planes_blocks = [[], [], []]
        used_refs, pids, mvs = {}, [], []
        for lst, ri, mvx, mvy in uses:
            lname = L0 if (lst == 0 or MUTANT == "l1_ref_from_l0") else L1
            ref = lname[min(ri, len(lname) - 1)] if MUTANT == "l1_ref_from_l0" else lname[ri]
            if ref is None:
                raise Unsupported(f"ref_idx {ri} of list {lst} names 'no reference picture'")
            used_refs[lst] = ri
            pids.append(ref.pid)
            mvs.append((mvx, mvy))
            planes_blocks[0].append((lst, interp_ref.luma_block(ref[0], W, H, X, Y, w, h, mvx, mvy)))
            for pl in (1, 2):
                planes_blocks[pl].append((lst, interp_ref.chroma_block(ref[pl], W // 2, H // 2, X // 2, Y // 2, w // 2, h // 2,
                                                                       mvx, mvy)))
        for pl in range(3):
            blk = combine(planes_blocks[pl], used_refs, wt, pl, len(uses) == 2)
            if pl == 0:
                for r in range(h):
                    pred[0][y + r][x:x + w] = blk[r]
            else:
                for r in range(h // 2):
                    pred[pl][y // 2 + r][x // 2:x // 2 + w // 2] = blk[r]
        if STATS is not None and MUTANT is None:
            kind = "Bi" if len(uses) == 2 else ("L0" if uses and uses[0][0] == 0 else "L1")
            STATS[f"partition {kind}"] += 1
            if len(uses) == 2:
                STATS["Bi same picture" if pids[0] == pids[1] else "Bi two pictures"] += 1
                if wt is not None:
                    STATS["explicit Bi"] += 1
                if uses[0][1] > 0 or uses[1][1] > 0:
                    STATS["Bi with ref_idx > 0"] += 1
            elif wt is not None and uses and uses[0][0] == 1:
                STATS["explicit L1 only"] += 1
            if uses and uses[-1][0] == 1 and uses[-1][1] > 0:
                STATS["ref_idx_l1 > 0"] += 1
        if record:
            info.append(dict(x=x, y=y, w=w, h=h, mode=p.get("mode", "L0"), refs=[(l, used_refs[l]) for l in sorted(used_refs)],
                             pids=pids, mvs=mvs))
    return pred, info


def inter_residual(rec, qp, offs, tr, slice_qp):
    f = rec.f
    cbp = f["cbp"] if f["cbp"] != "-" else 0
    blocks = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}
    resy = None
    q = slice_qp if MUTANT == "qp_slice_only" else qp
    if cbp & 15:
        resy = [[0] * 16 for _ in range(16)]
        for b in range(16):
            if (cbp >> (b >> 2)) & 1 and ("luma", b) in blocks:
                bx, by = transref.BLK_XY[b]
                r = transref.residual4x4(transref.inverse_zigzag(blocks[("luma", b)]), q, MUTANT == "inter_dc_prescaled", tr)
                for i in range(4):
                    for j in range(4):
                        resy[4 * by + i][4 * bx + j] = r[i][j]
    resc = None
    if cbp >> 4:
        resc = []
        for name, off in zip(("cb", "cr"), offs):
            qpc = q if MUTANT == "chroma_qp_luma" else transref.chroma_qp(q, off)
            z = lambda n: [0] * n  # noqa: E731
            dc = blocks.get((name + "dc", 0), z(4))
            ac = [blocks.get((name + "ac", k), z(15)) for k in range(4)] if cbp >> 4 == 2 else [z(15)] * 4
            resc.append(transref.chroma_residual(qpc, dc, ac, tr))
    return resy, resc


class Ref(tuple):
    """A stored picture: (Y, Cb, Cr) plane lists plus .pid, the identity used by the bS motion rule."""
    pid = None


def copy_planes(planes):
    return tuple([list(r) for r in pl] for pl in planes)


def blank_ref(planes):
    r = Ref(copy_planes(planes))
    r.pid = "blank"
    return r


def picture_meta(pics, order):
    """POC and IDR period of every picture (decoding-order index -> dict(poc, period, mmco5, top, bottom)) and the OUTPUT order
    (list of decoding-order indexes): sorted by POC inside each IDR period (a new period starts at every IDR picture and at every
    picture with memory_management_control_operation 5).  pics: picture index -> list of slice results (with .info.tab)."""
    poc_ref.MUTANT = "poc_no_msb" if MUTANT == "poc_no_msb" else None
    ps = poc_ref.PocState()
    meta, period = {}, 0
    for pi in order:
        s0 = pics[pi][0].info
        t0 = s0.tab
        idr = s0.nal_type == 5
        top, bot, poc = ps.next(s0.hdr, s0.sps, t0["nal_ref_idc"], idr)
        mmco5 = bool(t0["mmco"] and any(op == 5 for op, _ in t0["mmco"]))
        if idr or mmco5:
            period += 1
        meta[pi] = dict(poc=0 if mmco5 else poc, raw=poc, period=period, mmco5=mmco5, top=top, bottom=bot)
        ps.finish(mmco5, top, bot, t0["frame_num"], t0["nal_ref_idc"])
    poc_ref.MUTANT = None
    keyf = (lambda pi: pi) if MUTANT == "output_decode_order" else \
        ((lambda pi: (meta[pi]["poc"], pi)) if MUTANT == "output_ignores_idr" else (lambda pi: (meta[pi]["period"], meta[pi]["poc"], pi)))
    return meta, sorted(order, key=keyf)


def decode(path, upto=None, record=False, workdir=None, pixels=True, cache=None):
    """-> list of pictures in OUTPUT order (display order: sorted by POC inside each IDR period).  Each is
    dict(width, height, x0, y0, planes, mbs, w_mbs, h_mbs, bs, pid (decoding-order index), poc, period, slice_types, lists).
    upto = number of output frames wanted: pictures later in output order that are not needed are neither decoded nor returned
    (the entry is None).  pixels=False skips all sample work.  cache: a dict that keeps the parsed stream between calls (the
    macroblock parsing does not depend on the decoder-level wrong rules)."""
    td = workdir or tempfile.mkdtemp(prefix="bdec_")
    if cache is not None and "parsed" in cache:
        infos, results = cache["parsed"]
    else:
        infos = ir.load(path, td, 3)
        for s in infos:
            check_supported(s)
        tables = bslicehdr_ref.load(os.path.join(td, "ref_trace.txt"))
        for s, t in zip(infos, tables):
            s.tab = t
        results = ir.parse_all(infos, 3)
        for r in results:
            if r.status != "complete":
                raise Unsupported(f"slice {r.info.index}: parser status {r.status}: {r.error or r.note}")
        if cache is not None:
            cache["parsed"] = (infos, results)
    bmvpred_ref.MUTANT = MUTANT if MUTANT in MVPRED_MUTANTS else None
    try:
        bmvpred_ref.fill_results(results)
    except bmvpred_ref.Direct as e:
        raise Unsupported(str(e))
    finally:
        bmvpred_ref.MUTANT = None
    pics = {}
    for r in results:
        pics.setdefault(r.info.picture, []).append(r)
    order = sorted(pics)
    # ---- picture order count and output order, from the headers alone
    meta, out_order = picture_meta(pics, order)
    need = out_order if upto is None else out_order[:upto]
    last_decode = max(need) if need else -1
    dpb = Dpb(infos[0].sps)
    interp_ref.MUTANT = MUTANT if MUTANT in INTERP_MUTANTS else None
    bsref_b.MUTANT = MUTANT if MUTANT in bsref_b.MUTANTS else None
    transref.MUTANT = None
    decoded = {}
    try:
        for n, pi in enumerate(order):
            if pi > last_decode:
                break
            d = decode_picture(pics[pi], dpb, record, n, pixels, meta[pi]["raw"])
            d["poc"], d["period"] = meta[pi]["poc"], meta[pi]["period"]
            decoded[pi] = d
    finally:
        interp_ref.MUTANT = None
        bsref_b.MUTANT = None
    return [decoded.get(pi) for pi in out_order[:len(need)]] if upto is not None else [decoded[pi] for pi in out_order]


MVPRED_MUTANTS = ("b_mvp_l1_uses_l0_nb", "b_nb_unused_list_unavail", "b_nb_unused_list_ref0")
INTERP_MUTANTS = ("taps", "taps_sign", "b_round", "j_from_rounded", "j_from_clipped_b1", "j_round", "j_shift", "avg_round_down",
                  "diag_pair", "diag_pair_r", "clamp_off_by_one", "no_clamp", "mv_int_round", "chroma_mv_units",
                  "chroma_swap_frac", "chroma_swap_bc", "chroma_round")
ALL_MUTANTS.update(MUTANTS)
ALL_MUTANTS.update({k: v for k, v in brefmgmt_ref.MUTANTS.items() if k.startswith("b_")})
ALL_MUTANTS.update({"b_mvp_l1_uses_l0_nb": "list-1 vectors are predicted from the neighbours' LIST 0 data",
                    "b_nb_unused_list_unavail": "a neighbouring partition that does not use list X counts as 'not available' for list X",
                    "b_nb_unused_list_ref0": "a neighbouring inter partition that does not use list X counts as refIdxLX = 0 (instead of -1)"})
ALL_MUTANTS.update({k: v for k, v in bsref_b.MUTANTS.items() if k.startswith("b_") or k in ("ref_idx_compare", "mv_thr_5")})


def decode_picture(rs, dpb, record, pic_no=0, pixels=True, poc=0):
    s0 = rs[0].info
    w = s0.pic_width_in_mbs
    h = s0.pic_size_in_mbs // w
    sps = s0.sps
    cw, ch, x0, y0 = yuvref.cropped_size(
        w, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
        sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
        sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
    W, H = 16 * w, 16 * h
    planes = new_planes(w, h)
    owner, modes, intra, mbinfo = {}, {}, {}, {}
    dpb.start_picture(s0.hdr["frame_num"], s0.nal_type == 5, blank_ref(planes))
    cur_fn = s0.hdr["frame_num"]
    o1 = s0.pps["chroma_qp_index_offset"]
    chroma_offsets = (o1, s0.pps.get("second_chroma_qp_index_offset", o1))
    bp = bsref_b.Pic(w, h)
    qp_y, pcm_flag, adiv2, bdiv2 = [0] * (w * h), [False] * (w * h), [0] * (w * h), [0] * (w * h)
    slice_types, list_log = [], []
    for r in rs:
        s = r.info
        hd = s.hdr
        tab = s.tab
        bp.slices[s.index] = bsref_b.SliceFacts(hd.get("disable_deblocking_filter_idc", 0), s.slice_type)
        constrained = bool(s.pps.get("constrained_intra_pred_flag", 0))
        offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
        wt = tab["wt"] if s.slice_type in (0, 1) else None
        if s.slice_type in (0, 1):
            L0, L1 = dpb.lists(cur_fn, poc, s.slice_type, tab["active0"], tab["active1"], tab["mods0"], tab["mods1"])
            list_log.append(dict(slice=s.index, type=s.slice_type, slots=dpb.slots(cur_fn, poc, s.slice_type, tab["active0"],
                                                                                   tab["active1"], tab["mods0"], tab["mods1"])))
        else:
            L0 = L1 = []
        slice_types.append(s.slice_type)
        last_res = None
        for rec in r.mbs:
            a = rec.addr
            owner[a] = s.index
            f = rec.f
            mx, my = a % w, a // w
            is_intra = not f["name"].startswith(("P_", "B_"))
            intra[a] = is_intra
            mbav = mb_neighbours(a, w, owner, s.index, intra, constrained)
            avail = (mbav[0], mbav[1], mbav[3])
            qp = f["qp"]
            mb_f = bsref_b.Mb(s.index, intra=is_intra or rec.pcm is not None, pcm=rec.pcm is not None,
                              skip=f["name"] in ("P_Skip", "B_Skip"))
            bp.mbs[a] = mb_f
            adiv2[a], bdiv2[a] = hd.get("slice_alpha_c0_offset_div2", 0), hd.get("slice_beta_offset_div2", 0)
            pcm_flag[a] = rec.pcm is not None
            qp_y[a] = 0 if rec.pcm is not None else qp
            cbpv = f["cbp"] if f["cbp"] != "-" else 0
            mb_f.chroma_cbp = (cbpv >> 4) if not is_intra else 0
            if not is_intra:
                blocks_ = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}
                nz = [False] * 16
                for b in range(16):
                    bx, by = transref.BLK_XY[b]
                    nz[by * 4 + bx] = any(blocks_.get(("luma", b), ()))
                mb_f.nz = nz
                mb_f.cbp_luma = cbpv & 15
                grid, rids = [], []
                for g in rec.grid:
                    ent, ri = [], []
                    if g[0] >= 0:
                        ent.append((L0[g[0]].pid if L0[g[0]] is not None else "none", g[1], g[2]))
                        ri.append(g[0])
                    if g[3] >= 0:
                        ent.append((L1[g[3]].pid if L1[g[3]] is not None else "none", g[4], g[5]))
                        ri.append(g[3])
                    grid.append(tuple(ent))
                    rids.append(tuple(ri))
                mb_f.grid, mb_f.refidx = grid, rids
            elif rec.pcm is not None:
                mb_f.nz = [True] * 16
            if rec.pcm is not None:
                if pixels:
                    d = rec.pcm["samples"]
                    intraref.put(planes[0], 16 * mx, 16 * my, [list(d[16 * k:16 * k + 16]) for k in range(16)])
                    for p in range(2):
                        base = 256 + 64 * p
                        intraref.put(planes[1 + p], 8 * mx, 8 * my, [list(d[base + 8 * k:base + 8 * k + 8]) for k in range(8)])
                if record:
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=0, qp=qp, parts=[], slice=s.index)
            elif is_intra:
                if pixels:
                    blocks = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}
                    mb = dict(i16_mode=f["i16_mode"], chroma_pred=f["chroma_pred"], blocks=blocks)
                    if f["t8x8"] == 1:
                        raise Unsupported("Intra 8x8 (Phase 8)")
                    if f["name"] == "I_NxN":
                        syntax = [int(v) for v in f["modes"].split(",")]

                        def mo(which, a=a):
                            return modes.get(a - 1 if which == "A" else a - w)
                        m16 = intra4ref.derive_modes(syntax, a, mbav[0], mbav[1], mo)
                        modes[a] = m16
                        tr = transref.Tracker()
                        intra4ref.reconstruct_i4(planes, mx, my, mbav, m16, blocks, f["cbp"] & 15, qp, tr)
                        intraref.reconstruct_chroma(planes, mx, my, avail, mb, qp, offs, tr)
                    else:
                        intraref.reconstruct_i16(planes, mx, my, avail, mb, qp, offs)
                if record:
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=f["cbp"], qp=qp, parts=[], slice=s.index)
            else:
                pinfo = []
                if pixels:
                    pred, pinfo = inter_prediction(L0, L1, rec, mx, my, W, H, record, wt)
                    tr = transref.Tracker()
                    if f["name"] in ("P_Skip", "B_Skip"):
                        resy, resc = None, None
                    else:
                        resy, resc = inter_residual(rec, qp, offs, tr, s.slice_qp)
                    for k in range(16):
                        for j in range(16):
                            planes[0][16 * my + k][16 * mx + j] = transref.clip1(pred[0][k][j] + (resy[k][j] if resy else 0))
                    for pl in range(2):
                        for k in range(8):
                            for j in range(8):
                                planes[1 + pl][8 * my + k][8 * mx + j] = transref.clip1(
                                    pred[1 + pl][k][j] + (resc[pl][k][j] if resc else 0))
                if record:
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=cbpv, qp=qp, parts=pinfo, slice=s.index, grid=rec.grid,
                                     mvs=getattr(rec, "mvs", None))
    grid = bsref_b.picture_bs(bp)
    unfiltered = copy_planes(planes) if (record and pixels) else None
    if pixels:
        deblock_ref.filter_picture(planes, w, grid, qp_y, pcm_flag, adiv2, bdiv2, chroma_offsets)
    ref = Ref(planes)
    ref.pid = pic_no
    t0 = rs[0].info.tab
    dpb.mark(pic_no, ref, cur_fn, s0.nal_type == 5, t0["nal_ref_idc"], t0["lt_flag"], t0["mmco"], poc,
             any(x == 1 for x in slice_types))
    return dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, mbs=mbinfo, w_mbs=w, h_mbs=h, bspic=bp, bs=grid, pid=pic_no,
                unfiltered=unfiltered, slice_types=slice_types, lists=list_log, results=rs)


def write_yuv(pics, out):
    with open(out, "wb") as f:
        for p in pics:
            f.write(frame_bytes(p))


def frame_bytes(p):
    x0, y0, w, h = p["x0"], p["y0"], p["width"], p["height"]
    b = bytearray()
    for row in p["planes"][0][y0:y0 + h]:
        b += bytes(row[x0:x0 + w])
    cw, chh = (w + 1) // 2, (h + 1) // 2
    for pl in p["planes"][1:]:
        for row in pl[y0 // 2:y0 // 2 + chh]:
            b += bytes(row[x0 // 2:x0 // 2 + cw])
    return bytes(b)


if __name__ == "__main__":
    args = sys.argv[1:]
    n = None
    if "--frames" in args:
        i = args.index("--frames")
        n = int(args[i + 1])
        del args[i:i + 2]
    if len(args) != 2:
        print(__doc__)
        sys.exit(2)
    write_yuv(decode(args[0], upto=n), args[1])
