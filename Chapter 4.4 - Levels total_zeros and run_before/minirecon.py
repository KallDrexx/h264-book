"""A MINIMAL, deliberately limited pixel reconstruction used only as ground truth by Chapter 4.4's harness.

It reconstructs ONE macroblock that has no neighbours (the first macroblock of a slice), so every intra prediction
is trivial, from the coefficient arrays of its residual blocks:

  * Intra16x16 with DC prediction: prediction = 128 everywhere (no neighbours);
  * I_NxN with Intra 4x4 DC prediction: block by block in luma4x4BlkIdx order, the prediction is the rounded mean of
    the (already reconstructed) left / above neighbours inside the macroblock, or 128;
  * chroma with DC prediction: 128 everywhere.

Dequantisation and inverse transforms follow 8.5.9 - 8.5.12 with flat scaling lists (all weights 16), 8-bit video:
LevelScale4x4 = 16 * normAdjust4x4, 4x4 inverse transform, Intra16x16 luma DC Hadamard, chroma DC 2x2 transform,
chroma QP from Table 8-15.  It is Chapter 5.2 in miniature; the book's Phase 5 builds the real thing.  It is only
trusted because ffmpeg's decoded pixels are compared with it in the harness (a mistake here shows up as a pixel
mismatch on the synthetic stream, which is how it was debugged).

Input: coefficient lists in the form the residual parser produces them: for every residual block the list of its
maxNumCoeff levels (16 for i16dc / luma, 15 for i16ac / cbac / crac, 4 for cbdc / crdc).  List entry k of a
15-coefficient block is scan position k + 1.

range_ok is False when a dequantised coefficient or an intermediate value of the inverse transform leaves the signed
16-bit range (a conforming stream must not, 8.5.12; ffmpeg stores them in 16 bits).  clipped is True when a
reconstructed sample had to be clipped to 0..255 (the pixel then says less about the coefficients).
"""

# frame zig-zag scan of a 4x4 block (Table 8-12): scan position -> (row, column)
ZIGZAG = [(0, 0), (0, 1), (1, 0), (2, 0), (1, 1), (0, 2), (0, 3), (1, 2), (2, 1), (3, 0), (3, 1), (2, 2), (1, 3),
          (2, 3), (3, 2), (3, 3)]
NORM = [(10, 16, 13), (11, 18, 14), (13, 20, 16), (14, 23, 18), (16, 25, 20), (18, 29, 23)]
QPC = [29, 30, 31, 32, 32, 33, 34, 34, 35, 35, 36, 36, 37, 37, 37, 38, 38, 38, 39, 39, 39, 39]   # qPI 30..51
BLK_XY = [(((i >> 2) & 1) * 2 + (i & 1), ((i >> 3) & 1) * 2 + ((i >> 1) & 1)) for i in range(16)]   # (x, y)
I16_MODE_DC = 2


def level_scale(qp, i, j):
    n = NORM[qp % 6]
    return 16 * (n[0] if i % 2 == 0 and j % 2 == 0 else n[1] if i % 2 == 1 and j % 2 == 1 else n[2])


def chroma_qp(qp, offset):
    qpi = max(0, min(51, qp + offset))
    return qpi if qpi < 30 else QPC[qpi - 30]


class Tracker:
    def __init__(self):
        self.range_ok = True
        self.clipped = False

    def chk(self, v):
        if not -32768 <= v <= 32767:
            self.range_ok = False
        return v


def to_matrix(coefs, maxnum):
    """Levels in scan order -> 4x4 matrix (row, column); list entry k of a 15-coefficient block is scan position k+1."""
    m = [[0] * 4 for _ in range(4)]
    first = 16 - maxnum
    for k, v in enumerate(coefs):
        r, c = ZIGZAG[k + first]
        m[r][c] = v
    return m


