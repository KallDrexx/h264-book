# Chapter 10.7 - Performance

## 1. Goal and the project

You now have a decoder that is correct on progressive and interlaced video, passes the conformance sweep and survives damaged streams. This last chapter makes it **fast**: fast enough to decode a 1080p, 30 frames per second, High-profile stream at **30 frames per second or better on your own machine, with output still byte-identical** to the reference.

You build nothing new in the file-format sense. You do three things:

1. **Measure** with a harness that runs your decoder as a command, times it properly and checks every output byte.
2. **Profile** to find where the time goes.
3. **Optimise** the hot spots, one change at a time, re-measuring after each one.

Speed is only worth having if nothing else breaks. So the harness checks the output of **every** timed run. A faster decoder that is wrong is reported as wrong, not as fast.

## 2. Big picture

A decoder is a pipeline. For each macroblock it runs through the same stages:

| Stage | What it does | Typical cost character |
|---|---|---|
| Entropy decoding | CABAC / CAVLC: turn bits into syntax elements and coefficients | Serial, branchy, one bit-ish decision at a time. Cost grows with **bit rate**, not with picture size. |
| Prediction | Intra prediction, or inter prediction (motion-compensated interpolation from reference frames) | Many simple arithmetic operations per pixel. Cost grows with **picture size**. The 6-tap sub-pixel filters dominate. |
| Residual | Inverse quantisation and inverse transform, then adding to the prediction | Small; scales with the number of coded coefficients. |
| Deblocking | Filter every block edge after the picture is reconstructed | Touches most pixels near edges, with many conditions. Often the biggest or second biggest stage. |
| Output | Crop, order, write the frame | Memory copying and file I/O. 1080p 4:2:0 is 3.1 MB per frame, about 93 MB per second at 30 fps. |

The first lesson of performance work: **you do not know where the time goes until you measure**. Intuition is usually wrong, and it changes with the stream. A high-bit-rate intra clip is dominated by entropy decoding. A low-bit-rate clip with many skipped macroblocks is dominated by interpolation and deblocking. A decoder that allocates a new buffer per macroblock may spend most of its time in the memory allocator, a cost that appears in no stage of the table above.

The second lesson: **the target is a rate, so think in time per frame.** At 30 fps you have 33.3 ms for a 1080p frame, that is 8160 macroblocks, about 4 microseconds per macroblock. A realistic decoder in a compiled language gets there with plain scalar code if it avoids waste. Interpreted languages usually cannot (see section 3.6).

## 3. Concepts in detail

### 3.1 Measuring honestly

* **Wall time vs CPU time.** Wall time is what the viewer waits. CPU time is the work done. If CPU time is much larger than wall time you are using several cores; if wall time is much larger, you wait for something (disk, a lock, sleeping).
* **Warm-up.** The first run is slower: the input file is not yet in the operating system's file cache, the CPU clock ramps up, and a just-in-time (JIT) compiler, as in Java, C# or JavaScript, is still compiling. A **warm-up run** is executed and verified but not timed.
* **Noise and best-of-N.** Other programs, thermal limits and scheduler luck only make a run *slower*, never faster than the machine's true speed. So the **minimum** over several runs is the most repeatable estimate. The harness reports the best as the speed, and the median and spread as a warning when the machine is noisy.
* **Compare like with like.** Same clip, same machine state, same output location. Writing 280 MB to a slow disk can dominate; `--workdir /dev/shm` takes the disk out of the picture (it uses RAM).
* **Change one thing at a time**, keep a log: change, speed before, speed after, output identical or not.
* **Calibrate.** The harness also times `ffmpeg -threads 1` on the same clip, so you see your speed relative to a highly optimised single-threaded decoder. On a noisy day that ratio is steadier than the absolute fps.

### 3.2 Profiling, language-agnostic

Two methods work everywhere.

**Stage timers.** Put a cheap high-resolution clock around each stage, accumulate per picture, print totals at exit (to stderr, behind a flag). Timers cost something, so time *per slice or per row of macroblocks*, not per macroblock, and for fine detail use a counter of calls instead. You get a table like "CABAC 41 %, interpolation 22 %, deblocking 19 %, output 9 %, other 9 %". Because stages are interleaved per macroblock in many designs, the cleanest approach is to restructure temporarily: decode all macroblocks of a slice, then reconstruct, then deblock the picture, timing each loop. That also happens to be a good structure for optimisation.

