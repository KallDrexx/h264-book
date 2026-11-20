"""Reference implementation of the Chapter 5.2 arithmetic (spec 8.5.6 - 8.5.12, 8-bit 4:2:0, flat scaling lists).

Used by test_transforms.py as the truth for the vector file, and by recongen.py / the ffmpeg check as a
mini reconstruction.  Matrices are lists of rows, m[i][j] = row i, column j (the spec's c_ij, i = row).
A Tracker records whether a value leaves the signed 16-bit range that 8.5.12 requires of a conforming stream.
"""

# Table 8-12 frame zig-zag: scan position -> (row i, column j)
ZIGZAG = [(0, 0), (0, 1), (1, 0), (2, 0), (1, 1), (0, 2), (0, 3), (1, 2), (2, 1), (3, 0), (3, 1), (2, 2), (1, 3),
          (2, 3), (3, 2), (3, 3)]
# normAdjust4x4: v[m][0] for (even,even), v[m][1] for (odd,odd), v[m][2] otherwise
NORM = [(10, 16, 13), (11, 18, 14), (13, 20, 16), (14, 23, 18), (16, 25, 20), (18, 29, 23)]
# Table 8-15, qPI 30..51
QPC = [29, 30, 31, 32, 32, 33, 34, 34, 35, 35, 36, 36, 37, 37, 37, 38, 38, 38, 39, 39, 39, 39]
# luma4x4BlkIdx -> (x, y) in units of 4 samples
BLK_XY = [(2 * ((b >> 2) & 1) + (b & 1), 2 * (b >> 3) + ((b >> 1) & 1)) for b in range(16)]


class Tracker:
    def __init__(self):
        self.range_ok = True

    def chk(self, v):
        if not -32768 <= v <= 32767:
            self.range_ok = False
        return v


def norm_adjust(m, i, j):
    n = NORM[m]
    return n[0] if i % 2 == 0 and j % 2 == 0 else n[1] if i % 2 == 1 and j % 2 == 1 else n[2]


def level_scale(m, i, j):
    """LevelScale4x4(m, i, j) = weightScale(16) * normAdjust4x4(m, i, j)."""
    return 16 * norm_adjust(m, i, j)


def chroma_qp(qpy, offset):
    qpi = max(0, min(51, qpy + offset))
    return qpi if qpi < 30 else QPC[qpi - 30]


def inverse_zigzag(levels, first=0):
    """Levels in scan order -> matrix; levels[k] sits at scan position k + first."""
    m = [[0] * 4 for _ in range(4)]
    for k, v in enumerate(levels):
        i, j = ZIGZAG[k + first]
        m[i][j] = v
    return m


