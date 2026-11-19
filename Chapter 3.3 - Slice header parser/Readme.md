# Chapter 3.3 - Slice header parser

## 1. Goal and the project

Chapters 3.1 and 3.2 gave you two tables: SPSs by id and PPSs by id. Now you parse the header of every **slice**, the piece of a picture that holds the actual macroblocks: all of section 7.3.3, including its three sub-structures (reference list modification, prediction weight table, reference picture marking). You also check the **CABAC alignment bits** that follow the header.

Your program prints one text block per slice NAL unit (types 1 and 5), using the field names and order of ffmpeg's `trace_headers`, plus a few derived values that later chapters need. The harness extracts the same fields from `trace_headers`, compares them name by name and value by value, and checks the derived values. The slice header is the first thing in the whole book whose syntax depends on **both** parameter sets, so most failures will be a wrong table lookup, not a wrong bit read.

## 2. Big picture

A coded picture is made of one or more slices. Each slice is one NAL unit: a header, then the slice data (the macroblocks, Phase 4 onward). A slice can be decoded without the other slices of the same picture, so the header repeats everything the decoder needs to place and predict this slice:

- which PPS (and through it which SPS) applies,
- where the slice starts in the picture (`first_mb_in_slice`) and what kind it is (I, P or B),
- which picture this slice belongs to: `frame_num`, field flags, `idr_pic_id`, and the picture order count fields (Chapter 3.4 turns them into display order),
- for P and B slices: how many reference pictures each list has, optional re-ordering of those lists, optional explicit prediction weights,
- what to do with the reference picture buffer after this picture is decoded (the marking commands),
- the entropy coder start-up (`cabac_init_idc`), the quantiser (`slice_qp_delta`) and the deblocking controls.

Most fields are optional and appear only when the SPS, the PPS or an earlier field in the same header says so. A header is a straight-line program with many `if`s. One wrong condition shifts every following bit, so the harness reports the **first** field that differs, together with the SPS and PPS values that decide the conditions.

The header has no trailing bits: it simply runs into the slice data. (With CAVLC the macroblock data starts at the very next bit; with CABAC there is a little padding first, section 3.9.)

## 3. Concepts in detail

The fields are read from the RBSP (emulation prevention bytes removed) of a NAL unit of type 1 (non-IDR slice) or 5 (IDR slice), after the 1-byte NAL header. `u(n)` is n raw bits; `ue(v)`, `se(v)` are Exp-Golomb (section 9.1). The names are the trace_headers names. Other slice-like NAL types (2 to 4 data partitions, 19 auxiliary pictures, 20 MVC extension) are skipped by this book.

In the syntax below, `nal_ref_idc` and `nal_unit_type` come from the NAL header. `IdrPicFlag` is 1 when `nal_unit_type` is 5.

### 3.1 The first three fields

| Field | Descriptor | Notes |
| --- | --- | --- |
| `first_mb_in_slice` | ue(v) | Address of the first macroblock of this slice. In an MBAFF frame (section 3.2) the unit is a macroblock **pair**, so the first macroblock address is `first_mb_in_slice * 2`. The first slice of a picture has 0, but slices may arrive with a larger value (T4 has 4 slices per picture) and the spec allows arbitrary order. |
| `slice_type` | ue(v) | 0 to 9, see below. |
| `pic_parameter_set_id` | ue(v) | The key into your PPS table. |

**slice_type.** The value modulo 5 is the type: 0 = P, 1 = B, 2 = I, 3 = SP, 4 = SI. The values 5 to 9 mean the same thing, plus a promise: **every slice of this picture has the same type** (mod 5). x264 writes 7 (I, all the same) in IDR pictures. Your code must use `slice_type % 5` everywhere; the harness prints the raw value as the field and the reduced value as `DERIVED slice_type_mod5`. The promise is only useful as a hint for later optimisations: you can ignore it.

SP and SI slices exist only in the Extended profile (switching streams). The decoder in this book does not decode them, but the header is easy, so you parse them (section 3.8).

**Parameter set lookup.** Right after reading `pic_parameter_set_id`, look it up: PPS table entry, then its `seq_parameter_set_id`, then the SPS table entry. Use the table content **at the time this slice arrives**: a PPS may be re-sent with different content between pictures (the variants file does exactly this). Everything below that mentions "the PPS" or "the SPS" means these two entries. If either is missing, see section 4.2.

### 3.2 Picture identification

