"""Reference Intra 4x4 prediction (spec 8.3.1.1 mode prediction, 8.3.1.2 the nine modes) and I_NxN reconstruction.

SPOILER: this is the solution to Chapter 5.4.  Write your own from the Readme first; read this only to compare after
you have tried.

Planes are lists of rows (plane[y][x]) of ints.  Notation as in the Readme: p[x, -1] is above, p[-1, y] left,
p[-1, -1] the corner; x = 4..7 above are the "up-right" samples.
"""
import transref
from transref import clip1

# Deliberate mistakes for the harness self-test (test_intra4.py --self-test): the harness must reject each one.
MUTANT = None

BLK_XY = transref.BLK_XY                     # luma4x4BlkIdx -> (bx, by) in units of 4 samples
BLK_IDX = {xy: i for i, xy in enumerate(BLK_XY)}

# Up-right availability of a block INSIDE its macroblock (by > 0), by luma4x4BlkIdx: the block up and to the right must
# already be decoded, and it must be inside this macroblock.  Blocks of the top row (0, 1, 4, 5) look at the macroblock
# above (B) or, for block 5, above-right (C).
UP_RIGHT_INSIDE = {2: True, 3: False, 6: True, 7: False, 8: True, 9: True, 10: True, 11: False, 12: True, 13: False,
                   14: True, 15: False}

MODE_NAMES = ["vertical", "horizontal", "DC", "diagonal down-left", "diagonal down-right", "vertical-right",
              "horizontal-down", "vertical-left", "horizontal-up"]
# neighbours each mode needs (a = left column, b = above row, d = corner)
NEEDS = {0: "b", 1: "a", 2: "", 3: "b", 4: "abd", 5: "abd", 6: "abd", 7: "b", 8: "a"}


def block_flags(blk, mb_avail):
    """Neighbour availability of one 4x4 block.  mb_avail = (A, B, C, D) of the macroblock (left, above, above-right,
    above-left; same slice and inside the picture).  -> (a, b, c, d) for the block: left column, above row, up-right
    samples, corner sample."""
    ma, mb_, mc, md = mb_avail
    bx, by = BLK_XY[blk]
    a = ma if bx == 0 else True
    b = mb_ if by == 0 else True
    if bx == 0 and by == 0:
        d = md
    elif by == 0:
        d = mb_
    elif bx == 0:
        d = ma
    else:
        d = True
    if by == 0:
        c = mb_ if bx < 3 else mc
        if MUTANT == "ur_blk5_never" and blk == 5:
            c = False
        if MUTANT == "ur_blk5_as_B" and blk == 5:
            c = mb_
    else:
        c = UP_RIGHT_INSIDE[blk]
        if MUTANT == "ur_blk3_11" and blk in (3, 11):
            c = True
        if MUTANT == "ur_blk7" and blk == 7:
            c = True
        if MUTANT == "ur_blk13_15" and blk in (13, 15):
            c = True
        if MUTANT == "ur_blk2_never" and blk == 2:
            c = False
    if MUTANT == "ur_blk1_never" and blk == 1:
        c = False
    return a, b, c, d


def pred4(plane, x0, y0, mode, a, b, c, d):
    """Intra 4x4 prediction of the block at (x0, y0).  Raises ValueError if the mode needs a neighbour that is not
    available.  c = the up-right samples exist; when they do not (and b), p[3, -1] is replicated into p[4..7, -1]."""
    top = [plane[y0 - 1][x0 + i] for i in range(4)] if b else None
    left = [plane[y0 + i][x0 - 1] for i in range(4)] if a else None
    corner = plane[y0 - 1][x0 - 1] if d else None
    if b:
        if c:
            top = top + [plane[y0 - 1][x0 + 4 + i] for i in range(4)]
        elif MUTANT == "ur_subst_128":
            top = top + [128] * 4
        else:
            top = top + [top[3]] * 4

    def t(x):                     # p[x, -1], x = -1 is the corner
        return corner if x < 0 else top[x]

    def l(y):                     # noqa: E741   p[-1, y], y = -1 is the corner
        return corner if y < 0 else left[y]
    for ch in NEEDS[mode]:
        if {"a": left, "b": top, "d": corner}[ch] is None:
            raise ValueError(f"Intra4x4 mode {mode} ({MODE_NAMES[mode]}) needs neighbour {ch.upper()}")
    out = [[0] * 4 for _ in range(4)]
    for y in range(4):
        for x in range(4):
            if mode == 0:
                v = t(x)
            elif mode == 1:
                v = l(y)
            elif mode == 2:
                if a and b:
                    v = (sum(top[:4]) + sum(left) + (3 if MUTANT == "dc_round" else 4)) >> 3
                elif a:
                    v = (sum(left) + 2) >> 2
                elif b:
                    v = (sum(top[:4]) + (1 if MUTANT == "dc_top_round" else 2)) >> 2
                else:
                    v = 128
            elif mode == 3:
                if x == 3 and y == 3:
                    v = (t(6) + (2 if MUTANT == "ddl_corner" else 3) * t(7) + 2) >> 2
                else:
                    v = (t(x + y) + 2 * t(x + y + 1) + t(x + y + 2) + 2) >> 2
            elif mode == 4:
                if x > y:
                    v = (t(x - y - 2) + 2 * t(x - y - 1) + t(x - y) + 2) >> 2
                elif x < y:
                    v = (l(y - x - 2) + 2 * l(y - x - 1) + l(y - x) + 2) >> 2
                else:
                    v = (t(0) + 2 * corner + l(0) + 2) >> 2
            elif mode == 5:
                z = 2 * x - y
                k = x - (y >> 1)
                if z >= 0 and z % 2 == 0:
                    v = (t(k - 1) + t(k) + 1) >> 1
                elif z >= 0:
                    if MUTANT == "vr_odd_2tap":
                        v = (t(k - 1) + t(k) + 1) >> 1
                    else:
                        v = (t(k - 2) + 2 * t(k - 1) + t(k) + 2) >> 2
                elif z == -1:
                    v = (l(0) + 2 * corner + t(0) + 2) >> 2
                else:
                    v = (l(y - 1) + 2 * l(y - 2) + l(y - 3) + 2) >> 2
            elif mode == 6:
                z = 2 * y - x
                k = y - (x >> 1)
                if z >= 0 and z % 2 == 0:
                    v = (l(k - 1) + l(k) + 1) >> 1
                elif z >= 0:
                    v = (l(k - 2) + 2 * l(k - 1) + l(k) + 2) >> 2
                elif z == -1:
                    v = (l(0) + 2 * corner + t(0) + 2) >> 2
                elif MUTANT == "hd_else_2tap":
                    v = (t(x - 1) + t(x - 2) + 1) >> 1
                else:
                    v = (t(x - 1) + 2 * t(x - 2) + t(x - 3) + 2) >> 2
            elif mode == 7:
                k = x + (y >> 1)
                if y % 2 == 0 or MUTANT == "vl_odd_2tap":
                    v = (t(k) + t(k + 1) + 1) >> 1
                else:
                    v = (t(k) + 2 * t(k + 1) + t(k + 2) + 2) >> 2
            else:
                z = x + 2 * y
                k = y + (x >> 1)
                if z > 5:
                    v = l(3)
                elif z == 5:
                    v = l(3) if MUTANT == "hu_z5" else (l(2) + 3 * l(3) + 2) >> 2
                elif z % 2 == 0:
                    v = (l(k) + l(k + 1) + 1) >> 1
                else:
                    v = (l(k) + 2 * l(k + 1) + l(k + 2) + 2) >> 2
            out[y][x] = v
    return out


