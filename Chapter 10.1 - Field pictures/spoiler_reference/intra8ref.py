"""Reference Intra 8x8 prediction (spec 8.3.2: neighbour availability, reference-sample substitution and low-pass filtering,
the nine modes), Intra8x8PredMode derivation (8.3.2.1) and I_NxN reconstruction with the 8x8 transform.

SPOILER: the solution to Chapter 8.5's Intra 8x8 part.  Planes are lists of rows (plane[y][x]).  Notation: p[x, -1] above
(x = 0..15, 8..15 are the "up-right" samples), p[-1, y] left (y = 0..7), p[-1, -1] the corner.
MUTANT switches on ONE deliberately wrong rule for the harness self-test (None = correct).
"""
import transref
from transref import clip1

MUTANT = None
MUTANTS = {
    "i8_no_filter": "the reference samples are used unfiltered",
    "i8_filter_121_top_edge": "p'[0,-1] is (128 + 2 p[0,-1] + p[1,-1] + 2) >> 2 when the corner is unavailable (should be (3 p[0,-1] + p[1,-1] + 2) >> 2)",
    "i8_filter_last_top": "p'[15,-1] = (p[14] + 2 p[15] + 128 + 2) >> 2 (a made-up sample beyond the row) instead of (p[14] + 3 p[15] + 2) >> 2",
    "i8_filter_corner_both": "p'[-1,-1] = (p[0,-1] + p[-1,0] + 1) >> 1: the corner sample itself is not part of its own filter",
    "i8_filter_left_last": "p'[-1,7] = (p[-1,6] + 2 p[-1,7] + 128 + 2) >> 2 instead of (p[-1,6] + 3 p[-1,7] + 2) >> 2",
    "i8_no_ur_subst": "unavailable up-right samples are replaced by 128 instead of by p[7,-1]",
    "i8_ur_blk1_never": "block 1 never uses the up-right macroblock (up-right counts as unavailable)",
    "i8_ur_blk2_never": "block 2 never uses its up-right samples (they sit in block 1)",
    "i8_ur_blk3_avail": "block 3 up-right samples count as available",
    "i8_ur_blk0_never": "block 0 never uses the up-right samples (macroblock above, right half)",
    "i8_dc_round": "DC with both neighbours: (sum + 7) >> 4",
    "i8_dc_single": "DC with only the left neighbour: (sum + 3) >> 3",
    "i8_ddl_corner": "diagonal-down-left sample (7,7) uses the general 3-tap rule",
    "i8_vr_neg": "vertical-right: samples with zVR < -1 use the zVR = -1 formula",
    "i8_hd_swap": "horizontal-down: zHD > 0 odd rows use the 2-tap formula",
    "i8_vl_odd2": "vertical-left: odd rows use the 2-tap formula",
    "i8_hu_13": "horizontal-up: zHU = 13 uses p[-1,7]",
    "i8_hu_gt": "horizontal-up: zHU > 13 uses p[-1,6]",
    "i8_mode_min_to_max": "predIntra8x8PredMode = Max(A, B)",
    "i8_mode_a_idx": "neighbour A of an 8x8 block in an Intra 4x4 macroblock reads Intra4x4PredMode[blk8 * 4] (not * 4 + 1)",
    "i8_mode_b_idx": "neighbour B of an 8x8 block in an Intra 4x4 macroblock reads Intra4x4PredMode[blk8 * 4] (not * 4 + 2)",
    "i4_nb_i8_as_dc": "an Intra 4x4 / 8x8 block next to an Intra 8x8 macroblock treats it as DC (mode 2) instead of reading its modes",
    "i8_pred_all_first": "all four 8x8 predictions are made before any block is reconstructed",
}

MODE_NAMES = ["vertical", "horizontal", "DC", "diagonal down-left", "diagonal down-right", "vertical-right",
              "horizontal-down", "vertical-left", "horizontal-up"]
NEEDS = {0: "b", 1: "a", 2: "", 3: "b", 4: "abd", 5: "abd", 6: "abd", 7: "b", 8: "a"}
BLK8_XY = [(0, 0), (1, 0), (0, 1), (1, 1)]


def block_flags8(blk, mb_avail):
    """-> (a, b, c, d): left column, above row, up-right samples, corner of 8x8 block blk (0..3);
    mb_avail = (A, B, C, D) availability of the left / above / above-right / above-left macroblock."""
    ma, mb_, mc, md = mb_avail
    if blk == 0:
        a, b, c, d = ma, mb_, mb_, md
        if MUTANT == "i8_ur_blk0_never":
            c = False
    elif blk == 1:
        a, b, c, d = True, mb_, mc, mb_
        if MUTANT == "i8_ur_blk1_never":
            c = False
    elif blk == 2:
        a, b, c, d = ma, True, True, ma
        if MUTANT == "i8_ur_blk2_never":
            c = False
    else:
        a, b, c, d = True, True, False, True
        if MUTANT == "i8_ur_blk3_avail":
            c = True
    return a, b, c, d


