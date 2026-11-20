# Chapter 10.5 - Deinterlacing for display

## 1. Goal and the project

Chapters 10.1-10.4 taught your decoder to produce correct *frames* from field pictures and MBAFF. A frame of interlaced video is two photographs woven together, taken about 1/50 or 1/60 s apart. Show that frame on a screen as it is and moving objects grow "combs" (jagged horizontal teeth). This chapter adds the last step, the **presentation layer**: decide *which fields* of every decoded frame are shown, *in which order*, *how often*, and turn each shown field into a full-size picture with a **bob** deinterlacer.

Nothing in the decoder changes. You write a stage that sits after output ordering (Chapter 9.1) and cropping (9.2), and you test it **headless**: instead of watching a window, your program writes

* (A) a **presentation log**: one `FRAME` line per decoded frame and one `OUT` line per picture to be displayed, with the source frame, the field and a display time;
* (B) the **bob-deinterlaced pictures** as a raw yuv file, one cropped yuv420p frame per `OUT` line.

`test_deinterlace.py` checks both against its own reading of the stream, pixel-exactly. Opening a window (or SDL, or a file sink) for real viewing is then a five-line job that you can do by hand; the hard part is the logic, and that is what is tested.

## 2. Big picture

An interlaced camera shoots every second line at one instant and the lines in between about one **field period** later (20 ms at 50 fields/s, 16.7 ms at 60). The **top field** is the frame's lines 0, 2, 4, ...; the **bottom field** is lines 1, 3, 5, .... Which of the two was shot first is the **field order**: *top field first* (TFF) or *bottom field first* (BFF). It matters: showing the older field second makes motion jump backwards every other field.

A TV or monitor that shows progressive pictures needs one full picture per field period. Three ingredients:

1. **Sequence.** For each decoded frame, the list of fields to show: `TB`, `BT`, a single field, a repeated field, or the whole frame once or several times. The list comes from the stream (the *picture timing SEI*) or, when the stream says nothing, from the field order.
2. **Bob.** Each field has only half the lines. Build a full picture from it by keeping its lines where they are and *inventing* the missing lines from their neighbours (here: the average of the kept line above and below). Output rate = field rate (50 or 60 pictures/s), no combing, at the price of half the vertical resolution and a slight line "bobbing" on still detail.
3. **Time.** Each output has a display time, counted in field periods (**ticks**).

Smarter deinterlacers (motion-adaptive: weave where nothing moves, bob where something does; or motion-compensated) improve still areas. They are optional and not tested; bob is the baseline they all fall back to.

## 3. Concepts in detail

### 3.1 Where the frames come from

All four kinds of decoded frame reach the display stage as an ordinary frame buffer (Chapters 5.1, 10.1, 10.4): two woven fields.

| coded as | what the stream contains | note |
|---|---|---|
| progressive frame | `frame_mbs_only_flag = 1` | single instant; no fields at all unless the SEI says so |
| frame picture in an interlaced sequence | `frame_mbs_only_flag = 0`, `field_pic_flag = 0`, includes **MBAFF** | two fields shot at different instants even though coded together |
| **complementary field pair** | two pictures with `field_pic_flag = 1`, opposite `bottom_field_flag`, same `frame_num` | PAFF; your Chapter 10.1 pairing |

(A *lone* field with no partner is not a frame and is outside this chapter; the harness reports such a stream with exit 2. ffmpeg does not output lone fields either.)

The sample positions are those of Chapter 10.4 section 3.1: the top field is frame rows 0, 2, 4, ... of **every plane**, luma and chroma alike (in 4:2:0 interlaced video each field has its own chroma rows, so chroma row `c` belongs to the field with the parity of `c`). Cropping: in a stream with `frame_mbs_only_flag = 0` the crop offsets are multiples of 4 luma rows, so row 0 of the cropped frame is still a top field row. Work on the cropped frame.

### 3.2 Field order and the picture timing SEI

**Picture timing SEI** (D.1.3 / D.2.3, Annex D): an SEI message (NAL unit type 6, payload type 1) in front of a picture's slices. It carries a `pic_struct` only if the SPS has VUI (video usability information, Annex E) with `pic_struct_present_flag = 1`. `pic_struct` (Table D-1) says how to display the picture:

| `pic_struct` | meaning | display sequence (`seq`) | ticks used |
|---|---|---|---|
| 0 | progressive frame | `F` | 2 |
| 1 | top field only | `T` | 1 |
| 2 | bottom field only | `B` | 1 |
| 3 | top field, bottom field | `TB` | 2 |
| 4 | bottom field, top field | `BT` | 2 |
| 5 | top, bottom, top repeated | `TBT` | 3 |
| 6 | bottom, top, bottom repeated | `BTB` | 3 |
| 7 | frame doubling | `FF` | 4 |
| 8 | frame tripling | `FFF` | 6 |
| 9..15 | reserved | treat as "no SEI" | |

The spec allows 1 and 2 only on *field* pictures (where Rule A step 1 below ignores them) and 0, 3..8 only on frame pictures. A frame picture carrying 1 or 2 is non-conforming; this chapter defines it as "show that one field" (the `ps_cycle` test stream does this on purpose). The "ticks used" column is the spec's `DeltaTfiDivisor` (Table E-6).

`pic_struct` 5 and 6 are what **3:2 pulldown** (film at 24 frames/s shown on 60 fields/s TV) produces; 7 and 8 double or triple a progressive frame (24 p on 48/72 Hz). `F` means "the whole frame, once, unmodified" - there is no deinterlacing of a frame the stream calls progressive.

**Finding `pic_struct`.** SEI NAL unit payload: `payloadType` (a run of `0xFF` bytes plus a last byte), `payloadSize` (same coding), then the payload. For payloadType 1, `pic_struct` is `u(4)`, but the payload *starts* with two delays when the SPS VUI has HRD parameters (hypothetical reference decoder buffer timing, E.1.2: `nal_hrd_parameters_present_flag` or `vcl_hrd_parameters_present_flag` = 1): `cpb_removal_delay` `u(cpb_removal_delay_length_minus1 + 1)` and `dpb_output_delay` `u(dpb_output_delay_length_minus1 + 1)`. Then `pic_struct`, which is all you need; you may ignore the clock timestamp fields after it. To get there you must parse the SPS VUI (E.1.1) at least up to `pic_struct_present_flag`: aspect ratio (with the 255 = Extended_SAR case), overscan, video signal type (with colour description), chroma location, timing info (`num_units_in_tick`, `time_scale`, `fixed_frame_rate_flag`), the two HRD structures (each: `cpb_cnt_minus1`, two 4-bit scales, per CPB `bit_rate_value_minus1`, `cpb_size_value_minus1`, `cbr_flag`, then four 5-bit length fields; remember the `cpb_removal_delay_length_minus1` and `dpb_output_delay_length_minus1`), `low_delay_hrd_flag` (only if either HRD present), then `pic_struct_present_flag`.

The SEI belongs to the **next** picture (the one whose slices follow it in the same access unit). Remember it until that picture's first slice arrives and then forget it.

**Rule A - the sequence of a frame.** Apply the first rule that fits:

1. *Field pair:* the SEI is ignored (each field carries its own with `pic_struct` 1 or 2, which only restates the pairing). The sequence is `TB` if the top field has the smaller POC, `BT` if the bottom field has; if the two POCs are equal, the field that was **decoded first** goes first.
2. *Frame picture with a usable SEI* (`pic_struct_present_flag = 1`, an SEI arrived for this picture, value 0..8): the table above. **The SEI wins even where the POCs disagree** (see the `conflict.ts` stream below).
3. *Frame picture without one,* `frame_mbs_only_flag = 1`: `F`.
4. *Frame picture without one,* `frame_mbs_only_flag = 0`: `TB` if `TopFieldOrderCnt <= BottomFieldOrderCnt`, else `BT`. Equal POCs: top first. This covers MBAFF frames from an encoder that writes no SEI, and the frame pictures mixed into PAFF streams.

**POC of each field.** Rules 1 and 4 compare the two field POCs, which you already compute (Chapter 3.4 for frames, 10.1 for fields): `TopFieldOrderCnt` and `BottomFieldOrderCnt` of the frame. A bottom-first MBAFF stream shows up as `delta_pic_order_cnt_bottom < 0` (POC type 0), as in `bottom = top - 1`.

