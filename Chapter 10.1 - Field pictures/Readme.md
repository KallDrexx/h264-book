# Chapter 10.1 - Field pictures

## 1. Goal and the project

Until now every picture you decoded was a **frame**. Broadcast interlaced video is often coded as **field pictures** (PAFF: picture-adaptive frame/field coding): each picture holds only the odd or only the even lines of the frame, and a frame is two such pictures. In this chapter you teach your decoder to read `field_pic_flag` / `bottom_field_flag`, decode a field into alternate lines of a frame buffer, compute field picture order counts, pair the two fields of a frame, and output ordinary cropped frames.

Scope: **intra-only** field streams (I slices, CAVLC and CABAC, 4x4/8x8/16x16 intra, I_PCM, deblocking). Everything about predicting fields from other fields (reference lists, marking, direct modes) is Chapter 10.2. MBAFF (frame pictures with field macroblock pairs, `mb_adaptive_frame_field_flag = 1`) is Chapters 10.3-10.4.

You write: (A) a slice-level dump per coded picture and per output frame, and (B) the decoded frames as raw yuv. Both are checked by `test_fields.py`.

## 2. Big picture

An interlaced camera captures every second line at one instant and the lines in between about 1/50 or 1/60 s later; each such half-picture is a **field** (the **top field** holds frame lines 0, 2, 4, ..., the **bottom field** lines 1, 3, 5, ...). Coding each field as its own picture lets motion prediction work inside one instant. A **frame** is the two fields woven together line by line. A stream may mix frame pictures and field pictures freely, picture by picture.

Good news: a field picture is decoded like a small frame picture of `PicWidthInMbs` x (`FrameHeightInMbs / 2`) macroblocks; macroblock addresses, intra neighbours, CAVLC `nC`, CABAC neighbour rules and the deblocking filter all work inside that small picture and know nothing about the other field. Only **five things change**:

1. the headers (new flags, new sizes, new picture boundary rule, POC rules);
2. where the samples go (every second frame line);
3. the **coefficient scan** (field scan instead of zig-zag) and, for CABAC, the **coefficient contexts**;
4. the deblocking strength on horizontal macroblock edges (3 instead of 4);
5. pairing fields into frames, and what to do with a field that has no partner.

x264 cannot help you here: its `--interlaced` mode is MBAFF (`field_pic_flag` is always 0) and `--fake-interlaced` only sets flags. No common free encoder writes PAFF from raw video. That is why this chapter's streams are **synthetic** (section 6) and the pixel truth is ffmpeg decoding them.

## 3. Concepts in detail

### 3.1 Recap you need from earlier phases

* **SPS** fields: `frame_mbs_only_flag` (1 = every picture is a frame; 0 = fields and/or MBAFF possible), `mb_adaptive_frame_field_flag` (present only when `frame_mbs_only_flag = 0`; 0 here), `pic_width_in_mbs_minus1`, `pic_height_in_map_units_minus1`, `log2_max_frame_num_minus4`, `pic_order_cnt_type` and its parameters, the cropping offsets.
* `FrameHeightInMbs = (2 - frame_mbs_only_flag) * PicHeightInMapUnits` (7-18). With `frame_mbs_only_flag = 0` a "map unit" is a pair of macroblock rows.
* **Slice header** order (relevant part): `first_mb_in_slice, slice_type, pps_id, frame_num, [field_pic_flag, [bottom_field_flag]], [idr_pic_id], POC fields, ...`. `field_pic_flag` is read only if `frame_mbs_only_flag = 0`; `bottom_field_flag` only if `field_pic_flag = 1`. Both default to 0 when absent. A *picture* is a frame (`field_pic_flag = 0`), a top field (1, 0) or a bottom field (1, 1). All slices of one picture carry the same values.
* **DPB and bumping** (Chapter 9.1): decoded pictures wait in the decoded picture buffer; when it is full, or an IDR flushes it, the one with the smallest POC is output, so output is in ascending POC within each IDR period. With fields the DPB is made of *frame stores*: a store holds one frame, one field, or the two fields of one frame, and output takes a whole store, so a field pair is output once, as one frame.
* **Output** is the cropped 8-bit yuv420p frame (Chapters 5.1 and 9.1): Y plane, then Cb, then Cr, no padding.

