# Chapter 7.5 - Deblocking for inter edges

> **SPOILER WARNING: do not open `spoiler_reference/` until your own decoder passes.**
> It holds complete solutions: `bsref_inter.py` (edge flags and bS), `deblock_ref.py` (the filter), `interdec.py` (the whole decoder),
> plus copies of earlier chapters' solutions and `interdeblockgen.py` (the synthetic-stream generator). The harness uses them for its
> diagnosis, so you never need to read them. Build your decoder from this text alone. `test_interdeblock.py` is plumbing (comparison,
> diagnosis, self-test) and is safe to read.

## 1. Goal and the project

Your decoder already turns I and P pictures into pixels (7.4) and already knows how to filter block edges in intra pictures (Phase 6). This chapter joins the two:
the **deblocking filter runs on P pictures**, which means teaching the boundary-strength decision (bS) about motion and coefficients, and putting the filter at the right place in the decoding loop.

Milestone: your decoder writes a YUV file that is **byte-identical to ffmpeg's** for all 50 frames of `T6` (x264 Baseline, every partition size, `ref=4`, deblocking on).
That is the first **complete Baseline-profile decoder** of this book: I and P pictures, CAVLC, multiple slices, any QP, intra macroblocks inside P pictures, deblocking. The caveats are in section 4.

## 2. Big picture

The deblocking filter removes the blocky look that block-based coding leaves at the edges of 4x4 transform blocks and of prediction blocks. For every edge it asks two questions:

1. **How likely is a coding artefact here?** The answer is the *boundary strength* bS, 0 to 4. Intra blocks are the worst offenders (bS 3/4), then blocks with
   residual (bS 2), then blocks that were predicted from different places (bS 1), then everything else (bS 0 = leave the edge alone).
2. **Is the step across the edge small enough to be an artefact rather than a real picture edge?** That is decided per sample line by thresholds alpha and beta that grow with the quantiser.

In Phase 6 you answered question 1 for intra pictures, where every edge is bS 3 or 4. In a P picture most edges are *not* intra, so the answer comes from what the decoder already knows:
which reference picture and which vector each 4x4 block used (7.2, 7.3) and whether the block carried coefficients (7.1). **The filter itself (question 2) does not change at all.**

One more thing changes: the **loop**. In P pictures the filtered picture is the reference for later pictures, so deblocking is part of the decoding process, not a post-process.

## 3. Concepts in detail

### 3.1 What you carry over from Phase 6 (restated, in case you jump in here)

* **Edges.** A macroblock has 4 vertical luma edges (at x = 0, 4, 8, 12) and 4 horizontal ones (y = 0, 4, 8, 12); each is cut into 4 **segments** of 4 sample lines, so a macroblock has 2 x 4 x 4 = 32 bS values. Edge 0 is the **macroblock edge** (shared with the left / upper neighbour); edges 1-3 are **internal**.
  The unit is always a 4x4 luma block; with the 8x8 transform (Phase 8) the edges at 4 and 12 disappear.
* **Edge flags (filterEdgeFlag, per macroblock, the same as in intra pictures).** All edges are off if `disable_deblocking_filter_idc` of the macroblock's slice is 1. The left (top) macroblock edge is also off at the picture's left (top) boundary,
  and with idc 2 when the neighbouring macroblock belongs to a different slice. With idc 0 the macroblock edge IS filtered across slice boundaries. The slice of the **current (q) macroblock** decides idc and the alpha/beta offsets, also for its left and top edges.
* **Order.** Macroblocks in raster order; within a macroblock the luma vertical edges left to right, then the luma horizontal edges top to bottom, then Cb and Cr the same way. Every edge reads samples already modified by earlier edges (the 3 samples of
  macroblock edge 0 on the neighbour's side are already filtered by the neighbour's own internal edges and, for the left neighbour, by everything the neighbour did).
