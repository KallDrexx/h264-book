#!/usr/bin/env python3
"""SPOILER: complete reference decoder for the Chapter 8.5 scope: everything of Chapter 7.9's decoder (I, P and B pictures, frames,
8-bit 4:2:0, deblocking, reference management, weighted prediction, direct modes) with the entropy decoder of EITHER kind (CAVLC
through common/inter_ref.py, CABAC through parse8.py, one record shape: stream8.py) and the High-profile tools: transform_size_8x8_flag,
the 8x8 inverse transform for intra and inter macroblocks, Intra 8x8 prediction with its reference-sample filter, and deblocking
that skips the internal 4x4 edges of 8x8-transformed macroblocks and takes the coefficient test from the 8x8 block.  Chapter 8.6 adds the
scaling matrices (SPS / PPS lists with fall-back rules, scaling_ref.py; applied in transref.py).  Output in display order.  Do not read before your own decoder passes.

usage: hdec.py <in.ts|.264> <out.yuv>        (decodes everything; --frames N stops after N output frames)
decode(..., pixels=False) skips sample work; decode(..., cache=dict) keeps the parsed stream between calls (the mutants of the
transform / prediction / deblocking never change the parse).  MUTANT switches ONE deliberately wrong rule on for the harness self-test
(ALL_MUTANTS lists them; the parser-level wrong rules are parse8.MUTANTS, run through stream8.CABAC_MUTANT).
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import yuvref  # noqa: E402
import stream8  # noqa: E402
import intra8ref  # noqa: E402
import dmvpred_ref  # noqa: E402
import direct_ref  # noqa: E402
import brefmgmt_ref  # noqa: E402
import bslicehdr_ref  # noqa: E402
import bsref_b  # noqa: E402
import deblock_ref  # noqa: E402
import interp_ref  # noqa: E402
import intra4ref  # noqa: E402
import intraref  # noqa: E402
import poc_ref  # noqa: E402
import transref  # noqa: E402
import scaling_ref  # noqa: E402

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
    "w_single_list": "implicit weights are also applied to ONE-list partitions (sample * 2w / 64 with w = 64 - s resp. s)",
}
SCALING_MUTANTS = {
    "cb_uses_luma": "the Cb residual is scaled with the luma list of its macroblock type (list 0 / 3)",
    "chroma_uses_luma": "both chroma components use the luma list (0 / 3)",
    "cr_uses_cb": "Cr uses Cb's list (1 / 4) instead of its own (2 / 5)",
    "inter_uses_intra": "inter macroblocks use the Intra 4x4 lists",
    "intra_uses_inter": "intra macroblocks use the Inter 4x4 lists",
    "i16dc_flat": "the Intra16x16 DC levels are scaled with the flat 16 * normAdjust instead of the matrix entry (0, 0)",
    "chroma_dc_flat": "the chroma DC levels are scaled with the flat 16 * normAdjust instead of the matrix entry (0, 0)",
    "chroma_dc_uses_luma": "the chroma DC levels use the luma list's entry (0, 0) while the chroma AC use the chroma list",
    "weight_ignored_4x4": "4x4 blocks are scaled flat (the lists are parsed and ignored)",
    "weight_ignored_8x8": "8x8 blocks are scaled flat (the lists are parsed and ignored)",
    "inter8_uses_intra": "inter 8x8 blocks use the Intra 8x8 list",
    "intra8_uses_inter": "intra 8x8 blocks use the Inter 8x8 list",
    "t8_lists_swapped": "8x8 blocks use the other macroblock class's list (intra <-> inter)",
}
DECODER_MUTANTS = {
    "t8_scale_4x4_norm": "8x8 dequantisation uses the 4x4 normAdjust values (position classes of the 4x4 table)",
    "t8_shift_qp": "8x8 dequantisation always uses the (+ 2^(5 - qP/6)) >> (6 - qP/6) form (no left shift for qP >= 36)",
    "t8_no_final_round": "8x8 inverse transform: the final (x + 32) >> 6 becomes x >> 6",
    "t8_zigzag4": "8x8 coefficients are placed with the 4x4 zig-zag extended by raster order",
    "t8_cavlc_noninterleave": "CAVLC 8x8: the four 4x4 blocks are concatenated (coefficients 16 * k + i) instead of interleaved",
    "t8_bs_first_cell_only": "bS 2 for 8x8-transformed macroblocks: only the top-left 4x4 cell of each 8x8 block counts as coded",
    "t8_inter_4x4": "inter macroblocks with transform_size_8x8_flag are reconstructed with 4x4 transforms",
    "inter_dc_prescaled": "inter luma blocks use the Intra16x16 rule: coefficient 0 is not scaled",
    "no_final_clip": "pred + residual is not clipped to 0..255 (wraps modulo 256)",
    "qp_slice_only": "inter residual dequantised with the slice QP instead of the running QP_Y",
    "chroma_qp_luma": "chroma residual dequantised with QP_Y instead of QP_C",
}
DECODER_MUTANTS.update(SCALING_MUTANTS)
ALL_MUTANTS = {}            # every wrong rule reachable through MUTANT (decoder level, lists, vector prediction, bS); filled below
DEBLOCK = True        # False: the deblocking filter is never applied (compared with ffmpeg -skip_loop_filter all)
STATS = None          # a collections.Counter: coverage of the B rules (self-test)


Unsupported = stream8.Unsupported


class StopDecoding(Exception):
    """Raised by a hook to end decode() early (the pictures decoded so far are returned)."""


def new_planes(w_mbs, h_mbs, fill=128):
    return ([[fill] * (16 * w_mbs) for _ in range(16 * h_mbs)], [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)],
            [[fill] * (8 * w_mbs) for _ in range(8 * h_mbs)])


def check_supported(s):
    stream8.check_stream_supported(s)


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


def combine(blocks, refs, wt, plane, mode_bi, iw=None):
    """blocks: [(list, block)] prediction blocks of one partition and plane (1 for L0 or L1 only, 2 for Bi: list 0 first);
    refs: {list: ref_idx}; iw: implicit weights (w0, w1) of a bi-predicted block, or (w,) for the wrong single-list rule.
    -> predicted block (list of rows)."""
    if len(blocks) == 1:
        lst, b = blocks[0]
        if iw is not None:
            return [[clip1((v * 2 * iw[0] + 32) >> 6) for v in row] for row in b]
        if wt is None:
            return b
        w, o, lw = entry_weights(wt, lst, refs[lst], plane)
        return [[weight1(v, w, o, lw) for v in row] for row in b]
    b0, b1 = blocks[0][1], blocks[1][1]
    if iw is not None:
        return [[direct_ref.implicit_combine(x, y, iw[0], iw[1]) for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]
    if wt is None:
        r = 0 if MUTANT == "bi_round_down" else 1
        return [[(x + y + r) >> 1 for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]
    w0, o0, lw = entry_weights(wt, 0, refs[0], plane)
    w1, o1, _ = entry_weights(wt, 1, refs[1], plane)
    return [[weight2(x, y, w0, w1, o0, o1, lw) for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]


def inter_prediction(L0, L1, rec, mx, my, W, H, record, wt, imp=None):
    """Prediction of the whole macroblock (luma 16x16, Cb 8x8, Cr 8x8), partition by partition.  rec.grid gives, for each 4x4 block,
    (ref0, mv0x, mv0y, ref1, mv1x, mv1y).  wt = the slice's weight tables (explicit weighting) or None; imp = the slice's
    direct_ref.SliceCtx when weighted_bipred_idc is 2 (implicit weights), else None."""
    pred = ([[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(8)], [[0] * 8 for _ in range(8)])
    parts = getattr(rec, "pred_parts", None) or rec.parts or [dict(x=0, y=0, w=16, h=16)]
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
        iw = None
        if imp is not None:
            if len(uses) == 2:
                iw = direct_ref.implicit_weights(imp.cur_poc, imp.poc0[used_refs[0]], imp.poc1[used_refs[1]],
                                                 imp.lt0[used_refs[0]], imp.lt1[used_refs[1]])
            elif MUTANT == "w_single_list":
                (lst, ri, _, _), = uses
                wts = direct_ref.implicit_weights(imp.cur_poc, imp.poc0[ri if lst == 0 else 0], imp.poc1[ri if lst == 1 else 0],
                                                  False, False)
                iw = (wts[lst],)
        for pl in range(3):
            blk = combine(planes_blocks[pl], used_refs, wt, pl, len(uses) == 2, iw)
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
                if imp is not None:
                    STATS["implicit Bi" if iw != (32, 32) else "implicit Bi 32/32"] += 1
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


def block_map(rec):
    """(kind, idx) -> coefficient list of a record's blocks; the CAVLC 8x8 de-interleaving mutant is applied here."""
    out = {}
    for b in rec.blocks:
        c = b["coeffs"]
        if MUTANT == "t8_cavlc_noninterleave" and b.get("parts4"):
            c = [v for p in b["parts4"] for v in p]
        out[(b["kind"], b["idx"])] = c
    return out