### 3.2 Sizes and addresses

```
PicHeightInMbs = FrameHeightInMbs / (1 + field_pic_flag)
PicSizeInMbs   = PicWidthInMbs * PicHeightInMbs
```

`first_mb_in_slice` is a macroblock address in the **picture**: for a field picture it ranges over `0 .. PicSizeInMbs-1` of the *field*, and the last slice of a field ends at the field's last macroblock (CAVLC: no more data; CABAC: `end_of_slice_flag`). `MaxFrameNum` is unchanged (still `2^(log2_max_frame_num_minus4+4)`).

**Example (the suite).** `pic_width_in_mbs_minus1 = 4`, `pic_height_in_map_units_minus1 = 2`, `frame_mbs_only_flag = 0`: width 80, `FrameHeightInMbs = 2*3 = 6` (96 lines). A frame picture has `PicSizeInMbs = 30`; a field picture has `PicHeightInMbs = 3`, `PicSizeInMbs = 15` (48 field lines), slice addresses 0..14.

### 3.3 Where the samples go

Decode the field into its own small picture. Then, for field line `y` (luma, 0-based):

| field | frame line |
|---|---|
| top field (`bottom_field_flag = 0`) | `2*y` |
| bottom field | `2*y + 1` |

Chroma planes use the same rule on chroma lines (`2*y + parity`). Equivalent view: write into the frame buffer with `stride = 2 * frame_stride` and start offset `0` (top) or `frame_stride` (bottom). Example: line 5 of the top field is frame line 10; line 5 of the bottom field is frame line 11; chroma line 3 of the bottom field is chroma frame line 7.

Because of this, **intra prediction, the deblocking filter and CABAC/CAVLC neighbour rules never see the other field**: the "row above" a field macroblock is the row 2 frame lines above.

### 3.4 Residual: field scan and CABAC field contexts

Inverse scanning (8.5.6, 8.5.7) maps the coefficient list to the 4x4 / 8x8 matrix. In **field pictures** (and field macroblocks of MBAFF, later) the **field scan** replaces the zig-zag. The luma DC of Intra16x16 (a 4x4 scan), the 4x4 luma, the Intra16x16 AC (positions 1..15), the chroma AC (positions 1..15) and the 8x8 luma use it. Chroma DC (2x2 raster `c0 c1 / c2 c3`) is unaffected. Tables, scan position -> raster index (`row*N + column`):

```
4x4 field scan : 0 4 1 8 12 5 9 13 2 6 10 14 3 7 11 15
4x4 frame zigzag (for comparison): 0 1 4 8 5 2 3 6 9 12 13 10 7 11 14 15
8x8 field scan :
  0  8 16  1  9 24 32 17  2 25 40 48 56 33 10  3
 18 41 49 57 26 11  4 19 34 42 50 58 27 12  5 20
 35 43 51 59 28 13  6 21 36 44 52 60 29 14 22 37
 45 53 61 30  7 15 38 46 54 62 23 31 39 47 55 63
```

For CAVLC nothing else changes (with `transform_size_8x8_flag = 1` the four interleaved 4x4 parses are merged into 64 levels first, then the 8x8 field scan applies).

**CABAC.** Only two syntax elements have separate context sets for field-coded blocks: `significant_coeff_flag` and `last_significant_coeff_flag` (Table 9-34). `coded_block_flag` and `coeff_abs_level_minus1` are shared with frames. `ctxIdxOffset` (add the usual `ctxBlockCatOffset` for categories 0-4: 0, 15, 29, 44, 47):

| element | frame-coded | field-coded |
|---|---|---|
| significant_coeff_flag, ctxBlockCat < 5 | 105 | **277** |
| last_significant_coeff_flag, ctxBlockCat < 5 | 166 | **338** |
| significant_coeff_flag, ctxBlockCat 5 (8x8) | 402 | **436** |
| last_significant_coeff_flag, ctxBlockCat 5 | 417 | **451** |