* **Chroma** (4:2:0) has two vertical and two horizontal edges per 8x8 block, at chroma samples 0 and 4, which are the luma edges 0 and 2. They use **the bS of the corresponding luma segments**: chroma sample line k of an edge takes the bS of luma segment k/2.
  There is no separate chroma bS and chroma coefficients never matter.
* **The sample filter (8.7.2.3 / 8.7.2.4)** is the one you built in Chapter 6.2, unchanged: for each line of samples p3 p2 p1 p0 | q0 q1 q2 q3 across an edge
  (p0 / q0 = the samples next to the edge), the average of the two macroblocks' QP_Y (I_PCM counts as 0; for chroma, map each side to QP_C first) plus the slice's
  offsets gives indexA / indexB, hence alpha / beta (Table 8-16); the line is filtered only if bS > 0 and the alpha / beta tests pass; bS 1-3 use the normal filter
  with tC0 from Table 8-17, bS 4 the strong filter.
  **New in P pictures: bS 1 and 2 occur, so you now need the bS = 1 and bS = 2 columns of Table 8-17.** Intra pictures only ever use the bS = 3 column, so a
  Phase 6 filter may have only that one.

### 3.2 The bS ladder for P pictures (8.7.2.1, frame macroblocks)

For each segment look at the two 4x4 luma blocks that meet there: **p** (the block containing the sample just before the edge, in this or the neighbouring macroblock) and **q** (the block containing the sample just after it, always in the current macroblock). Take the first rule that applies:

| bS | condition |
|---|---|
| **4** | the edge is a **macroblock edge** and p or q is in an **intra** macroblock (I_NxN, I_16x16 or I_PCM) |
| **3** | p or q is in an intra macroblock and the edge is an **internal** edge |
| **2** | the 4x4 luma block containing p0 has non-zero transform coefficients, or the one containing q0 does |
| **1** | motion: the two blocks use **different reference pictures**, or a different number of motion vectors (always one in P slices), or the horizontal or the vertical vector difference is **4 or more** in quarter luma samples |
| **0** | otherwise |

(The spec also gives 4 / 3 to macroblock / internal edges in SP and SI slices, which this book does not decode. For field macroblocks and field pictures, Phase 10,
horizontal intra macroblock edges get 3 instead of 4; everything in this chapter is frame macroblocks.)

Details that decide whether your output matches:

* **Intra beats everything.** An intra macroblock inside a P slice gives 4 on its macroblock edges and 3 inside, whatever the other side is. `constrained_intra_pred_flag` has no influence on bS.
* **"Non-zero coefficients" means that 4x4 luma block's own coefficients**: for CAVLC the `total_coeff` of that block (Phase 4's `nC` bookkeeping) is not 0. It is *not* the macroblock's CBP, nor the 8x8 quadrant's CBP bit (a coded quadrant can contain 4x4 blocks with `total_coeff` 0),
  and chroma coefficients do not count - not even for chroma edges. For the 8x8 transform (Phase 8) the test is on the whole 8x8 block. A skipped macroblock and a macroblock with CBP 0 have no coefficients. (The Intra16x16 DC-only case is irrelevant here: intra macroblocks are decided by the first two rows.)
* **Reference pictures are compared as pictures.** Two blocks have "the same reference" if `RefPicList0[ref_idx]` of each resolves to the same decoded picture. Comparing `ref_idx` numbers is wrong in general: the p block may be in another slice with another list.
  Within this chapter's streams (default lists) equal pictures always have equal indices, so the mistake cannot show in a stream - keep a picture identity per stored picture anyway; from 7.6 on (list modification) one picture may sit at two indices, and then only the picture comparison is right.
  The hand-made unit cases below contain such a case.
* **Vectors** are the final vectors of 7.3 (after adding mvd), in quarter luma samples, per 4x4 block. `|dx| >= 4` OR `|dy| >= 4` gives bS 1; `(3, 3)` gives 0, `(4, 0)` gives 1. The limit is 4 in both directions for frame macroblocks.
  (The spec states the limit as 4 in quarter *frame* samples; field macroblocks and field pictures, Phase 10, store vertical vectors in field units, so there it is 2. B slices, 7.8: with two vectors per block the comparison is made on matched pairs and the swapped pairing is allowed; with one vector against two the answer is bS 1.)
