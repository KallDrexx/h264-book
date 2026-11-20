# Chapter 7.4 - Luma and chroma interpolation and inter residual

> **SPOILER WARNING - do not open `spoiler_reference/` until your own decoder passes.** Build your decoder from this text alone;
> the harness uses those files for its diagnosis, so you never need to read them.
> `spoiler_reference/interp_ref.py` (interpolation) and `spoiler_reference/interdec.py` (the whole P decoder) are complete solutions of this
> chapter; `intergen.py` is the generator of the synthetic streams. `test_inter.py` is plumbing (comparison, diagnosis, self-test) and is safe to read.
> The other files in that folder (`mvpred_ref.py`, `intraref.py`, `intra4ref.py`, `transref.py`) are copies of the solutions of Chapters 7.3 and 5.x.

## 1. Goal and the project

This is the first chapter in which a P picture turns into pixels. You already know, for every inter macroblock, its partitions, their
reference pictures (7.2) and their final motion vectors (7.3). Now you:

1. **fetch the predicted samples** from the reference picture at the position the vector points to, including fractions of a sample
   (luma in quarters, chroma in eighths) and including positions outside the picture;
2. **add the residual** (the same transform path as Phase 5, but with the inter rules), and
3. **store the finished picture** so that later pictures can use it as a reference.

Milestone: your decoder writes a YUV file that is **byte-identical to ffmpeg's** for the whole of `T5` (P 16x16 only, one reference) and then for
`T6_nodeblock` (every partition size, four references, intra macroblocks inside P pictures). Deblocking stays off in this chapter; it comes back
in 7.5.

## 2. Big picture

A motion vector says "this block looks like the block of the reference picture over there". If "over there" is a whole number of samples, you copy.
Real motion is rarely that tidy, so H.264 lets vectors point *between* samples, and the decoder invents the missing samples by interpolation:

* **Luma** has 1/4-sample precision. First the half-way samples are built with a 6-tap filter that looks at three samples on each side. The
  quarter-way samples are then simply the average of two neighbouring integer/half samples.
* **Chroma** has 1/8-sample precision (the vector is the same number, but chroma has half the resolution) and uses plain bilinear
  interpolation of the four surrounding samples.
* A vector may point **outside** the picture. The reference picture behaves as if its edge samples were repeated forever in every direction.

The residual is the correction the encoder sends on top of the prediction. For inter macroblocks it is decoded with the Phase 5 machinery
(dequantise, inverse transform, add, clip). The two things that differ from intra: every luma block carries all 16 coefficients (there is no separate
DC transform), and the prediction comes from another picture, so the order of work is "predict the whole macroblock, then add".

## 3. Concepts in detail

### 3.1 Units and the vector split

A luma vector `(mvx, mvy)` is in **quarter luma samples**. For a prediction block whose top-left luma sample is (x, y) in the picture:

```
xInt  = x + (mvx >> 2)      xFrac = mvx & 3      (0..3)
yInt  = y + (mvy >> 2)      yFrac = mvy & 3
```

`>>` is an arithmetic shift (rounds toward minus infinity) and `&` works on the two's-complement value, so negative vectors work out: mvx = -9 is
`-3 * 4 + 3`, i.e. three samples left plus a three-quarter fraction. Do not use division or `%` on negative numbers.

### 3.2 Reference sample fetch and clamping (8.4.2.2)

Whenever the algorithm reads a reference sample at integer position (x, y), it actually reads position (x', y') with

```
x' = Clip3(0, PicWidthInSamples - 1, x)        y' = Clip3(0, PicHeightInSamples - 1, y)        Clip3(lo, hi, v) = min(max(v, lo), hi)
```

This is the only mechanism for "outside the picture" and it is applied to **every** sample read, including the six taps of the filter and the
neighbours used for averaging. Easiest implementation: clamp each coordinate as you read. Three details:

* The size is the **decoded picture size** (macroblocks x 16 by macroblocks x 16), not the cropped output size. Cropping happens only when you write the file.
* For chroma the limits are the **chroma plane** size (half the luma size).
* The reference is the picture exactly as stored after its decoding (here: the reconstruction; from 7.5 on: after deblocking), never the picture that is being decoded.

### 3.3 Luma sample interpolation (8.4.2.2.1)

Name the integer luma samples around the target position like the standard does. G is the integer sample at (xInt, yInt), H is its right neighbour,
M the one below, N the one diagonally below-right. The fractional samples live between them:

```
 G   a   b   c   H
 d   e   f   g
 h   i   j   k   m
 n   p   q   r
 M       s       N
```

* **b** is the half sample between G and H (horizontal), **h** the half sample between G and M (vertical), **j** the centre of the four, **m** the vertical
  half sample between H and N, **s** the horizontal half sample between M and N.
* The other letters are the quarter positions.

**Half samples b and h** use the 6-tap filter with weights (1, -5, 20, 20, -5, 1):

```
b1 = E - 5F + 20G + 20H - 5I + J       (E, F, G, H, I, J = the six samples of row y at x-2 .. x+3; G and H are in the middle)
h1 = A - 5C + 20G + 20M - 5R + T       (A, C, G, M, R, T = the six samples of column x at y-2 .. y+3; G and M are in the middle)
b  = Clip1((b1 + 16) >> 5)             h = Clip1((h1 + 16) >> 5)           Clip1(v) = min(max(v, 0), 255)
```

`b1` and `h1` are the **intermediate** (unrounded) values. The filter weights add up to 32, hence the shift by 5. `s` and `m` are the same
formulas moved one sample down / right. Note the clip: a filter with negative weights overshoots on sharp edges, so `b1` can be negative or above 255 * 32.

**The centre sample j** is the half sample in both directions. It is built from the **unrounded** intermediates, not from clipped `b`/`h`:

```
j1 = filter(1,-5,20,20,-5,1) over six vertically adjacent b1 values (rows y-2 .. y+3, same column)
   = the same filter over six horizontally adjacent h1 values  (both give the identical number)
j  = Clip1((j1 + 512) >> 10)             (32 * 32 = 1024 total filter gain)
```

If you round and clip `b` first and then filter, you get slightly different samples (and the result does not match ffmpeg).

**Quarter positions** are the average of the two nearest samples (integer or half), rounding up: `avg(p, q) = (p + q + 1) >> 1`. The full table
(xFrac across, yFrac down):

| yFrac \ xFrac | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| **0** | G | a = avg(G, b) | b | c = avg(H, b) |
| **1** | d = avg(G, h) | e = avg(b, h) | f = avg(b, j) | g = avg(b, m) |
| **2** | h | i = avg(h, j) | j | k = avg(j, m) |
| **3** | n = avg(M, h) | p = avg(h, s) | q = avg(j, s) | r = avg(m, s) |

Reading the table: the *four diagonal* positions e, g, p, r average **two half samples** (never an integer sample). e sits between b (above it) and h (left of it);
g between b and m (right of it); p between h and s (below it); r between m and s. The positions a, c, d, n average an integer sample with the half sample next
to it; f, i, k, q average j with the neighbouring half sample.

Implementation hint: compute only what the position needs. For a W x H block, fetching a (W+5) x (H+5) window around the integer position (with clamping)
once and filtering inside it is faster and simpler than clamping at every tap.

### 3.4 Chroma sample interpolation (8.4.2.2.2)

For 4:2:0 the chroma vector is the luma vector, read in **eighths of a chroma sample** (chroma has half the resolution, so a quarter luma sample is an
eighth chroma sample). For a chroma block at (xC, yC) = (x / 2, y / 2) of the chroma plane:

```
xIntC = xC + (mvx >> 3)    xFracC = mvx & 7           (0..7)
yIntC = yC + (mvy >> 3)    yFracC = mvy & 7
A = C(xIntC, yIntC)      B = C(xIntC + 1, yIntC)
Cc = C(xIntC, yIntC + 1) D = C(xIntC + 1, yIntC + 1)        (all four coordinates clamped to the chroma plane)
pred = ((8 - xFracC)(8 - yFracC) A + xFracC (8 - yFracC) B + (8 - xFracC) yFracC Cc + xFracC yFracC D + 32) >> 6
```

The weights add to 64. Nothing is clipped (a weighted average cannot leave 0..255). Cb and Cr are predicted independently with the same vector and
reference. (For frame pictures there is no vertical chroma offset; that exists only for fields, Phase 10.)

A luma partition w x h gets a chroma block of (w/2) x (h/2): a 16x16 partition has an 8x8 chroma block, a 4x4 sub-partition a 2x2 block.

### 3.5 Block-wise prediction

