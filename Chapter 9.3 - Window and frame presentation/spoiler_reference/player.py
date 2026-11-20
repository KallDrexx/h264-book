#!/usr/bin/env python3
"""Reference headless player (spoiler).  No window, no GUI, never reads stdin.

  player.py YUV --size WxH --aspect-idc N --sar-ext W:H --matrix bt709|bt601 --range limited|full --window auto|WxH
            --format bgrx|bgra|rgbx|rgba|rgb24|bgr24 --align 4 --origin topdown|bottomup
            --script FILE --log FILE --dump-presented DIR
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import presenter as P


def run(a):
    w, h = (int(t) for t in a.size.split("x"))
    data = open(a.yuv, "rb").read()
    fs = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
    if not data or len(data) % fs:
        sys.exit(f"{a.yuv}: not a whole number of {w}x{h} yuv420p frames")
    n = len(data) // fs
    ext = tuple(int(t) for t in a.sar_ext.split(":"))
    sar = P.sar_from_idc(a.aspect_idc, ext, a.bug)
    dw, dh = P.display_size(w, h, sar, a.bug)
    win0 = (dw, dh) if a.window == "auto" else tuple(int(t) for t in a.window.split("x"))
    events = P.parse_script(open(a.script).read()) if a.script else []
    presents, end = P.simulate(n, events, win0, a.bug)
    native = {}
    lines = [f"HEADER src={w}x{h} frames={n} sar={sar[0]}:{sar[1]} display={dw}x{dh} format={a.format} align={a.align} origin={a.origin}"]
    if a.dump_presented:
        os.makedirs(a.dump_presented, exist_ok=True)
    for i, (frame, reason, (ww, wh), _line) in enumerate(presents):
        if frame not in native:
            native[frame] = P.convert_nearest(data, frame * fs, w, h, a.matrix, a.range == "full")
        src = native[frame - 1] if a.bug == "stale_frame" and frame > 0 and (frame - 1) in native else native[frame]
        rect = P.fit_rect(ww, wh, dw, dh, a.bug)
        rgb = P.render(src, w, h, ww, wh, rect, a.bug)
        raw, stride, claimed = P.pack(rgb, ww, wh, a.format, a.align, a.origin, a.bug)
        if a.dump_presented:
            base = os.path.join(a.dump_presented, "present_%05d" % i)
            if not (a.bug == "missing_ppm" and i == 2):
                P.write_ppm(base + ".ppm", ww, wh, rgb)
            open(base + ".raw", "wb").write(raw)
        shown = (frame + 1) if a.bug == "frames_1based" else frame
        lw, lh = (w, h) if a.bug == "log_frame_size" else (ww, wh)
        lines.append(f"PRESENT {i} frame={shown} reason={reason} window={lw}x{lh} rect={rect[0]},{rect[1]},{rect[2]},{rect[3]} "
                     f"format={a.format} stride={claimed} origin={a.origin}")
    lines.append(f"END presents={len(presents)} reason={end}")
    if a.bug == "bad_log_line":
        lines[2] = lines[2].replace("reason=", "why=")
    if a.log:
        open(a.log, "w").write("\n".join(lines) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("yuv")
    p.add_argument("--size", required=True)
    p.add_argument("--aspect-idc", type=int, default=0)
    p.add_argument("--sar-ext", default="0:0")
    p.add_argument("--matrix", default="bt601")
    p.add_argument("--range", default="limited")
    p.add_argument("--window", default="auto")
    p.add_argument("--format", default="bgrx", choices=sorted(P.FORMATS))
    p.add_argument("--align", type=int, default=4)
    p.add_argument("--origin", default="topdown", choices=("topdown", "bottomup"))
    p.add_argument("--script")
    p.add_argument("--log")
    p.add_argument("--dump-presented")
    p.add_argument("--bug", help=argparse.SUPPRESS)
    run(p.parse_args(argv))


if __name__ == "__main__":
    main()
