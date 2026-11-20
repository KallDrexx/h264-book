"""SPOILER - do not open until you have tried (and, for the filter itself, until Chapter 6.2).

Reference deblocking filter (8.7) for 8-bit 4:2:0 frames, written so that Chapter 6.2 can reuse it.  It is used in Chapter 6.1
only to prove, BY CONSEQUENCE, that the bS grid of bsref.py is right: the grid is fed to this filter, the filter is run on
ffmpeg's UNFILTERED picture (ffmpeg -skip_loop_filter all), and the result must equal ffmpeg's filtered picture.

Planes are lists of rows (lists of ints) covering whole macroblocks.  filter_picture() works in place, macroblock by macroblock
in raster order, vertical edges left to right and then horizontal edges top to bottom, reading samples already filtered.
"""
ALPHA = [0] * 16 + [4, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15, 17, 20, 22, 25, 28, 32, 36, 40, 45, 50, 56, 63, 71, 80, 90, 101, 113,
                    127, 144, 162, 182, 203, 226, 255, 255]
BETA = [0] * 16 + [2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15, 16, 16,
                   17, 17, 18, 18]
# tC0 for bS = 1, 2, 3 per indexA (Table 8-17)
TC0 = [(0, 0, 0)] * 17 + [(0, 0, 1)] * 4 + [(0, 1, 1)] * 2 + [(1, 1, 1)] * 4 + [(1, 1, 2)] * 4 + [(1, 2, 3)] * 2 + [
    (2, 2, 3), (2, 2, 4), (2, 3, 4), (2, 3, 4), (3, 3, 5), (3, 4, 6), (3, 4, 6), (4, 5, 7), (4, 5, 8), (4, 6, 9),
    (5, 7, 10), (6, 8, 11), (6, 8, 13), (7, 10, 14), (8, 11, 16), (9, 12, 18), (10, 13, 20), (11, 15, 23), (13, 17, 25)]
QPC = [29, 30, 31, 32, 32, 33, 34, 34, 35, 35, 36, 36, 37, 37, 37, 38, 38, 38, 39, 39, 39, 39]
assert len(ALPHA) == len(BETA) == len(TC0) == 52


def clip3(lo, hi, v):
    return lo if v < lo else hi if v > hi else v


def chroma_qp(qpy, offset):
    qpi = clip3(0, 51, qpy + offset)
    return qpi if qpi < 30 else QPC[qpi - 30]


def filter_line(p, q, bs, qp_p, qp_q, off_a, off_b, luma):
    """One line of samples across an edge.  p[0] is the sample next to the edge (p0), p[1] = p1 ..., same for q (p and q hold
    at least 3 samples for luma, 2 for chroma).  -> (new p, new q) as lists (same lengths)."""
    qav = (qp_p + qp_q + 1) >> 1
    ia = clip3(0, 51, qav + off_a)
    ib = clip3(0, 51, qav + off_b)
    alpha, beta = ALPHA[ia], BETA[ib]
    p, q = list(p), list(q)
    p0, p1, q0, q1 = p[0], p[1], q[0], q[1]
    if not (bs and abs(p0 - q0) < alpha and abs(p1 - p0) < beta and abs(q1 - q0) < beta):
        return p, q
    if bs < 4:
        tc0 = TC0[ia][bs - 1]
        if luma:
            ap, aq = abs(p[2] - p0), abs(q[2] - q0)
            tc = tc0 + (ap < beta) + (aq < beta)
        else:
            tc = tc0 + 1
        delta = clip3(-tc, tc, (((q0 - p0) << 2) + (p1 - q1) + 4) >> 3)
        p[0], q[0] = clip3(0, 255, p0 + delta), clip3(0, 255, q0 - delta)
        if luma:
            if ap < beta:
                p[1] = p1 + clip3(-tc0, tc0, (p[2] + ((p0 + q0 + 1) >> 1) - (p1 << 1)) >> 1)
            if aq < beta:
                q[1] = q1 + clip3(-tc0, tc0, (q[2] + ((p0 + q0 + 1) >> 1) - (q1 << 1)) >> 1)
        return p, q
    # bS == 4
    if luma:
        p2, q2, p3, q3 = p[2], q[2], p[3], q[3]
        small = abs(p0 - q0) < ((alpha >> 2) + 2)
        if small and abs(p2 - p0) < beta:
            p[0] = (p2 + 2 * p1 + 2 * p0 + 2 * q0 + q1 + 4) >> 3
            p[1] = (p2 + p1 + p0 + q0 + 2) >> 2
            p[2] = (2 * p3 + 3 * p2 + p1 + p0 + q0 + 4) >> 3
        else:
            p[0] = (2 * p1 + p0 + q1 + 2) >> 2
        if small and abs(q2 - q0) < beta:
            q[0] = (p1 + 2 * p0 + 2 * q0 + 2 * q1 + q2 + 4) >> 3
            q[1] = (p0 + q0 + q1 + q2 + 2) >> 2
            q[2] = (2 * q3 + 3 * q2 + q1 + q0 + p0 + 4) >> 3
        else:
            q[0] = (2 * q1 + q0 + p1 + 2) >> 2
    else:
        p[0] = (2 * p1 + p0 + q1 + 2) >> 2
        q[0] = (2 * q1 + q0 + p1 + 2) >> 2
    return p, q


