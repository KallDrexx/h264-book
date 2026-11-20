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


VIOLATIONS = 0        # how many intermediate values left the signed 16-bit range (a non-conforming stream; ffmpeg may then differ)


class Tracker:
    def __init__(self):
        self.range_ok = True

    def chk(self, v):
        global VIOLATIONS
        if not -32768 <= v <= 32767:
            self.range_ok = False
            VIOLATIONS += 1
        return v


def norm_adjust(m, i, j):
    n = NORM[m]
    return n[0] if i % 2 == 0 and j % 2 == 0 else n[1] if i % 2 == 1 and j % 2 == 1 else n[2]


def level_scale(m, i, j):
    """LevelScale4x4(m, i, j) = weightScale(16) * normAdjust4x4(m, i, j)."""
    return 16 * norm_adjust(m, i, j)


MUTANT = None       # 5.5 harness self-test: deliberately wrong chroma QP rules


def chroma_qp(qpy, offset):
    qpi = max(0, min(51, qpy + offset))
    if MUTANT == "cqp_noclip":
        qpi = qpy + offset
    elif MUTANT == "cqp_no_table":
        return qpi
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


# ================================================================ 8x8 transform (High profile, spec 8.5.7, 8.5.13)
def _zigzag(n):
    """Frame zig-zag scan of an n x n block: scan position -> (row, column)."""
    out, i, j, up = [], 0, 0, True
    for _ in range(n * n):
        out.append((i, j))
        if up:
            if j == n - 1:
                i += 1
                up = False
            elif i == 0:
                j += 1
                up = False
            else:
                i, j = i - 1, j + 1
        else:
            if i == n - 1:
                j += 1
                up = True
            elif j == 0:
                i += 1
                up = True
            else:
                i, j = i + 1, j - 1
    return out


ZIGZAG8 = _zigzag(8)
assert _zigzag(4) == ZIGZAG

# normAdjust8x8 (8-317): v[m][class] with the six position classes of the spec
NORM8 = [(20, 18, 32, 19, 25, 24), (22, 19, 35, 21, 28, 26), (26, 23, 42, 24, 33, 31), (28, 25, 45, 26, 35, 33),
         (32, 28, 51, 30, 40, 38), (36, 32, 58, 34, 46, 43)]


T8_MUTANT = None       # harness self-test: ONE deliberately wrong 8x8 rule (see hdec.DECODER_MUTANTS)


def norm_class8(i, j):
    if T8_MUTANT == "t8_scale_4x4_norm":
        return (0 if i % 2 == 0 and j % 2 == 0 else 1 if i % 2 == 1 and j % 2 == 1 else 3)
    if i % 4 == 0 and j % 4 == 0:
        return 0
    if i % 2 == 1 and j % 2 == 1:
        return 1
    if i % 4 == 2 and j % 4 == 2:
        return 2
    if (i % 4 == 0 and j % 2 == 1) or (i % 2 == 1 and j % 4 == 0):
        return 3
    if (i % 4 == 0 and j % 4 == 2) or (i % 4 == 2 and j % 4 == 0):
        return 4
    return 5


def level_scale8(m, i, j):
    """LevelScale8x8(m, i, j) = weightScale8x8(i, j) (flat 16) * normAdjust8x8(m, i, j)."""
    return 16 * NORM8[m][norm_class8(i, j)]


def inverse_zigzag8(levels):
    m = [[0] * 8 for _ in range(8)]
    for k, v in enumerate(levels):
        i, j = ZIGZAG8[k] if T8_MUTANT != "t8_zigzag4" else (ZIGZAG[k] if k < 16 else (k // 8, k % 8))
        m[i][j] = v
    return m


def scale8x8(c, qp, tr):
    d = [[0] * 8 for _ in range(8)]
    for i in range(8):
        for j in range(8):
            ls = level_scale8(qp % 6, i, j)
            if qp >= 36 and T8_MUTANT != "t8_shift_qp":
                v = (c[i][j] * ls) << (qp // 6 - 6)
            else:
                v = (c[i][j] * ls + (1 << (5 - qp // 6))) >> (6 - qp // 6)
            d[i][j] = tr.chk(v)
    return d


def _bfly8(d, tr):
    e0 = d[0] + d[4]
    e1 = -d[3] + d[5] - d[7] - (d[7] >> 1)
    e2 = d[0] - d[4]
    e3 = d[1] + d[7] - d[3] - (d[3] >> 1)
    e4 = (d[2] >> 1) - d[6]
    e5 = -d[1] + d[7] + d[5] + (d[5] >> 1)
    e6 = d[2] + (d[6] >> 1)
    e7 = d[3] + d[5] + d[1] + (d[1] >> 1)
    f0, f1, f2, f3 = e0 + e6, e1 + (e7 >> 2), e2 + e4, e3 + (e5 >> 2)
    f4, f5, f6, f7 = e2 - e4, (e3 >> 2) - e5, e0 - e6, e7 - (e1 >> 2)
    return [tr.chk(v) for v in (f0 + f7, f2 + f5, f4 + f3, f6 + f1, f6 - f1, f4 - f3, f2 - f5, f0 - f7)]


def idct8x8(d, tr):
    rows = [_bfly8(d[i], tr) for i in range(8)]
    r = [[0] * 8 for _ in range(8)]
    for j in range(8):
        col = _bfly8([rows[i][j] for i in range(8)], tr)
        for i in range(8):
            r[i][j] = (col[i] + 32) >> 6 if T8_MUTANT != "t8_no_final_round" else col[i] >> 6
    return r


def residual8x8(levels, qp, tr):
    """levels: 64 coefficients in 8x8 zig-zag scan order -> 8x8 residual rows."""
    return idct8x8(scale8x8(inverse_zigzag8(levels), qp, tr), tr)
