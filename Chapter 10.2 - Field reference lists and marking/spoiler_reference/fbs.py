"""SPOILER - do not open until your own boundary-strength code works (Chapters 7.5 and 7.8).

Reference for the edge flags and the boundary strength bS of P and B pictures (8.7.2.1), frame macroblocks, CAVLC (4x4 transform only).
Chapter 7.8 = Chapter 7.5's module with the motion rule extended to blocks with TWO motion vectors (bi-prediction).

Input: a `Pic` (w, h in macroblocks; .slices[slice id] = SliceFacts(idc, slice_type); .mbs[addr] = Mb) where an Mb carries
  .slice, .intra (intra macroblock or I_PCM), .pcm, .skip, .nz[16] (raster order of the 4x4 luma blocks: the block has at least one
  non-zero transform coefficient), .grid[16] (raster order: for each 4x4 block a tuple of one or two (reference picture identity, mvx, mvy) entries in quarter
  samples, list 0 entry first; None for intra),
  .refidx[16] (per block a tuple of the ref_idx values of the entries; used only by a mutants), .cbp_luma, .chroma_cbp.
Output per macroblock: dict(fv=[4], fh=[4], v=[16], h=[16]) exactly like Chapter 6.1 (edge e of V is at x = 4e, segment s = rows
4s..4s+3; H: edge at y = 4e, segment s = columns 4s..4s+3).

MUTANT switches on ONE deliberately wrong rule (harness self-test); None = the correct rules.
"""
MUTANT = None
FIELD = False        # set by the decoder: the picture is a FIELD picture (Chapter 10.2): bS 3 on intra horizontal MB edges, vertical vector limit 2
MUTANTS = {
    "field_bs4_horizontal": "field pictures: intra horizontal macroblock edges get bS 4 (frame rule)",
    "field_mv_limit_4": "field pictures: the vertical vector difference limit is 4 quarter samples (frame rule) instead of 2",
    "field_mv_limit_1": "field pictures: the vertical vector difference limit is 1 instead of 2",
    "field_ref_by_frame": "field pictures: reference FIELDS of one frame count as the same reference picture (parity ignored)",
    "b_bs_pair_by_list": "two-vector blocks: list 0 is always compared with list 0 and list 1 with list 1 (no pairing by reference picture)",
    "b_bs_same_ref_straight_only": "both vectors point to the SAME picture: only the L0-L0 / L1-L1 pairing is tried",
    "b_bs_same_ref_either": "both vectors point to the SAME picture: bS 1 if EITHER pairing fails (instead of both)",
    "b_bs_count_ignored": "blocks with one and with two vectors are compared on their first vector only",
    "b_bs_ref_set_ignored": "bi-predicted blocks: the reference pictures are not compared, only the vectors (by list)",
    "mv_thr_3": "motion threshold 3 instead of 4 quarter samples",
    "mv_thr_5": "motion threshold 5 (a difference of exactly 4 does not count)",
    "mv_y_ignored": "only the horizontal vector component is compared",
    "mv_x_ignored": "only the vertical vector component is compared",
    "mv_sum": "|dx| + |dy| >= 4 instead of either component >= 4",
    "ref_ignored": "the reference pictures are not compared",
    "ref_idx_compare": "ref_idx values are compared instead of the pictures they select",
    "coef_p_only": "bS 2 looks at the coefficients of the p side only",
    "coef_q_only": "bS 2 looks at the coefficients of the q side only",
    "coef_cbp_bit": "bS 2 uses the coded_block_pattern bit of the 8x8 quadrant instead of the 4x4 block's coefficients",
    "skip_mv_zero": "a skipped macroblock is taken to have vector (0, 0) for bS",
    "skip_is_coded": "a skipped macroblock is taken to have coefficients (bS 2)",
    "bs_swap_1_2": "coefficients give bS 1, motion gives bS 2",
    "intra_q_only": "only the q macroblock is checked for being intra",
    "intra_mbedge_3": "intra macroblock edges get bS 3 (no bS 4) in P pictures",
    "intra_internal_4": "internal edges of intra macroblocks get bS 4",
    "internal_motion_ignored": "internal edges look at coefficients only, never at motion",
    "internal_zero": "internal edges are never filtered in inter macroblocks (only intra ones)",
    "pcm_as_inter": "I_PCM macroblocks are treated like inter macroblocks with all coefficients non-zero",
    "idc2_crosses": "disable_deblocking_filter_idc 2 still filters across slice boundaries",
    "p_edge_filtered": "the left / top picture boundary is filtered",
    "mb_edge_no_motion": "macroblock edges look at coefficients only, never at motion",
}


