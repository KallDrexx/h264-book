# Chapter 6.1 - Edge selection and boundary strength

> **Spoiler warning.** The folder `spoiler_reference/` contains the harness's reference bS computer (`bsref.py`) and a complete reference deblocking filter (`deblock_ref.py`, which Chapter 6.2 builds on), plus `transref.py`, a copy of Chapter 5.2's solution that the stream generator `bsgen.py` uses. **Do not open it until you have tried.** The harness imports it for you; you never need to read it.

## 1. Goal and the project

After Phase 5 your decoder reconstructs intra pictures exactly as the standard describes *before* the in-loop deblocking filter. For T1, T2 and T4_nodeblock (and the variants of Chapter 5.5) your YUV is byte-identical to ffmpeg's. You also parse the slice header, including `disable_deblocking_filter_idc`, `slice_alpha_c0_offset_div2` and `slice_beta_offset_div2`, and you keep for every macroblock its type, QP, slice, `transform_size_8x8_flag` and coded-block information.

Phase 6 adds the deblocking filter (8.7), which smooths the block edges that quantisation leaves behind. T3 and T4 are encoded with it on, so until it works they will not match ffmpeg.

This chapter builds the first half of the filter, the part that decides **where** to filter and **how strongly to start**. For every macroblock of every picture you decide which of its edges are filtered, and for every filtered edge you assign a **boundary strength** (bS) per 4-sample segment. You print the result as a text dump, and the harness compares it with a reference. Chapter 6.2 then adds the sample arithmetic itself.

## 2. Big picture

**Deblocking runs after the whole picture is reconstructed, never inside the prediction loop.** Intra prediction (Phase 5) predicts from the *unfiltered* neighbouring samples of the current picture. If you filter a macroblock as soon as it is reconstructed, the next macroblock predicts from filtered samples and your output drifts. Two designs work:

- reconstruct the whole picture into a buffer, then run a separate deblocking pass over that buffer (simplest; recommended);
- filter in a delayed fashion while keeping unfiltered copies of the rows and columns that prediction still needs (what fast decoders do).

For *inter* pictures the filtered picture is also what later pictures predict from (hence "in-loop"). That is a Phase 7 concern; for now every picture is intra.

**The filter works along edges.** Every 4x4 (or 8x8) transform block has four sides, and the quantisation error shows up as a small step across those sides. The filter looks at the samples on both sides of each edge and, if the step looks like a coding artefact rather than real picture content, smooths it. Two questions come first:

1. **Is this edge filtered at all?** (the *filterEdgeFlag* of the edge). Picture borders, slice settings and the 8x8 transform decide it.
2. **How strong should the filtering be?** This is the *boundary strength* bS, 0 to 4. 0 means no filtering, 4 means the strongest filter. It depends on how the two blocks beside the edge were coded.

Chapter 6.2 uses bS together with QP, the slice offsets and the sample values; this chapter only needs the macroblock-level facts.

## 3. Concepts in detail

### 3.1 Edges and segments of a macroblock

A luma macroblock is 16x16 samples. The filter treats it as a grid of 4x4 blocks (spec 8.7 describes it in terms of the transform blocks):

- **4 vertical edges**, at x = 0, 4, 8, 12 (counted from the left of the macroblock). Edge 0 is the macroblock's **left edge**, shared with the left neighbour; edges 1-3 are **internal**.
- **4 horizontal edges**, at y = 0, 4, 8, 12. Edge 0 is the **top edge**, shared with the macroblock above.

bS is not one number per edge but one per **segment of 4 sample lines**: for a vertical edge, segment s covers rows 4s..4s+3 of the macroblock; for a horizontal edge, columns 4s..4s+3. The reason is that bS depends on the 4x4 blocks on each side of the segment; with inter prediction (Phase 7) neighbouring segments of one edge can differ. For intra macroblocks all four segments of an edge are equal, but you still print four values, so the dump format will not change later.

So a macroblock has 4 edges x 4 segments = **16 bS values per direction** (32 in all), and 4 + 4 filterEdgeFlags.

