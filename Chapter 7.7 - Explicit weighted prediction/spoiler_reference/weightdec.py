#!/usr/bin/env python3
"""SPOILER: complete reference decoder for the Chapter 7.7 scope: CAVLC I and P pictures (frames, 8-bit 4:2:0, no 8x8 transform) with
the deblocking filter, FULL reference management (Chapter 7.6: list modification, long-term pictures, MMCO, gaps) and EXPLICIT WEIGHTED
PREDICTION (8.4.2.3, pred_weight_table).  Do not read before your own decoder passes.

It is Chapter 7.5's decoder (interdec.py) plus: refmgmt_ref.py instead of the simple DPB, slicehdr_ref.py for the slice-header tables
(modification commands, MMCO, pred_weight_table) and the weighted-prediction step in inter_prediction().

usage: weightdec.py <in.ts|.264> <out.yuv>        (decodes everything; --frames N stops after N pictures)
The decoder also records, for diagnosis, what each macroblock's prediction looked like (decode(..., record=True)): the reference
picture, ref_idx, vector and the weights applied to every partition.  decode(..., pixels=False) skips sample work.

MUTANT switches on ONE deliberately wrong rule for the harness self-test (WEIGHT_MUTANTS lists the new ones).
"""
import os
import sys
import tempfile
import os as _os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import inter_ref as ir, yuvref  # noqa: E402
import bsref_inter  # noqa: E402
import deblock_ref  # noqa: E402
import interp_ref  # noqa: E402
import intraref  # noqa: E402
import intra4ref  # noqa: E402
import mvpred_ref  # noqa: E402
import refmgmt_ref  # noqa: E402
import slicehdr_ref  # noqa: E402
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
    # --- Chapter 7.5: wrong places for the loop filter in the decoding loop, and chroma bS
    "ref_unfiltered": "the picture stored as reference is the UNFILTERED reconstruction (the output is filtered)",
    "intra_from_filtered": "each macroblock is deblocked right after it is reconstructed, so intra prediction sees filtered samples",
    "chroma_bs_own": "chroma edges get their own bS: raised to 2 when the chroma CBP of either macroblock is non-zero",
    "skip_qp_slice": "skipped / CBP-0 macroblocks use the slice QP (not the running QP_Y) as qPp / qPq",
    "no_deblock_in_p": "deblocking is skipped for P slices (only I slices are filtered)",
}
WEIGHT_MUTANTS = {
    "round_none": "no rounding term: (pred * w >> logWD) + o  instead of ((pred * w + 2^(logWD-1)) >> logWD) + o",
    "offset_in_shift": "the offset is added before the shift: (pred * w + 2^(logWD-1) + o) >> logWD",
    "flag0_weight_one": "weight 1 (not 2^logWD) and offset 0 when luma_weight_l0_flag / chroma_weight_l0_flag is 0",
    "chroma_luma_weights": "Cb and Cr use the luma weight and offset",
    "chroma_luma_denom": "chroma uses luma_log2_weight_denom as logWD",
    "ref_idx_zero_weights": "the weights of ref_idx 0 are used for every partition",
    "ref_idx_off_by_one": "the weights of entry ref_idx + 1 (wrapping) are used",
    "weights_by_picture": "the weights are looked up at the FIRST list position of the referenced picture instead of at ref_idx",
    "skip_unweighted": "P_Skip macroblocks are predicted without weights",
    "no_clip": "no Clip1 after weighting (values wrap modulo 256)",
    "logwd0_no_offset": "when logWD = 0 the offset is forgotten",
    "weights_before_interp": "the weights are applied to the reference samples, before the interpolation",
    "weight_abs": "negative weights are used as their absolute value",
    "cb_cr_swapped": "Cb uses Cr's weight and offset and vice versa",
    "chroma_flag_is_luma_flag": "chroma weights are only used when the LUMA flag of the entry is set",
    "explicit_ignored": "weighted_pred_flag ignored: default prediction everywhere",
    "no_list_mod": "ref_pic_list_modification ignored (initial list used)",
    "bs_by_refidx": "deblocking bS compares ref_idx numbers instead of pictures",
}
BS_MUTANTS = bsref_inter.MUTANTS


