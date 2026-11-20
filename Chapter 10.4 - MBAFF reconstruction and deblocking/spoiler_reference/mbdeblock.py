"""SPOILER: Chapter 10.4 reference deblocking of MBAFF frames (8.7 with the MBAFF rules), sample-line oriented.

Every line of samples across an edge is handled on its own: for the line we find the macroblock that holds p0 (the geometry of the
picture decides, not a table), the 4x4 blocks that hold p0 and q0, the boundary strength and the QPs; then the common filter
(deblock_ref.filter_line, unchanged from Chapter 6.2) is applied.  Field macroblocks of a frame are filtered in their own lines
(stride 2); a frame macroblock below a field pair is filtered in two passes (top field lines, bottom field lines).

Input per macroblock (class MI of mbdec.py): fld, intra, qpd (QP_Y, 0 for I_PCM), nz[16] (4x4 block has coefficients, raster),
ref[2][16], mv[2][16], rid[2][16] (identity of the reference picture: (frame id, parity 0 frame / 1 top / 2 bottom)), slice,
dbf = (disable_deblocking_filter_idc, FilterOffsetA, FilterOffsetB).
MUTANT switches ONE deliberately wrong rule on (harness self-test).
"""
import deblock_ref as DR

MUTANT = None
MUTANTS = {
    "db_left_mixed_same_mb": "left MB edge next to a pair of the other kind: p0 is taken from the left MB at the same position (no row geometry)",
    "db_left_mixed_bs_min": "left edge between frame and field pairs: bS = 0 instead of at least 1 for non-intra blocks without coefficients",
    "db_top_double_missing": "frame MB below a field pair: the top edge is filtered once against the bottom MB of the pair above",
    "db_top_double_order": "frame MB below a field pair: both passes use the top field MB of the pair above",
    "db_intra_top_bs4": "intra bS on the top MB edge of a field MB / next to a field pair is 4 (not 3)",
    "db_intra_left_bs3": "intra bS on the left MB edge of field MBs is 3 (not 4)",
    "db_mvy_limit_4": "vertical vector difference limit is 4 for field MBs too (2 is right)",
    "db_ref_by_frame": "reference pictures of field MBs are compared by frame only (parity ignored)",
    "db_mixed_bs2": "mixed frame / field edges without coefficients get bS 2 (instead of 1)",
    "db_chroma_left_luma_line": "chroma line c of a left MB edge takes the bS of luma line 2c (parity of the line ignored)",
    "db_field_stride": "field MBs are filtered with frame line stride (consecutive lines instead of every second line)",
    "db_bottom_field_top_edge": "top edge of a bottom field MB is filtered against the top field MB of its own pair",
    "db_qp_p_mb": "qPp of a left MB edge line taken from the top left MB for all 16 lines",
    "db_idc2_pair": "disable_deblocking_filter_idc 2: edges to the pair above are filtered when the pair is in another slice",
    "db_bs_internal_intra4": "internal edges of intra MBs get bS 4",
}


def mut(n):
    return MUTANT == n


def motion_bs(pm, pc, qm, qc, lim):
    """bS 1 or 0 from references and vectors (no coefficients, not mixed)."""
    pe = [(pm.rid[l][pc], pm.mv[l][pc]) for l in (0, 1) if pm.ref[l][pc] >= 0]
    qe = [(qm.rid[l][qc], qm.mv[l][qc]) for l in (0, 1) if qm.ref[l][qc] >= 0]
    if mut("db_ref_by_frame"):
        pe = [((r[0], 0), m) for r, m in pe]
        qe = [((r[0], 0), m) for r, m in qe]
    if len(pe) != len(qe):
        return 1

    def far(a, b):
        return abs(a[0] - b[0]) >= 4 or abs(a[1] - b[1]) >= lim
    if len(pe) == 1:
        return 1 if pe[0][0] != qe[0][0] or far(pe[0][1], qe[0][1]) else 0
    if len(pe) == 0:
        return 0
    if sorted(r for r, _ in pe) != sorted(r for r, _ in qe):
        return 1
    if pe[0][0] != pe[1][0]:
        for r, m in pe:
            mq = qe[0][1] if qe[0][0] == r else qe[1][1]
            if far(m, mq):
                return 1
        return 0
    straight = far(pe[0][1], qe[0][1]) or far(pe[1][1], qe[1][1])
    crossed = far(pe[0][1], qe[1][1]) or far(pe[1][1], qe[0][1])
    return 1 if straight and crossed else 0


def bs_calc(pm, pc, qm, qc, vertical, mb_edge, mixed):
    if pm.intra or qm.intra:
        if vertical:
            if mb_edge:
                return 3 if (mut("db_intra_left_bs3") and (pm.fld or qm.fld)) else 4
            return 4 if mut("db_bs_internal_intra4") else 3
        if mb_edge and not pm.fld and not qm.fld:
            return 4
        if mb_edge and mut("db_intra_top_bs4"):
            return 4
        return 3
    if pm.nz[pc] or qm.nz[qc]:
        return 2
    if mixed:
        return 2 if mut("db_mixed_bs2") else 0 if mut("db_left_mixed_bs_min") and vertical else 1
    return motion_bs(pm, pc, qm, qc, 4 if (not qm.fld or mut("db_mvy_limit_4")) else 2)


