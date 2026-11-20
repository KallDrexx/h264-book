# Chapter 5.3 - Intra 16x16 and chroma prediction

## 1. Goal and the project

Chapter 5.1 gave you frame buffers and Chapter 5.2 turned levels into residual samples. This chapter adds the missing half of reconstruction: the **prediction**. For every macroblock you build a guess of its samples from already decoded neighbours, add the residual, clip, and store the result. When this works you can see your first real decoded pictures, which is why this is the first picture milestone.

You will build:

1. The four **Intra16x16** luma prediction modes (spec 8.3.3): vertical, horizontal, DC, plane.
2. The four **chroma** prediction modes (8.3.4): DC, horizontal, vertical, plane. Chroma numbers its modes differently from luma.
3. The neighbour-availability logic that decides which samples a prediction may read (picture edges, slice edges).
4. The reconstruction loop: for each macroblock, parse, predict, add the residual of Chapter 5.2, clip, store, then move to the next macroblock.
5. Your reader now writes real pictures to `out.yuv` for every macroblock type except I_NxN luma (Intra 4x4 is Chapter 5.4).

**An important honest note about T1.** The Summary says T1 should decode byte-identical after this chapter. It will not, and the reason is not your fault. T1 is encoded with `partitions=none`, yet x264 still codes about a quarter of its macroblocks (4789 of 19800) as I_NxN, which needs Intra 4x4 prediction. Chapter 4.1 hinted at this. So this chapter's verification is built differently, in three layers (section 6): synthetic streams made only of Intra16x16 macroblocks, a real x264 recipe that produces only Intra16x16 macroblocks, and a sound "ignore what is not decidable yet" mode for T1. Byte-identical T1 is the milestone of Chapter 5.4.

**Spoiler warning:** `intraref.py` and `refdecode.py` in this folder are a complete solution, shipped for the harness's self-test. Do not open them before you have written your own (section 6).

## 2. Big picture

An intra picture is coded without reference to any other picture. To make that cheap, the encoder predicts each macroblock from the samples just left of and just above it, which the decoder has already reconstructed, and transmits only the difference (the residual, Chapter 5.2). Smooth areas are predicted almost perfectly and cost almost nothing.

Intra16x16 predicts the whole 16x16 luma block from one row of 16 samples above (B), one column of 16 samples to the left (A) and the corner sample above-left (D). It suits flat or smoothly changing areas. Four modes cover "continue the top row downwards", "continue the left column to the right", "one flat value", and "a tilted plane". Chroma (8x8 per component) works the same way with its own four modes, and it is chosen independently of the luma mode: the macroblock carries one `intra_chroma_pred_mode` that applies to both Cb and Cr.

What the decoder must get exactly right:

- **Which neighbours exist.** A sample is usable only if its macroblock is inside the picture, in the same slice, and already decoded. Modes that need a missing neighbour never appear in a valid stream; DC has a rule for every combination.
- **The unfiltered samples.** Prediction reads reconstructed samples as they were before the deblocking filter. In Phase 5 there is no deblocking at all; in Phase 6, deblock after the whole picture has been reconstructed (or keep unfiltered copies), never in place while decoding.
- **The arithmetic.** Rounding and the constants of the plane mode are fixed by the standard.

## 3. Concepts in detail

Notation: `p[x, -1]` is the sample directly above column `x` of the block, `p[-1, y]` the sample left of row `y`, `p[-1, -1]` the corner above-left. `(x, y)` inside a block counts from its top-left sample; `x` is the column. All samples are 8-bit, `Clip1(v) = min(max(v, 0), 255)`, `>>` is an arithmetic right shift.

### 3.1 Neighbour availability (6.4.9, 8.3.3, 8.3.4)

For the current macroblock `mbAddr` (column `mbX = mbAddr mod w`, row `mbY = mbAddr / w`, `w` = PicWidthInMbs) define three flags:

| Flag | Macroblock | Available when |
| --- | --- | --- |
| A (left) | `mbAddr - 1` | `mbX > 0` and that macroblock belongs to the same slice |
| B (above) | `mbAddr - w` | `mbY > 0` and same slice |
| D (above-left) | `mbAddr - w - 1` | `mbX > 0`, `mbY > 0` and same slice |

"Same slice" means the macroblock was decoded as part of the current slice, not merely that its address is smaller. Keep an array with one entry per macroblock of the picture holding the number of the slice that decoded it, reset at the start of every picture; compare it with the current slice's number. Two things follow:

- A slice that starts in the middle of a row makes the macroblock above (at `mbAddr - w`) belong to the *previous* slice for the first `w` macroblocks of the slice. B is then unavailable even though the macroblock is inside the picture. This is the most common availability bug.
- If D is available then A and B are too (slices are consecutive in raster order). A with B but without D happens when a slice starts exactly at the macroblock above the current one. This matters for plane prediction.

Five combinations of (A, B, D) occur in practice: none (first macroblock of a slice), A only (first row of a slice), B only (leftmost column), A and B without D, and all three. The top-right macroblock is never used by Intra16x16 or chroma prediction (only Intra 4x4, Chapter 5.4, looks there).

Intra pictures with `constrained_intra_pred_flag = 1` change nothing here, because every neighbour is intra. That flag matters when inter macroblocks exist (Chapter 5.5).

A mode that needs an unavailable neighbour is a stream error. Detect it and stop with a message; do not guess.

### 3.2 Intra16x16 modes (8.3.3)

`Intra16x16PredMode` comes from `mb_type` (Chapter 4.1): for `mb_type` 1..24, `mode = (mb_type - 1) mod 4`.

| Mode | Name | Needs | Prediction `pred[x, y]` for 0 <= x, y <= 15 |
| --- | --- | --- | --- |
| 0 | Vertical | B | `p[x, -1]` |
| 1 | Horizontal | A | `p[-1, y]` |
| 2 | DC | nothing | one value for the whole block, below |
| 3 | Plane | A, B and D | a tilted plane, below |

**DC (mode 2).** Let `S_top = sum of p[x, -1]` for x = 0..15 and `S_left = sum of p[-1, y]` for y = 0..15.

| A | B | Value |
| --- | --- | --- |
| yes | yes | `(S_top + S_left + 16) >> 5` |
| yes | no | `(S_left + 8) >> 4` |
| no | yes | `(S_top + 8) >> 4` |
| no | no | 128 |

Example: top row all 100, left column all 60, both available: `(1600 + 960 + 16) >> 5 = 2576 >> 5 = 80`. The `+ 16` makes the shift round to nearest; with only the left column available the value would be `(960 + 8) >> 4 = 60`.

**Plane (mode 3).** Fit a tilted plane through the border samples. First a horizontal and a vertical gradient:

```
H = sum for x' = 0..7 of (x' + 1) * (p[8 + x', -1] - p[6 - x', -1])
V = sum for y' = 0..7 of (y' + 1) * (p[-1, 8 + y'] - p[-1, 6 - y'])
```

When `x' = 7` the second term is `p[-1, -1]`, the corner (index 6 - 7 = -1); same for `y' = 7`. Then

```
a = 16 * (p[-1, 15] + p[15, -1])
b = (5 * H + 32) >> 6
c = (5 * V + 32) >> 6
pred[x, y] = Clip1((a + b * (x - 7) + c * (y - 7) + 16) >> 5)
```

`H` and `V` can be negative; the shifts are arithmetic. The clip is needed: `a` is the value at the bottom-right, and with strong gradients the plane leaves 0..255.

Worked example. Top row `p[x, -1] = 100 + 2x` (100 .. 130), left column `p[-1, y] = 100 + 3y` (100 .. 145), corner 98. Then `H = 816`, `V = 1216`, `a = 16 * (145 + 130) = 4400`, `b = (4080 + 32) >> 6 = 64`, `c = (6080 + 32) >> 6 = 95`. So `pred[0, 0] = (4400 - 7*64 - 7*95 + 16) >> 5 = 103`, `pred[15, 0] = 133`, `pred[0, 15] = 147`, `pred[15, 15] = 177`. (With the same border, DC gives 119 everywhere.)

### 3.3 Chroma modes (8.3.4, 4:2:0)

The macroblock has one `intra_chroma_pred_mode` (0..3) used for both Cb and Cr; each 8x8 component block is predicted separately from its own plane's neighbours. **The numbering differs from luma:**

| Mode | Name | Needs |
| --- | --- | --- |
| 0 | DC | nothing |
| 1 | Horizontal | A |
| 2 | Vertical | B |
| 3 | Plane | A, B and D |