class SliceFacts:
    def __init__(self, idc, slice_type=0):
        self.idc, self.slice_type = idc, slice_type


class Mb:
    def __init__(self, slice_id, intra=False, pcm=False, skip=False):
        self.slice, self.intra, self.pcm, self.skip = slice_id, intra, pcm, skip
        self.nz, self.grid, self.refidx = [False] * 16, None, None
        self.cbp_luma = self.chroma_cbp = 0


class Pic:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.slices, self.mbs = {}, [None] * (w * h)


def mb_flags(pic, addr):
    """-> (fv, fh): the four vertical and four horizontal edge flags of macroblock addr (filterEdgeFlag, 8.7)."""
    mb = pic.mbs[addr]
    idc = pic.slices[mb.slice].idc
    mx, my = addr % pic.w, addr // pic.w
    internal = idc != 1
    left = internal and (mx > 0 or MUTANT == "p_edge_filtered")
    top = internal and (my > 0 or MUTANT == "p_edge_filtered")
    if idc == 2 and MUTANT != "idc2_crosses":
        if left and mx > 0 and pic.mbs[addr - 1].slice != mb.slice:
            left = False
        if top and my > 0 and pic.mbs[addr - pic.w].slice != mb.slice:
            top = False
    return [int(left), int(internal), int(internal), int(internal)], [int(top), int(internal), int(internal), int(internal)]


def _far(a, b, thr):
    dx, dy = abs(a[1] - b[1]), abs(a[2] - b[2])
    if FIELD and MUTANT != "mv_y_ignored":
        ylim = {"field_mv_limit_4": 4, "field_mv_limit_1": 1}.get(MUTANT, 2)
        if MUTANT == "mv_x_ignored":
            dx = 0
        return dx >= thr or dy >= ylim
    if MUTANT == "mv_y_ignored":
        dy = 0
    if MUTANT == "mv_x_ignored":
        dx = 0
    if MUTANT == "mv_sum":
        return dx + dy >= thr
    return dx >= thr or dy >= thr


def motion_bs(P, pi, Q, qi, internal):
    """bS 1 or 0 from the motion of the two 4x4 blocks (both inter, no coefficients).  8.7.2.1: bS = 1 if the blocks use different
    reference PICTURES or a different NUMBER of vectors (the comparison is of pictures, whatever list / ref_idx names them), or
    -- one vector each -- the vectors differ by >= 4 quarter samples in a component, or -- two vectors to two different pictures --
    the vectors that refer to the same picture differ, or -- two vectors to the same picture -- both pairings (L0-L0 with L1-L1,
    and L0-L1 with L1-L0) have a difference."""
    if internal and MUTANT == "internal_motion_ignored":
        return 0
    mp, mq = (P.grid[pi] if P.grid else ()), (Q.grid[qi] if Q.grid else ())
    mp, mq = list(mp), list(mq)
    if FIELD and MUTANT == "field_ref_by_frame":
        mp = [(e[0][0] if isinstance(e[0], tuple) else e[0],) + tuple(e[1:]) for e in mp]
        mq = [(e[0][0] if isinstance(e[0], tuple) else e[0],) + tuple(e[1:]) for e in mq]
    if MUTANT == "skip_mv_zero":
        if P.skip:
            mp = [(e[0], 0, 0) for e in mp]
        if Q.skip:
            mq = [(e[0], 0, 0) for e in mq]
    if MUTANT == "ref_idx_compare":
        rp, rq = (P.refidx[pi] if P.refidx else ()), (Q.refidx[qi] if Q.refidx else ())
        mp = [(rp[k],) + tuple(e[1:]) for k, e in enumerate(mp)]
        mq = [(rq[k],) + tuple(e[1:]) for k, e in enumerate(mq)]
    thr = {"mv_thr_3": 3, "mv_thr_5": 5}.get(MUTANT, 4)
    if MUTANT == "b_bs_count_ignored":
        mp, mq = mp[:1], mq[:1]
    if len(mp) != len(mq):
        return 1
    if len(mp) == 2 and MUTANT in ("b_bs_pair_by_list", "b_bs_ref_set_ignored"):
        if MUTANT == "b_bs_pair_by_list" and sorted(e[0] for e in mp) != sorted(e[0] for e in mq):
            return 1
        return int(_far(mp[0], mq[0], thr) or _far(mp[1], mq[1], thr))
    if MUTANT != "ref_ignored" and sorted(map(str, (e[0] for e in mp))) != sorted(map(str, (e[0] for e in mq))):
        return 1
    if len(mp) == 1:
        return int(_far(mp[0], mq[0], thr))
    (pa, pb), (qa, qb) = mp, mq
    if str(pa[0]) != str(pb[0]):
        if str(pa[0]) == str(qa[0]):
            return int(_far(pa, qa, thr) or _far(pb, qb, thr))
        return int(_far(pa, qb, thr) or _far(pb, qa, thr))
    straight = _far(pa, qa, thr) or _far(pb, qb, thr)
    if MUTANT == "b_bs_same_ref_straight_only":
        return int(straight)
    cross = _far(pa, qb, thr) or _far(pb, qa, thr)
    if MUTANT == "b_bs_same_ref_either":
        return int(straight or cross)
    return int(straight and cross)