def filter_picture(planes, wm, mbs, offs, trace=None):
    """planes (Y, Cb, Cr); mbs: addr -> MI for every macroblock (2 * wm * pair rows); offs = (cb offset, cr offset).
    Filters in place, macroblock by macroblock in address order."""
    n = len(mbs)
    for addr in range(n):
        filter_mb(planes, wm, mbs, addr, offs)


def chroma_qp(q, off):
    return DR.chroma_qp(q, off)


def filter_mb(planes, wm, mbs, addr, offs):
    mi = mbs[addr]
    idc, oa, ob = mi.dbf
    if idc == 1:
        return
    pair, bot = addr >> 1, addr & 1
    mbx, pr = pair % wm, pair // wm
    fld = bool(mi.fld)
    ypair, x0 = 32 * pr, 16 * mbx
    dy = 2 if fld and not mut("db_field_stride") else 1
    yrow0 = ypair + (bot if fld else 16 * bot)
    if fld and mut("db_field_stride"):
        yrow0 = ypair + 16 * bot
    cy0 = 16 * pr + (bot if fld else 8 * bot)
    if fld and mut("db_field_stride"):
        cy0 = 16 * pr + 8 * bot
    cx0 = 8 * mbx
    Y, CB, CR = planes
    t8 = getattr(mi, "t8", 0)
    left_pair = pair - 1
    left_ok = mbx > 0 and not (idc == 2 and mbs[2 * left_pair].slice != mi.slice)
    above_pair = pair - wm
    # ---------------------------------------------------------------- luma bS of vertical edges 0, 8 (chroma needs them) and 4, 12
    lbs, lpm = {}, {}
    luma_edges = []
    for e in (0, 4, 8, 12):
        if e == 0 and not left_ok:
            continue
        bsl, pml = [], []
        for r in range(16):
            qc = (r >> 2) * 4 + (e >> 2)
            if e == 0:
                lf = bool(mbs[2 * left_pair].fld)
                rr = (yrow0 + dy * r) - ypair
                if mut("db_left_mixed_same_mb") and lf != fld:
                    pm = mbs[2 * left_pair + bot]
                    prow = r
                elif lf:
                    pm, prow = mbs[2 * left_pair + (rr & 1)], rr >> 1
                else:
                    pm, prow = mbs[2 * left_pair + (rr >> 4)], rr & 15
                if mut("db_qp_p_mb"):
                    pm_q = mbs[2 * left_pair]
                else:
                    pm_q = pm
                pc = (prow >> 2) * 4 + 3
                b = bs_calc(pm, pc, mi, qc, True, True, lf != fld)
            else:
                pm, pm_q, pc = mi, mi, qc - 1
                b = bs_calc(mi, pc, mi, qc, True, False, False)
            bsl.append(b)
            pml.append((pm, pm_q))
        lbs[e], lpm[e] = bsl, pml
    # ---------------------------------------------------------------- luma vertical edges
    for e in (0, 4, 8, 12):
        if e not in lbs or (t8 and e in (4, 12)):
            continue
        for r in range(16):
            b = lbs[e][r]
            if not b:
                continue
            row = Y[yrow0 + dy * r]
            x = x0 + e
            p = [row[x - 1 - i] for i in range(4)]
            q = [row[x + i] for i in range(4)]
            pm, pm_q = lpm[e][r]
            np_, nq = DR.filter_line(p, q, b, pm_q.qpd, mi.qpd, oa, ob, True)
            for i in range(4):
                row[x - 1 - i], row[x + i] = np_[i], nq[i]
    # ---------------------------------------------------------------- chroma vertical edges
    left_field = bool(mbs[2 * left_pair].fld) if mbx > 0 else False
    for ce, e in ((0, 0), (4, 8)):
        if e not in lbs:
            continue
        for c in range(8):
            yl = 2 * c
            if ce == 0 and not fld and left_field and not mut("db_chroma_left_luma_line"):
                yl = 2 * c - (c & 1)
            b = lbs[e][yl]
            if not b:
                continue
            pm, pm_q = lpm[e][yl]
            for pi, plane in ((0, CB), (1, CR)):
                row = plane[cy0 + dy * c]
                x = cx0 + ce
                p = [row[x - 1 - i] for i in range(2)]
                q = [row[x + i] for i in range(2)]
                np_, nq = DR.filter_line(p, q, b, chroma_qp(pm_q.qpd, offs[pi]), chroma_qp(mi.qpd, offs[pi]), oa, ob, False)
                for i in range(2):
                    row[x - 1 - i], row[x + i] = np_[i], nq[i]
    # ---------------------------------------------------------------- horizontal edges
    # a "pass" = (pm, p luma rows(i), q luma rows(i), p chroma rows(i), q chroma rows(i), mixed)
    top_passes = []
    pa_ok = pr > 0 and not (idc == 2 and mbs[2 * above_pair].slice != mi.slice) or (pr > 0 and mut("db_idc2_pair"))
    if not fld and bot:
        pm = mbs[addr - 1]
        top_passes.append((pm, lambda i: yrow0 - 1 - i, lambda i: yrow0 + i, lambda i: cy0 - 1 - i, lambda i: cy0 + i, False))
    elif not fld:
        if pa_ok:
            af = bool(mbs[2 * above_pair].fld)
            if not af:
                pm = mbs[2 * above_pair + 1]
                top_passes.append((pm, lambda i: yrow0 - 1 - i, lambda i: yrow0 + i, lambda i: cy0 - 1 - i, lambda i: cy0 + i, False))
            elif mut("db_top_double_missing"):
                pm = mbs[2 * above_pair + 1]
                top_passes.append((pm, lambda i: yrow0 - 1 - i, lambda i: yrow0 + i, lambda i: cy0 - 1 - i, lambda i: cy0 + i, False))
            else:
                for j in (0, 1):
                    pm = mbs[2 * above_pair + (0 if mut("db_top_double_order") else j)]
                    top_passes.append((pm, (lambda j: lambda i: yrow0 - 2 * (i + 1) + j)(j), (lambda j: lambda i: yrow0 + j + 2 * i)(j),
                                       (lambda j: lambda i: cy0 - 2 * (i + 1) + j)(j), (lambda j: lambda i: cy0 + j + 2 * i)(j), True))
    else:
        if pa_ok:
            af = bool(mbs[2 * above_pair].fld)
            if af:
                pm = mbs[2 * above_pair + bot]
            else:
                pm = mbs[2 * above_pair + 1]
            if mut("db_bottom_field_top_edge") and bot:
                pm = mbs[addr - 1]
            top_passes.append((pm, lambda i: yrow0 - 2 * (i + 1), lambda i: yrow0 + 2 * i, lambda i: cy0 - 2 * (i + 1),
                               lambda i: cy0 + 2 * i, not af))
    # luma bS for the groups of columns: edge 0 per pass, internal edges 4, 8, 12
    hbs_int = {}
    for e in (4, 8, 12):
        hbs_int[e] = [bs_calc(mi, (e // 4 - 1) * 4 + g, mi, (e // 4) * 4 + g, False, False, False) for g in range(4)]
    hbs_top = []
    for (pm, pl, ql, pcl, qcl, mixed) in top_passes:
        hbs_top.append([bs_calc(pm, 12 + g, mi, g, False, True, mixed) for g in range(4)])

    def luma_h(bsg, prow, qrow, qpm_p):
        for c in range(16):
            b = bsg[c >> 2]
            if not b:
                continue
            x = x0 + c
            p = [Y[prow(i)][x] for i in range(4)]
            q = [Y[qrow(i)][x] for i in range(4)]
            np_, nq = DR.filter_line(p, q, b, qpm_p.qpd, mi.qpd, oa, ob, True)
            for i in range(4):
                Y[prow(i)][x], Y[qrow(i)][x] = np_[i], nq[i]

    def chroma_h(bsg, prow, qrow, qpm_p):
        for cc in range(8):
            b = bsg[cc >> 1]
            if not b:
                continue
            for pi, plane in ((0, CB), (1, CR)):
                x = cx0 + cc
                p = [plane[prow(i)][x] for i in range(2)]
                q = [plane[qrow(i)][x] for i in range(2)]
                np_, nq = DR.filter_line(p, q, b, chroma_qp(qpm_p.qpd, offs[pi]), chroma_qp(mi.qpd, offs[pi]), oa, ob, False)
                for i in range(2):
                    plane[prow(i)][x], plane[qrow(i)][x] = np_[i], nq[i]
    # order: luma top edge, luma internal edges, then chroma (planes are independent)
    for (pm, pl, ql, pcl, qcl, mixed), bsg in zip(top_passes, hbs_top):
        luma_h(bsg, pl, ql, pm)
    for e in (4, 8, 12):
        if t8 and e in (4, 12):
            continue
        luma_h(hbs_int[e], (lambda e: lambda i: yrow0 + dy * (e - 1 - i))(e), (lambda e: lambda i: yrow0 + dy * (e + i))(e), mi)
    for (pm, pl, ql, pcl, qcl, mixed), bsg in zip(top_passes, hbs_top):
        chroma_h(bsg, pcl, qcl, pm)
    chroma_h(hbs_int[8], lambda i: cy0 + dy * (3 - i), lambda i: cy0 + dy * (4 + i), mi)