The `(m, n)` initialisation values of ctxIdx 277..398 and 436..459 are the standard's (Tables 9-18 ff.); make sure your Chapter 8.1 table holds all 1024 entries. For categories < 5 `ctxIdxInc` = scan position (as for frames; chroma DC: `min(pos / NumC8x8, 2)`). For 8x8 blocks `ctxIdxInc` of `significant_coeff_flag` comes from a table indexed by scan position that differs between frame and field (Table 9-43); `last_significant_coeff_flag` uses the same table for both:

```
8x8 significant_coeff_flag ctxIdxInc, FIELD (positions 0..62):
 0 1 1 2 2 3 3 4 5 6 7 7 7 8 4 5   6 9 10 10 8 11 12 11 9 9 10 10 8 11 12 11
 9 9 10 10 8 11 12 11 9 9 10 10 8 13 13 9   9 10 10 8 13 13 9 9 10 10 14 14 14 14 14
8x8 significant_coeff_flag ctxIdxInc, FRAME (positions 0..62), for comparison:
 0 1 2 3 4 5 5 4 4 3 3 4 4 4 5 5   4 4 4 4 3 3 6 7 7 7 8 9 10 9 8 7
 7 6 11 12 13 11 6 7 8 9 14 10 9 8 6 11   12 13 11 6 9 14 10 9 11 12 13 11 14 10 12
last_significant_coeff_flag ctxIdxInc (both): 0, then 1 x15, 2 x16, 3 x8, 4 x8, 5 x4, 6 x4, 7 x4, 8 x3
```

Which table to use depends on the **picture** (`field_pic_flag = 1`) here; with MBAFF it will depend on the macroblock.

### 3.5 Deblocking in a field picture

The filter runs on the field picture, edge by edge, exactly as for a frame, with one change in the boundary strength. For an intra macroblock:

| edge | frame picture | field picture |
|---|---|---|
| left macroblock edge (vertical) | 4 | 4 |
| top macroblock edge (horizontal) | 4 | **3** |
| internal edges | 3 | 3 |

Reason: vertically adjacent field lines are 2 frame lines apart, so the strong filter is too aggressive across them (8.7.2.1: bS 4 needs "frame macroblocks" for horizontal edges). Everything else (indexA/indexB, alpha, beta, tc0, `disable_deblocking_filter_idc` 0/1/2, offsets, `transform_size_8x8_flag` skipping edges 1 and 3 for luma, I_PCM qP = 0) is unchanged. `disable_deblocking_filter_idc = 2` compares slices *within the field*. Filtering never mixes the two fields.

### 3.6 Picture boundaries (7.4.1.2.4)

A slice starts a **new picture** when it differs from the previous slice in any of: `frame_num`, `pic_parameter_set_id`, `field_pic_flag`, **`bottom_field_flag`**, `nal_ref_idc` (one of them 0), `nal_unit_type` 5 vs not (IdrPicFlag), `idr_pic_id` (both IDR), and for POC type 0 `pic_order_cnt_lsb` or `delta_pic_order_cnt_bottom`, for type 1 `delta_pic_order_cnt[0]` / `[1]`. The two fields of a frame always differ in `bottom_field_flag`. With POC type 2 (no POC syntax) `bottom_field_flag` is the *only* difference between the two fields of a pair: a decoder that ignores it merges two pictures into one.

### 3.7 Picture order count of fields (8.2.1)

Each *field* has its own POC: `TopFieldOrderCnt` or `BottomFieldOrderCnt`. A frame has both. `PicOrderCnt(picture)` = `Min(top, bottom)` for a frame or complementary pair, the field's own value for a single field.