def inter_residual(rec, qp, offs, tr, slice_qp):
    f = rec.f
    cbp = f["cbp"] if f["cbp"] != "-" else 0
    blocks = block_map(rec)
    resy = None
    q = slice_qp if MUTANT == "qp_slice_only" else qp
    if cbp & 15 and f["t8x8"] == 1 and MUTANT != "t8_inter_4x4":
        resy = [[0] * 16 for _ in range(16)]
        for b8 in range(4):
            if (cbp >> b8) & 1 and ("luma8", b8) in blocks:
                r = transref.residual8x8(blocks[("luma8", b8)], q, tr)
                for i in range(8):
                    for j in range(8):
                        resy[8 * (b8 >> 1) + i][8 * (b8 & 1) + j] = r[i][j]
    elif cbp & 15 and f["t8x8"] == 1:           # mutant: four 4x4 transforms of the first 16 coefficients of each 8x8 block
        resy = [[0] * 16 for _ in range(16)]
        for b8 in range(4):
            if (cbp >> b8) & 1 and ("luma8", b8) in blocks:
                for k in range(4):
                    bx, by = (b8 & 1) * 2 + (k & 1), (b8 >> 1) * 2 + (k >> 1)
                    r = transref.residual4x4(transref.inverse_zigzag(blocks[("luma8", b8)][16 * k:16 * k + 16]), q, False, tr)
                    for i in range(4):
                        for j in range(4):
                            resy[4 * by + i][4 * bx + j] = r[i][j]
    elif cbp & 15:
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
            transref.COMP = 1 + len(resc)
            resc.append(transref.chroma_residual(qpc, dc, ac, tr))
            transref.COMP = 0
    return resy, resc


