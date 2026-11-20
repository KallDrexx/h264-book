"""SPOILER - do not open until you have tried your own filter (Chapter 6.2).

Reference deblocking filter (8.7.2.2 - 8.7.2.4) for 8-bit 4:2:0 frames.  The same code ran in Chapter 6.1 as the proof of the bS
grid; here it is the truth of the harness.  It was checked against ffmpeg: on ffmpeg's UNFILTERED picture (-skip_loop_filter all)
it reproduces ffmpeg's filtered picture byte for byte (test_deblock.py --self-test does this on every test stream).

Planes are lists of rows (lists of ints) covering whole macroblocks.  filter_picture() works in place, macroblock by macroblock in
raster order; per macroblock the luma vertical edges left to right, the luma horizontal edges top to bottom, then Cb and Cr the
same way; every read sees the samples already filtered by earlier edges.

MUTANT switches break exactly one rule (harness self-test).  TRACE: filter_picture(trace=f) calls
f(plane, vertical, addr, edge, line, positions, info) for every sample line it looks at, `positions` = list of (x, y) of
p(taps-1) .. p0, q0 .. q(taps-1) and `info` = what the filter decided (see filter_line).
"""
MUTANT = None

# Table 8-16: alpha' and beta' by indexA / indexB
ALPHA = [0] * 16 + [4, 4, 5, 6, 7, 8, 9, 10, 12, 13, 15, 17, 20, 22, 25, 28, 32, 36, 40, 45, 50, 56, 63, 71, 80, 90, 101, 113,
                    127, 144, 162, 182, 203, 226, 255, 255]
BETA = [0] * 16 + [2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15, 16, 16,
                   17, 17, 18, 18]
# Table 8-17: tC0' for bS = 1, 2, 3 by indexA
TC0 = [(0, 0, 0)] * 17 + [(0, 0, 1)] * 4 + [(0, 1, 1)] * 2 + [(1, 1, 1)] * 4 + [(1, 1, 2)] * 4 + [(1, 2, 3)] * 2 + [
    (2, 2, 3), (2, 2, 4), (2, 3, 4), (2, 3, 4), (3, 3, 5), (3, 4, 6), (3, 4, 6), (4, 5, 7), (4, 5, 8), (4, 6, 9),
    (5, 7, 10), (6, 8, 11), (6, 8, 13), (7, 10, 14), (8, 11, 16), (9, 12, 18), (10, 13, 20), (11, 15, 23), (13, 17, 25)]
# Table 8-15 (qPI 30..51 -> QPC)
QPC = [29, 30, 31, 32, 32, 33, 34, 34, 35, 35, 36, 36, 37, 37, 37, 38, 38, 38, 39, 39, 39, 39]
assert len(ALPHA) == len(BETA) == len(TC0) == 52


def clip3(lo, hi, v):
    return lo if v < lo else hi if v > hi else v


def clip1(v):
    return v if MUTANT == "no_clip1" else clip3(0, 255, v)


def chroma_qp(qpy, offset):
    qpi = clip3(0, 51, qpy + offset)
    return qpi if qpi < 30 else QPC[qpi - 30]