def seg_bs(P, pi, Q, qi, mb_edge, vertical=True):
    """bS of one 4-sample segment between 4x4 block pi of macroblock P (the p side) and block qi of Q (the q side)."""
    pintra = P.intra and not (MUTANT == "pcm_as_inter" and P.pcm)
    qintra = Q.intra and not (MUTANT == "pcm_as_inter" and Q.pcm)
    if MUTANT == "intra_q_only":
        pintra = False
    if pintra or qintra:
        if MUTANT == "intra_mbedge_3":
            return 3
        if MUTANT == "intra_internal_4":
            return 4
        if mb_edge and FIELD and not vertical and MUTANT != "field_bs4_horizontal":
            return 3
        return 4 if mb_edge else 3
    if not mb_edge and MUTANT == "internal_zero":
        return 0

    def coded(M, i):
        if MUTANT == "coef_cbp_bit":
            by, bx = i >> 2, i & 3
            return bool((M.cbp_luma >> (2 * (by >> 1) + (bx >> 1))) & 1)
        if MUTANT == "skip_is_coded" and M.skip:
            return True
        return M.nz[i] or (MUTANT == "pcm_as_inter" and M.pcm)
    if MUTANT == "coef_p_only":
        c = coded(P, pi)
    elif MUTANT == "coef_q_only":
        c = coded(Q, qi)
    else:
        c = coded(P, pi) or coded(Q, qi)
    if c:
        return 1 if MUTANT == "bs_swap_1_2" else 2
    if mb_edge and MUTANT == "mb_edge_no_motion":
        return 0
    m = motion_bs(P, pi, Q, qi, not mb_edge)
    return 2 if (m and MUTANT == "bs_swap_1_2") else m


def mb_bs(pic, addr, w=None):
    fv, fh = mb_flags(pic, addr)
    Q = pic.mbs[addr]
    v, h = [], []
    for vertical in (True, False):
        out = v if vertical else h
        flags = fv if vertical else fh
        for e in range(4):
            for s in range(4):
                if not flags[e]:
                    out.append(0)
                    continue
                qi = (s * 4 + e) if vertical else (e * 4 + s)
                if e == 0:
                    pa = addr - 1 if vertical else addr - pic.w
                    pi = (s * 4 + 3) if vertical else (12 + s)
                    if pa < 0 or (vertical and addr % pic.w == 0):      # only reachable with a mutant: no macroblock there
                        out.append(4)
                        continue
                else:
                    pa = addr
                    pi = (s * 4 + e - 1) if vertical else ((e - 1) * 4 + s)
                out.append(seg_bs(pic.mbs[pa], pi, Q, qi, e == 0, vertical))
    return dict(fv=fv, fh=fh, v=v, h=h)


def picture_bs(pic):
    return [mb_bs(pic, a) for a in range(pic.w * pic.h)]


def chroma_own_grid(pic, grid, w):
    """WRONG on purpose (mutant chroma_bs_own): bS for the chroma edges derived from the macroblocks' chroma coefficients."""
    out = []
    for a, g in enumerate(grid):
        Q = pic.mbs[a]
        v, h = list(g["v"]), list(g["h"])
        for vertical, arr in ((True, v), (False, h)):
            for e in range(4):
                for s in range(4):
                    if arr[e * 4 + s] == 0 and (g["fv"] if vertical else g["fh"])[e]:
                        pa = a if e else (a - 1 if vertical else a - w)
                        if Q.chroma_cbp or pic.mbs[pa].chroma_cbp:
                            arr[e * 4 + s] = 2
        out.append(dict(g, v=v, h=h))
    return out
