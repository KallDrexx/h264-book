# Chapter 7.7 - Explicit weighted prediction

> **SPOILER WARNING - do not open `spoiler_reference/` until your own decoder passes.**
> `spoiler_reference/weightdec.py` is a complete solution of this chapter (the decoder the harness compares you with), and
> `spoiler_reference/weightgen.py` is the generator of the synthetic streams. The other files in that folder are copies of the solutions of
> Chapters 5.x, 7.3, 7.5 and 7.6 (`refmgmt_ref.py` is 7.6's reference management) plus `slicehdr_ref.py`, the reader of the repeating
> slice-header elements. Everything you need is in this Readme. `test_weightpred.py` is plumbing (comparison, diagnosis, self-test) and is safe to read.

## 1. Goal and the project

Until now a predicted sample was a plain copy of the interpolated reference sample. This chapter adds the first thing that *changes* the
prediction: **weighted prediction**. The slice header carries, for every entry of `RefPicList0`, a multiplier (weight) and an additive offset
for luma, and another pair for each chroma plane. The decoder applies them to the prediction, sample by sample, before the residual is added.

You build:

1. the parser of `pred_weight_table()` (7.3.3.2) in the P slice header, producing a weight, an offset and a `logWD` per reference index and plane;
2. the weighting step of the prediction (8.4.2.3), for every inter partition, P_Skip included;
3. an optional **weights dump** that lets you check the table parsing on its own;
4. the glue that makes real x264 output decode: x264's weighted prediction also relies on the **reference list modification** and the
   **duplicated references** of Chapter 7.6, and on the deblocking rule "compare pictures, not indices" of Chapter 7.5.

Milestone: recipe **T7** (Main profile, CAVLC, no B pictures, `weightp=2`, a 25-frame fade-in) decodes **byte-identical** to ffmpeg for all
50 frames, plus the other x264 and synthetic variants listed in section 6.

## 2. Big picture

Cross-fades, fades from black and flashes change the brightness of a whole picture. Ordinary motion compensation then predicts badly:
the picture it points to is similar in shape but too dark. A fade is nearly a *multiplication plus an addition* of the previous picture,
so the encoder simply says it: "predict from picture X, multiplied by 1.3, minus 7". The residual then only has to cover what is left.

That is weighted prediction. For one reference it is just

```
predicted = Clip1( (interpolated * weight  /  2^logWD)  +  offset )        (rounded, see 3.3)
```

* `logWD` ("log weight denominator") is the number of fractional bits of the weight: weight `64` with `logWD = 6` means 1.0, `91` means 1.42.
* There is one weight/offset pair **per reference index**. The same picture can sit at several indices of the list (Chapter 7.6) so the encoder
  can try different weights on the *same* picture and let each block choose with its `ref_idx`. This is exactly what x264's `weightp=2` ("smart")
  does: it puts the same picture once or several times into the list with different weights. Chapter 7.6's T7 example list `1,1,1,0` is such a list.
* There is a separate pair for Cb and for Cr, and separate `logWD` values for luma and chroma.

Two modes exist in the standard: *explicit* (the weights are in the slice header: this chapter, P slices, `weighted_pred_flag = 1`) and
*implicit* (weights derived from picture distances, only for B pictures: Chapter 7.9). A third state, *default*, is plain copying (all earlier chapters).
Bi-prediction with explicit weights has one more formula; it belongs to B slices (7.8 and 7.9) and is only mentioned here.

## 3. Concepts in detail

### 3.1 Switching it on: the PPS

The PPS has two relevant fields (7.3.2.2), both already parsed by Chapter 3.2:

| field | meaning |
|---|---|
| `weighted_pred_flag` u(1) | 1: **P (and SP)** slices using this PPS carry a `pred_weight_table` and use explicit weighted prediction. 0: default prediction, no table. |
| `weighted_bipred_idc` u(2) | B slices: 0 default, 1 explicit, 2 implicit. Not used in P slices; ignore it until 7.8. |

The decision is per slice, through the slice's PPS. A stream can mix: slices with PPS A (flag 1) and slices with PPS B (flag 0) in the same picture are legal.
Every P slice of a PPS with `weighted_pred_flag = 1` has a table, including slices where all flags in it are 0.

### 3.2 `pred_weight_table()` (7.3.3.2)

Where: in the slice header, after `ref_pic_list_modification()` and before `dec_ref_pic_marking()`, **only if**
`weighted_pred_flag = 1` and the slice is P (or SP), or `weighted_bipred_idc = 1` and the slice is B. The slice-header parser of Chapter 3.3
must already know where it ends; now you read it. For a P slice:

```
luma_log2_weight_denom                        ue(v)    0..7
if ChromaArrayType != 0:
    chroma_log2_weight_denom                  ue(v)    0..7
for i = 0 .. num_ref_idx_l0_active_minus1:                       (num_ref_idx_active entries: after the override rule of 7.2)
    luma_weight_l0_flag[i]                    u(1)
    if flag:
        luma_weight_l0[i]                     se(v)    -128..127
        luma_offset_l0[i]                     se(v)    -128..127
    if ChromaArrayType != 0:
        chroma_weight_l0_flag[i]              u(1)
        if flag:
            for j = 0, 1  (Cb, then Cr):
                chroma_weight_l0[i][j]        se(v)    -128..127
                chroma_offset_l0[i][j]        se(v)    -128..127
(B slices continue with the same loop for list 1: Chapter 7.8.)
```

Points that matter:

* **Order inside one entry**: luma flag, luma weight, luma offset, chroma flag, Cb weight, Cb offset, Cr weight, Cr offset. Weights and offsets are interleaved per
  plane, not "all weights, then all offsets".
* The luma flag and the chroma flag are **independent**. An entry can weight chroma only (the x264 fade-out streams do the opposite: luma only).
* `ChromaArrayType` is 1 for the 4:2:0 streams of this book. For monochrome (4:0:0, Phase 8) both chroma elements vanish: the table has only the luma part.
* There is no table entry for pictures, only for **list indices**. Entry `i` belongs to whatever `RefPicList0[i]` is *after* the modification of 7.6.
* The number of entries is the **active** count, not the number of pictures in the DPB, and not `max_num_ref_frames`.

**Defaults when a flag is 0** (semantics, 7.4.3.2):

| flag | what the decoder uses |
|---|---|
| `luma_weight_l0_flag[i] = 0` | luma weight `2^luma_log2_weight_denom`, luma offset 0 |
| `chroma_weight_l0_flag[i] = 0` | Cb and Cr weight `2^chroma_log2_weight_denom` (**the chroma denominator**), offsets 0 |

A default weight of `2^logWD` means "multiply by 1.0". Note it is **not** the number 1: with `logWD = 5` the default weight is 32. This is the most common
bug in the table parser.

Derived values (8.4.2.3): the offset actually added is `o = offset << (BitDepth - 8)`. All streams in this book are 8-bit, so `o = offset`.

### 3.3 The weighting formula (8.4.2.3)

For every sample of a partition's prediction (single list, so the "explicit, one list" case of the standard), with `predPartL0` the interpolated sample from 7.4:

```
if logWD >= 1:   result = Clip1( ((predPartL0 * w + 2^(logWD-1)) >> logWD) + o )
else:            result = Clip1(  predPartL0 * w + o )
```

* `Clip1(v) = min(max(v, 0), 255)`.
* `w` and `o` are those of the partition's `ref_idx` (3.4) and of the plane being predicted: luma uses the luma weight, offset and `luma_log2_weight_denom`; Cb uses the Cb weight/offset and
  `chroma_log2_weight_denom`; Cr uses the Cr values.
* `>>` is an arithmetic shift. `predPartL0 * w + 2^(logWD-1)` can be negative when `w` is negative; the shift rounds toward minus infinity (do not use division).
* The rounding term `2^(logWD-1)` is added **before** the shift; the offset is added **after** it. Adding the offset first changes results.
* The offset is **not** scaled by the weight and is not shifted.
* Weights are signed: a negative weight inverts the picture (a negative that is clipped to 0 is legal and common in synthetic tests).
* The result is a *prediction* sample. The residual is added afterwards and clipped again (7.4). Weighting happens **after** interpolation (luma 6-tap, chroma bilinear)
  and uses the interpolated, already rounded sample. Weighting the reference picture first and interpolating afterwards gives different results.

Worked example (luma of T7's first weighted slice: `luma_log2_weight_denom = 5`, entry 0: `w = 65`, `o = -17`). An interpolated sample `100`:
`(100 * 65 + 16) >> 5 = 6516 >> 5 = 203`; `203 - 17 = 186`.
Chroma of the same entry: `chroma_log2_weight_denom = 0`, `w = 2`, `o = -128`: sample `128` gives `128 * 2 - 128 = 128`, and `130` gives `132` (grey stays grey, other values move twice as far from it). This is the `logWD = 0` branch: no rounding term, no shift.

**Defaults are an identity.** With flag 0 the formula gives `((p * 2^logWD + 2^(logWD-1)) >> logWD) + 0 = p` for `logWD >= 1` (the rounding term is
smaller than `2^logWD`, so it is shifted away), and `p * 1 + 0 = p` for `logWD = 0`. So an entry with both flags 0 predicts exactly like default prediction.
You may skip the weighting for such entries, but you must still parse them; it is simplest not to skip at all.

### 3.4 Which entry applies: `ref_idx`

The partition's `ref_idx` (decoded in 7.1, 0 for P_Skip) indexes the weight table directly: `refIdxL0WP = refIdxL0` for frame macroblocks.
(In MBAFF frames, field macroblocks use `refIdxL0 >> 1` because their reference list is the doubled field list; that is Phase 10.)
Consequences:

* Two partitions that reference the *same picture* through different indices (list `1,1,1,0`) use **different weights** if their entries differ. Never key weights
  by picture.
* **P_Skip** is predicted from `RefPicList0[0]` with the predicted vector (7.3) and is weighted with the entry for index 0 like any other macroblock. A decoder that
  copies skipped macroblocks unweighted is wrong in every fade.
* Every partition of an 8x8 sub-macroblock uses the weights of its `ref_idx`; sub-partitions share them.
* Intra macroblocks inside P slices have no weights (no prediction from a reference).
* The weights come from the table of the macroblock's **own slice**; slices of a picture can have different tables and different reference lists.

### 3.5 Why x264 uses duplicated references (and what it means for you)

x264's `weightp=1` weights only `ref_idx` 0 (luma and chroma weight and offset); the other entries keep the defaults and the list is not modified.
`weightp=2` also creates *duplicates*: it uses `ref_pic_list_modification` to put the nearest picture at several indices with different weights, so each
macroblock can pick the weighting that suits it. T7's third picture (list `1,1,1,0`, see 7.6 section 3.3, and the dump line in 5.2) has picture 1 at index 0 with the
fade's weights, at index 1 with the same luma weight, a luma offset one lower and default chroma, and at index 2 unweighted.

Two earlier rules become load-bearing here:

* Chapter 7.5, bS: "different reference pictures" compares **pictures**. Neighbouring blocks with `ref_idx` 0 and 2 that name the same picture and have equal vectors have bS 0 (unless
  coefficients). Compare picture identities, not numbers. If you compared `ref_idx` instead, T7 deblocks wrongly (it may even look right on streams
  without duplicates, which is why this bug survives until now).
* Chapter 7.3, motion vector prediction: its "same reference index" tests compare the `refIdx` **numbers** (neighbour with `ref_idx` 2 does not count as `ref_idx` 0
  even if both are the same picture). The two rules look contradictory and both are right.

### 3.6 Order of operations for an inter macroblock (summary)

```
for each partition:   interpolate luma / Cb / Cr from RefPicList0[ref_idx]                (7.4)
                      weight each plane with entry ref_idx, if the slice has a table      (this chapter)
whole macroblock:     add the residual (inter transform path), Clip1                     (7.4)
whole picture:        deblock                                                             (7.5)
```

## 4. The project: exactly what to build

Extend your decoder of Chapter 7.6:

1. **Parse `pred_weight_table`** in the slice header (3.2), after the list modification, with the `ChromaArrayType` conditions and the active count. Fill the defaults (flag 0 -> `2^denom`, offset 0).
   Keep the table per slice.
2. **Apply the weights** to every inter partition (3.3, 3.4), P_Skip included, using the entry named by `ref_idx` and the right plane's weight, offset and `logWD`.
3. Check the rest of the chain with T7: list modification and duplicates (7.6), picture identity in the bS rule (7.5), the sliding window with a `frame_num` wrap.
4. **Optional weights dump** (section 5): write the table that you parsed. It lets you separate "table parsing is wrong" from "weighting is wrong".

**Verify:** `T7` (fade-in) decodes byte-identical for all 50 frames; so do the variants of section 6. `T5`, `T6`, `T6_ref16` must still pass.

What T7 contains (look at ffmpeg's trace yourself): CAVLC Main profile, 352x288, one slice per picture, an IDR picture and a non-IDR I picture followed by 48 P pictures,
`max_num_ref_frames = 3` (x264's default), `MaxFrameNum = 16`, `weighted_pred_flag = 1`, every P slice with a `pred_weight_table` and four or five active entries (more entries than
reference frames, because of the duplicates), `ref_pic_list_modification` with repeated pictures (commands like `abs_diff_pic_num_minus1 = 15` that repeat the same picture), no MMCO,
no long-term pictures, deblocking on. Everything beyond the weights is therefore Chapter 7.5 / 7.6 behaviour.

## 5. Harness input format

Two optional files; give at least one.

### 5.1 The decoded frames

A raw file: every frame in output order (= decoding order for these P-only streams), 8-bit planar yuv420p (all Y, then all Cb, then all Cr), cropped as the SPS says. Same as Chapters 5.1 and 7.4.

### 5.2 The weights dump (`--weights-dump FILE`)

One text line per **P slice whose PPS has `weighted_pred_flag = 1`**, in decoding order. No line for I slices, for P slices of PPSs with the flag 0, or for anything else.
Blank lines and lines starting with `#` are ignored. Fields separated by single spaces, all numbers decimal.

```
SLICE <pic> <slice> denomY=<n> denomC=<n> w0=<idx>:<wY>:<oY>:<wCb>:<oCb>:<wCr>:<oCr>,<idx>:...
```

| field | meaning |
|---|---|
| `<pic>` | 0-based decoding-order index of the coded picture (Chapter 3.4's grouping), the same numbering as in Chapter 7.6 |
| `<slice>` | 0-based index of the slice within its picture |
| `denomY`, `denomC` | `luma_log2_weight_denom`, `chroma_log2_weight_denom` |
| `w0=` | one entry per `ref_idx` 0 .. `num_ref_idx_l0_active - 1`, in order, comma separated, **no spaces** |
| entry | `<idx>` is repeated for checking, then the luma weight and offset, the Cb weight and offset, the Cr weight and offset **as used by the decoding process**: a flag of 0 shows the defaults `2^denom` and `0` |

Offsets are as sent in the bitstream (before the `<< (BitDepth - 8)` of 3.2, which is 0 here). Example (T7's first line: pictures 0 and 1 are I pictures, so it is picture 2;
entry 1 has only the luma flag set, entries 2 and 3 have both flags 0, `denomY` 5, `denomC` 0):

```
SLICE 2 0 denomY=5 denomC=0 w0=0:65:-17:2:-128:2:-128,1:65:-18:1:0:1:0,2:32:0:1:0:1:0,3:32:0:1:0:1:0
```

`python3 test_weightpred.py --expected-weights-out expected.txt T7.ts` writes the whole expected file, to diff against yours.

## 6. Running the harness

```
python3 test_weightpred.py T7.ts your.yuv                              # pixels
python3 test_weightpred.py T7.ts your.yuv --weights-dump weights.txt   # pixels and table parsing
python3 test_weightpred.py T7.ts --weights-dump weights.txt            # table parsing only
python3 test_weightpred.py --make-variants DIR                         # writes the test inputs below into DIR
python3 test_weightpred.py --self-test                                 # checks the harness itself (about 3 minutes)
```

`T7.ts` is `python3 -m common.testfiles T7` (in `.cache/`). The pixels are compared with ffmpeg's decode (`common/yuvcmp.py`). Exit 0 = identical, 1 = different, 2 = unusable input/output.

Inputs from `--make-variants` (1 second each unless noted; names are the file names):

| file | what it exercises |
|---|---|
| `T7.ts` | the milestone: `weightp=2`, fade-in, 3 reference frames -> list modification, duplicated references, both denominators |
| `wp_simple_fadein.ts` | `weightp=1`: weights without duplicated references |
| `wp_smart_fadeout.ts` | fade-out: luma-only weights (chroma flags 0), several entries |
| `wp_smart_ref3.ts` | `partitions=all`, `ref=3`: all partition shapes with weights, sub-partitions share the entry |
| `wp_slices.ts` | `slices=3`, fade-in then fade-out: a different table (and list) for each slice of a picture |
| `wp_ref6.ts` | six references with duplicates, the `frame_num` wrap |
| `synth_denoms.264`, `synth_clip.264`, `synth_neg.264` | logWD 0..7 for both planes; big weights/offsets that clip at 0 and 255; negative weights and offsets |
| `synth_flags.264`, `synth_chroma.264`, `synth_lumaonly.264` | all four flag combinations per entry; chroma-only and luma-only weights; two slices per picture |
| `synth_skip.264` | mostly P_Skip macroblocks with non-trivial weights on index 0 |
| `synth_manyrefs.264` | up to 10 active entries, many modifications, one picture at several indices, each with its own weights |
| `synth_dupdeblock.264` | duplicated references with deblocking on, vectors zero: bS must compare pictures |

The synthetic streams are tiny (96x80) Main-profile CAVLC streams made of I_PCM, P_Skip and P_L0_16x16 macroblocks without residual, with random vectors, `ref_idx` and tables.
They isolate the weighting from everything else; all x264 files also pass through the full chain.

## 7. Reading failures

### 7.1 Weights dump

The harness prints, per slice (first 10), `entry i: wCb expected 64 got 1` style differences with hints:

| symptom | likely cause |
|---|---|
| a weight is 1 (or an offset is the previous entry's) where flag = 0 | default must be `2^denom` of the **plane** (chroma: `chroma_log2_weight_denom`), offset 0 |
| the table is right for entry 0, then garbage | an element order mistake: luma flag, weight, offset, chroma flag, **Cb** weight, Cb offset, **Cr** weight, Cr offset |
| chroma shows the luma numbers | chroma has its own flag; the luma flag does not decide it |
| `denomC` differs | `chroma_log2_weight_denom` is a second `ue(v)` right after the luma one; it exists only when `ChromaArrayType != 0` |
| the number of entries differs | it is the active count after `num_ref_idx_active_override`, not `max_num_ref_frames` |
| everything after the table is shifted (macroblock parse fails) | the table was not consumed completely, or was read when `weighted_pred_flag = 0`; check the slice type condition |
| offsets with the wrong sign | `se(v)`, not `ue(v)` |

### 7.2 Pixels

When the pixels differ, the harness decodes the stream with the reference decoder up to the first wrong frame and prints:

* the first wrong macroblock (raster order), how many Y, Cb, Cr samples are wrong, its type and slice;
* the slice's weight table and, for each partition, `ref_idx`, the picture it names, the vector and the weights (`w`, `o`, `logWD` per plane) the reference applied;
* the size of the differences (only +-1? only at 0 or 255?);
* whether **your whole frame equals the reference decoder with one specific wrong rule**, from this list (if so it names the rule: that is your bug):

| wrong rule | typical visible symptom |
|---|---|
| no rounding term | differences of 1 in many samples, only with `logWD >= 1` |
| offset added before the shift | errors of nearly `|o|` wherever `logWD >= 1` |
| default weight 1 instead of `2^logWD` | whole entries wrong when a flag is 0 |
| chroma uses luma weights / luma denominator | chroma wrong, luma right |
| Cb and Cr swapped | chroma wrong when the two weights differ |
| chroma weight used only if the luma flag is set | chroma-only entries wrong |
| ref_idx ignored / off by one / weights looked up by picture | wrong entries whenever `ref_idx > 0` or a picture is listed twice |
| P_Skip unweighted | wrong skipped macroblocks |
| no clip | wrapped bright/dark pixels where values leave 0..255 |
| `logWD = 0` loses the offset | offsets ignored in entries with denominator 0 (T7's chroma) |
| weights before interpolation | small errors at fractional vectors |
| negative weights as absolute values | inverted fades |
| weighted_pred_flag ignored | everything wrong |
| list modification ignored | the pictures at `ref_idx` > 0 are wrong (7.6) |
| bS compares `ref_idx` | errors only at edges between partitions that use the same picture through different indices (deblocking, 7.5) |

Debugging order: (1) dump the table and diff it; (2) decode only the first P picture and compare; (3) make sure the list of that slice is right (7.6 log) because a
correct weight on the wrong picture looks like a weighting bug; (4) check P_Skip; (5) turn deblocking comparisons on last: the synthetic streams other than `dupdeblock` have deblocking off.

## 8. What's next

After this chapter these recipes decode byte-identical: **T5, T6 (with deblocking), T6_ref16 and T7**, in addition to every intra recipe. What remains in Phase 7 is B pictures:
**7.8** adds B macroblock types, list 0/1 initialisation by POC and default bi-prediction (the explicit bi-prediction formula and `weighted_bipred_idc = 1` join the work there),
and **7.9** adds the direct modes and implicit weights (`weighted_bipred_idc = 2`), which finish T8 and T9.