def filter_line(p, q, bs, qp_p, qp_q, off_a, off_b, luma, info=None):
    """One line of samples across an edge.  p[0] = p0 (next to the edge), p[1] = p1 ..., same for q; p and q hold 4 samples for
    luma, 2 for chroma (3 or 4 are read by some mutants).  qp_p / qp_q = qPp / qPq (for chroma already the chroma QPs), off_a /
    off_b = FilterOffsetA / B.  -> (new p, new q).  `info` (dict) is filled with the decision."""
    qav = ((qp_p + qp_q) >> 1) if MUTANT == "qpav_floor" else (qp_p + qp_q + 1) >> 1
    ia = clip3(0, 51, qav + off_a)
    ib = clip3(0, 51, qav + off_b)
    alpha, beta = ALPHA[ia], BETA[ib]
    if MUTANT == "swap_alpha_beta_index":
        alpha, beta = ALPHA[ib], BETA[ia]
    if MUTANT == "alpha_from_indexB_only":
        alpha = ALPHA[ib]
    p, q = list(p), list(q)
    p0, p1, q0, q1 = p[0], p[1], q[0], q[1]
    cond = bool(bs) and abs(p0 - q0) < alpha and abs(p1 - p0) < beta and abs(q1 - q0) < beta
    if MUTANT == "no_beta_check":
        cond = bool(bs) and abs(p0 - q0) < alpha
    if info is not None:
        info.update(bs=bs, qav=qav, indexA=ia, indexB=ib, alpha=alpha, beta=beta, filtered=cond, branch="off (thresholds)",
                    p=list(p), q=list(q))
    if not cond:
        return p, q
    if bs < 4:
        tc0 = TC0[ia][bs - 1 - (1 if MUTANT == "tc0_bs_index" else 0)]
        ap = abs(p[2] - p0) if len(p) > 2 else 0
        aq = abs(q[2] - q0) if len(q) > 2 else 0
        if luma:
            tc = tc0 if MUTANT == "tc_no_ap_aq" else tc0 + (ap < beta) + (aq < beta)
            if MUTANT == "tc_always_plus2":
                tc = tc0 + 2
        else:
            tc = tc0 if MUTANT == "chroma_tc_no_plus1" else tc0 + 1
            if MUTANT == "chroma_tc_like_luma":
                tc = tc0 + (abs(p[2] - p0) < beta) + (abs(q[2] - q0) < beta)
        rnd = 3 if MUTANT == "delta_round_3" else 4
        delta = (((q0 - p0) << 2) + (p1 - q1) + rnd) >> 3
        if MUTANT != "no_clip_delta":
            delta = clip3(-tc, tc, delta)
        p[0], q[0] = clip1(p0 + delta), clip1(q0 - delta)
        if luma and MUTANT != "no_p1_q1":
            lim = tc if MUTANT == "p1_clip_tc" else tc0
            if ap < beta:
                p[1] = p1 + clip3(-lim, lim, (p[2] + ((p0 + q0 + 1) >> 1) - (p1 << 1)) >> 1)
            if aq < beta and MUTANT != "q1_missing":
                q[1] = q1 + clip3(-lim, lim, (q[2] + ((p0 + q0 + 1) >> 1) - (q1 << 1)) >> 1)
        if info is not None:
            info.update(branch=f"bS<4 weak: tC0={tc0}, tC={tc}, delta={delta}" + (f", ap={ap}, aq={aq}" if luma else ""))
        return p, q
    # bS == 4
    if luma or MUTANT == "chroma_strong_luma":
        p2, q2, p3, q3 = p[2], q[2], p[3], q[3]
        small = abs(p0 - q0) < ((alpha >> 2) + 2)
        if MUTANT == "strong_no_small_check":
            small = True
        thr = (alpha >> 2) + 2 if MUTANT != "strong_thr_alpha" else alpha
        if MUTANT == "strong_thr_alpha":
            small = abs(p0 - q0) < thr
        if small and abs(p2 - p0) < beta:
            p[0] = (p2 + 2 * p1 + 2 * p0 + 2 * q0 + q1 + 4) >> 3
            p[1] = (p2 + p1 + p0 + q0 + 2) >> 2
            p[2] = (2 * p3 + 3 * p2 + p1 + p0 + q0 + 4) >> 3
            if MUTANT == "strong_p2_missing":
                p[2] = p2
        else:
            p[0] = (2 * p1 + p0 + q1 + 2) >> 2
        if small and abs(q2 - q0) < beta:
            q[0] = (p1 + 2 * p0 + 2 * q0 + 2 * q1 + q2 + 4) >> 3
            q[1] = (p0 + q0 + q1 + q2 + 2) >> 2
            q[2] = (2 * q3 + 3 * q2 + q1 + q0 + p0 + 4) >> 3
        else:
            q[0] = (2 * q1 + q0 + p1 + 2) >> 2
        if info is not None:
            info.update(branch=f"bS=4 luma: strong p side {small and abs(p2 - p0) < beta}, strong q side "
                               f"{small and abs(q2 - q0) < beta} (|p0-q0|={abs(p0 - q0)} < {(alpha >> 2) + 2}: {small})")
    else:
        p[0] = (2 * p1 + p0 + q1 + 2) >> 2
        q[0] = (2 * q1 + q0 + p1 + 2) >> 2
        if info is not None:
            info.update(branch="bS=4 chroma: p0 and q0 only")
    return p, q


def _line(plane, src, positions, taps_p, taps_q, bs, qp_p, qp_q, off_a, off_b, luma, trace, tinfo):
    """positions = p0, p1, ..., then q0, q1, ... as (x, y).  Reads src, writes plane."""
    p = [src[y][x] for x, y in positions[:taps_p]]
    q = [src[y][x] for x, y in positions[taps_p:]]
    info = {} if trace else None
    np_, nq = filter_line(p, q, bs, qp_p, qp_q, off_a, off_b, luma, info)
    for (x, y), v in zip(positions, np_ + nq):
        plane[y][x] = v
    if trace:
        info["new_p"], info["new_q"] = np_, nq
        trace(*tinfo, [positions[taps_p - 1 - i] for i in range(taps_p)] + positions[taps_p:], info)


