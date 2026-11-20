# Chapter 5.5 - QP tracking and multiple slices

> **SPOILER WARNING: do not open `spoiler_reference/`** (`refdecode.py`, `intraref.py`, `intra4ref.py`, `transref.py`). `refdecode.py` there is a complete reference reader, shipped only for the harness's self-test; the other three are the solutions of Chapters 5.2 to 5.4. Write your own first, and look at them only to compare after a test has failed and section 7 did not help. `qsgen.py` (the stream generator) computes no pictures, but it contains its own writer-side implementation of this chapter's QP rules, so read it only after your reader passes.

## 1. Goal and the project

Chapters 5.1 to 5.4 built every piece of an intra picture decoder: frame buffers, inverse quantisation and transforms, Intra16x16, chroma and Intra4x4 prediction. Two things were treated as "already handled" along the way: the **quantisation parameter** (QP) of each macroblock, and the fact that a picture can be split into several **slices**. This chapter makes both exact and closes Phase 5.

You will build:

1. **QP tracking** (spec 7.4.5): the running luma QP of a slice, the `mb_qp_delta` update with its wrap-around, the macroblocks that carry no `mb_qp_delta`, and the QP of an I_PCM macroblock.
2. **Chroma QP per macroblock** (8.5.8 / Chapter 5.2): derived from each macroblock's luma QP and the two PPS offsets.
3. **Slice-aware decoding of whole pictures**: a per-macroblock slice map, a QP chain that restarts at every slice, and neighbour availability (samples, Intra4x4 mode prediction, `nC`) that stops at slice boundaries, including slices that start in the middle of a picture row.
4. **`constrained_intra_pred_flag`**: parsed, carried to the availability logic, and understood (it changes nothing in an intra-only picture; it matters from Phase 7 on).
5. A complete reader for intra-only CAVLC streams of any number of slices per picture and any number of pictures.

**Milestone: after this chapter T1, T2 and T4_nodeblock decode byte-exact** (T1 and T2 were already exact after 5.4; this chapter re-verifies them and adds the multi-slice and QP-varying streams). The Summary's T4 has deblocking on, so the Phase 5 version is `T4_nodeblock`; the real T4 matches after Phase 6. A T2 variant encoded with `-crf 30` (`T2_crf30`) exercises QP deltas.

## 2. Big picture

**QP, in one paragraph.** The encoder chooses how coarsely each macroblock's residual is quantised: a small QP keeps detail and costs bits, a large QP throws detail away. Sending a full QP number for every macroblock would be wasteful, because neighbouring macroblocks usually have similar QPs. So the stream sends only the **difference** to the previous macroblock (`mb_qp_delta`), and only when the macroblock has a residual at all (Intra16x16 macroblocks always send it). The decoder keeps a running value and adds the differences. Everything hangs on keeping that running value exactly right: a QP that is off by one scales every coefficient of the macroblock wrongly and, through prediction, damages everything after it.

**Slices, in one paragraph.** A slice is a run of macroblocks (in raster order here) that can be decoded without looking at any other slice of the picture. That is its purpose: error resilience and parallelism. Two rules make a slice self-contained, and they are the whole of this chapter's second half. First, the QP chain restarts: every slice header carries its own starting QP. Second, macroblocks of other slices are **unavailable** as neighbours: no prediction samples, no mode prediction, no `nC` context, even though they are right next to the current macroblock in the picture.

What the decoder must get exactly right:

- **The update formula**, with wrap-around: the stream can legitimately say "+5" at QP 50 and mean QP 3.
- **Which macroblocks change the running QP**: only those that carry `mb_qp_delta`. I_PCM, and I_NxN with CBP 0, do not.
- **The restart** of the chain at every slice start, and the use of the slice's own starting QP.
- **The availability map** per slice, including slices that start mid-row (the macroblock above is then in the previous slice, but the one to the left is not, and so on).

## 3. Concepts in detail

### 3.1 The slice's starting QP (7.4.3, 7.4.5)

The picture parameter set carries `pic_init_qp_minus26` and the slice header carries `slice_qp_delta`. The slice's starting QP is

```
SliceQP_Y = 26 + pic_init_qp_minus26 + slice_qp_delta
```