Why POC and not just "first decoded"? POC is *defined* as display order; decoding order of the two fields of a pair is usually the same but need not be. ffmpeg also derives its `top_field_first` flag from the POCs when they differ.

### 3.3 Output order and ticks

Frames are output in the order of Chapter 9.1 (ascending POC inside a period that starts at each IDR picture or `memory_management_control_operation` 5, DPB bumping). The presentation layer takes them in that order and walks each frame's sequence.

**Ticks.** A tick is one field period. The display time of the first output is tick 0. An output of a single field adds **1** tick; an output of a whole frame (`F`) adds **2** (a frame lasts two field periods). So the ticks used by a frame equal the last column of the table in 3.2. Dividing by the field rate (or multiplying by `num_units_in_tick / time_scale` of the VUI) turns a tick into seconds; the chapter checks only the integer ticks, since mapping them to wall-clock time is Chapter 9.4's job and your frame's PTS gives the origin.

Each element of the sequence is one **output**, numbered `n = 0, 1, 2, ...` over the whole stream.

### 3.4 The bob filter (exact definition)

Input: one decoded, cropped frame (Y, then Cb, then Cr, 8 bit, Cb and Cr of size `ceil(W/2) x ceil(H/2)`), and a field `T`, `B` or `F`.

* `F`: output a byte-identical copy of the frame.
* `T` or `B`: process each of the three planes on its own, counting rows from 0 inside that plane. Let `parity` = 0 for `T`, 1 for `B`.
  * A row `y` with `y mod 2 = parity` is **kept**: copied unchanged.
  * Every other row `y` is **rebuilt** from the two nearest *kept* rows:

    `up = y - 1` if `y - 1 >= 0`, else `y + 1`
    `down = y + 1` if `y + 1 <= rows - 1`, else `y - 1`
    `out[y][x] = (kept[up][x] + kept[down][x] + 1) >> 1`

    (the `+ 1` rounds halves up; at the top or bottom edge only one kept neighbour exists and it is used twice, so the row simply repeats it). A plane with a single row is copied.

The rebuilt rows never read the other field's samples: bob throws that field away. (Reading them is "weave", which is what a frame in memory already is.)

**Worked example, one column of a 6-row plane** with source values `10, 200, 31, 100, 50, 7` (rows 0..5):

| field | kept rows | rebuilt rows | output column |
|---|---|---|---|
| `T` (parity 0) | 0, 2, 4 = 10, 31, 50 | row 1 = `(10+31+1)>>1` = 21; row 3 = `(31+50+1)>>1` = 41; row 5: up = 4, down = 6 is outside so down = 4: `(50+50+1)>>1` = 50 | `10, 21, 31, 41, 50, 50` |
| `B` (parity 1) | 1, 3, 5 = 200, 100, 7 | row 0: up = -1 so up = 1, down = 1: 200; row 2 = `(200+100+1)>>1` = 150; row 4 = `(100+7+1)>>1` = 54 | `200, 200, 150, 100, 54, 7` |

Chroma is treated identically with its own row count. (Note that the true chroma sampling positions of a field are not exactly on a chroma row; the plain row rule is the simple, exactly specified choice. Better chroma handling is an optional improvement, not tested.)

### 3.5 Worked example: 3:2 pulldown

`pd32.264` (24 p film coded as progressive frames, `pic_struct` 5, 4, 6, 3, 5, 4, ... in decoding order; in output order frames 0..3 have `ps` 5, 4, 6, 3):

| frame | `ps` | `seq` | outputs `n` (field @ tick) |
|---|---|---|---|
| 0 | 5 | `TBT` | 0 `T`@0, 1 `B`@1, 2 `T`@2 |
| 1 | 4 | `BT` | 3 `B`@3, 4 `T`@4 |
| 2 | 6 | `BTB` | 5 `B`@5, 6 `T`@6, 7 `B`@7 |
| 3 | 3 | `TB` | 8 `T`@8, 9 `B`@9 |

Ten fields from four film frames: exactly the 2-3-2-3 rhythm, and the alternation of the field parity continues across frames without a gap.

Worked example, a PAFF stream without any SEI (`paff_cabac_poc2.264`, POC type 2): the first pair was decoded bottom field first and both fields have POC 0, so the equal-POC rule keeps decoding order, `BT`; a *frame* picture later in the same stream with equal POCs is `TB` by rule 4.