def _edge(plane, src, vertical, pos, start, n, bss, per, qp_p, qp_q, off_a, off_b, luma, trace, tinfo):
    taps = 4 if (luma or MUTANT in ("chroma_strong_luma", "chroma_tc_like_luma")) else 2
    for k in range(n):
        if vertical:
            x, y = pos, start + k
            positions = [(x - 1 - i, y) for i in range(taps)] + [(x + i, y) for i in range(taps)]
        else:
            x, y = start + k, pos
            positions = [(x, y - 1 - i) for i in range(taps)] + [(x, y + i) for i in range(taps)]
        _line(plane, src, positions, taps, taps, bss[k // per], qp_p, qp_q, off_a, off_b, luma, trace, tinfo + (k,))


def filter_picture(planes, w_mbs, bs_grid, qp_y, pcm, alpha_div2, beta_div2, chroma_offsets, trace=None, only=None):
    """planes = (Y, Cb, Cr).  bs_grid[addr] = dict(v=[16], h=[16]) (bsref; 0 = edge not filtered).  qp_y[addr] = QP_Y,
    pcm[addr] = I_PCM flag, alpha_div2[addr] / beta_div2[addr] = slice_alpha_c0_offset_div2 / slice_beta_offset_div2 of the slice
    containing macroblock addr, chroma_offsets = (chroma_qp_index_offset, second_chroma_qp_index_offset).
    only = optional set of plane indexes to filter (0 Y, 1 Cb, 2 Cr)."""
    n = len(bs_grid)
    srcs = [[list(r) for r in pl] for pl in planes] if MUTANT == "unfiltered_samples" else None
    for addr in range(n):
        mx, my = addr % w_mbs, addr // w_mbs
        g = bs_grid[addr]
        for plane_idx in range(3):
            if only is not None and plane_idx not in only:
                continue
            luma = plane_idx == 0
            pl = planes[plane_idx]
            src = srcs[plane_idx] if srcs else pl
            dirs = (True, False)
            if MUTANT == "hv_swapped":
                dirs = (False, True)
            for vertical in dirs:
                bs = g["v"] if vertical else g["h"]
                edges = range(4)
                if MUTANT == "edges_reversed":
                    edges = range(3, -1, -1)
                for e in edges:
                    seg = bs[4 * e:4 * e + 4]
                    if not any(seg):
                        continue
                    if not luma and e not in (0, 2):
                        continue
                    nb = addr - 1 if (e == 0 and vertical) else addr - w_mbs if (e == 0) else addr
                    # slice offsets: those of the slice containing q0 (the current macroblock), 8.7.2.2
                    src_mb = nb if MUTANT == "offsets_from_p" else addr
                    off_a, off_b = alpha_div2[src_mb], beta_div2[src_mb]
                    if MUTANT != "offset_not_doubled":
                        off_a, off_b = off_a << 1, off_b << 1
                    if MUTANT == "offset_a_only":
                        off_b = 0

                    def qp_of(a):
                        if pcm[a] and MUTANT != "pcm_qp_real":
                            return 0                    # I_PCM: qP = 0
                        return qp_y[a]
                    if luma:
                        qpp, qpq = qp_of(nb), qp_of(addr)
                        ce = None
                    else:
                        co = chroma_offsets[plane_idx - 1]
                        if MUTANT == "cr_uses_cb_offset":
                            co = chroma_offsets[0]
                        if MUTANT == "chroma_uses_luma_qp":
                            qpp, qpq = qp_of(nb), qp_of(addr)
                        elif MUTANT == "chroma_qp_of_average":
                            avg = (qp_of(nb) + qp_of(addr) + 1) >> 1
                            qpp = qpq = chroma_qp(avg, co)
                        else:
                            qpp, qpq = chroma_qp(qp_of(nb), co), chroma_qp(qp_of(addr), co)
                        ce = e // 2
                    tinfo = (("Y", "Cb", "Cr")[plane_idx], vertical, addr, e)
                    if luma:
                        _edge(pl, src, vertical, (16 * mx + 4 * e) if vertical else (16 * my + 4 * e),
                              16 * my if vertical else 16 * mx, 16, seg, 4, qpp, qpq, off_a, off_b, True, trace, tinfo)
                    else:
                        _edge(pl, src, vertical, (8 * mx + 4 * ce) if vertical else (8 * my + 4 * ce),
                              8 * my if vertical else 8 * mx, 8, seg, 2, qpp, qpq, off_a, off_b, False, trace, tinfo)
