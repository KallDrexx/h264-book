# Chapter 7.2 - Decoded picture buffer and basic P reference lists

> **SPOILER WARNING: do not open the `spoiler_reference/` folder until your own implementation passes.**
> `spoiler_reference/dpb_ref.py` is a complete solution of this chapter (the model the harness compares you with), and
> `spoiler_reference/dpbgen.py` is the synthetic-stream generator. Everything you need is in this Readme. `test_dpb.py` itself is
> only plumbing (ffmpeg logs, file formats, comparison) and is safe to read.

## 1. Goal and the project

Chapter 7.1 taught your parser to read P slices, including `ref_idx_l0`: a number that says "copy from reference picture number
*k*". This chapter explains what "reference picture number *k*" means. You build the **decoded picture buffer** (DPB): the set of earlier pictures
the decoder keeps because later pictures may predict from them, together with the rules that

* decide which pictures stay in it (**reference picture marking**, with the **sliding window**),
* give every kept picture a number (**frame_num**, **FrameNumWrap**, **PicNum**), and
* turn the set into the ordered **reference picture list** that `ref_idx_l0` indexes (the **default P list**).

You do not decode pixels yet (motion vectors come in 7.3, interpolation in 7.4). The output is a text log that records, for every P slice, which
decoded picture each `ref_idx` points to, and for every picture, the reference set after its marking. The harness checks it against an
independent implementation of the spec rules, which is itself checked against ffmpeg as far as ffmpeg's output allows (section 6).

## 2. Big picture

A P picture is built from pieces of earlier pictures. The decoder therefore has to **remember** some of the pictures it has
decoded. The memory is the DPB. Three questions follow, and the bitstream answers all of them with a few header fields:

1. **Which pictures may be used for prediction?** Each picture says so itself: `nal_ref_idc` in its NAL header is 0 for a
   **non-reference picture** (nobody will ever predict from it) and non-zero for a **reference picture**. Pictures that are not references can be
   thrown away as soon as they have been shown.
2. **How many can be remembered?** The SPS says: `max_num_ref_frames`. When a new reference picture arrives and the buffer is full, the oldest one is
   forgotten. That automatic rule is the **sliding window**. (Chapter 7.6 adds explicit commands, MMCO, that override it.)
3. **How does a slice name a picture?** Not by a global number but by position in a list: `ref_idx_l0` = 0 is the first entry, 1 the second. The list is rebuilt for
   every slice by a fixed rule: most recently decoded first. An encoder that wants a different order says so in the slice header
   (`ref_pic_list_modification`, Chapter 7.6). Without that, `ref_idx` 0 is "the previous reference picture", 1 is the one before it, and so on.

Picture identity is `frame_num`, a counter in every slice header that goes up by one after each reference picture and wraps around at
`MaxFrameNum`. The wrap-around is the only subtle part: "the previous picture" must still sort first when the counter has restarted from 0.

Timeline of one picture:

```
slice 0 header -> build list0 for slice 0 (from the DPB as it is NOW) -> decode slice 0 macroblocks
slice 1 header -> build list0 for slice 1 (same DPB)                  -> decode slice 1 macroblocks
... all slices decoded (and deblocked) ...
marking process for the picture: sliding window / IDR clearing, then the picture itself becomes a reference
next picture ...
```

Lists are built **per slice, before the macroblocks**; marking happens **once per picture, after it is completely decoded**. The picture being decoded is never in
its own list.

## 3. Concepts in detail

### 3.1 What the DPB holds

A **frame store** is a decoded frame (luma + chroma, **after deblocking**: references are the final, filtered pictures) plus bookkeeping. Per frame the
bookkeeping you need in this chapter is: its `frame_num`, its marking (**short-term reference** or **unused for reference**) and, from 9.1 on, whether it is still waiting to be output.

Reference marking has three states (8.2.5): *unused for reference*, *used for short-term reference*, *used for long-term reference*. Long-term references and
the commands that create them are Chapter 7.6; **this chapter handles only short-term references**, and the harness refuses files that use long-term ones.