class Ref(tuple):
    """A stored picture: (Y, Cb, Cr) plane lists plus .pid, the identity used by the bS motion rule."""
    pid = None
    poc = 0         # PicOrderCnt of the picture
    mot = None      # dmvpred_ref.PicState: the stored motion data (read as co-located data by later B pictures)


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


def decode(path, upto=None, record=False, workdir=None, pixels=True, cache=None, hook=None, store=None):
    """-> list of pictures in OUTPUT order (display order: sorted by POC inside each IDR period).  Each is
    dict(width, height, x0, y0, planes, mbs, w_mbs, h_mbs, bs, pid (decoding-order index), poc, period, slice_types, lists).
    upto = number of output frames wanted: pictures later in output order that are not needed are neither decoded nor returned
    (the entry is None).  pixels=False skips all sample work.  cache: a dict that keeps the parsed stream between calls (the
    macroblock parsing does not depend on the decoder-level wrong rules).  hook / store: see the module docstring / a dict that is
    filled with all decoded pictures by decoding-order index."""
    td = workdir or tempfile.mkdtemp(prefix="ddec_")
    if cache is not None and "parsed" in cache:
        infos, results = cache["parsed"]
    else:
        try:
            infos = stream8.load(path, td)
            tables = bslicehdr_ref.load(os.path.join(td, "ref_trace.txt"))
        finally:
            if workdir is None:
                shutil.rmtree(td, ignore_errors=True)       # the traces are read; nothing else is kept in the directory
        for s, t in zip(infos, tables):
            s.tab = t
        results = stream8.parse_all(infos)
        for r in results:
            if r.status != "complete":
                raise Unsupported(f"slice {r.info.index}: parser status {r.status}: {r.error or r.note}")
        if cache is not None:
            cache["parsed"] = (infos, results)
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
    transref.T8_MUTANT = MUTANT if MUTANT in DECODER_MUTANTS else None
    intra8ref.MUTANT = MUTANT if MUTANT in intra8ref.MUTANTS else None
    dmvpred_ref.MUTANT = MUTANT if MUTANT in MVPRED_MUTANTS else None
    direct_ref.MUTANT = MUTANT if MUTANT in direct_ref.MUTANTS else None
    transref.MUTANT = None
    decoded = {}
    transref.WM = None
    rank = {pi: k for k, pi in enumerate(out_order)}
    try:
        for n, pi in enumerate(order):
            if pi > last_decode:
                break
            d = decode_picture(pics[pi], dpb, record, n, pixels, meta[pi]["raw"])
            d["poc"], d["period"] = meta[pi]["poc"], meta[pi]["period"]
            decoded[pi] = d
            if hook is not None:
                try:
                    hook(d, rank[pi])
                except StopDecoding:
                    break
    finally:
        interp_ref.MUTANT = None
        bsref_b.MUTANT = None
        transref.T8_MUTANT = None
        transref.WM, transref.INTER, transref.COMP = None, False, 0
        scaling_ref.MUTANT = None
        intra8ref.MUTANT = None
        dmvpred_ref.MUTANT = None
        direct_ref.MUTANT = None
    if store is not None:
        store.update(decoded)                  # every picture decoded so far by decoding-order index (co-located pictures included)
    if hook is not None:
        return [decoded.get(pi) for pi in out_order]
    return [decoded.get(pi) for pi in out_order[:len(need)]] if upto is not None else [decoded[pi] for pi in out_order]


