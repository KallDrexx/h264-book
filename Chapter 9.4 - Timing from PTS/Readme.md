# Chapter 9.4 - Timing from PTS

## 1. Goal and the project

Until now your player shows frames as fast as it can, or one per key press (9.3). In this chapter it learns **when** to show each frame: you carry every access unit's PES **PTS** through decoding to the output picture, and present the picture when the clock reaches `PTS - first PTS`. You also decide what to do when a frame is late, when a PTS is missing, when the 33-bit counter wraps, and when the timeline jumps.

Real-time playback is not deterministic, so the project is built around a **simulated clock**: the player runs headless with a *decode-cost model* instead of sleeping, writes a timing log, and the harness checks the log exactly. A short real-clock run is available as an optional manual check.

## 2. Big picture

* A TS stream carries a **PTS** (presentation time stamp) on PES packets, in **90 kHz ticks**; B-frame streams also carry a **DTS** (decode time stamp). Both are 33-bit values (Chapter 1.3, restated here).
* **Decode order is not output order.** With B-frames, pictures leave the decoder (9.1) in a different order than they entered. The PTS belongs to the *picture*, so it has to travel with the picture, not with the output slot.
* The clock does not care about decode order: picture *k* in output order is shown at `anchor + (PTS_k - PTS_0) / 90` ms. The anchor is one fixed start time. Never add delays up.
* If decoding is late, drop the frame instead of falling behind for ever.

## 3. Concepts in detail

### 3.1 Restated from Phase 1: PES timestamps

A PES packet starts with `00 00 01 E0` (video). Its header flag `PTS_DTS_flags` is `00` (none), `10` (PTS) or `11` (PTS and DTS). Each timestamp is 5 bytes: a 4-bit prefix (`0010` PTS only, `0011` PTS with DTS, `0001` DTS), then the 33 bits in three chunks of 3, 15 and 15 bits, each followed by a marker bit `1`. The value counts 1/90000 s. **The PTS applies to the first access unit that *starts* inside that PES.** A PES may hold several access units; the others have **no** PTS. Some PES have no PTS at all.

### 3.2 Carrying the PTS to the output picture

Two approaches:

1. **Carry it (best).** When you split the elementary stream into access units, store the PTS (or "none") in the access-unit record. Keep it in the picture's frame-buffer record through decoding and the DPB. When 9.1's bumping outputs the picture, its PTS comes out with it. This works for any stream and tells you exactly which pictures lack a PTS.
2. **Sort a queue.** In a conforming stream, PTS grows with output order, so the PTS multiset of a coded video sequence (IDR to IDR) equals the output-order list sorted ascending. You can keep a sorted queue and hand out the smallest each time a picture is output. It is simple, but it **cannot place a picture that has no PTS**, and it breaks on non-conforming streams.

Example (decode order `I P B B`, PTS in ticks): `I=133200, P=144000, B=136800, B=140400`. Output order is `I B B P` and the pictures get `133200, 136800, 140400, 144000`: the same values, but each B must receive *its own* PTS. Assigning PTS by decode order (`133200, 144000, 136800, ...`) is the classic bug.

### 3.3 Missing PTS

An access unit without a PTS gets one **in output order**: `PTS = PTS of the previous output picture + D`, where `D` is the frame duration in ticks. Take `D` from the VUI (3.5); without VUI timing use the step between two consecutive output pictures that both have a real PTS; if there is none, 3600 (25 fps). Inference only fills gaps: a picture with a real PTS keeps it. Log filled-in values with `src=inferred`. If **no** access unit has a PTS, picture *k* gets `floor(k * D)` (the stream starts at 0) and every picture is logged `src=vui`.

### 3.4 33-bit wrap-around

The counter wraps at 2^33 = 8 589 934 592 ticks = 95 443.7 s = 26.5 hours. Never compare raw values; take **signed 33-bit differences**:

* `d = (a - b) mod 2^33`; if `d > 2^32`, then `d = d - 2^33`.
* Unwrap by walking the access units **in decoding order** and adding each signed step to a running 64-bit value. Steps between neighbours are a few frames, far below 2^32.

Worked example: first PTS `8 589 754 592` (= 2^33 - 180 000), 50 frames later the raw PTS reads `0`. `d = (0 - 8 589 754 592) mod 2^33 = 180 000`, which is below 2^32, so the step is +180 000 ticks = 2000 ms. A plain subtraction would give -8.59e9.

### 3.5 Ticks to milliseconds, and the VUI fallback

`ms = ticks / 90`. From the first example: `(136 800 - 133 200) / 90 = 40 ms` (25 fps). At 29.97 fps one frame is 3003 ticks = 33.3667 ms.

The SPS VUI may carry `timing_info`: `num_units_in_tick` and `time_scale` count ticks of a clock that advances **two ticks per frame** for frame-coded video (one per field). So `fps = time_scale / (2 * num_units_in_tick)` and **`D = 90000 * 2 * num_units_in_tick / time_scale`** ticks. With `num_units_in_tick = 1, time_scale = 50` it is 25 fps and `D = 3600`. With 30000/1001 fps the VUI says `1001 / 60000`, `D = 3003`. `fixed_frame_rate_flag` may be 0 even for constant-rate streams (x264 does this), so use the VUI as a fallback and cross-check, not as a promise. The same `D` is the **duration of the last frame**, which has no successor to measure.

