# Chapter 7.6 - Full reference management

> **SPOILER WARNING: do not open the `spoiler_reference/` folder until your own implementation passes.**
> `spoiler_reference/refmgmt_ref.py` is a complete solution of this chapter (the model the harness compares you with) and
> `spoiler_reference/refmgmtgen.py` is the synthetic-stream generator. Everything you need is in this Readme. `test_refmgmt.py` itself is
> only plumbing (ffmpeg logs, file formats, comparison) and is safe to read.

## 1. Goal and the project

Chapter 7.2 gave your decoder a DPB with one marking rule (the sliding window) and one list order (newest first). Real encoders and real
streams need more, and this chapter adds all of it:

* **ref_pic_list_modification**: a slice header can reorder `RefPicList0`, so that `ref_idx` 0, 1, 2 name the pictures the encoder
  chooses, not the newest ones. x264's weighted prediction (T7) uses it to list one picture several times.
* **Long-term references**: pictures that stay in the DPB until told otherwise, outside the sliding window.
* **Adaptive marking (MMCO 1 to 6)**: explicit commands that unmark pictures, convert pictures to long-term, and so on. x264's B-pyramid
  uses MMCO 1.
* **IDR handling** with `long_term_reference_flag`, and the limit on the total number of reference frames.
* **frame_num gaps** (8.2.5.2): "non-existing" frames that keep the sliding window in step when pictures were deliberately left out.

You extend the log of Chapter 7.2 with the list after modification and with the long-term part of the DPB. The harness checks it against an
independent implementation of the spec rules, which is itself checked against ffmpeg's `-debug mmco` output and, on synthetic streams, against
ffmpeg's decoded pixels (section 6).

**After this chapter the P-only decoder is complete for CAVLC Baseline** (frames, any number of references, any list order, long-term
references): every in-scope Baseline stream (no FMO/ASO) decodes exactly. The one P-picture feature still missing is *weighted prediction* (7.7, Main profile).
B pictures (7.8, 7.9) and everything else of Main/High come later.

## 2. Big picture

Three small ideas:

1. **Numbering.** A slice header cannot say "use picture 17"; it says "the picture whose number is N" and the *current* picture's
   `frame_num` defines the numbers: `PicNum` for short-term pictures (as in 7.2), `LongTermPicNum` for long-term ones. Commands are
   expressed as small differences from the current picture's number.
2. **Two kinds of memory.** A *short-term* reference is forgotten automatically by the sliding window. A *long-term* reference is kept until an
   explicit MMCO command unmarks it, so an encoder can pin a good background or a recovery picture for as long as it likes.
3. **Two places for commands.** Commands that change the *order of one slice's list* live in that slice's header (ref_pic_list_modification;
   each slice of a picture may differ). Commands that change *which pictures are kept* live in `dec_ref_pic_marking` (all slices of a picture carry the
   same one) and run once, after the picture is decoded.

The order of operations for one picture, which you must follow exactly:

```
(frame_num gap?  insert non-existing frames, each with a sliding window step)         8.2.5.2
slice 0: initial list0  ->  apply this slice's modification commands  ->  decode its macroblocks
slice 1: initial list0  ->  apply this slice's modification commands  ->  decode its macroblocks   (same DPB)
...
all slices done (and deblocked):  marking process, using the FIRST slice's dec_ref_pic_marking
    IDR: clear everything, current = short-term (or long-term idx 0)
    else sliding window (flag 0) or the MMCO list (flag 1)
    then the current picture is added to the DPB (short-term unless MMCO 6 / IDR long-term made it long-term)
```

## 3. Concepts in detail

### 3.1 Numbers (8.2.4.1)

For a **frame** stream (fields: Phase 10), with `frame_num` the current slice's value:

| name | value |
|---|---|
| `MaxFrameNum` | `2^(log2_max_frame_num_minus4 + 4)` |
| `MaxPicNum` | `MaxFrameNum` |
| `CurrPicNum` | `frame_num` of the current picture |
| `FrameNumWrap` of a short-term frame | `FrameNum - MaxFrameNum` if `FrameNum > frame_num`, else `FrameNum` |
| `PicNum` of a short-term frame | `FrameNumWrap` (can be negative) |
| `LongTermPicNum` of a long-term frame | `LongTermFrameIdx`, the index it was given by MMCO 3, MMCO 6 or an IDR |