STATS = None          # a collections.Counter: coverage of the weighted-prediction rules (self-test)


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
    if (p.get("weighted_bipred_idc", 0)) and s.slice_type == 1:
        raise Unsupported("weighted bi-prediction (7.8, 7.9)")


class Dpb:
    """Chapter 7.6's reference management (refmgmt_ref.Dpb) plus the decoded pictures it manages."""

    def __init__(self, sps):
        self.d = refmgmt_ref.Dpb(sps["max_num_ref_frames"], sps["log2_max_frame_num_minus4"] + 4,
                                 bool(sps.get("gaps_in_frame_num_allowed_flag", 0)))
        self.pix = {}
        self.last = None

    def start_picture(self, frame_num, idr, blank):
        """Gap process (8.2.5.2): non-existing frames get a copy of the previous picture (never referenced by a conforming stream)."""
        try:
            labels = self.d.check_frame_num(frame_num, idr)
        except refmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        for lab in labels:
            self.pix[lab] = self.last or blank

    def list0(self, cur_fn, active, mods):
        if MUTANT == "no_list_mod":
            mods = []
        try:
            slots = self.d.build_list(cur_fn, active, mods)
        except refmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        return [self.pix[sl[0]] if sl is not None else None for sl in slots]

    def mark(self, label, pic, frame_num, idr, ref_idc, lt_flag, mmcos):
        try:
            self.d.finish_picture(label, frame_num, idr, ref_idc, lt_flag, mmcos)
        except refmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        if ref_idc:
            self.pix[label] = pic
            self.last = pic
        live = {f.label for f in self.d.frames}
        for k in list(self.pix):
            if k not in live:
                del self.pix[k]


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


def weight_sample(v, w, o, log_wd, clip=True):
    """8.4.2.3 explicit weighting of one predicted sample, single list (formulas 8-448 / 8-449)."""
    if MUTANT == "round_none":
        t = ((v * w) >> log_wd) + o if log_wd >= 1 else v * w + o
    elif MUTANT == "offset_in_shift":
        t = ((v * w + (1 << (log_wd - 1)) + o) >> log_wd) if log_wd >= 1 else v * w + o
    elif MUTANT == "logwd0_no_offset" and log_wd == 0:
        t = v * w
    elif log_wd >= 1:
        t = ((v * w + (1 << (log_wd - 1))) >> log_wd) + o
    else:
        t = v * w + o
    if STATS is not None:
        if t < 0:
            STATS["clip at 0"] += 1
        elif t > 255:
            STATS["clip at 255"] += 1
    if MUTANT == "no_clip":
        return t & 255
    return min(255, max(0, t))