### 3.6 First-PTS epoch and the anchor

`first PTS` is the PTS of the **first output picture** (not the first access unit's DTS, and not the smallest raw number: after a wrap that is meaningless). In streams that start with an IDR this is also the first access unit's PTS; in a stream cut mid-GOP the first decoded picture can be output after others, so always take it in output order. For every output picture, **`rel_k` = unwrapped PTS_k - first PTS**, in ticks. The clock starts when that picture is shown: that moment is time 0, and picture *k* is due at `sched_k = rel_k / 90` ms (until a discontinuity, 3.8). Always compute the due time from the anchor. If you compute it as "previous presentation + step", every late frame shifts all later frames and the error never goes away (drift).

### 3.7 Late frames

The player decodes picture *k* right after showing (or dropping) picture *k-1*. Define `ready_k` = when decoding finished and `sched_k` = due time. `lag_k = ready_k - sched_k`.

* `lag_k <= late_ms`: **present** at `max(ready_k, sched_k)` (wait if early, show slightly late if late).
* `lag_k > late_ms`: **drop**. The decoding time was spent anyway, so the next decode starts at `ready_k`.

The default `late_ms` is 40. The first picture is never dropped.

### 3.8 Discontinuities

Look at the step between consecutive **output** pictures: `step = rel_k - rel_{k-1}` (unwrapped ticks). If `step <= 0` or `step > 90 000` (more than 1 s) the timeline jumped (splice, encoder restart, PTS reset). **Re-anchor**: the new picture is due one frame duration after the previous one (dropped or not), `sched_k = sched_{k-1} + D/90`, and later pictures are due relative to it: `sched = base_sched + (rel - base_rel)/90`, where `base_sched`, `base_rel` are the `sched` and `rel` of the most recent re-anchored picture (picture 0 before any jump, giving 0 and 0). The unwrapped `rel` keeps the jump so you can see it.

### 3.9 Real clock

With a real clock: read a monotonic clock once when the first picture is shown (`t0`), sleep until `t0 + sched_k` (in short slices, checking again after waking), show, repeat. Do not sleep for "one frame" per frame. On a display, wake slightly early and let the vertical sync round it. Never use the wall clock (it can be changed).

## 4. The project: exactly what to build

Extend your player (9.3) with a **scheduler**:

1. Carry each access unit's PES PTS (or none) through decoding to the output picture (3.2).
2. Infer missing PTS values (3.3), unwrap (3.4), compute `rel`, the due time `sched`, and the VUI duration `D`.
3. Re-anchor on discontinuities (3.8). Drop late frames (3.7).
4. `--virtual-clock COSTFILE`: do not sleep; use the cost model (section 5.2) as the decode time; write `--timing-log`.
5. Without `--virtual-clock`: really wait with a monotonic clock and log measured times.

Verify (Summary): a 60 s clip plays in 60 s (within about 1%), and the logged presentation times match `pts_time` of `ffprobe -show_frames`. The harness checks the second one for every stream and the first through `span_ms` (= 60000.000 for `long60`) and the no-drift check. You may skip drawing pixels; the timing log is the output.

## 5. Harness input and output formats

### 5.1 Command line of your player

```
YOURPLAYER INPUT.ts --timing-log LOG [--virtual-clock COSTFILE] [--late-ms 40]
```

`--late-ms` is a number (ms); the harness always passes it. Options may come in any order (the harness passes `INPUT.ts --late-ms N --timing-log LOG --virtual-clock COSTFILE`). Exit status 0. Your player reads no stdin. If you also have a window mode, make these flags select the headless path.

### 5.2 Cost model file (`<case>.cost`)

Text; `#` starts a comment; blank lines are ignored.

```
default 5        # decode time in ms for every output picture
10 90            # output picture 10 costs 90 ms
```

The index is the **output** index (the cost belongs to the picture shown at that position). The `default` line is required. The cost of picture 0 is ignored. Time 0 is the moment picture 0 is shown.

### 5.3 Timing log (`--timing-log`)

ASCII, `\n` line ends, fields separated by one space, in this order:

```
HEADER frames=<N> dur_ticks=<D> late_ms=<L> clock=<virtual|real>
FRAME <k> pts=<raw33> rel=<ticks> src=<pes|inferred|vui> sched_ms=<x> ready_ms=<x> shown_ms=<x|-> lag_ms=<x> decision=<present|drop> dur_ms=<x> event=<none|reanchor>
...                                          (one per output picture, k = 0..N-1)
END presented=<P> dropped=<Q> span_ms=<x>
```

| Field | Meaning |
|---|---|
| `pts` | the picture's PTS as a raw 33-bit decimal value (0 .. 2^33-1), inferred ones included |
| `rel` | unwrapped ticks since the first output picture's PTS (signed; includes jumps) |
| `src` | `pes` real PTS, `inferred` filled in (3.3), `vui` no PTS in the whole stream |
| `sched_ms` | due time, ms since the first presentation, after re-anchoring |
| `ready_ms` | when decoding of this picture finished (same origin) |
| `shown_ms` | when it was shown; `-` for a dropped picture |
| `lag_ms` | `ready_ms - sched_ms` (negative = early) |
| `dur_ms` | `sched_{k+1} - sched_k`; for the last picture `D / 90` |
| `event` | `reanchor` on the picture that starts a new timeline segment |
| `span_ms` (END) | `sched_last + dur_last`: the length of the clip |

Decimals: print three digits (`40.000`); the harness accepts 0.0015 ms of error. `dur_ticks` in the HEADER is `D` rounded to an integer. Frame 0 has `sched = ready = shown = lag = 0` and `event=none`.

Example (25 fps, `late_ms=40`, picture 10 costs 90 ms):

```
FRAME 9 pts=165600 rel=32400 ... sched_ms=360.000 ready_ms=... shown_ms=360.000 ...
FRAME 10 pts=169200 rel=36000 src=pes sched_ms=400.000 ready_ms=450.000 shown_ms=- lag_ms=50.000 decision=drop dur_ms=40.000 event=none
FRAME 11 ... sched_ms=440.000 ready_ms=455.000 shown_ms=455.000 lag_ms=15.000 decision=present ...
```

### 5.4 Test streams (`--make-cases`)

Generated with ffmpeg (64x48, x264, 2 B-frames, IDR every 50 frames, 25 or 30000/1001 fps) and then **rewritten at PES level** by `spoiler_reference/tsrewrite.py` (the generator; read it for how to build a TS):

| Case | What it tests |
|---|---|
| `ontime`, `late` | B-frame reorder; drop rule, cascade of drops, a frame shown late |
| `wrap` | first PTS = 2^33 - 180 000: wraps after 2 s |
| `nopts` | PTS removed on some PES; PES holding two or three access units |
| `discont_fwd`, `discont_back` | PTS +10 s and -1.67 s at the second IDR |
| `vui_only`, `ntsc_vui` | no PTS at all; time from the VUI (25 and 29.97 fps) |
| `ntsc` | 3003-tick steps |
| `long60` | 60 s, 1500 frames, periodic late frames: no drift |
| `long180_wrap` | 3 minutes, wraps after 90 s |
| `rt5` | 5 s clip for the optional real-clock run |

Truth: `ffprobe -show_frames` PTS in output order (unwrapped by ffmpeg, relative to the first), plus the rule of section 3. Never edit these streams by hand; rerun `--make-cases`.

Alongside each stream: `<case>.truth` has one line per output picture, `<k> <rel> <src>` (the expected `rel` and `src`), and `cases.txt` has one line per case, `CASE <name> ts= cost= truth= late_ms= dur= pts0= mode=` (`dur` = expected `dur_ticks`, `pts0` = expected raw PTS of picture 0, `mode` = `virtual` or `realtime`). Read them when debugging.

## 6. Running the harness

```
python3 test_timing.py --make-cases DIR
python3 test_timing.py DIR --out OUT --reader 'python3 myplayer.py'      # runs your player per case, then checks
python3 test_timing.py DIR --out OUT                                      # only checks OUT/<case>/timing.log
python3 test_timing.py --self-test
```

Options: `--case NAME` (repeatable), `--timeout S` (default 300 s per run; the process group is killed on timeout). Exit codes: 0 pass, 1 mismatch, 2 missing or malformed file / player crash.

**Real-clock check (optional, not deterministic):** `--realtime` (with `--reader`) runs the `rt5` clip without `--virtual-clock`, with a hard 40 s limit. It checks that the frames are shown within 60 ms of their due time, that the log's `span_ms` is within 5% of 5000 ms and that the wall time is not shorter than the clip. To measure the 60 s clip yourself (about 1% expected):

```
time python3 myplayer.py DIR/long60.ts --timing-log /tmp/l60.log
```

## 7. Reading failures

* `pts` differs, `rel` fine: wrong PES field, or 32 bits kept. `rel` negative or huge: no 33-bit unwrap.
* `sched_ms` off by 11.1 or 0.09: `/1000` instead of `/90`.
* Only `pts`/`rel` wrong in B-frame cases: PTS assigned in decoding order.
* `nopts`: a PTS-less picture must be filled from the previous *output* picture, not decoding order.
* `dur_ticks` half or double the expected: VUI factor of two (3.5).
* `shown_ms` drifts but `sched_ms` is right: due time computed from the previous presentation.
* `decision` wrong at one frame: compare `lag_ms` with `late_ms` (strictly greater drops); dropped frames still cost their decode time.
* `reanchor` missing or the gap after it wrong: 3.8.

## 8. What's next

9.5 reads live input: TS from stdin or a UDP socket, starting at the first IDR or recovery point, concealing continuity-counter errors and missing references, and surviving SPS changes. There the clock comes from the sender, and PTS discontinuities are the normal case rather than a test.