def _vertical(plane, x, y0, n, bss, per, qp_p, qp_q, off_a, off_b, luma):
    taps = 4 if luma else 2
    for k in range(n):
        row = plane[y0 + k]
        p, q = filter_line([row[x - 1 - i] for i in range(taps)], [row[x + i] for i in range(taps)], bss[k // per],
                           qp_p, qp_q, off_a, off_b, luma)
        for i in range(taps):
            row[x - 1 - i], row[x + i] = p[i], q[i]


def _horizontal(plane, x0, y, n, bss, per, qp_p, qp_q, off_a, off_b, luma):
    taps = 4 if luma else 2
    for k in range(n):
        x = x0 + k
        p, q = filter_line([plane[y - 1 - i][x] for i in range(taps)], [plane[y + i][x] for i in range(taps)], bss[k // per],
                           qp_p, qp_q, off_a, off_b, luma)
        for i in range(taps):
            plane[y - 1 - i][x], plane[y + i][x] = p[i], q[i]


def filter_picture(planes, w_mbs, bs_grid, qp_y, pcm, offs_a, offs_b, chroma_offsets):
    """planes = (Y, Cb, Cr).  bs_grid[addr] = dict(v=[16], h=[16]) as produced by bsref (0 = edge not filtered).
    qp_y[addr] = QP_Y, pcm[addr] = I_PCM flag, offs_a[addr] / offs_b[addr] = FilterOffsetA / B (= 2 * the slice_*_div2 value of
    the slice containing the macroblock), chroma_offsets = (cb, cr) chroma_qp_index_offset values."""
    n = len(bs_grid)
    for addr in range(n):
        mx, my = addr % w_mbs, addr // w_mbs
        g = bs_grid[addr]
        oa, ob = offs_a[addr], offs_b[addr]
        qpq = 0 if pcm[addr] else qp_y[addr]
        for vertical in (True, False):
            bs = g["v"] if vertical else g["h"]
            for e in range(4):
                seg = bs[4 * e:4 * e + 4]
                if not any(seg):
                    continue
                nb = addr - 1 if (e == 0 and vertical) else addr - w_mbs if (e == 0) else addr
                qpp = 0 if pcm[nb] else qp_y[nb]
                if vertical:
                    _vertical(planes[0], 16 * mx + 4 * e, 16 * my, 16, seg, 4, qpp, qpq, oa, ob, True)
                else:
                    _horizontal(planes[0], 16 * mx, 16 * my + 4 * e, 16, seg, 4, qpp, qpq, oa, ob, True)
                if e in (0, 2):
                    ce = e // 2
                    for ci in range(2):
                        co = chroma_offsets[ci]
                        cqq = chroma_qp(0 if pcm[addr] else qp_y[addr], co)
                        cqp = chroma_qp(0 if pcm[nb] else qp_y[nb], co)
                        if vertical:
                            _vertical(planes[1 + ci], 8 * mx + 4 * ce, 8 * my, 8, seg, 2, cqp, cqq, oa, ob, False)
                        else:
                            _horizontal(planes[1 + ci], 8 * mx, 8 * my + 4 * ce, 8, seg, 2, cqp, cqq, oa, ob, False)
