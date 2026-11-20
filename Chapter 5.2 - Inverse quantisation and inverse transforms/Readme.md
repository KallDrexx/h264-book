# Chapter 5.2 - Inverse quantisation and inverse transforms

## 1. Goal and the project

Chapter 4.4 gave you, for every macroblock, lists of integer **levels** (the parsed residual coefficients). Chapter 5.1 gave you frame buffers. This chapter turns levels into **residual samples**: small signed numbers that, added to a prediction, give the picture. You build the arithmetic of spec section 8.5: scaling (inverse quantisation), the 4x4 inverse integer transform, the Intra16x16 luma DC and chroma DC transforms, the chroma QP derivation, and the final add-and-clip. Prediction itself is Chapter 5.3, so nothing here touches the frame buffer.

You will build, as pure functions with no state:

1. `LevelScale4x4` from the normAdjust table (flat weights, 16).
2. 4x4 coefficient scaling, and the 4x4 inverse transform with its `(x + 32) >> 6` rounding.
3. The Intra16x16 luma DC path (4x4 Hadamard, then scaling).
4. The chroma DC path (2x2 transform, then scaling).
5. The chroma QP mapping (Table 8-15, both chroma offsets).
6. Zig-zag placement of a level list into a 4x4 matrix, the insertion of DC values into AC blocks, and `Clip1(pred + residual)`.
7. A small program that reads a vector file of operations and writes a results file (section 5).

Verification is the Summary's "unit-test each transform on small hand-computed matrices", made mechanical: a vector file (hand-checkable cases plus random cases over every QP) and a harness with a reference implementation. The reference itself is proven against ffmpeg, so you can trust the expected numbers. The real end-to-end test comes in 5.3.

## 2. Big picture

An encoder does not store pixels. For each block it stores the **difference** between the picture and a prediction, then:

1. **Transforms** the difference block into frequency coefficients (a 4x4 integer approximation of the DCT). Smooth blocks become a few large low-frequency numbers and many near-zero ones.
2. **Quantises** the coefficients: divides by a step size and rounds. This is the lossy step, and why most coefficients become 0 and the rest become small **levels**.

A decoder reverses this in the opposite order: **scale** each level back up by the step size (inverse quantisation, the "dequantisation"), then **inverse-transform** to samples. Rounding information was thrown away, so the result is close to, not identical to, the original. Because the standard defines every shift and rounding exactly, every decoder produces the same samples.

Two properties to keep in mind:

- The step size doubles every 6 QP steps. QP (quantisation parameter) is 0..51; `QP / 6` is a power-of-two shift and `QP % 6` selects one of six multipliers. That is the whole reason for the `QP%6` / `QP/6` split.
- The transform needs only additions and shifts (no multiplications, no floating point), and the "scaling" is where the transform's non-orthonormal gains are corrected (the normAdjust table).

A macroblock's residual pipeline:

| Block kind | DC path | AC path | Output |
| --- | --- | --- | --- |
| Luma of an I_NxN macroblock (Intra 4x4) | none: DC is level 0 of each 16-level block | scale all 16 | 16 blocks of 4x4 |
| Luma of Intra16x16 | the 16 DC levels form one 4x4 matrix: Hadamard, scale | each block has 15 AC levels, scale | DC value becomes coefficient 0 of its block |
| Chroma (Cb and Cr separately) | the 4 DC levels form a 2x2 matrix: 2x2 transform, scale | each of 4 blocks has 15 AC levels, scale | DC value becomes coefficient 0 of its block |

Every block then goes through the same 4x4 inverse transform.

## 3. Concepts in detail

Notation: matrices are indexed `c_ij` with **i = row, j = column**, both 0..3 (spec 8.5 uses this order). All arithmetic is on signed integers. `>>` is an **arithmetic** right shift (floors toward minus infinity, so `-69.5` becomes `-70`, not `-69`). `<<` shifts left. 8-bit video only (bit depth 8, `QpBdOffset = 0`, so `QP'Y = QPY` and `QP'C = QPC`). Samples are 0..255.

### 3.1 Placing levels in a matrix (inverse zig-zag, 8.5.6)

The parser delivers levels in **scan order**. The frame zig-zag scan (Table 8-12) says where scan position `k` goes:

| scan k | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| row i | 0 | 0 | 1 | 2 | 1 | 0 | 0 | 1 | 2 | 3 | 3 | 2 | 1 | 2 | 3 | 3 |
| col j | 0 | 1 | 0 | 0 | 1 | 2 | 3 | 2 | 1 | 0 | 1 | 2 | 3 | 3 | 2 | 3 |

As a matrix, the scan position at each `(i, j)` is:

```
 0  1  5  6
 2  4  7 12
 3  8 11 13
 9 10 14 15
```

Level lists with 15 entries (the AC blocks of Intra16x16 and chroma) start at scan position 1: list entry `n` is scan position `n + 1`, and position 0 (the DC) is filled in separately. Field scan (for interlaced pictures) uses another table; Phase 8 adds it. For now frame scan only.

Hand check: levels 1, 2, ..., 16 in scan order give the raster matrix (row by row) `1 2 6 7 / 3 5 8 13 / 4 9 12 14 / 10 11 15 16`.

### 3.2 LevelScale4x4 (8.5.9)

`LevelScale4x4(m, i, j) = weightScale4x4(i, j) * normAdjust4x4(m, i, j)`, where `m = QP % 6`.

Without scaling matrices (Phase 8 adds them), `weightScale4x4` is 16 everywhere (spec "Flat_4x4_16"). So for now `LevelScale4x4 = 16 * normAdjust4x4`. Write it that way, as a table lookup of `weightScale(i, j)`: Phase 8 will replace the constant 16 by entries of a transmitted list, and your code then needs no other change.

`normAdjust4x4(m, i, j)` uses three values `v_m0, v_m1, v_m2`, chosen by the position class of `(i, j)`:

- `v_m0` when `i` and `j` are both even (positions (0,0), (0,2), (2,0), (2,2));
- `v_m1` when `i` and `j` are both odd ((1,1), (1,3), (3,1), (3,3));
- `v_m2` otherwise (one even, one odd).

| m | v_m0 | v_m1 | v_m2 |
| --- | --- | --- | --- |
| 0 | 10 | 16 | 13 |
| 1 | 11 | 18 | 14 |
| 2 | 13 | 20 | 16 |
| 3 | 14 | 23 | 18 |
| 4 | 16 | 25 | 20 |
| 5 | 18 | 29 | 23 |

Examples: `LevelScale4x4(0,0,0) = 160`; `(5,1,1) = 16*29 = 464`; `(3,2,1) = 16*18 = 288`. The step size grows by about 12% per QP (2^(1/6)): `LevelScale(5,0,0) = 288`, and one QP later (m wraps to 0, `QP/6` grows by 1) the effective multiplier is `160 * 2 = 320`.

Why `16` and why `normAdjust`? The basis vectors of the inverse transform (3.4) do not all have the same length: `1 1 1 1` and `1 -1 -1 1` (even index) have length 2, while `1 1/2 -1/2 -1` and `1/2 -1 1 -1/2` (odd index) have length sqrt(2.5). So a coefficient's gain depends on whether its row and column index are even or odd, and the three position classes get different multipliers to undo that. The 16 is a neutral weight; with a custom matrix, a weight of 32 would mean a coarser step for that frequency.

### 3.3 Scaling a 4x4 block (8.5.12.1)

Given the matrix `c` of levels and `qP`, produce `d`:

- For `qP >= 24`: `d_ij = (c_ij * LevelScale4x4(qP%6, i, j)) << (qP/6 - 4)`
- For `qP < 24`: `d_ij = (c_ij * LevelScale4x4(qP%6, i, j) + 2^(3 - qP/6)) >> (4 - qP/6)`

(`/` is integer division, so `qP/6` is 0..8.) The two forms are one formula: multiply by `LevelScale * 2^(qP/6)` and divide by 16, with rounding only when the division is real. At `qP = 24` the two agree (both are a left shift by 0).

**DC exception.** For Intra16x16 luma blocks and for chroma blocks, coefficient `c_00` is **not** scaled here: it has already been produced by the DC path (3.5, 3.6) in scaled form, and is used directly: `d_00 = c_00`. All other 15 are scaled as above. For Intra 4x4 luma blocks (I_NxN), all 16 including DC are scaled.