### 3.2 Which edges are filtered (8.7)

The standard (8.7) derives three flags per macroblock: **filterLeftMbEdgeFlag** (vertical edge 0), **filterTopMbEdgeFlag** (horizontal edge 0) and **filterInternalEdgesFlag** (edges 1-3 of both directions); with the 8x8 transform, luma edges 1 and 3 are then skipped. For the current macroblock (a *frame* macroblock in a frame picture, which is all you have until Phase 10):

| Edge | Filtered unless ... |
| --- | --- |
| Left edge (vertical edge 0) | the macroblock is in column 0 of the picture; or `disable_deblocking_filter_idc` of **the current macroblock's slice** is 1; or that idc is 2 and the left macroblock is in a different slice |
| Top edge (horizontal edge 0) | the macroblock is in row 0; or idc is 1; or idc is 2 and the macroblock above is in a different slice |
| Internal edges (1-3, both directions) | idc is 1; or `transform_size_8x8_flag` is 1 and the edge is number 1 or 3 (the edges at 4 and 12 are not edges of any 8x8 transform block, edge 2 still is) |

The three values of `disable_deblocking_filter_idc` (7.4.3):

- **0**: filter everything, including edges between macroblocks of different slices.
- **1**: filter nothing in this slice's macroblocks.
- **2**: filter, but not across slice boundaries. The standard words this as "the macroblock mbAddrA (or mbAddrB) is not available", where availability (6.4.x, the same notion you used for intra prediction in 5.5) means inside the picture *and in the same slice*.

When the PPS has `deblocking_filter_control_present_flag` = 0, the slice header does not carry these fields and the idc is inferred to be 0. When idc is 1 the two offset fields are also absent.

Notes:

- The decision is always made by the slice of the **current** macroblock (the one whose left/top edge it is). A macroblock in an idc-0 slice filters its left edge even if the left neighbour's slice has idc 1; the filter then modifies samples of the neighbour. Likewise an idc-1 macroblock leaves its left edge alone even if the left neighbour is in an idc-0 slice, and that neighbour does not filter it either, because only the current macroblock's flags matter.
- Picture edges are never filtered: there are no samples on the other side. (A common bug is to filter x = 0 and read x = -1.)
- With the 8x8 transform the **chroma** edges are not affected (see 3.4).
- *Preview of Phase 10.* In field pictures and for field macroblocks of MBAFF frames, some top edges are filtered differently (an extra edge in MBAFF, a different left neighbour, bS 3 instead of 4, see below). None of that applies to frame macroblocks of frame pictures.

### 3.3 Boundary strength for intra macroblocks (8.7.2.1)

For a segment that is filtered, in an intra picture (every macroblock is Intra4x4, Intra8x8, Intra16x16 or I_PCM, all of which count as "intra"):

| The segment lies on ... | bS |
| --- | --- |
| a **macroblock edge** (edge 0 of either direction), frame macroblocks | **4** |
| an **internal edge** | **3** |
| a segment that is not filtered (flag 0) | 0 (dumped as 0) |

Notes:

- Every macroblock edge in an intra picture has an intra macroblock on at least one side (here: on both), which is exactly the condition for bS 4.
- *Field preview.* In field pictures (and for field macroblocks in MBAFF frames) a **horizontal** macroblock edge between intra macroblocks gets bS 3, not 4. Vertical edges keep 4. You do not need it yet.
- *Inter preview (Chapter 7.5, do not implement now).* The full rule is a ladder: bS 4 (intra, macroblock edge) / bS 3 (intra, internal edge) / **bS 2** if either 4x4 block beside the segment has non-zero transform coefficients / **bS 1** if the blocks use different reference pictures, a different number of motion vectors, or motion vectors that differ by 4 or more quarter-samples in x or by 4 (frame) in y / **bS 0** otherwise. That is why bS is kept per 4-sample segment and why "filterEdgeFlag 1 with bS 0" will become possible. Your dump format already allows it.
- Whether the filter then changes any sample depends on QP and the sample values (Chapter 6.2). bS does not.

