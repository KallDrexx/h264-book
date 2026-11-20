# Chapter 9.1 - Output ordering (bumping)

> **SPOILER WARNING - do not open `spoiler_reference/` until your own bumping passes.** `spoiler_reference/bumping_ref.py` is a complete solution of this chapter (the C.4 bumping process), and `bump_reader.py` / `frontend.py` a complete reader built on it; `streamgen.py` and `cases.py` build the test suite. `test_bumping.py` is plumbing and safe to read.

## 1. Goal and the project

A decoder produces pictures in *decoding order*, but a viewer must see them in *output order*. Up to now you probably sorted the pictures of each IDR period by picture order count (POC) once the whole file was decoded. That shortcut needs the entire file in memory and can never run live. This chapter replaces it with what the standard really specifies: a small buffer, the **bumping process** of Annex C.4, that decides *when* each picture leaves the decoder and in what order. You build it as a stand-alone module and plug it into your decoder, so that frames come out one at a time, in order, cropped, as soon as the standard allows.

The harness checks two things, called *channels*. **Channel A** feeds your module a text trace (one line per decoded picture, section 5.1) and checks which pictures it outputs after each one; no pixel decoding is involved. **Channel B** compares your decoder's whole-file YUV output with ffmpeg's. You are done when (a) your module reproduces the exact output sequence and timing of a reference bumping implementation on a suite of unusual streams (channel A), and (b) your decoder's whole-file YUV output is byte-identical to ffmpeg's, with the same frame count, for T9, T11 and a broadcast-like file.

## 2. Big picture

B-frames and pyramids mean that a picture decoded early may have to be shown late (a P picture is decoded before the B pictures that are displayed before it). So the decoder must *hold* finished pictures back. How long? As few as possible (latency, memory), but never so briefly that a picture with a smaller POC could still arrive later. The standard answers with a model: a **decoded picture buffer (DPB)** of a fixed number of **frame buffers**. Pictures wait there; when a new picture needs a buffer and none is free, the waiting picture with the smallest POC is pushed out ("bumped"). A stream is conforming only if this process outputs in increasing POC order, so for good streams bumping and "sort by POC" give the same *order*. They differ in *timing*, in what happens at IDR / MMCO 5 boundaries when `no_output_of_prior_pics_flag` is set, and in what they do with broken streams.

## 3. Concepts in detail

### 3.1 What you need from earlier phases (restated)

* **POC.** Each decoded frame has a PicOrderCnt (an integer, can be negative; increasing = displayed later). For a frame it is min(top, bottom field order count). POC restarts at 0 on every IDR picture, and on every picture with memory_management_control_operation 5 (MMCO 5): **that picture's POC becomes 0 after it is decoded** (its stored POC is rebased to POC - min(top, bottom) = 0), and pictures after it count from there. Pictures from different "POC periods" are never compared: the DPB is flushed at each boundary.
* **Reference marking.** Every stored frame is "used for reference" until the sliding window or an MMCO command (or an IDR / MMCO 5) marks it "unused for reference". A frame buffer can be reused only when its picture is both unused for reference *and* already output.
* **Output format.** Output frames are the *cropped* picture (SPS `frame_crop_*_offset`, in crop units: 2 luma samples horizontally, `2 * (2 - frame_mbs_only_flag)` vertically), planar yuv420p, no padding. 1080p video is coded as 1088 rows and written as 1080.
* **VUI `bitstream_restriction`.** An optional part of the SPS VUI containing `max_num_reorder_frames` and `max_dec_frame_buffering`.

### 3.2 DPB size (in frames)

* If the SPS has `bitstream_restriction_flag = 1`: **size = max_dec_frame_buffering** (it may be 0; x264 writes 0 for intra-only streams).
* Otherwise, for an intra-only profile (`profile_idc` 44, 86, 100, 110, 122 or 244 with `constraint_set3_flag = 1`): **size = 0** (E.2.1).
* Otherwise: **size = Min( MaxDpbMbs / (PicWidthInMbs * FrameHeightInMbs), 16 )**, integer division, with MaxDpbMbs from Table A-1 selected by `level_idc`:

| level_idc | 9 | 10 | 11 | 12, 13, 20 | 21 | 22, 30 | 31 | 32 | 40, 41 | 42 | 50 | 51, 52 | 60, 61, 62 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MaxDpbMbs | 396 | 396 | 900 | 2376 | 4752 | 8100 | 18000 | 20480 | 32768 | 34816 | 110400 | 184320 | 696320 |

* **Level 1b** has MaxDpbMbs 396 (like level 1): it is `level_idc = 9`, or `level_idc = 11` with `constraint_set3_flag = 1` in Baseline, Main or Extended profile (profile_idc 66, 77, 88). In other profiles `level_idc 11` is level 1.1.
* FrameHeightInMbs = (2 - frame_mbs_only_flag) * (pic_height_in_map_units_minus1 + 1). Use the *coded* size in macroblocks, not the cropped one.