Capacity. A DPB holds at most `max_num_ref_frames` reference frames (SPS, 7.4.2.1.1; "frames" because with field coding two fields share one slot).
Two related limits are **not** this chapter: the level's DPB size and VUI `max_dec_frame_buffering` describe how many frames the decoder
may hold *including pictures waiting to be output*. They matter for output order, which is Chapter 9.1. For now, **P-only streams are written to the
output file immediately, in decoding order, which is also display order** (with `pic_order_cnt_type` 2 and no B pictures nothing is ever reordered). A simple
design is a pool of frame buffers with a "still needed" flag per buffer (reference, or waiting for output); a buffer returns to the pool when neither holds.

### 3.2 frame_num (7.4.3)

`frame_num` is `u(v)` in every slice header, with `log2_max_frame_num_minus4 + 4` bits. Define
`MaxFrameNum = 2^(log2_max_frame_num_minus4 + 4)` (16 to 65536). All slices of a picture carry the same value. The rules for a frame stream:

| picture | frame_num |
|---|---|
| IDR | 0 |
| any other picture | `(PrevRefFrameNum + 1) % MaxFrameNum` |

`PrevRefFrameNum` is the `frame_num` of the **previous reference picture** in decoding order (0 after an IDR). Consequences:

* the counter advances **only after reference pictures**. A non-reference picture has `PrevRefFrameNum + 1` but does not change `PrevRefFrameNum`; the next
  picture (reference or not) has the same `frame_num` again. Two consecutive pictures can therefore share a `frame_num`, and then the first is a non-reference picture;
* the counter wraps: with `MaxFrameNum` = 16 the sequence of reference pictures has `frame_num` 0, 1, ..., 15, 0, 1, ... (x264 uses 16 here: T6 wraps after 16 pictures, so T5 and T6 already exercise the wrap-around);
* if a picture's `frame_num` is neither `PrevRefFrameNum` nor `PrevRefFrameNum + 1`, pictures are missing: a **gap** (allowed only when the SPS says
  `gaps_in_frame_num_value_allowed_flag`; error concealment or temporal scalability use it). Gap handling is Chapter 7.6 (8.2.5.2); the harness refuses such files.

Use Chapter 3.4's picture grouping; the picture index used throughout this chapter is its 0-based decoding-order index over **all** coded pictures.

### 3.3 FrameNumWrap and PicNum (8.2.4.1)

The list must order references by "how recently decoded". `frame_num` alone cannot do that after a wrap: picture 17 has `frame_num` 1 and picture 15 has 15.
So, **for each slice**, with `frame_num` = the current slice's value, every short-term reference frame gets

```
FrameNumWrap = FrameNum - MaxFrameNum      if FrameNum > frame_num      (it was decoded before the last wrap)
FrameNumWrap = FrameNum                    otherwise
PicNum       = FrameNumWrap                (for frames; fields are Phase 10)
```

where `FrameNum` is the reference's own `frame_num`. `PicNum` can be negative. Larger PicNum = decoded more recently. Worked example (T6, log2_max_frame_num = 4, so
MaxFrameNum = 16): picture 17 has `frame_num` 1 and four references.

| picture | FrameNum | > 1? | FrameNumWrap = PicNum |
|---|---|---|---|
| 16 | 0 | no | 0 |
| 15 | 15 | yes | -1 |
| 14 | 14 | yes | -2 |
| 13 | 13 | yes | -3 |

Descending PicNum gives 16, 15, 14, 13: the correct decoding-order sequence. Without the subtraction it would be 15, 14, 13, 16.

### 3.4 Marking: the sliding window (8.2.5.3) and IDR (8.2.5.1)

Marking runs **after a reference picture (`nal_ref_idc != 0`) has been decoded**, once per picture:

1. **IDR picture** (`nal_unit_type` 5): all existing references are marked *unused*. Then the IDR picture itself becomes a short-term reference
   (when `long_term_reference_flag` is 0; 1 means long-term, Chapter 7.6). The IDR slice header has `no_output_of_prior_pics_flag` (9.1) and `long_term_reference_flag` instead of the next case's flag.
2. **Other reference picture, `adaptive_ref_pic_marking_mode_flag` = 0** (the sliding window; flag = 1 means MMCO commands, Chapter 7.6):
   let `numShortTerm` and `numLongTerm` be the numbers of short-term and long-term reference frames in the DPB, not counting the
   current picture (`numLongTerm` is always 0 in this chapter). If
   `numShortTerm + numLongTerm == Max(max_num_ref_frames, 1)`, the short-term frame with the **smallest FrameNumWrap** is marked unused. (FrameNumWrap is computed relative to the
   current picture's `frame_num`, exactly as in 3.3.) Then the current picture is marked short-term.
3. **Non-reference picture** (`nal_ref_idc == 0`): nothing is marked and nothing is removed. It is decoded, output, and never enters the reference set. Its `frame_num` does not
   count anywhere.

So the reference set always has at most `max_num_ref_frames` entries once the current picture is added; the oldest by FrameNumWrap makes room. Careful points:
the *smallest FrameNumWrap*, not the smallest raw `frame_num` (they differ across the wrap), and the window slides only on **reference** pictures.
Marking uses the state **before** the current picture was added, which is why a full buffer of `N` frames contains `N` frames after the new one arrives and not `N+1`.

### 3.5 The default P reference list (8.2.4.2.1)

For each P slice, "initialisation" produces `RefPicList0`:

1. Take all short-term reference frames of the DPB (state after the previous picture's marking).
2. Sort them by **descending PicNum** (3.3).
3. The slice's number of active entries is `num_ref_idx_l0_active_minus1 + 1` (where that value comes from is explained below). **If the list is longer, drop the extra entries at the end** (8.2.4.2).
4. **If the list is shorter**, the missing entries are **"no reference picture"**. They are *not* filled by repeating anything.
   A stream may *declare* more active entries than references exist (in the first P pictures after an IDR, or more than
   `max_num_ref_frames`), but a conforming stream never selects such an entry with `ref_idx_l0`. Treat a `ref_idx` that does as
   a stream error. (ffmpeg prints "Missing reference picture, default is N" for each such entry and substitutes a picture.)

(Then, in Chapter 7.6, `ref_pic_list_modification` may permute the list. Not here: a P slice with `ref_pic_list_modification_flag_l0` = 1 is refused by the harness.)

`num_ref_idx_l0_active_minus1` comes from the PPS default `num_ref_idx_l0_default_active_minus1`, unless the slice header has
`num_ref_idx_active_override_flag` = 1, in which case the slice's own `num_ref_idx_l0_active_minus1` (ue(v)) replaces it. It is **per slice**: two slices of the same picture may have lists of different length (the lists
themselves are the same order, truncated differently). This number also fixes the range of `te(v)` for `ref_idx_l0` (Chapter 7.1).

### 3.6 Putting it together: a trace

A small case, `max_num_ref_frames` = 2, three reference P pictures after an IDR (picture indices 0..3):

| after decoding picture | sliding window | DPB (index:frame_num, newest first) | list0 of the next P slice (active 2) |
|---|---|---|---|
| 0 (IDR, fn 0) | clear, add | 0:0 | 0, - (no reference picture) |
| 1 (fn 1) | 1 ref < 2, add | 1:1, 0:0 | 1, 0 |
| 2 (fn 2) | 2 refs = 2: remove smallest FrameNumWrap (picture 0), add | 2:2, 1:1 | 2, 1 |
| 3 (fn 3) | remove picture 1, add | 3:3, 2:2 | 3, 2 |

If picture 2 were a non-reference picture it would use list `1, 0` itself, but afterwards the DPB would still be `1:1, 0:0`, and picture 3 would have `frame_num` 2.

## 4. The project: exactly what to build

Extend your decoder (or the reader built in Chapter 7.1) with:

1. **A DPB** of frame stores with `frame_num` and marking state; a frame store for the picture being decoded.
2. **frame_num bookkeeping**: `MaxFrameNum` from the SPS, `FrameNumWrap` and `PicNum` computed per slice (3.3).
3. **The marking process** run when a picture is complete: IDR clearing and the sliding window (3.4). Non-reference pictures leave the reference set alone.
4. **Default P list construction** per P slice, with truncation / "no reference picture" entries (3.5).
5. **Logging** as defined in section 5: for every P slice, the picture each `ref_idx` entry points to; for every picture, the DPB after marking.
6. Make `ref_idx_l0` usable: from Chapter 7.4 on, `RefPicList0[ref_idx]` is the frame you read pixels from.

Inputs: any file that uses only frame pictures, short-term references, the sliding window and no list modification.
Files containing B pictures work too: log the P slices and, for **every** picture (I, P and B), the DPB line; B slices produce no `SLICE` line.

**Use T5, T6 and the `--make-variants` files (section 6) for this chapter. T7 and T8 are refused** (exit 2, "ref_pic_list_modification in a P slice"):
their recipes use x264's weighted P prediction (`weightp=2` in T7, and x264's Main-profile default in T8), and x264 implements it by
listing one reference twice with different weights, which needs `ref_pic_list_modification` (Chapter 7.6). T7 is used again in 7.7, T8 in 7.8.
The B-picture variant below sets `weightp=0` for exactly this reason. In general the harness refuses (exit 2) any input that uses
MMCO (`adaptive_ref_pic_marking_mode_flag` = 1), long-term references, frame_num gaps, list modification in a P slice, field pictures or SP/SI slices.

