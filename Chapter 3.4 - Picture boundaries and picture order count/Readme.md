# Chapter 3.4 - Picture boundaries and picture order count

## 1. Goal and the project

Chapter 3.3 gave you every slice header. This chapter does two things with them.

1. **Picture boundaries.** A coded picture is made of one or more slices, and nothing in the stream says "this slice starts a new picture". You work it out by comparing each slice header with the previous one (7.4.1.2.4), helped by a few NAL unit types (7.4.1.2.3). The result is the list of **pictures in decode order**, each with its slices.
2. **Picture order count (POC).** Pictures are stored in the stream in **decode order**, which differs from the order in which they are shown (**display order**) as soon as the stream has B pictures. The POC is a number per picture whose order is the display order. You compute it for all three `pic_order_cnt_type` modes (8.2.1).

Your program prints one line per picture, in decode order, and then a second list of the same pictures sorted into display order. The harness compares both lists with its own implementation of the same rules, with ffprobe's frame count and `pict_type` sequence, and with the order of the PTS values from Phase 1.

This is the last chapter of Phase 3. After it your decoder can read every header of the stream and knows which slices form which picture and in what order the pictures are shown.

**Scope.** Progressive frames, and MBAFF frames (T12), are in scope: `field_pic_flag` = 0 everywhere. Field pictures (PAFF) are described in places so that you understand the rules, but you do not implement them until Phase 10, and the harness refuses streams that contain them (exit 2). Gaps in `frame_num` are noted only.

## 2. Big picture

### Why a picture order count exists

An encoder gets the best compression by coding some pictures **out of display order**. A typical pattern, in display order, is `I B B P B B P`. The B pictures predict from the pictures on both sides, so the P that follows them must be coded first. The stream therefore holds them in the order `I P B B P B B`, and the decoder has to put them back.

The stream can carry timestamps (PTS and DTS in the TS, Phase 1) but an H.264 elementary stream must work without them, for example when read from a file with no container. So the order is also coded **inside** the slice headers, as small numbers that a decoder turns into a POC. Decoding itself needs POC too, not only display: temporal direct prediction and implicit weighted prediction (Phase 7) use **differences between POCs** as a measure of distance in time.

### Decode order vs display order

| Picture | I | P | B | B | P | B | B |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Decode order index | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
| POC | 0 | 6 | 2 | 4 | 12 | 8 | 10 |
| Display position | 0 | 3 | 1 | 2 | 6 | 4 | 5 |

The POC does not have to count by 1 (x264 counts by 2, because a frame has two fields), and it only needs to be monotonic in display order. Only differences and ordering matter.

### The IDR period

An **IDR picture** (`nal_unit_type` 5) empties the reference buffer and restarts the numbering: its POC is 0 and nothing after it refers to anything before it. So display order is only defined **inside** a period that starts at an IDR picture. Pictures of the next period never move in front of pictures of the previous one. The harness builds the display list per period and concatenates them. (Section 3.7 adds the second thing that starts a period, MMCO 5.)

### The two halves of the chapter

```
slice headers -> [picture boundary rule] -> pictures in decode order
                                          -> [POC rule per picture] -> POC
                                          -> sort inside each IDR period -> display order
```

## 3. Concepts in detail

### 3.1 Access units and primary coded pictures

Terms from 7.4.1.2:

- An **access unit** is everything that belongs to one decoded picture: optional header NAL units, the slices of the primary coded picture, and optionally a redundant coded picture and other data. In a TS, one PES packet normally holds one access unit, which is why Phase 1's packet list has one entry per picture.
- The **primary coded picture** is the normal one. Slices with `redundant_pic_cnt` above 0 belong to a redundant coded picture (an error-resilience copy). This book ignores them: **skip every slice whose `redundant_pic_cnt` is greater than 0**. They do not start a picture, are not counted in `num_slices` and do not take part in comparisons.

A picture has one or more slices; the first slice of a picture normally has `first_mb_in_slice` = 0, but you must **not** use that to find boundaries (with arbitrary slice order the first slice need not have 0, and a lost slice would fool you). Use the rules below.

### 3.2 The rule from the NAL unit types (7.4.1.2.3)