Worked example, `qP = 5` (so `qP%6 = 5`, `qP/6 = 0`; the rounding offset is `2^3 = 8`, the shift 4). LevelScale for m = 5: 288 (even,even), 464 (odd,odd), 368 (other). Levels (raster) `3 -3 2 -2 / 1 -1 0 0 / 5 -5 7 -7 / 1 1 1 1`:

- `d_00 = (3*288 + 8) >> 4 = 872 >> 4 = 54`
- `d_01 = (-3*368 + 8) >> 4 = -1096 >> 4 = -69` (floor of -68.5)
- `d_02 = (2*288 + 8) >> 4 = 36`, `d_03 = (-2*368 + 8) >> 4 = -46`, `d_10 = (1*368 + 8) >> 4 = 23`, `d_11 = (-464 + 8) >> 4 = -29`

Result `54 -69 36 -46 / 23 -29 0 0 / 90 -115 126 -161 / 23 29 23 29`. The same levels at `qP = 24` (shift left by 0): `480 -624 320 -416 / 208 -256 0 0 / 800 -1040 1120 -1456 / 208 256 208 256`.

A conforming stream never produces a `d_ij` outside the signed 16-bit range (-32768..32767). You may use 32-bit integers and not worry; just don't use 8-bit. Test vectors respect this limit.

### 3.4 The 4x4 inverse transform (8.5.12.2)

Input `d` (4x4), output residual `r`. First transform each **row**, then each **column** of the result. The same 1-D butterfly is used for both. For four inputs `a0 a1 a2 a3`:

```
e0 = a0 + a2
e1 = a0 - a2
e2 = (a1 >> 1) - a3
e3 = a1 + (a3 >> 1)
out0 = e0 + e3
out1 = e1 + e2
out2 = e1 - e2
out3 = e0 - e3
```

Procedure: for each row `i` apply the butterfly to `(d_i0, d_i1, d_i2, d_i3)` giving row `f_i0..f_i3`. Then for each column `j` apply it to `(f_0j, f_1j, f_2j, f_3j)` giving `h_0j..h_3j`. Finally `r_ij = (h_ij + 32) >> 6`.

The `>> 1` on `a1` and `a3` is the "half" coefficients of the integer transform. Do those shifts on the intermediate integers (floor for negative numbers). The order (rows, then columns) matters: the early `>>1` truncation makes the result differ slightly if you swap them.

Intermediate values (the `f` and `h`) must also fit in 16 bits for a conforming stream.

Worked examples (`d` listed row by row):

- DC only, `d_00 = 64`: row 0 becomes `64 64 64 64`, then each column is `(64, 0, 0, 0)` and gives 64 four times. Every `r = (64 + 32) >> 6 = 1`. So `r` is all 1.
- `d_01 = 323`, all others 0: row 0 gives `f = 323, 161, -161, -323` (since `161 = 323 >> 1`); other rows are 0; each column holds one nonzero entry and copies it down. `r_ij = (f + 32) >> 6`: `355>>6 = 5`, `193>>6 = 3`, `-129>>6 = -3`, `-291>>6 = -5`. Every row of `r` is `5 3 -3 -5`.
- `d_00 = 64, d_01 = 64`: row 0 has `e0 = 64, e1 = 64, e2 = 32, e3 = 64`, so `f = 128, 96, 32, 0`; rows below are 0. `r` rows are all `(128+32)>>6, (96+32)>>6, (32+32)>>6, 32>>6 = 2 2 1 0`.

Putting scaling and transform together, `qP = 28`, a single level 1 at `c_00` (Intra 4x4 luma): `LevelScale(4,0,0) = 256`; `qP >= 24` so `d_00 = 256 << (4 - 4) = 256`; the transform gives all `256`, and `(256+32)>>6 = 4`. All 16 residuals are 4. At `qP = 12` (`qP%6 = 0`, `qP/6 = 2`): `LS = 160`, `d_00 = (160 + 2^1) >> 2 = 40`, residual `(40+32)>>6 = 1` everywhere.

A fuller example, `qP = 26` (`m = 2`, `qP/6 = 4`, so a left shift by 0; `LS` = 208 / 320 / 256): levels `c_00 = 4, c_01 = 1, c_02 = 2, c_10 = -1`, rest 0. Scaling: `d_00 = 832, d_01 = 256, d_02 = 416, d_10 = -256`. After the transform the residual is `20 5 1 12 / 22 7 3 14 / 26 11 7 18 / 28 13 9 20`. (Use this to test your pipeline end to end; it is also a vector.)

