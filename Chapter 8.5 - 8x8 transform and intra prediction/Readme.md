# Chapter 8.5 - 8x8 transform and 8x8 intra prediction (High profile)

## 1. Goal and the project

So far every residual block is 4x4 and Intra prediction works on 4x4 blocks or the whole 16x16. **High profile** lets a macroblock use an **8x8 transform** instead of sixteen 4x4 ones. x264's default profile is High, and so are most real `.ts` files, so this is where your decoder starts working on them.

The switch is one syntax element, `transform_size_8x8_flag`. It changes the syntax (where the flag sits, its CABAC context), the residual (64-coefficient blocks: four interleaved 4x4 blocks in CAVLC, a new `ctxBlockCat` 5 in CABAC), the scan, dequantisation and inverse transform, adds **Intra 8x8 prediction**, and changes two deblocking rules.

You verify it two ways: a dump of what you parsed, and whole High-profile streams decoded to **ffmpeg's exact pixels**. Everything else comes from your decoder of Chapters 5 to 8.4. **Milestone**: `T11_high` (default x264 High: CABAC, B frames, 8x8 transform) and its CAVLC variant decode byte-identical.

Out of scope: scaling matrices (Chapter 8.6; all streams here use the flat default), field pictures and MBAFF, 4:4:4, high bit depth, lossless (`qpprime_y_zero_transform_bypass`).

## 2. Big picture

A transform turns residual samples into a few large coefficients and many small ones. A bigger block describes smooth areas with fewer coefficients but does worse near edges and detail, so the encoder chooses per macroblock; the flag carries that choice.

The 8x8 transform is the 4x4 idea scaled up: an integer approximation of a cosine transform, rows then columns, final `(x + 32) >> 6`, with its own scan order and dequantisation table. Chroma never uses it in 4:2:0.

Intra 8x8 mirrors Intra 4x4 (nine directions, modes predicted from the neighbours), but the neighbouring samples are **smoothed** first and the up-right samples come from different places. Deblocking follows the transform: there are no 4x4 block edges inside an 8x8-transformed macroblock, so those are not filtered.

## 3. Concepts in detail

### 3.1 Where transform_size_8x8_flag lives (7.3.5)

It exists only if `transform_8x8_mode_flag` = 1 in the **PPS** (in the High-profile PPS extension; absent = 0). The same extension carries `second_chroma_qp_index_offset`, used for **Cr** (Cb keeps `chroma_qp_index_offset`; the two are equal when the extension is absent). Make sure your Chapter 3.2 PPS parser reads it: Cr dequantisation and Cr deblocking use it.

For inter macroblocks one more condition is needed. `noSubMbPartSizeLessThan8x8Flag` is 1 unless some sub-macroblock of a P_8x8 / B_8x8 has more than one partition (8x4, 4x8, 4x4), or is a **direct** sub-macroblock while the SPS's `direct_8x8_inference_flag` is 0. (An 8x8 transform cannot straddle prediction blocks smaller than 8x8.)

| Macroblock | Flag is read | When |
|---|---|---|
| I_NxN | right after `mb_type`, **before** the prediction modes | always (1: Intra 8x8, 0: Intra 4x4) |
| Intra16x16, I_PCM, P_Skip, B_Skip | never | |
| other P / B macroblocks, incl. B_Direct_16x16 | right after `coded_block_pattern`, before `mb_qp_delta` | CBP luma (low 4 bits) is not 0, **and** `noSubMbPartSizeLessThan8x8Flag` = 1, **and** for B_Direct_16x16 `direct_8x8_inference_flag` = 1 |

When the flag is not read it is 0. An Intra 8x8 macroblock has **four** prediction modes (3.8) instead of sixteen, coded with the same elements (`prev_intra8x8_pred_mode_flag`, then a 3-bit `rem_intra8x8_pred_mode` if it is 0; in CABAC with contexts 68 and 69 as for 4x4).

**Coding the flag.** CAVLC: `u(1)`. CABAC: one bin, `ctxIdx = 399 + condTermFlagA + condTermFlagB`, where A / B are the macroblocks left / above, and `condTermFlagN` = 1 if N is available (inside the picture, same slice) **and its own** `transform_size_8x8_flag` is 1. Skipped, I_PCM, Intra16x16 and inter macroblocks without the flag count 0. The (m, n) values for 399..401 are in Chapter 8.1's tables.