It must lie in 0..51 (8-bit video). Different slices of the same picture may have different `slice_qp_delta`, so different starting QPs. Chapter 3.3 parsed both fields; Chapter 4.2's parser already used the sum.

### 3.2 The running QP and `mb_qp_delta` (7.3.5, 7.4.5)

Define for every macroblock of a slice `QP_Y` (its luma QP, used for its luma residual and as the base of its chroma QPs) and `QP_Y,PRED` (the value before the macroblock):

- `QP_Y,PRED` of the **first macroblock of a slice** is `SliceQP_Y`.
- `QP_Y,PRED` of every other macroblock is the `QP_Y` of the **previous macroblock in decoding order in the same slice**.

When the macroblock has an `mb_qp_delta` syntax element:

```
QP_Y = ((QP_Y,PRED + mb_qp_delta + 52 + 2 * QpBdOffset_Y) % (52 + QpBdOffset_Y)) - QpBdOffset_Y
```

For the 8-bit video of Phase 5, `QpBdOffset_Y = 0` and the formula is simply `QP_Y = (QP_Y,PRED + mb_qp_delta + 52) % 52`, with `%` the non-negative remainder. (Higher bit depths make `QpBdOffset_Y = 6 * (bit_depth - 8)`; they are outside this book's scope (8-bit 4:2:0 only, see `Summary.md`), but keeping it as a variable that is 0 costs nothing.) `mb_qp_delta` itself is limited to `-(26 + QpBdOffset_Y/2) .. +(25 + QpBdOffset_Y/2)`, i.e. -26..+25.

Why a wrap-around and not a clamp: a difference of -26..+25 reaches every QP from every QP only if the arithmetic is cyclic. Examples (8-bit):

| QP_Y,PRED | mb_qp_delta | sum | QP_Y |
| --- | --- | --- | --- |
| 30 | +4 | 34 | 34 |
| 50 | +5 | 55 | **3** (wraps up past 51) |
| 2 | -5 | -3 | **49** (wraps down past 0) |
| 0 | -26 | -26 | 26 |
| 51 | +25 | 76 | 24 |
| 26 | -26 | 0 | 0 |

A decoder that clamps to 0..51 gets rows 2 to 5 wrong; a decoder that does not wrap at all uses a QP outside the table (and normally crashes or reads garbage). Real encoders hardly ever need the wrap (they use small steps), but it is legal, conformance streams use it, and the synthetic streams of this chapter use it a lot.

**Which macroblocks have `mb_qp_delta`** (7.3.5):

| Macroblock | `mb_qp_delta` present? |
| --- | --- |
| Intra16x16 (all 24 `mb_type` values) | always, even with no coefficients at all |
| I_NxN (Intra4x4) | only if `coded_block_pattern > 0` |
| I_PCM | never |
| (P_Skip / B_Skip, later) | never |

Where it is absent, it is **inferred to be 0** (7.4.5: "When mb_qp_delta is not present for any macroblock (including P_Skip and B_Skip macroblock types), it shall be inferred to be equal to 0"). So `QP_Y = QP_Y,PRED`: the macroblock simply passes the running QP on. For an I_NxN macroblock with CBP 0 there is no residual, so its own QP is not used for any scaling, but it still matters as the running QP for the next macroblock.

### 3.3 The I_PCM subtlety

I_PCM macroblocks cause the one confusion in this chapter, because two different "QP of an I_PCM macroblock" exist.

- **For the QP chain (this chapter):** an I_PCM macroblock has no `mb_qp_delta`, so it is inferred to be 0 and, as for any macroblock, `QP_Y = QP_Y,PRED + 0`. The next macroblock's `QP_Y,PRED` is, by 7.4.5, "the luma quantisation parameter of the previous macroblock in decoding order in the current slice", which is that unchanged value: **the running QP passes through an I_PCM macroblock unchanged**. The macroblock after an I_PCM macroblock has the QP_Y,PRED the I_PCM macroblock itself had. It is neither reset to 0 nor reset to `SliceQP_Y`.
- **For the deblocking filter (Phase 6, 8.7.2.2):** when the filter needs the QP of an I_PCM macroblock, it uses `qPp = 0` (the 0 is substituted in the filter's formulas; it is not stored into the chain). This is why ffmpeg's `-debug qp` map shows 0 for I_PCM macroblocks.

So keep **two** things per macroblock if you want to be safe: the running QP (the chain; unaffected by I_PCM) and the QP the deblocking filter will use (`QP_Y`, but 0 for I_PCM). In this phase only the first matters; remember the second for Phase 6.

How this was checked, so you do not have to take it on trust: on streams where many I_PCM macroblocks are directly followed by macroblocks with and without `mb_qp_delta`, the harness's self-test (a) compares this pass-through chain with ffmpeg's `-debug qp` map (which shows 0 for I_PCM macroblocks and `QP_Y` for all others) and finds every macroblock in agreement, and (b) decodes the same streams to pixels and requires byte equality with ffmpeg. A reader that sets the chain to 0, or back to `SliceQP_Y`, after an I_PCM macroblock fails on such streams (see the mutants in section 6).

### 3.4 Chroma QP (8.5.8, details in Chapter 5.2)

Each macroblock's chroma QPs come from its luma `QP_Y` and the PPS offsets; nothing about them is carried forward:

```
qPI  = Clip3(0, 51, QP_Y + offset)       offset = chroma_qp_index_offset (Cb), second_chroma_qp_index_offset (Cr)
QP_C = qPI              if qPI < 30
     = Table 8-15(qPI)  otherwise (29, 30, 31, 32, 32, 33, 34, 34, 35, 35, 36, 36, 37, 37, 37, 38, 38, 38, 39, 39, 39, 39 for qPI = 30..51)
```

Common bugs: using `QP_Y` of the slice or of the previous macroblock instead of this macroblock's `QP_Y`; forgetting the clip when `QP_Y + offset` is negative or above 51; skipping the table; using the Cb offset for Cr. If the PPS has no High-profile extension, `second_chroma_qp_index_offset` is inferred equal to `chroma_qp_index_offset`.

### 3.5 Slice boundaries and availability (6.4.x)

A macroblock `mbAddrN` (A = left, B = above, C = above-right, D = above-left) is available to the current macroblock only if **all** hold:

- it lies inside the picture,
- it has already been decoded (its address is smaller than the current one),
- it belongs to the **same slice**.

Chapter 4.3 used this for `nC`; Chapter 5.3 for Intra16x16 and chroma prediction; Chapter 5.4 for Intra4x4 prediction and mode prediction. In every one of those places the test is the same, and **all of them must use the slice test**: sample availability for A, B, C, D, the `dcPredModePredictedFlag` in mode prediction (an unavailable neighbour macroblock forces DC), and `nC`. The easiest implementation, and the one recommended here, is a **slice id per macroblock of the current picture**: a table with one entry per macroblock, initialised to "not decoded yet" at the start of each picture, in which you store the slice's index as you decode each macroblock. A neighbour is then available when its entry equals the current slice's index. (Comparing `first_mb_in_slice` values works as well. Do not compare against "the previous macroblock's slice": the macroblock above can be several slices back.)

Slices may start anywhere. Typical layouts and what they imply for a macroblock `M` in a picture that is `w` macroblocks wide:

| Layout | Effect |
| --- | --- |
| one slice per picture | only picture edges matter |
| one slice per row | B, C, D are unavailable for every macroblock; only A is available (except at column 0) |
| a slice starting at column `k > 0` of row `r` | the left neighbour of its first macroblock is in the previous slice; in row `r` itself no macroblock has B, C or D (they are all in the previous slice); in row `r + 1` the macroblocks in columns `0..k-1` still have B in the previous slice, those from column `k` on have B in the current slice (above address >= `first_mb_in_slice`); C is available from column `k - 1` on |
| one macroblock per slice | all four neighbours unavailable for every macroblock, so every macroblock is predicted from the default values (DC 128, mode DC) |

The macroblock above is available exactly when `address - w >= first_mb_in_slice` (for slices of this chapter, no slice groups, no ASO). Slices appear in the stream in increasing `first_mb_in_slice` order here (Baseline would allow arbitrary slice order, ASO; x264 never produces it and Phase 5 does not need it), and every macroblock of the picture appears in exactly one slice.

**Picture boundaries from slice headers.** A picture's slices are all the slice NAL units between two picture boundaries (Chapter 3.4). Reset your per-picture tables (slice map, intra modes, `total_coeff` counts) when a new picture starts, not for each slice: the slice map must survive across the slices of a picture, because neighbour lookups ask "is that macroblock in my slice", which requires knowing which slice the other macroblock was in.

### 3.6 `constrained_intra_pred_flag` (7.4.2.2, 8.3.1.2, 8.3.1.1, 8.3.3, 8.3.4)

This PPS flag exists for streams that mix intra and inter macroblocks in the same picture, so that errors in inter-predicted macroblocks cannot spread into intra macroblocks through intra prediction. With `constrained_intra_pred_flag = 1`, an **Inter** macroblock counts as unavailable **for intra prediction**:

- **Intra4x4, Intra16x16 and chroma sample prediction:** a neighbouring macroblock coded in Inter prediction mode is "not available for Intra prediction", so its samples are not used; the mode rules for unavailable neighbours apply (Intra16x16 / chroma DC falls back to the other side or 128, and modes that need those samples are forbidden).
- **Intra4x4 mode prediction (8.3.1.1):** `dcPredModePredictedFlag` is set (both neighbour modes become DC) if the neighbouring macroblock is unavailable, **or it is Inter and `constrained_intra_pred_flag` is 1**. Without the flag, an Inter neighbour counts as "not I_NxN" and gives mode DC (2) for its own side only.

Two consequences for this chapter:

1. **In an I picture, every macroblock is Intra**, so with the flag set nothing is excluded and the result is identical to the flag being 0. This is true for I_PCM as well (I_PCM is an Intra macroblock). The `T2_cip` recipe (x264 `constrained-intra=1`) and the `qs_cip` synthetic stream (PPS flag 1) test exactly this: the decoder must produce the same pictures as for flag 0. `--make-variants` additionally proves, with ffmpeg, that `qs_cip` and `qs_nocip` (identical streams except for the PPS flag) decode to identical frames.
2. **Real coverage arrives in Phase 7**, when P slices bring Inter macroblocks. Wire it now so that you do not rewire later: parse the flag into the PPS record, and implement the availability test as a function of `(neighbour address, current slice, current macroblock, flag)` that returns "unavailable" for an Inter neighbour when the flag is 1. For this phase the Inter case never fires. A good shape: store each macroblock's class (`intra` or `inter`) in the per-picture table next to the slice id.

(There is a further rule for data-partitioned slices, `nal_unit_type` 2 to 4, and SI/SP slices in Extended profile; neither appears in this book.)

## 4. The project: exactly what to build

Extend your reader (`reader <in.ts|in.264> <out.yuv>`) so that every picture of an intra-only CAVLC stream, with any number of slices, decodes exactly. Concretely:

1. **Per slice**: compute `SliceQP_Y` from the PPS and slice header; reset the running QP to it; set the current slice index.
2. **Per macroblock**, in decoding order:
   - Update the running QP from `mb_qp_delta` if the macroblock has one (3.2); leave it unchanged otherwise (I_NxN with CBP 0, I_PCM). Use the resulting `QP_Y` for this macroblock's luma residual; derive Cb and Cr QPs from it (3.4).
   - Record the macroblock's slice index (and class, intra) in the per-picture tables **before** looking up neighbours.
   - Use the slice-aware availability everywhere (3.5): Intra16x16 (A, B, D), chroma (A, B, D), Intra4x4 (A, B, C, D plus block rules from 5.4), mode prediction (A, B), `nC` (A, B).
3. **Per picture**: reset the tables, decode all slices, then write the frame (Chapter 5.1). Decode **all pictures** of the stream; the output has exactly one frame per decoded picture.
4. Parse `constrained_intra_pred_flag` from the PPS and route it into the availability function (3.6).

Test before the real streams: write the QP update as a pure function of `(pred, delta)` and check the table in 3.2 by hand, including the wraps, before connecting it to the parser.

Verify (the Summary: "T4 decodes byte-identical; a T2 variant with `-crf 30` exercises QP deltas"): `T4_nodeblock`, `T2_crf30`, `T4_crf30`, `T2_aq`, `T2_cip`, T1, T2 and all synthetic streams decode byte-identical. T3 and T4 (deblocking on) need Phase 6.

## 5. Harness input format

The only file your reader produces is `out.yuv`, exactly as in Chapters 5.1, 5.3 and 5.4: raw planar yuv420p, no header, one **cropped** frame per decoded picture in decoding order (equal to output order for intra-only streams). The harness derives everything else itself, from the stream. The reader's input may be the `.ts` file or the raw `.264`.

## 6. Running the harness

Create the streams once (into the git-ignored `.cache`, or any folder):

```
cd "<repo>/Chapter 5.5 - QP tracking and multiple slices"
python3 test_qp_slices.py --make-variants ../.cache/v55            # writes *.ts and *.264 of the synthetic streams
cd .. && python3 -m common.testfiles T1 T2 T4_nodeblock T2_crf30 T4_crf30 T2_aq T2_cip
```

New recipes this chapter adds to `common/testfiles.py` (backward compatibly: the table format got an optional third element that replaces the default rate control `-qp 26`):

| Recipe | Codec options (rate control) | Why |
| --- | --- | --- |
| `T2_crf30` | T2's options (`-profile:v baseline -x264-params keyint=1:partitions=i4x4:no-deblock=1`), `-crf 30` | the Summary's QP-delta test: x264's adaptive quantisation varies QP_Y per macroblock |
| `T2_aq` | T2's options plus `aq-mode=2:aq-strength=1.6`, `-crf 24` | strong adaptive quantisation, larger and more frequent deltas |
| `T4_crf30` | `T4_nodeblock`'s options (`slices=4`), `-crf 30` | slices and QP deltas together: every slice restarts the chain |
| `T2_cip` | T2's options plus `constrained-intra=1`, `-qp 26` | PPS `constrained_intra_pred_flag = 1` in an intra-only stream |

Synthetic streams, written by `qsgen.py` (an independent encoder-side bit writer; all pictures are IDR, CAVLC, deblocking off, `transform_8x8_mode_flag` 0):

| Stream | Content | Tests |
| --- | --- | --- |
| `qs_mid` | ragged slices, QP in small steps (15..40) | the ordinary case |
| `qs_rand` | one slice, any `mb_qp_delta` -26..+25 | wrap-around up and down, extreme deltas |
| `qs_wrap` | QP kept near 0 and 51 with extreme deltas, ragged slices | wraps at both ends, chain restarts at 0/51 |
| `qs_pcm` | 30% I_PCM, random deltas | I_PCM then delta / no delta |
| `qs_single` | one macroblock per slice | every neighbour unavailable; `SliceQP_Y` used by every macroblock |
| `qs_rows` | one slice per row | B, C, D unavailable at every row start |
| `qs_midrow` | slices 1.5 rows long | slices starting mid-row: above in another slice, left in another slice |
| `qs_ragged` | random lengths | all of the above mixed |
| `qs_cip`, `qs_nocip` | same stream, PPS `constrained_intra_pred_flag` 1 and 0 | the flag changes nothing in an I picture |
| `qs_crop`, `qs_big` | cropping with slices; 22x18 macroblocks | cropping, a CIF-sized run |

Every picture has its own `pic_init_qp` (0..51), `slice_qp_delta` of both signs per slice, and chroma QP offsets up to +-12. `--make-variants` checks that the streams cover the cases listed in the harness (for instance: `mb_qp_delta` -26 and +25; both wrap directions; QP 0 and 51; a macroblock directly after an I_PCM with a delta; I_PCM followed by a macroblock without delta followed by one with a delta; one-macroblock slices; slices starting mid-row; A, B, C and D each in another slice; I_NxN neighbours in another slice; chroma `qPI` clipped low, clipped high, and in the table range).

Run your reader on a stream, then the harness:

```
<your reader> ../.cache/T4_crf30.ts out.yuv
python3 test_qp_slices.py ../.cache/T4_crf30.ts out.yuv
```

What is compared: ffmpeg's output (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`, cropped) against `out.yuv`, via `common/yuvcmp.py` for the first difference in pixel order. On success the harness prints what the stream exercised (slices per picture, QP range, number of non-zero and wrapping deltas, macroblocks after I_PCM, the constrained-intra flag value).

Exit codes: **0** = your YUV is identical to ffmpeg's, **1** = it differs (the report below), **2** = unusable input: the stream or your YUV file is missing or empty, ffmpeg cannot decode the stream, or the arguments are wrong. `--self-test` exits 0 when the harness passes its own checks, 1 otherwise.

On failure it additionally:

- finds the first wrong macroblock in **decoding order** (not pixel order, which can point at a later macroblock);
- prints its slice (index in the picture, `first_mb_in_slice`, `SliceQP_Y`, `pic_init_qp`, `slice_qp_delta`), its position in the slice, type, CBP, `QP_Y,PRED`, `mb_qp_delta`, `QP_Y` (from the independent reference parser), ffmpeg's own QP for it (`-debug qp` map; it prints 0 for I_PCM), its Cb and Cr QPs, which neighbours are in another slice, and the previous macroblock of the slice;
- lists how many macroblocks of each slice of that picture are wrong;
- prints **hints**: the first wrong macroblock is the first of its slice (QP reset or availability reset), all damaged slices start wrong, the previous macroblock is I_PCM, the previous macroblock had no `mb_qp_delta`, this delta wraps, luma right but chroma wrong (chroma QP), an other-slice neighbour in a mid-row slice, `constrained_intra_pred_flag` set.

Expected results: **everything passes**: all synthetic streams, T1, T2, T4_nodeblock, T2_crf30, T4_crf30, T2_aq, T2_cip. After this chapter **T1, T2 and T4_nodeblock decode byte-exact.** T3 and T4 still have the deblocking filter on; the harness falls back to a plain comparison for streams outside Phase 5 and will fail them.

**How the harness checks itself.** `python3 test_qp_slices.py --self-test` (a few minutes) generates every stream and runs the reference reader (`spoiler_reference/refdecode.py`) on them; it must match ffmpeg byte for byte. It checks that the real recipes contain what they should (four slices per picture, hundreds of non-zero deltas, the PPS flag set), that the reference parser's QP chain agrees with ffmpeg's own QP map on I_PCM-heavy, wrapping and multi-slice streams, then requires failure for a corrupted sample, a corrupted Cr sample and two truncations. Finally it plants **fifteen deliberately wrong rules**, one at a time, in the reference reader, and requires the harness to catch each one on some stream (a few also on real x264 streams):

| Mutant | The wrong rule |
| --- | --- |
| `qp_no_reset` | the chain is not restarted at the start of a slice |
| `qp_slice_pic_init` | `SliceQP_Y = pic_init_qp` (slice_qp_delta ignored) |
| `qp_clamp` | QP clamped to 0..51 instead of wrapping |
| `qp_nomod` | no wrap at all |
| `qp_pcm_zero` | I_PCM sets the running QP to 0 |
| `qp_pcm_slice` | I_PCM resets the running QP to `SliceQP_Y` |
| `qp_nodelta_reset` | a macroblock without `mb_qp_delta` resets the running QP |
| `cqp_noclip` | chroma `qPI` not clipped |
| `cqp_no_table` | Table 8-15 not applied |
| `cqp_cr_uses_cb` | Cr uses the Cb offset |
| `avail_noslice` | availability ignores slices |
| `avail_noslice_above` | only the left neighbour respects slices |
| `mp_avail_noslice` | mode prediction ignores slices, sample availability does not |
| `cip_all_unavailable` | the flag makes everything unavailable |
| `cip_pcm_unavailable` | the flag makes I_PCM neighbours unavailable |

Four of them (`cqp_noclip`, `mp_avail_noslice`, `cip_all_unavailable`, `cip_pcm_unavailable`) make the reference reader itself fail on every stream: the wrong rule produces a QP outside the tables, or meets a prediction mode that needs a neighbour the rule has just declared unavailable. The self-test labels these "ONLY BY CRASHING". Your reader may survive such a bug instead (or, if the bug also changes `nC`, lose the CAVLC bit position); it then writes wrong samples or too few frames, and the comparison rejects that. The self-test also checks that the failure reports of four mutants contain the right diagnosis. It prints `SELF-TEST PASS`.

> **Spoiler warning (again).** `spoiler_reference/refdecode.py` and the other `*ref.py` files there are complete solutions. Do not open them until you have tried, and then only to compare after a test has failed and the tips below did not help.

## 7. Reading failures / debugging tips

Read the "first wrong macroblock in DECODING order" block. Two numbers decide most cases: **position in the slice** and the **QP line**.

| Symptom | Likely cause |
| --- | --- |
| First wrong macroblock is the first of a slice (position 0), and slices after the first are wrong in the same way | QP_Y,PRED not reset to `SliceQP_Y` at the slice start (carried over from the previous slice, or taken from the PPS's `pic_init_qp`), or availability not reset |
| Position 0, the shapes are right but the residual detail is too strong or too weak | wrong QP: the whole residual scaled by a wrong step (each QP step is a factor 2^(1/6)) |
| Position 0 and the prediction itself (not the scaling) is wrong, edges look like they continue from the previous slice | the previous slice's macroblocks are used as neighbours: slice map not consulted, or filled too late (insert the current macroblock's slice id before looking at neighbours, and clear the map per picture) |
| First wrong macroblock is in the middle of a slice and the previous macroblock is I_PCM | the chain was changed by I_PCM (set to 0 or to the slice QP); it must pass through |
| First wrong macroblock follows an I_NxN with CBP 0 and its own delta is non-zero | a macroblock without `mb_qp_delta` changed the running QP, or you did not update the running QP for it (it must stay the same) |
| The report says "wraps" | `(pred + delta + 52) % 52`: check that your remainder is non-negative in your language (`-3 % 52` is negative in C and Java), and that you do not clamp |
| Luma right, chroma wrong, in macroblocks with extreme QP or offset | chroma QP: clip `qPI`, table for `qPI >= 30`, Cr uses the second offset |
| Wrong at a mid-row slice start only in Intra4x4 macroblocks, modes look off | mode prediction treats a macroblock of the previous slice as available (`dcPredModePredictedFlag` must be set) |
| Failures only at slice starts, and only after the first picture | per-picture tables not cleared at a new picture, or a slice index that restarts at 0 per picture compared with stale entries |
| Whole pictures missing or the file too short | you stopped after the first slice of a picture, or treat each slice as a picture (Chapter 3.4's boundary rule) |
| `qs_cip` fails but `qs_nocip` passes | you let `constrained_intra_pred_flag` change availability of Intra macroblocks (it may exclude only Inter ones) |
| T2_crf30 passes but `qs_rand` / `qs_wrap` fail | wrap-around: x264 almost never needs it |
| `qs_pcm` fails alone | I_PCM and the QP chain, or PCM macroblocks in neighbour tables (`nC` counts 16 for I_PCM; mode DC) |

A useful debugging trick: print your own `(addr, slice, QP_Y)` for the first wrong macroblock and compare it to the harness's line; if QP_Y agrees, the fault is availability or arithmetic, not the chain.

## 8. Phase 5 recap, and what's next

Phase 5 took you from parsed syntax to pictures: frame buffers and the YUV writer (5.1), inverse quantisation and the 4x4 transforms (5.2), Intra16x16 and chroma prediction (5.3), Intra4x4 prediction (5.4) and now QP tracking and slices (5.5). **T1, T2 and T4_nodeblock decode byte-exact**, along with the QP-delta, adaptive-quantisation, constrained-intra and synthetic streams: you have a working intra decoder for streams without in-loop filtering.

You can now look at your output. **Summary step 9.2 (YUV to image) may optionally be done now**: converting a frame of `out.yuv` to a viewable image is a few lines and is worth doing before Phase 6, because the deblocking filter changes how the picture looks, and seeing the before and after is instructive.

**Phase 6 adds the deblocking filter** (spec 8.7): after a picture is reconstructed, every macroblock edge and internal 4x4 edge is smoothed, using boundary strengths (bS = 4 and 3 for intra pictures), the QPs of the two macroblocks on each side (`qPp` and `qPq`, where this chapter's I_PCM remark applies: `qP = 0` for I_PCM), `FilterOffsetA/B`, and `disable_deblocking_filter_idc` (0 = filter everywhere, 1 = off as in all of Phase 5, 2 = filter but not across slice boundaries; the slice map you built here is what idc 2 needs). That is why the QP per macroblock and the slice map must be stored per picture, not discarded: the filter reads them. T3 and T4 then decode byte-exact.