### 3.4 Chroma edges (4:2:0)

Chroma has no bS of its own: the bS of a chroma sample line is the bS of the **corresponding luma line**. In 4:2:0 a chroma macroblock is 8x8, with edges at chroma x (or y) = 0 and 4 (transform blocks are 4x4 in chroma too):

| Chroma edge | At chroma position | Takes bS from luma edge | Each luma segment covers |
| --- | --- | --- | --- |
| vertical 0 / horizontal 0 | 0 | edge 0 (x = 0 / y = 0) | 2 chroma lines |
| vertical 1 / horizontal 1 | 4 | edge 2 (x = 8 / y = 8) | 2 chroma lines |

So chroma line k of a chroma edge uses luma segment k/2. Luma edges 1 and 3 have no chroma counterpart in 4:2:0. The chroma edge at 4 exists even when `transform_size_8x8_flag` is 1 (that flag only removes chroma edges in 4:4:4). In this chapter the dump contains luma edges only; the chroma mapping above is what you will use in 6.2. The harness validates it indirectly (section 6).

### 3.5 What stays out of this chapter

alpha, beta, tC0 and the sample arithmetic (6.2); QP averaging and the offsets (6.2); inter bS (7.5); field and MBAFF rules (Phase 10).

## 4. The project: exactly what to build

Add a **deblocking-decision pass** to your decoder, with a dump switch. For each picture of the stream, in decoding order (for these intra-only streams that is also output order), and for each macroblock in raster order:

1. Take the `disable_deblocking_filter_idc` of the macroblock's slice.
2. Decide the 4 vertical and 4 horizontal edge flags with the rules of 3.2.
3. Assign bS to every segment of every filtered edge (3.3). Non-filtered edges get flag 0 and bS 0.
4. Print one `MB` line (section 5).

Keep the pass a separate function that takes the stored per-macroblock facts (slice id, transform flag, intra flag) and returns edge flags and bS. In 6.2 you will call it before filtering each macroblock, and in 7.5 you will extend its bS rule with the inter ladder.

Also fix your pipeline's order of operations now: all macroblocks of the picture are reconstructed (unfiltered), then the deblocking pass runs over the picture. Do not filter yet; the output YUV of this chapter is still unfiltered.

**Verify (from the plan).** Print the bS grid of the first macroblocks and check by hand that the outer edge of the picture is never filtered: macroblock 0 has flag 0 on vertical edge 0 and on horizontal edge 0, and so do all macroblocks of column 0 (vertical) and row 0 (horizontal). The harness then checks every macroblock of every picture.

## 5. Harness input format

One text file per stream, produced by your decoder. Grammar, line oriented, tokens separated by single spaces (extra whitespace is tolerated, blank lines and lines starting with `#` are ignored):

```
PIC <n>
MB <addr> <mbx> <mby> FV <f0> <f1> <f2> <f3> FH <f0> <f1> <f2> <f3> V <16 numbers> H <16 numbers>
```

| Token | Meaning |
| --- | --- |
| `PIC <n>` | starts a picture. `n` = 0, 1, 2 ... counting pictures in decoding order. |
| `<addr>`, `<mbx>`, `<mby>` | macroblock address (`mby * PicWidthInMbs + mbx`), column, row. Every macroblock of the picture appears exactly once, in raster order (0, 1, 2 ...). |
| `FV f0..f3` | filterEdgeFlag (0 or 1) of vertical edges 0..3 (x = 0, 4, 8, 12). |
| `FH f0..f3` | the same for horizontal edges 0..3 (y = 0, 4, 8, 12). |
| `V` + 16 numbers | bS (0..4) of the vertical edges: edge 0 segment 0, 1, 2, 3, then edge 1 segment 0..3, edge 2, edge 3. Segment s = rows 4s..4s+3. |
| `H` + 16 numbers | the same for horizontal edges. Segment s = columns 4s..4s+3. |

A flag of 0 means the four bS values of that edge are 0. In this chapter a flag of 1 always comes with bS 3 or 4.

