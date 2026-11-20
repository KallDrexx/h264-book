"""SPOILER - do not open until you have tried to write your own edge selection and boundary strength.

Reference for Chapter 6.1: which edges of a macroblock are filtered (filterLeftMbEdgeFlag, filterTopMbEdgeFlag,
filterInternalEdgesFlag, the 8x8-transform rule) and their bS values (8.7.2.1), for INTRA pictures of frame macroblocks.

Input: a `Pic` (see test_bs.py): .w, .h in macroblocks, .mbs[addr] with .slice (slice index), .t8x8, .pcm and .slices[i] with
.idc.  Output: per macroblock dict(fv=[4 flags], fh=[4 flags], v=[16 bS], h=[16 bS]); edge e of vertical edges is the one at
x = 4e, segment s covers rows 4s..4s+3 of the macroblock (horizontal: edge at y = 4e, segment s covers columns 4s..4s+3).

MUTANT switches deliberately break one rule (used by the harness self-test and by the validation-by-consequence experiment).
"""
MUTANT = None


def mb_flags(pic, addr):
    """-> (fv, fh): the four vertical and four horizontal edge flags of macroblock addr (1 = filter this edge)."""
    mb = pic.mbs[addr]
    idc = pic.slices[mb.slice].idc
    if MUTANT == "idc2_as_0" and idc == 2:
        idc = 0
    if MUTANT == "idc2_as_1" and idc == 2:
        idc = 1
    if MUTANT == "idc0_as_2" and idc == 0:
        idc = 2
    if MUTANT == "idc1_ignored" and idc == 1:
        idc = 0
    mx, my = addr % pic.w, addr // pic.w
    internal = idc != 1
    left = internal and mx > 0
    top = internal and my > 0
    if idc == 2:
        # "do not filter across slice boundaries": the neighbour must be in the same slice (6.4.x availability)
        if left and pic.mbs[addr - 1].slice != mb.slice:
            left = False
        if top and pic.mbs[addr - pic.w].slice != mb.slice:
            top = False
    if MUTANT == "idc2_top_crosses" and idc == 2:
        top = internal and my > 0
    if MUTANT == "idc2_left_crosses" and idc == 2:
        left = internal and mx > 0
    if MUTANT == "picture_edge_filtered" and idc != 1:
        left, top = True, True
    if MUTANT == "idc1_still_mb_edges" and pic.slices[mb.slice].idc == 1:
        left, top = mx > 0, my > 0
    fv = [int(left), int(internal), int(internal), int(internal)]
    fh = [int(top), int(internal), int(internal), int(internal)]
    if mb.t8x8 and MUTANT != "t8x8_ignored":
        fv[1] = fv[3] = fh[1] = fh[3] = 0
    if MUTANT == "t8x8_skips_edge2" and mb.t8x8:
        fv[2] = fh[2] = 0
    if MUTANT == "internal_skipped" :
        fv[1:] = [0, 0, 0]
        fh[1:] = [0, 0, 0]
    return fv, fh


def edge_bs(is_mb_edge, vertical):
    """bS of a filtered edge between intra macroblocks / inside an intra macroblock, frame macroblocks (8.7.2.1)."""
    if MUTANT == "mbedge_3":
        return 3
    if MUTANT == "internal_4":
        return 4
    if MUTANT == "horizontal_mbedge_3" and not vertical:
        return 3
    if MUTANT == "vertical_mbedge_3" and vertical:
        return 3
    if is_mb_edge:
        return 4
    return 3


def mb_bs(pic, addr):
    fv, fh = mb_flags(pic, addr)
    v, h = [], []
    for e in range(4):
        for _ in range(4):
            v.append(edge_bs(e == 0, True) if fv[e] else 0)
    for e in range(4):
        for _ in range(4):
            h.append(edge_bs(e == 0, False) if fh[e] else 0)
    if MUTANT == "pcm_internal_0" and pic.mbs[addr].pcm:
        v[4:] = [0] * 12
        h[4:] = [0] * 12
        fv[1:] = [0, 0, 0]
        fh[1:] = [0, 0, 0]
    if MUTANT == "pcm_edges13_0" and pic.mbs[addr].pcm:
        for e in (1, 3):
            fv[e] = fh[e] = 0
            v[4 * e:4 * e + 4] = [0] * 4
            h[4 * e:4 * e + 4] = [0] * 4
    return dict(fv=fv, fh=fh, v=v, h=h)


def picture_bs(pic):
    return [mb_bs(pic, a) for a in range(pic.w * pic.h)]
