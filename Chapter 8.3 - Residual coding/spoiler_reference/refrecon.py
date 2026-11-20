"""Chapter 8.3 optional end-to-end reference: reconstruct INTRA-ONLY CABAC streams (I slices, deblocking disabled) from the
coefficient arrays that resref.py parsed, to prove with ffmpeg's pixels that the coefficients are right.

The reconstruction arithmetic is copied from Chapters 5.2 - 5.4 (transref.py, intraref.py, intra4ref.py: dequantisation,
inverse transforms, Intra 16x16 / chroma / Intra 4x4 prediction); only the glue is new: it feeds those functions with the
`rec.blocks` of the CABAC parser instead of CAVLC's.  Inter pictures would need Chapters 7.x (and Chapter 8.4's mvd
parsing), so P / B streams are checked by the other means described in the harness.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import yuvref  # noqa: E402
import intra4ref  # noqa: E402
import intraref  # noqa: E402
import transref  # noqa: E402


class NotIntra(Exception):
    pass


def neighbours(addr, w, owner, sidx):
    """(A, B, C, D) macroblock availability (6.4.9 / 6.4.12): inside the picture and already decoded in the same slice."""
    mx, my = addr % w, addr // w

    def ok(cond, a2):
        return cond and owner.get(a2) == sidx
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - w), ok(my > 0 and mx < w - 1, addr - w + 1),
            ok(mx > 0 and my > 0, addr - w - 1))


def decode_picture(items):
    """items: list of (info, recs) of one picture.  -> dict(width, height, x0, y0, planes)."""
    s0 = items[0][0]
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
    for info, recs in items:
        if info.slice_type not in (2, 4):
            raise NotIntra("not an I slice")
        if info.hdr.get("disable_deblocking_filter_idc", 0) != 1:
            raise NotIntra("deblocking is enabled (reconstruction here stops before Chapter 6)")
        o1 = info.pps["chroma_qp_index_offset"]
        offs = (o1, info.pps.get("second_chroma_qp_index_offset", o1))
        for rec in recs:
            addr = rec.addr
            owner[addr] = info.index
            mx, my = addr % w, addr // w
            mbav = neighbours(addr, w, owner, info.index)
            avail = (mbav[0], mbav[1], mbav[3])
            if rec.pcm:
                d = rec.pcm_samples
                intraref.put(planes[0], 16 * mx, 16 * my, [list(d[16 * r:16 * r + 16]) for r in range(16)])
                for p in range(2):
                    base = 256 + 64 * p
                    intraref.put(planes[1 + p], 8 * mx, 8 * my, [list(d[base + 8 * r:base + 8 * r + 8]) for r in range(8)])
                continue
            blocks = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks if b["coeffs"] is not None}
            mb = dict(i16_mode=(rec.mbt - 1) % 4, chroma_pred=rec.cpred, blocks=blocks)
            if rec.i_nxn:
                syntax = [-1 if c == "p" else int(c) for c in rec.modes]
                mo = lambda which, a=addr: modes.get(a - 1 if which == "A" else a - w)    # noqa: E731
                m16 = intra4ref.derive_modes(syntax, addr, mbav[0], mbav[1], mo)
                modes[addr] = m16
                tr = transref.Tracker()
                intra4ref.reconstruct_i4(planes, mx, my, mbav, m16, blocks, rec.cbp_l, rec.qp, tr)
                intraref.reconstruct_chroma(planes, mx, my, avail, mb, rec.qp, offs, tr)
            else:
                intraref.reconstruct_i16(planes, mx, my, avail, mb, rec.qp, offs)
    return dict(width=cw, height=ch, x0=x0, y0=y0, planes=planes)


def decode_all(parsed):
    """parsed: resref.parse_all result.  -> list of pictures in decoding order (== output order for intra-only streams)."""
    pics = {}
    for info, recs, _ in parsed:
        pics.setdefault(info.picture, []).append((info, recs))
    return [decode_picture(pics[k]) for k in sorted(pics)]


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