**Sampling.** A sampling profiler interrupts the program many times per second and records where it is; functions with many samples are hot. Every ecosystem has one (Linux `perf`, Instruments on macOS, `py-spy`/cProfile for Python, async-profiler or JFR for Java, the built-in profilers of Go, Rust tooling such as flamegraph, browser dev tools for JavaScript). Build with optimisation **and** symbols. Profiling a debug build measures the wrong program.

Whatever you use, look at three things: the top functions by *self* time, how many calls they get, and the share of time in memory allocation and copying.

### 3.3 The optimisation ladder

Climb in this order. Each step is cheaper and safer than the next.

1. **Build settings.** Optimised build, no assertions or bounds-check logging in the hot loops, link-time optimisation (optimising across source files) where available. Often a 3x to 10x difference by itself.
2. **Avoid per-pixel and per-macroblock allocation.** Allocate frame buffers, the macroblock state and scratch arrays once, reuse them. No dynamic lists of small objects in the macroblock loop. No string formatting in the hot path. Use flat arrays of integers or bytes (one array per plane, with a row stride), not arrays of arrays of boxed numbers.
3. **Integer-only arithmetic and the right widths.** The spec is all integer arithmetic; keep it that way. Use 8-bit storage for samples and 16/32-bit for intermediates.
4. **Lookup tables instead of branches and computation.** Clipping to 0..255 via a table, the CABAC state-transition and range tables, the zig-zag/field scan tables, `Clip3` for deblocking thresholds (alpha, beta, tc0 tables are already tables), the CAVLC code tables as direct-lookup tables indexed by the next N bits instead of bit-by-bit matching.
5. **Do less work.** Skip the inverse transform for blocks with no coefficients (and use a DC-only shortcut). Skip interpolation work for integer motion vectors (a plain copy). Skip deblocking edges whose boundary strength is 0 without evaluating pixel conditions. Predict only the part of a block you need. Copy whole rows (`memcpy`-style) instead of pixel by pixel when no clamping at picture edges is needed; handle the edges in a slow path and the interior in a fast path.
6. **Cache behaviour.** Process memory in order (row by row), keep per-macroblock data small and contiguous, deblock a row of macroblocks soon after it is reconstructed (it is still in cache) instead of after the whole picture. Keep the 6-tap filter intermediate (needed for the centre sub-pixel positions) in a small reusable buffer.
7. **Make the entropy decoder tight.** CABAC is a chain of dependent steps, so it cannot be vectorised, but it can be made lean: keep the arithmetic decoder state in local variables/registers, renormalise by shifting several bits at once, read input in bigger chunks than bytes, decode the common syntax elements (coded-block flag, significance map, levels) through specialised loops instead of generic functions.
8. **SIMD** (single instruction, multiple data: CPU instructions that do the same operation on 8 to 32 samples at once). The interpolation filters, the inverse transforms, deblocking filters and the output copy work on many pixels with the same arithmetic. Compilers can vectorise simple loops if you write them as tight loops over contiguous arrays without data-dependent branches; intrinsics (functions that map one-to-one to SIMD instructions) or hand-written assembly go further. SIMD is the last single-thread step because it is the most work and the hardest to keep bit-exact (take care of rounding and intermediate widths: the spec's intermediates exceed 16 bits in the 6-tap filter's second stage).
9. **Threads.** Section 3.4.
10. **Output buffering.** Write each frame with one large write (or hand frames to a writer thread) instead of many small writes. Convert and crop into the output buffer directly, not through an intermediate copy.

### 3.4 Threading and its pitfalls

Cores are the biggest remaining multiplier, but H.264 resists parallelism because of dependencies. There are three standard shapes.

| Shape | Idea | Limits and pitfalls |
|---|---|---|
| **Slice threading** | Slices of one picture are independent for parsing and for reconstruction (no prediction across slice borders, availability rules from Chapter 4.3). Decode each slice on its own thread. | Only helps streams with several slices per picture; x264's default is one. Deblocking crosses slice borders (unless disabled), so it must run after all slices of the picture, or row-by-row once both sides are done. |
| **Frame threading** | Decode picture N+1 while picture N is still being decoded. A P or B picture needs reference pixels only up to the rows its motion vectors reach, so it can start as soon as those rows of the reference are finished. | Needs progress tracking per reference picture: "rows decoded so far", with the consumer waiting when it needs more. Hard to get right (races), and the picture buffer pool, reference marking and output order (Chapter 9.1) all become concurrent. Latency grows by the number of frames in flight. |
| **Row / wavefront threading** | Macroblock (x, y) needs (x-1, y), (x, y-1), (x+1, y-1) as neighbours, so rows can run in a staggered wave, two macroblocks apart. | CABAC makes parsing serial within a slice: the wave helps reconstruction and deblocking, not entropy decoding. Needs per-row progress counters. |