* **Inside a macroblock** the same ladder applies to every internal edge. Two 4x4 blocks of the same partition have equal reference and vector, so only coefficients can raise that edge; an edge between two partitions can be 1 by motion. Do not special-case partitions: look at the 16 per-block (reference picture, vector) pairs you already stored for vector prediction.
* **P_Skip** is an inter block with reference index 0, the inferred vector and no coefficients. Skipped macroblocks must be in your per-block motion store with their *inferred* vector (not 0,0).
* **I_PCM** is intra: 4 / 3 like any other intra macroblock, and `qP = 0` in the filter (Phase 6.2).
* **Slices.** A macroblock edge between two slices (idc 0) is filtered with bS computed exactly as usual; the p block's reference picture is resolved through *its own* slice's list.

### 3.3 Where the filter sits in the decoding loop

Per picture:

1. Reconstruct **all** macroblocks of all slices (prediction + residual), writing into the new picture buffer. Intra prediction of later macroblocks reads the neighbours' **unfiltered** samples; no filtering has happened yet.
2. Remember for each macroblock what bS needs: slice, intra / I_PCM flag, QP_Y, the 16 flags "block has coefficients", the 16 (reference picture, vector) pairs, and the slice's idc / alpha / beta offsets.
3. Run the deblocking pass over the whole picture in macroblock order (3.1). It needs the whole picture because a macroblock's left and top edges change samples of earlier macroblocks.
4. The **filtered** picture is what you output, and what is stored in the decoded picture buffer (7.2) as a reference. Inter prediction of later pictures (7.4) reads these filtered samples, with the same clamping as before.

So: intra prediction uses unfiltered samples of the *current* picture, inter prediction uses fully filtered samples of *other* pictures. Two classic mistakes: storing the unfiltered picture as the reference (the I picture is right, every P picture after it is wrong, and the error grows); filtering right after each macroblock (intra prediction then sees filtered samples).
The harness's self-test contains both mistakes and shows that it catches them.

### 3.4 Things that surprise people

* The threshold is "4 or more", not "more than 4" and not 3. The harness streams contain many differences of exactly 3, 4 and 5.
* An edge between two blocks that both carry coefficients is 2, *regardless* of motion; an edge with coefficients on one side only is also 2.
* bS is computed per 4x4 block pair, not per macroblock: the four segments of one edge can differ (0, 1, 2 next to each other).
* The p block of a macroblock edge is the *neighbour's* block, so you need the neighbour's coefficient flags and vectors after that macroblock is long finished (keep them for the whole picture).
* QP for a skipped macroblock or one with CBP 0 is the **running QP_Y** (the QP of the previous macroblock in the slice), not the slice QP and not 0.
* Deblocking runs for P pictures and I pictures alike; a decoder that only filters I pictures drifts immediately.

### 3.5 Hand-made unit cases (`bS` of one segment)

Check your bS function on these before you run a stream (p, q are the two 4x4 blocks; "pic" = the reference picture; "idx" = ref_idx; mv in quarter samples). The harness verifies the same list against the reference.

| # | situation | bS |
|---|---|---|
| 1 | same picture, vectors (0,0) and (3,3), internal | 0 |
| 2 | same picture, (0,0) and (4,0) | 1 |
| 3 | same picture, (0,0) and (0,-4) | 1 |
| 4 | same picture, (7,1) and (7,6), macroblock edge | 1 |
| 5 | different pictures, identical vectors | 1 |
| 6 | same picture reached through ref_idx 0 and ref_idx 2, same vectors | 0 |
| 7 | different pictures, both with ref_idx 1 (lists of different slices) | 1 |
| 8 | coefficients on p only, vectors equal | 2 |
| 9 | coefficients on q only, macroblock edge | 2 |
| 10 | coefficients on p, and different pictures / vectors | 2 |
| 11 | P_Skip beside P_Skip, same vector (-6,3) | 0 |
| 12 | P_Skip with vector (0,0) beside a block with vector (4,0) | 1 |
| 13 | q intra, macroblock edge | 4 |
| 14 | p intra, q inter with coefficients, macroblock edge | 4 |
| 15 | both intra, internal edge | 3 |
| 16 | I_PCM beside inter, macroblock edge | 4 |