def scale_ac(m, qp, tr, skip_dc=False):
    d = [[0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            if skip_dc and i == 0 and j == 0:
                continue
            ls = level_scale(qp, i, j)
            if qp >= 24:
                v = (m[i][j] * ls) << (qp // 6 - 4)
            else:
                v = (m[i][j] * ls + (1 << (3 - qp // 6))) >> (4 - qp // 6)
            d[i][j] = tr.chk(v)
    return d


def idct4(d, tr):
    """8.5.12.2: rows first, then columns; -> residual after (x + 32) >> 6."""
    def one(a):
        e0, e1 = a[0] + a[2], a[0] - a[2]
        e2, e3 = (a[1] >> 1) - a[3], a[1] + (a[3] >> 1)
        return [tr.chk(e0 + e3), tr.chk(e1 + e2), tr.chk(e1 - e2), tr.chk(e0 - e3)]
    rows = [one(d[i]) for i in range(4)]
    out = [[0] * 4 for _ in range(4)]
    for j in range(4):
        col = one([rows[i][j] for i in range(4)])
        for i in range(4):
            out[i][j] = (col[i] + 32) >> 6
    return out


def put(plane, x0, y0, pred, res, tr):
    for i in range(4):
        for j in range(4):
            v = pred[i][j] + res[i][j]
            if not 0 <= v <= 255:
                tr.clipped = True
            plane[y0 + i][x0 + j] = max(0, min(255, v))


def luma_dc_dequant(c, qp, tr):
    h = [[1, 1, 1, 1], [1, 1, -1, -1], [1, -1, -1, 1], [1, -1, 1, -1]]
    t = [[sum(h[i][k] * c[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
    f = [[sum(t[i][k] * h[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
    ls = level_scale(qp, 0, 0)
    out = [[0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            v = (f[i][j] * ls) << (qp // 6 - 6) if qp >= 36 else (f[i][j] * ls + (1 << (5 - qp // 6))) >> (6 - qp // 6)
            out[i][j] = tr.chk(v)
    return out


def chroma_dc_dequant(c4, qpc, tr):
    c = [[c4[0], c4[1]], [c4[2], c4[3]]]
    h = [[1, 1], [1, -1]]
    t = [[sum(h[i][k] * c[k][j] for k in range(2)) for j in range(2)] for i in range(2)]
    f = [[sum(t[i][k] * h[k][j] for k in range(2)) for j in range(2)] for i in range(2)]
    ls = level_scale(qpc, 0, 0)
    return [[tr.chk(((f[i][j] * ls) << (qpc // 6)) >> 5) for j in range(2)] for i in range(2)]


def reconstruct(kind, qp, blocks, chroma_offset=0):
    """chroma_offset: chroma_qp_index_offset for Cb and Cr, an int or a (Cb, Cr) pair (second_chroma_qp_index_offset)."""
    offs = (chroma_offset, chroma_offset) if isinstance(chroma_offset, int) else tuple(chroma_offset)
    return _reconstruct(kind, qp, blocks, offs)


def _reconstruct(kind, qp, blocks, offs):
    """kind 'i16' or 'nxn'; blocks: dict (block kind, idx) -> coefficient list (absent block = all zero).
    -> (Y rows 16x16, Cb rows 8x8, Cr rows 8x8, range_ok, clipped)."""
    tr = Tracker()
    y = [[0] * 16 for _ in range(16)]
    zero = lambda n: [0] * n  # noqa: E731
    if kind == "i16":
        dc = luma_dc_dequant(to_matrix(blocks.get(("i16dc", 0), zero(16)), 16), qp, tr)
        for idx in range(16):
            bx, by = BLK_XY[idx]
            d = scale_ac(to_matrix(blocks.get(("i16ac", idx), zero(15)), 15), qp, tr, skip_dc=True)
            d[0][0] = dc[by][bx]
            put(y, bx * 4, by * 4, [[128] * 4] * 4, idct4(d, tr), tr)
    else:
        for idx in range(16):
            bx, by = BLK_XY[idx]
            top = [y[by * 4 - 1][bx * 4 + k] for k in range(4)] if by > 0 else None
            left = [y[by * 4 + k][bx * 4 - 1] for k in range(4)] if bx > 0 else None
            if top and left:
                p = (sum(top) + sum(left) + 4) >> 3
            elif left:
                p = (sum(left) + 2) >> 2
            elif top:
                p = (sum(top) + 2) >> 2
            else:
                p = 128
            d = scale_ac(to_matrix(blocks.get(("luma", idx), zero(16)), 16), qp, tr)
            put(y, bx * 4, by * 4, [[p] * 4] * 4, idct4(d, tr), tr)
    planes = []
    for (name_dc, name_ac), off in zip((("cbdc", "cbac"), ("crdc", "crac")), offs):
        qpc = chroma_qp(qp, off)
        dcc = chroma_dc_dequant(blocks.get((name_dc, 0), zero(4)), qpc, tr)
        pl = [[0] * 8 for _ in range(8)]
        for idx in range(4):
            bx, by = idx & 1, idx >> 1
            d = scale_ac(to_matrix(blocks.get((name_ac, idx), zero(15)), 15), qpc, tr, skip_dc=True)
            d[0][0] = dcc[by][bx]
            put(pl, bx * 4, by * 4, [[128] * 4] * 4, idct4(d, tr), tr)
        planes.append(pl)
    return y, planes[0], planes[1], tr.range_ok, tr.clipped


def flat_bytes(y, cb, cr):
    """The 384 samples in the order mbdump.mb_pixels returns them (16x16 Y, 8x8 Cb, 8x8 Cr, row by row)."""
    return bytes(v for pl in (y, cb, cr) for row in pl for v in row)
