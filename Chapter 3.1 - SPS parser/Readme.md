# Chapter 3.1 - SPS parser

## 1. Goal and the project

Phase 2 left you with three things: the list of NAL units of an Annex B stream, the RBSP bytes of each unit (emulation prevention bytes removed), and a bit reader with `u(n)`, `ue(v)`, `se(v)` and `more_rbsp_data()`.

In this chapter you use them to parse the **sequence parameter set (SPS)**, NAL unit type 7, completely: every field of section 7.3.2.1.1 including the High-profile branch, the scaling lists and the VUI of Annex E. You keep the parsed SPSs in a table indexed by `seq_parameter_set_id`, and you compute the picture size that later phases need.

Your program prints each SPS as text, using exactly the field names that ffmpeg's `trace_headers` prints. The harness extracts the same fields from `trace_headers` and compares them name by name and value by value, so a wrong bit is reported at the first field where it matters. A second, smaller check covers values you derive from the fields (picture size after cropping, frame rate, and a few more).

Chapters 3.2 to 3.4 (PPS, slice header, picture order) use the same output style, so the habits you set up here carry over.

## 2. Big picture

A video stream is a long sequence of pictures that all share a lot of facts: how big they are, which tools they may use, how many bits the frame counter has. Repeating those facts in every slice would waste space, so H.264 sends them once in a **parameter set** and refers to it by number.

There are two levels:

- The **SPS** holds facts that are valid for a whole coded video sequence (everything from one IDR picture to the next IDR picture): profile and level, picture size, cropping, how picture order is signalled, how many reference frames exist, optional video usability information.
- The **PPS** (Chapter 3.2) holds facts that may change from picture to picture: entropy coder, quantiser defaults, weighted prediction flags. Each PPS names an SPS by id.

Every slice header starts by naming a PPS, which names an SPS. Without the SPS you cannot even parse the slice header, because the width of `frame_num` and the shape of the POC fields depend on it. The SPS is therefore the first thing every later phase looks up.

An SPS is a short bit string: usually 10 to 40 bytes. Most of its fields are Exp-Golomb numbers or single flags, and many fields only exist when an earlier flag says so. The parser is a straight-line translation of the syntax table with a handful of conditionals, and the hard part is only being exact: one missing or extra bit shifts everything after it.

## 3. Concepts in detail

All numbers below come from the RBSP of an SPS NAL unit, after the 1-byte NAL header (`nal_unit_type` 7). The descriptor column uses the spec's notation: `u(n)` is n raw bits, `ue(v)` and `se(v)` are Exp-Golomb (section 9.1).

The field names in the tables are the **trace_headers names**. They are the spec names with one exception, noted in the table: `gaps_in_frame_num_allowed_flag` is spelled `gaps_in_frame_num_value_allowed_flag` in the standard. Always use the trace_headers spelling in your output.

### 3.1 Profile, level and id

| Field | Descriptor | Notes |
| --- | --- | --- |
| `profile_idc` | u(8) | 66 Baseline, 77 Main, 88 Extended, 100 High, 110 High 10, 122 High 4:2:2, 244 High 4:4:4 Predictive, 44 CAVLC 4:4:4 Intra, and more. |
| `constraint_set0_flag` ... `constraint_set5_flag` | u(1) each | Hints such as "also conforms to Baseline" (set0), "Main" (set1), "Extended" (set2). Printed as six separate fields. |
| `reserved_zero_2bits` | u(2) | Must be 0. Still a field: print it. |
| `level_idc` | u(8) | Level x.y is `level_idc` = 10 * x + y (40 means 4.0). Level 1b is `level_idc` 9 in High profiles, and 11 with `constraint_set3_flag` = 1 in Baseline, Main and Extended. |
| `seq_parameter_set_id` | ue(v) | 0 to 31. |

### 3.2 The High-profile branch

Only when `profile_idc` is one of 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135 do these fields exist. For Baseline and Main they are absent, and the values are inferred: `chroma_format_idc` = 1 (4:2:0), 8-bit samples, no scaling matrix.