A frame is exactly one of: unused for reference (not in the DPB any more), short-term, long-term. State kept besides the frames:
`MaxLongTermFrameIdx` (the largest index a long-term frame may get, or **none** = "no long-term frame indices"; **none** at the start and after an IDR without
the long-term flag) and `PrevRefFrameNum`.

### 3.2 The initial P list with long-term frames (8.2.4.2.1)

1. All short-term frames, sorted by **descending PicNum** (3.3 of Chapter 7.2).
2. Then all long-term frames, sorted by **ascending LongTermPicNum**.
3. Cut to `num_ref_idx_l0_active_minus1 + 1` entries if longer; entries beyond the end stay "no reference picture" (7.2).

Non-existing frames (3.8) are short-term frames like the others and stand in the list at their PicNum place.

### 3.3 ref_pic_list_modification (8.2.4.3)

Syntax in the slice header, P slices only here (after `num_ref_idx_active_override`):

```
ref_pic_list_modification_flag_l0   u(1)
if 1:  repeat:
         modification_of_pic_nums_idc    ue(v)     0, 1, 2 or 3
         idc 0 or 1:  abs_diff_pic_num_minus1   ue(v)
         idc 2:       long_term_pic_num         ue(v)
       until idc == 3   (3 only terminates; it carries no value)
```

At most `num_ref_idx_active` commands (not counting the final 3). Ffmpeg's trace prints the elements in order; the Chapter 3.3 parser already reads them.

**Process.** Start from the initial list, cut to `active` entries and padded with "no reference picture". Work on a list that is *temporarily one entry longer*
(`active + 1`, indices 0..active). Keep `refIdx` (the next position to fill, starts at 0) and `picNumLXPred` (starts at `CurrPicNum`).

*idc 0 or 1 (short-term picture):*

```
abs_diff = abs_diff_pic_num_minus1 + 1                       (1 .. MaxPicNum)
idc 0:  picNumNoWrap = pred - abs_diff,  if that is < 0 add MaxPicNum
idc 1:  picNumNoWrap = pred + abs_diff,  if that is >= MaxPicNum subtract MaxPicNum
pred = picNumNoWrap                                          (the "no wrap" value becomes the next predictor)
picNum = picNumNoWrap - MaxPicNum   if picNumNoWrap > CurrPicNum,  else picNumNoWrap
target = the short-term frame whose PicNum equals picNum
```

*idc 2 (long-term picture):* target = the long-term frame whose `LongTermPicNum` equals `long_term_pic_num`. `picNumLXPred` is not touched.

*Insertion (both cases):*

```
shift entries refIdx .. active-1 up by one (to refIdx+1 .. active)
list[refIdx] = target;  refIdx = refIdx + 1
copy the entries from refIdx .. active down, skipping every entry that is the target picture  (there is at most one such later copy)
```

After the last command the list is **cut back to `active` entries** (the extra position is dropped).

Points that cause most bugs:

* Both wraps are needed: a picture with a negative PicNum (decoded before the `frame_num` wrap) is reached by subtracting past 0 and adding `MaxPicNum`.
  The spec keeps the wrapped `picNumNoWrap` (in 0 .. MaxPicNum-1) as the predictor, not the possibly negative `picNum`; do the same.
* `abs_diff = MaxPicNum` is legal (minus1 = `MaxPicNum - 1`) and means "the same picture again": the predictor does not move. An encoder uses it to put one
  picture at *several* indices. The duplicate removal only looks at entries *after* the freshly inserted one, so the earlier copies stay (see 3.9).
