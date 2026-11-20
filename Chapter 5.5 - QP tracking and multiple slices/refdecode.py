#!/usr/bin/env python3
"""A reference "reader" for Chapter 5.5, used by the harness to verify itself (--self-test) and to prove that the rules of
the Readme reproduce ffmpeg.  Parsing is common/cavlc_ref.py (the Phase 4 truth parser); the QP chain (7.4.5), the
macroblock availability (6.4.x, constrained_intra_pred_flag) and the chroma QP are computed HERE, from the parsed
mb_qp_delta values, so that the harness can plant wrong rules in them.  The prediction and transform arithmetic is
copied from Chapters 5.2 - 5.4 (intraref.py, intra4ref.py, transref.py).

SPOILER: do not read this before you have written your own QP tracking and slice handling.

usage: refdecode.py <in.ts|.264> <out.yuv>
Decodes intra-only CAVLC streams without deblocking (disable_deblocking_filter_idc 1): the Phase 5 scope.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import cavlc_ref as R, yuvref  # noqa: E402
import intraref  # noqa: E402
import intra4ref  # noqa: E402
import transref  # noqa: E402

# Deliberate mistakes for the harness self-test (test_qp_slices.py --self-test); None = the correct rules.
MUTANT = None
QP_BD_OFFSET = 0          # 6 * (bit_depth_luma - 8); Phase 5 is 8-bit only


def next_qp(pred, delta):
    """7.4.5 (7-37): QP_Y from QP_Y,PRED and mb_qp_delta."""
    if MUTANT == "qp_clamp":
        return max(-QP_BD_OFFSET, min(51, pred + delta))
    if MUTANT == "qp_nomod":
        return pred + delta
    return (pred + delta + 52 + 2 * QP_BD_OFFSET) % (52 + QP_BD_OFFSET) - QP_BD_OFFSET


class QpChain:
    """QP_Y of the macroblocks of a picture, in decoding order."""

    def __init__(self):
        self.pred = None            # carried over slices only by the "no reset" mutant

    def start_slice(self, slice_qp, pic_init_qp):
        if MUTANT == "qp_slice_pic_init":
            slice_qp = pic_init_qp
        if not (MUTANT == "qp_no_reset" and self.pred is not None):
            self.pred = slice_qp
        self.slice_qp = slice_qp

    def macroblock(self, rec):
        """-> QP_Y of this macroblock (the value used for its residual); updates the running QP."""
        f = rec.f
        if rec.pcm is not None:
            if MUTANT == "qp_pcm_zero":
                self.pred = 0
            elif MUTANT == "qp_pcm_slice":
                self.pred = self.slice_qp
            return self.pred                      # no mb_qp_delta: inferred 0, QP_Y = QP_Y,PRED
        delta = f["qp_delta"]
        if delta == "-":                          # mb_qp_delta not present
            if MUTANT == "qp_nodelta_reset":
                self.pred = self.slice_qp
            return self.pred
        self.pred = next_qp(self.pred, delta)
        return self.pred


def mb_neighbours(addr, w, owner, slice_idx, constrained=False, intra=None):
    """(A, B, C, D) availability of macroblock addr (6.4.9 / 6.4.12): inside the picture, already decoded in the same
    slice, and - with constrained_intra_pred_flag = 1 - not an Inter macroblock (for sample prediction).  In an I
    slice every macroblock is intra, so the flag changes nothing; `intra` maps address -> is intra."""
    mx, my = addr % w, addr // w

    def ok(cond, a2):
        if not cond:
            return False
        if MUTANT == "avail_noslice":
            return True
        if MUTANT == "avail_noslice_above" and a2 != addr - 1:
            return True                         # the macroblocks above ignore slice boundaries, the left one does not
        if owner.get(a2) != slice_idx:
            return False
        if constrained:
            if MUTANT == "cip_all_unavailable":
                return False
            if MUTANT == "cip_pcm_unavailable" and intra.get(a2) == "pcm":
                return False
            return intra.get(a2) is not None     # every macroblock of an I slice is intra
        return True
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1),
            ok(mx > 0 and my > 0, addr - w - 1))


def decode_pictures(path, stats=None):
    """-> list of dict(width, height, x0, y0, planes=(Y, Cb, Cr) full macroblock-aligned, qp={addr: QP_Y})."""
    td = tempfile.mkdtemp(prefix="refdec_")
    slices = R.load_stream(path, td)
    pics = {}
    for s in slices:
        pics.setdefault(s.picture, []).append(s)
    out = []
    transref.MUTANT = MUTANT if MUTANT in ("cqp_noclip", "cqp_no_table") else None
    try:
        for pi in sorted(pics):
            out.append(decode_picture(pics[pi], stats))
    finally:
        transref.MUTANT = None
    return out


def decode_picture(sl, stats):
    s0 = sl[0]
    w = s0.pic_width_in_mbs
    h = s0.pic_size_in_mbs // w
    sps = s0.sps
    cw, ch, x0, y0 = yuvref.cropped_size(
        w, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
        sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
        sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
    planes = ([[128] * (16 * w) for _ in range(16 * h)], [[128] * (8 * w) for _ in range(8 * h)],
              [[128] * (8 * w) for _ in range(8 * h)])
    owner, modes, intra, qps = {}, {}, {}, {}
    chain = QpChain()
    for s in sl:
        constrained = bool(s.pps.get("constrained_intra_pred_flag", 0))
        res = R.parse_slice(s, 4)
        if res.status != "complete":
            raise SystemExit(f"refdecode: slice {s.index}: {res.status} {res.error or res.note}")
        o1 = s.pps["chroma_qp_index_offset"]
        offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
        if MUTANT == "cqp_cr_uses_cb":
            offs = (o1, o1)
        chain.start_slice(s.slice_qp, 26 + s.pps["pic_init_qp_minus26"])
        for rec in res.mbs:
            owner[rec.addr] = s.index
            intra[rec.addr] = "pcm" if rec.pcm is not None else "intra"
            mx, my = rec.addr % w, rec.addr // w
            mbav = mb_neighbours(rec.addr, w, owner, s.index, constrained, intra)
            avail = (mbav[0], mbav[1], mbav[3])
            f = rec.f
            qp = chain.macroblock(rec)
            qps[rec.addr] = qp
            if rec.pcm is not None:
                d = rec.pcm["samples"]
                intraref.put(planes[0], 16 * mx, 16 * my, [list(d[16 * r:16 * r + 16]) for r in range(16)])
                for p in range(2):
                    base = 256 + 64 * p
                    intraref.put(planes[1 + p], 8 * mx, 8 * my, [list(d[base + 8 * r:base + 8 * r + 8]) for r in range(8)])
                continue
            blocks = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}
            mb = dict(i16_mode=f["i16_mode"], chroma_pred=f["chroma_pred"], blocks=blocks)
            if f["name"] == "I_NxN":
                if f["t8x8"]:
                    raise SystemExit("refdecode: transform_size_8x8_flag = 1 (Intra 8x8) is Phase 8")
                syntax = [int(v) for v in f["modes"].split(",")]
                mo = lambda which, mx=mx, my=my, a=rec.addr: modes.get(a - 1 if which == "A" else a - w)    # noqa: E731
                if MUTANT == "mp_avail_noslice":
                    # mode prediction uses the picture-edge test only (sample availability stays slice-aware)
                    pa, pb = mx > 0, my > 0
                else:
                    pa, pb = mbav[0], mbav[1]
                m16 = intra4ref.derive_modes(syntax, rec.addr, pa, pb, mo)
                modes[rec.addr] = m16
                tr = transref.Tracker()
                intra4ref.reconstruct_i4(planes, mx, my, mbav, m16, blocks, f["cbp"] & 15, qp, tr)
                intraref.reconstruct_chroma(planes, mx, my, avail, mb, qp, offs, tr)
            else:
                intraref.reconstruct_i16(planes, mx, my, avail, mb, qp, offs)
    return dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, qp=qps)


def write_yuv(pics, out):
    with open(out, "wb") as f:
        for p in pics:
            x0, y0, w, h = p["x0"], p["y0"], p["width"], p["height"]
            for row in p["planes"][0][y0:y0 + h]:
                f.write(bytes(row[x0:x0 + w]))
            cw, chh = (w + 1) // 2, (h + 1) // 2
            for pl in p["planes"][1:]:
                for row in pl[y0 // 2:y0 // 2 + chh]:
                    f.write(bytes(row[x0 // 2:x0 // 2 + cw]))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    write_yuv(decode_pictures(sys.argv[1]), sys.argv[2])
