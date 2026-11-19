# Chapter 3.2 - PPS parser

## 1. Goal and the project

Chapter 3.1 gave you the SPS table. Now you parse the second kind of parameter set, the **picture parameter set (PPS)**, NAL unit type 8: every field of section 7.3.2.2, including the optional trailing fields that exist only when `more_rbsp_data()` says there are bits left.

Your program prints each PPS as text, with the field names and order of ffmpeg's `trace_headers`, in the same style as Chapter 3.1. The harness extracts the same fields from `trace_headers` and compares them name by name and value by value. A small set of derived values is checked too.

This chapter has one real trap that the SPS did not have: **the PPS cannot be parsed by itself.** The number of scaling lists in its optional tail depends on the `chroma_format_idc` of the SPS it points to. So the SPS table from Chapter 3.1 is an input to the PPS parser.

## 2. Big picture

The SPS holds facts that last a whole video sequence. The PPS holds the facts that may change from picture to picture, so that a slice header does not have to repeat them. A slice header names a PPS id; the PPS names an SPS id.

What the PPS carries, in plain words:

- which entropy coder the slice data uses (CAVLC or CABAC),
- the default number of reference pictures per list,
- whether weighted prediction is on,
- the starting quantiser values (QP) and the chroma QP offsets,
- a few switches for deblocking, intra prediction and redundant slices,
- for High profile: whether the 8x8 transform is allowed, and optional quantiser scaling matrices.

Up to 256 PPSs (ids 0 to 255) can exist at the same time, so an encoder can switch between settings by naming a different PPS id. A PPS may be **re-sent with new content** under the same id between pictures; the new content replaces the old one from then on. Streams also repeat identical PPSs before every IDR picture, as T1 does (50 of them).

A PPS is a short bit string, typically 4 to 15 bytes, except when it carries scaling lists. Like the SPS, it is a straight-line translation of a syntax table with a few conditionals. One bit too many or too few shifts everything after it.

## 3. Concepts in detail

The fields are read from the RBSP of the NAL unit (emulation prevention bytes already removed, 1-byte NAL header skipped). `u(n)` is n raw bits; `ue(v)` and `se(v)` are Exp-Golomb codes (section 9.1). The names below are the trace_headers names.

### 3.1 The fixed part

| Field | Descriptor | Notes |
| --- | --- | --- |
| `pic_parameter_set_id` | ue(v) | 0 to 255. The key of your PPS table. |
| `seq_parameter_set_id` | ue(v) | 0 to 31. Must name an SPS you already have: see section 3.5. |
| `entropy_coding_mode_flag` | u(1) | 0 = CAVLC (Exp-Golomb and tables, Phase 4), 1 = CABAC (Phase 8). |
| `bottom_field_pic_order_in_frame_present_flag` | u(1) | If 1, slice headers of frame pictures carry an extra `delta_pic_order_cnt[1]` (Chapter 3.3). The spec used to call this `pic_order_present_flag`. |
| `num_slice_groups_minus1` | ue(v) | 0 means one slice group, the normal case. See section 3.2. |
| `num_ref_idx_l0_default_active_minus1` | ue(v) | 0 to 31. Default size minus 1 of reference list 0. A slice header may override it. |
| `num_ref_idx_l1_default_active_minus1` | ue(v) | Same for list 1 (B slices). Always present, even in streams without B slices. |
| `weighted_pred_flag` | u(1) | 1: P slices carry explicit prediction weights. |
| `weighted_bipred_idc` | u(2) | 0 = default (plain average) for B slices, 1 = explicit weights, 2 = implicit weights computed from picture distances. 3 is reserved. |
| `pic_init_qp_minus26` | se(v) | See below. |
| `pic_init_qs_minus26` | se(v) | The same for SP/SI slices (Extended profile; you only parse it). |
| `chroma_qp_index_offset` | se(v) | -12 to 12. Added to the luma QP before the chroma QP lookup (Phase 5). |
| `deblocking_filter_control_present_flag` | u(1) | If 1, slice headers carry the deblocking control fields (Chapter 3.3). If 0 they are absent and deblocking is on with offsets 0. |
| `constrained_intra_pred_flag` | u(1) | If 1, intra prediction must not use samples of inter-coded neighbours (Phase 5). |
| `redundant_pic_cnt_present_flag` | u(1) | If 1, slice headers carry `redundant_pic_cnt` (Chapter 3.3). |