Predict each prediction block (macroblock partition, or sub-macroblock partition of P_8x8) separately with *its own* reference picture
(`RefPicList0[ref_idx]`) and *its own* vector. P_Skip is one 16x16 block with ref_idx 0 and the inferred vector (no residual). The 256 luma and 2 x 64 chroma
predicted samples of the macroblock are collected in a macroblock-sized buffer.

### 3.6 The inter residual

The syntax gave you (Chapter 7.1) `coded_block_pattern` (inter column), an optional `mb_qp_delta` and the coefficient levels. Decoding:

* **QP.** `mb_qp_delta` is present only when CBP != 0; it changes the running QP_Y exactly as in Phase 5 (7.4.5 wrap-around). Skipped macroblocks and
  macroblocks with CBP = 0 keep the running QP_Y and have no residual.
* **Luma.** CBP bit q (q = 0..3) covers the four 4x4 blocks `luma4x4BlkIdx` 4q .. 4q+3 of the 8x8 quadrant q (the numbering of Phase 4/5). A block of a quadrant whose bit is
  0 has residual zero. A coded block has **16 coefficients, DC included**, in zig-zag scan order. All 16 are scaled with the ordinary 4x4 rule of
  Chapter 5.2 (LevelScale(qP % 6, i, j), shifted by qP / 6); the DC coefficient is **not** special (the Intra16x16 DC transform of Chapter 5.2 does
  not exist for inter macroblocks). Then the 4x4 inverse transform and `(x + 32) >> 6`.
* **Chroma.** CBP chroma = 0: none; 1: only the two 2x2 DC blocks (Cb, Cr), AC zero; 2: DC and the 15 AC coefficients of each of the 4 blocks per plane.
  Exactly the Phase 5 chroma path with QP_C derived from QP_Y + `chroma_qp_index_offset` (Table 8-15).
* **Reconstruction.** `sample = Clip1(pred + residual)` for every luma and chroma sample. With no residual `sample = pred`. The 16-bit range limits of
  8.5.12 are respected by conforming streams; plain 32-bit integers are enough.
* **Intra macroblocks inside P slices** use the Phase 5 intra decoding unchanged, reading neighbouring samples of the *current* picture (inter
  neighbours are fine to read) - except that with `constrained_intra_pred_flag = 1` neighbours that are inter macroblocks count as **unavailable** for
  intra sample prediction, and for the Intra4x4 mode prediction (an unavailable neighbour makes the predicted mode 2). Without the flag an inter
  neighbour is available and simply counts as mode 2 (DC) in the Intra4x4 mode prediction, like an Intra16x16 neighbour in Phase 5.

### 3.7 Order of work, storage, output order

Per macroblock: predict (all partitions) -> add residual -> write into the new frame buffer. Allocate a **new** frame buffer for every picture: the
reference pictures must stay untouched while the current picture is built. When the last macroblock of the picture is done, run the marking process of
7.2 (the finished picture becomes a short-term reference, the sliding window may drop the oldest). Deblocking (7.5) will run between "last macroblock" and
"store", and what you store is the deblocked picture. In this chapter nothing is filtered, so stored = reconstructed.

P-only streams (T5, T6) have output order = decoding order, so you write each picture to the file as soon as it is complete.

### 3.8 Worked examples (produced by the reference)

A 12x12 reference plane with `P[y][x] = (37 x + 91 y + 13 x y) mod 200 + 20`; we predict a 1x1 block at (4, 4), where G = P[4][4] = 140 and H = P[4][5] = 29.

* b: the six taps on row 4 (x = 2..7) give `b1 = 2904`; `b = Clip1((2904 + 16) >> 5) = 91`. Vector (2, 0) -> **91**.
* a = avg(G, b) = (140 + 91 + 1) >> 1 = **116** (vector (1, 0)); c = avg(H, b) = (29 + 91 + 1) >> 1 = **60** (vector (3, 0)).
* h (vertical half sample) = 112, so e = avg(b, h) = (91 + 112 + 1) >> 1 = **102** (vector (1, 1)).
* j: the b1 values of rows 2..7 at that column are 1736, 4120, 2904, 4288, 2872, 5256, so
  `j1 = 1736 - 5*4120 + 20*2904 + 20*4288 - 5*2872 + 5256 = 115872` and `j = Clip1((115872 + 512) >> 10) = 113`. Vector (2, 2) -> **113**.
