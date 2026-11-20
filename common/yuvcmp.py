"""Compare two raw planar yuv420p (8-bit 4:2:0) files and say where they first differ.

    python3 -m common.yuvcmp expected.yuv actual.yuv WIDTH HEIGHT [--frames N] [--origin X0 Y0]

    from common.yuvcmp import compare_yuv
    res = compare_yuv("ref.yuv", "out.yuv", 352, 288)       # prints a report, returns a dict
    res = compare_yuv(..., quiet=True); print(res["text"])  # or just collect the text

WIDTH/HEIGHT are the dimensions of the frames IN THE FILES (i.e. after cropping).  Frame layout is
the Y plane (W x H), then Cb, then Cr, each (W+1)//2 x (H+1)//2, no padding, no frame headers.
Pixel positions are in the file's (cropped) planes.  Macroblocks are 16x16 luma units of the decoded
picture: a luma pixel (x, y) belongs to macroblock ((x0 + x) // 16, (y0 + y) // 16) and a chroma pixel
(cx, cy) to ((x0/2 + cx) // 8, (y0/2 + cy) // 8), where (x0, y0) = `origin` is the position of the
file's top-left luma sample in the decoded picture (CropUnitX * crop_left, CropUnitY * crop_top;
default (0, 0), right for streams that crop only on the right and bottom, such as 1080p).

Result dict keys: ok, frames_expected, frames_actual, size_problems (list of str), first_diff
(None or dict(frame, plane, mb_x, mb_y, x, y, expected, actual)), hints (list of str), frame_stats (list of dict(frame,
pixels, mbs) for the first differing frames), differing_frames (count among compared frames),
text (the report).  Exit codes of the CLI: 0 identical, 1 different, 2 unusable input.
"""
import os
import sys

PLANES = ("Y", "Cb", "Cr")


def frame_geometry(width, height):
    """-> list of (name, plane_w, plane_h, offset) and the frame size in bytes."""
    cw, ch = (width + 1) // 2, (height + 1) // 2
    geo = [("Y", width, height, 0), ("Cb", cw, ch, width * height), ("Cr", cw, ch, width * height + cw * ch)]
    return geo, width * height + 2 * cw * ch