* Duplicate removal compares *pictures*: a short-term entry matches only a short-term target, a long-term entry only a long-term target (the spec's `PicNumF` / `LongTermPicNumF`).
* Pictures not in the (cut) initial list can be targets: they are taken from the DPB and everything shifts, the last entry falls off.
* Commands refer to pictures that exist; a target that does not exist makes the stream non-conforming.

Worked example, T7's third picture (`frame_num` 2, `MaxFrameNum` 16, `active` 4, short-term PicNums 1 and 0 exist). Commands (idc, minus1): (0,0) (0,15) (0,15) (0,0).
Initial list `[1, 0, -, -]` (PicNum or `-`), plus the temporary fifth entry `-`.

| command | pred before -> NoWrap | picNum | insert at | list after (5 entries) |
|---|---|---|---|---|
| (0,0): diff 1 | 2 -> 1 | 1 | 0 | `[1, 0, -, -, -]` (the later copy of 1 is removed) |
| (0,15): diff 16 | 1 -> -15 -> 1 | 1 | 1 | `[1, 1, 0, -, -]` |
| (0,15): diff 16 | 1 -> 1 | 1 | 2 | `[1, 1, 1, 0, -]` |
| (0,0): diff 1 | 1 -> 0 | 0 | 3 | `[1, 1, 1, 0, -]` (the later 0 at index 4 removed) |

Cut to 4 entries: `RefPicList0 = [1, 1, 1, 0]`. This is line `SLICE 2 0 ... list0=1:1,1:1,1:1,0:0` of T7's expected log.

### 3.4 The reference count limit

After marking, the DPB holds `numShortTerm + numLongTerm` frames, **including the current picture**, and that number must not exceed
`Max(max_num_ref_frames, 1)`. With the sliding window this is automatic. With MMCO the encoder must make room itself (unmark something); a stream that does not is
**non-conforming**. Treat it as an error (the harness exits 2 on such input). Decoders that want to survive broken streams (Chapter 9.5) typically
drop the oldest short-term frame instead.

### 3.5 Marking (8.2.5)

Run once per reference picture (`nal_ref_idc != 0`), after it is decoded, with the first slice's `dec_ref_pic_marking`. Non-reference pictures change nothing.

**IDR picture.** All frames unused; `MaxLongTermFrameIdx` = none. Then the header says: `long_term_reference_flag` = 0: the IDR is short-term. = 1: it is
long-term with `LongTermFrameIdx` 0 and `MaxLongTermFrameIdx` = 0. (`no_output_of_prior_pics_flag` is output business, Chapter 9.1.)

**Non-IDR, `adaptive_ref_pic_marking_mode_flag` = 0: sliding window.** If `numShortTerm + numLongTerm == Max(max_num_ref_frames, 1)`, remove the short-term frame with the smallest
`FrameNumWrap`. *Long-term frames count toward the limit but are never removed by the window*: a stream that keeps many long-term frames leaves the window fewer
short-term slots (and if there is no short-term frame to remove, the stream is non-conforming). Then add the current picture as short-term.

**Non-IDR, `adaptive_ref_pic_marking_mode_flag` = 1: MMCO.** No sliding window. A list of operations (`memory_management_control_operation`, ue(v), 0 ends the list), executed in order:

| op | arguments | effect |
|---|---|---|
| 1 | `difference_of_pic_nums_minus1` | `picNumX = CurrPicNum - (difference_of_pic_nums_minus1 + 1)`; the short-term frame with PicNum `picNumX` becomes unused |
| 2 | `long_term_pic_num` | the long-term frame with that LongTermPicNum becomes unused |
| 3 | `difference_of_pic_nums_minus1`, `long_term_frame_idx` | picNumX as in op 1; first, if some long-term frame already has index `long_term_frame_idx`, **it becomes unused**; then the short-term frame becomes long-term with that index |
| 4 | `max_long_term_frame_idx_plus1` | `MaxLongTermFrameIdx = value - 1` (**none** if value is 0); every long-term frame with an index above the new maximum becomes unused |
| 5 | none | every frame becomes unused, `MaxLongTermFrameIdx` = none; plus the side effects of 3.6 |
| 6 | `long_term_frame_idx` | the *current picture* becomes long-term with that index; first, a long-term frame already holding the index becomes unused |

Rules the encoder must obey, which you may rely on: the index in ops 3 and 6 is at most `MaxLongTermFrameIdx` (so an op 4, in this or an earlier picture, must have set it);
`max_long_term_frame_idx_plus1` is at most `max_num_ref_frames`; operations name frames that exist. Afterwards the current picture is short-term unless op 6 (or the IDR flag)
made it long-term, and the frame count rule of 3.4 holds.

The reading of `long_term_frame_idx` vs `LongTermPicNum`: for frames they are the same number. Ops 3 and 6 *assign* a `LongTermFrameIdx`; op 2 and the list
modification *look up* by `LongTermPicNum`.

### 3.6 MMCO 5 and its side effects

MMCO 5 acts like an IDR in the middle of a stream: everything is forgotten. The decoder must also treat the picture as follows after it is decoded:

* its `frame_num` is regarded as **0** (7.4.3): the DPB entry says `frame_num` 0 and `PrevRefFrameNum` becomes 0, so the next picture has `frame_num` 1 (or, with gaps allowed, a gap is measured from 0).
  (The slice header of the MMCO-5 picture itself of course still carries its real `frame_num`; its `SLICE` log lines show that one.)
* its picture order count is rebased (8.2.1): `tempPicOrderCnt = Min(TopFieldOrderCnt, BottomFieldOrderCnt)` is subtracted from both, so for a frame
  its POC becomes 0 after decoding, and later pictures are counted from there (`prevPicOrderCntMsb` = 0 and `prevPicOrderCntLsb` = the rebased top POC).
  This is the MMCO-5 case of Chapter 3.4's POC computation and matters for output order in Chapter 9.1. This chapter's log does not contain POCs.

### 3.7 Three ways to clear the long-term state

An IDR (flag 0) sets `MaxLongTermFrameIdx` to none; an IDR with the flag sets it to 0 and the IDR itself is long-term index 0; MMCO 5 clears everything and sets none; MMCO 4 with value 0
removes all long-term frames and forbids new indices until a later MMCO 4 grants some. The synthetic generator uses MMCO 5 alone in its list, never combined with other operations.

### 3.8 frame_num gaps (8.2.5.2)

For every picture that is not an IDR, compare `frame_num` with `PrevRefFrameNum`. Normal: equal (a picture after a non-reference one, 7.2) or `(PrevRefFrameNum + 1) % MaxFrameNum`.
Anything else is a **gap**: pictures with the intermediate `frame_num` values are missing.

* If `gaps_in_frame_num_value_allowed_flag` in the SPS is 1, the gap is intentional (e.g. temporal sub-layers that were dropped). Before decoding the current
  picture, create one **non-existing frame** for every missing value `UnusedShortTermFrameNum = PrevRefFrameNum + 1, +2, ...` up to (excluding) the current `frame_num`
  (all modulo `MaxFrameNum`). For each: run the **sliding window** exactly as for a reference picture whose `frame_num` is that value (remove the smallest `FrameNumWrap` if the DPB is full),
  then insert it as a short-term frame. Afterwards `PrevRefFrameNum` equals the last inserted value.
  A non-existing frame has no pixels worth using and is never output. It occupies a slot, so it pushes real pictures out of the window and appears in default lists. A conforming stream
  never gives `ref_idx` a non-existing frame in inter prediction. A decoder may fill it with a copy of the previous picture (ffmpeg does).
* If the flag is 0 the gap is a **loss** (the stream is damaged or was cut). That is error concealment, Chapter 9.5; this harness exits 2 for it.

The gap check happens before the slice's lists are built (the non-existing frames take part in them).

### 3.9 The same picture at two indices, and bS

A modified list can contain one picture at more than one `ref_idx`: T7 has `list0 = 1,1,1,0`. The three first entries are the same picture, usually with different
weights (7.7). Consequently two neighbouring blocks with `ref_idx` 0 and 2 predict from the **same picture**, and the deblocking filter's boundary strength (Chapter 7.5) compares
the *pictures* the blocks refer to, not the `ref_idx` numbers: such an edge with equal motion vectors has bS 0. Resolve `ref_idx` to a picture (a frame identity) in your list, and let
7.5's bS code compare identities. Motion vector prediction (7.3) is different: its "same reference index" tests compare `refIdx` *numbers*, so there 0 and 2 stay different.
With 7.5's decoder reading `RefPicList0[ref_idx]` as a frame object, this comes for free.

## 4. The project: exactly what to build

Extend your 7.2 DPB:

1. **PicNum / LongTermPicNum numbering** and frame states short-term / long-term (3.1), plus `MaxLongTermFrameIdx`.
2. **Initial list** with long-term entries (3.2).
3. **Modification** per P slice (3.3). Your slice-header parser (Chapter 3.3) must deliver the command list; apply it to each slice's own list.
4. **Marking**: IDR with `long_term_reference_flag`, sliding window counting long-term frames, MMCO 1 to 6 (3.5), the MMCO-5 effects (3.6), the count rule (3.4).
5. **Gap process** (3.8) when the SPS allows gaps; report an error otherwise.
6. **Logging** (section 5).
7. Use `RefPicList0[ref_idx]` for prediction exactly as before; the entry may now be a long-term frame or a repeated picture.

**Verify.** The harness compares your log with the reference on x264 and synthetic streams (section 6). The Summary's check, "T6 with `ref=16` decodes byte-identical", is
the recipe `T6_ref16` (T6's options with `ref=16`): sixteen references, a full window, a `frame_num` wrap with a full buffer. It needs nothing of this chapter beyond a
correct sliding window and sort, but it is the regression test for it; check your decoded frames with `--yuv` (section 6).

What is exactly decodable (pixel-identical to ffmpeg) with your decoder after this chapter:

| input | pixels |
|---|---|
| `T6_ref16` | yes (CAVLC Baseline, deblocking on, 16 references) |
| the synthetic `synth_ref_*.264` streams | yes (I_PCM, P_Skip, P_L0_16x16 with zero motion; they are built to reveal the list) |
| `T7`, `x264_weightp_ref6` | the log yes, **pixels need 7.7** (explicit weighted prediction) |
| `T9_spatial`, `x264_pyramid*` | the log yes (P lines only, B pictures still go through marking), **pixels need 7.8 / 7.9** |

## 5. Harness input format

One text file, a superset of Chapter 7.2's. Blank lines and lines starting with `#` are ignored. Fields separated by single spaces, all numbers decimal, list entries by commas without spaces.

```
SLICE <pic> <slice> type=P frame_num=<n> active=<n> list0=<slot>,<slot>,...                one line per P slice
DPB <pic> short=<ref>,<ref>,... long=<ref>,<ref>,... maxlt=<n|none>                         one line per picture
```

| item | meaning |
|---|---|
| `<pic>` | 0-based decoding-order index of a coded picture (Chapter 3.4's grouping; I, P, B, reference or not). A non-existing frame (3.8) is named `g<k>`, k = 0, 1, 2, ... in order of creation over the whole stream (not reset at an IDR) |
| `<slice>` | 0-based index of the slice inside the picture (all slices counted) |
| `type=P` | always `P` in this chapter (B slices are not logged; 7.8 adds `type=B` lines and `list1`) |
| `frame_num=`, `active=` | the slice header's value; `num_ref_idx_l0_active_minus1 + 1` after the override rule |
| `<slot>` | a list entry: `<pic>:<PicNum>` (short-term, PicNum relative to this slice, may be negative), `<pic>:L<LongTermPicNum>` (long-term), or `-` for "no reference picture". Exactly `active` slots, **after** the modification |
| `short=` | the short-term set after picture `<pic>`'s marking: `<pic>:<frame_num>`, **most recently decoded first** (a non-existing frame counts as decoded at the moment it was created, so before the picture that followed the gap); `-` if empty. After an MMCO-5 picture, its own entry has `frame_num` 0 |
| `long=` | the long-term set: `<pic>:<LongTermFrameIdx>`, **ascending index**; `-` if empty |
| `maxlt=` | `MaxLongTermFrameIdx` after the marking, or `none` |

Rules: one `SLICE` line for **every P slice** and none for I or B slices; one `DPB` line for **every coded picture**. Lines go in decoding order: a picture's `SLICE` lines
(by slice index), then its `DPB` line. Non-existing frames get **no** line of their own; they only appear as `g<k>` entries inside `list0=` and `short=`. A non-reference picture's
`DPB` line repeats the unchanged state. The current picture is part of the `DPB` line of its own picture if it is a reference. Chapter 7.2's lines convert by adding `type=P`, renaming `refs=` to `short=` and appending ` long=- maxlt=none`.

Example (T7, start; from `--print-expected`):

```
DPB 0 short=0:0 long=- maxlt=none
DPB 1 short=1:1,0:0 long=- maxlt=none
SLICE 2 0 type=P frame_num=2 active=4 list0=1:1,1:1,1:1,0:0
DPB 2 short=2:2,1:1,0:0 long=- maxlt=none
```

And from `synth_ref_all` (picture 3 follows a gap: `frame_num` 3 is missing, so `g0` was created just before it; later, long-term frame `8:0`
was created by MMCO 3 and is used through `L0`):

```
DPB 3 short=3:4,g0:3,2:2,1:1,0:0 long=- maxlt=none
...
SLICE 10 0 type=P frame_num=5 active=6 list0=6:2,9:4,4:0,8:L0,-,-
DPB 10 short=10:5,9:4,6:2,4:0 long=8:0 maxlt=0
```

## 6. Running the harness

```
(cd .. && python3 -m common.testfiles T6_ref16 T7 T9_spatial)
python3 test_refmgmt.py ../.cache/T6_ref16.ts my_log.txt
python3 test_refmgmt.py ../.cache/T7.ts my_log.txt
python3 test_refmgmt.py --make-variants /tmp/ref_inputs
python3 test_refmgmt.py /tmp/ref_inputs/synth_ref_lt.264 my_log.txt --yuv my_decoded.yuv
python3 test_refmgmt.py --print-expected /tmp/ref_inputs/synth_ref_all.264 expected.txt
python3 test_refmgmt.py --self-test
```

Exit 0 pass, 1 your output differs, 2 your file is missing/malformed, the **input** is unsupported (field pictures, SP/SI, a gap without the SPS flag, a non-conforming stream)
or the reference model disagrees with ffmpeg (a harness problem). `--yuv FILE` additionally compares your decoded frames (raw cropped yuv420p, output order, only the
pictures that exist; non-existing frames are not output) with ffmpeg's; use it where section 4 says pixels are decodable. Note that `--yuv` was tested only with
ffmpeg's own frames and damaged copies of them, not with an independent decoder implementing this chapter: if it fails on a file whose log passes, check frame count and
order (for the inputs in section 4, output order equals decoding order) before you suspect your pixels.

`--make-variants` writes (besides T6_ref16, T7, T9_spatial):

| file | what it exercises |
|---|---|
| `x264_weightp_ref6.ts` | fade-in with `weightp=2`, 6 references: modification with `abs_diff = MaxPicNum` (a picture repeated), wrap in the predictor |
| `x264_pyramid.ts`, `x264_pyramid_ref8.ts` | B-pyramid (`bframes=3` / 5): MMCO 1 in reference B pictures; P slices rare |
| `synth_ref_modshort.264` | short-term modification (idc 0 and 1, repeated targets), two slices with different lists, non-reference pictures |
| `synth_ref_modwrap.264` | `MaxFrameNum` 16 with 10 references: predictor and PicNum wraps in nearly every list |
| `synth_ref_lt.264` | MMCO 1, 2, 3, 4, 6, long-term lists, idc 2 modification |
| `synth_ref_mmco5.264` | MMCO 5 in the middle (frame_num restarts, long-term set reset) together with everything above |
| `synth_ref_gaps.264` | `gaps_in_frame_num_value_allowed_flag` = 1, gaps of 1 to 3 frame_num values, 45 non-existing frames |
| `synth_ref_idrlt.264` | IDR pictures, some with `long_term_reference_flag` = 1, two IDRs in a row |
| `synth_ref_all.264` | all of the above in 90 pictures |

The synthetic streams are tiny 96x80 pictures, picture order count type 0 with lsb in decoding order, deblocking off. P macroblocks have mv (0,0) and no residual, so each
is an exact copy of the picture its `ref_idx` names. That is what lets ffmpeg's pixels confirm the list.

### What is compared, and what confirms the truth

Your `SLICE` and `DPB` lines are compared **exactly** with the output of the harness's reference model (the rules of section 3 run on the slice-header values
that ffmpeg's `trace_headers` prints, including the modification commands and MMCO operations of the first slice of each picture). Before comparing, the harness checks the model:

| claim | confirmed by | on which inputs |
|---|---|---|
| the short-term set (frame_nums, newest first) and the long-term set (index, frame_num) after every marking: sliding window, IDR, MMCO 1 to 6, gaps (also the non-existing frames) | ffmpeg `-debug mmco`: "short term list" / "long term list" after each marking process | **all** inputs |
| the list after modification, the long-term numbering, repeated pictures | decoded pixels of the synthetic streams: every inter macroblock must be a copy of `list0[ref_idx]` | synthetic streams |
| number of "no reference picture" entries | the number of "Missing reference picture" messages ffmpeg prints | synthetic streams |
| `maxlt=`, and the PicNum / LongTermPicNum values you print | nothing in ffmpeg shows them; they follow by the formulas from the verified sets (and a wrong `maxlt` would eventually change a later marking) | model only |
| the modification commands themselves (x264 files) | the DPB sets and the model's rules; ffmpeg does not print its lists | indirect |

## 7. Reading failures / debugging tips

Messages name the line (`SLICE 18 0` = picture 18, slice 0), expected and received text and a hint. They are in decoding order: the first message is the first thing that went wrong (a wrong
`DPB` line breaks later `SLICE` lines too; fix it first).

* **Only slices with modification are wrong**: you apply the commands to the wrong list (the cut one is right, with one extra temporary entry), or you forgot to cut back to `active`.
* **List right up to a point, then a picture appears twice or goes missing**: the duplicate removal. Remove the *later* copy of the inserted picture only, comparing pictures and kinds (short vs long).
* **Wrong only near the `frame_num` wrap**: `picNumLXPred` must be wrapped by `MaxPicNum` in both directions and then kept (the NoWrap value), while the PicNum used for lookup is `NoWrap - MaxPicNum` if `NoWrap > CurrPicNum`.
* **Long-term entries wrong**: after the short-term entries, ascending `LongTermPicNum`; `L` marks them in the log. Idc 2 looks up by `LongTermPicNum`, not PicNum.
* **MMCO 1 removes the neighbour of the right picture**: `picNumX = CurrPicNum - (difference_of_pic_nums_minus1 + 1)`.
* **MMCO 3 or 6 leaves two frames with one index** (`long=` shows the same index twice): free the old holder first.
* **`maxlt` wrong after MMCO 4**: value 0 means "none", otherwise `value - 1`; frames above the new maximum are unmarked at once. MMCO 5 and an IDR reset it to `none` (the IDR with the long-term flag sets it to 0).
* **DPB too large with MMCO pictures**: the sliding window must not run in MMCO mode. **Too small in long-term streams**: long-term frames count toward `Max(max_num_ref_frames, 1)`.
* **After an MMCO-5 picture the next `frame_num` looks wrong / lists wrong**: the picture counts as `frame_num` 0 (DPB entry and `PrevRefFrameNum`).
* **Gaps**: `g<k>` names count over the whole stream; each inserted frame runs the window with *its own* `frame_num`; after a gap the DPB can hold only non-existing frames and the picture itself.
* **A `SLICE` line is missing**: every P slice needs one, B slices none, every picture needs a `DPB` line.
* Write the expected file with `--print-expected` and `diff` it with yours: the first difference is nearly always the bug.

### Harness self-test

`python3 test_refmgmt.py --self-test` (about 15 seconds) builds T6_ref16, T7, T9_spatial, three x264 variants and seven synthetic streams, validates the reference against ffmpeg, checks that the
known-good log passes for each, that corrupted logs fail (dropped lines, swapped entries, a long-term slot turned short-term, a changed `maxlt`, shifted picture numbers, missing and malformed
files, and `--yuv` with exact and damaged frames), and that **21 deliberately wrong rules** each make the comparison fail on at least one input: the predictor not wrapped, `abs_diff` off by one, the predictor
not updated, the duplicate kept, the list not cut back, idc 2 using PicNum, long-term entries before the short-term ones or in descending order, MMCO 1 off by one, MMCO 3 and MMCO 6 not freeing the index, MMCO 4 not
truncating and off by one, MMCO 5 keeping `frame_num` and not resetting `MaxLongTermFrameIdx`, the window counting only short-term frames or also running with MMCO, gaps ignored or without the window, and the IDR long-term flag ignored or
not setting the maximum. For each it also prints which of ffmpeg's own checks (marking log, or pixels and messages) expose the wrong rule.

## 8. What's next

Chapter 7.7 applies explicit weighted prediction: with your lists in place, T7's repeated pictures finally get their different weights. Chapter 7.8 builds the B-slice lists, which reuse the
numbering, marking and modification of this chapter (list 1, and POC-based initial order).
