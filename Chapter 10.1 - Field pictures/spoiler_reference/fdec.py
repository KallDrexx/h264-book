"""Chapter 10.1 reference decoder for the intra-only field / frame streams of fgen.py (SPOILER).

It does NOT parse the bitstream: it takes the generator's manifest (the exact syntax elements the generator wrote: slice header
fields, macroblock types / modes / quantised coefficients) and does everything the decoder does AFTER parsing, for pictures that
are frames or fields:
  * picture boundary detection from slice header fields (7.4.1.2.4), including bottom_field_flag;
  * PicOrderCnt of every picture (8.2.1, types 0 / 1 / 2, frames and fields);
  * pairing of fields into frame stores (complementary field pairs; lone fields);
  * reconstruction of each field as an ordinary picture of PicHeightInMbs = FrameHeightInMbs / 2 macroblocks (intra prediction,
    inverse FIELD scan, transforms, deblocking with bS 3 on horizontal macroblock edges) and its interleaving into the frame store
    (top field = even lines, bottom field = odd lines; chroma likewise);
  * output order (ascending frame POC inside each IDR period) and the cropped yuv420p frames.
Because the generator's streams are decoded by ffmpeg with identical pixels (test_fields.py --self-test checks that for every
stream), the generator's CABAC / CAVLC field syntax (contexts, scans) is validated by ffmpeg, and this module validates the
reconstruction half.  The sample code (prediction, transform, deblocking filter) is Chapter 5-8's reference, copied alongside.
MUTANT = one of MUTANTS switches ONE deliberately wrong rule on.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import deblock_ref  # noqa: E402
import intra4ref  # noqa: E402
import intra8ref  # noqa: E402
import intraref  # noqa: E402
import transref  # noqa: E402
from common import yuvref  # noqa: E402
import cabacgen  # noqa: E402

MUTANT = None
MUTANTS = {
    "parity_swap": "top field written to the odd lines, bottom field to the even lines",
    "halves_layout": "each field stored as a contiguous half of the frame (top field on top, bottom field below) instead of interleaved",
    "chroma_parity_swap": "luma interleaved correctly, but the chroma lines of the two fields exchanged",
    "frame_scan4": "4x4 residual blocks of field pictures inverse-scanned with the frame zig-zag",
    "frame_scan8": "8x8 residual blocks of field pictures inverse-scanned with the frame zig-zag",
    "bs4_horizontal": "bS = 4 (instead of 3) on intra horizontal macroblock edges of field pictures",
    "bs3_vertical": "bS = 3 (instead of 4) on intra vertical macroblock edges of field pictures",
    "no_deblock": "deblocking skipped in field pictures",
    "crop_frame_units": "CropUnitY = 2 (frame_mbs_only_flag = 1 rule) although frame_mbs_only_flag = 0",
    "poc_bottom_delta_ignored": "delta_pic_order_cnt_bottom ignored: BottomFieldOrderCnt = TopFieldOrderCnt of a frame",
    "poc_no_msb": "PicOrderCntMsb always 0 (no wrap handling)",
    "poc2_second_field": "POC type 2: second field gets POC + 1",
    "poc1_no_top_to_bottom": "POC type 1: offset_for_top_to_bottom_field left out for bottom fields",
    "poc1_nonref_offset": "POC type 1: offset_for_non_ref_pic left out",
    "no_pairing": "every field starts its own frame store",
    "pair_same_parity": "a field pairs with the preceding field whatever its parity",
    "pair_ignore_frame_num": "opposite-parity fields pair even when frame_num differs",
    "idr_blocks_pairing": "the non-IDR second field of an IDR first field is not paired",
    "boundary_ignores_bottom_flag": "bottom_field_flag not used in new-picture detection",
    "picsize_frame": "PicSizeInMbs of a field picture taken as the frame's",
    "output_decode_order": "frames output in decoding order",
    "second_field_new_period": "an IDR first field's second field starts a new output period",
}


# ---------------------------------------------------------------- scans (Table 8-12 / 8-13, field column), (row, column)
FIELD4 = [(0, 0), (1, 0), (0, 1), (2, 0), (3, 0), (1, 1), (2, 1), (3, 1), (0, 2), (1, 2), (2, 2), (3, 2), (0, 3), (1, 3), (2, 3), (3, 3)]
_F8 = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (0, 3), (0, 4), (1, 2), (2, 0), (1, 3), (0, 5), (0, 6), (0, 7), (1, 4), (2, 1), (3, 0),
       (2, 2), (1, 5), (1, 6), (1, 7), (2, 3), (3, 1), (4, 0), (3, 2), (2, 4), (2, 5), (2, 6), (2, 7), (3, 3), (4, 1), (5, 0), (4, 2),
       (3, 4), (3, 5), (3, 6), (3, 7), (4, 3), (5, 1), (6, 0), (5, 2), (4, 4), (4, 5), (4, 6), (4, 7), (5, 3), (6, 1), (6, 2), (5, 4),
       (5, 5), (5, 6), (5, 7), (6, 3), (7, 0), (7, 1), (6, 4), (6, 5), (6, 6), (6, 7), (7, 2), (7, 3), (7, 4), (7, 5), (7, 6), (7, 7)]
FIELD8 = [(y, x) for x, y in _F8]          # _F8 above is typed as (x, y) from Table 8-13; the scan tables of transref are (row, column)
assert sorted(FIELD4) == sorted((i, j) for i in range(4) for j in range(4))
assert sorted(FIELD8) == sorted((i, j) for i in range(8) for j in range(8))
FRAME4, FRAME8 = list(transref.ZIGZAG), list(transref.ZIGZAG8)


def set_scan(field):
    transref.ZIGZAG[:] = FRAME4 if (not field or MUTANT == "frame_scan4") else FIELD4
    transref.ZIGZAG8[:] = FRAME8 if (not field or MUTANT == "frame_scan8") else FIELD8


# ---------------------------------------------------------------- headers -> pictures (7.4.1.2.4)
def slice_header_of(man, sl):
    p = man["pics"][sl["pic"]]
    return dict(first_mb=sl["first_mb"], frame_num=p["frame_num"], field=p["struct"] != "F", bottom=p["struct"] == "B",
                nal_ref_idc=p["ref_idc"], idr=p["idr"], idr_id=p["idr_id"], lsb=p["lsb"], dbot=p["dbot"], d0=p["d0"], d1=p["d1"])


def first_slice_of_new_picture(a, b, poc_type):
    """True if slice header b starts a new primary coded picture after a (7.4.1.2.4)."""
    if a["frame_num"] != b["frame_num"] or a["field"] != b["field"]:
        return True
    if MUTANT != "boundary_ignores_bottom_flag" and a["bottom"] != b["bottom"]:
        return True
    if (a["nal_ref_idc"] == 0) != (b["nal_ref_idc"] == 0) or a["idr"] != b["idr"]:
        return True
    if a["idr"] and a["idr_id"] != b["idr_id"]:
        return True
    if poc_type == 0 and (a["lsb"] != b["lsb"] or a["dbot"] != b["dbot"]):
        return True
    if poc_type == 1 and (a["d0"] != b["d0"] or a["d1"] != b["d1"]):
        return True
    return False


class PocState:
    """8.2.1 with explicit state, one call per picture in decoding order."""

    def __init__(self, sps):
        self.sps = sps
        self.prev_msb = self.prev_lsb = 0
        self.prev_fn_off = 0
        self.prev_fn = 0

    def picture(self, h):
        s = self.sps
        pt = s["poc_type"]
        field, bottom, ref = h["field"], h["bottom"], h["nal_ref_idc"] != 0
        if pt == 0:
            maxlsb = 1 << s["log2_lsb"]
            if h["idr"]:
                self.prev_msb = self.prev_lsb = 0
            lsb = h["lsb"]
            msb = self.prev_msb
            if lsb < self.prev_lsb and self.prev_lsb - lsb >= maxlsb // 2:
                msb += maxlsb
            elif lsb > self.prev_lsb and lsb - self.prev_lsb > maxlsb // 2:
                msb -= maxlsb
            if MUTANT == "poc_no_msb":
                msb = 0
            if ref:
                self.prev_msb, self.prev_lsb = msb, lsb
            top = None if bottom else msb + lsb
            bot = None
            if not field:
                bot = top if MUTANT == "poc_bottom_delta_ignored" else top + h["dbot"]
            elif bottom:
                bot = msb + lsb
            return top, bot
        maxfn = 1 << 4
        fn_off = 0 if h["idr"] else self.prev_fn_off + (maxfn if self.prev_fn > h["frame_num"] else 0)
        self.prev_fn_off, self.prev_fn = fn_off, h["frame_num"]
        if pt == 1:
            n = s["cycle_len"]
            absfn = fn_off + h["frame_num"] if n else 0
            if not ref and absfn > 0:
                absfn -= 1
            exp = 0
            if absfn > 0:
                cyc, inc = divmod(absfn - 1, n)
                exp = cyc * sum(s["offset_ref_frame"]) + sum(s["offset_ref_frame"][:inc + 1])
            if not ref and MUTANT != "poc1_nonref_offset":
                exp += s["offset_non_ref"]
            t2b = 0 if MUTANT == "poc1_no_top_to_bottom" else s["offset_t2b"]
            if not field:
                top = exp + h["d0"]
                return top, top + t2b + h["d1"]
            if not bottom:
                return exp + h["d0"], None
            return None, exp + t2b + h["d0"]
        t = 0 if h["idr"] else 2 * (fn_off + h["frame_num"]) - (0 if ref else 1)
        if not field:
            return t, t
        return (None, t) if bottom else (t, None)


# ---------------------------------------------------------------- reconstruction of one picture
def mb_avail(addr, w, owner):
    mx, my = addr % w, addr // w
    me = owner[addr]

    def ok(c, a2):
        return c and owner.get(a2) == me
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1), ok(mx > 0 and my > 0, addr - w - 1))


def i16_fields(mbt):
    v = mbt - 1
    return v % 4, (v // 4) % 3, 15 if v // 12 else 0


def decode_picture(man, pic_slices, is_field):
    sc = man["sc"]
    w = sc["mbw"]
    h = sc["map_h"] if is_field else 2 * sc["map_h"]
    planes = [[[128] * (16 * w) for _ in range(16 * h)], [[128] * (8 * w) for _ in range(8 * h)], [[128] * (8 * w) for _ in range(8 * h)]]
    set_scan(is_field)
    owner, qp_y, pcm, adiv, bdiv, t8s, idcs, sidx = {}, {}, {}, {}, {}, {}, {}, {}
    offs = (sc["cqo"], sc["cqo2"])
    for si, sl in enumerate(pic_slices):
        for k, m in enumerate(sl["mbs"]):
            a = sl["first_mb"] + k
            owner[a] = si
            mx, my = a % w, a // w
            av = mb_avail(a, w, owner)
            pcm[a] = m["k"] == "pcm"
            qp_y[a] = 0 if m["k"] == "pcm" else m["qp"]
            idc, ao, bo = sl["dbf"]
            adiv[a], bdiv[a], idcs[a], sidx[a] = ao, bo, idc, si
            t8s[a] = bool(m["t8"])
            if m["k"] == "pcm":
                d = m["samples"]
                intraref.put(planes[0], 16 * mx, 16 * my, [list(d[16 * r:16 * r + 16]) for r in range(16)])
                for p in range(2):
                    base = 256 + 64 * p
                    intraref.put(planes[1 + p], 8 * mx, 8 * my, [list(d[base + 8 * r:base + 8 * r + 8]) for r in range(8)])
                continue
            blocks = {(kind, idx): c for kind, idx, c in m.get("blocks", []) if c is not None}
            tr = transref.Tracker()
            if m["k"] == "i16":
                mode, ccbp, lcbp = i16_fields(m["mbt"])
                mb = dict(i16_mode=mode, chroma_pred=m["cpred"], blocks=blocks)
                intraref.reconstruct_i16(planes, mx, my, (av[0], av[1], av[3]), mb, m["qp"], offs)
            else:
                mb = dict(chroma_pred=m["cpred"], blocks=blocks)
                am = m["amodes"]
                if m["k"] == "i8":
                    modes = [am[(2 * (b & 1), 2 * (b >> 1))] for b in range(4)]
                    intra8ref.reconstruct_i8(planes, mx, my, av, modes, blocks, m["cbp_l"], m["qp"], tr)
                else:
                    modes = [am[cabacgen.CELL_ORDER[i]] for i in range(16)]
                    intra4ref.reconstruct_i4(planes, mx, my, av, modes, blocks, m["cbp_l"], m["qp"], tr)
                intraref.reconstruct_chroma(planes, mx, my, (av[0], av[1], av[3]), mb, m["qp"], offs, tr)
    n = w * h
    deblock = not (is_field and MUTANT == "no_deblock")
    grid = []
    for a in range(n):
        mx, my = a % w, a // w
        v, hh = [0] * 16, [0] * 16
        idc = idcs[a]
        if idc != 1:
            for e in range(4):
                if t8s[a] and e in (1, 3):
                    continue
                for s in range(4):
                    if e == 0:
                        if mx > 0 and (idc != 2 or sidx[a - 1] == sidx[a]):
                            v[s] = 3 if (is_field and MUTANT == "bs3_vertical") else 4
                        if my > 0 and (idc != 2 or sidx[a - w] == sidx[a]):
                            hh[s] = 4 if (not is_field or MUTANT == "bs4_horizontal") else 3
                    else:
                        v[4 * e + s] = hh[4 * e + s] = 3
        grid.append(dict(v=v, h=hh))
    if deblock:
        deblock_ref.filter_picture(planes, w, grid, [qp_y[a] for a in range(n)], [pcm[a] for a in range(n)],
                                   [adiv[a] for a in range(n)], [bdiv[a] for a in range(n)], offs)
    return planes


# ---------------------------------------------------------------- the decoder
def decode(man):
    """-> (dump_lines, frames) ; frames = list (OUTPUT order) of dict(bytes, kind 'F'|'TB'|'T'|'B', store)."""
    sc = man["sc"]
    sps = dict(poc_type=sc["poc_type"], log2_lsb=sc["log2_lsb"], cycle_len=sc["cycle_len"], offset_ref_frame=sc["offset_ref_frame"],
               offset_non_ref=sc["offset_non_ref"], offset_t2b=sc["offset_t2b"])
    # 1. pictures from slices
    pics = []
    prev = None
    for sl in man["slices"]:
        hd = slice_header_of(man, sl)
        if prev is None or first_slice_of_new_picture(prev, hd, sc["poc_type"]):
            pics.append(dict(hdr=hd, slices=[]))
        pics[-1]["slices"].append(sl)
        prev = hd
    ps = PocState(sps)
    stores = []
    out_period = 0
    open_first = None
    w, mh = sc["mbw"], sc["map_h"]
    dump = []
    for n, pc in enumerate(pics):
        hd = pc["hdr"]
        field = hd["field"]
        top, bot = ps.picture(hd)
        planes = decode_picture(man, pc["slices"], field)
        pairing_poc2 = MUTANT == "poc2_second_field" and sc["poc_type"] == 2 and field
        if hd["idr"]:
            out_period += 1
        # pairing (3.30 / 3.31, 7.4.3 frame_num semantics)
        second = False
        if field and open_first is not None and not hd["idr"]:
            f = open_first
            ok = (f["bottom"] != hd["bottom"]) and (f["frame_num"] == hd["frame_num"]) and ((f["nal_ref_idc"] != 0) == (hd["nal_ref_idc"] != 0))
            if MUTANT == "pair_same_parity":
                ok = f["frame_num"] == hd["frame_num"]
            if MUTANT == "pair_ignore_frame_num":
                ok = f["bottom"] != hd["bottom"] and ((f["nal_ref_idc"] != 0) == (hd["nal_ref_idc"] != 0))
            if MUTANT == "idr_blocks_pairing" and f["idr"]:
                ok = False
            if MUTANT == "no_pairing":
                ok = False
            second = ok
        if pairing_poc2 and second:
            top, bot = (None if top is None else top + 1), (None if bot is None else bot + 1)
        if second:
            st = stores[-1]
        else:
            st = dict(id=len(stores), top=None, bot=None, period=out_period, seq=len(stores),
                      planes=[[[128] * (16 * w) for _ in range(16 * 2 * mh)], [[128] * (8 * w) for _ in range(8 * 2 * mh)],
                              [[128] * (8 * w) for _ in range(8 * 2 * mh)]], fields="")
            stores.append(st)
        if MUTANT == "second_field_new_period" and second and open_first["idr"]:
            st["period"] += 1
        # interleave
        if not field:
            for pl in range(3):
                for y, row in enumerate(planes[pl]):
                    st["planes"][pl][y] = list(row)
            st["fields"] = "F"
        else:
            par = 1 if hd["bottom"] else 0
            for pl in range(3):
                p = par
                if MUTANT == "parity_swap" or (MUTANT == "chroma_parity_swap" and pl > 0):
                    p = 1 - par
                for y, row in enumerate(planes[pl]):
                    yy = (p * len(planes[pl]) + y) if MUTANT == "halves_layout" else 2 * y + p
                    st["planes"][pl][yy] = list(row)
            st["fields"] += "B" if hd["bottom"] else "T"
        if top is not None:
            st["top"] = top
        if bot is not None:
            st["bot"] = bot
        pic_poc = min(x for x in (top, bot) if x is not None)
        mbs = w * (mh if field else 2 * mh)
        if MUTANT == "picsize_frame":
            mbs = w * 2 * mh
        dump.append(f"PIC {n} struct={'F' if not field else 'B' if hd['bottom'] else 'T'} frame_num={hd['frame_num']} "
                    f"idr={int(hd['idr'])} ref={int(hd['nal_ref_idc'] != 0)} slices={len(pc['slices'])} mbs={mbs} poc={pic_poc} "
                    f"top={'-' if top is None else top} bot={'-' if bot is None else bot} store={st['id']} second={int(second)}")
        open_first = hd if (field and not second) else None
    # output order
    def key(s):
        poc = min(x for x in (s["top"], s["bot"]) if x is not None)
        return (s["seq"],) if MUTANT == "output_decode_order" else (s["period"], poc, s["seq"])
    frames = []
    cl, cr, ct, cb = sc["crop"]
    fmo = 1 if MUTANT == "crop_frame_units" else 0
    cw, ch, x0, y0 = yuvref.cropped_size(w, mh, fmo, cl, cr, ct, cb)
    if fmo:
        ch = 16 * 2 * mh - 2 * (ct + cb)
    for k, st in enumerate(sorted(stores, key=key)):
        b = bytearray()
        for row in st["planes"][0][y0:y0 + ch]:
            b += bytes(row[x0:x0 + cw])
        for pl in st["planes"][1:]:
            for row in pl[y0 // 2:y0 // 2 + (ch + 1) // 2]:
                b += bytes(row[x0 // 2:x0 // 2 + (cw + 1) // 2])
        kind = st["fields"] if len(st["fields"]) == 1 else "TB" if st["fields"] in ("TB", "BT") else st["fields"]
        poc = min(x for x in (st["top"], st["bot"]) if x is not None)
        frames.append(dict(bytes=bytes(b), kind=kind, store=st["id"], top=st["top"], bot=st["bot"], poc=poc, size=(cw, ch),
                           order=st["fields"]))
        dump.append(f"FRAME {k} store={st['id']} fields={st['fields']} top={'-' if st['top'] is None else st['top']} "
                    f"bot={'-' if st['bot'] is None else st['bot']} poc={poc}")
    set_scan(False)
    return dump, frames


if __name__ == "__main__":
    import fgen
    sc = fgen.scenarios()[sys.argv[1]]
    data, man = fgen.build(sc)
    dump, frames = decode(man)
    print("\n".join(dump))