**The +26 offset.** The two QP fields are stored relative to 26, the middle of the QP range 0 to 51, because a typical value is close to it and small numbers cost fewer bits. That is why they are signed (`se(v)`): a starting QP below 26 is a negative value. The real starting QP is `26 + pic_init_qp_minus26` (the field ranges from -26 to +25 for 8-bit video), and a slice then adds its own `slice_qp_delta` to it. The same applies to `pic_init_qs_minus26`. Print the field as it is coded (the `_minus26` name and raw value); the harness also checks the sum as a derived value.

**Minus-one fields.** `num_ref_idx_l0_default_active_minus1` is coded as count minus 1, as the name says. The count is what you will use later.

### 3.2 Slice groups (FMO): out of scope

When `num_slice_groups_minus1` is greater than 0, the picture is divided into several **slice groups**, each a set of macroblocks (flexible macroblock ordering, FMO). Right after `num_slice_groups_minus1` come `slice_group_map_type` (ue(v), 0 to 6, the shape of the groups) and then, depending on it:

| `slice_group_map_type` | Extra fields |
| --- | --- |
| 0 | `run_length_minus1[i]` for each group |
| 1 | none (dispersed) |
| 2 | `top_left[i]`, `bottom_right[i]` for each group but the last |
| 3, 4, 5 | `slice_group_change_direction_flag`, `slice_group_change_rate_minus1` |
| 6 | `pic_size_in_map_units_minus1`, then `slice_group_id[i]` for each map unit, each `Ceil(Log2(num_slice_groups_minus1 + 1))` bits wide |

FMO is allowed only in Baseline and Extended profiles (Main and High forbid it) and is rare in practice. This book does not decode it. So:

- Your reader **must reject** any PPS with `num_slice_groups_minus1` > 0 with a clear error message, because the decoder would otherwise silently produce garbage.
- It must still **print** `num_slice_groups_minus1` first, so the harness (and you) can see why.
- You are not required to parse the remaining slice-group fields; the harness does not compare them. After the error, carry on with the next NAL unit. Stopping the whole program is allowed for a real decoder, but then you cannot pass the `pps_fmo.264` input of section 4.3.

### 3.3 Optional trailing fields and `more_rbsp_data()`

The original 2003 syntax ended after `redundant_pic_cnt_present_flag`. High profile (2005) needed more PPS fields. To stay compatible, the new fields were added at the **end** and are present only if the RBSP has more data before the trailing bits. Old decoders stop after `redundant_pic_cnt_present_flag` and ignore the rest, which is harmless; new decoders ask `more_rbsp_data()`.

This is why `more_rbsp_data()` from Chapter 2.2 exists. There is no flag that says "the tail is present". The only way to know is to look at the remaining bits:

- The RBSP ends with `rbsp_trailing_bits`: one 1 bit, then 0 bits up to the byte boundary.
- `more_rbsp_data()` is true if there is any bit after the current position other than that final 1 bit and its padding zeros. Equivalently: find the **last 1 bit** of the whole RBSP; if the current position is before it, more data exists.

Be careful: you cannot just test "is the current position at the end of the data". A PPS may end in the middle of a byte, and the padding zeros must not count as data; neither does the stop bit itself. Searching for the last 1 bit from the end also copes with streams that append extra zero bytes after the stop bit.

Baseline and Main streams (T1, T10_full) have **no tail**: they stop after `redundant_pic_cnt_present_flag`. x264 High streams (T11_high) have it, with `transform_8x8_mode_flag` = 1 and no scaling matrix. The tail:

| Field | Descriptor | Notes |
| --- | --- | --- |
| `transform_8x8_mode_flag` | u(1) | 1: macroblocks may use the 8x8 integer transform (Phase 8). |
| `pic_scaling_matrix_present_flag` | u(1) | 1: scaling lists follow, which override the SPS ones for pictures using this PPS. |
| `pic_scaling_list_present_flag[i]` | u(1) | One per list, see below. |
| scaling list data | `scaling_list()` | After a flag that is 1. Printed as `delta_scale[j]`. |
| `second_chroma_qp_index_offset` | se(v) | Offset for the Cr plane. Always present when the tail exists. |

When the tail is **absent**, you must infer:

- `transform_8x8_mode_flag` = 0,
- `pic_scaling_matrix_present_flag` = 0 (no PPS lists; the SPS lists, if any, apply),
- `second_chroma_qp_index_offset` = `chroma_qp_index_offset`.

Do not print these inferred fields (only fields present in the bitstream are printed), but store the inferred values in your table.

A tail that is present always has `transform_8x8_mode_flag`, `pic_scaling_matrix_present_flag` and `second_chroma_qp_index_offset`, so a PPS cannot end between them. Your dump's `DERIVED trailing_fields_present` line (section 4.2) records which case a PPS was.

### 3.4 Scaling lists in the PPS: how many?

If `pic_scaling_matrix_present_flag` is 1, there is a list of present flags, and the **count depends on two things**:

    number of lists = 6 + (chroma_format_idc != 3 ? 2 : 6) * transform_8x8_mode_flag

| chroma_format_idc of the SPS | transform_8x8_mode_flag | Lists |
| --- | --- | --- |
| 0, 1 or 2 (4:0:0, 4:2:0, 4:2:2) | 0 | 6 |
| 0, 1 or 2 | 1 | 8 |
| 3 (4:4:4) | 0 | 6 |
| 3 | 1 | 12 |

Be careful with two points. The formula uses **`chroma_format_idc`** of the SPS, not `ChromaArrayType` (the two differ when `separate_colour_plane_flag` is 1). And for an SPS of a Baseline or Main profile, which has no `chroma_format_idc` field, the inferred value is 1.

(The SPS formula in Chapter 3.1 is different: it uses 8 lists, or 12 for 4:4:4, and has no `transform_8x8_mode_flag` factor, because the SPS has no such flag.)

The list order and sizes are those of Chapter 3.1: lists 0 to 5 are 4x4 (Intra Y, Cb, Cr, then Inter Y, Cb, Cr), 16 entries each; lists 6 and up are 8x8 and have 64 entries: Intra Y, Inter Y, and in 4:4:4 Intra Cb, Inter Cb, Intra Cr, Inter Cr. Each present list is coded by the same `scaling_list()` delta code (7.3.2.1.1.1) as in the SPS: start with `lastScale` and `nextScale` at 8, read `delta_scale` while `nextScale` is not 0, and stop early once `nextScale` becomes 0. A list with `pic_scaling_list_present_flag` 0 has nothing coded; it is later filled by the **fall-back rules** (Table 7-2; the fall-back reference differs between PPS and SPS), which belong to Phase 8. Print `delta_scale[j]` with j restarting at 0 for every list, as in Chapter 3.1.

On x264: it writes custom matrices in the PPS only. Its `cqm=jvt` preset uses the spec's default matrices, so it sends all flags 0 (T11_cqm shows 8 flags, all 0). Only a custom matrix gives you real `delta_scale` values; the harness creates such a file (section 4.3).

### 3.5 The dependence on the SPS

Because of section 3.4, you cannot parse the tail of a PPS without knowing the SPS's `chroma_format_idc`. In your program:

1. Keep the SPS table of Chapter 3.1, filled as the NAL units arrive.
2. When a PPS arrives, look up `seq_parameter_set_id` in it and use that SPS's `chroma_format_idc` (1 if the SPS has none).
3. If there is no such SPS, you cannot continue with this PPS. ffmpeg's `trace_headers` stops with "SPS id N not available", so the harness cannot check such a stream and says so. The spec allows a PPS to arrive before its SPS, as long as the SPS is there when a slice uses it. A robust decoder would keep the raw RBSP and parse it later; this book's streams always send the SPS first, so reporting an error is enough.