* Chroma: an 8x8 plane with `C[y][x] = (21 x + 55 y) mod 180 + 30`, a 2x2 block at (1, 1), vector (5, 3): xInt = 1 + (5 >> 3) = 1, xFrac = 5, yFrac = 3;
  A, B, Cc, D = 106, 127, 161, 182; first sample = `(3*5*106 + 5*5*127 + 3*3*161 + 5*3*182 + 32) >> 6 = 8976 >> 6` = **140**; the block is [[140, 161], [127, 148]].
* Clamping: the 2x2 block at (0, 0) with vector (-9, -6) -> xInt = -3, xFrac = 3, yInt = -2, yFrac = 2: every tap that falls left of column 0 or above
  row 0 reads column 0 / row 0. The result here is [[23, 24], [11, 12]].

You can check your own implementation on exactly these numbers before you run any stream.

### 3.9 Things that surprise people

* Rounding `b` and `h` and then filtering them again for `j` is wrong; j needs the unrounded intermediates.
* The two samples averaged for a diagonal position are *both half samples*; for the "straight" quarter positions one of the two is an integer sample.
* Clamp every tap, not just the block corner: a block that is mostly inside but whose filter window reaches 2-3 samples outside needs clamping too.
* Clamping limit is `width - 1`, using the decoded (macroblock-multiple) size.
* The chroma vector is not halved: it is the same number with a different unit (`>> 3`, `& 7`).
* Different partitions of one macroblock may use different reference pictures; do not look up the reference once per macroblock.
* The DC coefficient of an inter 4x4 block is scaled like all others.
* A P_Skip macroblock has a prediction and no residual, and does not change the running QP.

## 4. The project: exactly what to build

Extend your decoder so that it decodes **I and P pictures** (CAVLC, 8-bit 4:2:0, frames, no deblocking, no weighted prediction) and writes every
decoded picture to a YUV file:

1. For every inter prediction block: reference picture (7.2 list), vector (7.3), luma interpolation (3.3), chroma interpolation (3.4), into a prediction buffer.
2. The inter residual (3.6) added with clipping; P_Skip = prediction only.
3. Intra macroblocks in P slices through your Phase 5 path (with the `constrained_intra_pred_flag` rule).
4. Store the finished picture as a reference (7.2 marking); write it to the output in decoding order.

**Verify** (milestones, in this order):