## 4. The project: exactly what to build

Extend the 7.4 decoder:

1. While decoding each macroblock, keep for the picture: slice id, intra / I_PCM flag, QP_Y (0 for the filter if I_PCM), per 4x4 luma block "has non-zero coefficients" and (reference picture identity, vector) for inter macroblocks. Give every stored picture an identity.
2. After the last macroblock of a picture: for every macroblock the edge flags (Phase 6.1) and the 32 bS values with the ladder of 3.2.
3. The Phase 6.2 filter over the picture (3.1, 3.3), then output the picture and store the filtered picture as a reference.
4. Optional but recommended: the bS dump (section 5), which lets you debug the decision separately from the filter.

**Verify:** `T6` (deblocking on) decodes byte-identical for all 50 frames. Before that, check the unit cases of 3.5; `T5` and `T6_nodeblock` (from 7.4) must still pass.

**What "complete Baseline decoder" means here, and what it does not.** The decoder now handles everything x264's Baseline output and the synthetic streams of this chapter use: IDR + P pictures, CAVLC, 4x4 transform, all partition and sub-partition shapes, several reference frames with the default list and the sliding window, intra macroblocks in P slices, I_PCM, several slices per picture, all `disable_deblocking_filter_idc` values and offsets.
Not yet there: `ref_pic_list_modification` and MMCO / long-term references / `frame_num` gaps (7.6; x264 does not emit them in T6), weighted prediction (7.7), B pictures (7.8-7.9), CABAC and the 8x8 transform (Phase 8), interlace (Phase 10), and the Baseline-only tools FMO / ASO / redundant slices (never used by x264; the book does not implement them).

### Which recipes can be decoded exactly after each Phase 7 chapter