| Field | Descriptor | Notes |
| --- | --- | --- |
| `chroma_format_idc` | ue(v) | 0 monochrome, 1 4:2:0, 2 4:2:2, 3 4:4:4. |
| `separate_colour_plane_flag` | u(1) | Only present when `chroma_format_idc` = 3. |
| `bit_depth_luma_minus8` | ue(v) | 0 for 8-bit video. |
| `bit_depth_chroma_minus8` | ue(v) | |
| `qpprime_y_zero_transform_bypass_flag` | u(1) | Lossless mode. |
| `seq_scaling_matrix_present_flag` | u(1) | If 1, scaling lists follow (next section). |

This book decodes only 8-bit 4:2:0, but you parse the other values anyway, because a real file may contain them and a clean "unsupported" message is better than a misparse. The test inputs below do include 4:2:2, 4:4:4 and 4:0:0 SPSs.

### 3.3 Scaling lists (parsed now, applied in Phase 8)

A scaling matrix lets the encoder weight the quantiser per frequency. If `seq_scaling_matrix_present_flag` is 1, there is a list of flags:

- For i = 0 to 7 when `chroma_format_idc` is not 3, or i = 0 to 11 when it is 3: one flag `seq_scaling_list_present_flag[i]`, u(1).
- If the flag is 1, a `scaling_list()` follows. Lists 0 to 5 have 16 entries (4x4 blocks: Intra Y, Cb, Cr, then Inter Y, Cb, Cr). Lists 6 and up have 64 entries (8x8 blocks: Intra Y, Inter Y, then Cb and Cr pairs in 4:4:4).
- If the flag is 0, nothing follows for that list. The list is later filled by the **fall-back rules** (Table 7-2), which belong to Phase 8.

`scaling_list()` is a delta code (7.3.2.1.1.1). Keep two variables, `lastScale` and `nextScale`, both starting at 8. For each position j from 0 up to the list size minus 1:

1. If `nextScale` is not 0, read `delta_scale` as se(v) and set `nextScale` = (`lastScale` + `delta_scale` + 256) mod 256. If this makes `nextScale` 0 at j = 0, the list means "use the default list" (`useDefaultScalingMatrixFlag`).
2. The entry is `lastScale` if `nextScale` is 0, otherwise `nextScale`.
3. Set `lastScale` to the entry.

Two consequences matter for the parser. First, once `nextScale` becomes 0, **no more `delta_scale` values are read** for that list: the remaining entries repeat the last value. So a list can be much shorter in the bitstream than its size. Second, with `delta_scale` of -8 at j = 0, `nextScale` is 0 immediately, and the list consists of that single field.

For now you only need to read the right number of `delta_scale` values and print them. Print each as `delta_scale[j]` where j restarts at 0 for every list. The entries themselves (the list values) are not printed, since trace_headers does not print them; keep them in your table if you like, because Phase 8 needs them.

x264 never puts scaling lists in the SPS (it uses the PPS, even for `cqm=jvt`), so the harness builds synthetic SPSs for this (section 4.3).

### 3.4 Frame number size and picture order count

| Field | Descriptor | Notes |
| --- | --- | --- |
| `log2_max_frame_num_minus4` | ue(v) | `frame_num` in slice headers has `log2_max_frame_num_minus4 + 4` bits. MaxFrameNum = 2 to that power. |
| `pic_order_cnt_type` | ue(v) | 0, 1 or 2. Selects one of the three branches below. |

**Type 0** (the common one; x264 uses it whenever there are B-frames or interlacing). The slice header carries the low bits of the POC directly.

| Field | Descriptor |
| --- | --- |
| `log2_max_pic_order_cnt_lsb_minus4` | ue(v) |

**Type 1** (POC predicted from `frame_num` with a repeating pattern of offsets; rare).

| Field | Descriptor | Notes |
| --- | --- | --- |
| `delta_pic_order_always_zero_flag` | u(1) | |
| `offset_for_non_ref_pic` | se(v) | |
| `offset_for_top_to_bottom_field` | se(v) | |
| `num_ref_frames_in_pic_order_cnt_cycle` | ue(v) | 0 to 255. |
| `offset_for_ref_frame[i]` | se(v) | One per i from 0 to the count minus 1. Printed with the index. |