**Type 0.** `pic_order_cnt_lsb` (u(v), `log2_max_pic_order_cnt_lsb_minus4+4` bits). `PicOrderCntMsb` from the previous *reference* picture's `(msb, lsb)` (both 0 after an IDR): if `lsb < prevLsb` and `prevLsb - lsb >= MaxLsb/2` then `msb = prevMsb + MaxLsb`; if `lsb > prevLsb` and `lsb - prevLsb > MaxLsb/2` then `msb = prevMsb - MaxLsb`; else `msb = prevMsb`. Then
* frame: `Top = msb + lsb`, `Bottom = Top + delta_pic_order_cnt_bottom`;
* top field: `Top = msb + lsb`;  bottom field: `Bottom = msb + lsb`.

`delta_pic_order_cnt_bottom` exists only for **frame** pictures, and only if the PPS flag `bottom_field_pic_order_in_frame_present_flag` is 1. (The encoder picks the lsb values; a bottom-first pair normally gives the bottom field the smaller POC, e.g. `Bottom = 2k`, `Top = 2k+1`.)
*Example* (MaxLsb 16): previous reference lsb 14 / msb 0, new lsb 2: `14 - 2 = 12 >= 8` so msb = 16; a top field gets POC 18 and its bottom partner (lsb 3) 19. A frame with lsb 5, msb 16 and delta -1 has Top 21, Bottom 20.

**Type 1.** Frame-number based. `FrameNumOffset` = 0 after IDR, else previous + `MaxFrameNum` when `frame_num` wrapped. With `n = num_ref_frames_in_pic_order_cnt_cycle`: `absFrameNum = FrameNumOffset + frame_num` (0 if n = 0), minus 1 if `nal_ref_idc = 0` and it is > 0. If `absFrameNum > 0`: `cycleCnt = (absFrameNum-1)/n`, `inCycle = (absFrameNum-1)%n`, `expected = cycleCnt * sum(offset_for_ref_frame) + sum(offset_for_ref_frame[0..inCycle])`, else 0. A non-reference picture adds `offset_for_non_ref_pic`. Then
* frame: `Top = expected + delta_pic_order_cnt[0]`, `Bottom = Top + offset_for_top_to_bottom_field + delta_pic_order_cnt[1]` ([1] only for frames with the PPS flag);
* top field: `Top = expected + delta_pic_order_cnt[0]`;
* bottom field: `Bottom = expected + offset_for_top_to_bottom_field + delta_pic_order_cnt[0]`.

*Example* (cycle `[4, 2]`, top_to_bottom 1): reference `frame_num` 3: `absFrameNum 3`, `cycleCnt 1`, `inCycle 0`, `expected = 1*6 + 4 = 10`; a top field with delta 0 has POC 10, the bottom field of the same frame 11.

**Type 2.** Output order = decoding order, no POC syntax. `tempPicOrderCnt = 2*(FrameNumOffset + frame_num)` (minus 1 for non-reference pictures, 0 for IDR). A frame gets Top = Bottom = temp; **a field gets temp in its own parity**. So both fields of a reference pair (same `frame_num`) have the **same** POC; a non-reference picture sorts one before the next reference picture's POC (`2*fn - 1`).

### 3.8 Pairing fields (3.30, 3.31, 7.4.3)

The decoder keeps a "first field waiting for its partner". A field picture is the **second field** of a frame when all hold:
* the previous picture in decoding order was a field that is still unpaired (a "first field");
* it has the **opposite parity** (`bottom_field_flag` differs);
* **same `frame_num`**;
* it is **not an IDR picture**;
* both are reference fields, or both are non-reference fields (`nal_ref_idc` zero-ness equal).

Then both fields live in one frame store: a *complementary reference field pair* or *complementary non-reference field pair*. An IDR picture may be a first field: an **IDR top (or bottom) field followed by a non-IDR field of the other parity** (an I or P field, `nal_unit_type` 1, same `frame_num` 0, `nal_ref_idc != 0`) is a normal reference pair - this is how every PAFF stream starts. The second field must not be IDR.

`frame_num` obeys the usual rule (it advances after each reference *frame or pair*): both fields of a frame share it; the next picture uses `PrevRefFrameNum + 1`; non-reference pictures use `PrevRefFrameNum + 1` too (they do not advance it).