| Field | Present when | Descriptor | Notes |
| --- | --- | --- | --- |
| `colour_plane_id` | SPS `separate_colour_plane_flag` = 1 | u(2) | 4:4:4 coded as three independent planes; each plane has its own slices. 0 to 2. |
| `frame_num` | always | u(v) | Width is `log2_max_frame_num_minus4 + 4` bits (SPS, 4 to 16). Counts reference frames modulo `MaxFrameNum` = 2^width; Phase 7 uses it. |
| `field_pic_flag` | SPS `frame_mbs_only_flag` = 0 | u(1) | 1: this slice belongs to a single **field** (half the picture's lines), 0: a frame. |
| `bottom_field_flag` | `field_pic_flag` = 1 | u(1) | 1: bottom field, 0: top field. |
| `idr_pic_id` | `IdrPicFlag` = 1 | ue(v) | Tells two consecutive IDR pictures apart. Identical in all slices of a picture. |

When `field_pic_flag` is absent it is inferred to be 0, and `bottom_field_flag` is inferred 0 when absent. Do not print inferred fields, but use the inferred values. (T1 to T10 never have these fields because x264 writes `frame_mbs_only_flag` = 1; T12 has `field_pic_flag` = 0 in every slice. Real field pictures only appear in the variants file.)

**MbaffFrameFlag** (derived, 7.4.3) is 1 when the SPS has `mb_adaptive_frame_field_flag` = 1 and `field_pic_flag` is 0. It selects macroblock-pair addressing for `first_mb_in_slice`. T12 (x264 `interlaced=1`) is an MBAFF stream.

### 3.3 Picture order count fields

Which fields exist depends on `pic_order_cnt_type` of the SPS (Chapter 3.1):

| Type | Fields |
| --- | --- |
| 0 | `pic_order_cnt_lsb`, u(v) of `log2_max_pic_order_cnt_lsb_minus4 + 4` bits. Then, if the PPS has `bottom_field_pic_order_in_frame_present_flag` = 1 **and** `field_pic_flag` = 0: `delta_pic_order_cnt_bottom`, se(v). |
| 1 | Only if the SPS has `delta_pic_order_always_zero_flag` = 0: `delta_pic_order_cnt[0]`, se(v), then, if also the PPS flag above is 1 and `field_pic_flag` = 0: `delta_pic_order_cnt[1]`, se(v). With `delta_pic_order_always_zero_flag` = 1 neither field is present. |
| 2 | None. The order is the decoding order. |

Both delta fields are printed with their index, exactly like that: `delta_pic_order_cnt[0]`. You only read and print them here; Chapter 3.4 computes the order from them.

### 3.4 Redundant pictures, direct mode, reference counts

| Field | Present when | Descriptor | Notes |
| --- | --- | --- | --- |
| `redundant_pic_cnt` | PPS `redundant_pic_cnt_present_flag` = 1 | ue(v) | 0 for a primary slice, above 0 for a redundant copy used for error resilience. A decoder with correct data discards the redundant slices; this book never meets them outside the variants. |
| `direct_spatial_mv_pred_flag` | `slice_type % 5` = 1 (B) | u(1) | 1: spatial direct prediction, 0: temporal (Phase 6/7). |
| `num_ref_idx_active_override_flag` | type P, SP or B | u(1) | 1: the counts below replace the PPS defaults. |
| `num_ref_idx_l0_active_minus1` | override flag = 1 | ue(v) | Size of list 0 minus 1. |
| `num_ref_idx_l1_active_minus1` | override flag = 1 and type B | ue(v) | Size of list 1 minus 1. |

**The active reference counts** (you will need them in this very header, for the weight table):

- I and SI slices use no references: both counts are 0.
- P and SP slices: `num_ref_idx_l0_active` = the coded value + 1 if the override flag is 1, else the PPS's `num_ref_idx_l0_default_active_minus1 + 1`. List 1 is not used (count 0).
- B slices: the same for both lists, each with its own PPS default.
- The limits are 16 per list for frames (and MBAFF), 32 for field pictures. The default from the PPS is used as is: it is **not** doubled for fields.

### 3.5 Reference picture list modification (7.3.3.1)

Normally the decoder builds the reference lists by a fixed rule from the pictures in the buffer (Phase 7). A slice can ask for a different order by sending a list of commands. The syntax is written once per list:

    if slice_type % 5 is not I and not SI:
        ref_pic_list_modification_flag_l0   u(1)
        if flag = 1: repeat
            modification_of_pic_nums_idc    ue(v)
            if idc = 0 or 1:  abs_diff_pic_num_minus1   ue(v)
            if idc = 2:       long_term_pic_num         ue(v)
        until idc = 3
    if slice_type % 5 is B:
        the same again with ref_pic_list_modification_flag_l1

| `modification_of_pic_nums_idc` | Meaning | Argument |
| --- | --- | --- |
| 0 | Move a short-term picture whose picture number is *lower* than the running prediction | `abs_diff_pic_num_minus1` |
| 1 | The same, *higher* | `abs_diff_pic_num_minus1` |
| 2 | Move a long-term picture | `long_term_pic_num` |
| 3 | End of the list of commands | none |
| 4, 5 | Only in MVC (NAL type 20); an error here | |

Facts that matter for the parser:

- The terminator is a command too: **`modification_of_pic_nums_idc` with value 3 is printed** as a field, like any other. A flag of 1 is followed by at least that terminator.
- If the flag is 0 nothing follows for that list.
- Unlike the other array-like fields, the command fields are printed **without an index**: the lines are just `modification_of_pic_nums_idc`, `abs_diff_pic_num_minus1` (or `long_term_pic_num`), `modification_of_pic_nums_idc`, ... in order, whichever list they belong to. The harness's derived `num_rplm_l0` / `num_rplm_l1` count the commands (the terminator excluded) so you can see which list your parser thinks they belong to.
- At most `num_ref_idx_active` commands per list are legal. The meaning of the commands (how the lists change) is Phase 7; here you only parse.

T7 (weighted P with several refs) and T8 to T12 (B slices, both lists) contain real commands.

### 3.6 Prediction weight table (7.3.3.2)

Weighted prediction scales and shifts the prediction before it is added to the residual. It is used when:

- the slice is P or SP **and** the PPS has `weighted_pred_flag` = 1, or
- the slice is B **and** the PPS has `weighted_bipred_idc` = 1.

(With `weighted_bipred_idc` = 2 the weights are implicit, computed from picture distances in Phase 7; no table is sent. With 0 plain averaging is used.) The table:

    luma_log2_weight_denom            ue(v)
    if ChromaArrayType != 0:
        chroma_log2_weight_denom      ue(v)
    for i = 0 .. num_ref_idx_l0_active - 1:
        luma_weight_l0_flag[i]        u(1)
        if flag: luma_weight_l0[i] se(v), luma_offset_l0[i] se(v)
        if ChromaArrayType != 0:
            chroma_weight_l0_flag[i]  u(1)
            if flag: for j = 0 .. 1:  chroma_weight_l0[i][j] se(v), chroma_offset_l0[i][j] se(v)
    if slice_type % 5 = B:
        the same loop for list 1 with the l1 names, for i = 0 .. num_ref_idx_l1_active - 1

Points to get right:

- **ChromaArrayType** is `chroma_format_idc` of the SPS, except 0 when `separate_colour_plane_flag` = 1. So the chroma parts disappear for monochrome and for separate colour planes. (`DERIVED chroma_array_type` shows your value.)
- The loop counts are the **active** counts of section 3.4, after the override. Using the PPS default when the slice overrides is a classic bug.
- The flags are interleaved with the values entry by entry (luma flag, luma values, chroma flag, chroma values, next reference), not luma table then chroma table. A 0 flag means the entry's fields are absent.
- Print names with their indices exactly as shown: `luma_weight_l0_flag[2]`, `luma_weight_l0[2]`, `luma_offset_l0[2]`, `chroma_weight_l0_flag[2]`, `chroma_weight_l0[2][0]`, `chroma_offset_l0[2][0]`, `chroma_weight_l0[2][1]`, `chroma_offset_l0[2][1]`.
- Inferred when a flag is 0: weight = 2^`luma_log2_weight_denom` (or `chroma_...`), offset = 0. Keep them in your slice record; Phase 7 uses them.
- Weights are -128 to 127, offsets -128 to 127 (8-bit video).

### 3.7 Decoded reference picture marking (7.3.3.3)

After a picture is decoded the decoder updates its reference picture buffer. The slice header carries the instructions. The whole structure exists **only if `nal_ref_idc` is not 0** (a non-reference picture does not touch the buffer).

    if IdrPicFlag:
        no_output_of_prior_pics_flag          u(1)
        long_term_reference_flag              u(1)
    else:
        adaptive_ref_pic_marking_mode_flag    u(1)
        if flag = 1: repeat
            memory_management_control_operation   ue(v)
            (arguments, see table)
        until operation = 0

| Operation | Meaning | Arguments (printed in this order) |
| --- | --- | --- |
| 0 | End of the commands | none |
| 1 | Mark a short-term picture as unused | `difference_of_pic_nums_minus1` |
| 2 | Mark a long-term picture as unused | `long_term_pic_num` |
| 3 | Make a short-term picture long-term | `difference_of_pic_nums_minus1`, `long_term_frame_idx` |
| 4 | Set the maximum long-term index | `max_long_term_frame_idx_plus1` |
| 5 | Mark everything unused (and reset numbering) | none |
| 6 | Make the current picture long-term | `long_term_frame_idx` |

- The terminator (operation 0) is printed like the others, as in the other command loops.
- With `adaptive_ref_pic_marking_mode_flag` = 0 the default **sliding window** applies (Phase 7); nothing more is coded.
- IDR pictures: `no_output_of_prior_pics_flag` says the pictures still waiting for output may be dropped; `long_term_reference_flag` = 1 makes the IDR a long-term reference. Both are 0 in x264 output.
- Like the reordering commands, the MMCO fields have **no index** in the output. A slice with `nal_ref_idc` = 0 prints none of them.
- All slices of one picture carry the same marking commands. Your parser does not need to check this.
- T9_spatial, T9_temporal, T10_full and T12 contain operation 1 commands (x264 uses them with B pyramids). Operations 2 to 6 only appear in the variants file.

### 3.8 The tail: entropy, QP, deblocking

| Field | Present when | Descriptor | Notes |
| --- | --- | --- | --- |
| `cabac_init_idc` | PPS `entropy_coding_mode_flag` = 1 and type not I, SI | ue(v) | 0 to 2; selects one of three context initialisation tables (Phase 8). |
| `slice_qp_delta` | always | se(v) | `SliceQPY = 26 + pic_init_qp_minus26 + slice_qp_delta`, where `pic_init_qp_minus26` is from the PPS. |
| `sp_for_switch_flag` | type SP | u(1) | Extended profile. |
| `slice_qs_delta` | type SP or SI | se(v) | Extended profile; QS is `26 + pic_init_qs_minus26 + slice_qs_delta`. |
| `disable_deblocking_filter_idc` | PPS `deblocking_filter_control_present_flag` = 1 | ue(v) | 0 = filter all edges, 1 = filter off, 2 = filter but not across slice boundaries. |
| `slice_alpha_c0_offset_div2` | the above present and its value is not 1 | se(v) | -6 to 6. Offset A is twice the value. |
| `slice_beta_offset_div2` | same | se(v) | Offset B is twice the value. |

When the deblocking control is absent: `disable_deblocking_filter_idc` is inferred 0 and both offsets 0 (the filter is on, with default strength). When the idc is 1 the offsets are absent and irrelevant, and are inferred 0.

There is one more field in the spec, `slice_group_change_cycle`, which exists only when the PPS uses slice groups of map types 3 to 5. This book rejects such PPSs in Chapter 3.2, so you never get there; see section 4.2.

### 3.9 Where the header ends, and the CABAC alignment bits

The slice header syntax ends after the deblocking fields. The next thing in the NAL unit is `slice_data()`, whose first syntax elements are, **when `entropy_coding_mode_flag` = 1**:

    while not byte_aligned():
        cabac_alignment_one_bit        (always 1)

then the CABAC-coded macroblocks, which start on a byte boundary (the arithmetic decoder initialises by reading whole bytes). With CAVLC there is nothing: the macroblock layer starts at the very next bit, whatever its position.

Two uses:

- Your position after the alignment is where slice data starts. Phases 4 and 8 begin there. The harness checks it as `DERIVED header_end_bit`.
- **A built-in self check.** The alignment bits must all be 1. If you find a 0 you have misread an earlier field (or misjudged a condition), because the alignment is the first thing that depends on your header ending at exactly the right bit. Nothing else in the header gives you such a check, so test it while debugging. (CAVLC has no equivalent check: a wrong header silently shifts the macroblock data.)

Bit positions in this chapter are **bit offsets from the start of the NAL unit, counting the 8-bit NAL header**, in the RBSP (emulation prevention bytes removed). `first_mb_in_slice` therefore starts at bit 8. This is the numbering of the first column of the raw trace.

### 3.10 Summary of the dependencies

| You need | From | For |
| --- | --- | --- |
| `separate_colour_plane_flag`, `chroma_format_idc` | SPS | `colour_plane_id`; ChromaArrayType for the weight table |
| `log2_max_frame_num_minus4` | SPS | width of `frame_num` |
| `frame_mbs_only_flag`, `mb_adaptive_frame_field_flag` | SPS | field flags; MbaffFrameFlag |
| `pic_order_cnt_type`, `log2_max_pic_order_cnt_lsb_minus4`, `delta_pic_order_always_zero_flag` | SPS | POC fields |
| `bottom_field_pic_order_in_frame_present_flag` | PPS | `delta_pic_order_cnt_bottom`, `delta_pic_order_cnt[1]` |
| `redundant_pic_cnt_present_flag` | PPS | `redundant_pic_cnt` |
| `num_ref_idx_l0/l1_default_active_minus1` | PPS | reference counts without override |
| `weighted_pred_flag`, `weighted_bipred_idc` | PPS | pred_weight_table |
| `entropy_coding_mode_flag` | PPS | `cabac_init_idc`, alignment bits |
| `pic_init_qp_minus26` | PPS | SliceQPY |
| `deblocking_filter_control_present_flag` | PPS | deblocking fields |

## 4. The project: exactly what to build

### 4.1 Parser and dump program

Write `parse_slice_header(rbsp, nal_unit_type, nal_ref_idc, sps_table, pps_table)` returning a record with every field, the inferred values, the weight table and command lists in a usable form (you will need them in Phases 7 and 8), the derived values of section 4.3 and the bit position where the slice data starts.

Write a **dump program** that:

1. reads a `.ts` file or a raw `.264` file with your earlier code,
2. goes over the NAL units in stream order, keeping the SPS table (type 7) and PPS table (type 8) up to date as units arrive,
3. for every slice NAL unit (type 1 or 5), parses the header and prints one text block in the format of section 5. All other NAL types are skipped (they still update the tables if SPS or PPS).

Print **every** slice, including all slices of multi-slice pictures. Keep the slice dump separate from your SPS and PPS dumps.

Verify (the Summary's step): your dump matches `trace_headers` field by field on every test input, and on CABAC files (T10_full, T12) the `cabac_alignment_one_bit`s are all 1.

### 4.2 Missing parameter sets

A slice header cannot be parsed without its PPS and SPS. If `pic_parameter_set_id` names a PPS that is not in the table, or that PPS names an SPS that is not in the table (this includes a PPS your Chapter 3.2 code rejected for slice groups), do this:

1. print the block start `SLICE <n>`, the three fields before the lookup (`first_mb_in_slice`, `slice_type`, `pic_parameter_set_id`) and one line `ERROR <free text>`; nothing else for that slice;
2. skip the NAL unit and continue with the next one.

(The spec allows parameter sets to arrive late as long as they are there when a slice uses them; a real decoder would also drop all slices of a picture whose header cannot be parsed.) The harness's truth comes from ffmpeg, which cannot parse such a stream either, so the harness never tests this branch: it exits 2 with an explanation if the stream has a slice whose PPS is missing. It does check that **valid** streams produce no `ERROR` line.

Out of scope (not tested): `slice_group_change_cycle` (FMO) and decoding of SP/SI slices. SP/SI headers are parsed and checked.

### 4.3 Derived values

Printed after the syntax lines of each block, in this exact order, all always present (except in an error block):

| Line | Meaning |
| --- | --- |
| `DERIVED nal_unit_type N` | 1 or 5. |
| `DERIVED nal_ref_idc N` | 0 to 3. |
| `DERIVED slice_type_mod5 N` | `slice_type % 5`. |
| `DERIVED slice_type_name W` | `P`, `B`, `I`, `SP` or `SI` for 0 to 4. The only non-integer value anywhere in the dump. |
| `DERIVED slice_type_all_same N` | 1 if `slice_type` is 5 to 9, else 0. |
| `DERIVED sps_id N` | The `seq_parameter_set_id` of the slice's PPS. |
| `DERIVED chroma_array_type N` | 0 if `separate_colour_plane_flag` is 1, else `chroma_format_idc` (1 if the SPS has none). |
| `DERIVED entropy_coding_mode_flag N` | From the PPS. |
| `DERIVED frame_num_bits N` | `log2_max_frame_num_minus4 + 4`. |
| `DERIVED field_pic N` | `field_pic_flag`, 0 if absent. |
| `DERIVED bottom_field N` | `bottom_field_flag`, 0 if absent. |
| `DERIVED mbaff_frame N` | MbaffFrameFlag, section 3.2. |
| `DERIVED first_mb_addr N` | `first_mb_in_slice * (1 + MbaffFrameFlag)`. |
| `DERIVED num_ref_idx_l0_active N` | Section 3.4; 0 for I and SI. |
| `DERIVED num_ref_idx_l1_active N` | Section 3.4; 0 unless B. |
| `DERIVED num_rplm_l0 N` | Number of reordering commands for list 0 (terminator not counted). |
| `DERIVED num_rplm_l1 N` | Same for list 1. |
| `DERIVED weight_mode N` | 1 if the slice carries a `pred_weight_table`, 2 if it is a B slice with `weighted_bipred_idc` = 2 (implicit weights), else 0. |
| `DERIVED num_mmco N` | Number of memory management commands (the operation 0 terminator not counted); 0 if none. |
| `DERIVED slice_qp N` | `26 + pic_init_qp_minus26 + slice_qp_delta`. |
| `DERIVED deblocking_idc N` | `disable_deblocking_filter_idc`, 0 if absent. |
| `DERIVED filter_offset_a N` | `2 * slice_alpha_c0_offset_div2`, 0 if absent. |
| `DERIVED filter_offset_b N` | `2 * slice_beta_offset_div2`, 0 if absent. |
| `DERIVED header_end_bit N` | Bit offset (section 3.9) of the first bit of slice data: after the last header field **and** after the alignment bits for CABAC. |
| `DERIVED cabac_alignment_bits N` | Number of `cabac_alignment_one_bit`s you read (0 to 7); always 0 with CAVLC. |
| `DERIVED cabac_alignment_ok N` | 1 if all of them were 1 (also 1 when there are none, as with CAVLC), else 0. |

### 4.4 Test inputs

Run the harness on `T1` (Baseline, IDR only), `T4` (4 slices per picture), `T5`, `T6` (P, several refs), `T7` (weighted P, reordering commands), `T8`, `T9_spatial`, `T9_temporal` (B, direct modes, MMCO), `T10_full` (CABAC) and `T12` (MBAFF, CABAC), and any real file. To cover what x264 cannot easily write:

    python3 test_slice.py --make-variants some_dir

| File | Content |
| --- | --- |
| `slice_variants.264` | 8 synthetic SPSs, 9 synthetic PPSs, then 324 synthetic slice headers with random but reproducible content (random bytes in place of slice data, so no decoder can make a picture of them). The SPSs cover field pictures (PAFF), MBAFF, all three POC types, monochrome, 4:2:2, 4:4:4 with and without separate colour planes, and `frame_num` widths from 4 to 16 bits. The PPSs combine CAVLC and CABAC, every weighted prediction setting and every optional slice header field, and include PPS id 200. Together the headers reach every branch of 7.3.3: all slice types in both ranges, field pictures, SP and SI, reference count overrides, reordering idc 0 to 2 on both lists, weight tables on both lists with and without chroma, MMCO operations 1 to 6, `nal_ref_idc` 0 and every deblocking case. Halfway through, PPS 2 and PPS 1 are **re-sent with different content**: a parser that caches the first version fails. |
| `slice_multi.ts` | x264 High: 3 slices per picture, 5 reference frames, weighted P, CABAC, B pyramid. |
| `slice_cavlc_multi.ts` | x264 Main: 4 slices per picture, CAVLC, temporal direct, implicit weighted B, strict B pyramid. |

Coverage note: x264 does not write field pictures, long-term operations, B weight tables, POC types 1 and 2, redundant slices, SP/SI or separate colour planes. For those, the **only check is the field-by-field comparison with ffmpeg's `trace_headers`** of `slice_variants.264`; no decoder reads these headers. The self-test also checks that the generator (`slicegen.py`) and ffmpeg agree on every synthetic header.

## 5. Harness input format

One text file, produced by your dump program. Blocks, one per slice NAL unit of type 1 or 5, in stream order:

    SLICE <ordinal>
    <name> <value>
    ...
    DERIVED <name> <value>
    ERROR <free text>            (only for slices that cannot be parsed, section 4.2)

Rules:

- The block header is exactly two words: `SLICE` and a number (0, 1, 2, ... in order).
- A syntax line is exactly two words: the name exactly as in trace_headers, then a decimal integer (minus sign if negative). Flags are 0 or 1.
- Fields appear **in syntax order**, only if present in the bitstream (not inferred). The reordering and marking terminators (`modification_of_pic_nums_idc 3`, `memory_management_control_operation 0`) are printed.
- Names **with** an index, no spaces: `delta_pic_order_cnt[0]`, `delta_pic_order_cnt[1]`, `luma_weight_l0_flag[i]`, `luma_weight_l0[i]`, `luma_offset_l0[i]`, `chroma_weight_l0_flag[i]`, `chroma_weight_l0[i][j]`, `chroma_offset_l0[i][j]`, and the same with `l1`. `i` is the reference index, `j` is 0 (Cb) or 1 (Cr).
- Names **without** an index (repeated in command order): `modification_of_pic_nums_idc`, `abs_diff_pic_num_minus1`, `long_term_pic_num`, `memory_management_control_operation`, `difference_of_pic_nums_minus1`, `long_term_frame_idx`, `max_long_term_frame_idx_plus1`.
- Not printed: `forbidden_zero_bit`, `nal_ref_idc`, `nal_unit_type` (the harness drops them if you do) and `cabac_alignment_one_bit` (reported by the two DERIVED alignment lines instead; also dropped if you print them).
- `DERIVED` lines: the 26 of section 4.3, in that order, each the word, a name and one value (integer, except `slice_type_name`).
- An error block (section 4.2) has the three syntax lines, then one line starting with `ERROR` (the text is free), and **no** `DERIVED` lines. The next block starts normally.
- Blank lines and `#` lines are ignored.

Example: the first slice of T10_full (IDR, CABAC), complete:

    SLICE 0
    first_mb_in_slice 0
    slice_type 7
    pic_parameter_set_id 0
    frame_num 0
    idr_pic_id 0
    pic_order_cnt_lsb 0
    no_output_of_prior_pics_flag 0
    long_term_reference_flag 0
    slice_qp_delta -3
    disable_deblocking_filter_idc 0
    slice_alpha_c0_offset_div2 0
    slice_beta_offset_div2 0
    DERIVED nal_unit_type 5
    DERIVED nal_ref_idc 3
    DERIVED slice_type_mod5 2
    DERIVED slice_type_name I
    DERIVED slice_type_all_same 1
    DERIVED sps_id 0
    DERIVED chroma_array_type 1
    DERIVED entropy_coding_mode_flag 1
    DERIVED frame_num_bits 4
    DERIVED field_pic 0
    DERIVED bottom_field 0
    DERIVED mbaff_frame 0
    DERIVED first_mb_addr 0
    DERIVED num_ref_idx_l0_active 0
    DERIVED num_ref_idx_l1_active 0
    DERIVED num_rplm_l0 0
    DERIVED num_rplm_l1 0
    DERIVED weight_mode 0
    DERIVED num_mmco 0
    DERIVED slice_qp 23
    DERIVED deblocking_idc 0
    DERIVED filter_offset_a 0
    DERIVED filter_offset_b 0
    DERIVED header_end_bit 40
    DERIVED cabac_alignment_bits 2
    DERIVED cabac_alignment_ok 1

(`header_end_bit` 40: the last deblocking field ends at bit 38, two alignment bits bring it to the byte boundary 40.) The syntax part of a B slice with marking commands from T9_spatial:

    first_mb_in_slice 0
    slice_type 6
    pic_parameter_set_id 0
    frame_num 5
    pic_order_cnt_lsb 14
    direct_spatial_mv_pred_flag 1
    num_ref_idx_active_override_flag 0
    ref_pic_list_modification_flag_l0 0
    ref_pic_list_modification_flag_l1 0
    adaptive_ref_pic_marking_mode_flag 1
    memory_management_control_operation 1
    difference_of_pic_nums_minus1 2
    memory_management_control_operation 1
    difference_of_pic_nums_minus1 3
    memory_management_control_operation 0
    slice_qp_delta 1
    ...

Choices the harness makes, so you are never guessing:

- **Which slices are the truth.** Every slice header block of `trace_headers` whose `nal_unit_type` is 1 or 5, in stream order, repeats and multi-slice pictures included.
- **Context.** The harness reads the SPS and PPS blocks of the same trace and, for each slice, uses the latest SPS/PPS NAL units with the ids in question that precede it (so re-sent parameter sets are handled the way section 3.1 says). The SPS and PPS blocks before the first packet of the trace (the "Extradata" copies) seed the tables.
- **Bit positions.** ffmpeg's trace prints the bit offset of every element (section 3.9). `header_end_bit` is the offset after the last printed element, which includes the alignment bits. The harness gets `cabac_alignment_bits` from the number of alignment lines in the trace.
- **The alignment check does not rely on ffmpeg's word.** The harness separately extracts the raw H.264 stream (the input itself if it is a `.264`, else `ffmpeg -c copy -f h264`), finds the slice NAL units with its own start code scanner, removes emulation prevention bytes, and checks on the real bits that the alignment bits sit where the trace says, all are 1 and the slice data starts on a byte boundary, for CABAC slices (and that CAVLC slices have none). Your `cabac_alignment_ok` and `cabac_alignment_bits` are then compared to this truth.
- **Ignored.** NAL header fields, SPS and PPS blocks (except as context), other NAL types, log noise (including decoder messages that ffmpeg interleaves into the trace).

## 6. Running the harness

    python3 test_slice.py <input.ts|input.264> <your_slice_dump.txt>

It runs `ffmpeg -c:v copy -bsf:v trace_headers` on the input. Exit 0 on pass, 1 on a mismatch (the first 10 are listed), 2 if your file is malformed or the input is unusable.

    python3 test_slice.py --make-variants some_dir     # writes the extra inputs of section 4.4
    python3 test_slice.py --self-test                  # checks the harness itself

`--self-test` builds known-good dumps from ffmpeg truth for all the inputs of section 4.4 (must pass); checks the synthetic generator against ffmpeg; checks that the independent alignment test notices a cleared alignment bit; then corrupts a dump eight ways (wrong `frame_num`, missing `luma_log2_weight_denom`, missing `bottom_field_flag`, missing MMCO terminator, wrong two DERIVED values, `cabac_alignment_ok` 0, a missing block; each must fail). It needs ffmpeg with libx264.

## 7. Reading failures and debugging tips

A failure message looks like:

    FAIL: SLICE #4 (slice_type 6) field #16: field name: expected 'luma_log2_weight_denom', got 'luma_weight_l0_flag[0] 0'
             expected syntax section: pred_weight_table (7.3.3.2)
             previous fields (all matched): abs_diff_pic_num_minus1 36; ...
             context: nal_unit_type 1, nal_ref_idc 2; PPS 4: entropy_coding_mode_flag 1, weighted_pred_flag 1, ...

`SLICE #n` counts slice NAL units from 0 and `field #i` counts compared fields in the header. The `context` line lists the PPS and SPS values that decide the optional fields, as ffmpeg sees them: compare it with the values your own tables hold for that slice.

Common causes of a first mismatch:

- **`frame_num` wrong or later fields shifted:** the `frame_num` width comes from the SPS (`log2_max_frame_num_minus4 + 4`), not a constant 4 or 8.
- **`field_pic_flag` expected but absent (or vice versa):** it depends on `frame_mbs_only_flag`, not on interlace in the picture.
- **`idr_pic_id` missing or extra:** test `nal_unit_type` = 5, not `slice_type` = 2.
- **`delta_pic_order_cnt_bottom` / `delta_pic_order_cnt[1]` wrong:** needs the PPS flag **and** `field_pic_flag` = 0.
- **`redundant_pic_cnt` wrong:** the PPS flag, not the SPS.
- **The weight table has too many or too few entries:** you used the PPS default list sizes although the slice overrode them (or the other way round); or you forgot list 1 for B slices; or you read chroma fields for monochrome / separate colour planes (use ChromaArrayType).
- **Weight table appears in a B slice with `weighted_bipred_idc` 2:** it must not; only value 1 sends a table.
- **Reordering commands run on:** you stopped at the first command; the loop ends at `modification_of_pic_nums_idc` 3 (and is not entered when the flag is 0). Same for MMCO and operation 0.
- **MMCO fields wrong:** operation 3 has two arguments (`difference_of_pic_nums_minus1`, then `long_term_frame_idx`); operation 5 has none; 2 uses `long_term_pic_num`.
- **`dec_ref_pic_marking` present when it must not be (or absent when present):** it depends on `nal_ref_idc` ≠ 0. B pictures that are not used for reference have `nal_ref_idc` 0.
- **`cabac_init_idc` extra in I slices or missing in P/B:** also depends on the PPS `entropy_coding_mode_flag`.
- **`slice_alpha_c0_offset_div2` extra when `disable_deblocking_filter_idc` is 1:** the offsets are present only for 0 and 2.
- **`DERIVED header_end_bit` wrong though all fields match:** you counted from the start of the RBSP instead of the NAL header byte (off by 8), or forgot the alignment bits.
- **`cabac_alignment_ok` 0:** an earlier field was misread but happens to produce plausible values. Fix the first field that differs from the trace.
- **`DERIVED num_ref_idx_l0_active` wrong:** override flag logic, see section 3.4.
- **Block count wrong:** you skipped a slice of a multi-slice picture, or you printed slices of NAL types 2 to 4, 19 or 20.

The raw trace shows every element with its bit offset: `ffmpeg -i X -c:v copy -bsf:v trace_headers -f null - 2>&1 | less`. Compare your read position with column one as you go.

## 8. What's next

Chapter 3.4 uses `frame_num` and the POC fields you just parsed to find picture boundaries and compute the picture order count, so that pictures can be put in display order.