A **new access unit starts** when one of these NAL units appears after a slice (VCL) NAL unit of the current picture:

| nal_unit_type | Name |
| --- | --- |
| 9 | access unit delimiter (AUD) |
| 7 | SPS |
| 8 | PPS |
| 6 | SEI |
| 14 to 18 | prefix NAL, subset SPS, reserved: SVC/MVC related, you will not see them in this book's streams |

So when you scan NAL units in order and see any of those after a slice, the **next slice** starts a new picture. The ffmpeg-written TS files have an AUD in front of every picture, which makes this rule enough on its own for them. Real streams often have no AUDs, so you need the second rule too.

### 3.3 The rule from the slice headers (7.4.1.2.4)

The first slice NAL unit of a picture is detected by comparing the current slice with the **previous slice** (the previous primary slice, in stream order). It is the first slice of a new picture if **any** of these values differs:

| # | Compared value | Notes |
| --- | --- | --- |
| 1 | `frame_num` | |
| 2 | `pic_parameter_set_id` | |
| 3 | `field_pic_flag` | 0 if absent |
| 4 | `bottom_field_flag` | 0 if absent (a top field and the bottom field of the same frame are two pictures) |
| 5 | `nal_ref_idc` | only when **one of the two values is 0**: 3 vs 2 is not a difference; 0 vs 1 is |
| 6 | for `pic_order_cnt_type` 0: `pic_order_cnt_lsb`, and `delta_pic_order_cnt_bottom` | 0 if absent |
| 7 | for `pic_order_cnt_type` 1: `delta_pic_order_cnt[0]`, and `delta_pic_order_cnt[1]` | 0 if absent |
| 8 | `IdrPicFlag` (`nal_unit_type` 5 or not) | |
| 9 | if both slices are IDR: `idr_pic_id` | |

(The spec also compares `MVCview` fields for MVC; not relevant here.) `pic_order_cnt_type` 2 has no POC fields to compare.

Why this works: slices of one picture repeat these fields identically in every header, and two consecutive pictures cannot be identical in all of them. Some cases where only **one** field separates two pictures, which are worth a test of their own:

- Two non-reference B pictures in a row have the same `frame_num` (non-reference pictures do not advance it) and typically the same PPS: only `pic_order_cnt_lsb` (type 0) or `delta_pic_order_cnt[0]` (type 1) tells them apart.
- A non-reference picture followed by a reference picture has the same `frame_num` (the reference picture's number is the previous reference number plus 1, and so was the non-reference picture's): with POC type 2 only `nal_ref_idc` (0 vs not 0) differs.
- Two IDR pictures in a row both have `frame_num` 0 and POC 0: only `idr_pic_id` differs. This is what `idr_pic_id` exists for.

**Combine the two rules**: a slice starts a new picture if the header rule fires **or** a non-VCL NAL unit of section 3.2 came since the previous slice. On valid streams the two agree. When they disagree, this book (and the harness) lets either one start a new picture.

The first slice of the stream always starts the first picture.

### 3.4 What to record per picture

Everything the later chapters need, taken from the **first slice** of the picture (all slices agree on these):

| Value | Source |
| --- | --- |
| `idr` | 1 if `nal_unit_type` is 5 |
| `nal_ref_idc` | first slice's NAL header |
| `type` | the picture's slice type: `slice_type % 5` of the **first slice**: `P`, `B`, `I`, `SP` or `SI`. |
| `num_slices` | the number of primary slices in the picture |
| `frame_num` | slice header |
| `mmco5` | 1 if the marking commands in the slice header contain `memory_management_control_operation` 5 |
| `first_nal` | index of the picture's first slice NAL unit among **all** NAL units of the stream (section 5) |
| TopFieldOrderCnt, BottomFieldOrderCnt, PicOrderCnt | section 3.5 |

**Which slice type represents a picture?** The spec allows a picture to mix slice types (an I slice inside a P picture, for instance). This book uses the type of the first slice. x264 writes the same type in all slices of a picture, so on the test files this always equals ffprobe's `pict_type`.

### 3.5 Picture order count (8.2.1)