Horizontal: `pred[x, y] = p[-1, y]`. Vertical: `pred[x, y] = p[x, -1]`, for 0 <= x, y <= 7. Availability flags A, B, D are those of the macroblock (3.1) and apply to both components.

**DC (mode 0).** The 8x8 block is treated as four 4x4 blocks, numbered 0 (top-left), 1 (top-right), 2 (bottom-left), 3 (bottom-right). Each gets its **own** DC value from only the neighbouring samples next to it. For block at offset `(xO, yO)` (xO, yO in {0, 4}), let `S_top` = sum of the four samples `p[xO .. xO+3, -1]` and `S_left` = sum of the four samples `p[-1, yO .. yO+3]`.

| Block | Rule |
| --- | --- |
| 0 (0,0) and 3 (4,4) | A and B: `(S_top + S_left + 4) >> 3`. Only A: `(S_left + 2) >> 2`. Only B: `(S_top + 2) >> 2`. Neither: 128. |
| 1 (4,0) | B available: `(S_top + 2) >> 2`. Else A available: `(S_left + 2) >> 2`. Else 128. |
| 2 (0,4) | A available: `(S_left + 2) >> 2`. Else B available: `(S_top + 2) >> 2`. Else 128. |

Why the asymmetry: block 1 touches the top neighbours directly and is far from the left ones, so it prefers the top row; block 2 prefers the left column. Block 3 touches neither border directly and uses both. Note that block 3 uses the top samples `x = 4..7` and the left samples `y = 4..7`, not the samples next to block 0.

Example with top row `10 20 30 40 50 60 70 80` and left column `100 110 ... 170`, A and B available: block 0: `(100 + 460 + 4) >> 3 = 70`; block 1: top only `(260 + 2) >> 2 = 65`; block 2: left only `(620 + 2) >> 2 = 155`; block 3: `(260 + 620 + 4) >> 3 = 110`. With only A available the result is `115 / 115 / 155 / 155` (blocks 0, 1, 2, 3); with only B, `25 / 65 / 25 / 65`; with neither, 128 everywhere.