## 4. The project: exactly what to build

Extend your decoder program (or write a small program that reads its output) as follows.

1. Parse the VUI of the SPS up to `pic_struct_present_flag`, and the picture timing SEI as described in 3.2 (HRD delay lengths included). Keep the pic_struct for the next picture.
2. For every output frame (in output order) compute its sequence by Rule A. You need per frame: whether it was a field pair (and the order its fields were decoded), `ps`, the two field POCs, `frame_mbs_only_flag`.
3. Write the log (section 5) while walking the sequences with the tick counter of 3.3.
4. For every `OUT`, write the bob picture of 3.4 into the yuv file.
5. Optional, untested: a window or a file sink that shows the pictures at their tick times; a motion-adaptive deinterlacer.

Verify: with `--suite`, all streams pass. Bonus: a real 1080i recording (an ad-hoc `--stream` run) shows no combing in your viewer, with 50 or 60 pictures per second.

## 5. Harness input format

For a stream `NAME.ts` or `NAME.264` your program writes `NAME.log` and `NAME.yuv`.

**Log**: UTF-8 text, one record per line, single spaces, `key=value` without spaces. Blank lines and lines starting with `#` are ignored. Anything else that does not match is malformed (exit 2).

```
FRAME <k> coded=<F|TB|BT> ps=<0..8|-> seq=<string of T, B, F>
OUT <n> frame=<k> field=<T|B|F> tick=<t>
```

* `k`: 0-based index of the frame in **output order** (the same numbering as the yuv frames of Chapter 5.1). `FRAME` lines come in increasing `k`, each followed by the `OUT` lines of its sequence (the harness does not require that interleaving, it compares the two kinds of line as two lists).
* `coded`: `F` for a frame picture (progressive, MBAFF or plain frame), `TB` / `BT` for a field pair, in the order the two fields were **decoded**.
* `ps`: the `pic_struct` of the frame picture's SEI (decimal), or `-` when there is none, when `pic_struct_present_flag` is 0, when the value is reserved (9..15), and always for a field pair.
* `seq`: the display sequence of Rule A, e.g. `TBT`, `BT`, `F`, `FF`.
* `n`: 0-based output number, counting over the whole stream. `frame`: the `k` of the frame this output shows. `field`: `T`, `B` or `F`. `tick`: integer display time in field periods, starting at 0.

**YUV**: the `n`-th picture is bytes `n * F .. (n+1) * F - 1`, with `F = W*H + 2*ceil(W/2)*ceil(H/2)` for the *cropped* size `W x H`: Y plane (`W` bytes per row), then Cb, then Cr, no headers, no padding. The file must hold exactly as many pictures as there are `OUT` lines.

Example log (first lines of `pd32.264`):

```
FRAME 0 coded=F ps=5 seq=TBT
OUT 0 frame=0 field=T tick=0
OUT 1 frame=0 field=B tick=1
OUT 2 frame=0 field=T tick=2
FRAME 1 coded=F ps=4 seq=BT
OUT 3 frame=1 field=B tick=3
OUT 4 frame=1 field=T tick=4
```

Example, a PAFF field pair decoded top first with the top POC smaller, then a progressive-coded frame in an interlaced sequence (no SEI): `FRAME 0 coded=TB ps=- seq=TB` then `FRAME 1 coded=F ps=- seq=TB`.

## 6. Running the harness

```
python3 test_deinterlace.py --make DIR          # writes the 21 test inputs into DIR
python3 test_deinterlace.py --suite DIR --reader 'myprog {stream} {log} {yuv}'
python3 test_deinterlace.py --check DIR OUTDIR  # OUTDIR/<name>.log and OUTDIR/<name>.yuv for every input in DIR
python3 test_deinterlace.py --stream X.ts --log X.log --yuv X.yuv     # any stream
python3 test_deinterlace.py --show-expected X.ts                      # print the expected log (not the pixels)
python3 test_deinterlace.py --self-test                               # the harness checks itself (about 15 s)
```