MVPRED_MUTANTS = ("b_mvp_l1_uses_l0_nb", "b_nb_unused_list_unavail", "b_nb_unused_list_ref0")
INTERP_MUTANTS = ("taps", "taps_sign", "b_round", "j_from_rounded", "j_from_clipped_b1", "j_round", "j_shift", "avg_round_down",
                  "diag_pair", "diag_pair_r", "clamp_off_by_one", "no_clamp", "mv_int_round", "chroma_mv_units",
                  "chroma_swap_frac", "chroma_swap_bc", "chroma_round")
ALL_MUTANTS.update(MUTANTS)
ALL_MUTANTS.update(direct_ref.MUTANTS)
ALL_MUTANTS.update({k: v for k, v in brefmgmt_ref.MUTANTS.items() if k.startswith("b_")})
ALL_MUTANTS.update({"b_mvp_l1_uses_l0_nb": "list-1 vectors are predicted from the neighbours' LIST 0 data",
                    "b_nb_unused_list_unavail": "a neighbouring partition that does not use list X counts as 'not available' for list X",
                    "b_nb_unused_list_ref0": "a neighbouring inter partition that does not use list X counts as refIdxLX = 0 (instead of -1)"})
ALL_MUTANTS.update({k: v for k, v in bsref_b.MUTANTS.items()
                    if k.startswith("b_") or k in ("ref_idx_compare", "mv_thr_5", "skip_mv_zero", "skip_is_coded")})


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
    st = dmvpred_ref.PicState(w, h)
    for r in rs:
        s = r.info
        hd = s.hdr
        tab = s.tab
        bp.slices[s.index] = bsref_b.SliceFacts(hd.get("disable_deblocking_filter_idc", 0), s.slice_type)
        constrained = bool(s.pps.get("constrained_intra_pred_flag", 0))
        slice_modes = stream8.slice_imodes(r)
        transref.WM = s.wm
        offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
        wt = tab["wt"] if s.slice_type in (0, 1) else None
        if s.slice_type in (0, 1):
            L0, L1 = dpb.lists(cur_fn, poc, s.slice_type, tab["active0"], tab["active1"], tab["mods0"], tab["mods1"])
            slots = dpb.slots(cur_fn, poc, s.slice_type, tab["active0"], tab["active1"], tab["mods0"], tab["mods1"])
            list_log.append(dict(slice=s.index, type=s.slice_type, slots=slots))
            sl0, sl1 = slots

            def facts(lst, sls):
                return ([x.pid if x is not None else None for x in lst], [x.poc if x is not None else 0 for x in lst],
                        [e is not None and e[1] == "L" for e in sls])
            p0_, o0_, t0_ = facts(L0, sl0)
            p1_, o1_, t1_ = facts(L1, sl1)
            mot1 = L1[0].mot if L1 and L1[0] is not None else None
            sctx = direct_ref.SliceCtx(poc, p0_, o0_, t0_, p1_, o1_, t1_, mot1, bool(hd.get("direct_spatial_mv_pred_flag", 0)),
                                       bool(s.sps.get("direct_8x8_inference_flag", 0)), tab["bipred_idc"] == 2)
        else:
            L0 = L1 = []
            sctx = direct_ref.SliceCtx(poc, [], [], [], [], [], [], None, False, False)
        try:
            dmvpred_ref.fill_slice(st, r, sctx)
        except direct_ref.DirectInvalid as e:
            e.pic, e.slice = pic_no, s.index
            raise
        imp = sctx if (s.slice_type == 1 and tab["bipred_idc"] == 2) else None
        slice_types.append(s.slice_type)
        last_res = None
        for rec in r.mbs:
            a = rec.addr
            owner[a] = s.index
            f = rec.f
            mx, my = a % w, a // w
            is_intra = not f["name"].startswith(("P_", "B_"))
            intra[a] = is_intra
            transref.INTER = not is_intra
            mbav = mb_neighbours(a, w, owner, s.index, intra, constrained)
            avail = (mbav[0], mbav[1], mbav[3])
            qp = f["qp"]
            mb_f = bsref_b.Mb(s.index, intra=is_intra or rec.pcm is not None, pcm=rec.pcm is not None,
                              skip=f["name"] in ("P_Skip", "B_Skip"))
            mb_f.t8 = f["t8x8"] == 1
            bp.mbs[a] = mb_f
            adiv2[a], bdiv2[a] = hd.get("slice_alpha_c0_offset_div2", 0), hd.get("slice_beta_offset_div2", 0)
            pcm_flag[a] = rec.pcm is not None
            qp_y[a] = 0 if rec.pcm is not None else qp
            cbpv = f["cbp"] if f["cbp"] != "-" else 0
            mb_f.chroma_cbp = (cbpv >> 4) if not is_intra else 0
            if not is_intra:
                blocks_ = block_map(rec)
                nz = [False] * 16
                if mb_f.t8:
                    for q in range(4):
                        v = any(blocks_.get(("luma8", q), ()))
                        for k in range(1 if MUTANT == "t8_bs_first_cell_only" else 4):
                            nz[((q >> 1) * 2 + (k >> 1)) * 4 + (q & 1) * 2 + (k & 1)] = v
                else:
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
                if imp is not None:
                    wg = {}
                    rec.wgrid = [wg.setdefault((g[0], g[3]), direct_ref.implicit_weights(
                        poc, sctx.poc0[g[0]], sctx.poc1[g[3]], sctx.lt0[g[0]], sctx.lt1[g[3]])) if g[0] >= 0 and g[3] >= 0 else None
                        for g in rec.grid]
                else:
                    rec.wgrid = None
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
                    blocks = block_map(rec)
                    mb = dict(i16_mode=f["i16_mode"], chroma_pred=f["chroma_pred"], blocks=blocks)
                    if f["name"] == "I_NxN":
                        m16 = slice_modes[a]
                        tr = transref.Tracker()
                        if f["t8x8"] == 1:
                            intra8ref.reconstruct_i8(planes, mx, my, mbav, [m16[0], m16[4], m16[8], m16[12]], blocks,
                                                     f["cbp"] & 15, qp, tr)
                            if STATS is not None and MUTANT is None:
                                for k in (0, 4, 8, 12):
                                    STATS[f"i8 mode {m16[k]}"] += 1
                                STATS["i8 mb"] += 1
                        else:
                            intra4ref.reconstruct_i4(planes, mx, my, mbav, m16, blocks, f["cbp"] & 15, qp, tr)
                        intraref.reconstruct_chroma(planes, mx, my, avail, mb, qp, offs, tr)
                    else:
                        intraref.reconstruct_i16(planes, mx, my, avail, mb, qp, offs)
                if record:
                    mbinfo[a] = dict(addr=a, name=f["name"], cbp=f["cbp"], qp=qp, parts=[], slice=s.index)
            else:
                pinfo = []
                if pixels:
                    pred, pinfo = inter_prediction(L0, L1, rec, mx, my, W, H, record, wt, imp)
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
    if pixels and DEBLOCK:
        deblock_ref.filter_picture(planes, w, grid, qp_y, pcm_flag, adiv2, bdiv2, chroma_offsets)
    ref = Ref(planes)
    ref.pid = pic_no
    ref.poc = poc
    ref.mot = st
    t0 = rs[0].info.tab
    dpb.mark(pic_no, ref, cur_fn, s0.nal_type == 5, t0["nal_ref_idc"], t0["lt_flag"], t0["mmco"], poc,
             any(x == 1 for x in slice_types))
    return dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, mbs=mbinfo, w_mbs=w, h_mbs=h, bspic=bp, bs=grid, pid=pic_no, mot=st,
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