A field that never gets a partner is a **non-paired field** (a "lone" field): a field followed by a frame, by a same-parity field, or by an opposite-parity field with a different `frame_num`. By the standard it occupies a frame store of its own and is output as a frame; the lines of the missing field have no defined content (decoders copy or double lines). **ffmpeg does not output lone fields at all** (anywhere in the stream), so the harness treats them specially (5.3). ffmpeg also rejects a reference field paired with a non-reference field; the suite has none.

### 3.9 Cropping

`CropUnitX = 2`, `CropUnitY = 2 * (2 - frame_mbs_only_flag)` for 4:2:0. With `frame_mbs_only_flag = 0` the vertical unit is **4** lines (a pair of macroblock rows keeps fields balanced). Output width `16*W - CropUnitX*(left+right)`, height `16*FrameHeightInMbs - CropUnitY*(top+bottom)`. *Example*: offsets (left 1, right 2, top 1, bottom 3) on 80x96: width `80 - 2*3 = 74`, height `96 - 4*4 = 80`, top-left sample at (2, 4).

## 4. The project: exactly what to build

Starting from your intra decoder (Phases 5 and 8; CAVLC + CABAC, 4x4/8x8/16x16, deblocking):

1. **Headers.** Parse `frame_mbs_only_flag` / `mb_adaptive_frame_field_flag` (reject `frame_mbs_only_flag = 0` with `mb_adaptive_frame_field_flag = 1` and `field_pic_flag = 0` pictures; they are MBAFF), and in the slice header `field_pic_flag` / `bottom_field_flag`. Derive `PicHeightInMbs`, `PicSizeInMbs`; bound `first_mb_in_slice` by them.
2. **Picture boundaries** with the full rule of 3.6.
3. **POC** of frames and fields for types 0, 1, 2 (3.7).
4. **Pairing** into frame stores and **output** per store in POC order within each IDR period (your Chapter 9.1 bumping, applied to stores). Lone fields: write a frame whose present field is decoded and whose other lines are anything you like.
5. **Decode a field as a picture** of `PicHeightInMbs` rows using a field-sized working picture (or stride doubling), with the **field scan** and the **field CABAC contexts**; deblock the field with bS 3 on horizontal intra MB edges; interleave into the frame store.
6. **Crop** with `CropUnitY = 4` and write yuv420p.
7. Write the **dump** (section 5.1).