def filter_refs(top, left, corner):
    """8.3.2.2.1.  top: 16 samples (already substituted) or None; left: 8 or None; corner: sample or None.
    -> (top', left', corner')."""
    if MUTANT == "i8_no_filter":
        return top, left, corner
    t = l = None
    if top is not None:
        t = [0] * 16
        if corner is not None or MUTANT == "i8_filter_121_top_edge":
            c = corner if corner is not None else 128
            t[0] = (c + 2 * top[0] + top[1] + 2) >> 2
        else:
            t[0] = (3 * top[0] + top[1] + 2) >> 2
        for x in range(1, 15):
            t[x] = (top[x - 1] + 2 * top[x] + top[x + 1] + 2) >> 2
        t[15] = (top[14] + 2 * top[15] + 128 + 2) >> 2 if MUTANT == "i8_filter_last_top" else (top[14] + 3 * top[15] + 2) >> 2
    if left is not None:
        l = [0] * 8
        if corner is not None:
            l[0] = (corner + 2 * left[0] + left[1] + 2) >> 2
        else:
            l[0] = (3 * left[0] + left[1] + 2) >> 2
        for y in range(1, 7):
            l[y] = (left[y - 1] + 2 * left[y] + left[y + 1] + 2) >> 2
        l[7] = (left[6] + 2 * left[7] + 128 + 2) >> 2 if MUTANT == "i8_filter_left_last" else (left[6] + 3 * left[7] + 2) >> 2
    c2 = None
    if corner is not None:
        if MUTANT == "i8_filter_corner_both" and top is not None and left is not None:
            c2 = (top[0] + left[0] + 1) >> 1
        elif top is None or left is None:
            if top is not None:
                c2 = (3 * corner + top[0] + 2) >> 2
            elif left is not None:
                c2 = (3 * corner + left[0] + 2) >> 2
            else:
                c2 = corner
        else:
            c2 = (top[0] + 2 * corner + left[0] + 2) >> 2
    return t, l, c2


def pred8(plane, x0, y0, mode, a, b, c, d):
    """Intra 8x8 prediction of the block at (x0, y0) -> 8x8 rows.  a, b, c, d as block_flags8."""
    top = [plane[y0 - 1][x0 + i] for i in range(8)] if b else None
    left = [plane[y0 + i][x0 - 1] for i in range(8)] if a else None
    corner = plane[y0 - 1][x0 - 1] if d else None
    if b:
        if c:
            top = top + [plane[y0 - 1][x0 + 8 + i] for i in range(8)]
        elif MUTANT == "i8_no_ur_subst":
            top = top + [128] * 8
        else:
            top = top + [top[7]] * 8
    for ch in NEEDS[mode]:
        if {"a": left, "b": top, "d": corner}[ch] is None:
            raise ValueError(f"Intra8x8 mode {mode} ({MODE_NAMES[mode]}) needs neighbour {ch.upper()}")
    top, left, corner = filter_refs(top, left, corner)

    def t(x):
        return corner if x < 0 else top[x]

    def l(y):  # noqa: E743
        return corner if y < 0 else left[y]
    out = [[0] * 8 for _ in range(8)]
    for y in range(8):
        for x in range(8):
            if mode == 0:
                v = t(x)
            elif mode == 1:
                v = l(y)
            elif mode == 2:
                if a and b:
                    v = (sum(top[:8]) + sum(left) + (7 if MUTANT == "i8_dc_round" else 8)) >> 4
                elif a:
                    v = (sum(left) + (3 if MUTANT == "i8_dc_single" else 4)) >> 3
                elif b:
                    v = (sum(top[:8]) + 4) >> 3
                else:
                    v = 128
            elif mode == 3:
                if x == 7 and y == 7 and MUTANT != "i8_ddl_corner":
                    v = (t(14) + 3 * t(15) + 2) >> 2
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
                    v = (t(k - 2) + 2 * t(k - 1) + t(k) + 2) >> 2
                elif z == -1 or MUTANT == "i8_vr_neg":
                    v = (l(0) + 2 * corner + t(0) + 2) >> 2
                else:
                    v = (l(y - 2 * x - 1) + 2 * l(y - 2 * x - 2) + l(y - 2 * x - 3) + 2) >> 2
            elif mode == 6:
                z = 2 * y - x
                k = y - (x >> 1)
                if z >= 0 and z % 2 == 0:
                    v = (l(k - 1) + l(k) + 1) >> 1
                elif z >= 0:
                    if MUTANT == "i8_hd_swap":
                        v = (l(k - 1) + l(k) + 1) >> 1
                    else:
                        v = (l(k - 2) + 2 * l(k - 1) + l(k) + 2) >> 2
                elif z == -1:
                    v = (l(0) + 2 * corner + t(0) + 2) >> 2
                else:
                    v = (t(x - 2 * y - 1) + 2 * t(x - 2 * y - 2) + t(x - 2 * y - 3) + 2) >> 2
            elif mode == 7:
                k = x + (y >> 1)
                if y % 2 == 0 or MUTANT == "i8_vl_odd2":
                    v = (t(k) + t(k + 1) + 1) >> 1
                else:
                    v = (t(k) + 2 * t(k + 1) + t(k + 2) + 2) >> 2
            else:
                z = x + 2 * y
                k = y + (x >> 1)
                if z > 13:
                    v = l(6) if MUTANT == "i8_hu_gt" else l(7)
                elif z == 13:
                    v = l(7) if MUTANT == "i8_hu_13" else (l(6) + 3 * l(7) + 2) >> 2
                elif z % 2 == 0:
                    v = (l(k) + l(k + 1) + 1) >> 1
                else:
                    v = (l(k) + 2 * l(k + 1) + l(k + 2) + 2) >> 2
            out[y][x] = v
    return out


