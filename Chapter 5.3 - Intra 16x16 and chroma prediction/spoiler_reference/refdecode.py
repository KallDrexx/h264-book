#!/usr/bin/env python3
"""A reference "reader" for Chapter 5.3, used by the harness to verify itself (--self-test) and to prove that the
formulas of the Readme reproduce ffmpeg.  Parsing is common/cavlc_ref.py (the Phase 4 truth parser); everything after
parsing is intraref.py + transref.py.  Do not copy it before you have tried: the point of the chapter is that you write
this part yourself.

usage: refdecode.py <in.ts|.264> <out.yuv> [--nxn gray|skip]
  I_NxN macroblocks (Intra 4x4 arrives in 5.4): their chroma is reconstructed normally, their luma is filled with 128.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import cavlc_ref as R, yuvref  # noqa: E402
import intraref  # noqa: E402


def decode_pictures(path):
    """-> list of dict(width, height, x0, y0, planes=(Y, Cb, Cr) full macroblock-aligned, nxn=count)."""
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
        owner = {}
        nxn = 0
        for s in sl:
            res = R.parse_slice(s, 4)
            if res.status != "complete":
                raise SystemExit(f"refdecode: slice {s.index}: {res.status} {res.error or res.note}")
            o1 = s.pps["chroma_qp_index_offset"]
            offs = (o1, s.pps.get("second_chroma_qp_index_offset", o1))
            for rec in res.mbs:
                owner[rec.addr] = s.index
                mx, my = rec.addr % w, rec.addr // w
                av = lambda ok, a2: ok and owner.get(a2) == s.index     # noqa: E731
                avail = (av(mx > 0, rec.addr - 1), av(my > 0, rec.addr - w), av(mx > 0 and my > 0, rec.addr - w - 1))
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
                    nxn += 1
                    intraref.put(planes[0], 16 * mx, 16 * my, [[128] * 16 for _ in range(16)])
                    import transref
                    intraref.reconstruct_chroma(planes, mx, my, avail, mb, f["qp"], offs, transref.Tracker())
                else:
                    intraref.reconstruct_i16(planes, mx, my, avail, mb, f["qp"], offs)
        out.append(dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes, nxn=nxn))
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
