# Chapter 6.2 - Luma and chroma edge filters

> **SPOILER WARNING: do not open `spoiler_reference/` until your own filter works.** It contains `deblock_ref.py`, a complete reference deblocking filter (the one Chapter 6.1 used to validate its bS grid), and `bsref.py`, the reference bS computer. The harness imports them for you; you never need to read them.

## 1. Goal and the project

Chapter 6.1 decided *where* the deblocking filter works: for every macroblock, which edges are filtered and with which boundary strength bS (0 to 4) per 4-sample segment. This chapter adds the part that touches pixels: from bS, the quantisation parameters, the slice offsets and the sample values themselves, it decides how much to smooth each line of samples across each edge, and does it.

You build the filter for luma and for both chroma planes (4:2:0, 8 bits), run it over every picture after reconstruction, and write the final YUV. **Milestone of Phase 6:** T3 decodes byte-identical to ffmpeg, over all frames, and so do T3_dm3 (`deblock=-3,-3`), T3_dp3 (`deblock=3,3`) and T4 (`slices=4`; deblocking across the slice edges, idc 0, x264's default). The comparison uses `common/yuvcmp.py`, as in Phase 5.

## 2. Big picture

Quantisation is applied block by block, so neighbouring blocks end up with slightly different errors and a faint grid of steps appears along the block edges. The deblocking filter looks at the samples on both sides of an edge and asks one question per line of samples: *is this step a coding artefact or real picture content?* A small step next to flat areas is an artefact and is smoothed; a large step, or a busy area, is content and is left alone. The thresholds depend on how coarsely the picture was quantised: coarse quantisation (high QP) makes artefacts bigger, so the thresholds grow with QP.

Four things follow, and they shape the whole chapter:

- **Everything is per line of samples.** For a vertical edge, a "line" is one row of the picture crossing the edge; for a horizontal edge, one column. Per line you read up to 4 samples on each side and change at most 3 on each side.
- **The decision uses only numbers you already have**: bS (6.1), the QP of the two macroblocks, the slice offsets, and the samples. There is no hidden state.
- **The order matters, and the filter reads its own output.** Filtering one edge changes samples that the next edge reads. The standard fixes the order exactly; any other order gives a different picture.
- **The filter runs after the whole picture is reconstructed.** Intra prediction (Phase 5) must see *unfiltered* neighbours, so you cannot filter a macroblock as soon as it is done. The filtered picture is what is output and (from Phase 7) what later pictures predict from.

## 3. Concepts in detail

Notation: for one line across an edge, **p0, p1, p2, p3** are the samples on the first-processed side counted away from the edge (p0 touches the edge), and **q0..q3** the same on the other side. For a vertical edge p is to the left and q to the right; for a horizontal edge p is above and q below. The macroblock being filtered (the "current" macroblock) always contains q0, because the current macroblock owns its left and top edges. Luma uses p0..p3 and q0..q3; chroma only needs p0, p1, q0, q1. p0' etc. are the new values.

### 3.1 The sweep (8.7)

For each picture, for each macroblock in raster order (address 0, 1, 2 ...):

1. luma: the vertical edges 0, 1, 2, 3 left to right (x = 0, 4, 8, 12), then the horizontal edges 0, 1, 2, 3 top to bottom;
2. Cb: vertical edges then horizontal edges; then Cr the same way. Cb and Cr never interact, so their relative order does not matter, nor does the luma-before-chroma order. What does matter is: **within a plane, all vertical edges of the macroblock before any horizontal edge of it, and edge n before edge n+1.**

"In place" means: every sample you read is whatever is currently in the picture buffer, including changes made by earlier edges of this macroblock and by the *previous macroblocks*. Concretely, when macroblock n filters its left edge, the three columns of macroblock n-1 beside it were already changed by n-1's internal vertical edges and by n-1's horizontal edges (n-1 is entirely done), and the filter changes them again. Likewise the top edge reads rows of the macroblock above that are final except for the 3 rows this edge changes. Macroblock n+1 has not been touched yet when n runs.

Edges which 6.1 marked as not filtered (bS 0, flag 0) are simply skipped. With bS 0 the filter does nothing, so you can also run the filter on every edge and let bS 0 do the skipping; but never read outside the picture (the edges at x = 0 / y = 0 of the picture have flag 0).

### 3.2 Which samples form a line

| Plane | Vertical edge at column X | Horizontal edge at row Y |
| --- | --- | --- |
| Luma | for each of the 16 rows y of the macroblock: q0..q3 = (X, y) .. (X+3, y); p0..p3 = (X-1, y) .. (X-4, y) | for each of the 16 columns x: q0..q3 = (x, Y) .. (x, Y+3); p0..p3 = (x, Y-1) .. (x, Y-4) |
| Chroma (4:2:0) | for each of the 8 chroma rows: q0, q1 = (X, y), (X+1, y); p0, p1 = (X-1, y), (X-2, y) | for each of the 8 chroma columns the same, vertically |

Luma edges are at 0, 4, 8, 12 within the macroblock. **Chroma edges are at 0 and 4** (chroma sample units; 4:2:0 chroma is 8x8 with 4x4 transform blocks). Chroma edge 0 is the macroblock edge (bS from luma edge 0), chroma edge 4 is internal (bS from luma edge 2, at luma 8). Luma edges 1 and 3 have no chroma counterpart.

**Chroma line k takes the bS of luma segment k/2** (integer division): the chroma line sits at luma line 2k, and a luma segment of 4 lines covers 2 chroma lines. So a luma edge with 4 segments gives a chroma edge of 8 lines in four pairs.

### 3.3 QP of the two sides: qPp, qPq (8.7.2.2)

qPp and qPq are the quantiser values of the macroblocks containing p0 and q0:

- **Luma:** QP_Y of each macroblock. **An I_PCM macroblock counts as 0** (its QP_Y is not meaningful). This is a filter-only rule: the running QP of Chapter 5.5 (the predictor for the next mb_qp_delta) is *not* reset by I_PCM; only here does an I_PCM side contribute qP = 0.
- **Chroma:** each side separately maps *its own* luma QP to a chroma QP: QPc = table(Clip3(0, 51, QP_Y + offset)), using chroma_qp_index_offset for Cb and second_chroma_qp_index_offset for Cr (PPS; for streams without the extension both are the same value). For an I_PCM side QP_Y is 0 first, then the offset applies. The table maps values below 30 to themselves:

| qPI | 30 | 31 | 32 | 33 | 34 | 35 | 36 | 37 | 38 | 39 | 40 | 41 | 42 | 43 | 44 | 45 | 46 | 47 | 48 | 49 | 50 | 51 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| QPc | 29 | 30 | 31 | 32 | 32 | 33 | 34 | 34 | 35 | 35 | 36 | 36 | 37 | 37 | 37 | 38 | 38 | 38 | 39 | 39 | 39 | 39 |

The two chroma QPs are **averaged** (below); you do not average the luma QPs first and convert afterwards, and the two results can differ.

Inside a macroblock, p and q belong to the same macroblock, so qPp = qPq. On a macroblock edge p belongs to the left or top neighbour (any slice, if the edge is filtered at all).

### 3.4 indexA, indexB, alpha, beta (8.7.2.2)

```
qPav    = (qPp + qPq + 1) >> 1
indexA  = Clip3(0, 51, qPav + filterOffsetA)
indexB  = Clip3(0, 51, qPav + filterOffsetB)
filterOffsetA = slice_alpha_c0_offset_div2 << 1      (i.e. -12 .. +12)
filterOffsetB = slice_beta_offset_div2     << 1
alpha = alpha'[indexA]    beta = beta'[indexB]       (8-bit video: no scaling)
```

Notes:

- The slice header carries the offsets divided by 2; **double them**. When the slice header has no offsets (`deblocking_filter_control_present_flag` 0 in the PPS, or idc 1), they are 0.
- The offsets are those of **the slice containing q0, the current macroblock**, even on a left or top edge that borders another slice. The neighbour's offsets are never used for the current macroblock's edges.
- alpha uses indexA, beta uses indexB. tC0 (below) uses indexA.
- For indexA < 16 alpha is 0, so no line ever passes the `|p0 - q0| < alpha` test: **a very low QP switches the filter off**, whatever bS says. Positive offsets can switch it back on, negative offsets can push a mid QP below 16.

Table 8-16 (alpha', beta') and Table 8-17 (tC0') in one table, one row per index. alpha' and tC0' are looked up with indexA, beta' with indexB. tC0' columns are the bS values 1, 2, 3 (bS 4 does not use tC0, and bS 0 never filters).

| index | alpha' | beta' | tC0' bS=1 | bS=2 | bS=3 |
| --- | --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 0 | 0 | 0 |
| 1 | 0 | 0 | 0 | 0 | 0 |
| 2 | 0 | 0 | 0 | 0 | 0 |
| 3 | 0 | 0 | 0 | 0 | 0 |
| 4 | 0 | 0 | 0 | 0 | 0 |
| 5 | 0 | 0 | 0 | 0 | 0 |
| 6 | 0 | 0 | 0 | 0 | 0 |
| 7 | 0 | 0 | 0 | 0 | 0 |
| 8 | 0 | 0 | 0 | 0 | 0 |
| 9 | 0 | 0 | 0 | 0 | 0 |
| 10 | 0 | 0 | 0 | 0 | 0 |
| 11 | 0 | 0 | 0 | 0 | 0 |
| 12 | 0 | 0 | 0 | 0 | 0 |
| 13 | 0 | 0 | 0 | 0 | 0 |
| 14 | 0 | 0 | 0 | 0 | 0 |
| 15 | 0 | 0 | 0 | 0 | 0 |
| 16 | 4 | 2 | 0 | 0 | 0 |
| 17 | 4 | 2 | 0 | 0 | 1 |
| 18 | 5 | 2 | 0 | 0 | 1 |
| 19 | 6 | 3 | 0 | 0 | 1 |
| 20 | 7 | 3 | 0 | 0 | 1 |
| 21 | 8 | 3 | 0 | 1 | 1 |
| 22 | 9 | 3 | 0 | 1 | 1 |
| 23 | 10 | 4 | 1 | 1 | 1 |
| 24 | 12 | 4 | 1 | 1 | 1 |
| 25 | 13 | 4 | 1 | 1 | 1 |
| 26 | 15 | 6 | 1 | 1 | 1 |
| 27 | 17 | 6 | 1 | 1 | 2 |
| 28 | 20 | 7 | 1 | 1 | 2 |
| 29 | 22 | 7 | 1 | 1 | 2 |
| 30 | 25 | 8 | 1 | 1 | 2 |
| 31 | 28 | 8 | 1 | 2 | 3 |
| 32 | 32 | 9 | 1 | 2 | 3 |
| 33 | 36 | 9 | 2 | 2 | 3 |
| 34 | 40 | 10 | 2 | 2 | 4 |
| 35 | 45 | 10 | 2 | 3 | 4 |
| 36 | 50 | 11 | 2 | 3 | 4 |
| 37 | 56 | 11 | 3 | 3 | 5 |
| 38 | 63 | 12 | 3 | 4 | 6 |
| 39 | 71 | 12 | 3 | 4 | 6 |
| 40 | 80 | 13 | 4 | 5 | 7 |
| 41 | 90 | 13 | 4 | 5 | 8 |
| 42 | 101 | 14 | 4 | 6 | 9 |
| 43 | 113 | 14 | 5 | 7 | 10 |
| 44 | 127 | 15 | 6 | 8 | 11 |
| 45 | 144 | 15 | 6 | 8 | 13 |
| 46 | 162 | 16 | 7 | 10 | 14 |
| 47 | 182 | 16 | 8 | 11 | 16 |
| 48 | 203 | 17 | 9 | 12 | 18 |
| 49 | 226 | 17 | 10 | 13 | 20 |
| 50 | 255 | 18 | 11 | 15 | 23 |
| 51 | 255 | 18 | 13 | 17 | 25 |

(Check yours against the standard when you type it in. In Phase 6 every picture is intra, so only bS 3 and 4 occur: the **bS 1 and bS 2 columns are not exercised by any test until Chapter 7.5**. Type them in carefully now.)

### 3.5 Does this line get filtered? (filterSamplesFlag)

A line is filtered only if all of these hold:

```
bS != 0   and   |p0 - q0| < alpha   and   |p1 - p0| < beta   and   |q1 - q0| < beta
```

The first test is the edge decision of 6.1. The second says "the step is small enough to be an artefact"; the last two say "both sides are smooth enough". If the flag is false the whole line is left unchanged.

### 3.6 bS < 4 (bS 1, 2, 3): the weak filter

For a bS 3 edge (all internal edges of an intra macroblock; in Phase 7 also bS 1 and 2):

```
tC0 = tC0'[indexA][bS]                       (Table 8-17)
luma:   ap = |p2 - p0|    aq = |q2 - q0|
        tC = tC0 + (ap < beta) + (aq < beta)
chroma: tC = tC0 + 1
delta = Clip3(-tC, tC, ((((q0 - p0) << 2) + (p1 - q1) + 4) >> 3))
p0' = Clip1(p0 + delta)
q0' = Clip1(q0 - delta)                       Clip1 = clip to 0..255
luma only, if ap < beta:  p1' = p1 + Clip3(-tC0, tC0, (p2 + ((p0 + q0 + 1) >> 1) - (p1 << 1)) >> 1)
luma only, if aq < beta:  q1' = q1 + Clip3(-tC0, tC0, (q2 + ((p0 + q0 + 1) >> 1) - (q1 << 1)) >> 1)
```

Everything else (p2, p3, q2, q3 and, for chroma, p1/q1) is unchanged. Notice the two different limits: delta is limited by **tC** (which grows with ap and aq), the p1/q1 corrections by **tC0**. `>>` is an arithmetic shift (rounding towards minus infinity) on possibly negative numbers.

### 3.7 bS = 4: the strong filter (macroblock edges of intra macroblocks)

Luma. First `small = |p0 - q0| < ((alpha >> 2) + 2)` (note: alpha, not alpha'; for 8-bit video they are equal). The p side and the q side are decided independently:

```
if  ap < beta  and small:             (ap = |p2 - p0|)
    p0' = (p2 + 2*p1 + 2*p0 + 2*q0 + q1 + 4) >> 3
    p1' = (p2 + p1 + p0 + q0 + 2) >> 2
    p2' = (2*p3 + 3*p2 + p1 + p0 + q0 + 4) >> 3
else:
    p0' = (2*p1 + p0 + q1 + 2) >> 2            (p1, p2 unchanged)
if  aq < beta  and small:             (aq = |q2 - q0|)
    q0' = (p1 + 2*p0 + 2*q0 + 2*q1 + q2 + 4) >> 3
    q1' = (p0 + q0 + q1 + q2 + 2) >> 2
    q2' = (2*q3 + 3*q2 + q1 + q0 + p0 + 4) >> 3
else:
    q0' = (2*q1 + q0 + p1 + 2) >> 2            (q1, q2 unchanged)
```

All right-hand sides use the *original* values of the line (compute all new values, then write them). No Clip1 is needed (averages cannot leave 0..255).

Chroma, bS 4: only the "else" formulas, **never the strong one**: `p0' = (2*p1 + p0 + q1 + 2) >> 2`, `q0' = (2*q1 + q0 + p1 + 2) >> 2`. For chroma the weak filter (bS < 4) also changes p0 and q0 only.

### 3.8 Worked examples (all produced by running the reference on T3, picture 0)

T3 is Baseline, slice offsets 0, chroma_qp_index_offset -2 (x264's default). Macroblocks 0..4 all have QP_Y 23.

**Luma, bS 4, strong on the p side and weak on the q side** (macroblock 2, vertical edge 0, row 4). QP 23 on both sides: qPav = 23, indexA = indexB = 23, alpha = 10, beta = 4.

```
p3 p2 p1 p0 | q0 q1 q2 q3  =  32 31 31 33 | 31 34 38 35
```

|p0-q0| = 2 < 10, |p1-p0| = 2 < 4, |q1-q0| = 3 < 4: filtered. small: 2 < (10>>2)+2 = 4. ap = |31-33| = 2 < 4, strong on p: p0' = (31+62+66+62+34+4)>>3 = 259>>3 = 32, p1' = (31+31+33+31+2)>>2 = 32, p2' = (64+93+31+33+31+4)>>3 = 32. aq = |38-31| = 7, not < 4: weak on q: q0' = (68+31+31+2)>>2 = 33. Result `32 32 32 32 | 33 34 38 35`.

**Luma, bS 3, both p1 and q1 updated** (macroblock 1, horizontal edge 2, column 11), same indexA 23, tC0 = 1 (bS 3 column of index 23).

```
p3 p2 p1 p0 | q0 q1 q2 q3  =  32 32 33 33 | 33 32 34 34
```

ap = |32-33| = 1 < 4, aq = |34-33| = 1 < 4, so tC = 1+1+1 = 3. delta = Clip3(-3, 3, ((0<<2) + (33-32) + 4)>>3) = 5>>3 = 0, so p0 and q0 stay. p1' = 33 + Clip3(-1, 1, (32 + ((33+33+1)>>1) - 66)>>1) = 33 + Clip3(-1,1,-1) = 32; q1' = 32 + Clip3(-1, 1, (34 + 33 - 64)>>1) = 32 + 1 = 33. Result `32 32 32 33 | 33 33 34 34`.

**Chroma, bS 4** (Cb, macroblock 1, vertical edge 0, line 0): the QPc of each side is table(23 - 2 = 21) = 21, qPav = 21, indexA = indexB = 21: alpha = 8, beta = 3.

```
p1 p0 | q0 q1  =  89 91 | 90 91
```

|p0-q0| = 1 < 8, |p1-p0| = 2 < 3, |q1-q0| = 1 < 3: filtered. p0' = (178+91+91+2)>>2 = 90, q0' = (182+90+89+2)>>2 = 90. Result `89 90 | 90 91`.

**With offsets** (T3_dp3, slice_alpha_c0_offset_div2 = 3): filterOffsetA = 6, so the same QP 23 gives indexA = 29, alpha = 22, tC0 (bS 3) = 2.

These examples are small enough to type into a unit test of your own before you run any picture.

### 3.9 Edge cases that cause most failures

- **I_PCM neighbours.** qP = 0 on the I_PCM side, so qPav is about half the other side's QP, and its luma internal edges never change (indexA small). The chroma QP of an I_PCM side is table(Clip3(0,51, 0 + offset)).
- **Slice boundaries with idc 0** are filtered with the *current* macroblock's offsets and each side's own QP, as above. With idc 2 (6.1) the edge is not filtered at all.
- **Edges at the picture boundary** are not filtered (6.1). Never read x = -1.
- **Extreme QP.** QP 51 plus offset +12 clips to indexA = 51 (alpha 255); QP 0 plus offset -12 clips to 0. Clip3 *after* adding the offset.
- **Clip1** matters only where delta pushes a sample past 0 or 255; test streams with saturated colours (the synthetic ones) exercise it.
- **Arithmetic on negatives.** `(q0 - p0) << 2` and `>> 3` on negative values must be an arithmetic shift (floor), not a division truncating towards zero.
- **A macroblock's own unfiltered samples must not be used for its intra neighbours.** Two designs work: reconstruct the whole picture and then filter (simplest), or keep a copy of the unfiltered right column and bottom row of each macroblock. Chapter 6.1 recommended the first.

## 4. The project: exactly what to build

Add to your decoder, after the picture is fully reconstructed, a **deblocking pass** over the picture buffer:

1. Per macroblock in raster order, call your 6.1 function for the edge flags and bS grid.
2. For luma: all vertical edges, then all horizontal edges, with the line rules of 3.2.
3. For Cb and Cr: chroma edges 0 and 4 in both directions, bS from luma edges 0 and 2.
4. Per line: derive qPp / qPq (I_PCM -> 0; chroma via the QPc table with the right offset), qPav, indexA, indexB, alpha, beta, apply 3.5, then the weak or strong filter of 3.6 / 3.7.
5. Write the filtered pictures to the output YUV (Chapter 5.1's writer, cropped like ffmpeg's output).

Needed per macroblock, kept from earlier chapters: QP_Y, I_PCM flag, slice index, plus per slice the idc and the two offsets. Do **not** overwrite the unfiltered picture before the whole picture has been reconstructed.

Suggested structure: one "filter one line" function that takes the eight (luma) or four (chroma) samples of a line, bS, the two QPs, the two offsets and a luma/chroma flag, and returns the new values. Unit-test it on the worked examples of 3.8 first.

**Optional but recommended: also write the unfiltered picture** to a second file with the same format as the main output (the pictures exactly as Phase 5 produced them, before the deblocking pass). Pass it to the harness with `--unfiltered`: the harness compares it with ffmpeg's `-skip_loop_filter all` decode (the true picture before deblocking, because intra prediction reads unfiltered samples) and tells you at once whether a mismatch is a reconstruction bug (Phase 5 territory) or a deblocking bug.

**Verify (from the plan).** T3 decodes byte-identical, then T3_dm3, T3_dp3 and T4 (slice-edge handling), then the synthetic variants of `--make-variants`.

## 5. Harness input format

Two files at most, both plain raw video:

| File | Content |
| --- | --- |
| `<out.yuv>` | every picture after deblocking, in output order (for these intra-only streams, decoding order), 8-bit planar 4:2:0: for each picture the Y plane (W x H, rows top to bottom), then Cb, then Cr (each (W+1)/2 x (H+1)/2), no headers, no padding. W x H is the **cropped** size ffmpeg outputs (the SPS cropping, as in Chapter 5.1). |
| `<out_unfiltered.yuv>` (optional, `--unfiltered`) | the same pictures *before* deblocking: same layout, same number of pictures, same order. |

## 6. Running the harness

```
python3 test_deblock.py <in.ts|in.264> <out.yuv> [--unfiltered out_unfiltered.yuv]
python3 test_deblock.py --make-variants DIR       # synthetic and x264 streams for more thorough testing
python3 test_deblock.py --self-test               # checks the harness itself (about a minute)
```

The reference output is ffmpeg's decode (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`). `common/yuvcmp.py` reports the first differing frame, plane, macroblock and pixel. Exit code 0 identical, 1 different, 2 missing or unusable input.

Test streams: **T3, T3_dm3, T3_dp3, T4** from `python3 -m common.testfiles T3 T3_dm3 T3_dp3 T4` (all 50 frames each: the milestone). `--make-variants DIR` writes more:

- `bs_*` (from 6.1) and `db_*` synthetic intra streams from a small bit writer (`bsgen.py`): idc 0, 1 and 2 mixed in one picture, slices at row starts and mid-row, offsets -6..+6, QP 0..51 including wrapping deltas, I_PCM neighbours, flat and busy content. x264 cannot write idc 2 or I_PCM, so only these cover them.
- `x_*` x264 Baseline streams at QP 18 to 51 with other offsets, other image sources (`mandelbrot`, `smptehdbars`: saturated colours that exercise Clip1) and `slices=4`; and `x_*_8x8`, High-profile Intra 8x8 streams (your reader needs the Phase 8 Intra 8x8 reconstruction for these; the transform_size_8x8_flag rule of 6.1 applies).

Your reader must handle every stream in the list except the `_8x8` ones for this chapter's milestone (they are a Phase 8 matter). The harness compares pictures of any stream ffmpeg decodes; its edge-level diagnosis (below) needs a CAVLC intra frame stream.

### What the diagnosis tells you

ffmpeg cannot dump its internal filter decisions, so the harness uses its own reference filter (`spoiler_reference/deblock_ref.py`). The reference is *validated against ffmpeg*: `--self-test` runs it on ffmpeg's unfiltered decode of 32 streams (every test stream above, 2 pictures of each recipe) and requires it to reproduce ffmpeg's filtered decode byte for byte. The reference therefore stands for what ffmpeg does, and for a mismatch it can say what *should* have happened:

1. If `--unfiltered` is given and differs from ffmpeg's unfiltered decode, the harness says **reconstruction bug** and stops.
2. Otherwise, for the first wrong picture it prints the number of wrong samples per plane, the first wrong macroblock in raster order (type, QP_Y, slice, idc, offsets), and the first wrong sample in it. Then it lists **every edge line of the reference filter that looks at that sample**, in filtering order, e.g.

```
- Y vertical edge 1 of macroblock 0, line 0 (the sample is p1): bS 3, qPav 27, indexA 39, indexB 39, alpha 71, beta 12
    reads p3..p0 | q0..q3 = 73 71 74 73 | 73 73 73 73   ->  73 71 72 73 | 73 73 73 73   [bS<4 weak: tC0=6, tC=8, delta=0, ap=2, aq=0]
```

   Compare each number with your own decision for that line. The first one that differs is your bug.
3. Hints for patterns: only chroma wrong (QPc, tC0 + 1, bS mapping); only Cr wrong (second offset); only samples 2+ away from the edge wrong (p1/q1 update or strong filter); wrong macroblocks all in a QP range (a table entry or index clipping); only slices with non-zero offsets wrong; wrong macroblocks touching I_PCM.

The diagnosis needs a stream the reference parser handles and which is not cropped; for others only the plain comparison is printed. Because a reader that produces a correct unfiltered picture and an identical filter reproduces the reference exactly, the printed edge decisions are also what you would print.

## 7. Reading failures

- **Picture equals the unfiltered picture.** The filter is not run, or you filter into a copy that you never output.
- **T3 fine at the left of every macroblock row but wrong afterwards / drift to the bottom right.** Order or in-place problem: the filter must read the already filtered samples (vertical edges of the macroblock, then horizontal; macroblock after macroblock). Writing results into a second buffer is the classic mistake.
- **Only chroma wrong.** QPc derivation (per macroblock, then averaged, offset by chroma_qp_index_offset, through the table above 29), `tC = tC0 + 1`, only p0/q0 changed, chroma edge at 4 uses luma edge 2's bS, chroma line k uses luma segment k/2.
- **Only Cr wrong** with offsets in the PPS: Cr uses second_chroma_qp_index_offset.
- **Wrong at p1/q1 (2nd sample from the edge) only.** Luma weak filter: p1' / q1' use tC0, only when ap / aq < beta; or the strong filter's p1' formula.
- **Macroblock edges wrong, internal edges fine.** bS 4 strong filter: the `small` test with `(alpha >> 2) + 2`, and the else branch.
- **T3_dm3 / T3_dp3 wrong, T3 fine.** FilterOffsetA = slice_alpha_c0_offset_div2 * 2 (and B likewise), added to qPav before Clip3.
- **T4 wrong only along the slice boundaries.** idc 0: filter across; neighbour QP is the neighbour's own; offsets are the current slice's.
- **Wrong only at very dark / bright content.** Missing Clip1.
- **Everything wrong at high QP, fine at low.** Table entries near the end (indexA 40..51), or Clip3(0, 51) missing.
- **Crash at the picture edge.** Do not read x = -1 or y = -1; skip edges with flag 0.
- **Wrong with I_PCM macroblocks** (synthetic streams): qP 0 for the I_PCM side.

## 8. Phase 6 recap and what's next

Phase 6 turned a reconstructing decoder into one that produces the pictures an encoder actually intended: edge selection and bS (6.1), the sample filters (6.2). Every intra stream from x264 with Baseline features now decodes byte-identical, with or without deblocking, offsets and slices. The filter you wrote has no intra-specific parts except the bS rule of 6.1: it consumes a bS per segment and nothing else.

**Phase 7** adds inter prediction: P slices (skip runs, partitions, motion vectors, reference lists, the decoded picture buffer), motion compensation with interpolation, and inter residual. The Phase 6 filter is reused unchanged in 7.5, where the bS rule of 6.1 grows the inter ladder (bS 2 for coded coefficients, bS 1 for different references or vectors, bS 0 otherwise), so the bS 1 and bS 2 columns of Table 8-17 you typed in here are finally exercised. One new requirement arrives with it: the filtered picture is stored as a reference, so the unfiltered copy can be dropped once the picture is filtered, and `-skip_loop_filter` stops being a usable truth for the unfiltered picture.