def pick_weights(wt, ref_idx, refpics, plane, flag_pair=None):
    """(weight, offset, logWD) of one partition and plane (0 Y, 1 Cb, 2 Cr)."""
    n = len(wt["entries"])
    idx = ref_idx
    if MUTANT == "ref_idx_zero_weights":
        idx = 0
    elif MUTANT == "ref_idx_off_by_one":
        idx = (ref_idx + 1) % n
    elif MUTANT == "weights_by_picture":
        idx = next(i for i, p in enumerate(refpics) if p is refpics[ref_idx])
    e = wt["entries"][idx]
    lf, cf = wt["flags"][idx]
    pl = plane
    if MUTANT == "cb_cr_swapped" and plane:
        pl = 3 - plane
    if pl == 0 or (MUTANT == "chroma_luma_weights"):
        w, o = e["wy"], e["oy"]
    else:
        w, o = (e["wcb"], e["ocb"]) if pl == 1 else (e["wcr"], e["ocr"])
    log_wd = wt["denom_y"] if (plane == 0 or MUTANT == "chroma_luma_denom") else wt["denom_c"]
    if MUTANT == "flag0_weight_one":
        if (plane == 0 and not lf) or (plane and not cf):
            w, o = 1, 0
    if MUTANT == "chroma_flag_is_luma_flag" and plane and not lf:
        w, o = 1 << log_wd, 0
    if MUTANT == "weight_abs":
        w = abs(w)
    if STATS is not None and MUTANT is None:
        STATS[f"logWD {log_wd} {'luma' if plane == 0 else 'chroma'}"] += 1
        if w < 0:
            STATS["negative weight"] += 1
        if o < 0:
            STATS["negative offset"] += 1
        if o > 0:
            STATS["positive offset"] += 1
        if plane == 0:
            STATS[f"flags luma={lf} chroma={cf}"] += 1
            if idx != next(i for i, p in enumerate(refpics) if p is refpics[idx]):
                STATS["duplicate list entry used"] += 1
            if idx > 0:
                STATS["ref_idx > 0"] += 1
    return w, o, log_wd


def weighted_ref(ref, wt, ref_idx, refpics):
    """MUTANT weights_before_interp: the reference picture with the weights applied to its samples."""
    out = []
    for pl in range(3):
        w, o, lw = pick_weights(wt, ref_idx, refpics, pl)
        out.append([[weight_sample(v, w, o, lw) for v in row] for row in ref[pl]])
    return tuple(out)


