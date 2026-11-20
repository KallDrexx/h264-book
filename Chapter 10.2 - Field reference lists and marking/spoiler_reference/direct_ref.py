"""SPOILER: reference for direct prediction (8.4.1.2) and implicit weights (8.4.3), frame pictures, Chapter 7.9.
Do not read before your own decoder passes.

Everything here is a pure function of (a) the slice context `SliceCtx` (the two reference lists as picture identities, POCs and
short/long-term flags, the current POC, direct_spatial_mv_pred_flag, direct_8x8_inference_flag) and (b) stored motion data.

Stored motion data of a picture = a `dmvpred_ref.PicState`: per 4x4 luma block (raster order of the whole picture) and list X the
refIdxLX, mvLX and the IDENTITY (pid) of the picture that refIdxLX named in the slice that coded the block; -1 / None for an unused
list.  A block with both lists unused is intra.

derive(...) returns for one 8x8 quarter of a macroblock four 4x4 entries (refIdxL0, mvL0, refIdxL1, mvL1); the caller stores them.

MUTANT switches ONE deliberately wrong rule on (MUTANTS); None = the correct rules.  The harness self-test shows that every one of
them changes the decoded pictures of at least one test stream.
"""
MUTANT = None
STRICT = False     # True (stream generator): refuse temporal direct when ffmpeg's one-table-per-picture limitation would matter
STATS = None       # a collections.Counter: coverage of the rules (harness self-test); only counted while MUTANT is None


def stat(key):
    if STATS is not None and MUTANT is None:
        STATS[key] += 1


MUTANTS = {
    # --- spatial direct (8.4.1.2.2)
    "d_minpos_min": "refIdxLX = Min(refIdxA, refIdxB, refIdxC) instead of MinPositive (an unavailable / intra neighbour's -1 wins)",
    "d_minpos_a_only": "refIdxLX is taken from neighbour A alone",
    "d_no_c_to_d": "neighbour C is never replaced by D when C is not available",
    "d_nb_per_part": "in B_Direct_8x8 the neighbours A, B, C are those of the 8x8 sub-macroblock instead of those of the whole macroblock",
    "d_zero_pred_l0_only": "both reference indices negative: only list 0 is used (refIdxL0 = 0, no list 1)",
    "d_col_mv_zero_only": "colZeroFlag needs mvCol == (0, 0) exactly, not both components in [-1, 1]",
    "d_col_mv_pm2": "colZeroFlag accepts mvCol components in [-2, 2]",
    "d_col_ref_any": "colZeroFlag does not look at refIdxCol (any reference index counts)",
    "d_col_lt_ignored": "colZeroFlag does not check that RefPicList1[0] is a short-term picture",
    "d_col_zero_any_ref": "a list's vector is zeroed by colZeroFlag whatever its refIdx (the rule says: only refIdx == 0)",
    "d_col_zero_never": "colZeroFlag is never set (vectors are always the median predictions)",
    "d_col_l0_only": "the co-located block is taken as intra when it does not use list 0 (L1-only blocks are ignored)",
    "d_col_l1_first": "the co-located block's list 1 data is used when it has any, list 0 only otherwise",
    "d_col_mv_ref_swapped": "colZeroFlag tests the co-located block's list-1 refIdx / mv although its list 0 is used (mixed data)",
    # --- direct_8x8_inference_flag
    "d_infer_ignored": "direct_8x8_inference_flag is ignored: every 4x4 block uses the co-located 4x4 block at its own position",
    "d_infer_topleft": "with direct_8x8_inference_flag the top-left 4x4 block of each co-located 8x8 is used instead of the corner block",
    "d_infer_forced": "the corner-block rule is used even when direct_8x8_inference_flag is 0",
    # --- temporal direct (8.4.1.2.3)
    "d_t_map_none": "refIdxL0 = refIdxCol (the co-located block's index is not mapped to the current list 0)",
    "d_t_map_last": "MapColToList0 takes the HIGHEST index that holds the picture instead of the lowest",
    "d_t_ref1_col": "refIdxL1 = refIdxCol (limited to the list size) instead of 0 (it is always 0)",
    "d_t_intra_ref": "an intra co-located block gives refIdxL0 = refIdxL1 = 0 with mvCol = (0, 0) but only list 0 is used",
    "d_t_dsf_round": "DistScaleFactor = (tb * tx) >> 6 (the + 32 is missing)",
    "d_t_tx_floor": "tx uses floor division (negative td rounds the wrong way)",
    "d_t_tb_clip_missing": "tb and td are not clipped to -128..127",
    "d_t_mv_noround": "mvL0 = (DistScaleFactor * mvCol) >> 8 (the + 128 is missing)",
    "d_t_mvl1_sign": "mvL1 = mvCol - mvL0 (the sign is wrong)",
    "d_t_lt_ignored": "a long-term RefPicList0[refIdxL0] is scaled like a short-term one",
    "d_t_pic0_is_col_ref": "pic0 for tb / td is RefPicList0[0] instead of RefPicList0[refIdxL0]",
    "d_t_dsf_clip_missing": "DistScaleFactor is not clipped to -1024..1023",
    # --- bookkeeping
    "d_direct_not_stored": "B_Skip / B_Direct_16x16 macroblocks leave no motion data in the picture store (neighbours and later co-located reads see intra)",
    "d_sub_direct_as_l0": "a B_Direct_8x8 sub-macroblock is predicted like B_L0_8x8 with refIdxL0 = 0 and mvd = (0, 0)",
    # --- implicit weights (8.4.3)
    "w_swapped": "w0 and w1 are swapped (w0 = DistScaleFactor >> 2)",
    "w_shift_3": "the weights use DistScaleFactor >> 3 instead of >> 2",
    "w_no_range_fallback": "no fallback to 32 / 32 when DistScaleFactor >> 2 is outside -64..128",
    "w_range_symmetric": "the fallback range is -64..64 (instead of -64..128)",
    "w_no_td0_fallback": "no fallback to 32 / 32 when DiffPicOrderCnt(pic1, pic0) == 0",
    "w_no_lt_fallback": "no fallback to 32 / 32 when a reference picture is long-term",
    "w_single_list": "implicit weights are also applied to partitions that use ONE list only (the prediction is weighted with w = 64 - s or s and rounded as if bi-predicted from the same samples)",
    "w_logwd_6": "logWD = 6 instead of 5",
    "w_round_missing": "the rounding term 2^logWD is missing",
    "w_poc_cur_ignored": "tb is DiffPicOrderCnt(pic1, pic0) (the current picture's POC is ignored)",
    "w_clip_tb_td_missing": "tb and td are not clipped to -128..127 in the weight derivation",
    "w_idc2_as_default": "weighted_bipred_idc 2 is treated as 0 (plain average)",
}


