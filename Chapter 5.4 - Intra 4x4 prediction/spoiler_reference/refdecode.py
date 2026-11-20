#!/usr/bin/env python3
"""A reference "reader" for Chapter 5.4, used by the harness to verify itself (--self-test) and to prove that the
formulas of the Readme reproduce ffmpeg.  Parsing is common/cavlc_ref.py (the Phase 4 truth parser); everything after
parsing is intraref.py (Intra16x16, chroma; Chapter 5.3), intra4ref.py (Intra 4x4; this chapter) and transref.py (5.2).

SPOILER: do not read this before you have written your own reader; the point of the chapter is that you write this
part yourself.

usage: refdecode.py <in.ts|.264> <out.yuv>
Decodes intra-only CAVLC streams without deblocking (disable_deblocking_filter_idc 1): the Phase 5 scope.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import cavlc_ref as R, yuvref  # noqa: E402
import intraref  # noqa: E402
import intra4ref  # noqa: E402
import transref  # noqa: E402


def mb_neighbours(addr, w, owner, slice_idx):
    """(A, B, C, D) availability of macroblock addr: inside the picture and decoded by the current slice."""
    mx, my = addr % w, addr // w
    if intra4ref.MUTANT == "avail_noslice":
        ok = lambda cond, a2: cond      # noqa: E731
    else:
        ok = lambda cond, a2: cond and owner.get(a2) == slice_idx     # noqa: E731
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1),
            ok(mx > 0 and my > 0, addr - w - 1))


def decode_pictures(path, stats=None):
    """-> list of dict(width, height, x0, y0, planes=(Y, Cb, Cr) full macroblock-aligned, modes={addr: 16 modes})."""
    td = tempfile.mkdtemp(prefix="refdec_")
    slices = R.load_stream(path, td)
    pics = {}
    for s in slices:
        pics.setdefault(s.picture, []).append(s)
    out = []
    for pi in sorted(pics):
        sl = pics[pi]
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
        owner, modes = {}, {}
        for s in sl:
            if s.pps.get("constrained_intra_pred_flag", 0):
                raise SystemExit("refdecode: constrained_intra_pred_flag = 1 is Chapter 5.5")
            res = R.parse_slice(s, 4)
            if res.status != "complete":
                raise SystemExit(f"refdecode: slice {s.index}: {res.status} {res.error or res.note}")
            o1 = s.pps["chroma_qp_index_offset"]
            offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
            for rec in res.mbs:
                owner[rec.addr] = s.index
                mx, my = rec.addr % w, rec.addr // w
                mbav = mb_neighbours(rec.addr, w, owner, s.index)
                avail = (mbav[0], mbav[1], mbav[3])
                f = rec.f
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
                    mo = lambda which, mx=mx, my=my: modes.get(rec.addr - 1 if which == "A" else rec.addr - w)    # noqa: E731
                    m16 = intra4ref.derive_modes(syntax, rec.addr, mbav[0], mbav[1], mo)
                    modes[rec.addr] = m16
                    tr = transref.Tracker()
                    intra4ref.reconstruct_i4(planes, mx, my, mbav, m16, blocks, f["cbp"] & 15, f["qp"], tr)
                    intraref.reconstruct_chroma(planes, mx, my, avail, mb, f["qp"], offs, tr)
                    if stats is not None:
                        for blk in range(16):
                            a_, b_, c_, d_ = intra4ref.block_flags(blk, mbav)
                            stats[("i4", m16[blk], a_, b_, d_)] = stats.get(("i4", m16[blk], a_, b_, d_), 0) + 1
                else:
                    intraref.reconstruct_i16(planes, mx, my, avail, mb, f["qp"], offs)
        out.append(dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, modes=modes))
    return out


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