| recipe | after 7.4 | after 7.5 | 7.6 | 7.7 | 7.8 | 7.9 |
|---|---|---|---|---|---|---|
| T1, T2, T4_nodeblock, `default_intra` (intra, no deblock) | yes | yes | yes | yes | yes | yes |
| T3, T3_dm3, T3_dp3, T4 (intra, deblocking) | yes (Phase 6) | yes | yes | yes | yes | yes |
| T5 (P16x16) | yes | yes | yes | yes | yes | yes |
| T6_nodeblock (all partitions, ref=4, no deblock) | yes | yes | yes | yes | yes | yes |
| T6 (deblocking on) | no | **yes - first complete Baseline decoder** | yes | yes | yes | yes |
| T6 with `ref=16`, reordering / MMCO variants | no | plain `ref=16` streams without reordering (`d_ref16`) | **yes** | yes | yes | yes |
| T7 (weighted P, fade) | no | no | no | **yes** | yes | yes |
| T8 (B, no direct) | no | no | no | no | **yes** | yes |
| T9_spatial, T9_temporal (B pyramid, direct, implicit weights) | no | no | no | no | no | **yes** |
| `default` (x264 Main CAVLC, I/P/B, weighted P) | no | no | no | no | no | yes (with Phase 7's sort-by-POC output) |
| T10 (CABAC), T11 (High), T12 (interlaced) | Phase 8 / 10 | | | | | |

## 5. Harness input format

Your decoder writes one or two files, which you then pass to the harness (section 6): the decoded YUV and, optionally, a bS dump.

**Output YUV (required)**: the Chapter 5.1 format: all frames in output order, 8-bit planar yuv420p (all Y, then all Cb, then all Cr), cropped as the SPS says, no padding, no headers. For T6: 352 x 288, 50 frames, 7 603 200 bytes.

**bS dump (optional, `--bs-dump FILE`)**: the Chapter 6.1 text format, unchanged, now written for P pictures too. One block per picture in **decoding order** (for these P-only streams = output order), covering **every** picture of the stream, the I pictures included (there the values are the 0 / 3 / 4 of Chapter 6.1). Line grammar:

```
PIC <n>
MB <addr> <mbx> <mby> FV <f0> <f1> <f2> <f3> FH <f0> <f1> <f2> <f3> V <16 numbers> H <16 numbers>
```

| Token | Meaning |
|---|---|
| `PIC <n>` | starts a picture; n = 0, 1, 2, ... in decoding order |
| `<addr> <mbx> <mby>` | macroblock address (`mby * PicWidthInMbs + mbx`), column, row. Every macroblock of the picture exactly once, in raster order |
| `FV f0..f3` | filterEdgeFlag (0/1) of the vertical edges at x = 0, 4, 8, 12 |
| `FH f0..f3` | the same for the horizontal edges at y = 0, 4, 8, 12 |
| `V` + 16 numbers | bS (0..4) of the vertical edges: edge 0 segments 0, 1, 2, 3 (segment s = rows 4s..4s+3), then edge 1, edge 2, edge 3 |
| `H` + 16 numbers | the same for horizontal edges (segment s = columns 4s..4s+3) |

A flag of 0 means the four bS values of that edge are 0. A flag of 1 can come with any bS 0..4 (in P pictures an edge with flag 1 and bS 0 is normal). Blank lines and lines starting with `#` are ignored; tokens are separated by spaces. Example, macroblocks of picture 1 of `T6`:

```
PIC 1
MB 0 0 0 FV 0 1 1 1 FH 0 1 1 1 V 0 0 0 0 2 2 0 0 2 0 2 0 0 0 2 0 H 0 0 0 0 2 2 0 0 2 0 2 2 0 0 2 2
...
MB 24 2 1 FV 1 1 1 1 FH 1 1 1 1 V 2 2 1 1 2 2 0 0 0 2 0 0 0 0 0 0 H 2 1 1 1 2 2 0 0 2 2 1 1 0 0 0 0
```

(The first line: macroblock 0 has no left / top neighbour, so flags 0 on edge 0; its internal edges carry coefficients (2) in some segments and bS 0 elsewhere. Macroblock 24: the left edge has bS 2 on rows 0-7 and bS 1 on rows 8-15; the other edges are 2, 1 or 0 segment by segment.)

`python3 test_interdeblock.py --expected-bs-out ref.txt <stream>` writes the reference dump so that you can `diff` it with yours.

## 6. Running the harness

```
python3 test_interdeblock.py ../.cache/T6.ts my_t6.yuv                       # pixels
python3 test_interdeblock.py ../.cache/T6.ts my_t6.yuv --bs-dump my_bs.txt   # pixels and the bS grid
python3 test_interdeblock.py ../.cache/T6.ts --bs-dump my_bs.txt             # only the bS grid
python3 test_interdeblock.py --make-variants variants/    # x264 variants + synthetic streams + recipes T5, T6
python3 test_interdeblock.py --self-test                  # about two minutes
```

(`python3 -m common.testfiles T5 T6 T6_nodeblock` builds the recipes in `.cache/`.) The pixels are compared with ffmpeg's decode (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`, via `common/yuvcmp.py`).
ffmpeg cannot print bS, so the dump is compared with an independent reference (`spoiler_reference/`). That reference is validated by consequence: bS plus filter plus the whole P decoder must reproduce ffmpeg's output byte for byte on `T6`, on x264 variants and on the synthetic streams
(`--self-test` checks this), which a wrong bS rule cannot do on these streams. Exit 0 = identical, 1 = different, 2 = unusable (missing file, malformed dump, a stream this chapter cannot decode).

Test streams from `--make-variants` (all Baseline-style CAVLC unless noted):

* x264, 25 frames each: `d_ref1` (one reference), `d_ref16` (sixteen references), `d_slices` (4 slices per picture), `d_offp3` / `d_offm3` (deblock offsets +3 / -3), `d_noisy_qp36` (noisy source: many coefficients, high QP), `d_main_cip` (Main profile, CAVLC, constrained intra prediction).
  None of them uses list modification or MMCO (the reference decoder would refuse such a stream until 7.6).
* Synthetic `synth_motion`, `synth_refs`, `synth_coef`, `synth_intra`, `synth_slices`, `synth_mix` (96 x 64, 12-14 pictures, made by a bit writer): smooth I_PCM textures (so the filter thresholds are met and the filter really changes samples), QP 22-46, offsets -6..+6, every slice with idc 0, 1 or 2.
  Vectors are the predicted vector plus a delta from -9 to +9 (many differences of exactly 3, 4, 5), several references, blocks with coefficients next to blocks without (including coded quadrants with all-zero 4x4 blocks), skipped, I_PCM and intra macroblocks inside P pictures, edges between slices.
  The self-test prints how many segments of each kind occur and fails if a rule is not exercised.

## 7. Reading failures

On a pixel mismatch the harness prints the usual first-difference report (frame, plane, macroblock, pixel, expected / actual, per-frame counts, an ASCII map), then a **diagnosis** from its reference decoder:

* the first wrong macroblock: type, CBP, QP, slice and idc, the 16 (reference picture, vector, has-coefficients) entries and the reference bS of its 32 segments;
* how your wrong luma samples relate to the reference's unfiltered and filtered pictures: *not filtered by you* (your sample equals the unfiltered one: your bS is too low or the edge is missing), *filtered by you, not by ffmpeg* (bS too high), *filtered differently* (bS 1 vs 2, 3 vs 4, QP, offsets);
* how many wrong samples sit next to segments of reference bS 0, 1, 2, 3, 4;
* hints, for example *only edges with reference bS 1 are involved: the motion rule*.

With `--bs-dump` the harness also lists the first 10 differing flags / bS values (picture, macroblock, edge, segment, expected, got) and, for the first three, the two 4x4 blocks that meet there (macroblock, reference picture id, `ref_idx`, vector, coefficients) with a hint.

| symptom | usual cause |
|---|---|
| frame 0 right, frame 1 wrong only along edges | bS decision (compare dumps); if the dump is right, the filter's QP handling for skipped / CBP-0 macroblocks |
| bS dump right, pixels wrong only in later frames | the stored reference is the unfiltered picture, or intra prediction read filtered samples |
| expected 0, got 1 along partition borders | motion limit 3 instead of 4, vertical only checked, `ref_idx` instead of pictures, skip vectors taken as 0 |
| expected 1, got 0 | difference of exactly 4 treated as equal (`>` instead of `>=`), references not compared |
| expected 2, got 1 / 0 | coefficient test on the CBP bit, on one side only, or on the macroblock instead of the 4x4 block |
| expected 0 / 1, got 2 | skipped macroblocks or chroma coefficients counted |
| expected 4, got 3 (or the reverse) | intra macroblock edge rule; 4 only on MACROBLOCK edges |
| bS dump right, samples "filtered differently" only next to bS 1 / 2 edges | the bS 1 / bS 2 columns of Table 8-17 (tC0) missing or wrong |
| wrong only in chroma | chroma uses luma segments `k/2` on edges 0 and 2 only; QP_C of each side |
| wrong only at slice borders | idc 2 across slices, offsets of the wrong slice (the q macroblock's slice decides) |
| wrong only after I_PCM neighbours | `qP = 0` for I_PCM in the filter |

If the harness prints "WARNING: the harness's own reference decoder does not reproduce ffmpeg's frame", do not trust the hints for that frame. Debug the **first** bad frame only: later frames predict from it.

## 8. What's next

Chapter 7.6 completes reference management (list modification, MMCO, long-term references, frame_num gaps), after which T6 variants with `ref=16` and the multi-reference conformance streams decode too.