Examples: 352x288 (22x18 = 396 MBs), level 1.3: 2376 / 396 = **6**. 640x360 coded as 640x368 (40x23 = 920 MBs), level 3.0: 8100 / 920 = **8**. 1280x720 (3600 MBs), level 3.1: 18000 / 3600 = **5**. 1920x1088 (8160 MBs): level 4.0 gives 32768 / 8160 = **4**; level 5.1 gives 22, capped to **16**. 176x144 (99 MBs): level 1b gives 396 / 99 = **4**, but level 1.1 gives 900 / 99 = **9**.

`max_num_reorder_frames` is *not* part of the C.4 model. It is the stream's promise of how many pictures can precede any picture in decoding order and follow it in output order, so a low-latency player may output a picture as soon as more than that many are waiting. It never changes the order, only the time. Ignore it here; the project needs only the DPB size. (No later chapter uses it either; it only matters for a low-latency player, which you can add on top.)

### 3.3 The state of a frame buffer

Each of the `size` frame buffers is empty or holds one decoded frame with two independent facts: **needed for output** (decoded, not yet output) and **used for reference**. DPB *fullness* = number of non-empty buffers. A buffer is emptied as soon as its picture is neither needed for output nor used for reference.

### 3.4 The bumping process (C.4.5.3)

Select, among the pictures *needed for output*, the one with the **smallest POC**. Output it (cropped) and clear its "needed for output" flag. If it is no longer used for reference, empty its buffer. (A bumped picture that is still a reference stays in its buffer, so one bump does not always free a buffer.)

### 3.5 What happens when a picture has been decoded (C.4.4, C.4.5.1, C.4.5.2)

Handle the pictures strictly in decoding order. For each picture, using the stream's DPB size:

1. **IDR picture, or a picture with MMCO 5.** Mark every frame in the DPB unused for reference. Then:
   * IDR with `no_output_of_prior_pics_flag = 1`: empty all buffers **without output**; the waiting pictures are lost.
   * otherwise (flag 0; MMCO 5 pictures always behave like this, the flag exists only in IDR slices): bump repeatedly until no picture is waiting for output, then all buffers are empty.
   Store the current picture (an MMCO 5 picture is stored with POC 0). It is a reference picture, so it is "used for reference" and "needed for output". Done.
2. **Any other picture.** First apply its reference marking (sliding window or MMCO 1-4; in the channel A trace these are the frames listed in `unref=`; the current picture is never among them), then empty every buffer whose picture is neither needed for output nor a reference. Then:
   * **Reference picture:** while no buffer is empty, bump. Then store it.
   * **Non-reference picture:** if a buffer is empty, store it. If none is empty and its POC is **lower than the POC of every picture waiting for output**, it is output immediately and not stored (it would be the next bump anyway). Otherwise bump while no buffer is empty, then store it.
3. **DPB size 0:** nothing can wait; every picture is output immediately and nothing is stored (after the flush of step 1, if any).
4. **End of the stream:** bump repeatedly until the DPB holds no waiting picture.

The standard also allows (does not require) a decoder to *infer* `no_output_of_prior_pics_flag = 1` at an IDR when the picture size or DPB size changes. Do not infer; use the flag as written.

### 3.6 Worked example 1 - a pyramid stream (T9-like, size 4)

Pictures in decoding order with POC: 0 I(0), 1 P(6), 2 P(2, a reference B), 3 b(4, non-reference), 4 P(10), 5 b(8), 6 P(18), ... The marking in this stream makes picture 0 unused when 4 arrives.

| after picture | action | output (ids) | fullness |
| --- | --- | --- | --- |
| 0, 1, 2, 3 | store, buffers fill up | - | 1, 2, 3, 4 |
| 4 (P, POC 10) | picture 0 unused; buffer still needed, DPB full -> bump 0 (POC 0), its buffer empties; store | 0 | 4 |
| 5 (b, POC 8) | full; POC 8 not lowest -> bump 2 (POC 2): still a reference, no room; bump 3 (POC 4): non-reference, room; store | 2, 3 | 4 |
| 6 (P, POC 18) | full -> bump 1 (POC 6, reference, no room); bump 5 (POC 8, room); store | 1, 5 | 4 |

Output order so far 0, 2, 3, 1, 5 = ascending POC 0, 2, 4, 6, 8.

### 3.7 Worked example 2 - IDR with and without `no_output_of_prior_pics_flag` (size 3)

