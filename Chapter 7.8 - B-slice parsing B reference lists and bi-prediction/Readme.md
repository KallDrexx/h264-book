# Chapter 7.8 - B-slice parsing, B reference lists and bi-prediction

> **SPOILER WARNING - do not open `spoiler_reference/` until your own decoder passes.**
> `spoiler_reference/bdec.py` is a complete solution of this chapter (the decoder the harness compares you with), `bgen.py` is the generator of the
> synthetic streams, `bmvpred_ref.py` (vector prediction for two lists), `brefmgmt_ref.py` (reference lists, including the B lists), `poc_ref.py`
> (picture order count) and `bsref_b.py` (boundary strength with two vectors) are the solutions of the individual parts. The other files in that
> folder are copies of earlier chapters' solutions (interpolation, intra, transform, deblocking filter) and `bslicehdr_ref.py`, the reader of the
> repeating slice-header elements. Everything you need is in this Readme. `test_bslice.py` is plumbing (comparison, diagnosis, self-test) and is safe to read.
> The parser behind the dumps lives in `common/inter_ref.py` (level 3): it is also a solution of the parsing part.

## 1. Goal and the project

Until now every inter macroblock was predicted from **one** earlier picture. A **B slice** may predict a block from one picture *or from two* and
average them. The second picture is often a *later* one in display order, which means that the decoder must decode pictures in a different order from the
one it displays them in. This chapter adds the parts of that which do not need "direct" prediction:

1. the **B macroblock syntax**: `mb_skip_run` in B slices, the 23 `mb_type` values of Table 7-14, the 13 `sub_mb_type` values of Table 7-18, and the order of
   `ref_idx_l0`, `ref_idx_l1`, `mvd_l0`, `mvd_l1` (7.3.5.1, 7.3.5.2);
2. **motion vector prediction for two lists** (8.4.1.3 applied separately to list 0 and list 1);
3. the **reference lists of a B slice** (8.2.4.2.3): list 0 and list 1 built from picture order counts, the "swap" rule, the truncation, and the
   modification commands for both lists; plus the DPB behaviour of non-reference B pictures;
4. **bi-prediction**: the default average, and the explicit weighted formula for `weighted_bipred_idc = 1`;
5. the **boundary strength** rule for blocks with two vectors (8.7.2.1);
6. **output order**: frames are written in display order, i.e. sorted by picture order count (POC) inside each IDR period.

Milestone: recipe **T8** (`-profile:v main -x264-params cabac=0:bframes=2:b-pyramid=none:direct=none:weightb=0`, 50 frames, I/P/B) decodes
**byte-identical** to ffmpeg. The harness also checks the x264 and synthetic variants in section 6 and, optionally, your macroblock dump, your reference-list
log and your motion vectors.