**Verify** (the Summary's check): in T5 (one reference) every `list0` is the previous picture; in T6 the order is descending PicNum with the wrap; the harness confirms all of that and the DPB sets.

## 5. Harness input format

One text file. Blank lines and lines starting with `#` are ignored. Two line types; the lines may come in any order (decoding
order is natural). Fields are separated by single spaces, all numbers are decimal, list entries are separated by commas with no spaces.

```
SLICE <pic> <slice> frame_num=<n> active=<n> list0=<slot>,<slot>,...      one line per P slice
DPB <pic> refs=<ref>,<ref>,...                                             one line per picture
```

A `<slot>` is `<pic>:<PicNum>` or `-`; a `<ref>` is `<pic>:<frame_num>`. Note the difference: list slots carry the **PicNum**
(relative to the current slice, may be negative), DPB entries the reference's own **frame_num**.

| field | meaning |
|---|---|
| `<pic>` | 0-based decoding-order index of a coded picture (counting all pictures: I, P, B, reference or not; Chapter 3.4's grouping) |
| `<slice>` | 0-based index of the slice inside the picture, in stream order (counting all slices of the picture, so a B or I slice in a mixed picture still uses up an index) |
| `frame_num=` | the slice header's `frame_num` |
| `active=` | `num_ref_idx_l0_active_minus1 + 1` after the override rule |
| `list0=` | exactly `active` slots of `RefPicList0`, from `ref_idx` 0 up: `<pic>:<PicNum>` of the referenced picture, or `-` for "no reference picture" |
| `refs=` (DPB line) | the short-term reference set **after the marking process of picture `<pic>`**, newest decoded picture first, as `<pic>:<frame_num>`; `refs=-` if the set is empty |

Rules: one `SLICE` line for **every P slice** (slice_type 0 or 5) and none for I or B slices; one `DPB` line for **every picture**. For a non-reference picture the `DPB` line shows the unchanged set (it does not contain the picture itself).
For a reference picture the set includes the picture itself. The `DPB` line of an IDR picture shows only the IDR picture.

Example (T6, the beginning and the wrap; a real run produces 99 lines):

```
DPB 0 refs=0:0
SLICE 1 0 frame_num=1 active=1 list0=0:0
DPB 1 refs=1:1,0:0
SLICE 2 0 frame_num=2 active=2 list0=1:1,0:0
...
SLICE 17 0 frame_num=1 active=4 list0=16:0,15:-1,14:-2,13:-3
DPB 17 refs=17:1,16:0,15:15,14:14
```

And the start of `synth_dpb_ref1.264` (`max_num_ref_frames` = 1; picture 1 is a non-reference picture, so picture 2 reuses `frame_num` 1;
slices that declare more active entries than references exist get `-`):

```
DPB 0 refs=0:0
SLICE 1 0 frame_num=1 active=2 list0=0:0,-
DPB 1 refs=0:0
SLICE 2 0 frame_num=1 active=1 list0=0:0
DPB 2 refs=2:1
```

`python3 test_dpb.py --print-expected T6.ts expected.txt` writes the expected file for any input, to `diff` against yours.

## 6. Running the harness

Run from the chapter folder (the first command builds the standard inputs into the repository's `.cache/`):

```
(cd .. && python3 -m common.testfiles T5 T6)
python3 test_dpb.py ../.cache/T5.ts my_log.txt
python3 test_dpb.py ../.cache/T6.ts my_log.txt
python3 test_dpb.py --make-variants /tmp/dpb_inputs         # more inputs: see below
python3 test_dpb.py /tmp/dpb_inputs/synth_dpb_wrap.264 my_log.txt
python3 test_dpb.py --self-test
```

Exit 0 pass, 1 your log differs from the truth, 2 your file is missing/malformed, the **input** is unsupported (section 4: this includes T7 and T8), or the reference model disagrees with ffmpeg (a harness problem).

`--make-variants` writes (besides copies of T5 and T6):

| file | what it exercises |
|---|---|
| `x264_ref16.ts` | 16 reference frames: the window slides at 16, and wraps (`MaxFrameNum` 16) with a full buffer |
| `x264_idr10.ts` | IDR every 10 pictures, `ref=4`: references cleared mid-stream, `frame_num` restarts |
| `x264_ref1_slices.ts` | one reference, three slices per picture |
| `x264_nonref_b.ts` | Main profile with B pictures (`nal_ref_idc` 0, 31 of 50 pictures) and `weightp=0`: non-reference pictures; P lists unaffected |
| `synth_dpb_wrap.264` | `log2_max_frame_num` 4, 45 pictures, 4 references, two slices with different `num_ref_idx_active`, active larger than the references that exist |
| `synth_dpb_nonref.264` | non-reference P pictures between reference ones (the next picture reuses their `frame_num`) |
| `synth_dpb_idr.264` | IDR pictures in the middle, two in a row (different `idr_pic_id`), 2 references |
| `synth_dpb_ref8.264` | 8 references and up to 9 active entries across the wrap |
| `synth_dpb_ref1.264` | `max_num_ref_frames` = 1, IDR and non-reference pictures |

The synthetic streams are tiny 96x80 pictures written by a bit-writer; their P macroblocks all have motion vector 0 and no residual, so each one is an exact copy of the
picture its `ref_idx` names. That is what lets ffmpeg's *pixels* confirm the list.

### What is compared, and what confirms the truth

The harness compares your `SLICE` and `DPB` lines **exactly** with the output of its reference model (rules 3.3 to 3.5 run on the slice-header values
that ffmpeg's `trace_headers` prints). Before comparing, it checks the model against what ffmpeg can show; the table says which parts ffmpeg confirms and which rest on the model alone:

| claim | confirmed by | on which inputs |
|---|---|---|
| the DPB content after each reference picture (sliding window, IDR, non-reference pictures not stored, wrap-around) | ffmpeg `-debug mmco`: the "short term list" printed after each marking process; the harness compares its `frame_num` sequence, newest first (frame_nums in the set are distinct, so this pins down the pictures) | **all** inputs (non-reference pictures print nothing; their `DPB` line is the previous set) |
| the P list order, PicNum rule, truncation | decoded pixels of the synthetic streams: every inter macroblock must equal `RefPicList0[ref_idx]` of the model | synthetic streams only |
| number of "no reference picture" entries | the number of "Missing reference picture" messages ffmpeg prints | synthetic streams only |
| the PicNum *values* in your `list0=` | nothing in ffmpeg shows them; they follow by the formula from the verified DPB and `frame_num` | the model only |
| the `frame_num=` and `active=` values | read from ffmpeg's `trace_headers` (PPS default or slice override), not checked separately | indirectly, on synthetic streams: the pixels and the "missing" count depend on `active` |

So for real x264 files ffmpeg verifies the DPB, and the list is a deterministic function of the DPB and `frame_num`; the ordering rule itself is tested against ffmpeg's actual
prediction in the synthetic streams. ffmpeg does not print its internal ref lists, and its `export_mvs` data contains no reference index per vector.

## 7. Reading failures / debugging tips

Messages name the line (`SLICE 18 0` = picture 18, slice 0), the expected and received text and often a hint. They are listed in decoding order, so the first message is the first thing that went wrong (a wrong `DPB` line usually breaks the next picture's `SLICE` lines too: fix the `DPB` line first).

* **Same pictures, wrong PicNum, only after about picture 16**: you do not apply the FrameNumWrap subtraction, or you use a different `MaxFrameNum` (remember `+ 4`).
* **List order reversed**: ascending instead of descending PicNum.
* **`DPB` has too many entries**: sliding window missing or fired late (the test is `==` against `Max(max_num_ref_frames, 1)` *before* the current picture is added); an IDR did not clear the set; a non-reference picture was stored.
* **`DPB` has too few entries / the newest is missing**: window fired before the picture was decoded; the IDR itself not stored.
* **`DPB` has the right size but a different member**: the sliding window removed the wrong picture; it must remove the smallest FrameNumWrap, not the smallest raw `frame_num` and not the newest.
* **Right pictures but different order in `DPB`**: the line lists the newest decoded picture first (descending decoding index).
* **Lists wrong only in the first P pictures**: missing entries must be `-`, not a repeat of an existing one; the list is truncated or short, not padded.
* **Lists wrong only in multi-slice pictures**: the list is per slice; `active` may differ between slices. Also check that marking happens once per picture and not per slice, and that the lists of all slices see the same DPB.
* **Everything is off by the non-reference pictures** (B-picture files): `frame_num` does not advance for them, and a non-reference picture does not slide the window.
* **A line is missing**: every P slice needs a `SLICE`, every picture a `DPB` (including B and I pictures).
* Write your own expected file with `--print-expected` and `diff` it with yours: the first difference is nearly always the bug.

### Harness self-test

`python3 test_dpb.py --self-test` generates inputs (T5, T6, four x264 variants, five synthetic streams), validates the reference against ffmpeg, checks that a known-good log passes, that
corrupted logs fail (a dropped line, swapped entries, a changed PicNum, shifted picture numbers, a missing file, a malformed file) and that **14 deliberately wrong rules** each make the
comparison fail on at least one input: no FrameNumWrap, wrong MaxFrameNum, sliding window removing the newest, removing the smallest raw `frame_num`, window firing late, ascending list order,
references not cleared at an IDR, IDR not stored, non-reference pictures stored, truncation one too long / one too short, short lists padded by repetition, marking before the lists are built, current picture in its own list. For each
it also prints which of ffmpeg's own checks (DPB log or pixels and "missing" messages) would expose the wrong rule; e.g. the ordering and truncation errors are invisible to the DPB log and need the synthetic pixels.

## 8. What's next

Chapter 7.3 predicts motion vectors from neighbouring partitions; the `ref_idx` you can now resolve decides which neighbours' vectors count as "the same reference". Chapter 7.6 returns to this DPB to add `ref_pic_list_modification`, MMCO, long-term references and frame_num gaps.