Decode order: 0 I(0), 1 P(6), 2 B(2), 3 B(4), 4 IDR, 5 P(4), 6 B(2), end. Picture 3 arrives with the DPB holding 0, 1, 2 (full, nothing lowest), so bumping outputs 0 (stays, reference) then 2 (room). Now 1 (POC 6) and 3 (POC 4) wait.
* IDR 4 with flag 0: flush -> outputs 3 then 1, store 4 (fullness 1). Total output: 0 2 3 1 4 ...
* IDR 4 with flag 1: pictures 3 and 1 are **never output**. Output: 0 2 4 ...

Example 3 - MMCO 5: same start with picture 4 an MMCO 5 P-picture with POC 12: the flush outputs 3, 1, then picture 4 is stored with POC 0; a later picture 5 with POC -4 is output *before* it, one with POC 6 after.

Example 4 - immediate output: size 1, nref 1: I(0) stored (fullness 1); then a non-reference picture with POC -2 arrives with no free buffer and a lower POC than the waiting picture: output at once. Then P(4) bumps picture 0, and the flush at the end outputs the P.

### 3.8 Where real decoders and files deviate

* **no_output_of_prior_pics_flag.** ffmpeg ignores the flag and outputs every frame. Channel A follows the standard; channel B follows ffmpeg by default (option `--noout-rule spec` switches it to the standard's frame set). No standard test file sets the flag to 1; only the synthetic suite does.
* **Missing `bitstream_restriction`.** Plain ffmpeg guesses the reorder depth and may drop or misorder the first frames of a stream without it. The harness therefore generates the reference with `-strict 1`, which gives the true POC order. For streams with the VUI field this changes nothing. The standard's behaviour (level-derived size) and `-strict 1` agree on frame set and order.
* **Streams whose own DPB size is too small.** x264 writes `max_dec_frame_buffering = max_num_ref_frames` even when B-frames need one more buffer (for example `bframes=1, ref=3`; T8). Under the strict rules of 3.5, such a stream can reach a state where every buffer holds a reference picture that has already been output, so bumping can free nothing. A conforming stream never does this. A *robust* decoder handles it by **growing the DPB**: whenever a picture needs an empty buffer and *every* buffer holds a reference picture (so no bump could free one), do not bump; add one frame to the size instead, and keep the larger size until the next IDR / MMCO 5. Pictures waiting for output in those buffers are then bumped later, as usual. Use it in your decoder (ffmpeg's frames are in plain POC order, which is what you then produce). The strict rules of 3.5 are what channel A checks; two optional suite cases (`b1_asis`, `nopyr_asis`, marked `# robust rule needed` in their trace) test the robust rule when you pass `--robust` (section 6); for a single stream checked with `--bump`, add `--lenient`.
* **Immediate output.** JM (the ITU-T reference decoder) outputs a lowest-POC non-reference picture immediately even if a buffer is free; 3.5 follows the standard text (only when full). The output order is the same for conforming streams; the timing differs.

## 4. The project: exactly what to build

1. **`dpb_size`**: from the SPS (`max_dec_frame_buffering` if present, else level, coded size, 1b rule, cap 16). Keep the VUI parser from Phase 3.
2. **Bumper**: the state machine of 3.3-3.5 as a module with two entry points, "picture decoded" (POC, flags, which frames were unmarked, DPB size) and "end of stream". It returns the list of pictures to output *now*. In your decoder, the module owns the frame buffers (or indexes into your DPB of 7.2/7.6: you now keep a picture until it is both output and unused for reference).
3. **Output stage**: crop, write planar yuv420p. Output happens inside the bumper callback, not after the whole file.
4. **Replace the sort-by-POC shortcut** with the bumper. Add the robust rule of 3.8 as a switch that is on by default.
5. Verify (Verify bullet of the plan): whole-file YUV for T9, T11 and a broadcast-like file is byte-identical to ffmpeg's `ref.yuv` and has the same frame count; and your bumper reproduces the trace suite.

## 5. Harness input and output formats

Input streams are MPEG-TS (`.ts`; demux with your Phase 1 code) or Annex B (`.264`; the synthetic suite).

### 5.1 Channel A - trace file (reader input)

Text, one line per picture (frame), in decoding order; blank lines and lines starting with `#` are ignored. Fields are `key=value` separated by single spaces, always in this order:

`PIC id=<n> poc=<int> idr=<0|1> mmco5=<0|1> ref=<0|1> noout=<0|1> unref=<ids|-> profile=<n> level=<n> cs3=<0|1> wmbs=<n> hmbs=<n> mdfb=<n|->`

| key | meaning |
| --- | --- |
| id | decoding-order index, 0, 1, 2, ... |
| poc | PicOrderCnt of the frame as 8.2.1 gives it (negative allowed). For an `mmco5=1` picture this is the value *before* the rebase; you must store 0 |
| idr / mmco5 | picture is an IDR / has MMCO 5 in its marking |
| ref | nal_ref_idc != 0 |
| noout | no_output_of_prior_pics_flag (0 for non-IDR) |
| unref | comma list of ids that this picture's reference marking makes "unused for reference" (sliding window or MMCO 1-4); `-` if none. For IDR / MMCO 5 pictures it lists all earlier references (you flush anyway) |
| profile, level, cs3 | profile_idc, level_idc, constraint_set3_flag |
| wmbs, hmbs | PicWidthInMbs, FrameHeightInMbs |
| mdfb | max_dec_frame_buffering, or `-` if the SPS has no bitstream_restriction |

Example: `PIC id=4 poc=10 idr=0 mmco5=0 ref=1 noout=0 unref=0 profile=77 level=13 cs3=0 wmbs=22 hmbs=18 mdfb=4`

Frame pictures only (no field pictures, no frame_num gaps).

### 5.2 Channel A - reader output

One `AFTER` line per input picture in the same order, then one `END` line:

`AFTER <id> out=<ids|-> [full=<n>]` and `END out=<ids|->`

`out` lists, comma-separated and in output order, the ids output **while processing that picture** (including the picture itself if it was output immediately; `-` if none). `full` = number of non-empty frame buffers after processing the picture (checked when present; strongly recommended, it exposes buffer-freeing bugs). `END out` = the flush at the end. Example for 3.6: `AFTER 5 out=2,3 full=4`.

### 5.3 Channel B - YUV

Your decoder's output for the whole input: all frames, output order, cropped, planar yuv420p 8-bit, no headers or padding, one file. Expected = ffmpeg `-strict 1 -fps_mode passthrough -f rawvideo -pix_fmt yuv420p`.

## 6. Running the harness

Python 3 and ffmpeg/ffprobe on PATH.

```
python3 test_bumping.py --make-trace T9_spatial.ts -o t9.trace          # input of your reader
python3 test_bumping.py T9_spatial.ts --bump t9.bump                     # channel A, one stream
python3 test_bumping.py T9_spatial.ts --yuv out.yuv                      # channel B (T9_spatial, T11_high from common.testfiles)
python3 test_bumping.py --suite suite_dir --reader 'myreader {trace} {out}'    # channel A, all 35 synthetic cases
python3 test_bumping.py --suite suite_dir                                # generate, then check existing suite_dir/<case>.bump
python3 test_bumping.py --self-test                                      # verify the harness itself (about 15 s)
```

Exit code 0 pass, 1 fail, 2 missing or malformed input. The suite writes `suite_dir/<case>.trace` and (stream-based cases) `<case>.264`; run channel B on those `.264` files too (`--noout-rule spec` for the `*_noout_*` ones if your decoder honours the flag). Cases include: pyramids (3 and 7 B-frames), a stream with an IDR every 7 frames, open GOP, P-only, intra-only (DPB 0, 1, 3), `max_dec_frame_buffering` raised to 16 or lowered to the minimum, `num_reorder_frames` raised, streams with the restriction stripped (level-derived size, 1080p-like cropping `broadcast_level`), no_output_of_prior_pics_flag set, and hand-made traces for MMCO 5 (x264 cannot produce it), level 1b, an intra-only profile without the VUI field, the 16-frame cap, negative POC, size 0 and 1, a size change at an IDR and a too-small DPB. The "broadcast-like" file is `broadcast.ts` (640x360, High, long GOP with open GOP and pyramid, 8-row cropping); the harness creates it in `.cache/ch9_1/` and `--suite` also writes `broadcast_level.264`.

## 7. Reading failures

* `expected out=[2, 3], got out=[3, 2]`: two pictures leave in one step; you output a not-smallest POC (comparison by decoding order, or by absolute value).
* Right order but wrong `out` step: your DPB size is wrong (VUI vs level, 1b, the 16 cap, or max_dec_frame_buffering of 0) or you forgot to free buffers of unused, already-output pictures (check `full=`).
* `pictures never output`: missing end-of-stream flush, or the flush at an IDR / MMCO 5 discards without output.
* An MMCO 5 case differs in the pictures *after* the boundary: you kept the old POC for the MMCO 5 picture.
* Channel B `DIAGNOSIS: your output frame k is byte-identical to expected frame j`: the pixels are right, only order or count is wrong. Frame count too small by a few frames: pictures dropped by an IDR / flush rule or not flushed at the end. Pixel differences in every frame: a decoding bug, go back to the chapter that owns it; try the same stream with the intra-only or P-only files first.
* The harness stops with exit 2 and "overflows its own DPB size": the stream violates its size (see 3.8); pass `--lenient` and implement the robust rule.

## 8. What's next

9.2 turns the YUV frames you now emit in correct order into something viewable: YCbCr to RGB with the VUI `matrix_coefficients` (BT.709 vs BT.601) and limited range, chroma upsampling, and PPM output compared against an ffmpeg PNG with a small tolerance, since this step is not bit-exact.