**Plane (mode 3).** For 4:2:0 the constants are fixed (the spec's `xCF` and `yCF` are 0):

```
H = sum for x' = 0..3 of (x' + 1) * (p[4 + x', -1] - p[2 - x', -1])
V = sum for y' = 0..3 of (y' + 1) * (p[-1, 4 + y'] - p[-1, 2 - y'])
a = 16 * (p[-1, 7] + p[7, -1])
b = (34 * H + 32) >> 6
c = (34 * V + 32) >> 6
pred[x, y] = Clip1((a + b * (x - 3) + c * (y - 3) + 16) >> 5)
```

Again the second term for `x' = 3` is the corner `p[-1, -1]`. The constant 34 is specific to 4:2:0. To match this with the spec's general text: there `xCF = 4 * (chroma_format_idc == 3)` and `yCF = 4 * (chroma_format_idc != 1)` widen the sums for 4:2:2 and 4:4:4, `b = ((34 - 29 * (chroma_format_idc == 3)) * H + 32) >> 6`, `c = ((34 - 29 * (chroma_format_idc != 1)) * V + 32) >> 6` and `a = 16 * (p[-1, MbHeightC - 1] + p[MbWidthC - 1, -1])`. With `chroma_format_idc = 1` (4:2:0) all of that reduces to the lines above. (For 4:4:4 the factor becomes 5, the same as luma, because the chroma block is then 16x16.) Example: top `10 20 ... 80`, left `100 110 ... 170`, corner 5: `H = 580`, `V = 940`, `a = 4000`, `b = 308`, `c = 499`, `pred[0, 0] = 49`, `pred[7, 0] = 117`, `pred[0, 7] = 159`, `pred[7, 7] = 226`.

### 3.4 Prediction plus residual, and the order of work

For one macroblock:

1. Compute the availability flags A, B, D (3.1).
2. Luma: `pred` from the Intra16x16 mode; the residual is the 16x16 result of your `i16_luma_residual` of Chapter 5.2, called with the macroblock's QP and its DC and AC level arrays; write `Clip1(pred + residual)` into the luma plane.
3. Chroma: for each of Cb and Cr: `pred` 8x8 from the chroma mode; residual from `chroma_residual` of Chapter 5.2 with the chroma QP of that component; write `Clip1(pred + residual)`.
4. Only then go to the next macroblock. Its prediction reads what you just wrote.

Facts to wire up from Phase 4 and Chapter 5.2:

- **Levels.** Intra16x16 delivers a DC block (16 levels) always, 16 AC blocks of 15 levels only when the luma CBP is 15 (otherwise all AC are zero, but the DC still goes through the transform path). Chroma: CBP chroma 0 = no chroma levels at all (residual is zero, prediction alone), 1 = DC levels only, 2 = DC and AC. Missing blocks are all-zero arrays. Levels are in scan order; Chapter 5.2 explains the placement.
- **QP.** `QP_Y` is the running QP of the slice: it starts at the slice's `SliceQPY` and changes by `mb_qp_delta` in every macroblock that has one (always for Intra16x16), wrapping modulo 52. I_PCM macroblocks have no `mb_qp_delta` and leave the running QP unchanged. Chroma QP: Chapter 5.2's `chroma_qp(QP_Y, offset)` with `chroma_qp_index_offset` for Cb and `second_chroma_qp_index_offset` for Cr.
- **I_PCM.** Copy the raw samples (Chapter 5.1). They then serve as neighbours like any other samples.
- **I_NxN macroblocks.** Their chroma is predicted exactly like any other macroblock's: it needs only `intra_chroma_pred_mode`, the chroma CBP and the chroma levels, all parsed by Phase 4. Do it now. Their luma needs Intra 4x4 (5.4): until then fill their 16x16 luma with 128 (or anything). Do not stop and do not skip the macroblock: chroma neighbours depend on it.
- **Cropping and output** are unchanged from Chapter 5.1.

The reconstruction touches only the frame buffer you already have. A clean structure is one function per component: "predict this block into a small array, add the residual array, clip, store".

## 4. The project: exactly what to build

Extend your reader (`reader <in.ts> <out.yuv>`) so that:

1. For every Intra16x16 macroblock, the luma is predicted (8.3.3), the residual added and the result stored.
2. For every non-I_PCM macroblock (Intra16x16 and I_NxN), Cb and Cr are predicted (8.3.4) and reconstructed with their residuals.
3. Availability uses a per-picture "slice number of each macroblock" array as in 3.1.
4. I_NxN luma stays a placeholder (128) until Chapter 5.4.
5. A mode that needs an unavailable neighbour makes the reader exit non-zero with a message.

Keep the prediction functions pure: `(plane, position, mode, availability)` in, a block out. Chapter 5.4 adds a third function in the same style, and Chapter 5.5 adds a variation (constrained intra prediction) that only changes the availability inputs.

Verify. The Summary's wording is "T1 decodes byte-identical to ref.yuv". As explained in section 1, this chapter's verification has three layers, all run by the harness against ffmpeg: (a) synthetic Intra16x16 streams, byte-identical required; (b) a real x264 stream with only Intra16x16 macroblocks (`-preset ultrafast`), byte-identical required; (c) T1 in `--only-i16` mode: every chroma sample and every provably decidable luma sample must match. If (a) and (b) pass, the prediction part of Chapter 5.4 is the only thing between you and a byte-identical T1.

## 5. Harness input format

The only file your reader produces is `out.yuv`, exactly as in Chapter 5.1: raw planar yuv420p, no header, one **cropped** frame per decoded picture in decode order (equal to display order for these streams). The harness derives everything else itself. The reader's input may be the `.ts` file or the raw `.264` (the harness accepts both; use whichever your reader supports).

## 6. Running the harness

Create the test streams once (into the git-ignored `.cache`, or any folder):

```
python3 test_intra16.py --make-variants ../.cache/v53
```

| Stream | Content | Frames | Tests |
| --- | --- | --- | --- |
| `i16_edges` | 6x5 macroblocks, one slice per picture, QP 15..40 | 6 | picture edges, all modes, all CBPs |
| `i16_rows` | same size, one slice per macroblock row | 5 | B unavailable at every row start |
| `i16_ragged` | 7x6, slices of random length (they start mid-row), a few I_PCM | 8 | the above-macroblock-in-another-slice case, A without D, I_PCM neighbours |
| `i16_qp` | 5x4, every QP 0..51, random chroma QP offsets (-12..12) for Cb and Cr | 10 | QP chain, chroma QP |
| `i16_pcm` | 6x5, 20% I_PCM macroblocks with ramps and noise | 6 | plane prediction from steep neighbours, clipping |
| `i16_crop` | 5x4 coded, cropped to 74x56 on all sides | 4 | prediction with cropping |
| `i16_big` | 22x18, ragged slices | 3 | a CIF-sized sanity run |
| `x264_i16` | real x264 stream, T1's testsrc2 content, `-preset ultrafast`, QP 26 | 25 | only Intra16x16 macroblocks |
| `x264_i16_slices` | same, 4 slices per picture | 25 | real encoder + slices |
| `x264_i16_smooth` | smooth generated gradients (many plane-mode macroblocks) | 25 | plane modes of a real encoder |
| `x264_mixed` | the smooth content with an ordinary preset, QP 30, 4 slices: about 4% I_NxN | 25 | for `--only-i16` |

The synthetic streams are written by `predgen.py`, an encoder-side bit writer that shares no code with the decoder-side helpers. It draws, for every macroblock, an Intra16x16 mode and a chroma mode that are legal for the macroblock's neighbour availability, a random CBP, random levels, a random QP (the mb_qp_delta chain included), and writes valid CAVLC. It does not compute the pictures; ffmpeg's decode of the stream is the truth. The harness `--make-variants` run checks that every legal (mode x availability) combination occurs in the synthetic streams (every luma mode, every chroma mode, each under A / B / D combinations).

The x264 recipe (finding a real stream with only Intra16x16): `-preset ultrafast`. That preset switches x264's mode analysis off, which removes Intra 4x4 from the encoder's choices, so every macroblock is Intra16x16 on any content, T1's included: `ffmpeg -f lavfi -i testsrc2=size=352x288:rate=25 -t 1 -pix_fmt yuv420p -c:v libx264 -preset ultrafast -qp 26 -profile:v baseline -x264-params keyint=1:no-deblock=1:threads=1 -f mpegts out.ts`. `partitions=none` alone is not enough (T1 still gets 25% I_NxN; the other presets from `superfast` up choose Intra 4x4 freely), and `-tune` options do not help. `x264_mixed` uses an ordinary preset on smooth content, where only a few macroblocks prefer Intra 4x4. The harness's self-test verifies, with `common/cavlc_ref.py`'s parser, that the three `x264_i16*` streams contain no I_NxN macroblock, and every run prints a macroblock-type summary.

Run your reader on a stream, then the harness:

```
<your reader> ../.cache/v53/i16_ragged.ts out.yuv
python3 test_intra16.py ../.cache/v53/i16_ragged.ts out.yuv
```

What is compared: ffmpeg's output (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`, cropped) against `out.yuv`, via `common/yuvcmp.py` (first differing frame, plane, macroblock, pixel, values, per-frame counts, macroblock map). On failure the harness adds:

- a table of "which macroblock types are wrong": for luma, per Intra16x16 mode (and I_PCM, I_NxN); for chroma, per chroma mode (and I_PCM, I_NxN), as `checked / wrong`;
- the first wrong luma macroblock and the first wrong chroma macroblock, described with everything you need to reproduce it: address and position, the address where its slice starts, availability of A, B and D, its modes, CBP and QP.

Expected results: on all `i16_*` and `x264_i16*` streams the full comparison must PASS now. (A free bonus: the `x264_i16` stream is T1's picture content, so you can look at your first real decoded video with the ffplay command of Chapter 5.1.)

### The T1 situation and `--only-i16`

On any stream containing I_NxN macroblocks (T1, T2, T4, `x264_mixed`) the default full comparison fails until Chapter 5.4, and says so. Use `--only-i16`:

```
python3 test_intra16.py ../.cache/T1.ts out.yuv --only-i16
```

It does not compare the luma of an I_NxN macroblock, nor the luma of an Intra16x16 macroblock whose prediction mode reads (A for horizontal, B for vertical, A and B for DC, A, B and D for plane, counting only available neighbours) the luma of an ignored macroblock, in raster order, transitively. Everything else is compared, **chroma entirely**, because chroma prediction and residual never depend on luma, and I_NxN macroblocks still have a normal chroma. This is sound: a correct reader cannot fail it, since every compared sample is a function of parsed syntax and of other compared samples; and the harness's self-test shows it still catches errors in compared samples. It is also honest about its power: it prints how much luma was compared.

- On `x264_mixed`: luma is compared in about 43% of the macroblocks, chroma in all.
- On T1: **luma in 0%**. The first macroblock of every T1 picture is I_NxN, and the dependence spreads along every row and column. So for T1 the check is a complete chroma check (all 19800 macroblocks, all four chroma modes, every availability case of a real encoder) and the harness prints a warning that no luma was compared. That is still valuable: wrong chroma DC rules, plane constants or chroma QP all show up. Full T1 is Chapter 5.4's pass/fail.

Exit code: 0 pass, 1 fail, 2 missing or unusable reader output.

**How the harness checks itself.** `python3 test_intra16.py --self-test` (about a minute) runs a reference reader on every stream and requires it to pass. It then requires these to fail: one wrong luma or Cr sample, Cb and Cr swapped, a truncated file, and eight deliberately wrong prediction rules (no clip in luma plane, wrong luma and chroma plane constants, wrong DC rounding, wrong chroma DC preference for blocks 1, 2 and 3, chroma horizontal and vertical swapped). It also checks the `--only-i16` rules in both directions: a wrong luma sample in a macroblock that depends on an I_NxN one is ignored, while a wrong luma sample in a checked macroblock, or a wrong chroma sample in an I_NxN macroblock, fails.

> **Spoiler warning.** This folder contains a complete solution to this chapter: `intraref.py` (the prediction formulas of section 3 in executable form) and `refdecode.py` (the reference reader, built on `common/cavlc_ref.py`, `intraref.py` and Chapter 5.2's `transref.py`). The harness needs them for `--self-test`, but writing this code yourself is the exercise. Do not open them until you have tried, and then only to compare once a test has failed and the debugging tips below did not help. `predgen.py` (the stream generator) is encoder-side and computes no predictions; it does contain the availability rule of section 3.1, which it needs to choose legal modes.

## 7. Reading failures / debugging tips

Start from the "macroblock types with wrong samples" table and the first wrong macroblock line.

| Symptom | Likely cause |
| --- | --- |
| Whole stream wrong from macroblock 0, luma only | forgot to add the residual, or `QP` not the running QP, or 5.2 DC path not used for Intra16x16 |
| Wrong only in macroblocks at the start of a slice in the middle of a row | availability: you test only "inside the picture" instead of "same slice" (B above belongs to the previous slice) |
| Wrong only in macroblocks of the leftmost column or top row | edge availability, or reading a sample outside the picture (index -1 wraps in some languages) |
| Luma mode 2 (DC) wrong by 1 or 2 in a few cases | rounding: `+ 16 >> 5` for both sides, `+ 8 >> 4` for one side |
| Luma mode 3 wrong, small differences growing toward the bottom right | `H` or `V` missing the corner term, off-by-one in `x - 7`, constant 5 |
| Luma mode 3 wrong only at strong edges | forgot `Clip1` |
| Chroma wrong where horizontal or vertical is used | you reused the luma numbering: for chroma 0 = DC, 1 = horizontal, 2 = vertical, 3 = plane (luma: 0 = vertical, 1 = horizontal, 2 = DC, 3 = plane) |
| Chroma mode 0 wrong in blocks 1 or 2 only, only on edge macroblocks | the block 1 / block 2 preference rule (top first / left first) |
| Chroma mode 0 wrong in block 3 | block 3 must use the sums at `x = 4..7` and `y = 4..7` |
| Chroma plane wrong | constant 34 (not 5), or the `x - 3` offsets, or `p[-1, 7] + p[7, -1]` in `a` |
| Chroma wrong only when `chroma_qp_index_offset != 0`, or Cb and Cr differ | chroma QP mapping and offsets (5.2) |
| Only chroma of I_NxN macroblocks wrong | you skipped I_NxN macroblocks entirely instead of reconstructing their chroma |
| Wrong right after an I_PCM macroblock | QP chain (I_PCM leaves the running QP alone), or PCM samples not stored into the planes before the next prediction |
| `i16_*` pass but `x264_i16` fails | something content-dependent: large levels, clipping, `mb_qp_delta` wrap, 16-bit overflow in your arithmetic (use 32 bits) |

Use `common/yuvcmp.py` map to see the pattern: one wrong macroblock then a spread right/down is normal (errors propagate by prediction); the first wrong macroblock in raster order is the cause. For the first wrong macroblock, dump your prediction block and compare it with a hand calculation using the formulas above.

## 8. What's next

Chapter 5.4 adds Intra 4x4 prediction for I_NxN macroblocks. Then T1 and T2 decode byte-identical, and `--only-i16` is no longer needed.