`--suite` runs your command once per stream (stdin closed, 300 s limit each); `{stream}`, `{log}`, `{yuv}` are replaced by quoted paths, outputs land in `DIR/out`. Add `--only pd32,tff` to restrict. Exit codes: 0 pass, 1 a check failed, 2 missing or malformed input (including a log line that does not parse, a yuv size that is not a whole number of frames, or a stream with a lone field).

**What it compares against.** Frames: ffmpeg's decode (`-fps_mode passthrough`, cropped yuv420p). Structure: ffmpeg's `trace_headers` log of the same stream (field pictures, POC type 0/1/2 recomputed independently, SPS flags, `pic_struct` of each picture timing SEI), grouped into frames and output order. Rules A and 3.4 are then applied by a reference in `spoiler_reference/` (do not read it before you have tried!). ffprobe's `top_field_first` and `repeat_pict` are used as a cross-check; where they disagree with the harness you get a `note:` line (not a failure).

**The 21 streams** (`--make`):

| stream | what it tests |
|---|---|
| `tff.ts`, `bff.ts` | x264 MBAFF, SEI `pic_struct` 3 / 4, POCs consistent |
| `conflict.ts` | libx264 via ffmpeg: the SEI says `TB` for frames whose POCs say bottom first (and mixes 3 and 4). Rule A step 2: the SEI wins. ffmpeg's own `top_field_first` follows the POCs here, which is why the harness cross-check cannot be used as truth |
| `tff_nosei.264`, `bff_nosei.264` | same MBAFF streams with every picture timing SEI removed (the VUI flag stays 1): Rule A step 4 from the POCs |
| `ps_cycle.264` | the SEI values rewritten to cycle through 0..8 and back: single fields, repeats, doubling, tripling, mixed in one stream |
| `pd32.264`, `pd_euro.264`, `pd_double.264`, `pd_triple.264` | x264 `--pulldown` 32 / euro / double / triple: progressive stream, `pic_struct` 3..8 |
| `prog.ts`, `prog_ps.264` | progressive, without SEI (all `F`) and with `pic_struct` 0 |
| `paff_*.264` (9) | the PAFF streams of Chapter 10.1 (field pairs in both orders, frames mixed in, POC types 0/1/2, equal POCs, non-reference pairs, output reordering, cropping): Rule A steps 1, 3, 4. They are built by Chapter 10.1's `spoiler_reference/fgen.py`, which the harness loads from that chapter's folder (not a copy), so it needs the book's full checkout |

## 7. Reading failures

* `FRAME line k: ps expected 6, got -`: your SEI parsing missed this picture. Check HRD lengths and that the SEI is attached to the *following* picture, not the previous one.
* `FRAME ... seq expected BT, got TB` on a stream without SEI: field order. Use POC, not "top first" by habit; for a field pair, the *decoded* order only breaks POC ties.
* `OUT line: tick expected 7, got 6`: a whole-frame output (`F`) lasts 2 ticks, a field output 1. Ticks run through the whole stream, never restart.
* `OUT line: field expected T, got B` with the right `seq`: you walk the sequence correctly but print the wrong letter, or you repeat the wrong field for 5/6 (the repeat is of the *first* field: `TBT`, `BTB`).
* Pixel message `output i (frame k, field T, tick t): Y plane row r column c: expected e, got a` plus a hint in brackets:
  * *"bob of the OTHER parity"*: kept lines on the wrong rows (T keeps rows 0, 2, 4...).
  * *"unmodified source frame"*: no interpolation.
  * *"equals the expected output i+-1"*: an order or repeat error.
  * *"kept rows right, interpolated rows wrong"*: rounding (`+1`), edge rule (first row of `B`, last row of `T`), or you used a wrong neighbour.
  * *"Cb differs"* but luma fine: chroma planes need the same treatment with their own height.
* Exit 2 "ffmpeg output is not a whole number of frames" or "N frames by the harness's reading but ffmpeg decodes M": the stream has features outside the harness's picture grouping (lone fields, arbitrary slice order); not a bug of yours.

## 8. What's next

Chapter 10.6 turns the decoder into something that survives the world: a script that runs *every* H.264.1 conformance stream you claim to support and compares with the supplied yuv or MD5 (note: conformance output is the plain decoded frames, never the bob pictures, so keep this chapter's stage strictly after the decoder), then hardens every table index and vector against truncated and randomly corrupted `.ts` files.