# ------------------------------------------------------------------ mode prediction (8.3.1.1)
def derive_modes(syntax, addr, avail_a, avail_b, modes_of):
    """The 16 Intra4x4PredMode values of an I_NxN macroblock.
    syntax   = 16 entries, -1 when prev_intra4x4_pred_mode_flag is 1, else rem_intra4x4_pred_mode (0..7)
    avail_a / avail_b = the left / above macroblock is available (same slice, inside the picture)
    modes_of = callable(which) with which 'A' or 'B' -> the 16 modes of that neighbouring macroblock if it is I_NxN,
               else None (Intra16x16 or I_PCM: such a neighbour counts as DC, mode 2).
    -> list of 16 modes in luma4x4BlkIdx order."""
    cur = [None] * 16
    nbr = {"A": modes_of("A") if avail_a else None, "B": modes_of("B") if avail_b else None}
    for blk in range(16):
        bx, by = BLK_XY[blk]
        found = []
        unavailable = False
        for which, (nx, ny) in (("A", (bx - 1, by)), ("B", (bx, by - 1))):
            if nx >= 0 and ny >= 0:
                found.append(cur[BLK_IDX[(nx, ny)]])
                continue
            avail = avail_a if which == "A" else avail_b
            if MUTANT == "mp_unavail_is_dc_only" and not avail:
                found.append(2)                      # wrong: only this neighbour counts as DC
                continue
            if not avail:
                unavailable = True
                found.append(2)
                continue
            m = nbr[which]
            if m is None:
                found.append(0 if MUTANT == "mp_other_is_vertical" else 2)
            else:
                nx2, ny2 = (3, ny) if which == "A" else (nx, 3)
                found.append(m[BLK_IDX[(nx2, ny2)]])
        if unavailable:
            pred = 2
        elif MUTANT == "mp_max":
            pred = max(found)
        else:
            pred = min(found)
        s = syntax[blk]
        if s < 0:
            cur[blk] = pred
        elif MUTANT == "mp_rem_le":
            cur[blk] = s if s <= pred else s + 1
        else:
            cur[blk] = s if s < pred else s + 1
    return cur


# ------------------------------------------------------------------ reconstruction of an I_NxN macroblock
def reconstruct_i4(planes, mbx, mby, mb_avail, modes, blocks, cbp, qp, tr):
    """Predict, add residual and store the 16 luma 4x4 blocks of one I_NxN macroblock (z-order: block k reads the
    reconstructed samples of the blocks before it).  blocks = dict ('luma', blkIdx) -> 16 levels in scan order;
    cbp = coded_block_pattern luma bits (bit q set: the 4 blocks of 8x8 quadrant q have levels)."""
    plane = planes[0]
    if MUTANT == "pred_all_first":              # wrong: all 16 predictions before any block is stored
        preds = []
        for blk in range(16):
            bx, by = BLK_XY[blk]
            preds.append(pred4(plane, 16 * mbx + 4 * bx, 16 * mby + 4 * by, modes[blk], *block_flags(blk, mb_avail)))
    for blk in range(16):
        bx, by = BLK_XY[blk]
        x0, y0 = 16 * mbx + 4 * bx, 16 * mby + 4 * by
        a, b, c, d = block_flags(blk, mb_avail)
        pred = preds[blk] if MUTANT == "pred_all_first" else pred4(plane, x0, y0, modes[blk], a, b, c, d)
        if (cbp >> (blk >> 2)) & 1 and ("luma", blk) in blocks:
            res = transref.residual4x4(transref.inverse_zigzag(blocks[("luma", blk)]), qp, False, tr)
        else:
            res = [[0] * 4 for _ in range(4)]
        for y in range(4):
            for x in range(4):
                plane[y0 + y][x0 + x] = clip1(pred[y][x] + res[y][x])