**Worked example 1.** Left neighbour Intra 8x8, above neighbour Intra 4x4: `ctxIdx` = 399 + 1 + 0 = **400**. Top row, I_PCM on the left: 399.

### 3.2 The 8x8 residual in CAVLC (7.3.5.3.2, 9.2)

Each 8x8 luma block whose CBP bit is set (quadrant order as before) is sent as **four ordinary 4x4 `residual_block`s** of 16 coefficients (`coeff_token`, levels, `total_zeros`, `run_before`). They are numbered `4 * b8 + k` (k = 0..3) as in the 4x4 case, and **each keeps its own `total_coeff`** for the nC of later blocks and neighbouring macroblocks (Chapter 4.3). Nothing about nC changes.

What changes is where the coefficients go: coefficient `i` (0..15) of 4x4 block `k` is the 8x8 coefficient at 8x8 scan position **`4 * i + k`**. Block 0 holds positions 0, 4, 8, ..., 60; block 1 holds 1, 5, ..., 61; and so on.

**Worked example 2.** 4x4 block 1 of an 8x8 block decodes `5` at index 0, `-2` at index 1, `1` at index 3. These are 8x8 positions 4 * 0 + 1 = **1**, **5** and **13**; the dump (5.1) prints them merged with blocks 0, 2 and 3 as `luma8<b8>:1=5,5=-2,13=1,...`.

In CAVLC an 8x8 block with its CBP bit set can still have all four `TotalCoeff` 0; the dump then prints `luma8<b8>:-`.

### 3.3 The 8x8 residual in CABAC: ctxBlockCat 5 (7.3.5.3.3, 9.3.3.1.3)

Each 8x8 block whose CBP bit is set is **one** block of **64 coefficients**, decoded with the three passes of Chapter 8.3 (significance map, levels in reverse order, bypass signs). Differences:

* **No `coded_block_flag`.** In 4:2:0 it is not sent and is inferred 1, so the CBP bit alone says whether the block exists.
* **Own contexts** (frame macroblocks): `significant_coeff_flag` base **402** (15 contexts), `last_significant_coeff_flag` base **417** (9), `coeff_abs_level_minus1` base **426** (10).
* **Significance increments come from Table 9-43**, not from the position (that would need 63 contexts). Both flags use `ctxIdx = base + table[position]` for positions 0..62; position 63 is never coded (inferred significant if nothing earlier was last). Rows of 8 positions:

```
sig (ctxIdxInc of significant_coeff_flag, positions 0..62):
 0  1  2  3  4  5  5  4 |  4  3  3  4  4  4  5  5 |  4  4  4  4  3  3  6  7 |  7  7  8  9 10  9  8  7
 7  6 11 12 13 11  6  7 |  8  9 14 10  9  8  6 11 | 12 13 11  6  9 14 10  9 | 11 12 13 11 14 10 12
last (ctxIdxInc of last_significant_coeff_flag, positions 0..62):
 0  1  1  1  1  1  1  1 |  1  1  1  1  1  1  1  1 |  2  2  2  2  2  2  2  2 |  2  2  2  2  2  2  2  2
 3  3  3  3  3  3  3  3 |  4  4  4  4  4  4  4  4 |  5  5  5  5  6  6  6  6 |  7  7  7  7  8  8  8
```

* **Levels**: Chapter 8.3's increments on base 426: bin 0 `(numDecodAbsLevelGt1 != 0) ? 0 : Min(4, 1 + numDecodAbsLevelEq1)`, bins 1..13 `5 + Min(4 - (ctxBlockCat == 3 ? 1 : 0), numDecodAbsLevelGt1)`, which for cat 5 is `5 + Min(4, numDecodAbsLevelGt1)`; same Exp-Golomb escape.
* **Neighbours that are 8x8-coded.** When a 4x4 block (cat 1 / 2) predicts its `coded_block_flag` from a neighbouring macroblock that used the 8x8 transform: if that quadrant's CBP bit is set, the neighbour's flag is the **inferred 1** (for all four 4x4 positions of the quadrant); if it is 0, the block counts as not coded, as in 8.3.