def _mb_of(plane, x, y, x0=0, y0=0):
    return ((x0 + x) // 16, (y0 + y) // 16) if plane == "Y" else ((x0 // 2 + x) // 8, (y0 // 2 + y) // 8)


def _diff_frame(ea, ac, geo, mbw, mbh, x0=0, y0=0):
    """-> (pixels, set of (mbx, mby), first_diff or None) for one pair of frames (bytes)."""
    pixels, mbs, first = 0, set(), None
    for name, pw, ph, off in geo:
        for y in range(ph):
            a, b = ea[off + y * pw: off + (y + 1) * pw], ac[off + y * pw: off + (y + 1) * pw]
            if a == b:
                continue
            for x in range(pw):
                if a[x] != b[x]:
                    pixels += 1
                    mx, my = _mb_of(name, x, y, x0, y0)
                    mbs.add((min(mx, mbw - 1), min(my, mbh - 1)))
                    if first is None:
                        first = dict(plane=name, mb_x=mx, mb_y=my, x=x, y=y, expected=a[x], actual=b[x])
    return pixels, mbs, first


def mb_map(mbs, mbw, mbh, max_cols=80):
    """Compact ASCII picture: '#' = macroblock differs, '.' = identical.  Wide pictures are shrunk by
    grouping macroblocks (a cell is '#' if any macroblock in it differs)."""
    g = 1
    while (mbw + g - 1) // g > max_cols:
        g += 1
    rows = []
    for cy in range((mbh + g - 1) // g):
        row = ""
        for cx in range((mbw + g - 1) // g):
            hit = any((x, y) in mbs for y in range(cy * g, min(mbh, cy * g + g))
                      for x in range(cx * g, min(mbw, cx * g + g)))
            row += "#" if hit else "."
        rows.append(row)
    head = f"macroblock map ({mbw}x{mbh} MBs" + (f", 1 cell = {g}x{g} MBs" if g > 1 else "") + ", # = differs):"
    return head + "\n" + "\n".join(rows)


def _plane(frame, geo, name):
    _, pw, ph, off = [g for g in geo if g[0] == name][0]
    return frame[off:off + pw * ph]


def _hints(ea, ac, geo):
    """Recognise a few whole-plane mistakes in the first differing frame."""
    e = {n: _plane(ea, geo, n) for n in PLANES}
    a = {n: _plane(ac, geo, n) for n in PLANES}
    if e["Cb"] != e["Cr"] and a["Cb"] == e["Cr"] and a["Cr"] == e["Cb"]:
        return ["hint: your Cb plane equals the expected Cr plane and vice versa: Cb and Cr are swapped"]
    same = [n for n in PLANES if e[n] == a[n]]
    if same:
        bad = [n for n in PLANES if n not in same]
        return [f"hint: in this frame only {' and '.join(bad)} differ{'s' if len(bad) == 1 else ''} "
                f"({', '.join(same)} identical)"]
    return []


def compare_yuv(expected_path, actual_path, width, height, max_frames=5, quiet=False, out=None, origin=(0, 0)):
    """Compare two yuv420p files.  Prints the report unless quiet; always returns the result dict.
    origin = (x0, y0): where the files' top-left luma sample sits in the decoded picture (left/top
    cropping), so that macroblock numbers match the decoder's; both must be even."""
    res = dict(ok=False, frames_expected=0, frames_actual=0, size_problems=[], first_diff=None, hints=[],
               frame_stats=[], differing_frames=0, text="")
    lines = []
    x0, y0 = origin
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if x0 < 0 or y0 < 0 or x0 % 2 or y0 % 2:
        raise ValueError("origin must be even and non-negative")
    geo, fsize = frame_geometry(width, height)
    mbw, mbh = (x0 + width + 15) // 16, (y0 + height + 15) // 16
    sizes = []
    for label, p in (("expected", expected_path), ("actual", actual_path)):
        if not os.path.isfile(p):
            res["size_problems"].append(f"{label} file {p!r} does not exist")
            sizes.append(None)
        else:
            sizes.append(os.path.getsize(p))
    if None not in sizes:
        for label, n in zip(("expected", "actual"), sizes):
            if n % fsize:
                res["size_problems"].append(
                    f"{label} file is {n} bytes: not a whole number of {width}x{height} frames "
                    f"({fsize} bytes each); {n // fsize} whole frames + {n % fsize} stray bytes (truncated or wrong size?)")
        res["frames_expected"], res["frames_actual"] = sizes[0] // fsize, sizes[1] // fsize
        if res["frames_expected"] != res["frames_actual"]:
            res["size_problems"].append(
                f"frame count: expected {res['frames_expected']}, actual {res['frames_actual']}")
        n = min(res["frames_expected"], res["frames_actual"])
        shown_map = False
        with open(expected_path, "rb") as fe, open(actual_path, "rb") as fa:
            for i in range(n):
                ea, ac = fe.read(fsize), fa.read(fsize)
                if ea == ac:
                    continue
                res["differing_frames"] += 1
                if len(res["frame_stats"]) >= max_frames and res["first_diff"] is not None:
                    continue                      # later bad frames are only counted
                px, mbs, first = _diff_frame(ea, ac, geo, mbw, mbh, x0, y0)
                if res["first_diff"] is None:
                    res["first_diff"] = dict(frame=i, **first)
                    res["hints"] = _hints(ea, ac, geo)
                res["frame_stats"].append(dict(frame=i, pixels=px, mbs=len(mbs)))
                if not shown_map:
                    shown_map = True
                    res["_map"] = mb_map(mbs, mbw, mbh)
    for p in res["size_problems"]:
        lines.append("size: " + p)
    fd = res["first_diff"]
    if fd:
        lines.append(f"first difference: frame {fd['frame']}, plane {fd['plane']}, macroblock (x={fd['mb_x']}, y={fd['mb_y']}) "
                     f"= address {fd['mb_y'] * mbw + fd['mb_x']}, pixel (x={fd['x']}, y={fd['y']}) in the {fd['plane']} plane: "
                     f"expected {fd['expected']}, actual {fd['actual']}")
        for s in res["frame_stats"]:
            lines.append(f"  frame {s['frame']}: {s['pixels']} differing samples in {s['mbs']} of {mbw * mbh} macroblocks")
        more = res["differing_frames"] - len(res["frame_stats"])
        if more > 0:
            lines.append(f"  ... and {more} more differing frame(s) (not detailed)")
        lines.append(f"{res['differing_frames']} of {min(res['frames_expected'], res['frames_actual'])} compared frames differ")
        lines.extend(res["hints"])
        lines.append(res["_map"].replace("map (", f"map of frame {fd['frame']} (", 1))
    elif None not in sizes and res["size_problems"]:
        n = min(res["frames_expected"], res["frames_actual"])
        lines.append(f"the {n} whole frame(s) present in both files are identical" if n else "no whole frame to compare")
    res["ok"] = not res["size_problems"] and fd is None and None not in sizes
    if res["ok"]:
        lines.append(f"identical: {res['frames_expected']} frame(s) of {width}x{height}")
    res.pop("_map", None)
    res["text"] = "\n".join(lines)
    if not quiet:
        print(res["text"], file=out or sys.stdout)
    return res


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m common.yuvcmp",
                                 description="Compare two 8-bit yuv420p files; report the first differing frame/plane/macroblock/pixel. "
                                             "Exit 0 identical, 1 different, 2 a file is missing.")
    ap.add_argument("expected")
    ap.add_argument("actual")
    ap.add_argument("width", type=int, help="frame width in the files (after cropping)")
    ap.add_argument("height", type=int)
    ap.add_argument("--frames", type=int, default=5, help="detail this many differing frames (default 5)")
    ap.add_argument("--origin", type=int, nargs=2, default=(0, 0), metavar=("X0", "Y0"),
                    help="position of the files' top-left luma sample in the decoded picture "
                         "(left/top crop in luma samples, even); makes macroblock numbers match the decoder's")
    a = ap.parse_args(argv)
    if a.width <= 0 or a.height <= 0 or min(a.origin) < 0 or a.origin[0] % 2 or a.origin[1] % 2:
        print("error: width and height must be positive, origin even and non-negative", file=sys.stderr)
        return 2
    r = compare_yuv(a.expected, a.actual, a.width, a.height, max_frames=a.frames, origin=tuple(a.origin))
    if r["ok"]:
        return 0
    return 2 if any("does not exist" in p for p in r["size_problems"]) else 1


if __name__ == "__main__":
    sys.exit(main())
