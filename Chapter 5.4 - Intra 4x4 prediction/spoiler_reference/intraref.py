"""Reference Intra16x16 and chroma prediction (spec 8.3.3, 8.3.4) for 8-bit 4:2:0, plus macroblock reconstruction.

SPOILER: this is the solution to Chapter 5.3 (copied unchanged from that chapter so that this folder runs on its own).
It is not Chapter 5.4's subject; Chapter 5.4's solution is intra4ref.py.  Write your own first.

Planes are lists of rows (plane[y][x]) of ints.  A prediction function is given the plane, the position of the
macroblock's top-left sample inside that plane and three availability flags (left A, above B, above-left D); it reads only
the neighbouring samples the mode needs and returns the predicted block as a list of rows.
"""
import transref
from transref import clip1

# Deliberate mistakes for the harness self-test (test_intra16.py --self-test): the harness must reject each one.
MUTANT = None


def i16_pred(plane, x0, y0, mode, a, b, d):
    """Intra16x16 prediction (8.3.3).  mode 0 vertical, 1 horizontal, 2 DC, 3 plane.  Raises ValueError if the mode
    needs a neighbour that is not available."""
    top = [plane[y0 - 1][x0 + x] for x in range(16)] if b else None
    left = [plane[y0 + y][x0 - 1] for y in range(16)] if a else None
    if mode == 0:
        if not b:
            raise ValueError("vertical prediction needs the above neighbour")
        return [list(top) for _ in range(16)]
    if mode == 1:
        if not a:
            raise ValueError("horizontal prediction needs the left neighbour")
        return [[left[y]] * 16 for y in range(16)]
    if mode == 2:
        if a and b:
            v = (sum(top) + sum(left) + 16) >> 5
        elif a:
            v = (sum(left) + 8) >> 4
        elif b:
            v = (sum(top) + (7 if MUTANT == "ldc_top_round" else 8)) >> 4
        else:
            v = 128
        return [[v] * 16 for _ in range(16)]
    if not (a and b and d):
        raise ValueError("plane prediction needs left, above and above-left neighbours")
    corner = plane[y0 - 1][x0 - 1]
    t = lambda x: corner if x < 0 else top[x]      # noqa: E731  p[x, -1], x = -1 is the corner
    l = lambda y: corner if y < 0 else left[y]     # noqa: E731  p[-1, y]
    h = sum((i + 1) * (t(8 + i) - t(6 - i)) for i in range(8))
    v = sum((i + 1) * (l(8 + i) - l(6 - i)) for i in range(8))
    aa = 16 * (left[15] + top[15])
    bb = (4 * h + 32) >> 6 if MUTANT == "lplane_b4" else (5 * h + 32) >> 6
    cc = (5 * v + 32) >> 6
    cl = (lambda v: v) if MUTANT == "lplane_noclip" else clip1     # noqa: E731
    return [[cl((aa + bb * (x - 7) + cc * (y - 7) + 16) >> 5) for x in range(16)] for y in range(16)]