**Type 2** (display order equals decoding order, so no B-frame reordering is possible; x264 uses it for progressive streams without B-frames, which includes the Baseline test files). It has **no fields at all**.

Chapter 3.4 turns these into picture order counts. Here you only parse and store them.

### 3.5 Reference frames, picture size and frame/field coding

| Field | Descriptor | Notes |
| --- | --- | --- |
| `max_num_ref_frames` | ue(v) | Size of the reference frame store, in frames. |
| `gaps_in_frame_num_allowed_flag` | u(1) | Spec name: `gaps_in_frame_num_value_allowed_flag`. |
| `pic_width_in_mbs_minus1` | ue(v) | Width in macroblocks (16-pixel columns) minus 1. |
| `pic_height_in_map_units_minus1` | ue(v) | Height in "map units" minus 1; see below. |
| `frame_mbs_only_flag` | u(1) | 1: every picture is a frame. 0: pictures may be fields or MBAFF frames. |
| `mb_adaptive_frame_field_flag` | u(1) | **Only present when `frame_mbs_only_flag` is 0.** |
| `direct_8x8_inference_flag` | u(1) | Constrains B direct mode; needed in Phase 7. |

A **map unit** is one macroblock when `frame_mbs_only_flag` is 1, and a vertical pair of macroblocks (32 rows of a frame) when it is 0. So:

- PicWidthInMbs = `pic_width_in_mbs_minus1` + 1
- FrameHeightInMbs = (2 - `frame_mbs_only_flag`) * (`pic_height_in_map_units_minus1` + 1)

The coded frame is 16 * PicWidthInMbs by 16 * FrameHeightInMbs luma samples. A 1920x1080 video is coded as 120 by 68 macroblocks = 1920x1088; the extra 8 rows are cut off by cropping.

### 3.6 Cropping

| Field | Descriptor | Notes |
| --- | --- | --- |
| `frame_cropping_flag` | u(1) | |
| `frame_crop_left_offset` | ue(v) | Only if the flag is 1; all four are present or none are. |
| `frame_crop_right_offset` | ue(v) | |
| `frame_crop_top_offset` | ue(v) | |
| `frame_crop_bottom_offset` | ue(v) | |

The offsets are not in pixels but in **crop units**, which depend on the chroma format, so that the cropped chroma plane still has whole samples (7.4.2.1.1, equations 7-19 to 7-22):

- ChromaArrayType = 0 if `separate_colour_plane_flag` is 1, else `chroma_format_idc`.
- If ChromaArrayType is 0: CropUnitX = 1 and CropUnitY = 2 - `frame_mbs_only_flag`.
- Otherwise: CropUnitX = SubWidthC and CropUnitY = SubHeightC * (2 - `frame_mbs_only_flag`).
- SubWidthC and SubHeightC: 4:2:0 gives 2 and 2, 4:2:2 gives 2 and 1, 4:4:4 gives 1 and 1.

Output size:

- width = 16 * PicWidthInMbs - CropUnitX * (left + right)
- height = 16 * FrameHeightInMbs - CropUnitY * (top + bottom)

Examples: 4:2:0 progressive 1920x1088 with bottom offset 4 gives 1088 - 2 * 4 = 1080. 4:2:0 interlaced (`frame_mbs_only_flag` 0) needs bottom offset 2 for the same result, because CropUnitY is then 4. Cropping is only a display rule: you still decode all 1088 rows, and the crop is applied when writing the output picture.

### 3.7 VUI parameters (Annex E)

The last SPS field is `vui_parameters_present_flag`, u(1). If it is 1, `vui_parameters()` follows (E.1.1). Everything inside is optional and gated by flags. The syntax, in order:

| Field | Descriptor | Present when |
| --- | --- | --- |
| `aspect_ratio_info_present_flag` | u(1) | always |
| `aspect_ratio_idc` | u(8) | the flag above is 1. 1 = square pixels, 255 = Extended_SAR, other values are a table of standard ratios (Table E-1). |
| `sar_width`, `sar_height` | u(16) each | `aspect_ratio_idc` = 255 |
| `overscan_info_present_flag` | u(1) | always |
| `overscan_appropriate_flag` | u(1) | the flag above is 1 |
| `video_signal_type_present_flag` | u(1) | always |
| `video_format` | u(3) | the flag above is 1 (5 = unspecified) |
| `video_full_range_flag` | u(1) | same |
| `colour_description_present_flag` | u(1) | same |
| `colour_primaries` | u(8) | `colour_description_present_flag` is 1 |
| `transfer_characteristics` | u(8) | same |
| `matrix_coefficients` | u(8) | same. 1 = BT.709, 5 or 6 = BT.601, 2 = unspecified (Table E-5). Phase 9 uses this to pick the YUV to RGB matrix. |
| `chroma_loc_info_present_flag` | u(1) | always |
| `chroma_sample_loc_type_top_field`, `chroma_sample_loc_type_bottom_field` | ue(v) each | the flag above is 1 |
| `timing_info_present_flag` | u(1) | always |
| `num_units_in_tick` | u(32) | the flag above is 1 |
| `time_scale` | u(32) | same |
| `fixed_frame_rate_flag` | u(1) | same |
| `nal_hrd_parameters_present_flag` | u(1) | always |
| `hrd_parameters()` | see below | the flag above is 1 |
| `vcl_hrd_parameters_present_flag` | u(1) | always |
| `hrd_parameters()` | see below | the flag above is 1 (a second, independent copy) |
| `low_delay_hrd_flag` | u(1) | at least one of the two HRD flags is 1 |
| `pic_struct_present_flag` | u(1) | always |
| `bitstream_restriction_flag` | u(1) | always |
| `motion_vectors_over_pic_boundaries_flag` | u(1) | the flag above is 1 |
| `max_bytes_per_pic_denom` | ue(v) | same |
| `max_bits_per_mb_denom` | ue(v) | same |
| `log2_max_mv_length_horizontal` | ue(v) | same |
| `log2_max_mv_length_vertical` | ue(v) | same |
| `max_num_reorder_frames` | ue(v) | same. How many frames may precede any frame in decode order and follow it in output order. |
| `max_dec_frame_buffering` | ue(v) | same. The size of the output (DPB) buffer needed, in frames. Phase 9 uses it to decide when a frame can be output. |

`hrd_parameters()` (E.1.2) describes the hypothetical decoder buffer. You do not use it, but you must parse it to reach the fields after it:

| Field | Descriptor | Notes |
| --- | --- | --- |
| `cpb_cnt_minus1` | ue(v) | 0 to 31. |
| `bit_rate_scale` | u(4) | |
| `cpb_size_scale` | u(4) | |
| `bit_rate_value_minus1[i]` | ue(v) | For i from 0 to `cpb_cnt_minus1`: these three fields repeat, interleaved in this order (value, size, flag, then the next i). |
| `cpb_size_value_minus1[i]` | ue(v) | |
| `cbr_flag[i]` | u(1) | |
| `initial_cpb_removal_delay_length_minus1` | u(5) | |
| `cpb_removal_delay_length_minus1` | u(5) | |
| `dpb_output_delay_length_minus1` | u(5) | |
| `time_offset_length` | u(5) | |

The NAL and VCL copies use identical field names. Only their position tells them apart.

Two details. The timing fields are 32 bits wide, which does not fit a signed 32-bit integer: use a 64-bit type in your reader (`u(32)` of a bit reader that peeks 32 bits at once is a classic overflow). And when `bitstream_restriction_flag` is 0, `max_dec_frame_buffering` is not present; a decoder must then derive a buffer size from the level (Phase 9). This chapter only prints the field when it exists.

### 3.8 End of the SPS

After the last field comes `rbsp_trailing_bits`: a 1 bit and zero bits up to the byte boundary. A parser that consumed the right number of bits will find `more_rbsp_data()` false here. It is a good internal assertion: if the trailing 1 bit does not sit where you expect, you misparsed something. The harness does not look at trailing bits, but your own debugging should.