Verify: all suite streams pass (`--check`), and - when you have them - the intra-only field streams of the H.264.1 conformance suite (not available offline here; use `--stream` with the suite's yuv). Streams with P/B fields wait for Chapter 10.2.

## 5. Harness input format

### 5.1 The dump: `<case>.dump`

Plain text, one line per item: the word `PIC` or `FRAME`, its decimal index, then `name=value` words, separated by spaces. All numbers are decimal (POCs may be negative); `-` means "not defined". The `name=value` words may come in any order and extra ones are ignored. Blank lines and lines starting with `#` are ignored; `PIC` and `FRAME` lines may be interleaved, but each kind must be in its own order.

```
PIC <n> struct=<F|T|B> frame_num=<fn> idr=<0|1> ref=<0|1> slices=<k> mbs=<PicSizeInMbs> poc=<PicOrderCnt> top=<TopFieldOrderCnt|-> bot=<BottomFieldOrderCnt|-> store=<s> second=<0|1>
FRAME <k> store=<s> fields=<F|TB|BT|T|B> top=<..|-> bot=<..|-> poc=<..>
```

* `PIC`: one line per **coded picture** (frame or field) in **decoding order**, `n` from 0. `struct` F frame, T top field, B bottom field. `ref` = `nal_ref_idc != 0`. `slices` = number of slices of the picture. `mbs` = `PicSizeInMbs` of that picture (30 for a frame, 15 for a field in the suite). `poc` = `PicOrderCnt` of the picture (frame: min of top/bot). `top` / `bot`: the field order count(s) the picture defines (`-` for the parity a field picture does not have). `store` = frame store number, counting stores from 0 in order of creation (a first field, a frame or a lone field creates a store; a second field reuses its first field's). `second` = 1 for a second field.
* `FRAME`: one line per **output frame**, in **output order**, `k` from 0. `fields` = what the store holds in arrival order (`F` frame, `TB` / `BT` complete pair, `T` / `B` lone field). `top` / `bot` the store's field order counts (`-` if missing), `poc` = the minimum of those present.
* Output order = ascending `poc` within each IDR period, periods in decoding order (all suite streams stay inside the reorder depth of their DPB).

Example (stream `cavlc_lone_fields`, first lines):
```
PIC 0 struct=T frame_num=0 idr=1 ref=1 slices=3 mbs=15 poc=0 top=0 bot=- store=0 second=0
PIC 1 struct=B frame_num=0 idr=0 ref=1 slices=2 mbs=15 poc=1 top=- bot=1 store=0 second=1
PIC 2 struct=T frame_num=1 idr=0 ref=1 slices=1 mbs=15 poc=2 top=2 bot=- store=1 second=0
FRAME 0 store=0 fields=TB top=0 bot=1 poc=0
FRAME 1 store=1 fields=T top=2 bot=- poc=2
FRAME 2 store=2 fields=B top=- bot=4 poc=4
```

### 5.2 The pixels: `<case>.yuv`

All output frames in output order (one per `FRAME` line, lone fields included), each the **cropped** yuv420p frame (Y, Cb, Cr, no padding, 8 bit).

### 5.3 Lone fields in the pixel check

For frames and complete pairs the truth is **ffmpeg's** decoded frame, compared sample for sample. For a lone-field frame ffmpeg writes nothing, so the harness uses the frame of its own reference decoder (`spoiler_reference/fdec.py`). That decoder does not parse the bitstream: it rebuilds the pictures from the syntax elements the generator recorded while writing the stream, and it matches ffmpeg byte for byte on every frame ffmpeg does output. The harness compares only the lines of the field that is present (even lines for `T`, odd lines for `B`, in all three planes, counted in the full frame: line parity is that of the *uncropped* frame line). The other lines may contain anything.

### 5.4 The suite (all 80 x 96 pictures, 5 x 6 macroblocks; fields 5 x 3)

| case | entropy | POC | what it exercises |
|---|---|---|---|
| `cavlc_tff` | CAVLC | 0 (lsb wraps at 16) | IDR top + bottom pairs, top first |
| `cabac_bff` | CABAC | 0 | bottom-first pairs, field-coded contexts |
| `cabac_t8_mixed` | CABAC, 8x8 transform | 0 | frames and pairs mixed, frame bottom-first, 8x8 field scan / contexts |
| `cavlc_mixed_poc1` | CAVLC | 1 | frames and pairs, deltas |
| `cabac_poc2` | CABAC + 8x8 | 2 | `bottom_field_flag` is the only difference of paired fields; non-reference pictures |
| `cavlc_crop` | CAVLC | 0 | cropping (1, 2, 1, 3): 74 x 80 output |
| `cabac_idr_again` | CABAC + 8x8 | 0 | several IDR pairs (second IDR bottom first), IDR frame |
| `cavlc_nonref_pairs` | CAVLC | 0 | non-reference pairs and frames |
| `cavlc_lone_fields` | CAVLC | 0 | lone reference and non-reference fields, T/B in a row with different `frame_num`, two non-reference same-parity fields |
| `cavlc_reorder` | CAVLC | 0 (lsb 6 bits) | POC order different from decoding order; the SPS VUI says `max_num_reorder_frames = 2` |
| `cabac_poc1_lone` | CABAC | 1 | lone fields with POC type 1 |

Every stream: High profile, 8-bit 4:2:0, `frame_mbs_only_flag = 0`, `mb_adaptive_frame_field_flag = 0`, `direct_8x8_inference_flag = 1`, one reference frame, I slices only (types in a picture are all `I`), 1-3 slices per picture with random `slice_qp`, `disable_deblocking_filter_idc` 0/1/2 with random offsets, random `chroma_qp_index_offset` / `second_chroma_qp_index_offset`, and per macroblock random I_PCM, Intra16x16, Intra4x4 (and Intra8x8 where marked) with random residual blocks. `log2_max_frame_num` is 4 (so `frame_num` wraps).

## 6. Running the harness

```
python3 test_fields.py --make DIR                           # DIR/<case>.264
# run YOUR program on every DIR/<case>.264: write OUTDIR/<case>.dump and OUTDIR/<case>.yuv
python3 test_fields.py --check DIR OUTDIR [--only a,b] [--no-dump] [--no-yuv]
python3 test_fields.py --suite DIR --reader './mydec {stream} {dump} {yuv}'    # all of it in one go
python3 test_fields.py --stream any.264 --yuv mine.yuv [--expected-yuv ref.yuv] [--ignore-frames 3,4]
python3 test_fields.py --self-test
```

* Streams are fully determined by the generator's seeds; `--check` regenerates the truth and refuses a stale `DIR/<case>.264`.
* `--suite` writes the streams into DIR, runs your command once per case (stdin closed, 120 s limit) with the placeholders replaced by `DIR/<case>.264`, `DIR/<case>.dump`, `DIR/<case>.yuv`, then checks them.
* The dump is compared with one built from the generator's own record of each stream (store numbers from the scenario, POCs from the formulas of 3.7); the yuv with ffmpeg (`-fps_mode passthrough -pix_fmt yuv420p`) as in 5.3. Dump failures print case, picture / frame, key, expected and actual; pixel failures name the first differing frame, plane, macroblock (counted in the frame) and sample, say whether that frame is a frame picture, a pair or a lone field, and which field line the sample is on.
* `--stream` is for any file, for example a conformance stream with field coding and its yuv: ffmpeg decodes it for the truth (or give `--expected-yuv` with the conformance yuv). ffmpeg omits lone fields, so pass `--ignore-frames` with the 0-based indexes of the frames *you* wrote for lone fields (the conformance yuv files normally contain them; then just compare with `--expected-yuv`).
* Exit 0 pass, 1 fail, 2 missing or malformed output.
* `--self-test` checks the harness itself: the reference decoder matches ffmpeg on every case and its dump matches the construction; 22 deliberately wrong decoder rules (parity, layout, scans, bS, crop unit, POC, pairing, boundary, output order, ...) and 2 streams written with wrong CABAC field contexts are all caught; corrupted outputs fail.

## 7. Reading failures

* **Every field's lines are swapped** (picture looks shifted by one line): parity. Top field = even frame lines.
* **Picture is two stacked halves**: you stored the field contiguously instead of interleaving.
* **Garbage after the first coefficient block in CABAC field pictures / ffmpeg-clean but yours desyncs**: field contexts (277 / 338 / 436 / 451) or the 8x8 field `ctxIdxInc` table; CAVLC streams still pass in that case, so compare `cavlc_*` and `cabac_*` results.
* **Blocks look transposed or scrambled but parse fine**: zig-zag used instead of the field scan (check `cavlc_tff` first; it has no CABAC and no 8x8).
* **Only horizontal macroblock edges look different** (thin lines at multiples of 16 field lines): bS 4 vs 3.
* **POC wrong only after the 8th or so pair**: `pic_order_cnt_lsb` wrap (msb).
* **POC wrong on the second field of a frame (type 1/2)**: `offset_for_top_to_bottom_field`, or type 2: second field gets the same POC as the first.
* **`store` numbers or `second` off**: pairing rule; the `cavlc_lone_fields` case contains every near miss.
* **PIC count too small in `cabac_poc2`**: `bottom_field_flag` missing from the new-picture test.
* **Wrong frame count / size in `cavlc_crop`**: `CropUnitY`.

## 8. What's next

Chapter 10.2 makes P and B **field** pictures work: reference list initialisation by alternating parity from the fields of the reference frames (8.2.4.2.5), field-based MMCO marking (a frame store can have one field marked "used for reference" and the other not), `FrameNumWrap` for fields, temporal direct and implicit weights using field POCs, and the field/frame distinction in motion vector scaling. Your 10.1 pairing, POC and interleaving code is what it builds on.