**Worked example 3.** Coefficients `[0, 3, 0, ...]`: position 0's significance flag uses 402 + 0, position 1's uses 402 + 1 and, being significant, its last flag uses 417 + 1. Position 6 would use 402 + 5, position 22 uses 402 + 6; the last flag of position 20 uses 417 + 2.

### 3.4 The 8x8 zig-zag scan (Table 8-13, frame macroblocks)

The same zig-zag as the 4x4 scan: walk the anti-diagonals alternately up-right and down-left, first step to the right, ending at (7, 7). Scan position -> (row, column):

```
pos:  0     1     2     3     4     5     6     7     8     9     10    11    12 ...
(r,c):(0,0) (0,1) (1,0) (2,0) (1,1) (0,2) (0,3) (1,2) (2,1) (3,0) (4,0) (3,1) (2,2) ...
```

Raster index `8 * row + column` of positions 0..15: 0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5.

### 3.5 Dequantisation (8.5.9, flat weights)

For 8-bit luma `qP` = QP_Y. The coefficient matrix `c` (row `i`, column `j`, from the scan) becomes `d` with `LevelScale8x8(m, i, j)` = 16 (flat weight) * `normAdjust8x8(m, i, j)`, `m` = qP % 6:

| m | class 0 | class 1 | class 2 | class 3 | class 4 | class 5 |
|---|---|---|---|---|---|---|
| 0 | 20 | 18 | 32 | 19 | 25 | 24 |
| 1 | 22 | 19 | 35 | 21 | 28 | 26 |
| 2 | 26 | 23 | 42 | 24 | 33 | 31 |
| 3 | 28 | 25 | 45 | 26 | 35 | 33 |
| 4 | 32 | 28 | 51 | 30 | 40 | 38 |
| 5 | 36 | 32 | 58 | 34 | 46 | 43 |

Position class of (i, j), first match wins:

| Class | Condition |
|---|---|
| 0 | `i % 4 == 0` and `j % 4 == 0` |
| 1 | `i % 2 == 1` and `j % 2 == 1` |
| 2 | `i % 4 == 2` and `j % 4 == 2` |
| 3 | (`i % 4 == 0` and `j % 2 == 1`) or (`i % 2 == 1` and `j % 4 == 0`) |
| 4 | (`i % 4 == 0` and `j % 4 == 2`) or (`i % 4 == 2` and `j % 4 == 0`) |
| 5 | everything else |

If `qP >= 36`: `d = (c * LevelScale8x8) << (qP / 6 - 6)`; else `d = (c * LevelScale8x8 + 2^(5 - qP / 6)) >> (6 - qP / 6)` (arithmetic shift, rounds toward minus infinity). There is no separate DC path.

**Worked example 4.** qP = 28: m = 4, qP / 6 = 4, so `>> 2` with offset 2. Level 3 at (0, 0): class 0, LevelScale 16 * 32 = 512, `(1536 + 2) >> 2` = **384**. Level -2 at (1, 0): class 3, LevelScale 480, `(-960 + 2) >> 2` = **-240** (floor of -239.5). At qP = 40 (shift `<< 0`): level 1 at (0, 0) gives 512.

### 3.6 The 8x8 inverse transform (8.5.13)

Transform every **row** of `d`, then every **column** of the result, with this butterfly on eight values `d0..d7` (outputs `g0..g7`):

```
e0 = d0 + d4                    e1 = -d3 + d5 - d7 - (d7 >> 1)
e2 = d0 - d4                    e3 =  d1 + d7 - d3 - (d3 >> 1)
e4 = (d2 >> 1) - d6             e5 = -d1 + d7 + d5 + (d5 >> 1)
e6 = d2 + (d6 >> 1)             e7 =  d3 + d5 + d1 + (d1 >> 1)
f0 = e0 + e6    f1 = e1 + (e7 >> 2)    f2 = e2 + e4    f3 = e3 + (e5 >> 2)
f4 = e2 - e4    f5 = (e3 >> 2) - e5    f6 = e0 - e6    f7 = e7 - (e1 >> 2)
g0 = f0 + f7    g1 = f2 + f5    g2 = f4 + f3    g3 = f6 + f1
g4 = f6 - f1    g5 = f4 - f3    g6 = f2 - f5    g7 = f0 - f7
```