Easier **pipeline** parallelism that works on any stream: split the stages across threads, for example one thread parses (entropy decoding of picture N+1) while another reconstructs and deblocks picture N and a third writes output. The frames travel through queues. Entropy decoding is serial per slice, so the pipeline's speed is bounded by its slowest stage, but it needs no progress tracking across pictures.

Pitfalls, in the order people meet them:

* **Races change output.** The decoder passes a test, then fails one run in fifty. After adding threads, run the harness many times (`--runs 10`), and the fuzz pass of Chapter 10.6. Every timed run is verified for exactly this reason.
* **Shared mutable state** you forgot: a global bit reader, the "previous QP" variable, scratch buffers, the CABAC context table, a shared picture-buffer pool. Give each thread its own, or protect it.
* **Locks per macroblock** cost more than they save. Synchronise per row or per picture.
* **Memory growth**: frames in flight multiply memory use.
* **Determinism**: output must not depend on thread timing. If it can, you have a bug, even if it has not shown yet.
* **Thread start-up cost** matters for short clips; the harness times whole-process wall time, which is what a user feels.
* Check the `cpu/wall` column of the report: 1.0 means one core busy; 3.5 means about three and a half cores. If you added threads and the number stays near 1.0, they are not running in parallel.

### 3.5 What not to do

* **Do not change the output.** Not one sample. No "faster approximate filter", no skipping deblocking, no reduced-precision interpolation, no dropping frames to keep up. Speed gained by being wrong is not speed. The harness compares every byte of every run with `ffmpeg -threads 1`.
* **Do not optimise before measuring**, and do not keep an optimisation that the measurement does not support. Complex code that gives 1 % is a cost.
* **Do not remove the bounds checks of Chapter 10.6** to gain speed in code that is not hot. If you must remove one from a hot loop, prove the bound once outside the loop (clamp or validate at the slice level), and re-run the fuzz pass.
* **Do not trust a single run.** One fast run proves little; best of several runs is the figure.
* **Do not compare numbers across machines or days** without the reference timing.

### 3.6 Language realism