def inter_prediction(refpics, rec, mx, my, W, H, record, wt=None, is_skip=False):
    """Prediction of the whole macroblock: luma 16x16, Cb 8x8, Cr 8x8, block by block.  wt = the slice's weight table (explicit
    weighted prediction, 8.4.2.3) or None (default prediction)."""
    pred = ([[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(8)], [[0] * 8 for _ in range(8)])
    parts = rec.parts or [dict(x=0, y=0, w=16, h=16, ref=0)]
    info = []
    if MUTANT in ("explicit_ignored",) or (MUTANT == "skip_unweighted" and is_skip):
        wt = None
    if STATS is not None and is_skip and wt:
        STATS["P_Skip with weights"] += 1
    for p in parts:
        x, y, w, h = p["x"], p["y"], p["w"], p["h"]
        _, mvx, mvy = rec.grid[(y >> 2) * 4 + (x >> 2)]
        ri = 0 if MUTANT == "ref_always_zero" else p["ref"]
        ref = refpics[ri]
        if ref is None:
            raise Unsupported(f"ref_idx {ri} names 'no reference picture'")
        X, Y = 16 * mx + x, 16 * my + y
        src = weighted_ref(ref, wt, ri, refpics) if (wt and MUTANT == "weights_before_interp") else ref
        blk = interp_ref.luma_block(src[0], W, H, X, Y, w, h, mvx, mvy)
        used = None
        if wt and MUTANT != "weights_before_interp":
            wy, oy, lw = pick_weights(wt, ri, refpics, 0)
            blk = [[weight_sample(v, wy, oy, lw) for v in row] for row in blk]
            used = [(wy, oy, lw)]
        for r in range(h):
            pred[0][y + r][x:x + w] = blk[r]
        for pl in (1, 2):
            cb = interp_ref.chroma_block(src[pl], W // 2, H // 2, X // 2, Y // 2, w // 2, h // 2, mvx, mvy)
            if wt and MUTANT != "weights_before_interp":
                wc, oc, lw = pick_weights(wt, ri, refpics, pl)
                cb = [[weight_sample(v, wc, oc, lw) for v in row] for row in cb]
                used.append((wc, oc, lw))
            for r in range(h // 2):
                pred[pl][y // 2 + r][x // 2:x // 2 + w // 2] = cb[r]
        if record:
            xi, yi = X + (mvx >> 2), Y + (mvy >> 2)
            info.append(dict(x=x, y=y, w=w, h=h, ref=p["ref"], mv=(mvx, mvy), xf=mvx & 3, yf=mvy & 3, pid=ref.pid,
                             weights=used,
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


class Ref(tuple):
    """A stored picture: (Y, Cb, Cr) plane lists plus .pid, the identity used by the bS motion rule (8.7.2.1: 'different reference
    pictures' means different pictures, whatever their ref_idx)."""
    pid = None


def decode(path, upto=None, record=False, workdir=None, pixels=True):
    """-> list of dict(width, height, x0, y0, planes, mbs=[per macroblock info when record], bspic=bsref_inter picture facts, bs=grid).
    pixels=False skips all sample work (prediction, residual, filtering): only the facts bS needs are collected (fast)."""
    td = workdir or tempfile.mkdtemp(prefix="interdec_")
    infos = ir.load(path, td)
    for s in infos:
        check_supported(s)
    tables = slicehdr_ref.load(_os.path.join(td, "ref_trace.txt"))
    for s, t in zip(infos, tables):
        s.tab = t
    results = ir.parse_all(infos)
    for r in results:
        if r.status != "complete":
            raise Unsupported(f"slice {r.info.index}: parser status {r.status}: {r.error or r.note}")
    mvpred_ref.fill_results(results)
    pics = {}
    for r in results:
        pics.setdefault(r.info.picture, []).append(r)
    dpb, out = Dpb(infos[0].sps), []
    interp_ref.MUTANT = MUTANT if MUTANT in INTERP_MUTANTS else None
    bsref_inter.MUTANT = MUTANT if MUTANT in BS_MUTANTS else None
    transref.MUTANT = None
    try:
        for n, pi in enumerate(sorted(pics)):
            if upto is not None and pi >= upto:
                break
            out.append(decode_picture(pics[pi], dpb, record, n, pixels))
    finally:
        interp_ref.MUTANT = None
        bsref_inter.MUTANT = None
    return out


INTERP_MUTANTS = ("taps", "taps_sign", "b_round", "j_from_rounded", "j_from_clipped_b1", "j_round", "j_shift", "avg_round_down",
                  "diag_pair", "diag_pair_r", "clamp_off_by_one", "no_clamp", "mv_int_round", "chroma_mv_units",
                  "chroma_swap_frac", "chroma_swap_bc", "chroma_round")


def blank_ref(planes):
    r = Ref(copy_planes(planes))
    r.pid = "blank"
    return r


def copy_planes(planes):
    return tuple([list(r) for r in pl] for pl in planes)


def decode_picture(rs, dpb, record, pic_no=0, pixels=True):
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
    max_fn = 1 << (sps["log2_max_frame_num_minus4"] + 4)
    cur_fn = s0.hdr["frame_num"]
    last_res = None
    o1 = s0.pps["chroma_qp_index_offset"]
    chroma_offsets = (o1, s0.pps.get("second_chroma_qp_index_offset", o1))
    # ---- facts for the deblocking pass (bsref_inter.Pic)
    bp = bsref_inter.Pic(w, h)
    qp_y, pcm_flag, adiv2, bdiv2 = [0] * (w * h), [False] * (w * h), [0] * (w * h), [0] * (w * h)
    slice_qp_of = {}
    filtered_each = MUTANT == "intra_from_filtered"
    for r in rs:
        s = r.info
        hd = s.hdr
        bp.slices[s.index] = bsref_inter.SliceFacts(hd.get("disable_deblocking_filter_idc", 0), s.slice_type)
        constrained = bool(s.pps.get("constrained_intra_pred_flag", 0))
        offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
        tab = s.tab
        wt = tab["wt"] if s.slice_type == 0 else None
        refpics = dpb.list0(cur_fn, s.num_ref_idx_l0_active, tab["mods"]) if s.slice_type == 0 else []
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
            mb_f = bsref_inter.Mb(s.index, intra=is_intra or rec.pcm is not None, pcm=rec.pcm is not None,
                                  skip=f["name"] == "P_Skip")
            bp.mbs[a] = mb_f
            adiv2[a], bdiv2[a] = hd.get("slice_alpha_c0_offset_div2", 0), hd.get("slice_beta_offset_div2", 0)
            pcm_flag[a] = rec.pcm is not None
            qp_y[a] = 0 if rec.pcm is not None else qp
            if MUTANT == "skip_qp_slice" and not pcm_flag[a] and (f["name"] == "P_Skip" or f["cbp"] in (0, "-")) and not f["name"].startswith("I_16x16"):
                qp_y[a] = s.slice_qp
            cbpv = f["cbp"] if f["cbp"] != "-" else 0
            mb_f.chroma_cbp = (cbpv >> 4) if not is_intra else 0
            if not is_intra:
                blocks_ = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}
                nz = [False] * 16
                for b in range(16):                    # luma4x4BlkIdx b -> raster position of the 4x4 block
                    bx, by = transref.BLK_XY[b]
                    nz[by * 4 + bx] = any(blocks_.get(("luma", b), ()))
                mb_f.nz = nz
                mb_f.cbp_luma = cbpv & 15
                mb_f.grid = [(g[0] if MUTANT == "bs_by_refidx" else refpics[g[0]].pid, g[1], g[2]) for g in rec.grid]
                mb_f.refidx = [g[0] for g in rec.grid]
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
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=f["cbp"], qp=qp, parts=[])
            else:
                # ---- inter macroblock (P_Skip included): whole-macroblock prediction, then the residual per 4x4 block
                pinfo = []
                if pixels:
                    pred, pinfo = inter_prediction(refpics, rec, mx, my, W, H, record, wt, f["name"] == "P_Skip")
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
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=(f["cbp"] if f["cbp"] != "-" else 0), qp=qp, parts=pinfo, slice=s.index)
            if filtered_each and pixels:
                # wrong on purpose: filter this macroblock's edges now (later macroblocks then predict from filtered samples)
                one = [dict(v=[0] * 16, h=[0] * 16) for _ in range(w * h)]
                one[a] = bsref_inter.mb_bs(bp, a, w)
                deblock_ref.filter_picture(planes, w, one, qp_y, pcm_flag, adiv2, bdiv2, chroma_offsets)
    # ---- the loop filter, over the finished (unfiltered) picture; what is stored as reference is the filtered picture
    grid = bsref_inter.picture_bs(bp)
    unfiltered = copy_planes(planes) if (record and pixels) else None
    stored = planes
    if pixels and not filtered_each and not (MUTANT == "no_deblock_in_p" and s0.slice_type == 0):
        if MUTANT == "ref_unfiltered":
            stored = copy_planes(planes)
        if MUTANT == "chroma_bs_own":
            cgrid = bsref_inter.chroma_own_grid(bp, grid, w)
            deblock_ref.filter_picture(planes, w, grid, qp_y, pcm_flag, adiv2, bdiv2, chroma_offsets, only={0})
            deblock_ref.filter_picture(planes, w, cgrid, qp_y, pcm_flag, adiv2, bdiv2, chroma_offsets, only={1, 2})
        else:
            deblock_ref.filter_picture(planes, w, grid, qp_y, pcm_flag, adiv2, bdiv2, chroma_offsets)
    elif filtered_each:
        stored = planes
    ref = Ref(stored)
    ref.pid = pic_no
    t0 = rs[0].info.tab
    dpb.mark(pic_no, ref, cur_fn, s0.nal_type == 5, s0.hdr.get("nal_ref_idc", 1), t0["lt_flag"], t0["mmco"])
    return dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, mbs=mbinfo, w_mbs=w, h_mbs=h, bspic=bp, bs=grid, pid=pic_no, unfiltered=unfiltered)


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
