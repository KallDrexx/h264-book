"""Synthetic 4:2:0 test frames, 96x64, deterministic.  Pure standard library.

frame 0  flat 8x8 patches (12 x 8 = 96): Y / Cb / Cr extremes, legal primaries in both matrices, then seeded random.
frame 1  ramps: Y ramp, Cb ramp, Cr ramp, and a combined diagonal ramp (16 rows each).
frame 2  chroma edges: vertical and horizontal step edges, 1-pixel chroma stripes (2 luma wide), chroma checkerboard,
         a diagonal edge; luma flat or striped so that chroma siting errors show.
frame 3  seeded noise in all planes, including illegal values (0..255 everywhere): tests clamping.
"""
import random

W, H = 96, 64
NFRAMES = 4
FRAME_BYTES = W * H * 3 // 2


def _new():
    return [bytearray(W * H), bytearray(W * H // 4), bytearray(W * H // 4)]


def _forward(r, g, b, kr, kb):
    """limited-range RGB -> YCbCr rounding (used only to place the primaries)."""
    kg = 1 - kr - kb
    y = kr * r + kg * g + kb * b
    return (round(16 + 219 * y / 255), round(128 + 224 * (b - y) / (2 * (1 - kb)) / 255),
            round(128 + 224 * (r - y) / (2 * (1 - kr)) / 255))


def patches():
    p = []
    for y in (0, 16, 126, 235, 255):
        for cb, cr in ((128, 128), (16, 16), (240, 240), (16, 240), (240, 16), (0, 255), (255, 0), (90, 200)):
            p.append((y, cb, cr))
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255), (255, 0, 255), (255, 255, 0), (255, 255, 255), (0, 0, 0)]
    for kr, kb in ((0.2126, 0.0722), (0.299, 0.114)):
        for c in cols:
            p.append(_forward(*c, kr, kb))
    rnd = random.Random(92)
    while len(p) < 96:
        p.append((rnd.randint(16, 235), rnd.randint(16, 240), rnd.randint(16, 240)))
    return p


def frame0():
    Y, U, V = _new()
    for i, (y, cb, cr) in enumerate(patches()):
        px, py = (i % 12) * 8, (i // 12) * 8
        for r in range(8):
            for c in range(8):
                Y[(py + r) * W + px + c] = y
        for r in range(4):
            for c in range(4):
                U[(py // 2 + r) * (W // 2) + px // 2 + c] = cb
                V[(py // 2 + r) * (W // 2) + px // 2 + c] = cr
    return Y, U, V


def frame1():
    Y, U, V = _new()
    cw = W // 2
    for y in range(H):
        for x in range(W):
            band = y // 16
            if band == 0:
                Y[y * W + x] = x * 255 // (W - 1)
            elif band == 1:
                Y[y * W + x] = 126
            elif band == 2:
                Y[y * W + x] = 100
            else:
                Y[y * W + x] = 16 + x * 219 // (W - 1)
    for y in range(H // 2):
        for x in range(cw):
            band = y // 8
            u = v = 128
            if band == 1:
                u = x * 255 // (cw - 1)
            elif band == 2:
                v = x * 255 // (cw - 1)
            elif band == 3:
                u = 16 + (x + y) * 224 // (cw + H // 2 - 2)
                v = 240 - (x + y) * 224 // (cw + H // 2 - 2)
            U[y * cw + x], V[y * cw + x] = u, v
    return Y, U, V


def frame2():
    Y, U, V = _new()
    cw, ch = W // 2, H // 2
    for y in range(H):
        for x in range(W):
            Y[y * W + x] = 126 if (x // 16 + y // 16) % 3 else 60 + (x + y) % 2 * 100
    for y in range(ch):
        for x in range(cw):
            u, v = 128, 128
            if x < 24 and y < 16:        # quadrant 1: vertical step at chroma column 12
                u, v = (64, 192) if x < 12 else (192, 64)
            elif x >= 24 and y < 16:     # quadrant 2: horizontal step at chroma row 8, 1-sample stripes in the right part
                u, v = (240, 16) if y < 8 else (16, 240)
                if x >= 36:
                    u, v = (30, 220) if x % 2 else (220, 30)
            elif x < 24:                 # quadrant 3: chroma checkerboard
                u, v = (40, 210) if (x + y) % 2 else (210, 40)
            else:                        # quadrant 4: diagonal edge
                u, v = (90, 170) if x - 24 > y - 16 else (170, 90)
            U[y * cw + x], V[y * cw + x] = u, v
    return Y, U, V


def frame3():
    rnd = random.Random(1992)
    Y, U, V = _new()
    for pl in (Y, U, V):
        for i in range(len(pl)):
            pl[i] = rnd.randint(0, 255)
    return Y, U, V


def build():
    return b"".join(bytes(a) + bytes(b) + bytes(c) for a, b, c in (f() for f in (frame0, frame1, frame2, frame3)))