Example, the first macroblocks of T3 (single slice, deblocking on). Macroblock 0 has no left or top neighbour, macroblock 1 has a left neighbour:

```
PIC 0
MB 0 0 0 FV 0 1 1 1 FH 0 1 1 1 V 0 0 0 0 3 3 3 3 3 3 3 3 3 3 3 3 H 0 0 0 0 3 3 3 3 3 3 3 3 3 3 3 3
MB 1 1 0 FV 1 1 1 1 FH 0 1 1 1 V 4 4 4 4 3 3 3 3 3 3 3 3 3 3 3 3 H 0 0 0 0 3 3 3 3 3 3 3 3 3 3 3 3
```

Pictures in the file follow the order of the stream, all pictures of the stream (use `--pictures N` while debugging to have the harness look at the first N only; your file may be longer).

## 6. Running the harness

```
python3 test_bs.py <in.ts|in.264> <your_bs_dump.txt> [--pictures N]
```

Test streams:

- Recipes of `common/testfiles.py`: **T3** (deblocking on, single slice), **T3_dm3** and **T3_dp3** (slice offsets -3,-3 / +3,+3, so alpha/beta offsets appear in the slice header; bS does not depend on them but your header parsing must not break), **T4** (four slices, idc 0 across slice boundaries). Generate with `python3 -m common.testfiles T3 T3_dm3 T3_dp3 T4`. A stream of the *no-deblock* recipes (T2, T4_nodeblock) has idc 1 everywhere: the correct dump is all zeros.
- `python3 test_bs.py --make-variants DIR` writes more streams:
  - `bs_*` synthetic intra streams (a small bit-writer in `bsgen.py`): idc 0, 1 and 2 **mixed within one picture**, slices that start at row starts and in mid-row, single-macroblock slices, I_PCM macroblocks, offsets -6..+6, QP 0..51. x264 cannot write idc 2, so only these streams test it.
  - `x_*_8x8` x264 High-profile CAVLC streams with the 8x8 transform (Intra 8x8 macroblocks), with and without slices and offsets. Your Phase 4 parser must handle `transform_size_8x8_flag`; if your decoder cannot reconstruct Intra 8x8 yet (Phase 8) it can still produce this chapter's dump, which needs the flag only.
- `python3 test_bs.py --expected-out ref.txt <stream>` writes the reference dump so you can `diff` it with your own.

On a mismatch the harness prints the first differing macroblocks (picture, address, which edge, expected and actual flag and bS) and for the first one a context block: macroblock type, slice, idc, offsets, `transform_size_8x8_flag`, and whether the left and top neighbours are in another slice, plus a hint for the usual causes. Exit code 0 = pass, 1 = fail, 2 = missing or malformed dump or unsupported stream (CABAC, P/B slices, field/MBAFF: later phases).

### Where the truth comes from

ffmpeg has **no bS dump**. Checked on ffmpeg 8.0: the decoder's `-debug` flags are `pict`, `rc`, `bitstream`, `mb_type`, `qp`, `dct_coeff`, `skip`, `startcode`, `er`, `mmco`, `bugs`, `buffers`, `thread_ops`, `nomc` (none shows deblocking), `codecview` visualises motion vectors, QP and block types only, and `trace_headers` stops at the slice header. What ffmpeg does offer is the final picture, and `-skip_loop_filter all`, which decodes the same stream with the loop filter off.

So the harness computes the expected grid with an independent reference implementation of the rules (`spoiler_reference/bsref.py`) driven by the Phase 4 reference parser (types, `transform_size_8x8_flag`, QP, slice membership) and ffmpeg's `trace_headers` (idc and offsets). A reference nobody checked would be worth nothing, so it is **validated by consequence**, `python3 test_bs.py --validate-reference <stream>`:

1. decode the stream with ffmpeg and `-skip_loop_filter all` (the unfiltered picture),
2. run the reference filter (`spoiler_reference/deblock_ref.py`) on it, fed with the reference bS grid,
3. compare with ffmpeg's normal (filtered) output: it must be byte-identical.