**What T8 really contains** (checked with the reference parser and ffmpeg's `-debug mb_type`): every B slice has `mb_skip_run = 0` before every macroblock, and there is
**no** `B_Skip`, no `B_Direct_16x16` and no `B_Direct_8x8` - `direct=none` really keeps x264 away from them. The B macroblocks are `B_L0/L1/Bi_16x16`, the 16x8
and 8x16 combinations, `B_8x8` with sub types 1..3 only (x264 does not split B sub-blocks below 8x8), and some intra macroblocks. The B pictures are non-reference
(`nal_ref_idc = 0`). The P slices of T8 have `weighted_pred_flag = 1` in their PPS, so Chapter 7.7's table parsing is exercised as well; `log2_max_pic_order_cnt_lsb` is 5,
so the POC lsb wraps after 16 pictures. Direct prediction, `B_Skip`, implicit weights and reference B pictures *with* direct blocks are Chapter 7.9
(T9 needs it). Your parser should still accept the direct types (they carry almost no syntax, see 3.2); your reconstruction may reject streams that use them.

## 2. Big picture

**Why B pictures.** A picture in the middle of a motion can be predicted well from the picture before *and* the picture after it: what is hidden in
one is visible in the other, and averaging two noisy predictions removes noise. To use a "future" picture the encoder codes it *first*:

```
display order:   I0  B1  B2  P3  B4  B5  P6          (the number is the display position)
decoding order:  I0  P3  B1  B2  P6  B4  B5
```

The decoder therefore needs (a) a **picture order count** to know where each picture belongs on the time axis, (b) a rule that, for each B slice, sorts
the available reference pictures into two lists - **list 0** (mostly the past) and **list 1** (mostly the future) - and (c) macroblock types that say, per partition,
"use list 0", "use list 1" or "use both". Everything else (interpolation, residual, loop filter) is what you already have.

**What changes in the code.** Think of every partition as having *two* optional predictions, each with its own `ref_idx`, vector and prediction:

```
predFlagL0, refIdxL0, mvL0        predFlagL1, refIdxL1, mvL1
```

L0-only: the old behaviour. L1-only: the same with the other list. Bi: both predictions are made (full interpolation each, including its own rounding and clipping),
then combined sample by sample: `(p0 + p1 + 1) >> 1`. Motion data is therefore stored **per list** for every 4x4 block: vector prediction, the loop filter and (7.9) the
direct modes all read it.

**Display order.** Until Chapter 9.1 you do not need the "bumping" process of the standard: you write the frames of each IDR period sorted by POC. When an IDR picture arrives (or
a picture with memory_management_control_operation 5), everything before it is finished first.

## 3. Concepts in detail

### 3.1 The slice header of a B slice

You already parse all of it (Chapter 3.3): `direct_spatial_mv_pred_flag` (ignore for now, no direct blocks), `num_ref_idx_active_override_flag` followed by **both**
`num_ref_idx_l0_active_minus1` and `num_ref_idx_l1_active_minus1`, `ref_pic_list_modification()` for list 0 **and then list 1**, the `pred_weight_table()` (only if the PPS has
`weighted_bipred_idc = 1`; it then contains the list-0 entries followed by the list-1 entries), `dec_ref_pic_marking()` if `nal_ref_idc != 0`. Without the override
the active counts are the PPS defaults `num_ref_idx_l0_default_active_minus1 + 1` and `num_ref_idx_l1_default_active_minus1 + 1`. T8 overrides to l0 = 2, l1 = 1 in most B slices.

### 3.2 `mb_skip_run` and direct

The slice data loop of a B slice is the one of a P slice: `mb_skip_run`, then a macroblock, ... The skipped macroblocks of a B slice are **B_Skip**, which is *direct prediction without residual*
(motion derived from neighbours or from the co-located picture: Chapter 7.9). `B_Skip` has no syntax at all. `B_Direct_16x16` (mb_type 0) has no `ref_idx` and no `mvd`, only
`coded_block_pattern` and the residual; a `B_Direct_8x8` sub-macroblock (sub_mb_type 0) inside a `B_8x8` has no `ref_idx` and no `mvd` of its own. So a parser can handle them completely - only the
*vectors* need 7.9. In T8 every `mb_skip_run` is 0. Handle the end of the slice as in P slices: a run followed by no more data finishes the slice.

### 3.3 `mb_type` in B slices (Table 7-14)

| mb_type | name | partitions (mode of each) |
|---|---|---|
| 0 | B_Direct_16x16 | direct, 16x16 |
| 1 / 2 / 3 | B_L0_16x16 / B_L1_16x16 / B_Bi_16x16 | one 16x16 partition: L0 / L1 / Bi |
| 4, 5 | B_L0_L0_16x8, B_L0_L0_8x16 | two partitions: (L0, L0) |
| 6, 7 | B_L1_L1_16x8, B_L1_L1_8x16 | (L1, L1) |
| 8, 9 | B_L0_L1_16x8, B_L0_L1_8x16 | (L0, L1) |
| 10, 11 | B_L1_L0_16x8, B_L1_L0_8x16 | (L1, L0) |
| 12, 13 | B_L0_Bi_16x8, B_L0_Bi_8x16 | (L0, Bi) |
| 14, 15 | B_L1_Bi_16x8, B_L1_Bi_8x16 | (L1, Bi) |
| 16, 17 | B_Bi_L0_16x8, B_Bi_L0_8x16 | (Bi, L0) |
| 18, 19 | B_Bi_L1_16x8, B_Bi_L1_8x16 | (Bi, L1) |
| 20, 21 | B_Bi_Bi_16x8, B_Bi_Bi_8x16 | (Bi, Bi) |
| 22 | B_8x8 | four sub-macroblocks, each with a `sub_mb_type` |
| 23 .. 48 | intra | `mb_type - 23` is the I-slice value of Table 7-11 (23 = I_NxN, 48 = I_PCM) |

Pattern: from 4 on, the pairs run through (L0,L0), (L1,L1), (L0,L1), (L1,L0), (L0,Bi), (L1,Bi), (Bi,L0), (Bi,L1), (Bi,Bi); inside a pair the **even** number is the 16x8 shape (two
partitions on top of each other: the first is the upper one) and the **odd** number the 8x16 shape (side by side: the first is the left one).

### 3.4 `sub_mb_type` in B_8x8 (Table 7-18)

| sub_mb_type | name | list use | sub-partitions |
|---|---|---|---|
| 0 | B_Direct_8x8 | direct | (4 blocks of 4x4, or one 8x8 with direct_8x8_inference) |
| 1 / 2 / 3 | B_L0_8x8 / B_L1_8x8 / B_Bi_8x8 | L0 / L1 / Bi | 1 (8x8) |
| 4 / 5 | B_L0_8x4 / B_L0_4x8 | L0 | 2 |
| 6 / 7 | B_L1_8x4 / B_L1_4x8 | L1 | 2 |
| 8 / 9 | B_Bi_8x4 / B_Bi_4x8 | Bi | 2 |
| 10 / 11 / 12 | B_L0_4x4 / B_L1_4x4 / B_Bi_4x4 | L0 / L1 / Bi | 4 |

(Values above 12 are invalid.) A sub-macroblock's two lists share one shape: a `B_Bi_4x4` sub-macroblock has four sub-partitions, each with a list-0 and a list-1 vector.

### 3.5 What is read, and in which order (7.3.5.1, 7.3.5.2)

For every macroblock that is not direct and not intra, after `mb_type` (and, for B_8x8, after the four `sub_mb_type` values):

```
for each partition (sub-macroblock) that uses list 0:   ref_idx_l0     if num_ref_idx_l0_active > 1
for each partition (sub-macroblock) that uses list 1:   ref_idx_l1     if num_ref_idx_l1_active > 1
for each partition (sub-macroblock) that uses list 0:   one mvd_l0 per prediction block (sub-partition)
for each partition (sub-macroblock) that uses list 1:   one mvd_l1 per prediction block
coded_block_pattern, [transform_size_8x8_flag], [mb_qp_delta], residual            (the same as in P slices)
```

* "uses list 0" means the mode is L0 or Bi; "uses list 1" means L1 or Bi. A `Bi` partition appears in all four loops. A B_Direct_8x8 sub-macroblock appears in none.
* **All** `ref_idx_l0` come before **any** `ref_idx_l1`, and all `mvd_l0` before any `mvd_l1`; the ref_idx loops come before the mvd loops. Nothing is interleaved per partition.
* `ref_idx` is `te(v)` with range `num_ref_idx_lX_active - 1`, **list by list**: for list 1 the range comes from `num_ref_idx_l1_active`. Range 0: not present (value 0); range 1: one bit, **inverted**
  (0 means ref_idx 1); larger: `ue(v)`. A value above the range is a stream error.
* `mvd` is `se(v)`, x then y, in quarter samples.
* `coded_block_pattern` uses the **inter** column of Table 9-4 as in P slices. For `B_Direct_16x16` and for `B_8x8` with a direct sub-macroblock the 8x8 transform flag
  condition needs `direct_8x8_inference_flag` (Main profile has no 8x8 transform: ignore).
* The residual of a macroblock coded after a skipped one, `mb_qp_delta` chaining, and the `nC` rules for `coeff_token` are unchanged (a `B_Skip` has total_coeff 0 everywhere).

### 3.6 Motion vectors with two lists (8.4.1.3)

The vector of a partition and list X is `mvpLX + mvdLX`, where `mvpLX` is predicted **with exactly the procedure of Chapter 7.3, applied to list X on its own**:

* the neighbours A, B, C (D replaces an unavailable C) are the partitions covering the same positions as before;
* for each neighbour take its `refIdxLX` and `mvLX`. A neighbour partition that is *not available* (outside the picture, other slice, not yet decoded) is unavailable as before. A neighbour that **is available
  but does not use list X** (intra, or an inter partition whose mode is the other list) gets `refIdxLX = -1` and `mvLX = (0, 0)`: it is available (so the C-to-D replacement and the "only A is
  available" rule do not trigger) but it never matches the current `ref_idx`;
* the current `refIdxLX` is the partition's own `ref_idx_lX`; median prediction, the "exactly one neighbour has the same refIdx" rule and the directional rules for 16x8 / 8x16 compare that number with the neighbours' number **in the same list**;
* list 0 and list 1 predictions of one partition are independent. Both final vectors are stored (together with both `refIdx` values, `-1` for an unused list) before the next partition is processed.

Store, for every 4x4 block of the picture, `refIdxL0, mvL0, refIdxL1, mvL1`. Chapter 7.9's direct modes read the stored data of the *co-located* picture, so keep it with the picture (and remember which picture each refIdx named).
`P_Skip` in a P slice is unchanged (list 0 only; list 1 of its blocks is "not used"). There is no skip prediction for B slices in this chapter.

### 3.7 Picture order count

A B picture's position comes from `PicOrderCnt` (8.2.1), which you computed in Chapter 3.4 (`pic_order_cnt_type` 0, 1 or 2; the PicOrderCnt of a frame is `Min(TopFieldOrderCnt, BottomFieldOrderCnt)`). Two things get real here:

* the **MSB**: with type 0 only `pic_order_cnt_lsb` (here 5 bits: 0..31) is transmitted and the MSB is inferred from the previous *reference* picture (the "prevPicOrderCntMsb / Lsb" rule). T8 wraps its lsb after 16 pictures. A decoder that
  uses the lsb as the POC works for the first 16 pictures and then misorders pictures;
* x264 steps the POC by 2 per picture (`poc = 2 * display_index`), the standard does not require it. Only the order and the signs of differences matter.

### 3.8 Reference lists of a B slice (8.2.4.2.3, frames)

Take all frames marked "used for short-term reference" (the pictures stored in the DPB; the current picture is not among them) and the long-term frames. Let `POC(f)` be a frame's PicOrderCnt, `POC(cur)` the current picture's.

```
list 0, initial:   short-term frames with POC < POC(cur), DEscending POC (nearest in the past first)
                   then short-term frames with POC > POC(cur), ASCENDING POC (nearest in the future first)
                   then long-term frames, ascending LongTermPicNum
list 1, initial:   short-term frames with POC > POC(cur), ASCENDING POC (nearest future first)
                   then short-term frames with POC < POC(cur), DEscending POC
                   then long-term frames, ascending LongTermPicNum
swap rule:         if list 1 has MORE THAN ONE entry and list 1 is identical to list 0, swap its first two entries
```

* The swap rule is tested on the **complete initial lists** (all reference frames), *before* the lists are cut to `num_ref_idx_l0_active` / `num_ref_idx_l1_active`. It matters when every reference is in the past
  (a B picture that is a "late" picture), or when there is a single reference direction: list 0 and list 1 would otherwise be the same and the second list would be useless.
* Then each list is **truncated** to its active count; if the list is shorter than the active count the remaining entries are "no reference picture" (a conforming stream does not use them). Neither list is padded by repeating entries.
* Then `ref_pic_list_modification()` for list 0 and, independently, for list 1 (Chapter 7.6's commands, 8.2.4.3, applied to the already truncated list; `picNumLXPred` starts at `CurrPicNum` for each list). The modification of list 1 does not
  touch list 0.
* `PicNum` of short-term frames is `FrameNumWrap` as in P slices (it is not related to POC). The long-term frames are also used by B slices.
* P slices are unchanged: their list 0 is still sorted by descending PicNum. Which rule applies depends on the slice type; one picture can mix P and B slices.

Worked example from T8 (decoding-order picture indexes; `n:m` is picture n with `PicNum` m, from `--print-expected lists`):

```
DPB 3 short=3:2,1:1,0:0 long=- maxlt=none           pictures 3, 1, 0 have POC 10, 4, 0
SLICE 4 0 type=B frame_num=3 poc=6 active=2 list0=1:1,0:0 active1=1 list1=3:2
```

For the B picture with POC 6: earlier pictures are POC 4 (picture 1) and POC 0 (picture 0), the later one is POC 10 (picture 3). Initial list 0 = [1, 0, 3], list 1 = [3, 1, 0]; they differ, so no swap;
truncated to 2 and 1 entries: list 0 = [1, 0], list 1 = [3]. If picture 3 were not in the DPB (only past references), both initial lists would be [1, 0]; they are identical and list 1 has two
entries, so list 1 becomes [0, 1], and truncated to one entry list 1 = [0]. (Testing the swap after truncation would compare [1, 0] with [1] and give list 1 = [1]: wrong.)

### 3.9 The DPB and B pictures

* A B picture with `nal_ref_idc = 0` is **not a reference**: it is decoded, output, and never stored as a reference (no sliding window step, no entry in any list). `frame_num` of a non-reference picture is
  `PrevRefFrameNum + 1` (and the next reference picture uses the same value). That is not a "gap".
* A B picture with `nal_ref_idc != 0` is a reference like any other: it takes part in the sliding window (or MMCO) and later pictures find it in their lists with its own POC (`b-pyramid=normal` makes x264 do this; the synthetic `lists` stream and `b_pyr` contain such pictures).
* Marking, `PrevRefFrameNum` handling and IDR/MMCO rules are Chapter 7.6's, unchanged.

### 3.10 Bi-prediction (8.4.2.3)

For each prediction block you form `predPartL0` and/or `predPartL1` with the interpolation of Chapter 7.4 from `RefPicList0[refIdxL0]` and `RefPicList1[refIdxL1]` and the block's own vectors. The two are
real 8-bit sample arrays (the interpolation already rounds and clips them), luma and chroma separately, chroma with its own chroma vector derivation per list.

**Default (weighted_bipred_idc = 0):**

```
one list:   pred = predPartLX
two lists:  pred = ( predPartL0 + predPartL1 + 1 ) >> 1
```

**Explicit (weighted_bipred_idc = 1)** uses the weight tables of Chapter 7.7 - one table per list in the slice header, indexed by `ref_idx` of **that list** - with `logWD`, `w0, o0` (list-0 table, entry `refIdxL0`), `w1, o1` (list-1 table, entry
`refIdxL1`); an entry whose flag is 0 has weight `2^logWD` and offset 0:

```
only list 0:   logWD >= 1:  Clip1( ((pred0 * w0 + 2^(logWD-1)) >> logWD) + o0 )        logWD = 0:  Clip1( pred0 * w0 + o0 )
only list 1:   the same with pred1, w1, o1
both lists:    Clip1( ((pred0 * w0 + pred1 * w1 + 2^logWD) >> (logWD + 1)) + ((o0 + o1 + 1) >> 1) )
```

Luma uses `luma_log2_weight_denom` and the luma weights, Cb and Cr use `chroma_log2_weight_denom` and their own weights/offsets. With all flags 0 this is exactly the default average. For bi-predicted blocks a conforming stream keeps `-128 <= w0 + w1 <= 127` when `logWD = 7` and `<= 128` otherwise. (Offsets are
in sample units, 8-bit video.) **Implicit** weights (`weighted_bipred_idc = 2`: weights from POC distances; one-list predictions unweighted) are Chapter 7.9. The PPS flag `weighted_pred_flag` affects only P slices and `weighted_bipred_idc` only B slices.
T8 has `weighted_bipred_idc = 0`; the synthetic stream `synth_wbi` exercises idc 1.

### 3.11 Deblocking in B pictures (8.7.2.1)

Only the motion part of the boundary strength changes: when neither side is intra and neither has coefficients, `bS = 1` if the prediction of the two 4x4 blocks differs in a way that matters, else 0. Each block has **one or two** (reference picture, vector) pairs. The comparison is of
**pictures**, never of list or `ref_idx` numbers (the same picture may sit in both lists and at several indices):

| the two blocks | bS = 1 when |
|---|---|
| different number of vectors (one vs two) | always |
| different reference pictures (as sets: `{A}` vs `{B}`, or `{A, B}` vs `{A, C}`; `{A, A}` differs from `{A, B}`) | always |
| one vector each, same picture | `abs(dx) >= 4` or `abs(dy) >= 4` (quarter samples) |
| two vectors, **two different** pictures `A, B` on both sides | pair the vectors **by picture** (the vector to A with the vector to A, B with B, whichever list they came from): bS = 1 if either pair differs by >= 4 in a component |
| two vectors, **both to the same picture** A, on both sides | bS = 1 only if **both** pairings fail: `(P_L0 vs Q_L0 or P_L1 vs Q_L1 differ by >= 4)` **and** `(P_L0 vs Q_L1 or P_L1 vs Q_L0 differ by >= 4)` |

Intra edges (bS 3 / 4) and coefficient edges (bS 2) are as in Chapter 7.5. The "different reference pictures" test must see through the lists: a block predicted from list 1 entry 0 and one from list 0 entry 1 use the same picture if the lists say so. The vertical-difference limit is 4 for frame macroblocks.

### 3.12 Output order

Your decoder now has two orders: the decoding order of pictures and the display order. This chapter's shortcut (until Chapter 9.1): hold the decoded pictures of one IDR period (an IDR picture, or a picture with MMCO 5, starts a new period; the POC restarts there)
and write them sorted by POC; a non-reference B picture is decoded, kept for output, and not entered into the DPB for reference. Write the previous period before starting the next one. The yuv file the harness checks is in this order; ffmpeg's default output order is the same
for the streams in this chapter (they all carry `max_num_reorder_frames` or are x264 streams).

## 4. The project: exactly what to build

Extend your P-picture decoder (Chapter 7.7) so that it decodes B pictures:

1. **Parsing**: `mb_skip_run` and `mb_type` for B slices (including intra macroblocks at offset 23), `sub_mb_type`, the four syntax loops of 3.5, `te(v)` per list, the shared CBP/residual path. Keep a record per macroblock: type name, mode and `ref_idx`/`mvd` of every
   partition and list.
2. **Vectors**: 3.6, per list, stored per 4x4 block for both lists.
3. **POC**: type 0 with MSB handling (and 1 and 2 if you support them).
4. **Lists**: initial lists of 3.8 for B slices, swap rule, truncation, modification of both lists; keep the P rules for P slices; do not store non-reference pictures.
5. **Prediction**: per partition: one or two interpolations, default average, or the explicit weights for `weighted_bipred_idc = 1`; P slices keep Chapter 7.7's weighting.
6. **Deblocking**: bS of 3.11 over the stored per-list motion data.
7. **Output**: pictures in display order (3.12).

Verify: T8 and the other files of section 6 are **byte-identical** to ffmpeg. Optional, to find bugs quickly: write the three dumps of section 5. Recipes that decode correctly once this chapter is done: **T5, T6, T6_ref16, T7 and T8**
(plus all intra recipes). T9 uses direct prediction and needs Chapter 7.9.

## 5. Harness input format

**Primary: the pixels.** `your.yuv`: every frame in display order, 8-bit planar yuv420p, cropped as the SPS says, no padding, no headers (Chapter 5.1 format).

Three **optional text dumps**; all are line oriented, tokens separated by single spaces, `key=value` without spaces in the value, the `key=value` tokens of a line in any order, blank lines and lines starting with `#` ignored, all numbers decimal.

### 5.1 `--parse-dump`: macroblock syntax (Chapter 7.1's dump plus two keys)

```
SLICE <i> type=<I|P|B> first_mb=<n> pic_size_in_mbs=<n> header_end_bit=<n>       one per slice NAL unit (types 1 and 5), stream order
MB <i> <addr> name=<..> mb_type=<..> sub=<..> refidx=<..> mvd=<..> refidx1=<..> mvd1=<..> cbp=<..> qp_delta=<..> [other keys]    one per macroblock, ascending address, skipped ones too
PCM <i> <addr> align_bits=<n> sum_y=<n> sum_cb=<n> sum_cr=<n>                    after each I_PCM MB line
END <i> mbs=<n> status=<complete|error> end_bit=<n>
HIST <name>:<count> ...                                                            last line, names alphabetical
```

`i` is the 0-based index of the slice NAL unit; `end_bit` is the bit position of the `rbsp_stop_one_bit` counted from the first bit of the NAL header. The harness compares the keys above (the Phase 4 and 7.1 keys such as `bit`, `nc`, `coef` are ignored here; a 7.1 dump
without `refidx1` / `mvd1` is accepted for macroblocks that have none).

| key | value |
|---|---|
| `name` | `B_Skip`, `B_Direct_16x16`, `B_L0_16x16`, `B_L1_16x16`, `B_Bi_16x16`, `B_<a>_<b>_16x8` / `_8x16` (a, b in L0, L1, Bi), `B_8x8`, the P names of 7.1, intra names (`I_NxN`, `I_PCM`, `I_16x16_<mode>_<chroma cbp>_<luma flag>`) |
| `mb_type` | the value **as coded** (B: 0..22, intra 23..48; P: 0..4 / 5..30); `skip` for a skipped macroblock |
| `sub` | the four `sub_mb_type` values of a `B_8x8` (comma separated); `-` otherwise |
| `refidx` | `ref_idx_l0` of every partition (sub-macroblock for B_8x8) that **uses list 0**, syntax order, comma separated; `0` where it is not coded (active count 1); `-` if none (intra, skip, direct, list-1-only macroblock) |
| `mvd` | `x:y` of every prediction block that uses list 0 (sub-partitions in order), `-` if none |
| `refidx1`, `mvd1` | the same for list 1 |
| `cbp`, `qp_delta` | as in 7.1 (`-` where not present) |

For P macroblocks `refidx`/`mvd` are Chapter 7.1's (one entry per partition / prediction block) and `refidx1 = mvd1 = -`. Examples (from T8, shortened):

```
MB 2 46 name=B_Bi_16x16 mb_type=3 sub=- refidx=0 mvd=48:0 refidx1=0 mvd1=-56:-24 cbp=1 qp_delta=0 ...
MB 2 47 name=B_L0_L1_16x8 mb_type=8 sub=- refidx=0 mvd=-16:0 refidx1=0 mvd1=0:0 cbp=45 qp_delta=0 ...
MB 2 49 name=B_8x8 mb_type=22 sub=3,2,1,2 refidx=0,0 mvd=56:0,-51:0 refidx1=0,0,0 mvd1=-56:0,-1:0,40:16 cbp=16 qp_delta=0 ...
```

(The second line: partition 0 uses list 0, partition 1 list 1 -> one entry each. The third line: sub-macroblocks 0 and 2 use list 0 -> two `refidx` entries and two `mvd` entries; sub-macroblocks 0, 1, 3 use list 1 -> three entries.) `python3 test_bslice.py --print-expected parse T8.ts out.txt` writes the whole expected dump.

### 5.2 `--lists-dump`: reference lists and DPB (Chapter 7.6's log + B)

```
SLICE <pic> <slice> type=<P|B> frame_num=<n> poc=<n> active=<n> list0=<slot>,...  [active1=<n> list1=<slot>,...]      one line per P and B slice
DPB <pic> short=<ref>,... long=<ref>,... maxlt=<n|none>                                                                one line per coded picture
```

Everything is as in Chapter 7.6: `<pic>` is the 0-based **decoding-order** index of the coded picture, `<slice>` the slice index inside it; a `<slot>` is `<pic>:<PicNum>`, `<pic>:L<LongTermPicNum>` or `-` ("no reference picture"), exactly `active` (`active1`) slots **after** modification and truncation; `short=` lists
`<pic>:<frame_num>` of the short-term set after the picture's marking, most recently decoded first; non-reference pictures (and their `DPB` line repeating the unchanged state) included. New: `type=B` lines carry `active1=` and `list1=`; `poc=` is the PicOrderCnt of the picture
(required on B lines, checked on P lines if present). Lines are ordered: a picture's `SLICE` lines, then its `DPB` line.

```
SLICE 4 0 type=B frame_num=3 poc=6 active=2 list0=1:1,0:0 active1=1 list1=3:2
DPB 4 short=3:2,1:1,0:0 long=- maxlt=none
```

### 5.3 `--mv-dump`: final motion vectors of both lists

```
MV <pic> <addr> l0=<16 entries> l1=<16 entries>        one line per INTER macroblock (P_Skip, P and B types), ascending <pic> then <addr>; no line for intra macroblocks
```

`<pic>` as above. Each list has exactly 16 comma-separated entries, one per 4x4 luma block in **raster order** (row by row, not luma4x4BlkIdx order): `refIdx:mvx:mvy` (quarter samples) of the partition covering the block, or `-` if the block does not use that list.
(`B_Skip` / direct blocks are Chapter 7.9; the harness refuses the mv channel for a stream that has them.) Example: a P_Skip: `MV 1 0 l0=0:0:0,...(16 entries) l1=-,-,-,...(16 entries)`.

## 6. Running the harness

```
python3 test_bslice.py T8.ts your.yuv                                   # pixels (the milestone)
python3 test_bslice.py T8.ts your.yuv --parse-dump p.txt --lists-dump l.txt --mv-dump m.txt
python3 test_bslice.py T8.ts --lists-dump l.txt                         # only one channel
python3 test_bslice.py --print-expected parse|lists|mv T8.ts out.txt    # the expected text, to diff against yours
python3 test_bslice.py --make-variants DIR                              # writes the test inputs below into DIR
python3 test_bslice.py --self-test                                      # checks the harness itself (3 to 4 minutes)
```

`T8.ts` is `python3 -m common.testfiles T8` (in `.cache/`). Exit 0 = identical, 1 = different, 2 = unusable input / output (missing file, malformed dump, a stream with direct prediction when you ask for a dump that needs it, or a disagreement between the harness's reference and an independent source - that one is the harness's problem).

**Truth.** Pixels: ffmpeg's decode in its default output order (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`), compared with `common/yuvcmp.py`. The dumps come from the reference implementation in `spoiler_reference/` and `common/inter_ref.py`. Before your dump is compared, the reference is checked against independent sources:
every slice must end exactly on its stop bit; ffmpeg's `-debug mb_type` map must show the same class (`>` list 0 only, `<` list 1 only, `X` both) and partition shape for every macroblock; `-debug qp` the same QP_Y; `-debug mmco` the same short- and long-term frames with the same POC *differences* after every marking; the
motion vectors that ffmpeg exports (`-flags2 +export_mvs`: `source` -1 = list 0, +1 = list 1, one vector per macroblock partition at 8x8 granularity) equal the reference's; synthetic streams come with a generator manifest (every mb_type, sub_mb_type, ref_idx, mvd). The reference decoder reproduces ffmpeg's pixels on every test stream (the self-test checks it).

Inputs from `--make-variants` (the x264 files are 1 second, all `cabac=0:direct=none:weightb=0`):

| file | what it exercises |
|---|---|
| `T8.ts` | the milestone: `bframes=2`, 3 reference frames, P weights, POC wrap, 50 frames |
| `b_bf1.ts`, `b_bf3.ts` | one B between anchors; `bframes=3` with `keyint=10` (several IDR periods: POC restarts) |
| `b_pyr.ts` | `b-pyramid=normal`: B pictures that are references (they appear in later lists, with POC between others) |
| `b_slices.ts` | `slices=3`: lists, ref counts and vector neighbours per slice |
| `b_nodeblock.ts` | no loop filter: pixels without the bS rules |
| `b_wpfade.ts` | `weightp=2` fade with B pictures: P weights next to B slices |
| `b_part.ts`, `b_badapt.ts` | `partitions=all`, 6 references; `bframes=4` with adaptive B placement |
| `synth_basic.264` | I, P, B pictures with random structure; all non-direct B types; no modification |
| `synth_types.264` | every `mb_type` 1..22 and sub type 1..12 many times; 3 active entries per list |
| `synth_lists.264` | 6 references, B pictures as references, modification commands on **both** lists with repeated targets, 2 slices |
| `synth_tail.264` | B pictures whose references are all in the past (the swap rule), small active counts (swap before truncation) |
| `synth_dbk.264` | deblocking on, small vectors: bS for one- and two-vector blocks, same-picture pairs |
| `synth_wbi.264`, `synth_wp_only.264` | `weighted_bipred_idc = 1` with tables for both lists, negative weights/offsets, clipping; P weights only |
| `synth_idr2.264` | several IDR periods and a long-term IDR: output order, POC restart, long-term frames in B lists |
| `synth_ltref.264` | an IDR marked long-term: list 0 / list 1 with short-term entries followed by the long-term frame |
| `synth_slices.264` | up to 3 slices per picture, P and B slices mixed in one picture, different active counts |
| `synth_intra.264` | many I_PCM and Intra16x16 macroblocks inside P and B slices (neighbour handling, bS 3/4) |
| `synth_mvfar.264` | large `mvd`: vectors far outside the picture in both lists |

The synthetic streams (96x80) use a VUI `max_num_reorder_frames`, POC type 0 with a 5-bit lsb (wraps every 16 pictures), residual-free inter macroblocks and no direct macroblocks. The expected `refidx`/`mvd` text of every macroblock comes from the generator, independent of the parser.

## 7. Reading failures / debugging tips

**Pixels.** On a difference the harness decodes the stream with the reference decoder up to the first wrong frame and prints: the frame's picture index and POC, the first wrong macroblock, its type, the slice's `list0`/`list1`, every prediction block with its lists, `ref_idx`, the *picture* each
index names, and its vectors; the size of the differences; and whether **your whole frame equals the reference decoder with one deliberately wrong rule** (it then names it - that is almost certainly your bug). The rules it knows:

| wrong rule | visible as |
|---|---|
| `bi_round_down` | +-1 errors in bi-predicted partitions |
| `bi_only_l0` / `l1_ref_from_l0` | bi partitions wrong; list-1 pictures never used |
| `bi_w_offset_sum`, `bi_w_no_round`, `bi_w_swapped`, `l1_uni_weights_l0` | explicit bi-prediction (`synth_wbi`) off by small amounts, or list-1-only partitions with the wrong table |
| `b_l0_no_poc_order`, `b_l0_future_first`, `b_l0_past_ascending`, `p_lists_for_b` | list 0 in the wrong order |
| `b_l1_same_as_l0`, `b_l1_future_desc`, `b_l1_past_ascending`, `b_lt_first` | list 1 in the wrong order, or long-term frames in front |
| `b_no_swap`, `b_swap_after_trunc`, `b_swap_always` | the swap rule missing, tested too late (after truncation) or always applied |
| `b_l1_mod_uses_l0` | list-1 modification commands ignored |
| `poc_no_msb` | wrong order / wrong lists after the lsb wraps (the first wrong frame is late in the stream) |
| `nonref_b_stored`, `output_decode_order`, `output_ignores_idr` | non-reference B pictures in the DPB; frames written in decoding order; one POC sort over several IDR periods |
| `b_mvp_l1_uses_l0_nb`, `b_nb_unused_list_unavail`, `b_nb_unused_list_ref0` | vector prediction mixing the lists / treating a neighbour that does not use list X wrongly (3.6) |
| `b_bs_pair_by_list`, `b_bs_same_ref_straight_only`, `b_bs_same_ref_either`, `b_bs_count_ignored`, `b_bs_ref_set_ignored`, `ref_idx_compare`, `mv_thr_5` | deblocking bS between B blocks (3.11); errors only at partition edges |

If your frame is the right frame at the wrong place ("your frame N is exactly ffmpeg's frame M") it is an output-order problem.

**Debugging order.** (1) `--parse-dump`: every slice must end exactly (`status=complete`, `end_bit` equal); the first differing `MB` line and the hint say which table or which loop is wrong. (2) `--lists-dump`: compare the lists before looking at pixels - a wrong list shows up as a wrong
picture in an otherwise right prediction. Remember the swap rule and truncation. (3) `--mv-dump`: the first differing macroblock tells you whether the predicted vector or the stored data is wrong. (4) Pixels with `b_nodeblock.ts` (no loop filter), then with deblocking.

| symptom | likely cause |
|---|---|
| the first B slice does not end where it should, mb_type values look random | `mb_skip_run` forgotten in B slices, or the intra offset (23, not 5) |
| right partition counts, wrong `refidx` lists | order of the loops (all l0 refs, all l1 refs, all l0 mvds, all l1 mvds), te range of the wrong list |
| list 1 empty or equal to list 0 | the swap rule; or list 1 built like list 0 |
| T8 right for 16 frames, wrong afterwards | POC MSB |
| everything right but the last B frames of a group | output order, or the DPB keeping non-reference B pictures |

## 8. What's next

After this chapter these recipes decode byte-identical: **T5, T6, T6_ref16, T7 and T8** (and every intra recipe). **T9** (`direct=spatial` / `direct=temporal`, `weightb=1`, `b-pyramid=normal`) still needs Chapter 7.9: `B_Skip`, `B_Direct_16x16` and `B_Direct_8x8`
(spatial and temporal direct prediction, with the co-located picture's stored motion data) and implicit weights (`weighted_bipred_idc = 2`).