### 3.5 Intra16x16 luma DC (8.5.10)

An Intra16x16 macroblock transmits the 16 DC coefficients (one per 4x4 luma block) as a separate residual block of 16 levels, because DCs of neighbouring blocks are strongly correlated. Treat them as a 4x4 matrix:

1. Place the 16 DC levels with the **same** zig-zag (3.1): matrix `c` (4x4).
2. Transform: `f = A * c * A` with the symmetric Hadamard matrix

```
      1  1  1  1
A =   1  1 -1 -1
      1 -1 -1  1
      1 -1  1 -1
```

   (plain matrix products, no shifts; each `f_ij` is a sum or difference of all 16 `c`).
3. Scale each `f_ij` with `qP = QP'Y` and `LS = LevelScale4x4(qP%6, 0, 0)` (always the (0,0) class: 160, 176, 208, 224, 256, 288 for m = 0..5):
   - `qP >= 36`: `dcY_ij = (f_ij * LS) << (qP/6 - 6)`
   - `qP < 36`: `dcY_ij = (f_ij * LS + 2^(5 - qP/6)) >> (6 - qP/6)`
4. `dcY_ij` is the DC value of the 4x4 luma block at **block row i, block column j** of the macroblock (a raster position: spec Figure 8-6). In `luma4x4BlkIdx` terms (chapter 5.1's table), block `b` with position `(x, y)` in units of 4 samples gets `dcY[y][x]`. This is not the zig-zag order and not the block index order.

Each luma block then gets `c_00 = dcY[y][x]` (marked "already scaled", 3.3), its 15 AC levels placed at scan positions 1..15, and is inverse-transformed. A macroblock whose coded block pattern says "no luma AC" (CBP luma = 0) still has the DC block and still needs the transform on every block: every block's AC is simply zero.

Worked example: `qP = 30`. `LS = LevelScale(0,0,0) = 160`, `qP/6 = 5`, so `dcY = (f*160 + 1) >> 1`. One level `c_00 = 1`: every `f = 1`, every `dcY = 161 >> 1 = 80`. At `qP = 36`: `qP/6 = 6`, shift 0, `dcY = 160`. With only `c_01 = 1` (scan position 1) at `qP = 30`: every row of `f` is `1 1 -1 -1` (a horizontal frequency in `c` gives a horizontal pattern), so `dcY` rows are `80 80 -80 -80`: the left two block columns get `+80`, the right two `-80`.

### 3.6 Chroma DC (8.5.11) and chroma QP

For 4:2:0 each chroma component of a macroblock has four 4x4 blocks in a 2x2 arrangement, so the four DC levels form a 2x2 matrix, in plain raster order (no zig-zag): `c = [[c0, c1], [c2, c3]]` where `ck` is the DC of `chroma4x4BlkIdx` k (block 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right).

1. `f = A2 * c * A2` with `A2 = [[1, 1], [1, -1]]`.
2. `dcC_ij = ((f_ij * LS) << (qP/6)) >> 5` with `LS = LevelScale4x4(qP%6, 0, 0)` and `qP = QP'c` of this component (so Cb and Cr can differ). The order matters: shift left first, then right by 5 (no rounding offset).
3. `dcC_ij` is the DC (`c_00`, already scaled) of chroma block `2*i + j`.

Chroma AC (15 levels per block, scan positions 1..15) is scaled with the same `QP'c` as in 3.3 and transformed as in 3.4.

Example: `qP = 30`, levels `c = 1 0 / 0 0`: `f` is all 1, `LS = 160`, `qP/6 = 5`: `(160 << 5) >> 5 = 160` for every block. With levels `1 1 / 1 1`: `f = 4 0 / 0 0` and `dcC = 640, 0, 0, 0`. Another: `qP = 20` (`m = 2`, `LS = 208`, shift 3): `c = 5 -3 / 2 -1`. `A2*c = [[7, -4], [3, -2]]`, times `A2` gives `f = [[3, 11], [1, 5]]`. `(3*208 << 3) >> 5 = 156`, similarly `572, 52, 260`.

**Chroma QP (8.5.8 / Table 8-15).** The chroma `qP` is derived from the macroblock's luma `QPY` and a PPS offset:

- `qPOffset = chroma_qp_index_offset` for Cb, `second_chroma_qp_index_offset` for Cr. (Without the High-profile extension of the PPS, `second_chroma_qp_index_offset` is inferred equal to `chroma_qp_index_offset`.) Both are -12..12.
- `qPI = Clip3(-QpBdOffsetC, 51, QPY + qPOffset)`. For 8-bit this is `Clip3(0, 51, QPY + qPOffset)`.
- `QPC` = `qPI` when `qPI < 30`; otherwise from the table:

| qPI | 30 | 31 | 32 | 33 | 34 | 35 | 36 | 37 | 38 | 39 | 40 | 41 | 42 | 43 | 44 | 45 | 46 | 47 | 48 | 49 | 50 | 51 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| QPC | 29 | 30 | 31 | 32 | 32 | 33 | 34 | 34 | 35 | 35 | 36 | 36 | 37 | 37 | 37 | 38 | 38 | 38 | 39 | 39 | 39 | 39 |

(The chroma QP grows slower than luma above 29, so the chroma gets finer quantisation at high QP.) `QP'c = QPC` for 8-bit. Examples: `QPY = 40, off 0` gives 36; `QPY = 51, off 12` gives `qPI = 51` giving 39; `QPY = 5, off -12` gives `qPI = 0` (clipped) giving 0.

Which QP does each block use? Intra 4x4 luma and all Intra16x16 luma work (DC and AC) with `QP'Y = QPY`; Cb with `QP'c` from `chroma_qp_index_offset`; Cr with `QP'c` from `second_chroma_qp_index_offset`. `QPY` is the macroblock's QP after mb_qp_delta (Chapter 4.2).

### 3.7 Sample reconstruction (8.5.14)

For every sample, `u = Clip1(pred + r)` with `Clip1(x) = min(max(x, 0), 255)`. `pred` comes from intra prediction (5.3), `r` from this chapter. `r` can be far outside -255..255 (up to about ±512), so `pred + r` often leaves 0..255 at sharp edges. The clip is part of the standard, and bit-exact output depends on it.

## 4. The project: exactly what to build

Write these functions (names are yours):

| Function | Input | Output |
| --- | --- | --- |
| `level_scale(m, i, j)` | `m` 0..5 | `16 * normAdjust4x4` |
| `chroma_qp(qpy, offset)` | luma QP, one of the two offsets | `QPC` |
| `inverse_zigzag(levels, first)` | 16 or 15 levels, `first` = 0 or 1 | 4x4 matrix |
| `scale4x4(c, qP, dc_pre_scaled)` | matrix, QP | matrix `d` |
| `idct4x4(d)` | matrix | residual matrix `r` (after `(x+32)>>6`) |
| `luma_dc(c, qP)` | 4x4 DC matrix | 4x4 `dcY` |
| `chroma_dc(c, qP)` | 2x2 matrix | 2x2 `dcC` |
| `clip_add(pred, r)` | | clipped samples |

And two macroblock-level compositions, since this is what 5.3 will call:

1. `i16_luma_residual(qP, dc_levels[16], ac_levels[16][15])`: 16 AC lists in `luma4x4BlkIdx` order; returns the 16x16 residual in raster order (use the block position table of 5.1 to place each 4x4 result).
2. `chroma_residual(QP'c, dc_levels[4], ac_levels[4][15])`: returns the 8x8 residual of one component.

I_NxN luma blocks use `inverse_zigzag` + `scale4x4(dc_pre_scaled = false)` + `idct4x4` per block.

Keep these as plain functions on arrays: in 5.3 you will call them once per block/macroblock. Use at least 32-bit integers; the multiplications `level * LevelScale << shift` reach beyond 16 bits (levels up to about 2^15 in the vectors).

Verify (from the Summary): each function against hand-computed matrices. The vector file below contains the examples of section 3 as its first vectors, followed by hundreds of random cases.

## 5. Harness input format

The harness gives you a **vector file**; your program reads it and writes a **results file**.

```
python3 test_transforms.py --write-vectors vectors.txt
```

### Vector file

Plain text, one vector per line, numbers in decimal separated by single spaces. Blank lines and lines starting with `#` are comments: skip them, **write no result line for them**. Matrices are always listed in **raster order** (row 0 left to right, then row 1, ...), except where "scan order" is stated. All values fit in 32-bit signed integers. Every scaled coefficient and transform intermediate (`d`, `dcY`, `dcC`, the `f` and `h` of 3.4) stays within signed 16 bits, as 8.5.12 requires of a conforming stream, but products such as `level * LevelScale` can exceed 16 bits, so compute in 32 bits.

| Line | Fields | Result (integers on one line) |
| --- | --- | --- |
| `LEVELSCALE <m> <i> <j>` | m 0..5 | 1 number: `LevelScale4x4(m, i, j)` |
| `CHROMAQP <qpy> <offset>` | qpy 0..51, offset -12..12 | 1 number: `QPC` (8-bit, so also `QP'c`) |
| `CHROMAQPS <qpy> <cb_offset> <cr_offset>` | `chroma_qp_index_offset`, then `second_chroma_qp_index_offset` | 2 numbers: `QP'c` of Cb, then of Cr |
| `ZIGZAG4X4 levels <16>` | levels in scan order | 16 numbers: the matrix `c`, raster |
| `SCALE4X4 qp <qp> coeffs <16> [dc_pre_scaled]` | matrix `c`, raster | 16 numbers: `d` (3.3) |
| `IDCT4X4 d <16>` | matrix `d`, raster | 16 numbers: `r` after `(x+32)>>6` |
| `RESIDUAL4X4 qp <qp> coeffs <16> [dc_pre_scaled]` | matrix `c`, raster | 16 numbers: `r` (scale then transform) |
| `BLOCK4X4 qp <qp> levels <16>` | 16 levels in **scan order**; all 16 scaled, as for I_NxN luma | 16 numbers: `r` (zig-zag + scale + transform) |
| `LUMADC qp <qp> coeffs <16>` | matrix `c` of DC levels, raster | 16 numbers: `dcY`, raster (row i, column j = block row i, column j) |
| `CHROMADC qp <qpc> coeffs <4>` | `c0 c1 c2 c3` | 4 numbers: `dcC`, raster `00 01 10 11` |
| `RECON4X4 pred <16> res <16>` | | 16 numbers: `Clip1(pred + res)` |
| `I16RESID qp <qp> dc <16> ac <240>` | 16 DC levels in scan order; then 16 blocks of 15 AC levels (scan positions 1..15), blocks in `luma4x4BlkIdx` order | 256 numbers: the 16x16 residual, raster (row 0 of the macroblock first) |
| `CHROMARESID qp <qpc> dc <4> ac <60>` | 4 DC levels (`chroma4x4BlkIdx` order); 4 blocks of 15 AC levels | 64 numbers: the 8x8 residual, raster |

`dc_pre_scaled` (optional last word on `SCALE4X4` and `RESIDUAL4X4`) means `c_00` is already scaled: copy it to `d_00` unscaled. In `qp` fields, "qp" is `qP` as used by the formula (`QP'Y`, or `QP'c` for chroma lines); there is no further offset applied in any line except `CHROMAQP`.

Example lines:

```
LEVELSCALE 5 1 1
CHROMAQP 40 0
CHROMAQPS 30 0 6
RESIDUAL4X4 qp 28 coeffs 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
IDCT4X4 d 0 323 0 0 0 0 0 0 0 0 0 0 0 0 0 0
CHROMADC qp 20 coeffs 5 -3 2 -1
```

### Results file

One line per vector, same order, integers separated by single spaces, no other text:

```
464
36
29 34
4 4 4 4 4 4 4 4 4 4 4 4 4 4 4 4
5 3 -3 -5 5 3 -3 -5 5 3 -3 -5 5 3 -3 -5
156 572 52 260
```

Blank lines and `#` lines in the results are ignored. Negative numbers use `-`. Anything that is not an integer is a malformed file (exit 2).

## 6. Running the harness

```
python3 test_transforms.py --write-vectors vectors.txt
<your program> vectors.txt results.txt
python3 test_transforms.py --vectors vectors.txt results.txt
```

The expected result of every vector is recomputed from `transref.py` (the reference, in this folder). The harness reports vector index, the vector text, the first element that differs (with its row and column) and expected vs actual, then the first 10 failures. Exit code 0 = pass, 1 = failures (or wrong number of result lines), 2 = missing or malformed file.

Optional: `--seed N` with `--write-vectors` gives a different random set (default 52).

**Why you can trust the expected values.** `python3 test_transforms.py --ffmpeg-check` (also part of `--self-test`) does not look at your program at all. It builds a synthetic H.264 stream (`recongen.py`: High profile, CAVLC, 4x3 macroblocks per picture, every macroblock the only macroblock of its slice so all neighbours are unavailable and the prediction is trivial, deblocking off), with known coefficient arrays at QP 0..51 (each value several times), random `chroma_qp_index_offset` / `second_chroma_qp_index_offset` per picture, Intra16x16 and I_NxN macroblocks, small and large levels, clipping included. It decodes the stream with ffmpeg and checks that the reference pipeline reproduces every one of ffmpeg's 384 samples per macroblock exactly. What this covers: 4x4 scaling and transform (both rounding branches), the luma DC Hadamard and chroma DC paths, DC insertion, both chroma QP mappings, rounding and `Clip1`. What it cannot cover: scaling matrices, 8x8 transform, higher bit depths (later phases) and anything involving neighbouring-macroblock prediction (Chapter 5.3, whose real streams are the true test of your integration). The vector-only ops (`LEVELSCALE`, `CHROMAQP`, `CHROMAQPS`, `ZIGZAG4X4`, `IDCT4X4` standalone, `RECON4X4`) are exercised by the same reference functions that the ffmpeg check validates, plus hand-computed values in `--self-test`.

`python3 test_transforms.py --self-test` checks the harness: hand-computed reference values, the ffmpeg check, four deliberately broken references that the ffmpeg check must reject (a wrong normAdjust value, a wrong Table 8-15 entry, a wrong rounding, Cb and Cr offsets swapped), and the vector protocol (correct results pass, a one-number error in every op type fails, a missing line fails, malformed output is exit 2).

## 7. Reading failures / debugging tips

Test from the smallest operation up: `LEVELSCALE`, `CHROMAQP`, `ZIGZAG4X4`, `SCALE4X4`, `IDCT4X4`, then the compositions. The first failing vector tells you which stage is wrong.

- `LEVELSCALE` wrong: check the position classes (odd,odd is `v_m1`; mixed parity is `v_m2`) and the factor 16.
- `SCALE4X4` wrong only at low qp (below 24) and only by 1: the rounding offset `2^(3 - qP/6)`, or a logical instead of arithmetic shift on negatives.
- `SCALE4X4` wrong for `qP >= 24`: shift `qP/6 - 4`, not `qP/6`.
- `IDCT4X4` off by small amounts in some positions: the `>> 1` on the wrong inputs (`a1` and `a3` in the odd part), or columns before rows, or rounding before the second pass (round only at the very end, `(x + 32) >> 6`).
- Negative values off by one: you used division (`/` truncates toward zero in C and Java) instead of an arithmetic shift.
- `LUMADC` wrong at `qP >= 36` only: `<< (qP/6 - 6)` branch. Wrong everywhere: Hadamard sign pattern, or applying scaling before the transform.
- `CHROMADC`: shift left by `qP/6` first, then right by 5; do not use a rounding offset.
- `I16RESID` wrong but `LUMADC` and `RESIDUAL4X4` right: block order (use `luma4x4BlkIdx` for the AC lists, and `dcY[row][col]` by the block's position), the DC not marked pre-scaled, or the AC list starting at scan position 0 instead of 1.
- `CHROMARESID`: the same, and DC block `2*i + j`.
- `CHROMAQPS` wrong but `CHROMAQP` right: Cb and Cr offsets swapped (Cb uses `chroma_qp_index_offset`, Cr `second_chroma_qp_index_offset`).
- Everything passes here but 5.3 fails: a `QPY` that forgot mb_qp_delta, or a chroma block given the luma QP.

## 8. What's next

Chapter 5.3 adds Intra16x16 and chroma prediction: `u = Clip1(pred + r)` using your residual functions, and T1 decodes byte-identical for the first time.