## 4. The project: exactly what to build

### 4.1 Parser and table

Write `parse_sps(rbsp)`, the straight-line translation of sections 3.1 to 3.7, returning a record with every field (including inferred defaults for fields absent in non-High profiles) and the derived values of 4.2. Keep a table of 32 SPS slots indexed by `seq_parameter_set_id`. A new SPS with an id that is already stored replaces the old one (streams repeat their SPS before every IDR picture, usually byte-identical).

Write the **dump program** that:

1. reads a `.ts` file (with your Phase 1 and 2 code) or a raw `.264` file,
2. finds every NAL with `nal_unit_type` 7, in stream order,
3. converts to RBSP, parses, stores into the table,
4. prints one text block per SPS NAL unit, in the format of section 5.

Print **every SPS NAL unit that occurs**, including repeats. Do not de-duplicate.

### 4.2 Derived values

Besides the parsed fields, compute and print these (formulas in section 3):

| Line | Meaning |
| --- | --- |
| `DERIVED width W` | Cropped output width in luma samples. |
| `DERIVED height H` | Cropped output height. Must match `ffprobe -show_streams`. |
| `DERIVED coded_width W` | 16 * PicWidthInMbs. |
| `DERIVED coded_height H` | 16 * FrameHeightInMbs. |
| `DERIVED chroma_array_type N` | 0 if `separate_colour_plane_flag` is 1, else `chroma_format_idc` (inferred 1 when absent). |
| `DERIVED max_frame_num N` | 2 to the power (`log2_max_frame_num_minus4` + 4). |
| `DERIVED frame_rate NUM DEN` | Only when `timing_info_present_flag` is 1. The ratio `time_scale / (2 * num_units_in_tick)`, reduced to lowest terms (divide both by their greatest common divisor). The factor 2 is because one "tick" of the clock is one field, and a frame is two ticks. For `time_scale` 50 and `num_units_in_tick` 1, this prints `25 1`. |
| `DERIVED matrix_coefficients N` | Only when `colour_description_present_flag` is 1. |
| `DERIVED max_num_reorder_frames N` | Only when `bitstream_restriction_flag` is 1. |
| `DERIVED max_dec_frame_buffering N` | Only when `bitstream_restriction_flag` is 1. |

Derived lines come in exactly this order, after the last syntax line of the block, with the "only when" lines omitted if their condition is false.