Then the residual is `r = (x + 32) >> 6`, added to the prediction and clipped to 0..255, as in 4x4.

**Worked example 5.** Only `d(0,0)` = 384: every sample becomes 384 after both passes, and `(384 + 32) >> 6` = **6** everywhere. Only level 1 at scan position 1 = (0, 1), qP 28: `d(0,1)` = (480 + 2) >> 2 = 120; the row pass gives `[180, 150, 90, 45, -45, -90, -150, -180]`, and every residual row is **`[3, 2, 1, 1, -1, -1, -2, -3]`**.

### 3.7 Intra 8x8 prediction (8.3.2)

An Intra 8x8 macroblock (I_NxN, flag 1) has four 8x8 luma blocks `blk8` = 0..3 (0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right), predicted and reconstructed **one after the other** (block 1 reads block 0's reconstructed samples, and so on). Chroma is unchanged (Chapter 5.3).

**Neighbouring samples** of the block at (x0, y0): above `p[x, -1]` for x = 0..7, **up-right** `p[x, -1]` for x = 8..15, left `p[-1, y]` for y = 0..7, corner `p[-1, -1]`.

**Availability.** Macroblock availability is the usual one (inside the picture, same slice; with `constrained_intra_pred_flag` = 1 an inter macroblock counts as unavailable). Left: blocks 0 and 2 need macroblock A, blocks 1 and 3 always have it. Above: blocks 0 and 1 need B, blocks 2 and 3 always. Corner: block 0 needs D, block 1 B, block 2 A, block 3 always. Up-right:

| Block | Up-right samples come from | Available when |
|---|---|---|
| 0 | macroblock B (right half of its bottom row) | B is available |
| 1 | macroblock C (above-right) | C is available |
| 2 | block 1 of this macroblock (its bottom row) | always |
| 3 | not yet decoded | **never** |

**Substitution.** If the up-right samples are unavailable but the above ones are, copy `p[7, -1]` into `p[8..15, -1]`. Now the above row is all 16 samples or none.

**Smoothing filter (8.3.2.2.1).** Prediction uses filtered samples `p'`, always computed from the unfiltered (substituted) `p`:

* above row, if available: `p'[0,-1]` = `(p[-1,-1] + 2 p[0,-1] + p[1,-1] + 2) >> 2` if the corner is available, else `(3 p[0,-1] + p[1,-1] + 2) >> 2`; `p'[x,-1]` = `(p[x-1,-1] + 2 p[x,-1] + p[x+1,-1] + 2) >> 2` for x = 1..14; `p'[15,-1]` = `(p[14,-1] + 3 p[15,-1] + 2) >> 2`;
* corner, if available: with both `p[0,-1]` and `p[-1,0]` available `(p[0,-1] + 2 p[-1,-1] + p[-1,0] + 2) >> 2`; with only one of them, `(3 p[-1,-1] + that one + 2) >> 2`; with neither, `p[-1,-1]` unchanged;
* left column, if available: `p'[-1,0]` = `(p[-1,-1] + 2 p[-1,0] + p[-1,1] + 2) >> 2` if the corner is available, else `(3 p[-1,0] + p[-1,1] + 2) >> 2`; `p'[-1,y]` = `(p[-1,y-1] + 2 p[-1,y] + p[-1,y+1] + 2) >> 2` for y = 1..6; `p'[-1,7]` = `(p[-1,6] + 3 p[-1,7] + 2) >> 2`.

**Worked example 6.** Corner unavailable, above row 100, 104, ...: `p'[0,-1]` = `(300 + 104 + 2) >> 2` = **101**; with a corner of 60 it is `(60 + 200 + 104 + 2) >> 2` = 91. `p[14,-1]` = 120, `p[15,-1]` = 128: `p'[15,-1]` = `(120 + 384 + 2) >> 2` = **126**.

**The nine modes** (`pred[y][x]`, x = column, y = row, all on `p'`; numbers and names as in Intra 4x4). An index of -1 into the above or left array means the corner `p'[-1,-1]`. A stream never uses a mode whose samples are unavailable.

| Mode | Needs | Definition |
|---|---|---|
| 0 vertical | above | `p'[x,-1]` |
| 1 horizontal | left | `p'[-1,y]` |
| 2 DC | - | both: `(sum of p'[0..7,-1] + sum of p'[-1,0..7] + 8) >> 4`; only one side: `(sum of its 8 + 4) >> 3`; neither: 128 |
| 3 diagonal down-left | above (16) | x = y = 7: `(p'[14,-1] + 3 p'[15,-1] + 2) >> 2`; else `(p'[x+y,-1] + 2 p'[x+y+1,-1] + p'[x+y+2,-1] + 2) >> 2` |
| 4 diagonal down-right | above, left, corner | x > y: `(p'[x-y-2,-1] + 2 p'[x-y-1,-1] + p'[x-y,-1] + 2) >> 2`; x < y: `(p'[-1,y-x-2] + 2 p'[-1,y-x-1] + p'[-1,y-x] + 2) >> 2`; x = y: `(p'[0,-1] + 2 p'[-1,-1] + p'[-1,0] + 2) >> 2` |
| 5 vertical-right | above, left, corner | `zVR = 2x - y`. Even, >= 0: `(p'[x-(y>>1)-1,-1] + p'[x-(y>>1),-1] + 1) >> 1`; odd, >= 0: `(p'[x-(y>>1)-2,-1] + 2 p'[x-(y>>1)-1,-1] + p'[x-(y>>1),-1] + 2) >> 2`; -1: `(p'[-1,0] + 2 p'[-1,-1] + p'[0,-1] + 2) >> 2`; else: `(p'[-1,y-2x-1] + 2 p'[-1,y-2x-2] + p'[-1,y-2x-3] + 2) >> 2` |
| 6 horizontal-down | above, left, corner | `zHD = 2y - x`. Even, >= 0: `(p'[-1,y-(x>>1)-1] + p'[-1,y-(x>>1)] + 1) >> 1`; odd, >= 0: `(p'[-1,y-(x>>1)-2] + 2 p'[-1,y-(x>>1)-1] + p'[-1,y-(x>>1)] + 2) >> 2`; -1: `(p'[-1,0] + 2 p'[-1,-1] + p'[0,-1] + 2) >> 2`; else: `(p'[x-2y-1,-1] + 2 p'[x-2y-2,-1] + p'[x-2y-3,-1] + 2) >> 2` |
| 7 vertical-left | above (16) | y even: `(p'[x+(y>>1),-1] + p'[x+(y>>1)+1,-1] + 1) >> 1`; y odd: `(p'[x+(y>>1),-1] + 2 p'[x+(y>>1)+1,-1] + p'[x+(y>>1)+2,-1] + 2) >> 2` |
| 8 horizontal-up | left | `zHU = x + 2y`. Even, < 13: `(p'[-1,y+(x>>1)] + p'[-1,y+(x>>1)+1] + 1) >> 1`; odd, < 13: `(p'[-1,y+(x>>1)] + 2 p'[-1,y+(x>>1)+1] + p'[-1,y+(x>>1)+2] + 2) >> 2`; 13: `(p'[-1,6] + 3 p'[-1,7] + 2) >> 2`; > 13: `p'[-1,7]` |

**Worked example 7.** DC, filtered above samples all 100, left all 120: `(800 + 960 + 8) >> 4` = **110**. Left only: `(960 + 4) >> 3` = 120.

### 3.8 Intra 8x8 prediction modes (8.3.2.1)

Decoded as in Intra 4x4: `prev_intra8x8_pred_mode_flag` = 1 means "use the predicted mode"; otherwise the mode is `rem_intra8x8_pred_mode` (0..7) if it is below the predicted mode, else `rem + 1`.

**Predicted mode** = `Min(modeA, modeB)` of the 8x8 blocks to the left (A) and above (B), except that it is 2 (DC) when the macroblock containing A or B is unavailable (or inter, with `constrained_intra_pred_flag` = 1). An available neighbouring macroblock that is not I_NxN (Intra16x16, I_PCM, inter) gives mode 2.

A neighbour inside the current macroblock is an earlier block of it. In another macroblock it is:

* **Intra 8x8**: the `Intra8x8PredMode` of the adjacent 8x8 block;
* **Intra 4x4**: A reads `Intra4x4PredMode[blk8A * 4 + 1]`, B reads `Intra4x4PredMode[blk8B * 4 + 2]`, where `blk8A` / `blk8B` is the adjacent 8x8 block (in `luma4x4BlkIdx` numbering `* 4 + 1` is that quadrant's top-right 4x4 block, `* 4 + 2` its bottom-left).

The reverse matters too: an Intra **4x4** block next to an Intra 8x8 macroblock uses the `Intra8x8PredMode` of the 8x8 block containing the neighbouring 4x4 block. Storing each macroblock's modes as 16 entries by `luma4x4BlkIdx` (an Intra 8x8 mode fills its four entries) handles that direction.

**Worked example 8.** Current `blk8` = 0; left macroblock Intra 4x4 with modes `m[0..15]`; above macroblock Intra 8x8 with modes `[7, 1, 4, 6]`. A is the left macroblock's block 1: modeA = `m[1 * 4 + 1]` = `m[5]`. B is the above macroblock's block 2: modeB = 4. Predicted = `Min(m[5], 4)`. For `blk8` = 3 both neighbours (blocks 2 and 1) are inside the macroblock.

### 3.9 Deblocking with the 8x8 transform (8.7)

The filters, `alpha`, `beta`, `tc0` and the chroma rules are unchanged. Two rules change:

1. **Edges.** In a macroblock with the flag set, the **luma** edges 1 and 3 (4 and 12 samples in, between 4x4 columns / rows) are **not filtered**, in both directions; edges 0 (macroblock edge) and 2 (middle) are. Chroma (4:2:0) is not affected: its edges 0 and 4 correspond to luma edges 0 and 8, which are always filtered.
2. **Coefficient test for bS 2.** "The 4x4 block containing the sample has non-zero coefficients" becomes, for an 8x8-transformed macroblock, "the **8x8 block** containing the sample has non-zero coefficients" (CAVLC: any non-zero in its four interleaved 4x4 blocks). This applies to each side separately, so on a macroblock edge also when only the neighbour is 8x8-transformed. Intra macroblocks are still bS 3 / 4; edge 2 inside an Intra 8x8 macroblock is bS 3.

### 3.10 What to keep per macroblock

On top of earlier chapters: `transform_size_8x8_flag` (neighbours' CABAC context, deblocking edges); the coded-block information per 4x4 position, following 3.3 (CABAC) or the per-4x4 `total_coeff` (CAVLC); the 16-entry intra mode array (3.8); a per-4x4 "has coefficients" map following 3.9.

## 4. The project: exactly what to build

1. **Parameter sets**: `transform_8x8_mode_flag`, `second_chroma_qp_index_offset`, `direct_8x8_inference_flag`.
2. **Syntax**: `transform_size_8x8_flag` (3.1) with CABAC contexts 399..401; four intra modes instead of sixteen.
3. **Residual**: CAVLC (3.2) and CABAC `ctxBlockCat` 5 (3.3), including the `coded_block_flag` neighbour rule.
4. **Reconstruction**: 8x8 scan, dequantisation and inverse transform (3.4 to 3.6) for intra and inter macroblocks; Intra 8x8 prediction (3.7) and mode derivation (3.8).
5. **Deblocking** (3.9).
6. **Output**: the dump of 5.1 (optional, but the fastest way to debug) and the decoded frames (5.2).

**Verify** (the Summary's check): `T11_high` (`-profile:v high`) and its CAVLC variant (`-x264-params cabac=0`) decode byte-identical to ffmpeg. Work up to it with the provided streams: synthetic ones and `T11_intra_cavlc` (Intra 8x8, CAVLC residual), `T11_intra` (CABAC cat 5), `T11_p_cavlc` / `T11_p` (inter flag rules, no B frames), then `T11_cavlc`, `T11_high`, and the multi-slice `T11_slices_aq*`.

## 5. Harness input and output formats

The only input is the `.ts` or Annex B `.264` file. Outputs are plain text (decimal numbers, `#` starts a comment, blank lines ignored) and raw YUV.

### 5.1 The syntax dump (`--dump`)

```
SLICE <slice> type=<I|P|B> first_mb=<first_mb_in_slice>
MB <slice> <addr> mbt=<..> t8=<..> modes=<..> imode=<..> cbp=<..> qp=<..> coef=<..>
...
END <slice> mbs=<number of MB lines>
```

`<slice>` is the 0-based index of the slice NAL unit among all slice NAL units (types 1 and 5) in the stream. Every macroblock of the slice is listed in decoding order, with all seven keys in this order. The dump is the same for CAVLC and CABAC.

| Key | Value |
|---|---|
| `mbt` | `mb_type` in the slice's own table (Tables 7-11, 7-13, 7-14: I 0..25; P 0..4 inter, 5..30 intra; B 0..22 inter, 23..48 intra), or `skip` |
| `t8` | `transform_size_8x8_flag`, 0 or 1, for I_NxN and non-skipped inter macroblocks (0 when not coded); `-` for skipped, I_PCM, Intra16x16 |
| `modes` | I_NxN only: one character per mode element, `p` if the prev flag was 1, else the `rem` digit; 16 characters, or 4 when `t8` = 1; otherwise `-` |
| `imode` | I_NxN only: the final mode (0..8) of each block, comma separated: 16 values by `luma4x4BlkIdx`, or 4 by `blk8` when `t8` = 1; otherwise `-` |
| `cbp` | `coded_block_pattern` (luma bits + 16 * chroma), for Intra16x16 derived from `mb_type`; `-` for skipped and I_PCM |
| `qp` | QP_Y after `mb_qp_delta` (the running value for skipped / I_PCM / CBP-0 macroblocks) |
| `coef` | the residual blocks in decoding order, `;`-separated, each `<kind><idx>:<list>` as in Chapters 4.4 and 8.3 (`<list>`: `-`, or comma separated `position=value` for the non-zero coefficients, ascending positions); `-` if there is no block |

Block kinds as before: `i16dc`, `i16ac` (idx 0..15), `luma` (4x4, idx = `luma4x4BlkIdx` 0..15), `cbdc` / `crdc`, `cbac` / `crac` (idx 0..3; 15-coefficient lists print scan position `k + 1`). New: **`luma8`** with idx `b8` 0..3, one per 8x8 block with its CBP bit set, positions 0..63 in **8x8 scan order** (CAVLC: the four 4x4 blocks merged as in 3.2; all-zero prints `-`). A `t8` = 1 macroblock has `luma8` entries and no `luma` entries. Beware the names: `luma80:` is 8x8 block 0, while `luma8:` is the 4x4 block 8 of a `t8` = 0 macroblock.

Example lines (real; coefficient lists shortened with `...`):

```
SLICE 1 type=P first_mb=0
MB 1 0 mbt=0 t8=1 modes=- imode=- cbp=23 qp=24 coef=luma80:1=1,2=1;luma81:0=2,7=1,9=1,...;luma82:9=-1,14=-1,16=1,18=1;cbdc0:0=1,2=-1;crdc0:0=1
MB 0 12 mbt=0 t8=0 modes=07213p2pp2747677 imode=1,8,3,1,3,3,3,3,3,3,8,5,8,7,8,8 cbp=47 qp=21 coef=luma0:1=1,2=-1,...
MB 4 27 mbt=0 t8=1 modes=2pp4 imode=2,2,2,5 cbp=47 qp=17 coef=luma80:0=-5,1=2,...
MB 3 4 mbt=skip t8=- modes=- imode=- cbp=- qp=7 coef=-
```

The first is a P_L0_16x16 with the 8x8 transform: CBP 23 = luma 7 (quadrants 0, 1, 2) + 16 * chroma 1, so three `luma8` blocks, then chroma DC. The second is Intra 4x4 (sixteen modes). The third is Intra 8x8: four mode elements (`rem` 2, predicted, predicted, `rem` 4) and four final modes.

### 5.2 Decoded frames (`--yuv`, `--yuv-nodeblock`)

Raw planar yuv420p, 8 bit, frames in **display order**, cropped as the SPS says: exactly what `ffmpeg -fps_mode passthrough -f rawvideo -pix_fmt yuv420p` writes (Chapter 5.1). `--yuv-nodeblock` is the same from a decoder that **never deblocks** (reference pictures included); it is compared with ffmpeg's `-skip_loop_filter all` and separates reconstruction bugs from deblocking bugs.

## 6. Running the harness

```
python3 test_transform8x8.py INPUT --dump your_dump.txt --yuv your.yuv [--yuv-nodeblock your_nd.yuv]
python3 test_transform8x8.py --print-expected-dump INPUT expected.txt     # to diff against yours
python3 test_transform8x8.py --make-variants DIR                          # test streams + expected dumps
python3 test_transform8x8.py --self-test                                  # takes about a minute (a few on a slow machine)
```

Give at least one of the three outputs; each is checked on its own. Exit 0 = pass; 1 = differs (per slice the first wrong macroblock and field, for `coef` the first differing block and position; for frames the first differing frame / plane / macroblock / pixel); 2 = unusable input or output.

`--make-variants DIR` writes, each with `<name>.expected.txt`: `syn_t8_1..6.264` (synthetic, from an independent CABAC encoder; 5 and 6 carry huge levels and are for the dump only; 2, 4 and 6 have `direct_8x8_inference_flag` = 0), small x264 streams `tex_*.ts` (textured, all nine Intra 8x8 modes, CABAC and CAVLC, `partitions=all`, `constrained-intra=1`, QP 40) and the 352x288 `T11_*` recipes listed in 4.

What it compares against:

1. **Pixels**: ffmpeg.
2. **The dump**: a reference parser, trusted only if every slice ends exactly where its data ends (one wrong CABAC context anywhere breaks that), and backed by a reference decoder on the same parse that matches ffmpeg bit for bit on these streams.
3. **Synthetic streams**: built to cover every Intra 8x8 mode on every availability pattern, mixed Intra 4x4 / 8x8 neighbours, the flag next to every neighbour kind, the inter flag rules, dense and sparse 8x8 blocks, random QP, chroma QP offsets and deblocking parameters; their expected dump comes from the construction.

`--self-test` also runs 50 deliberately wrong rules (covering 3.1 to 3.9) and requires each to be caught on at least one stream, which it names.

**Spoiler warning:** `spoiler_reference/` holds the reference parser, decoder and stream generator. Don't open it before your own code passes.

## 7. Reading failures

* **First wrong field is `t8`:** the conditions of 3.1 (CBP luma, sub-partition sizes, direct inference), its position (before the modes for I_NxN, after the CBP for inter), or context 399 + A + B.
* **Slice ends early / garbage right after an 8x8 macroblock:** CABAC: no `coded_block_flag` for cat 5, bases 402 / 417 / 426 and Table 9-43, position 63 never coded, the neighbour rule of 3.3. CAVLC: four 4x4 blocks, not one big one.
* **`coef` right in CABAC streams, wrong in CAVLC:** the interleave `4 * i + k`.
* **`imode` wrong while `modes` is right:** 3.8 (Min, DC for an unavailable macroblock, 2 for a non-I_NxN one, `* 4 + 1` / `* 4 + 2`, 8x8 modes seen by Intra 4x4 neighbours).
* **First wrong pixels in an Intra 8x8 macroblock, `--yuv-nodeblock` differs too:** the filter of 3.7 (corner and ends), up-right availability and substitution, or modes 2 / 3 / 7 using 16 above samples.
* **Small +-1 errors spread over a block:** dequantisation (class table, rounding offset, `qP >= 36`) or the final `(x + 32) >> 6`.
* **`--yuv-nodeblock` matches, `--yuv` does not:** deblocking: skip edges 1 and 3 of 8x8-transformed macroblocks; the bS 2 test uses the 8x8 block (also on the neighbour's side).
* **Cr wrong, Cb fine:** `second_chroma_qp_index_offset`.
* **Only multi-slice or `constrained-intra` streams wrong:** macroblock availability for the up-right and corner samples of blocks 0 and 1.

## 8. What's next

Chapter 8.6 adds **scaling matrices**: the SPS and PPS can replace the flat weight 16 in `LevelScale4x4` and `LevelScale8x8` with 4x4 and 8x8 lists (default tables, fall-back rules A and B of Table 7-2). Your dequantisation stays; only the weights change.