def clip3(lo, hi, v):
    return lo if v < lo else hi if v > hi else v


def tdiv(a, b):
    """C division: truncates toward zero."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class DirectInvalid(Exception):
    """The stream asks for something the standard does not allow (e.g. the co-located block's reference picture is not in
    RefPicList0): the stream is non-conforming or outside this chapter's scope."""


class SliceCtx:
    """Reference lists as seen by one slice.  pid0 / pid1: picture identity per index (None = 'no reference picture'); poc0 / poc1:
    PicOrderCnt; lt0 / lt1: True for long-term; mot1: stored motion data (PicState) of RefPicList1[0] (None when there is none)."""

    def __init__(self, cur_poc, pid0, poc0, lt0, pid1, poc1, lt1, mot1, spatial, inference, implicit=False):
        self.cur_poc = cur_poc
        self.pid0, self.poc0, self.lt0 = pid0, poc0, lt0
        self.pid1, self.poc1, self.lt1 = pid1, poc1, lt1
        self.mot1 = mot1
        self.spatial, self.inference, self.implicit = spatial, inference, implicit


# ---------------------------------------------------------------- DistScaleFactor and implicit weights (8-196..8-199, 8.4.3)
def dist_scale_factor(cur, p0, p1, clip_tbtd=True, mut=None):
    """DistScaleFactor from the POCs of the current picture, pic0 and pic1 (td must not be 0)."""
    mut = MUTANT if mut is None else mut
    if mut in ("d_t_tb_clip_missing", "w_clip_tb_td_missing"):
        clip_tbtd = False
    tb, td = cur - p0, p1 - p0
    if mut == "w_poc_cur_ignored":
        tb = p1 - p0
    if clip_tbtd:
        tb, td = clip3(-128, 127, tb), clip3(-128, 127, td)
    if td == 0:
        return 0
    if mut == "d_t_tx_floor":
        tx = (16384 + abs(td // 2)) // td
    else:
        tx = tdiv(16384 + abs(tdiv(td, 2)), td)
    v = (tb * tx + (0 if mut == "d_t_dsf_round" else 32)) >> 6
    if td < 0:
        stat("DistScaleFactor with td < 0")
    if v < -1024 or v > 1023:
        stat("DistScaleFactor clipped to -1024..1023")
    return v if mut == "d_t_dsf_clip_missing" else clip3(-1024, 1023, v)


def implicit_weights(cur, p0, p1, lt0, lt1):
    """(w0, w1) of a bi-predicted block with implicit weights (logWD = 5, offsets 0)."""
    m = MUTANT
    if m == "w_idc2_as_default":
        return 32, 32
    if p1 - p0 == 0 and m != "w_no_td0_fallback":
        stat("implicit weights: fallback, DiffPicOrderCnt(pic1, pic0) == 0")
        return 32, 32
    if (lt0 or lt1) and m != "w_no_lt_fallback":
        stat("implicit weights: fallback, long-term reference")
        return 32, 32
    dsf = dist_scale_factor(cur, p0, p1)
    s = dsf >> (3 if m == "w_shift_3" else 2)
    hi = 64 if m == "w_range_symmetric" else 128
    if (s < -64 or s > hi) and m != "w_no_range_fallback":
        stat("implicit weights: fallback, DistScaleFactor >> 2 outside -64..128")
        return 32, 32
    stat("implicit weights: computed, w1 < 0 or > 64 (extrapolation)" if (s < 0 or s > 64) else
         "implicit weights: computed, 0 <= w1 <= 64")
    if m == "w_swapped":
        return s, 64 - s
    return 64 - s, s


def weight_log_wd():
    return 6 if MUTANT == "w_logwd_6" else 5


def implicit_combine(v0, v1, w0, w1):
    """Sample of a bi-predicted block, implicit weights: Clip1(((v0 * w0 + v1 * w1 + 2^logWD) >> (logWD + 1)) + 0)."""
    lw = weight_log_wd()
    t = (v0 * w0 + v1 * w1 + (0 if MUTANT == "w_round_missing" else (1 << lw))) >> (lw + 1)
    return 0 if t < 0 else 255 if t > 255 else t


# ---------------------------------------------------------------- co-located block (8.4.1.2.1, frame pictures)
def corner_block(k):
    """(bx, by) of the 4x4 corner block of 8x8 quarter k: luma4x4BlkIdx 0, 5, 10, 15 -> the four corners of the macroblock."""
    return (k & 1) * 3, (k >> 1) * 3


def col_block(ctx, addr, wm, bx, by, k):
    """The co-located 4x4 block of block (bx, by) (0..3) of quarter k of macroblock addr:
    -> (refIdxCol, mvColX, mvColY, pid of the picture refIdxCol named, intra).  Uses the corner block when direct_8x8_inference_flag
    is 1 (luma4x4BlkIdx = 5 * mbPartIdx), the block itself otherwise."""
    m = MUTANT
    infer = ctx.inference
    if m == "d_infer_ignored":
        infer = False
    elif m == "d_infer_forced":
        infer = True
    if infer:
        bx, by = ((k & 1) * 2, (k >> 1) * 2) if m == "d_infer_topleft" else corner_block(k)
    col = ctx.mot1
    X0, Y0 = (addr % wm) * 4, (addr // wm) * 4
    i = (Y0 + by) * (wm * 4) + X0 + bx
    r0, r1 = col.ref[0][i], col.ref[1][i]
    use = None
    if m == "d_col_l1_first":
        use = 1 if r1 >= 0 else (0 if r0 >= 0 else None)
    elif m == "d_col_l0_only":
        use = 0 if r0 >= 0 else None
    else:
        use = 0 if r0 >= 0 else (1 if r1 >= 0 else None)
    if use is None:
        stat("co-located block is intra")
        return -1, 0, 0, None, True
    if r0 < 0:
        stat("co-located block predicted from list 1 only")
    if m == "d_col_mv_ref_swapped" and use == 0 and r1 >= 0:
        return col.ref[1][i], col.mvx[0][i], col.mvy[0][i], col.rpid[0][i], False
    return col.ref[use][i], col.mvx[use][i], col.mvy[use][i], col.rpid[use][i], False


# ---------------------------------------------------------------- spatial direct (8.4.1.2.2)
def min_positive(a, b):
    if MUTANT == "d_minpos_min":
        return min(a, b)
    return min(a, b) if a >= 0 and b >= 0 else max(a, b)


def spatial_mb(ctx, st, sl, addr, x0=0, y0=0, pw=16):
    """The macroblock-level part of spatial direct: refIdxL0, refIdxL1 and the median predictions mvpL0, mvpL1 (one set for the
    whole macroblock, from the neighbours A, B, C (D) of the 16x16 block).  -> (refs[2], mvps[2])."""
    refs, mvps = [-1, -1], [(0, 0), (0, 0)]
    for lst in (0, 1):
        A, B, C = st.neighbours(sl, lst, (addr % st.wm) * 16 + x0, (addr // st.wm) * 16 + y0, pw)
        if MUTANT == "d_no_c_to_d":
            C = st.nb(sl, lst, (addr % st.wm) * 16 + x0 + pw, (addr // st.wm) * 16 + y0 - 1)
        if MUTANT == "d_minpos_a_only":
            refs[lst] = A[1]
        else:
            refs[lst] = min_positive(A[1], min_positive(B[1], C[1]))
        mvps[lst] = (A, B, C)
    if refs[0] < 0 and refs[1] < 0:
        stat("spatial: both refIdx negative (directZeroPrediction)")
        if MUTANT == "d_zero_pred_l0_only":
            return [0, -1], [(0, 0), (0, 0)]
        return [0, 0], [(0, 0), (0, 0)]
    out = []
    for lst in (0, 1):
        out.append(st.median(*mvps[lst], refs[lst]) if refs[lst] >= 0 else (0, 0))
    stat("spatial: both lists used" if refs[0] >= 0 and refs[1] >= 0 else "spatial: one list only (the other refIdx < 0)")
    if refs[0] > 0 or refs[1] > 0:
        stat("spatial: a derived refIdx > 0")
    return refs, out


def spatial_quarter(ctx, st, sl, addr, k, mbinfo):
    """-> four (ref0, mv0, ref1, mv1) entries (raster order inside the quarter) for quarter k.  mbinfo = spatial_mb's result
    (computed once per macroblock)."""
    refs, mvps = mbinfo
    out = []
    for j in range(4):
        bx, by = (k & 1) * 2 + (j & 1), (k >> 1) * 2 + (j >> 1)
        rc, mx, my, _pid, intra = col_block(ctx, addr, st.wm, bx, by, k)
        short1 = not (ctx.lt1[0] if ctx.lt1 else False)
        if MUTANT == "d_col_lt_ignored":
            short1 = True
        if MUTANT == "d_col_mv_zero_only":
            rng = (0, 0)
        elif MUTANT == "d_col_mv_pm2":
            rng = (-2, 2)
        else:
            rng = (-1, 1)
        col_zero = (short1 and (rc == 0 or (MUTANT == "d_col_ref_any" and rc >= 0))
                    and rng[0] <= mx <= rng[1] and rng[0] <= my <= rng[1])
        if MUTANT == "d_col_zero_never":
            col_zero = False
        if STATS is not None and MUTANT is None:
            if intra:
                stat("spatial: colZeroFlag 0, co-located intra")
            elif col_zero:
                stat("spatial: colZeroFlag 1")
            elif not short1:
                stat("spatial: colZeroFlag 0 only because RefPicList1[0] is long-term"
                     if rc == 0 and -1 <= mx <= 1 and -1 <= my <= 1 else "spatial: colZeroFlag 0, long-term and other reasons")
            elif rc != 0:
                stat("spatial: colZeroFlag 0, refIdxCol != 0")
            else:
                stat("spatial: colZeroFlag 0, moving co-located block (mvCol outside -1..1)")
        ent = []
        for lst in (0, 1):
            r = refs[lst]
            if r < 0:
                ent += [-1, (0, 0)]
                continue
            mv = mvps[lst]
            if col_zero and (r == 0 or MUTANT == "d_col_zero_any_ref"):
                mv = (0, 0)
            ent += [r, mv]
        out.append(tuple(ent))
    return out


# ---------------------------------------------------------------- temporal direct (8.4.1.2.3)
def map_col_to_list0(ctx, pid):
    idxs = [i for i, p in enumerate(ctx.pid0) if p is not None and p == pid]
    if not idxs:
        raise DirectInvalid("temporal direct: the picture referred to by the co-located block is not in RefPicList0")
    if len(idxs) > 1:
        stat("temporal: the co-located reference is in RefPicList0 more than once (lowest index wins)")
    return idxs[-1] if MUTANT == "d_t_map_last" else idxs[0]


def temporal_quarter(ctx, st, sl, addr, k):
    out = []
    for j in range(4):
        bx, by = (k & 1) * 2 + (j & 1), (k >> 1) * 2 + (j >> 1)
        rc, mx, my, pid, intra = col_block(ctx, addr, st.wm, bx, by, k)
        if rc < 0:
            stat("temporal: intra co-located block (refIdxL0 = 0, mv 0)")
            if MUTANT == "d_t_intra_ref":
                out.append((0, (0, 0), -1, (0, 0)))
                continue
            r0, mvc = 0, (0, 0)
        else:
            r0 = rc if MUTANT == "d_t_map_none" else map_col_to_list0(ctx, pid)
            mvc = (mx, my)
        if r0 >= len(ctx.pid0) or ctx.pid0[r0] is None:
            raise DirectInvalid(f"temporal direct: refIdxL0 {r0} names 'no reference picture'")
        if ctx.pid1[0] is None:
            raise DirectInvalid("temporal direct: RefPicList1[0] is 'no reference picture'")
        r1 = 0
        if MUTANT == "d_t_ref1_col" and rc > 0:
            r1 = min(rc, len(ctx.pid1) - 1)
        i0 = 0 if MUTANT == "d_t_pic0_is_col_ref" else r0
        p0, p1 = ctx.poc0[i0], ctx.poc1[0]
        lt0 = ctx.lt0[r0] and MUTANT != "d_t_lt_ignored"
        if r0 > 0:
            stat("temporal: refIdxL0 > 0 after MapColToList0")
        if lt0 or p1 - p0 == 0:
            stat("temporal: long-term fallback mvL0 = mvCol" if lt0 else "temporal: DiffPicOrderCnt(pic1, pic0) == 0 fallback")
            mv0, mv1 = mvc, (0, 0)
        else:
            stat("temporal: scaled")
            dsf = dist_scale_factor(ctx.cur_poc, p0, p1)
            rnd = 0 if MUTANT == "d_t_mv_noround" else 128
            mv0 = ((dsf * mvc[0] + rnd) >> 8, (dsf * mvc[1] + rnd) >> 8)
            if MUTANT == "d_t_mvl1_sign":
                mv1 = (mvc[0] - mv0[0], mvc[1] - mv0[1])
            else:
                mv1 = (mv0[0] - mvc[0], mv0[1] - mvc[1])
        out.append((r0, mv0, r1, mv1))
    return out


def derive(ctx, st, sl, addr, k, mbinfo=None):
    """Direct motion of quarter k (0..3) of macroblock addr -> four entries (ref0, mv0, ref1, mv1).  Spatial mode needs `mbinfo`
    (spatial_mb's result for the macroblock)."""
    if ctx.mot1 is None or ctx.pid1[0] is None or (ctx.pid0 and ctx.pid0[0] is None):
        raise DirectInvalid("direct prediction needs RefPicList0[0] and RefPicList1[0]")
    if STRICT and not ctx.spatial and ctx.mot1.ambiguous():
        raise DirectInvalid("the co-located picture has slices with different reference lists (ffmpeg keeps one table per picture)")
    if not ctx.spatial and STATS is not None and ctx.mot1.ambiguous():
        stat("temporal: co-located picture whose slices used different reference lists")
    if ctx.spatial:
        if MUTANT == "d_nb_per_part":
            mbinfo = spatial_mb(ctx, st, sl, addr, (k & 1) * 8, (k >> 1) * 8, 8)
        return spatial_quarter(ctx, st, sl, addr, k, mbinfo)
    return temporal_quarter(ctx, st, sl, addr, k)
