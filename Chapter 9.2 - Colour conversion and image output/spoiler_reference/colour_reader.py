#!/usr/bin/env python3
"""Spoiler: a good reader.  colour_reader.py YUV WIDTH HEIGHT MATRIX RANGE UPSAMPLE FRAMES OUTDIR

Uses 16.16 fixed-point coefficients (what a C implementation would do) for nearest upsampling and
falls back to the float reference for the optional bilinear modes.  Writes OUTDIR/frame_NNNNN.ppm."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import colour_ref as C


def fixed_frame(Y, U, V, w, h, matrix, full):
    kr, kb = C.MATRICES[matrix]
    kg = 1 - kr - kb
    ys = 1.0 if full else 255 / 219
    cs = 1.0 if full else 255 / 224
    S = 1 << 16
    cy = round(ys * S)
    crr = round(2 * (1 - kr) * cs * S)
    cbb = round(2 * (1 - kb) * cs * S)
    cgu = round(2 * (1 - kb) * kb / kg * cs * S)
    cgv = round(2 * (1 - kr) * kr / kg * cs * S)
    yo = 0 if full else 16
    cw = (w + 1) // 2
    out = bytearray(w * h * 3)
    o = 0
    for y in range(h):
        for x in range(w):
            c = (y >> 1) * cw + (x >> 1)
            yy = (Y[y * w + x] - yo) * cy + 32768
            u, v = U[c] - 128, V[c] - 128
            r = (yy + crr * v) >> 16
            g = (yy - cgu * u - cgv * v) >> 16
            b = (yy + cbb * u) >> 16
            out[o] = 0 if r < 0 else 255 if r > 255 else r
            out[o + 1] = 0 if g < 0 else 255 if g > 255 else g
            out[o + 2] = 0 if b < 0 else 255 if b > 255 else b
            o += 3
    return bytes(out)


def main(argv):
    yuv, w, h, matrix, rng, up, frames, outdir = argv
    w, h = int(w), int(h)
    data = open(yuv, "rb").read()
    os.makedirs(outdir, exist_ok=True)
    for n in [int(t) for t in frames.split(",")]:
        Y, U, V, _, _ = C.planes(data, w, h, n)
        if up == "nearest":
            rgb = fixed_frame(Y, U, V, w, h, matrix, rng == "full")
        else:
            rgb = C.convert(Y, U, V, w, h, matrix, rng == "full", up)
        C.write_ppm(os.path.join(outdir, "frame_%05d.ppm" % n), w, h, rgb)


if __name__ == "__main__":
    main(sys.argv[1:])