def chroma_pred(plane, x0, y0, mode, a, b, d):
    """Chroma prediction of one 8x8 component block (8.3.4, 4:2:0).  mode 0 DC, 1 horizontal, 2 vertical, 3 plane."""
    top = [plane[y0 - 1][x0 + x] for x in range(8)] if b else None
    left = [plane[y0 + y][x0 - 1] for y in range(8)] if a else None
    if mode == 0:
        out = [[0] * 8 for _ in range(8)]
        for blk in range(4):
            xo, yo = 4 * (blk & 1), 4 * (blk >> 1)
            st = sum(top[xo:xo + 4]) if b else None
            sl = sum(left[yo:yo + 4]) if a else None
            if (xo, yo) in ((0, 0), (4, 4)) and not (MUTANT == "cdc_blk3_top" and xo == 4):
                if a and b:
                    v = (st + sl + 4) >> 3
                elif a:
                    v = (sl + 2) >> 2
                elif b:
                    v = (st + 2) >> 2
                else:
                    v = 128
            elif yo == 0 and MUTANT == "cdc_blk1_left":
                v = (sl + 2) >> 2 if a else (st + 2) >> 2 if b else 128
            elif yo == 0:                               # block 1: prefers the above row
                v = (st + 2) >> 2 if b else (sl + 2) >> 2 if a else 128
            elif MUTANT == "cdc_blk2_top":
                v = (st + 2) >> 2 if b else (sl + 2) >> 2 if a else 128
            else:                                       # block 2: prefers the left column
                v = (sl + 2) >> 2 if a else (st + 2) >> 2 if b else 128
            for y in range(4):
                for x in range(4):
                    out[yo + y][xo + x] = v
        return out
    if MUTANT == "cmode_swap" and mode in (1, 2):
        mode = 3 - mode
    if mode == 1:
        if not a:
            raise ValueError("horizontal chroma prediction needs the left neighbour")
        return [[left[y]] * 8 for y in range(8)]
    if mode == 2:
        if not b:
            raise ValueError("vertical chroma prediction needs the above neighbour")
        return [list(top) for _ in range(8)]
    if not (a and b and d):
        raise ValueError("plane chroma prediction needs left, above and above-left neighbours")
    corner = plane[y0 - 1][x0 - 1]
    t = lambda x: corner if x < 0 else top[x]      # noqa: E731
    l = lambda y: corner if y < 0 else left[y]     # noqa: E731
    h = sum((i + 1) * (t(4 + i) - t(2 - i)) for i in range(4))
    v = sum((i + 1) * (l(4 + i) - l(2 - i)) for i in range(4))
    aa = 16 * (left[7] + top[7])
    bb = (35 * h + 32) >> 6 if MUTANT == "cplane_b35" else (34 * h + 32) >> 6
    cc = (34 * v + 32) >> 6
    return [[clip1((aa + bb * (x - 3) + cc * (y - 3) + 16) >> 5) for x in range(8)] for y in range(8)]


def put(plane, x0, y0, block):
    for y, row in enumerate(block):
        plane[y0 + y][x0:x0 + len(row)] = row


def add_clip(pred, res):
    return [[clip1(p + r) for p, r in zip(pr, rr)] for pr, rr in zip(pred, res)]


def zeros(n):
    return [0] * n


def reconstruct_i16(planes, mbx, mby, avail, mb, qp, offs):
    """Reconstruct one Intra16x16 macroblock into planes = (Y, Cb, Cr) (lists of rows).
    avail = (A, B, D); mb = dict(i16_mode, chroma_pred, blocks=dict (kind, idx) -> level list); qp = QP_Y;
    offs = (chroma_qp_index_offset, second_chroma_qp_index_offset)."""
    tr = transref.Tracker()
    a, b, d = avail
    bl = mb["blocks"]
    g = lambda k, n: bl.get(k, zeros(n))     # noqa: E731
    pred = i16_pred(planes[0], 16 * mbx, 16 * mby, mb["i16_mode"], a, b, d)
    res = transref.i16_residual(qp, g(("i16dc", 0), 16), [g(("i16ac", k), 15) for k in range(16)], tr)
    put(planes[0], 16 * mbx, 16 * mby, add_clip(pred, res))
    reconstruct_chroma(planes, mbx, mby, avail, mb, qp, offs, tr)
    return tr.range_ok


def reconstruct_chroma(planes, mbx, mby, avail, mb, qp, offs, tr):
    a, b, d = avail
    bl = mb["blocks"]
    g = lambda k, n: bl.get(k, zeros(n))     # noqa: E731
    for p, (name, off) in enumerate(zip(("cb", "cr"), offs)):
        pred = chroma_pred(planes[1 + p], 8 * mbx, 8 * mby, mb["chroma_pred"], a, b, d)
        res = transref.chroma_residual(transref.chroma_qp(qp, off), g((name + "dc", 0), 4),
                                       [g((name + "ac", k), 15) for k in range(4)], tr)
        put(planes[1 + p], 8 * mbx, 8 * mby, add_clip(pred, res))