This holds for T3, T3_dm3, T3_dp3, T4 (all 50 pictures; `--self-test` checks only their first 3 to stay fast) and for every synthetic and 8x8 stream. A wrong bS value almost always changes some pixel, so a byte-exact result means the grid is right *wherever it matters*.

Two limits of this argument. First, it only works for intra pictures: ffmpeg's unfiltered decode is exactly the picture before deblocking because intra prediction reads unfiltered samples (section 2); once inter pictures predict from filtered references (Phase 7), `-skip_loop_filter` changes the references too. Second, the bS grid and the reference filter are validated together, so in principle a bS error could be hidden by a matching filter error; the mutation tests below show that every plausible bS error that can move a pixel does move one.

**Where a bS error is invisible.** The filter changes samples only if the step across the edge is smaller than alpha, and the texture beside it smaller than beta (Chapter 6.2). Both come from a table indexed by QP plus the slice offsets: below index 16 alpha is 0 and the filter does nothing at all. Consequences, which shaped the synthetic streams:

- A **low QP** (say average QP + offset < 16) switches the filter off whatever bS says. Random noise at high QP fails the thresholds as well. So a plain noisy stream proves little; the synthetic streams use *calm* content (small levels, neighbouring blocks differing by a few steps) at QP from low to high, with offsets at both extremes, so the filter is active in most places.
- **Flat content** is unchanged by any filter.
- An I_PCM macroblock has qP = 0 for the filter: its *luma* internal edges are never changed, so a wrong bS there is invisible in the pixels (the chroma edges use a chroma QP and offset and can still change). Only the dump comparison catches that mistake.
- Edges on the picture boundary cannot be filtered at all, so "flag set on a picture edge" is invisible in pixels; again only the dump catches it.

`--self-test` makes this concrete: it applies 17 deliberately wrong rules (bS 3 on macroblock edges, bS 4 inside, idc 2 treated as 0 or 1, idc 0 treated as 2, idc 1 ignored, the 8x8 rule wrong, picture edge flagged, ...) and checks that every one produces a dump the harness rejects, that all but two of them also change the pixels (so the test streams are sensitive to them), and that the remaining two are invisible for the reasons given above (the I_PCM one is run through the filter to confirm it leaves the pixels unchanged; the picture-edge one cannot be fed to a filter at all). It also feeds 7 corrupted dump files (dropped macroblock, swapped V and H, reversed segments, column-major order, missing PIC lines ...) and checks that each is rejected.

## 7. Reading failures

- **Picture edge flagged** (expected flag 0 at x = 0 / y = 0): your edge test must come before everything else.
- **Expected 4, got 3** (or the reverse): bS 4 is for macroblock edges, bS 3 for internal edges; not the other way round, and not "4 for the left edge only".
- **Flag expected 0 at a slice boundary, you print 1**: this is idc 2. The test is "the neighbour is in a different slice", not "this is the first macroblock of a slice" (a macroblock in the middle of a row has a left neighbour in the same slice, but its top neighbour can be in another one).
- **Flag expected 1 at a slice boundary** with idc 0: slice edges are filtered unless idc is 2, and the idc used is the *current* macroblock's.
- **A whole picture all zeros** and idc 1 expected: fine. All zeros and idc 0: you read the idc field from the wrong slice (the first slice of the picture instead of the macroblock's own).
- **Mismatches only in the 8x8 streams**: internal edges 1 and 3 must be 0 for macroblocks with `transform_size_8x8_flag` 1; edge 2 stays. Check that you stored the flag per macroblock, and that it is 0 (not stale) for Intra16x16 and I_PCM.
- **Mismatches only after a slice change**: you are looking at the idc of a previous slice. Slices of one picture can differ in idc and offsets, and the synthetic streams do exactly that.
- Use `--expected-out` and a text `diff` to find the first line that differs; the harness's context block names the cause for the first mismatch.

## 8. What's next

Chapter 6.2 adds the filter itself: indexA/indexB, alpha, beta, tC0, the bS < 4 and bS = 4 filters for luma and chroma, applied edge by edge in the order the standard fixes, using the grid you built here.