* Unit-check your interpolation on the numbers of 3.8.
* `T5` (P 16x16 only, one reference): byte-identical for **all** frames. Frame 0 is the I picture decoded by your Phase 5 path (deblocking is off in T5).
* `T6_nodeblock` (T6's options plus `no-deblock=1`; added to `common/testfiles.py` in this chapter): byte-identical. It uses every partition size from 7.3, four references,
  intra macroblocks inside P pictures and lots of fractional vectors.
* The synthetic streams of `--make-variants` (every one of the 16 luma positions, vectors far outside the picture, residual of every shape).

Unrelated streams that need deblocking, B pictures, CABAC, weighted prediction... are refused by the diagnosis (not by the comparison, which always runs).

### Which recipes can be decoded exactly after each Phase 7 chapter

This is the book's one copy of this table; later Phase 7 chapters refer to it.

| recipe | after 7.4 | after 7.5 | 7.6 | 7.7 | 7.8 | 7.9 |
|---|---|---|---|---|---|---|
| T1, T2, T4_nodeblock, `default_intra` (intra, no deblock) | yes (Phase 5) | yes | yes | yes | yes | yes |
| T3, T3_dm3, T3_dp3, T4 (intra, deblocking) | yes (Phase 6) | yes | yes | yes | yes | yes |
| T5 (P16x16, no deblock) | **yes** | yes | yes | yes | yes | yes |
| T6_nodeblock (all partitions, ref=4, no deblock) | **yes** | yes | yes | yes | yes | yes |
| T6 (deblocking on) | no | **yes - first complete Baseline decoder** | yes | yes | yes | yes |
| T6 with `ref=16`, reordering / MMCO variants | no | plain `ref=16` streams without reordering (7.5's `d_ref16`) | **yes** | yes | yes | yes |
| T7 (weighted P, fade) | no | no | no | **yes** | yes | yes |
| T8 (B, no direct) | no | no | no | no | **yes** | yes |
| T9_spatial, T9_temporal (B pyramid, direct, implicit weights) | no | no | no | no | no | **yes** |
| `default` (x264 Main CAVLC, I/P/B, weighted P) | no | no | no | no | no | yes (with Phase 7's sort-by-POC output) |
| T10 (CABAC), T11 (High), T12 (interlaced) | Phase 8 / 10 | | | | | |

("yes" for a column means the recipe decodes byte-identically with a decoder that has that chapter's features. Output order of B files is the shortcut of Summary Phase 7: frames sorted by POC within each IDR period, until Chapter 9.1's bumping replaces it.)

## 5. Harness input format

Your program is run as `your_decoder <input.ts|input.264> <output.yuv>`. The output is the **Chapter 5.1 format**: all frames in output order, 8-bit planar
yuv420p (all Y, then all Cb, then all Cr), cropped as the SPS says, no padding and no headers. For T5 and T6 that is 352 x 288 and 50 frames, i.e.
7 603 200 bytes. There is no other text format in this chapter: the pixels are the whole interface.

## 6. Running the harness

```
python3 test_inter.py ../.cache/T5.ts my_t5.yuv
python3 test_inter.py ../.cache/T6_nodeblock.ts my_t6.yuv
python3 test_inter.py --make-variants variants/        # x264 variants + the synthetic streams (.264) + recipes T5, T6_nodeblock
python3 test_inter.py --self-test                      # about one to two minutes
```

(`python3 -m common.testfiles T5 T6_nodeblock` builds the recipes in `.cache/`.) The comparison is with ffmpeg's decode (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`).
Exit 0 = identical, 1 = different, 2 = unusable (missing file, ...).

The **synthetic streams** (`synth_frac`, `synth_edge`, `synth_resid`, `synth_multiref`, `synth_sub`, `synth_cip`; 96x64, 12-14 pictures) are made by a bit-writer
generator: the IDR is built from I_PCM macroblocks of noise (full range, mild, 0/255 checkerboards, ramps), the P pictures use all partition shapes with a
chosen fractional position cycling through all 16 (xFrac, yFrac), targets inside the picture, touching an edge, half outside and far outside (up to ~200 samples), and
residual of random shape, QP chains with wrap-around and chroma QP offsets. The harness self-test checks that all 16 positions occur both with the filter window inside
and with it reaching outside the picture.

## 7. Reading failures

On a mismatch the harness first prints the usual first-difference report (frame, plane, macroblock, pixel, expected/actual, per-frame counts, an ASCII map).
Then it runs its reference decoder up to the first bad frame and prints a **diagnosis**:

* the first wrong macroblock in raster order: its type, CBP, QP, and for every partition the size, `ref_idx`, vector, quarter-sample position letter (G, a, b, ... r),
  chroma fraction, whether the reference block lies outside the picture, and how many luma/chroma samples are wrong in it;
* a 4 x 4 table: for each fractional position, how many partitions of the frame use it and how many of them contain wrong samples;
* hints, derived from that table, for example

```
hints:
  - only positions using the centre sample j (j, f, i, k, q) are wrong: j must come from the UNROUNDED intermediate values (...)
```

| pattern in the table | usual cause |
|---|---|
| only G (integer) blocks wrong | wrong reference picture / list order, wrong mvx>>2, or reference is not the stored final picture |
| integer or fractional blocks wrong only where the window leaves the picture | clamping (missing, wrong limit, off by one, cropped size used) |
| b, h wrong | taps, `+16 >> 5`, Clip1 |
| only j, f, i, k, q wrong | j from rounded values, `+512 >> 10` |
| a, c, d, n wrong but b, h, j right | averaging: round up `(a+b+1)>>1`, or wrong second sample |
| e, g, p, r wrong | wrong pair of half samples |
| luma right, chroma wrong where no chroma residual | bilinear weights, `+32 >> 6`, `>>3`/`&7`, chroma block size, clamping with the luma size |
| wrong only in macroblocks with residual | DC scaled specially, QP chain, chroma QP, missing Clip1, CBP bit mapping |
| wrong only in intra macroblocks | the Phase 5 path in a P slice (constrained_intra_pred, neighbours) |

The diagnosis is advice: if it prints "WARNING: the harness's own reference decoder does not reproduce ffmpeg's frame", do not trust the hints for that frame.
Debug the **first** bad frame only; later frames are bad because they predict from it.

## 8. What's next

Chapter 7.5 adds the deblocking filter for inter edges (boundary strengths 2/1/0), after which `T6` itself decodes byte-identically.