Verify (the Summary's step): your dump matches `trace_headers` field by field, and width and height match ffprobe (1088 coded rows crop to 1080, for example).

### 4.3 Test inputs

Run the harness on any H.264 `.ts` from the Summary recipes (T1, T9, T11_high, T11_cqm, T12 from `common.testfiles`) and on any real file. Those exercise Baseline, Main and High, POC types 0 and 2, interlaced coding and a small VUI. To cover the rest, make extra inputs:

    python3 test_sps.py --make-variants some_dir

which writes into `some_dir`:

| File | Content |
| --- | --- |
| `sps_variants.264` | 11 **synthetic** SPS NAL units written by the harness (ids 1 to 11), followed by a real 3-frame High stream (SPS id 0). The synthetic ones cover: POC type 1 with three `offset_for_ref_frame` values, POC type 2 with a 16-bit `frame_num`, scaling lists inside an SPS (explicit, `-8` use-default, absent, early-terminated, all-zero deltas, 64-entry lists), a 4:4:4 SPS with 12 lists and `separate_colour_plane_flag`, 1080p interlaced with crop, 1080p progressive with crop, 4:2:2 and 4:0:0 with all four crop offsets, and three VUI variants (SAR 255 + overscan + colour + chroma location + timing + NAL and VCL HRD with two CPBs + `pic_struct` + restrictions; SAR idc with a video signal type but no colour description; VCL HRD only). |
| `sps_1080p.ts` | x264 at 1920x1080: coded 1088 rows with `frame_crop_bottom_offset` 4. |
| `sps_vui_hrd.ts` | x264 with a sample aspect ratio, a colour description (BT.709), NAL HRD and bitstream restrictions. |
| `sps_interlaced_crop.ts` | x264 MBAFF at 1920x1080: `frame_mbs_only_flag` 0, `frame_crop_bottom_offset` 2 (crop unit of 4 rows). |

Which branches cannot be made by x264, and why:

- **POC type 1**: x264 only writes type 0 (when B-frames or interlace are used) or type 2.
- **Scaling lists in the SPS**: x264 writes a scaling matrix in the PPS only.
- **4:2:2, 4:4:4, 4:0:0, VCL HRD, separate colour planes**: outside what the book's x264 recipes produce.

These live only in `sps_variants.264`. The harness checks them by the field-by-field comparison alone, plus the DERIVED lines, which it computes from the same trace fields (ffprobe cannot confirm them, since it reports only the active SPS and its decoder does not support all of them). The synthetic file is a valid Annex B stream of parameter sets followed by a real stream, so ffmpeg and your reader treat it like any other input. Only SPS id 0 is used by the slices, so ffprobe checks id 0.

## 5. Harness input format

One text file, produced by your dump program. Blocks, one per SPS NAL unit, in stream order:

    SPS <ordinal>
    <name> <value>
    <name> <value>
    ...
    DERIVED <name> <value> [<value>]

Rules:

- The block header is exactly two words: `SPS` and a number (0, 1, 2, ... in order). The harness uses it only to start a new block, so always print it, one per SPS NAL unit.
- A syntax line is exactly two words, `<name> <integer>`, separated by whitespace: the name exactly as in the tables of section 3, then a decimal integer, with a minus sign if negative (`offset_for_non_ref_pic -1`). Flags are 0 or 1. No hex, no extra text.
- A derived line is the word `DERIVED`, a name from 4.2 and one decimal integer (two for `frame_rate`: numerator, then denominator).
- Array elements carry their index in the name, with no spaces: `delta_scale[3]`, `seq_scaling_list_present_flag[0]`, `offset_for_ref_frame[2]`, `bit_rate_value_minus1[1]`, `cpb_size_value_minus1[0]`, `cbr_flag[1]`. Indices restart at 0 for each scaling list and for each HRD.
- Fields appear **in syntax order** and only if they exist (not inferred). Flags that gate fields are printed (`frame_cropping_flag 0`, `vui_parameters_present_flag 0`). Printed even when 0: `reserved_zero_2bits`, all six `constraint_setN_flag`.
- A field that exists only conditionally is omitted when its condition is false: for a Main-profile SPS there is no `chroma_format_idc`; with `pic_order_cnt_type` 2 there are no POC fields; with `frame_mbs_only_flag` 1 there is no `mb_adaptive_frame_field_flag`.
- `DERIVED` lines come after the block's syntax lines, in the order of the table in 4.2.
- Blank lines and lines starting with `#` are ignored. You may also print the NAL header fields `forbidden_zero_bit`, `nal_ref_idc`, `nal_unit_type` and the trailing `rbsp_stop_one_bit` / `rbsp_alignment_zero_bit`; the harness drops them. It is simplest not to.

Example (first lines of a Main-profile stream with `pic_order_cnt_type` 2, shortened):

    SPS 0
    profile_idc 77
    constraint_set0_flag 0
    constraint_set1_flag 1
    constraint_set2_flag 0
    constraint_set3_flag 0
    constraint_set4_flag 0
    constraint_set5_flag 0
    reserved_zero_2bits 0
    level_idc 13
    seq_parameter_set_id 0
    log2_max_frame_num_minus4 0
    pic_order_cnt_type 2
    max_num_ref_frames 1
    ...
    DERIVED width 352
    DERIVED height 288
    DERIVED coded_width 352
    DERIVED coded_height 288
    DERIVED chroma_array_type 1
    DERIVED max_frame_num 16

Choices the harness makes, so you are never guessing:

- **Which SPS blocks are the truth.** ffmpeg prints an "Extradata" copy of the parameter sets before the first packet; the harness ignores that and uses only the SPS NAL units inside packets, so truth equals "every SPS NAL in the stream, in order". Your count must match, repeats included (T1 has 50, one per IDR picture).
- **Ignored lines.** Anything in the trace that is not an SPS syntax element is dropped: the three NAL header fields, `rbsp_stop_one_bit`, `rbsp_alignment_zero_bit`, the "Picture Parameter Set" and "Slice Header" blocks, packet lines, log noise. Derived values are not in the trace, so they are never compared with it; the harness recomputes them from the trace's syntax fields.
- **Scaling list entries.** trace_headers prints the coded `delta_scale[j]` values, not the final entries. So do you.
- **ffprobe.** For the SPS that the first slice actually uses (slice, then its PPS, then its SPS id), your `DERIVED width` and `DERIVED height` are compared with `ffprobe -show_streams` `width` and `height`, and `matrix_coefficients` with ffprobe's `color_space` name when it is a known value other than 2. Frame rate is not compared with ffprobe, because ffprobe's frame rates come from timestamps as well.

## 6. Running the harness

    python3 test_sps.py <input.ts|input.264> <your_sps_dump.txt>

It runs `ffmpeg -c:v copy -bsf:v trace_headers` (and `ffprobe`) on the input. Exit 0 on pass, 1 on a mismatch (the first 10 are listed), 2 if your file is malformed or the input is unusable.

    python3 test_sps.py --make-variants some_dir     # writes the extra inputs of 4.3
    python3 test_sps.py --self-test                  # checks the harness itself

`--self-test` builds known-good dumps from ffmpeg truth for T1, T11_high, T12, T9_spatial and the four generated inputs (must pass), then corrupts a dump four ways (wrong scaling-list value, missing field, wrong derived height, missing SPS block; each must fail). It needs ffmpeg with libx264.

## 7. Reading failures and debugging tips

A failure message looks like:

    FAIL: SPS #2 field #19: value of 'delta_scale[3]': expected 4, got 99
             expected syntax section: scaling lists, scaling_list() 7.3.2.1.1.1
             previous fields (all matched): seq_scaling_list_present_flag[0] 1; delta_scale[0] 8; ...

- `SPS #n` counts SPS NAL units from 0. `field #i` counts the compared fields inside it.
- The **previous fields** line shows the last four fields that matched; the bug is in how you read the field after them, or in a flag before them that you misjudged.
- A field-name mismatch means you printed a different field than the syntax requires at that point: usually a missing or extra conditional. Look at the section label in the message to find the table.
- "block ends early" means you stopped reading, often a loop that ends one entry short or a flag you treated as absent.

Common causes of a first mismatch:

- Right at `chroma_format_idc`: your High-profile list of `profile_idc` values is missing one (the long list in 3.2).
- After the scaling lists: you read all 16 or 64 `delta_scale` values instead of stopping when `nextScale` reaches 0, or you used 8 lists for a 4:4:4 stream.
- After `pic_order_cnt_type`: type 2 has no fields; type 1 reads `num_ref_frames_in_pic_order_cnt_cycle` offsets.
- At `mb_adaptive_frame_field_flag`: it exists only when `frame_mbs_only_flag` is 0.
- A DERIVED height off by a factor in interlaced files: the map unit and crop unit both double when `frame_mbs_only_flag` is 0.
- In the VUI: remember that the 8-bit `aspect_ratio_idc` value 255 adds two 16-bit fields; `low_delay_hrd_flag` is present if **either** HRD flag is set; an overflowing `u(32)` in `time_scale`.
- Matching up to some field and wrong everywhere after it is the signature of one misread-length field. Use the bit positions printed in the raw trace (`ffmpeg -i X -c:v copy -bsf:v trace_headers -f null - 2>&1`): the first number on each line is the bit offset of the field, counted from the start of the NAL unit (the 8-bit NAL header included, emulation prevention bytes already removed), and the binary string is the exact bits read. Compare with your bit reader's position.

## 8. What's next

Chapter 3.2 parses the PPS with the same dump style (plus a lookup into the SPS table you built here). Chapter 3.3 parses slice headers, which need the SPS for `frame_num` width, POC fields and `frame_mbs_only_flag`.