def scale4x4(c, qp, dc_pre_scaled, tr):
    d = [[0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            if i == 0 and j == 0 and dc_pre_scaled:
                d[0][0] = tr.chk(c[0][0])
                continue
            ls = level_scale(qp % 6, i, j)
            if qp >= 24:
                v = (c[i][j] * ls) << (qp // 6 - 4)
            else:
                v = (c[i][j] * ls + (1 << (3 - qp // 6))) >> (4 - qp // 6)
            d[i][j] = tr.chk(v)
    return d


def _bfly(a, tr):
    e0, e1 = a[0] + a[2], a[0] - a[2]
    e2, e3 = (a[1] >> 1) - a[3], a[1] + (a[3] >> 1)
    return [tr.chk(e0 + e3), tr.chk(e1 + e2), tr.chk(e1 - e2), tr.chk(e0 - e3)]


def idct4x4(d, tr):
    rows = [_bfly(d[i], tr) for i in range(4)]
    r = [[0] * 4 for _ in range(4)]
    for j in range(4):
        col = _bfly([rows[i][j] for i in range(4)], tr)
        for i in range(4):
            r[i][j] = (col[i] + 32) >> 6
    return r


def residual4x4(c, qp, dc_pre_scaled, tr):
    return idct4x4(scale4x4(c, qp, dc_pre_scaled, tr), tr)


def luma_dc(c, qp, tr):
    h = [[1, 1, 1, 1], [1, 1, -1, -1], [1, -1, -1, 1], [1, -1, 1, -1]]
    mul = lambda a, b: [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]  # noqa: E731
    f = mul(mul(h, c), h)
    for row in f:
        for v in row:
            tr.chk(v)
    ls = level_scale(qp % 6, 0, 0)
    out = [[0] * 4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            if qp >= 36:
                v = (f[i][j] * ls) << (qp // 6 - 6)
            else:
                v = (f[i][j] * ls + (1 << (5 - qp // 6))) >> (6 - qp // 6)
            out[i][j] = tr.chk(v)
    return out


def chroma_dc(c4, qpc, tr):
    c = [[c4[0], c4[1]], [c4[2], c4[3]]]
    a = [[1, 1], [1, -1]]
    mul = lambda x, y: [[sum(x[i][k] * y[k][j] for k in range(2)) for j in range(2)] for i in range(2)]  # noqa: E731
    f = mul(mul(a, c), a)
    for row in f:
        for v in row:
            tr.chk(v)
    ls = level_scale(qpc % 6, 0, 0)
    return [[tr.chk(((f[i][j] * ls) << (qpc // 6)) >> 5) for j in range(2)] for i in range(2)]


def clip1(v):
    return 0 if v < 0 else 255 if v > 255 else v


def flat(m):
    return [v for row in m for v in row]


def unflat(v, n):
    return [list(v[i * n:(i + 1) * n]) for i in range(n)]


def i16_residual(qp, dc_levels, ac_levels, tr):
    """dc_levels: 16 in scan order; ac_levels: 16 lists of 15 (luma4x4BlkIdx order, scan positions 1..15).
    -> 16x16 residual rows."""
    dcy = luma_dc(inverse_zigzag(dc_levels), qp, tr)
    res = [[0] * 16 for _ in range(16)]
    for b in range(16):
        bx, by = BLK_XY[b]
        c = inverse_zigzag(ac_levels[b], 1)
        c[0][0] = dcy[by][bx]
        r = residual4x4(c, qp, True, tr)
        for i in range(4):
            for j in range(4):
                res[4 * by + i][4 * bx + j] = r[i][j]
    return res


def chroma_residual(qpc, dc_levels, ac_levels, tr):
    """dc_levels: 4 (chroma4x4BlkIdx order); ac_levels: 4 lists of 15.  -> 8x8 residual rows."""
    dcc = chroma_dc(dc_levels, qpc, tr)
    res = [[0] * 8 for _ in range(8)]
    for b in range(4):
        bx, by = b & 1, b >> 1
        c = inverse_zigzag(ac_levels[b], 1)
        c[0][0] = dcc[by][bx]
        r = residual4x4(c, qpc, True, tr)
        for i in range(4):
            for j in range(4):
                res[4 * by + i][4 * bx + j] = r[i][j]
    return res


# ----------------------------------------------------------- mini macroblock reconstruction (first MB of a slice)
def reconstruct_mb(kind, qp, offs, arrays):
    """Single macroblock with no neighbours.  kind 'i16' (Intra16x16 DC prediction = 128) or 'nxn' (Intra 4x4 DC
    prediction, block by block); chroma DC prediction = 128.  arrays: dict (name, idx) -> level list, absent = zeros.
    Names: i16dc(16) i16ac(15, idx 0..15) luma(16, idx 0..15) cbdc/crdc(4) cbac/crac(15, idx 0..3).
    -> (384 bytes Y,Cb,Cr, range_ok)."""
    tr = Tracker()
    z = lambda n: [0] * n  # noqa: E731
    g = lambda k, n: arrays.get(k, z(n))  # noqa: E731
    y = [[0] * 16 for _ in range(16)]
    if kind == "i16":
        res = i16_residual(qp, g(("i16dc", 0), 16), [g(("i16ac", b), 15) for b in range(16)], tr)
        y = [[clip1(128 + res[i][j]) for j in range(16)] for i in range(16)]
    else:
        for b in range(16):
            bx, by = BLK_XY[b]
            top = [y[4 * by - 1][4 * bx + k] for k in range(4)] if by else None
            left = [y[4 * by + k][4 * bx - 1] for k in range(4)] if bx else None
            if top and left:
                p = (sum(top) + sum(left) + 4) >> 3
            elif left:
                p = (sum(left) + 2) >> 2
            elif top:
                p = (sum(top) + 2) >> 2
            else:
                p = 128
            r = residual4x4(inverse_zigzag(g(("luma", b), 16)), qp, False, tr)
            for i in range(4):
                for j in range(4):
                    y[4 * by + i][4 * bx + j] = clip1(p + r[i][j])
    planes = []
    for n, off in zip(("cb", "cr"), offs):
        res = chroma_residual(chroma_qp(qp, off), g((n + "dc", 0), 4), [g((n + "ac", b), 15) for b in range(4)], tr)
        planes.append([[clip1(128 + v) for v in row] for row in res])
    return bytes(v for pl in (y, planes[0], planes[1]) for row in pl for v in row), tr.range_ok