* Compiled languages (C, C++, Rust, Go, Zig, Java, C#) can reach 30 fps for 1080p High with ladder steps 1 to 7, usually without SIMD and sometimes on one thread. Add threads (step 9) where a single core is not enough.
* Managed runtimes: avoid garbage creation in the macroblock loop; warm-up matters (JIT).
* Pure Python, Ruby and similar will not reach 1080p30 on one core, whatever you do. Their route is native extensions or the vectorised array libraries for the inner loops (pixel filters), and multiple processes. The harness still helps you: the smaller `tiny` and `sd` clips (section 5.2) let you track progress, and `--target-fps 0` reports without failing.

## 4. The project: exactly what to build

1. Make sure the decoder fits the command interface of section 5.1 (the same as Chapter 10.6, except that `{in}` is always Annex B).
2. **Measure** the baseline with the harness on `hd`. Write down the fps and the tier (the speed band of section 5.4).
3. **Profile**: produce a table of where the time goes (stage timers or a sampling profiler) for `hd` and for the smaller `tiny`. Decide the top two targets.
4. **Optimise** following the ladder, one change at a time. After each change run the harness (the `--clip hd` run verifies the output, and the other clips cover different shapes) and keep a log.
5. Re-run Chapter 10.6's sweep (`--mode sweep`) and a fuzz pass; optimisation is where bounds checks and rare paths break.
6. Reach the Verify condition.

Verify (from the Summary): measured decode rate for a 1080p30 High-profile file exceeds 30 frames per second on your machine, with output still byte-identical. In harness terms: `python3 test_performance.py --decoder "..." --clip all` prints PASS (exit 0) with tier 3 or above on every clip.

## 5. Harness input and output format

### 5.1 How your decoder is invoked

Exactly as in Chapter 10.6. `--decoder` is a command template; `{in}` and `{out}` are replaced by absolute paths; the template is split like a shell command line (quotes work) and **no shell is used** (no pipes or redirections).

| Item | Rule |
|---|---|
| `{in}` | An Annex B byte stream (start codes). A `.ts`/`.mp4`/`.mkv` given with `--stream` is first remuxed to Annex B with ffmpeg. |
| `{out}` | Raw planar yuv420p, 8 bits, **cropped** to the SPS window, frames back to back, in output order, no header. Create or overwrite; the harness deletes it before each run. |
| Exit status | 0 on success. Anything else is a failure (even if the output is right; it says so). A signal or status 128 or more is a CRASH. |
| Time | `--timeout` seconds (default 120) per run. Then the decoder and every process it started (its own process group) are killed. |
| Files | The core-dump size is set to 0. The output file may not grow beyond 4 x the expected size plus 16 MiB (an OUTPUT EXPLOSION is a failure). |
| Memory | The peak resident memory of the decoder process (and the children it waited for) is read from the operating system after each run and compared with `--max-rss-mb` (default 2048). |
| stdin / stdout / stderr | stdin is closed. Output and errors go to a log; the last 300 bytes are shown on failure. Print your stage timings to stderr if you like. |

### 5.2 The clips

Generated once with x264 (through ffmpeg) into `.cache/perf/` (git-ignored; a few seconds, and again only if you delete the cache). All are High profile, CABAC, 2 B-frames, 3 reference frames, 8x8 transform, weighted P prediction, one IDR every 30 frames, with moving test patterns and noise so that real residuals are decoded.

| Clip | Size | Frames | Note |
|---|---|---|---|
| `hd` (default) | 1920x1080 | 90 (3 s) | the Verify clip, about 12 Mbit/s, 1 slice per picture. 1080 is not a multiple of 16, so cropping is exercised. |
| `hd_slices` | 1920x1080 | 90 | the same with 4 slices per picture (for slice threading) |
| `sd` | 640x360 | 60 | development clip |
| `tiny` | 320x180 | 30 | quick check, process start-up dominates |

`--clip a,b` selects several, `--clip all` all four. `--frames N` changes the clip length (a new file is generated). `--stream FILE` measures any other H.264 stream instead (truth from ffmpeg).

### 5.3 Truth and the comparison

The truth is `ffmpeg -threads 1 -fps_mode passthrough -pix_fmt yuv420p -f rawvideo` of the same file, summarised as size and MD5 (cached in `.cache/perf/truth/`). **Every** run, warm-up or timed, must match both. On a mismatch the harness decodes the truth again and locates the first difference:

```
  hd run 1: WRONG OUTPUT: not byte-identical to ffmpeg -threads 1 (md5 differs)
      first difference: frame 1 (0-based, output order) plane Y MB(0,0) x=0 y=0 expected 82 got 83
      2 of 90 compared frames differ
      in frame 1: 2886047 sample(s) differ, planes Cb,Cr,Y
      hint: output frame 1 equals expected frame 2 (dropped, duplicated or reordered frame)
```

Frame numbers are in output order, 0-based. `MB(x,y)` is in 16x16 luma units (8x8 for chroma planes), counted in the cropped picture. Other lines you may see: "output is a correct prefix but stops early" (frames missing), "N extra byte(s) follow", "file size is not a whole number of frames", "Cb and Cr are swapped", "luma is right; only chroma differs", "the size matches the UNCROPPED picture".

### 5.4 The report

```
performance: decoder './mydec {in} {out}'; target 30 fps; 1 warm-up + 3 timed run(s); 12 cpus, load 0.4
clip hd: 1920x1080, 90 frames, 5.7 MB, 1080p30 High, CABAC, 2 B-frames, ...
  run       wall s      fps    cpu s cpu/wall  peak MB   output
  warm-up    3.402     26.5     3.30     0.97      212   identical
  1          3.021     29.8     2.96     0.98      212   identical
  2          3.035     29.7     2.98     0.98      212   identical
  3          3.018     29.8     2.95     0.98      212   identical
  best of 3: 3.018 s = 29.8 fps (0.99x of 30 fps); median 3.021 s; spread 1%
  reference: ffmpeg -threads 1 needs 0.762 s = 118.1 fps; your decoder runs at 0.25x of that

clip       frames  best fps x target  peak MB  correct  tier
hd             90      29.8     0.99      212  yes      2 almost: half real time or better
  FAIL: clip hd: 29.8 fps is below the target 30 fps (tier 2: almost: half real time or better)
FAIL: performance (1/2 checks)
```

* `wall s` is the whole process, start to exit. `fps` = frames / wall. `x target` = best fps / `--target-fps`. `cpu s` is user + system CPU time, `cpu/wall` the average number of busy cores, `peak MB` the peak resident memory.
* The speed of a clip is the **best** timed wall time. Warm-up runs are verified but not counted.
* **Tiers** (against `--target-fps`, default 30; the number is the tier value in the table and in the JSON file):

| Tier | Speed | Name |
|---|---|---|
| 0 | below 0.25x target | correct but slow |
| 1 | 0.25x to 0.5x | slow: a quarter of real time or better |
| 2 | 0.5x to 1x | almost: half real time or better |
| 3 | 1x to 2x | REAL TIME: at or above the target |
| 4 | 2x to 4x | real time with headroom |
| 5 | 4x and more | fast |

Tier 3 or more is a pass. `--target-fps 0` never fails on speed (use it while developing). A wrong output, crash, hang, explosion, non-zero exit or peak memory above `--max-rss-mb` always fails, whatever the speed; the line begins with the clip and run, and one of `WRONG OUTPUT`, `HANG`, `CRASH`, `OUTPUT EXPLOSION`, `NO OUTPUT FILE`, `non-zero exit status`, `MEMORY`.
* `--json FILE` writes all numbers (per clip: fps, best, runs, tier, peak memory, correctness).

### 5.5 Options and exit status

| Option | Meaning |
|---|---|
| `--decoder T` | the command template (required) |
| `--clip`, `--stream`, `--frames` | what to decode (section 5.2) |
| `--runs N` (3), `--warmup N` (1) | timed and untimed runs. Use more runs on a noisy machine. |
| `--target-fps F` (30) | the requirement; 0 = report only |
| `--timeout S` (120) | seconds per run |
| `--max-rss-mb M` (2048) | peak memory limit |
| `--workdir D` | where the output file goes (default `.cache/perf/work`) |
| `--no-ref` | skip the ffmpeg reference timing |
| `--json F` | write results |
| `--self-test` | check the harness itself |

Exit status: **0** every clip correct, at or above the target and within the memory limit; **1** at least one clip wrong, crashed, hung, too slow or too big; **2** unusable input (no `{in}`/`{out}` in the template, unbalanced quotes, decoder program that cannot be started, bad numbers such as `--runs 0` or a negative target, unknown clip, missing/empty/undecodable `--stream`, an unknown option, ffmpeg missing).

## 6. Running the harness

```
# the Verify run: 1080p30 High, best of 3 after a warm-up
python3 test_performance.py --decoder "./mydec {in} {out}"

# while developing: small clips, no failure on speed, quick
python3 test_performance.py --decoder "./mydec {in} {out}" --clip tiny,sd --target-fps 0 --runs 2

# everything, 5 timed runs, output in RAM, results saved
python3 test_performance.py --decoder "./mydec {in} {out}" --clip all --runs 5 --workdir /dev/shm --json perf.json

# a threaded decoder: verify many times for races
python3 test_performance.py --decoder "./mydec -j 4 {in} {out}" --clip hd_slices --runs 10

# the harness's own check (about a minute)
python3 test_performance.py --self-test
```

The harness can never hang: every run is limited by `--timeout` and its whole process group is killed afterwards.

### 6.1 Worked example: a profile-driven session

Baseline of a straightforward C decoder:

```
hd   90 frames   best 7.4 fps   tier 0   correct
```

Stage timers say: CABAC 24 %, interpolation 31 %, deblocking 28 %, output 4 %, allocation and other 13 %. The three big stages are about equal, so start with the cheapest fixes (ladder steps 1, 2 and 5). A compiler optimisation level (`-O2`): 7.4 to 22 fps. The profile shows 11 % of the time in the allocator, from a per-macroblock scratch array: allocate once: 22 to 25 fps. Deblocking evaluates the pixel conditions for edges with boundary strength 0; skipping them: 25 to 31 fps. The harness says tier 3 and `identical` throughout. Done for the single-threaded target; threads would add headroom: with a two-stage pipeline (parse | reconstruct and deblock) the `cpu/wall` column goes to 1.7 and fps to 49, tier 4.

### 6.2 Worked example: speed up that breaks output

```
clip hd: ...
  warm-up   2.100     42.8 ...   identical
  1         2.050     43.9 ...   FAIL
  hd run 1: WRONG OUTPUT ...
      first difference: frame 31 (0-based, output order) plane Y MB(80,12) x=1283 y=196 expected 117 got 118
```

The warm-up run of the threaded decoder passed, run 1 did not: a race. MB(80,12) is far from the picture's start, so it is not an initialisation problem. Typical suspects: a reference picture being read before its deblocking finished, a shared scratch buffer, an output queue releasing a buffer too early. The harness's "every run is verified" rule is what found it.

## 7. Reading failures and debugging tips

* **WRONG OUTPUT after a change**: revert and bisect the change. If it is only wrong in later runs, it is a race or uninitialised memory (state left from the previous run in a static buffer).
* **Frame 0 differs at MB(0,0) after an unrelated optimisation**: look at what is initialised once per picture or per slice (the optimisation reordered or skipped it).
* **Wrong only at picture edges**: the fast path of interpolation or deblocking is being used where clamping (reference padding) is needed.
* **Wrong only in the clips with B frames, or only `hd_slices`**: the fast path is missing a case (bi-prediction rounding, weighted prediction, a slice-border neighbour).
* **Fast on `tiny`, slow on `hd`**: something grows with picture size (memory zeroing per frame, a per-frame table rebuild, an O(n^2) neighbour search, cache misses).
* **Slow on all clips by a similar factor**: build settings, or a per-pixel function call that is not inlined.
* **Speed varies much between runs (spread above 25 %)**: noisy machine; thermal limits on a laptop; another heavy process; frequency scaling. Raise `--runs`, or compare against the reference ratio.
* **cpu/wall below 1.0**: the decoder waits (output writing, a sleeping thread). **Far above the thread count you intended**: some library uses its own threads.
* **MEMORY**: you keep every decoded frame; output queue unbounded; per-frame allocations never freed.
* **HANG** with threads: a deadlock, usually a waiting thread that is never woken at the end of the stream or after an error. Remember to release all waiters on every exit path.

## 8. The self-test

`--self-test` (about a minute) uses `ref_decoder.py`, a wrapper around `ffmpeg -threads 1` with switchable slowdowns and bugs, as the decoder. It checks that:

1. the correct stand-in passes on all four clips and on a `.ts` input, with the JSON report and the reference timing line;
2. stand-ins paced at 4, 12 and 60 fps (target 15) are measured in that order, below each pace, and get the right tiers and exit status. The pace is a minimum run time, so a busy machine can only make the slow cases slower, never make them pass;
3. a stand-in whose first call is 3 s slower passes with a warm-up run or best of 3, and fails with one cold run;
4. 19 wrong behaviours each exit 1 with their own diagnosis: wrong samples, swapped or grey chroma, missing, duplicated, swapped or partial frames, extra bytes, uncropped output, empty or missing output, non-zero exit, crash, hang (whose child process must not survive), endless output, too much memory, and output that is right only the first time; plus a correct but too-slow decoder;
5. 16 malformed inputs exit 2.

## 9. Wrap-up of the whole book

Starting from a stream of 188-byte transport packets, you built every layer of an H.264 decoder yourself, each one checked against ffmpeg before the next began:

* **Containers and bitstreams** (Phases 1 and 2): packets, tables, PES, timestamps, start codes, emulation prevention and the bit reader.
* **Headers** (Phase 3): SPS, PPS, slice headers, picture boundaries and picture order count.
* **Syntax and reconstruction** (Phases 4 to 6): macroblock parsing with CAVLC, intra prediction, transforms, quantisation, multiple slices and the deblocking filter.
* **Inter coding** (Phase 7): P and B slices, motion vectors, interpolation, reference management, weighted prediction and direct modes.
* **CABAC and the High profile** (Phase 8): arithmetic decoding, the full syntax, 8x8 transform and scaling matrices.
* **Output** (Phase 9): output order, colour conversion, presentation, timing and live input.
* **Interlace and hardening** (Phase 10): fields, MBAFF, deinterlacing, a conformance sweep with fuzzing, and, here, speed.

The lesson that repeated in every chapter: **make a verifiable claim, then check it against an independent truth.** Correctness came first, bit for bit, and only then speed, always under the same check. You can carry that discipline to any codec, container or protocol.

Where to go next, if you want more: the features this book skipped (FMO and ASO, data partitioning, redundant slices, SP/SI pictures, the 4:2:2 and 4:4:4 and high-bit-depth profiles, SVC and MVC extensions), an encoder (the same tools run in reverse, and you now know what a good one must produce), hardware decode APIs, or the successors that build on the same ideas (HEVC, AV1, VVC). Your decoder is a good place to start experimenting.