Which SPS counts is the one stored **at the time the PPS arrives**. A later re-sent SPS with a different `chroma_format_idc` does not change how an earlier PPS was parsed, and it would only be legal at an IDR picture anyway. Re-sent SPSs with the same content are the usual case.

### 3.6 End of the PPS

After the last field comes `rbsp_trailing_bits`. As in Chapter 3.1, after a correct parse the stop bit must sit exactly at the current position (`more_rbsp_data()` is false). Use this as an assertion while debugging. It is how you will catch a wrong scaling-list count immediately.

## 4. The project: exactly what to build

### 4.1 Parser and table

Write `parse_pps(rbsp, sps_table)` returning a record with every field, the inferred values of section 3.3, and the derived values of section 4.2. Keep a table of 256 PPS slots indexed by `pic_parameter_set_id`. A new PPS with an id that is already stored replaces the old one. Chapter 3.3 will use this table to find, for each slice, the PPS and then the SPS.

Write a **dump program** that:

1. reads a `.ts` file or raw `.264` file with your earlier code,
2. goes over the NAL units in stream order, parsing every SPS (type 7, with your 3.1 code, into the SPS table) and every PPS (type 8),
3. prints one text block per PPS NAL unit, in the format of section 5.

Print **every PPS NAL unit that occurs**, including repeats and re-sent changed ones. Do not de-duplicate. You may print the SPSs too, but the harness will not look at them, so keep them in a separate file or leave them out: the PPS dump file must contain only `PPS` blocks.

### 4.2 Derived values

| Line | Meaning |
| --- | --- |
| `DERIVED pic_init_qp N` | 26 + `pic_init_qp_minus26`. |
| `DERIVED pic_init_qs N` | 26 + `pic_init_qs_minus26`. |
| `DERIVED num_ref_idx_l0_default_active N` | `num_ref_idx_l0_default_active_minus1` + 1. |
| `DERIVED num_ref_idx_l1_default_active N` | Same for list 1. |
| `DERIVED trailing_fields_present N` | 1 if `more_rbsp_data()` was true after `redundant_pic_cnt_present_flag`, else 0. |
| `DERIVED transform_8x8_mode_flag N` | The coded value, or 0 if the tail is absent. |
| `DERIVED sps_chroma_format_idc N` | The `chroma_format_idc` of the SPS this PPS was parsed against (1 if that SPS has no such field). |
| `DERIVED num_scaling_lists N` | 0 if `pic_scaling_matrix_present_flag` is 0 or absent, else the count of section 3.4. |
| `DERIVED second_chroma_qp_index_offset N` | The coded value, or `chroma_qp_index_offset` if the tail is absent. |

The lines come in exactly this order, after the last syntax line of the block, all always present (except for rejected PPSs, see below). The checkable consequences of the SPS dependence are `sps_chroma_format_idc` and `num_scaling_lists`.

