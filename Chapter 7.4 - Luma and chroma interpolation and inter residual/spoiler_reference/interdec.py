#!/usr/bin/env python3
"""SPOILER: complete reference decoder for the Chapter 7.4 scope: CAVLC I and P pictures (frames, 8-bit 4:2:0, no weighted
prediction, no 8x8 transform, no reference list modification / MMCO) WITHOUT deblocking.  Do not read before your own decoder
passes.

Syntax comes from common/inter_ref.py (Chapter 7.1), vectors from mvpred_ref.py (Chapter 7.3, copied here), the intra path is
Chapter 5's code (intraref.py / intra4ref.py / transref.py, copied), the interpolation is interp_ref.py.  Only the glue is here:
reference lists (default P list, sliding window), prediction block by block, the inter residual, storage.

usage: interdec.py <in.ts|.264> <out.yuv>        (decodes everything; --frames N stops after N pictures)
The decoder also records, for diagnosis, what each macroblock's prediction looked like (decode(..., record=True)).

MUTANT switches on ONE deliberately wrong rule for the harness self-test (interp_ref.MUTANT holds the interpolation ones).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import inter_ref as ir, yuvref  # noqa: E402
import interp_ref  # noqa: E402
import intraref  # noqa: E402
import intra4ref  # noqa: E402
import mvpred_ref  # noqa: E402
import transref  # noqa: E402

MUTANT = None
DECODER_MUTANTS = {
    "inter_dc_prescaled": "inter luma blocks use the Intra16x16 rule: coefficient 0 is not scaled (as if it came from the DC transform)",
    "no_final_clip": "pred + residual is not clipped to 0..255 (wraps modulo 256)",
    "qp_slice_only": "inter residual dequantised with the slice QP instead of the running QP_Y",
    "chroma_qp_luma": "chroma residual dequantised with QP_Y instead of QP_C",
    "ref_list_ascending": "default P list sorted by ascending PicNum",
    "ref_always_zero": "ref_idx ignored: always the first list entry",
    "residual_before_skip": "P_Skip macroblocks get the residual of the previous coded macroblock added",
}


class Unsupported(Exception):
    pass


def new_planes(w_mbs, h_mbs, fill=128):
    return ([[fill] * (16 * w_mbs) for _ in range(16 * h_mbs)], [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)],
            [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)])


def check_supported(s):
    h, p, sps = s.hdr, s.pps, s.sps
    if s.slice_type not in (0, 2):
        raise Unsupported("B slices (7.8) / SP / SI slices are not handled in this chapter")
    if s.entropy:
        raise Unsupported("CABAC (Phase 8)")
    if h.get("field_pic_flag", 0) or (sps.get("mb_adaptive_frame_field_flag", 0) and not sps.get("frame_mbs_only_flag", 1)):
        raise Unsupported("field / MBAFF pictures (Phase 10)")
    if s.transform_8x8_mode:
        raise Unsupported("transform_8x8_mode_flag = 1 (Phase 8)")
    if h.get("disable_deblocking_filter_idc", 0) != 1:
        raise Unsupported("this stream uses the deblocking filter (Phase 6/7.5): decode it with deblocking in 7.5")
    if s.slice_type == 0 and (p.get("weighted_pred_flag", 0)):
        raise Unsupported("weighted prediction (7.7)")
    if h.get("ref_pic_list_modification_flag_l0", 0) or h.get("adaptive_ref_pic_marking_mode_flag", 0):
        raise Unsupported("reference list modification / MMCO (7.6)")


class Dpb:
    """Short-term frame references with the sliding window (8.2.5.3) and the default P list (8.2.4.2.1)."""

    def __init__(self):
        self.refs = []                       # (frame_num, pic)

    def list0(self, cur_fn, max_fn, active):
        def wrap(fn):
            return fn - max_fn if fn > cur_fn else fn
        order = sorted(self.refs, key=lambda r: wrap(r[0]), reverse=(MUTANT != "ref_list_ascending"))
        lst = [r[1] for r in order][:active]
        if len(lst) < active:
            raise Unsupported(f"reference list has {len(lst)} entries but num_ref_idx_l0_active is {active}")
        return lst

    def mark(self, frame_num, pic, idr, max_refs, ref_idc, max_fn, cur_fn):
        if idr:
            self.refs = []
        if not ref_idc:
            return
        if not idr and len(self.refs) >= max(max_refs, 1):
            def wrap(fn):
                return fn - max_fn if fn > cur_fn else fn
            self.refs.remove(min(self.refs, key=lambda r: wrap(r[0])))
        self.refs.append((frame_num, pic))


def mb_neighbours(addr, w, owner, slice_idx, intra, constrained):
    """(A, B, C, D) availability for intra sample prediction (6.4.x): in the picture, same slice, already decoded, and with
    constrained_intra_pred_flag = 1 not an Inter macroblock."""
    mx, my = addr % w, addr // w

    def ok(cond, a2):
        if not cond or owner.get(a2) != slice_idx:
            return False
        return not (constrained and intra.get(a2) is False)
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1),
            ok(mx > 0 and my > 0, addr - w - 1))


def inter_prediction(refpics, rec, mx, my, W, H, record):
    """Prediction of the whole macroblock: luma 16x16, Cb 8x8, Cr 8x8, block by block."""
    pred = ([[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(8)], [[0] * 8 for _ in range(8)])
    parts = rec.parts or [dict(x=0, y=0, w=16, h=16, ref=0)]
    info = []
    for p in parts:
        x, y, w, h = p["x"], p["y"], p["w"], p["h"]
        _, mvx, mvy = rec.grid[(y >> 2) * 4 + (x >> 2)]
        ref = refpics[0 if MUTANT == "ref_always_zero" else p["ref"]]
        X, Y = 16 * mx + x, 16 * my + y
        blk = interp_ref.luma_block(ref[0], W, H, X, Y, w, h, mvx, mvy)
        for r in range(h):
            pred[0][y + r][x:x + w] = blk[r]
        for pl in (1, 2):
            cb = interp_ref.chroma_block(ref[pl], W // 2, H // 2, X // 2, Y // 2, w // 2, h // 2, mvx, mvy)
            for r in range(h // 2):
                pred[pl][y // 2 + r][x // 2:x // 2 + w // 2] = cb[r]
        if record:
            xi, yi = X + (mvx >> 2), Y + (mvy >> 2)
            info.append(dict(x=x, y=y, w=w, h=h, ref=p["ref"], mv=(mvx, mvy), xf=mvx & 3, yf=mvy & 3,
                             outside=xi < 0 or yi < 0 or xi + w > W or yi + h > H,
                             window_outside=xi - 2 < 0 or yi - 2 < 0 or xi + w + 3 > W or yi + h + 3 > H))
    return pred, info


def inter_residual(rec, qp, offs, tr, slice_qp):
    """-> (luma 16x16 residual or None, [Cb 8x8, Cr 8x8] residual or None)."""
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
                # inter macroblocks: all 16 coefficients, DC included, are scaled like any other coefficient (8.5.12.1)
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


def decode(path, upto=None, record=False, workdir=None):
    """-> list of dict(width, height, x0, y0, planes, mbs=[per macroblock info when record], slice_types)."""
    td = workdir or tempfile.mkdtemp(prefix="interdec_")
    infos = ir.load(path, td)
    for s in infos:
        check_supported(s)
    results = ir.parse_all(infos)
    for r in results:
        if r.status != "complete":
            raise Unsupported(f"slice {r.info.index}: parser status {r.status}: {r.error or r.note}")
    mvpred_ref.fill_results(results)
    pics = {}
    for r in results:
        pics.setdefault(r.info.picture, []).append(r)
    dpb, out = Dpb(), []
    interp_ref.MUTANT = MUTANT if MUTANT in INTERP_MUTANTS else None
    transref.MUTANT = None
    try:
        for pi in sorted(pics):
            if upto is not None and pi >= upto:
                break
            out.append(decode_picture(pics[pi], dpb, record))
    finally:
        interp_ref.MUTANT = None
    return out


INTERP_MUTANTS = ("taps", "taps_sign", "b_round", "j_from_rounded", "j_from_clipped_b1", "j_round", "j_shift", "avg_round_down",
                  "diag_pair", "diag_pair_r", "clamp_off_by_one", "no_clamp", "mv_int_round", "chroma_mv_units",
                  "chroma_swap_frac", "chroma_swap_bc", "chroma_round")


def decode_picture(rs, dpb, record):
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
    max_fn = 1 << (sps["log2_max_frame_num_minus4"] + 4)
    cur_fn = s0.hdr["frame_num"]
    last_res = None
    for r in rs:
        s = r.info
        constrained = bool(s.pps.get("constrained_intra_pred_flag", 0))
        o1 = s.pps["chroma_qp_index_offset"]
        offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
        refpics = dpb.list0(cur_fn, max_fn, s.num_ref_idx_l0_active) if s.slice_type == 0 else []
        for rec in r.mbs:
            a = rec.addr
            owner[a] = s.index
            f = rec.f
            mx, my = a % w, a // w
            is_intra = not f["name"].startswith("P_")
            intra[a] = is_intra
            mbav = mb_neighbours(a, w, owner, s.index, intra, constrained)
            avail = (mbav[0], mbav[1], mbav[3])
            qp = f["qp"]
            if rec.pcm is not None:
                d = rec.pcm["samples"]
                intraref.put(planes[0], 16 * mx, 16 * my, [list(d[16 * k:16 * k + 16]) for k in range(16)])
                for p in range(2):
                    base = 256 + 64 * p
                    intraref.put(planes[1 + p], 8 * mx, 8 * my, [list(d[base + 8 * k:base + 8 * k + 8]) for k in range(8)])
                if record:
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=0, qp=qp, parts=[])
                continue
            if is_intra:
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
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=f["cbp"], qp=qp, parts=[])
                continue
            # ---- inter macroblock (P_Skip included): whole-macroblock prediction, then the residual per 4x4 block
            pred, pinfo = inter_prediction(refpics, rec, mx, my, W, H, record)
            tr = transref.Tracker()
            if f["name"] == "P_Skip":
                resy, resc = (last_res if MUTANT == "residual_before_skip" and last_res else (None, None))
            else:
                resy, resc = inter_residual(rec, qp, offs, tr, s.slice_qp)
                last_res = (resy, resc)
            lim = (lambda v: v & 255) if MUTANT == "no_final_clip" else transref.clip1  # noqa: E731
            for k in range(16):
                for j in range(16):
                    v = pred[0][k][j] + (resy[k][j] if resy else 0)
                    planes[0][16 * my + k][16 * mx + j] = lim(v)
            for pl in range(2):
                for k in range(8):
                    for j in range(8):
                        v = pred[1 + pl][k][j] + (resc[pl][k][j] if resc else 0)
                        planes[1 + pl][8 * my + k][8 * mx + j] = lim(v)
            if record:
                mbinfo[a] = dict(addr=a, name=f["name"], cbp=(f["cbp"] if f["cbp"] != "-" else 0), qp=qp, parts=pinfo)
    dpb.mark(cur_fn, planes, s0.nal_type == 5, sps["max_num_ref_frames"], s0.hdr.get("nal_ref_idc", 1), max_fn, cur_fn)
    return dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, mbs=mbinfo, w_mbs=w, h_mbs=h)


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