# ------------------------------------------------------------------ mode prediction (8.3.2.1)
def derive_modes8(syntax, avail_a, avail_b, modes_of):
    """The 4 Intra8x8PredMode values of an I_NxN macroblock with transform_size_8x8_flag.
    syntax = 4 entries, -1 when prev_intra8x8_pred_mode_flag is 1, else rem_intra8x8_pred_mode (0..7);
    modes_of(which) -> the 16 mode entries of the neighbouring macroblock 'A' / 'B' (an Intra 8x8 macroblock stores each of its
    modes four times), or None when that macroblock is not I_NxN (Intra16x16, I_PCM, inter: counts as DC).
    -> list of 4 modes."""
    cur = [None] * 4
    nbr = {"A": modes_of("A") if avail_a else None, "B": modes_of("B") if avail_b else None}
    for blk in range(4):
        bx, by = BLK8_XY[blk]
        found, unavailable = [], False
        for which, inside in (("A", bx > 0), ("B", by > 0)):
            if inside:
                found.append(cur[blk - 1] if which == "A" else cur[blk - 2])
                continue
            avail = avail_a if which == "A" else avail_b
            if not avail:
                unavailable = True
                found.append(2)
                continue
            m = nbr[which]
            if m is None:
                found.append(2)
            elif which == "A":
                q = by * 2 + 1                                  # the neighbouring 8x8 block (right column, same row)
                found.append(m[q * 4] if MUTANT == "i8_mode_a_idx" else m[q * 4 + 1])
            else:
                q = 2 + bx                                      # bottom row, same column
                found.append(m[q * 4] if MUTANT == "i8_mode_b_idx" else m[q * 4 + 2])
        pred = 2 if unavailable else (max(found) if MUTANT == "i8_mode_min_to_max" else min(found))
        s = syntax[blk]
        cur[blk] = pred if s < 0 else (s if s < pred else s + 1)
    return cur


def expand8(modes4):
    """4 Intra8x8 modes -> 16 entries indexed by luma4x4BlkIdx (what an Intra 4x4 neighbour reads)."""
    if MUTANT == "i4_nb_i8_as_dc":
        return [2] * 16
    return [modes4[i >> 2] for i in range(16)]


# ------------------------------------------------------------------ reconstruction of an Intra 8x8 macroblock
def reconstruct_i8(planes, mbx, mby, mb_avail, modes, blocks, cbp, qp, tr):
    """Predict, add residual and store the four 8x8 luma blocks (block k reads the reconstructed samples of the blocks before it).
    blocks: dict ('luma8', b8) -> 64 levels in 8x8 zig-zag order; cbp: luma bits."""
    plane = planes[0]
    preds = None
    if MUTANT == "i8_pred_all_first":
        preds = []
        for blk in range(4):
            bx, by = BLK8_XY[blk]
            preds.append(pred8(plane, 16 * mbx + 8 * bx, 16 * mby + 8 * by, modes[blk], *block_flags8(blk, mb_avail)))
    for blk in range(4):
        bx, by = BLK8_XY[blk]
        x0, y0 = 16 * mbx + 8 * bx, 16 * mby + 8 * by
        pred = preds[blk] if preds else pred8(plane, x0, y0, modes[blk], *block_flags8(blk, mb_avail))
        if (cbp >> blk) & 1 and ("luma8", blk) in blocks:
            res = transref.residual8x8(blocks[("luma8", blk)], qp, tr)
        else:
            res = [[0] * 8 for _ in range(8)]
        for y in range(8):
            for x in range(8):
                plane[y0 + y][x0 + x] = clip1(pred[y][x] + res[y][x])