Each frame has a **TopFieldOrderCnt** and a **BottomFieldOrderCnt**: the POC of its top and bottom field (the lines 0, 2, 4, ... and 1, 3, 5, ... of a frame). Their meaning for a **frame** is "when the two fields would be displayed". For a frame:

> PicOrderCnt(frame) = Min(TopFieldOrderCnt, BottomFieldOrderCnt)

That is the number you sort by. In a progressive stream the two are usually equal or differ by 1. (For a single field, PicOrderCnt is that field's own count; for the pair of fields of a frame it is again the minimum. Phase 10.)

The computation depends on `pic_order_cnt_type` from the SPS. It needs a little **state from earlier pictures**, described per type. Compute the POC when the picture's first slice arrives, in decode order.

Each type also mentions **MMCO 5**, a marking command (parsed in 3.3) that resets the numbering in the middle of a stream, much like an IDR picture. Section 3.7 explains it; until then, read "MMCO 5" as "reset the state the way that section says".

#### Type 0: explicit lsb, `pic_order_cnt_type` = 0

The slice header carries the low bits: `pic_order_cnt_lsb`, u(v) with `log2_max_pic_order_cnt_lsb_minus4 + 4` bits (SPS). `MaxPicOrderCntLsb` = 2 to the power of that width. The decoder reconstructs the **high part**, `PicOrderCntMsb`, by watching the lsb wrap around.

State: `prevPicOrderCntMsb` and `prevPicOrderCntLsb`, which are the values of the **previous reference picture** (`nal_ref_idc` not 0). Non-reference pictures neither read nor write them.

Steps for each picture:

1. If the picture is an **IDR** picture: `prevPicOrderCntMsb` = `prevPicOrderCntLsb` = 0.
   Otherwise, if the previous reference picture contained **MMCO 5**: `prevPicOrderCntMsb` = 0 and `prevPicOrderCntLsb` = the TopFieldOrderCnt that picture has **after the reset** of section 3.7 (0 if that picture was a bottom field). Otherwise use the stored values.
2. Let `lsb` = `pic_order_cnt_lsb` of this picture.
   - if `lsb` < `prevPicOrderCntLsb` and `prevPicOrderCntLsb` - `lsb` >= `MaxPicOrderCntLsb` / 2: `PicOrderCntMsb` = `prevPicOrderCntMsb` + `MaxPicOrderCntLsb` (the lsb wrapped upward)
   - else if `lsb` > `prevPicOrderCntLsb` and `lsb` - `prevPicOrderCntLsb` > `MaxPicOrderCntLsb` / 2: `PicOrderCntMsb` = `prevPicOrderCntMsb` - `MaxPicOrderCntLsb` (a picture from before the wrap, for example a B picture decoded after a P that already wrapped)
   - else `PicOrderCntMsb` = `prevPicOrderCntMsb`.
3. Frame: TopFieldOrderCnt = `PicOrderCntMsb` + `lsb`; BottomFieldOrderCnt = TopFieldOrderCnt + `delta_pic_order_cnt_bottom` (0 if the field is absent).
   (A top field has just the top value; a bottom field has BottomFieldOrderCnt = `PicOrderCntMsb` + `lsb`.)
4. If the picture is a reference picture, remember `PicOrderCntMsb` and `lsb` as the new `prevPicOrderCntMsb` / `prevPicOrderCntLsb` (or, with MMCO 5, 0 and the reset top value, as in step 1).

Requirement on the encoder, so that this works: two consecutive reference pictures must differ in POC by less than `MaxPicOrderCntLsb` / 2. The decoder cannot do anything if they do not.

#### Type 1: expected values from the SPS, plus deltas, `pic_order_cnt_type` = 1

The SPS holds a small table that describes the usual pattern, and the slice header only corrects it with `delta_pic_order_cnt[0]` (and `[1]`). Fields (3.1): `offset_for_non_ref_pic`, `offset_for_top_to_bottom_field`, `num_ref_frames_in_pic_order_cnt_cycle` (N), `offset_for_ref_frame[0..N-1]`, and `delta_pic_order_always_zero_flag` (if 1, the slice header has no deltas: both are 0).

This type works from the **frame number**, so it needs to unwrap `frame_num`, which wraps at `MaxFrameNum` = 2 to the power of `log2_max_frame_num_minus4 + 4`.

State: `prevFrameNum` and `prevFrameNumOffset` of the **previous picture** (reference or not).

Steps:

1. `FrameNumOffset`:
   - IDR picture: 0.
   - otherwise let `prevFrameNumOffset` be the stored one (0 if the previous picture contained MMCO 5). If `prevFrameNum` > `frame_num` (the number wrapped): `FrameNumOffset` = `prevFrameNumOffset` + `MaxFrameNum`; else `FrameNumOffset` = `prevFrameNumOffset`.
2. `absFrameNum` = `FrameNumOffset` + `frame_num` if N is not 0, else 0. If the picture is **non-reference** (`nal_ref_idc` = 0) and `absFrameNum` is above 0, subtract 1 (a non-reference picture shares the previous reference picture's slot).
3. `expectedDeltaPerPicOrderCntCycle` = sum of the N `offset_for_ref_frame` values.
4. If `absFrameNum` is above 0: `picOrderCntCycleCnt` = (`absFrameNum` - 1) / N (integer division), `frameNumInPicOrderCntCycle` = (`absFrameNum` - 1) mod N, and `expectedPicOrderCnt` = `picOrderCntCycleCnt` * `expectedDeltaPerPicOrderCntCycle` + the sum of `offset_for_ref_frame[0]` to `offset_for_ref_frame[frameNumInPicOrderCntCycle]`. If `absFrameNum` is 0: `expectedPicOrderCnt` = 0.
5. If the picture is non-reference: `expectedPicOrderCnt` += `offset_for_non_ref_pic`.
6. Frame: TopFieldOrderCnt = `expectedPicOrderCnt` + `delta_pic_order_cnt[0]`; BottomFieldOrderCnt = TopFieldOrderCnt + `offset_for_top_to_bottom_field` + `delta_pic_order_cnt[1]`.
   (A top field: top value as above. A bottom field: BottomFieldOrderCnt = `expectedPicOrderCnt` + `offset_for_top_to_bottom_field` + `delta_pic_order_cnt[0]`.)
7. Remember `frame_num` and `FrameNumOffset` as `prevFrameNum`, `prevFrameNumOffset`, for **every** picture (after MMCO 5: both 0).

Notes: with N = 0 the expected value is 0 and the POC is made of the deltas alone (a legal and odd case, covered by the variants). `delta_pic_order_always_zero_flag` = 1 means the order is fully determined by the SPS: the stream cannot have B pictures that come after their P picture in decode order unless `offset_for_non_ref_pic` is negative (that is what it is for: it places a non-reference picture just before the reference picture whose slot it shares).

#### Type 2: decode order is display order, `pic_order_cnt_type` = 2

No POC fields in the slice header at all. It is meant for streams without B pictures (and without any other reordering), so the order is just the decode order:

1. `FrameNumOffset` as in type 1.
2. IDR picture: `tempPicOrderCnt` = 0. Otherwise: 2 * (`FrameNumOffset` + `frame_num`), minus 1 if the picture is non-reference.
3. TopFieldOrderCnt = BottomFieldOrderCnt = `tempPicOrderCnt` (for a frame; a field gets the value for its own parity).
4. Remember `frame_num` and `FrameNumOffset` for every picture (0 and 0 after MMCO 5).

Two non-reference pictures in a row are not allowed in this type (they would get the same POC). So a non-reference picture is followed by a reference picture (or an IDR picture), often with the same `frame_num`: that is the case where only `nal_ref_idc` separates them (section 3.3). x264 writes this type when it is run with no B pictures (T1, T4, T5 here).

### 3.6 Frame_num gaps (note only)

`frame_num` normally goes up by one after each reference picture (and stays after a non-reference one). If the SPS has `gaps_in_frame_num_value_allowed_flag` = 1, a stream may skip numbers on purpose (temporal scalability, dropped frames); the decoder then invents "frame num gap" pictures that exist only in the reference buffer. That also advances the POC state for types 1 and 2. This book does not implement gaps (the test streams never use them); a decoder that meets a jump in `frame_num` that is not allowed should treat it as a lost picture. Phase 7 mentions it again with the reference buffer.

### 3.7 MMCO 5: a second kind of reset

A reference picture can carry the memory management command **5** in its marking list (3.3 parsed it). It means "forget every reference picture and renumber": almost an IDR in the middle of a stream, but the picture itself is an ordinary P or B picture. The decoder does these things **after decoding that picture**:

- `tempPicOrderCnt` = PicOrderCnt of that picture (the minimum for a frame); then TopFieldOrderCnt -= `tempPicOrderCnt`, BottomFieldOrderCnt -= `tempPicOrderCnt`. For a frame the picture's POC is now **0** (one of the two fields is 0; the other may be positive, for example top = 2 when bottom was lower by 2).
- The picture is treated as if its `frame_num` were 0. The next reference picture will have `frame_num` 1.
- The POC state for the **next** picture: type 0 uses `prevPicOrderCntMsb` = 0 and `prevPicOrderCntLsb` = the (reset) TopFieldOrderCnt of that picture; types 1 and 2 use `prevFrameNumOffset` = 0 and `prevFrameNum` = 0.

What the harness reports (decisions, so you are never guessing):

- The `poc`, `top` and `bottom` of the **PIC line** of an MMCO 5 picture are the values as **decoded, before the reset** (the numbers that 8.2.1 computes from its header).
- For display order, an MMCO 5 picture **starts a new period** exactly like an IDR picture, and sorts with POC **0**, its value after the reset. The `DISPLAY` line prints this 0. All pictures before it in decode order are displayed before it. (A conforming stream has no picture that follows it in decode order but precedes it in display order.)

MMCO 5 never appears in x264 output; it is covered by the synthetic variants (section 4.3).

### 3.8 From POC to display order

Within a period (an IDR picture or an MMCO 5 picture up to the next one), sort the pictures by PicOrderCnt, smallest first. Then concatenate the periods in the order they occur. There are no ties in a valid stream; the harness breaks a tie by decode index. This is a **simplification**: a real decoder outputs a picture as soon as the reordering rules (Annex C.4, "bumping") allow, and a stream can also drop pictures with `no_output_of_prior_pics_flag`. Phase 9 does the real thing. For all inputs here, sorting gives exactly what ffprobe reports.

### 3.9 Cross-check with the PTS

The Phase 1 timestamps are the container's version of the display order. For TS files written by ffmpeg, **one PES packet is one access unit, so packet number i is picture number i**; its PTS is the time at which that picture is shown. Therefore if you list the PTS of packet 0, 1, 2, ... **in your display order**, the list must be strictly increasing (and it must be exactly the list of PTS values that ffprobe attaches to the decoded frames). Your own POC sort and the container agree, or one of them is wrong. The harness does this check with ffprobe's numbers; the Verify bullet of the Summary asks you to do the same with your Phase 1 PTS list too, which is a good exercise (it also tests that Phase 1 gives you the same packet boundaries as this chapter).

## 4. The project: exactly what to build

### 4.1 Picture builder and POC

Write:

1. A **picture boundary detector** that takes the slice records from Chapter 3.3 (with the SPS and PPS that apply to each), in stream order, and also knows where AUD/SPS/PPS/SEI NAL units occurred between slices, and returns the list of pictures. Slices with `redundant_pic_cnt` above 0 are skipped.
2. A **POC calculator** for all three types, with the state of section 3.5 and the MMCO 5 handling of section 3.7. Frames only: if you meet `field_pic_flag` = 1, stop with a clear message (Phase 10).
3. A **display order** step: the sorted list of section 3.8.

Verify (the Summary's step): sort the pictures by POC and print their slice types; the sequence and the picture count match `ffprobe -show_frames` (the `pict_type` column); and ordering the pictures by your Phase 1 PTS gives the same order.

### 4.2 Dump program

Reads a `.ts` (or `.264`) with your earlier code, goes through the NAL units in stream order (counting **every** NAL unit, index 0 for the first, whatever its type), keeps the SPS and PPS tables and parses every slice header (Chapter 3.3), then prints the lines of section 5.

### 4.3 Test inputs

Use `T1` (all IDR, POC type 2, 50 pictures one after the other), `T5` (P only, POC type 2, one IDR), `T4` (four slices per picture, POC type 2), `T8` (B pictures, POC type 0), `T9_spatial` and `T9_temporal` (B pyramid: reference B pictures, so the reference/non-reference rules and the prev-reference state matter) and `T12` (interlaced MBAFF: frames, `frame_mbs_only_flag` = 0). T12's pictures are all frames, so it works with the frame POC rules of this chapter; its `field_pic_flag` is present and 0. Real PAFF files (field pictures) would be refused.

To cover what x264 cannot easily write:

    python3 test_poc.py --make-variants some_dir

| File | Content |
| --- | --- |
| `poc_variants.264` | 298 synthetic pictures in six sections, each with its own SPS and PPS(s). Real slice headers with random filler instead of macroblock data, so no decoder can show them; only the header logic is tested. All three POC types; `frame_num` wrapping (4-bit frame_num) and `pic_order_cnt_lsb` wrapping (5-bit lsb); consecutive pictures that differ in a **single** boundary field (non-reference B pictures in a row, non-reference then reference picture, two IDR pictures in a row); MMCO 5 pictures with a bottom delta of 0 and of -2; pictures with 1 to 3 slices whose `nal_ref_idc` values differ but are all non-zero; two PPSs, one with and one without the bottom delta fields; streams with and without AUD, with and without SEI; POC type 1 with a cycle of three, with `delta_pic_order_always_zero_flag` = 1, and with an empty cycle. |
| `poc_variants.264.expected` | The generator's own expected output (section 5 format). It computes the POC from the other direction: it picks the POC first and derives the lsb or deltas from it, so it is independent of the harness's implementation. The harness uses it as extra truth for this file. |
| `poc_idr_periods.ts` | x264 Main, an IDR every 10 pictures, B pyramid: the display lists of several periods are concatenated. |
| `poc_type2_slices.ts` | x264 Baseline without B pictures (x264 writes POC type 2), 3 slices per picture, an IDR every 12. |
| `poc_opengop.ts` | x264 Main with open GOP: I pictures that are not IDR, with B pictures that follow them in decode order but come before them in display order, across the I picture. POC is not reset there, and only your POC (not an IDR rule) can order them. |

x264 never writes POC type 1, MMCO 5 or field pictures; only the first variants file has types 1 and MMCO 5.

## 5. Harness input format

One text file, produced by your dump program, in two parts. Blank lines and lines starting with `#` are ignored. All fields are separated by single spaces, all numbers are decimal integers (minus sign if negative) except the slice type, which is a word.

### 5.1 PIC lines, one per primary coded picture, in decode order

    PIC <index> <idr> <nal_ref_idc> <type> <num_slices> <frame_num> <poc> <top> <bottom> <mmco5> <first_nal>

| Field | Meaning |
| --- | --- |
| `index` | 0, 1, 2, ... in order (decode index). |
| `idr` | 1 if the picture is an IDR picture (`nal_unit_type` 5), else 0. |
| `nal_ref_idc` | 0 to 3, of the picture's first slice. |
| `type` | `I`, `P`, `B`, `SP` or `SI`: `slice_type % 5` of the first slice. |
| `num_slices` | Number of primary slices (redundant slices are not counted). |
| `frame_num` | As coded in the first slice. |
| `poc` | PicOrderCnt of the frame: min(`top`, `bottom`). |
| `top`, `bottom` | TopFieldOrderCnt and BottomFieldOrderCnt of the frame. For an MMCO 5 picture the values **before** the reset (section 3.7). |
| `mmco5` | 1 if the picture's marking commands contain operation 5, else 0. |
| `first_nal` | Index of the picture's **first slice NAL unit** among all NAL units of the elementary stream, counted from 0 in stream order, whatever the NAL type. It does not point at the AUD or other NAL units in front of the slice. The harness takes the stream from `ffmpeg -c copy -f h264`, which is byte-identical to your Phase 1 `out.264` for a correct demuxer, and counts NAL units with its own start code scanner. |

Example from `T9_spatial` (a B pyramid; AUD, SPS, PPS, SEI come first, so the IDR slice is NAL unit 4):

    PIC 0 1 3 I 1 0 0 0 0 0 4
    PIC 1 0 2 P 1 1 6 6 6 0 6
    PIC 2 0 2 B 1 2 2 2 2 0 8
    PIC 3 0 0 B 1 3 4 4 4 0 10
    PIC 4 0 2 P 1 3 10 10 10 0 12
    PIC 5 0 0 B 1 4 8 8 8 0 14

(Picture 2 is a reference B: `nal_ref_idc` 2. Picture 3 is a non-reference B: same `frame_num` as the P that follows it.) Example with four slices per picture, from `T4`: `PIC 1 1 3 I 4 0 0 0 0 0 11`.

### 5.2 DISPLAY lines, one per picture, in display order

    DISPLAY <display_index> <pic_index> <poc> <type>

| Field | Meaning |
| --- | --- |
| `display_index` | 0, 1, 2, ... in order. |
| `pic_index` | The `index` of the PIC line of the picture shown at this position. |
| `poc` | The POC used for sorting: the picture's `poc`, except 0 for an MMCO 5 picture (section 3.7). |
| `type` | The picture's `type`. |

The list is sorted by `poc` inside each period (a period starts at an IDR picture or an MMCO 5 picture), periods concatenated in decode order. Example for the same file:

    DISPLAY 0 0 0 I
    DISPLAY 1 2 2 B
    DISPLAY 2 3 4 B
    DISPLAY 3 1 6 P
    DISPLAY 4 5 8 B
    DISPLAY 5 4 10 P

The `type` column read downwards is the sequence that the harness compares with ffprobe's `pict_type` (`IBBPBP...`); `pic_index` lets it name the exact picture that is out of place.

## 6. Running the harness

    python3 test_poc.py <input.ts|input.264> <your_pictures.txt>

What it does:

1. Runs `ffmpeg -c:v copy -bsf:v trace_headers` for the slice headers (and SPS/PPS as context), extracts the raw stream and scans its NAL units.
2. Runs its **own** implementation of section 3 (boundary rules including the NAL type rule, POC types 0/1/2 with all the state, MMCO 5, display order) on those headers. This is the main truth for the `PIC` and `DISPLAY` lines, compared field by field.
3. For real inputs (not the synthetic file), asks **ffprobe**: the number of frames must equal your picture count, the `pict_type` column in output order must equal the `type` column of your `DISPLAY` list, and the PTS list: the PTS of packet `pic_index` taken in your display order must increase strictly and must equal the PTS ffprobe attaches to the frames. One packet is one access unit, and the harness checks that the packet count equals your picture count.
4. If the harness's own truth and ffprobe disagree on a file (for instance a stream that starts in the middle of a GOP and has undecodable B pictures that ffmpeg drops), it stops with exit 2: there is no usable truth for that file and your result is not judged.

Exit codes: 0 pass, 1 mismatch (first 10 listed), 2 malformed reader output or unusable input (including field pictures).

    python3 test_poc.py --make-variants some_dir     # extra inputs of section 4.3
    python3 test_poc.py --self-test                  # checks the harness itself

`--self-test` writes the variants, builds known-good output from the harness truth for T1, T5, T4, T8, T9_spatial, T9_temporal, T12 and the four variant files (all must pass; it also checks that the generator's own expected output agrees with the harness's implementation and that POC types 0, 1, 2, multi-slice pictures and MMCO 5 are all present), and then corrupts that known-good output thirteen ways (wrong `poc`, `top`, `bottom`, `frame_num`, `num_slices`, a cleared `mmco5`, two pictures merged on T4 and on the variants, a lost picture, swapped display positions, display order equal to decode order, no `DISPLAY` section, a wrong type letter); each must fail.

## 7. Reading failures and debugging tips

Example:

    FAIL: PIC 50 (first slice NAL #144): poc: expected 32, got 33
             header fields of its first slice: pic_order_cnt_type 0, frame_num 7, nal_unit_type 1, nal_ref_idc 3, pps 1, pic_order_cnt_lsb 0, MaxPicOrderCntLsb 32, MaxFrameNum 16
    FAIL: PIC 3: picture boundary differs: expected a picture starting at NAL #25 with 4 slice(s), got one starting at NAL #25 with 8. You merged it with the next picture, which starts at NAL #32 because: idr_pic_id 1 -> 0. Later pictures cannot be paired and are not compared

The harness checks boundaries first (`first_nal` and `num_slices` of each picture) and stops comparing pictures at the first one where they differ, because after a missed or extra boundary the pictures cannot be paired. It says whether you merged or split a picture and names the reason the correct boundary is there (which of the nine values of section 3.3 changed, or a non-VCL NAL unit of section 3.2). The header-fields line under a value mismatch shows the values POC is computed from.

Common causes:

- **Fewer pictures than expected, two pictures merged:** a boundary field is missing from your comparison. Look at the reason printed: `idr_pic_id` (two IDR pictures in a row, T1), `nal_ref_idc` with one side 0, `pic_order_cnt_lsb` only (two non-reference B pictures), or `delta_pic_order_cnt[0]`.
- **More pictures, a picture split:** you compare `nal_ref_idc` for plain inequality (3 vs 2 is not a boundary), or you treat `first_mb_in_slice` = 0 as a start, or you compare a field that is allowed to differ between slices of one picture (`slice_type`, `first_mb_in_slice`, `slice_qp_delta`, the marking commands' presence...).
- **POC off by a multiple of `MaxPicOrderCntLsb`:** the wrap logic of step 2, type 0: the two comparisons have `>=` in the first and `>` in the second. A B picture decoded after a P that already wrapped must go **down** by one period.
- **POC wrong only for non-reference pictures:** you updated `prevPicOrderCntMsb/Lsb` for a non-reference picture (type 0 uses reference pictures only), or you forgot the minus 1 for non-reference pictures in types 1 and 2.
- **Types 1 or 2 wrong after the frame number wraps:** `FrameNumOffset` adds `MaxFrameNum` when `prevFrameNum` > `frame_num`. `prevFrameNum` is the previous picture's, **including** non-reference ones.
- **Wrong right after an IDR or MMCO 5:** IDR sets `FrameNumOffset` 0 and `prevPicOrderCntMsb/Lsb` 0; MMCO 5 sets the state as in section 3.7. Also: `top` and `bottom` are the **pre-reset** values in the PIC line, while `DISPLAY` uses 0 for that picture.
- **`bottom` wrong but `poc` right:** `poc` is the minimum of `top` and `bottom`, so an error in the larger of the two does not show in `poc`. Check `delta_pic_order_cnt_bottom` (type 0) or `offset_for_top_to_bottom_field` and `delta_pic_order_cnt[1]` (type 1).
- **Display order wrong at the start of a new period:** you sorted all pictures together instead of per IDR / MMCO 5 period (POC restarts at 0 in every period).
- **The PTS check fails but POC matches the harness:** packet i is not picture i. Your Phase 1 packetizing and this chapter disagree on what a picture is (for example, a PES packet holding two access units).
- **`first_nal` off:** count **all** NAL units from 0, including the AUD and the SPS, PPS and SEI before the first slice, and report the first **slice** NAL unit, not the AUD.

## 8. What's next, and a recap of Phase 3

**Phase 3 recap.** You now have the whole header layer. 3.1 parsed the SPS (picture size, `frame_num` and POC modes, ...), 3.2 the PPS (entropy coder, QP, weighted prediction flags, ...), 3.3 every slice header (references, weights, marking, QP, deblocking) and 3.4 turned slice headers into **pictures** with a decode order and a display order. Everything a decoder needs *before* touching a macroblock is in your hands: for each slice you know which picture it is in, what type it is, how its data is coded, where its data starts (the header end bit) and in what order the pictures come out. The POC values you computed will be used in Phase 7 (temporal direct and implicit weights) and in Phase 9 (output order).

**Phase 4** goes below the slice header into the **slice data**: CAVLC macroblock parsing (7.3.4, 7.3.5, 9.2). You read `mb_type`, the intra prediction modes, `coded_block_pattern`, `mb_qp_delta` and the residual blocks (`coeff_token`, trailing ones, levels, `total_zeros`, `run_before`) of every I slice of T1 and T2, down to the last coefficient. There are no pixels yet; the strongest check will be that the read position of every slice lands exactly on the `rbsp_trailing_bits` after the last macroblock.