Verify (the Summary's step): your dump matches `trace_headers` field by field, on at least one Baseline (T1) and one High (T11_high) file.

### 4.3 Test inputs

Run the harness on `T1` (Baseline: no tail, 50 repeated PPSs), `T10_full` (Main, CABAC, weighted prediction flags set), `T11_high` (tail with `transform_8x8_mode_flag` 1), `T11_cqm` (tail with all-zero list flags), and any real file. To cover the rest:

    python3 test_pps.py --make-variants some_dir

writes into `some_dir`:

| File | Content |
| --- | --- |
| `pps_variants.264` | Four **synthetic SPS** NAL units (ids 1 to 4: Main with no `chroma_format_idc`, High 4:2:0, High 4:4:4, High 4:2:2), then ten synthetic PPS NAL units, followed by a real 3-frame High stream (SPS 0, PPS 0). The PPSs cover: no tail (Main SPS); a tail with `transform_8x8_mode_flag` 0 and no matrix; a matrix with 8 lists (explicit, `-8` use-default, absent, early-terminated, all-zero deltas, 64-entry lists) on a 4:2:0 SPS; a matrix with `transform_8x8_mode_flag` 0 and so only 6 lists; the same for a 4:4:4 SPS (12 lists, then 6 lists); a 4:2:2 SPS (8 lists); extreme values (PPS id 255, QP -26 and +25, chroma offsets -12 and +12, 32 reference indices, `weighted_bipred_idc` 2, all flags set); and PPS id 2 **re-sent twice** with changed, then identical content. |
| `pps_fmo.264` | A Baseline SPS and four PPSs, three of which use slice groups (map types 0, 2 and 6), then a real stream. |
| `pps_cqm.ts` | x264 with custom quantiser matrices (`cqm4iy` and `cqm8iy`): real explicit scaling lists in a PPS. |

The synthetic SPSs are minimal (POC type 2, no VUI): they exist only so that the PPSs have an SPS to refer to, and your program has to parse them with its Chapter 3.1 code. Most of these cases cannot be made by x264, which writes at most `transform_8x8_mode_flag`, a matrix and `second_chroma_qp_index_offset`, and only for 4:2:0.

## 5. Harness input format

One text file, produced by your dump program. Blocks, one per PPS NAL unit, in stream order:

    PPS <ordinal>
    <name> <value>
    <name> <value>
    ...
    DERIVED <name> <value>
    ERROR <free text>            (only for rejected PPSs)

Rules:

- The block header is exactly two words: `PPS` and a number (0, 1, 2, ... in order). The harness uses it to start a new block.
- A syntax line is exactly two words, `<name> <integer>`, separated by whitespace: the name exactly as in the tables of section 3, then a decimal integer (with a minus sign if negative, `chroma_qp_index_offset -2`). Flags are 0 or 1. No hex, no extra text.
- Array elements carry their index with no spaces: `pic_scaling_list_present_flag[3]`, `delta_scale[0]`, `run_length_minus1[1]`. Indices of `delta_scale` restart at 0 for each scaling list.
- Fields appear **in syntax order** and only if they are present in the bitstream (not inferred). `transform_8x8_mode_flag`, `pic_scaling_matrix_present_flag` and `second_chroma_qp_index_offset` appear only if the tail is present.
- A rejected PPS (`num_slice_groups_minus1` > 0) is a block that ends right after the line `num_slice_groups_minus1 <n>`, followed by one line starting with `ERROR` (the text is free and not compared) and **no** DERIVED lines. The next block starts normally.
- `DERIVED` lines (the 9 of section 4.2, in that order) come after the syntax lines. A derived line is the word `DERIVED`, a name and one decimal integer.
- Blank lines and lines starting with `#` are ignored. You may also print the NAL header fields and `rbsp_stop_one_bit` / `rbsp_alignment_zero_bit`; the harness drops them. It is simplest not to.

Example (a Baseline PPS from T1, no tail):

    PPS 0
    pic_parameter_set_id 0
    seq_parameter_set_id 0
    entropy_coding_mode_flag 0
    bottom_field_pic_order_in_frame_present_flag 0
    num_slice_groups_minus1 0
    num_ref_idx_l0_default_active_minus1 0
    num_ref_idx_l1_default_active_minus1 0
    weighted_pred_flag 0
    weighted_bipred_idc 0
    pic_init_qp_minus26 0
    pic_init_qs_minus26 0
    chroma_qp_index_offset -2
    deblocking_filter_control_present_flag 1
    constrained_intra_pred_flag 0
    redundant_pic_cnt_present_flag 0
    DERIVED pic_init_qp 26
    DERIVED pic_init_qs 26
    DERIVED num_ref_idx_l0_default_active 1
    DERIVED num_ref_idx_l1_default_active 1
    DERIVED trailing_fields_present 0
    DERIVED transform_8x8_mode_flag 0
    DERIVED sps_chroma_format_idc 1
    DERIVED num_scaling_lists 0
    DERIVED second_chroma_qp_index_offset -2

Choices the harness makes, so you are never guessing:

- **Which PPS blocks are the truth.** As in Chapter 3.1, ffmpeg prints an "Extradata" copy first; the harness ignores that and uses only the PPS NAL units inside packets. Your count must match, repeats included.
- **Ignored lines.** The NAL header fields, `rbsp_stop_one_bit`, `rbsp_alignment_zero_bit`, every SPS and slice header block, and log noise.
- **SPS lookup in the harness.** It reads the SPS blocks of the same trace, and for each PPS uses the latest SPS NAL with the same id seen before it. It checks its own list-count formula against the number of `pic_scaling_list_present_flag` fields ffmpeg actually printed, so a wrong formula in the harness is not possible to hide.
- **Scaling list entries.** trace_headers prints the coded `delta_scale[j]` values, not the final entries. So do you.

## 6. Running the harness

    python3 test_pps.py <input.ts|input.264> <your_pps_dump.txt>

It runs `ffmpeg -c:v copy -bsf:v trace_headers` on the input. Exit 0 on pass, 1 on a mismatch (the first 10 are listed), 2 if your file is malformed or the input is unusable.

    python3 test_pps.py --make-variants some_dir     # writes the extra inputs of section 4.3
    python3 test_pps.py --self-test                  # checks the harness itself

`--self-test` builds known-good dumps from ffmpeg truth for T1, T10_full, T11_high, T11_cqm, T9_spatial, T12 and the three generated inputs (must pass), then corrupts dumps six ways (wrong QP value, missing `second_chroma_qp_index_offset`, 8 lists where 6 are right, wrong derived value, a missing PPS block, an FMO block without ERROR line; each must fail). It needs ffmpeg with libx264.

## 7. Reading failures and debugging tips

A failure message looks like:

    FAIL: PPS #3 field #41: field name: expected 'second_chroma_qp_index_offset', got 'pic_scaling_list_present_flag[6] 0'
             expected syntax section: optional trailing fields (only if more_rbsp_data()): ...
             previous fields (all matched): ...

- `PPS #n` counts PPS NAL units from 0 and `field #i` counts compared fields inside it. The "previous fields" show the last four that matched.
- "block ends early" means you stopped reading: often you decided `more_rbsp_data()` is false when it is true, or you read one list too few.

Common causes of a first mismatch:

- At `transform_8x8_mode_flag` (expected but missing): your `more_rbsp_data()` is wrong. A typical bug is treating the stop bit as data, or testing "position at end of byte".
- An extra `transform_8x8_mode_flag` where none is expected (Baseline PPSs): the same bug the other way: you read the stop bit and padding as fields.
- After `pic_scaling_matrix_present_flag`, one list too many or too few: you used 8 lists for a 4:4:4 stream, or 6 and 8 mixed up. Use `DERIVED num_scaling_lists` and `sps_chroma_format_idc` to see which; the second one wrong means your SPS lookup fails (wrong id, or a Main-profile SPS that did not get the inferred value 1).
- After a `delta_scale` run: you did not stop reading when `nextScale` became 0, as in Chapter 3.1.
- `second_chroma_qp_index_offset` wrong: it is `se(v)`, not `ue(v)`; the same for the QP fields.
- `DERIVED second_chroma_qp_index_offset` wrong for Baseline: it must equal `chroma_qp_index_offset` (-2 in the example above).
- Block count wrong: you dropped repeats or the re-sent PPS.
- Slice group fields printed after `num_slice_groups_minus1`: stop printing there and print the `ERROR` line instead.

A good internal assertion: after the last field of every PPS, `more_rbsp_data()` must be false. If it is true, you stopped too early, and the stray bits tell you how much you missed. The bit positions in the raw trace (`ffmpeg -i X -c:v copy -bsf:v trace_headers -f null - 2>&1`) are counted from the start of the NAL unit including its 8-bit header.

## 8. What's next

Chapter 3.3 parses slice headers: it looks up the PPS by id (your new table), then its SPS, and uses fields from both (`frame_num` width, `pic_order_cnt_type`, `bottom_field_pic_order_in_frame_present_flag`, `num_ref_idx_l0_default_active_minus1`, `weighted_pred_flag`, `deblocking_filter_control_present_flag`, ...).
