# Chapter 10.6 - Conformance sweep and robustness

## 1. Goal and the project

Your decoder now handles progressive and interlaced video of every kind the book covers. Two questions remain before you can trust it:

1. **Is it right on streams you did not write?** Your tests so far used x264 files and generated streams. The H.264 standard has an official answer key: the **conformance suite** (ITU-T H.264.1), a few hundred small streams, each with the exact decoded output (a `.yuv` file, sometimes only an MD5), made to hit corners of the standard that ordinary encoders never produce.
2. **Is it safe on streams that are broken?** Real inputs lose packets, get truncated and start in the middle. A decoder must never crash, hang or fill the disk, and it must find its way back to correct output.

You build (a) a **sweep script** that runs your decoder over a set of streams and prints a pass/fail table with a diagnosis of the first mismatch, and (b) a **robustness pass** that corrupts streams in reproducible ways and checks that your decoder survives and recovers. You also **harden the decoder itself** (bounds checks everywhere), which is the part that makes (b) pass.

The H.264.1 streams cannot be downloaded by this book's tools (no network). So `test_conformance.py` does both jobs:

* give it any stream and expected output you obtain yourself (`--stream X.264 --expected X.yuv`, or a directory of such pairs) and it is your conformance sweep;
* with no arguments it uses a **built-in set** of 35 streams made by x264 and by the book's own generators (Chapters 10.1-10.4) that cover the features of every earlier chapter, with ffmpeg as the answer key.

Unlike earlier chapters, the thing under test is your **whole decoder**, run as a command. Nothing new is written in a file format; you only have to obey the decoder interface in section 5.

## 2. Big picture

**Conformance.** A decoder conforms to a profile and level when, for every conforming stream of that profile, it produces the output the standard specifies. H.264.1 lists test streams per profile and feature (CAVLC baseline, multiple slices, arbitrary slice order (ASO) and flexible macroblock ordering (FMO) for Baseline, CABAC, B pictures, weighted prediction, interlace/MBAFF, 8x8 transform, scaling lists, ...). Each stream comes with the decoded frames. If you decode all the streams of your profiles and compare, you have the strongest correctness evidence there is. Streams for features you do not implement (FMO, ASO, redundant slices, data partitioning, SP/SI, 4:2:2, high bit depth) are simply out of scope. List which ones you skip.

**Robustness.** A stream with errors is outside the standard's guarantees, so there is no "correct output" for the damaged part. What you *can* demand:

* *Safety*: no crash (signal), no hang, no output explosion, whatever the bytes.
* *Resynchronisation*: after the damage, decoding resumes at a place where the decoder needs no earlier data (an **IDR picture**; a recovery-point I picture works the same way) and from there the output is exactly what a clean decode gives.
* What happens to the frames in between (concealment: repeat the previous frame, show grey, ...) is up to you and is not checked.

**Hardening** is mostly discipline: every number read from the stream is untrusted until checked. The usual hole is an array index or a loop count that came from a syntax element.

## 3. Concepts in detail

### 3.1 Conformance pairs

A conformance stream comes with its decoded output in one of two forms:

| Form | Meaning |
|---|---|
| `X.yuv` | all output frames, planar 4:2:0, 8-bit, in output order, no header. |
| `X.md5` | the MD5 of that yuv. Only a verdict is possible; to locate a difference you need a yuv. |

H.264.1 file names vary (`.264`, `.h264`, `.jsv`, `.jvt`, `.26l`, `.avc`, `.bit`, `.bits`; yuv files like `X.yuv` or `X_dec.yuv`). The sweep accepts all of these. Two things that often trip people up:

* **Cropping.** The reference output of most streams is the *cropped* picture (the SPS `frame_cropping_*` window), which is what ffmpeg produces and what your output stage writes since Chapter 9.1 (cropping on output). If your size is a multiple of 16 but the file is not, check cropping first.
* **Frame count.** Output order and the number of frames output are part of the answer (Chapter 9.1). A decoder that drops the last frames fails the stream.

### 3.2 The ffmpeg answer key and its deviations

Without an expected file the harness decodes with `ffmpeg -threads 1 -fps_mode passthrough -pix_fmt yuv420p -f rawvideo` (single thread removes any dependence on threading; `passthrough` stops ffmpeg from duplicating or dropping frames for timing). ffmpeg agrees with the standard on every *conforming* stream the book generates, but it is not the standard. Known differences, so you recognise them:

* It **conceals** instead of following Annex C when references are missing (frame_num gaps, a missing field, lost slices). The built-in `paff_cavlc_lone_fields` stream contains unpaired fields; ffmpeg's output for those is what the answer key contains.
* It may drop frames before the first IDR/recovery point and uses its own heuristics for the output delay of non-conforming streams.
* It converts everything to 8-bit 4:2:0. For other formats an `.yuv` from the conformance suite is the only truth.
* It decodes the file you give it: container timestamps, edit lists and start offsets of `.ts`/`.mp4` files are ignored by the harness, because the harness extracts Annex B first and both your decoder and ffmpeg read that.

### 3.3 Recovery rules (what the fuzz pass checks)

A damaged access unit (AU, all NAL units of one picture) can leave wrong pixels in every picture that predicts from it. Only an **IDR** picture breaks that dependency: it empties the reference buffer. So the fuzz pass defines for each corrupted stream:

* `last_damaged`: the last AU the corruption may have touched (every kind records which AUs it touches, section 5.5).
* The **recovery IDR**: the first IDR picture after `last_damaged + guard` AUs (`--guard`, default 1: a decoder may finish the broken picture late and let it bleed into the very next picture), provided an SPS and a PPS that were *not* damaged precede it. If the damage *created* an SPS or PPS that is not in the original (a flipped bit can turn a NAL header into type 7 or 8), that bogus set may have replaced a good one, so then the good SPS and PPS must be re-sent after the damage. x264 streams repeat SPS+PPS before every IDR, so they qualify; the generated streams carry them once at the start, so after damage to those no recovery is demanded.
* From the recovery IDR to the end of the stream the output must be **exactly the last k frames of the clean decode**, where k is the number of frames from that IDR on. Anything before is unchecked, including extra or missing frames.

If no recovery IDR exists (damage near the end, or no usable parameter sets), only the safety rules apply.

### 3.4 Hardening checklist

Work through your decoder looking for every value that came out of the bitstream:

* **Parameter sets**: `pic_width_in_mbs`, `pic_height`, `num_ref_frames`, `log2_max_frame_num`, `max_num_reorder_frames`, scaling-list and `offset_for_ref_frame` counts. Reject sizes that are zero or enormous (a limit such as 16384x16384 and a few hundred MB for frame memory), reject `num_ref_frames > 16`. Reject a slice that names a PPS/SPS id that was never received or is out of range (0..255 / 0..31).
* **Slice header**: `first_mb_in_slice` beyond the picture, `num_ref_idx_active` above 32, reordering commands that name a picture that does not exist, MMCO commands on empty entries, `slice_qp_delta` that leaves QP outside 0..51, `disable_deblocking_filter_idc` above 2, `slice_group` fields for FMO you do not support.
* **Macroblock layer**: `mb_type` out of range for the slice type, `ref_idx` at or above the list length (the list may be shorter than `num_ref_idx_active`: empty entries must be handled, not dereferenced), `mb_qp_delta` range, `coded_block_pattern` code numbers above 47, `intra_chroma_pred_mode` above 3, intra prediction modes that need unavailable neighbours.
* **Residual**: `total_coeff`, `total_zeros` and `run_before` that overflow the 4x4/8x8 block (the index `coeffNum` can run past 15), level prefix above 15/16 and escape lengths, CABAC `coeff_abs_level_minus1` bit counts, a CAVLC table lookup that finds no code.
* **Motion vectors**: vectors that point far outside the picture are legal, but the *clamping* of reference coordinates must come first so no memory read leaves the reference frame; accumulating `mvd` values may overflow a 16-bit variable.
* **Bit reader**: reading past the end of the NAL unit returns zeros and sets an error flag; `ue(v)` must stop at 32 leading zeros; the slice data loop must stop at the end of the NAL unit (CABAC: when the data is exhausted).
* **Picture management**: a P slice with an empty reference list, `frame_num` gaps (Chapter 7.6) with absurd sizes, a second field without a first field, more than `max_dec_frame_buffering` pictures waiting for output.
* **Output**: write only whole frames; bound the number of frames you will write per input byte if you can (a 100-byte input cannot honestly produce thousands of frames).

Choose one **error policy** and apply it everywhere: when a slice is bad, stop decoding that slice, mark its remaining macroblocks as missing, conceal them (copy from the previous output or from the nearest decoded neighbour), and go on with the next NAL unit. Do not abort the process, and do not exit successfully with half a frame.

## 4. The project: exactly what to build

1. **Harden the decoder** along section 3.4. Wrap the top level so that any internal error ends in a normal (non-signal) exit; no uncaught exception, no `abort`/`panic`.
2. **Make the decoder command-line compatible** with the interface in section 5.1, if it is not already (one input file, one output file, everything else off by default).
3. **Run the sweep** on the built-in set and fix what differs (`--mode sweep`). Every earlier chapter's harness passing individually does not guarantee that the *combinations* in this set work.
4. **Run the sweep on real conformance streams** you obtain (`--dir`). Make a list: stream, in scope or not, pass or fail. The target is: every in-scope stream passes.
5. **Run the robustness pass** (`--mode fuzz`) until it is clean, then let it run for a long time with many seeds (section 6).

Verify (from the Summary): the pass list covers all in-scope conformance streams, and a long fuzz run (`--seeds 1000` or a shell loop over `--seed-base`) finishes without crash, hang or recovery failure. The book's equivalent of "overwrite bytes with `dd`" is the fuzz pass; you can also do it by hand: `dd if=junk of=x.264 bs=1 seek=N conv=notrunc`.

## 5. Harness input format

### 5.1 How your decoder is invoked

The harness runs a **command template** given with `--decoder`. The placeholders `{in}` and `{out}` are replaced by absolute file paths. The template is split like a shell command line (quotes work), but **no shell is used**, so write `"./mydec {in} {out}"` or `"java -jar dec.jar {in} {out}"`, not pipes or redirections. It runs in the harness's current directory. Standard input is closed; standard output and error are captured into a log (the last 300 bytes are shown on failure).

| Item | Rule |
|---|---|
| `{in}` | An Annex B byte stream file (start codes `00 00 01` / `00 00 00 01`). `.ts`/`.mp4`/`.mkv` inputs are first remuxed to Annex B with ffmpeg (`-c:v copy`); `--input-format original` hands over the original file unchanged instead. |
| `{out}` | The decoded video as raw planar yuv420p, 8 bits per sample, **cropped** to the SPS window, frames back to back (Y then Cb then Cr, rows without padding, chroma `(w+1)/2 x (h+1)/2`), in **output order**, every frame, no header. Create or overwrite the file; do not append. |
| Exit status | 0 after a normal decode. On a damaged stream any status from 0 to 127 is fine (a decoder that reports "errors seen" with status 1 is fine). A signal, a status of 128 or more (what shells report for signals), or a timeout is a failure. In the *sweep* (valid streams) any non-zero status is a failure. |
| Time | `--timeout` seconds (default 60) per run; afterwards the decoder and every process it started are killed (the decoder runs in its own process group). |
| Resources | The harness sets the core-dump size to 0 and limits the size of any file the decoder writes (`16 x` the expected output, at least 16 MiB, in the sweep; `4 x clean output + 4 MiB` in the fuzz pass). A decoder that tries to write more is killed and reported as an **output explosion**. `--mem-mb N` also limits address space. |

The x264 built-in streams are 128x96 (18432 bytes per frame); the generated ones are other small sizes, so do not hard-code a size.

### 5.2 Sources of streams

| Option | Meaning |
|---|---|
| (none) / `--builtin quick` | 35 built-in streams (generated once into `.cache/conformance/`, a few seconds). |
| `--builtin full` | All x264 variants, all scenarios of the Chapter 10.1/10.2/10.4 generators and the `common/testfiles` recipes (larger; several minutes). |
| `--stream S [--expected E]` | One stream. `E` is `.yuv` or `.md5` (decided by the extension); without it ffmpeg is the truth. |
| `--dir D` | Every file in `D` with a stream extension (`.264 .h264 .avc .26l .jsv .jvt .bit .bits .bin .ts .mp4 .mkv .mov .m4v`). Its expected output is the first of `X.yuv`, `X.YUV`, `X_dec.yuv`, `X.md5`, `X.MD5`; without any, ffmpeg is the truth (the table says which). |
| `--only a,b` | Keep only streams whose name contains one of the substrings (`--list` prints the built-in names with their feature tags). |

### 5.3 Output of the sweep

```
conformance sweep: 35 stream(s); decoder: ./mydec {in} {out}
    #  stream                                frames  truth  result detail
    1  x264_cavlc_intra                       16/16  ffmpeg PASS
    ...
    9  x264_cabac_b_pyramid                   16/16  ffmpeg FAIL   frame 5 (B) plane Y MB(3,2) x=50 y=40 expected 120 got 121
        3 of 16 compared frames differ; in frame 5: 12 sample(s) in 2 macroblock-position(s), planes Y
        only luma differs (chroma is right)
        stream: Main, CABAC, slices IPB, implicit weighted B, direct spatial
        guess (heuristic, most likely first): motion vector prediction / interpolation (Ch 7.3-7.4); ...
```

* `frames` = frames you wrote / frames expected. `truth` = `yuv`, `md5` or `ffmpeg`.
* The first mismatch is located by **frame** (0-based, output order, with the picture type I/P/B of that output frame as ffmpeg reports it), **plane** (Y, Cb, Cr), **macroblock** (x,y in 16x16 luma units, or 8x8 for chroma planes, counted in the *cropped* picture; for MBAFF read the pair row as `y/2`), and the sample position `x`,`y` within the plane with both values.
* Hints: "output frame i is identical to expected frame j" (dropped, duplicated or reordered frames), "Cb and Cr are swapped", "only luma/chroma differs", "looks UNCROPPED" (the file is exactly the right number of 16-aligned frames).
* The **guess** line is a heuristic. It reads the stream's headers (`ffmpeg -bsf trace_headers`) and combines the picture type of the failing frame, the plane and the features present (profile, CAVLC/CABAC, PAFF/MBAFF, slice types, weighted prediction, 8x8 transform, scaling lists, multiple slices, MMCO, list reordering, ...) to name the chapters most likely at fault. Treat it as a starting point.
* For `.md5` truth the verdict is the MD5. If ffmpeg's own decode matches the MD5, the difference is located against ffmpeg's yuv; otherwise the harness says so.
* A **FAIL** is also reported for: timeout (`HANG`), signal (`CRASH`), `OUTPUT EXPLOSION`, `NO OUTPUT FILE`, a non-zero exit status with correct output, and a size mismatch (frames missing or extra, or a partial last frame).

### 5.4 Output of the fuzz pass

```
fuzz base x264_cabac_b_pyramid: 16 frames, 21 NAL units, 2 IDR picture(s)
  fuzz bitflip    3/3 passed
  fuzz zerofill   3/3 passed
  ...
    FAIL x264_cabac_b_pyramid cutmid#2: WRONG AFTER RECOVERY: tail frame 0 of 6 (output frame 10, expected frame 10) differs: plane Y MB(0,0) expected 80 got 76
         damage: remove 410 bytes at offset 2210 (touches AU [3])
         must be correct again from IDR #1 on: the last 6 frame(s)
         repro: --only x264_cabac_b_pyramid --kinds cutmid --seed-base 2 --seeds 1
```

A corruption is named `<stream>.<kind>#<seed>`. It is a pure function of the stream's name (file name without extension), the kind and the seed (Python's `random.Random` seeded with the string `name|kind|seed`), so the repro line reproduces it exactly. `--keep DIR` saves the failing corrupted streams as `<name>.<kind>.<seed>.264` so you can debug with them directly. Before fuzzing a base stream, the harness runs your decoder on the clean stream; if that is not byte-identical to ffmpeg's decode the base is reported as failing and not fuzzed (fix the sweep first).

Failure labels: `HANG`, `CRASH`, `OUTPUT EXPLOSION` (also "more than 2 x clean frames + 8"), `PARTIAL FRAME` (exit status 0 but the file does not end on a frame boundary; waived when the damage hit an SPS/PPS because the picture size may legitimately change), `NO RECOVERY` (fewer frames than the recovery tail needs), `WRONG AFTER RECOVERY` (first wrong frame of the tail, with plane and macroblock).

### 5.5 The corruption kinds (exact)

All kinds work on the Annex B file (also with `--input-format original`: the fuzz pass always hands your decoder Annex B). "AU" numbers refer to the original stream.

| Kind | What is done | AUs counted as damaged |
|---|---|---|
| `bitflip` | 1 to 6 single bits flipped at random byte positions anywhere in the file. | the AU of each position |
| `zerofill` | 4 to 64 consecutive bytes set to 0. | AUs of first and last byte |
| `cutmid` | 8 bytes to 1/6 of the file removed from the middle (packet loss). | AUs at both cut ends |
| `truncate` | file cut at a random point after the first fifth. | the cut AU and all after it |
| `dropau` | all NAL units of one access unit removed (a lost picture). | that AU |
| `loseslice` | one slice NAL unit removed (from a multi-slice picture when there is one). | that AU |
| `garbage` | 8 to 160 random bytes inserted at a NAL boundary or anywhere; 60 % of the time prefixed by a start code and a random NAL header byte (never types 7/8). | AUs around the insertion |
| `noparam` | SPS/PPS NAL units removed: all of the first AU, one random one, or all. | the AUs they were in |
| `swap` | two adjacent NAL units exchanged. | both AUs |

AU boundaries are found by a simple rule: a new AU starts at an AUD/SPS/PPS/SEI after a slice, or at a slice with `first_mb_in_slice` = 0. (Streams with arbitrary slice order are not supported for fuzzing.) Seeds are numbered from `--seed-base` (default 1), `--seeds` per kind (default 3).

## 6. Running the harness

```
# the built-in sweep, then the fuzz pass (about two to three minutes with a fast decoder)
python3 test_conformance.py --decoder "./mydec {in} {out}"

# only the sweep, only some streams
python3 test_conformance.py --decoder "./mydec {in} {out}" --mode sweep --only mbaff,paff

# a conformance directory (X.264 + X.yuv or X.md5 pairs) and one extra pair
python3 test_conformance.py --decoder "./mydec {in} {out}" --mode sweep --dir ~/h264-conformance/baseline
python3 test_conformance.py --decoder "./mydec {in} {out}" --mode sweep --stream CABA3_Sony_C.jsv --expected CABA3_Sony_C.yuv

# fuzz one stream a lot: 500 seeds per kind, then continue from seed 501
python3 test_conformance.py --decoder "./mydec {in} {out}" --mode fuzz --only cabac_b_pyramid --seeds 500
python3 test_conformance.py --decoder "./mydec {in} {out}" --mode fuzz --only cabac_b_pyramid --seeds 500 --seed-base 501 --keep failures/

# the harness's own check
python3 test_conformance.py --self-test
```

The long run of the Verify step is just a loop over `--seed-base` values. Run it in a terminal you watch or with a shell `timeout`; every decoder run is limited to `--timeout`, so the harness itself cannot hang.

**Exit status**: 0 all checks passed; 1 at least one stream or corruption failed; 2 unusable input (missing or unreadable stream, expected file whose size is not a whole number of frames, no `{in}`/`{out}` in the template, decoder program that cannot be started, empty directory, unknown option value, a stream ffmpeg cannot decode).

`--fail-fast` stops at the first failure; `--verbose` also prints passing corruptions; `--guard N` changes the recovery guard; `--kinds a,b` restricts the corruptions.

### 6.1 Worked example: reading a failure

```
 12  x264_high_cqm_jvt   16/16  ffmpeg FAIL   frame 0 (I) plane Y MB(0,0) x=0 y=0 expected 61 got 57
     16 of 16 compared frames differ; in frame 0: 17408 sample(s) ...
     stream: High, CABAC, slices IPB, 8x8 transform, scaling lists
     guess (...): intra prediction / residual reconstruction (Ch 5.2-5.4); 8x8 transform and intra 8x8 (Ch 8.5); scaling lists / flat-16 fallback rules (Ch 8.6)
```

Every frame wrong from the very first pixel of an I frame, in a stream that is the only one with scaling lists: the default tables or fall-back rules (Chapter 8.6) are the first suspects; confirm with `--only cqm` and the Chapter 8.6 harness.

```
    FAIL x264_cavlc_multislice loseslice#2: NO RECOVERY: only 5 frame(s) written, 8 needed
```
The "needed" number is the length of the tail from the recovery IDR on. Fewer frames than that means the decoder gave up, or did not resynchronise at the next IDR: after a lost slice, check that the IDR with its SPS/PPS clears all state, including a half-built current picture left over from the damaged AU.

### 6.2 Typical causes

* *Crash/CRASH on `bitflip`/`garbage`*: an unchecked index (section 3.4). Run with `--keep` and your language's sanitizer or debugger on the saved stream.
* *HANG*: a loop whose exit condition depends on parsed data (slice data loop at end of data; CABAC reading past the end; skipping `mb_skip_run` forever; `ue(v)` reading zeros forever).
* *OUTPUT EXPLOSION*: a `frame_num` gap that "fills" billions of frames, or output queued for every garbage picture.
* *WRONG AFTER RECOVERY with plane/MB at (0,0) of the first tail frame*: state left over across the IDR (reference list, POC state, `frame_num`, previous-QP, an unfinished picture).
* *NO RECOVERY after `noparam`*: parameter sets must be activated by the first IDR after they arrive; a decoder that refuses all slices for ever after the first error is too strict.
* *PARTIAL FRAME*: the output writer writes a frame while it is being decoded and the error path exits before finishing it. Write only complete (possibly concealed) frames.

## 7. The built-in streams and the self-test

`--list` prints the 35 built-in streams (`--builtin full` lists all). Their prefixes: `x264_*` (x264 via ffmpeg: CAVLC/CABAC, I/P/B, pyramid, multi-slice, constrained intra, QP extremes, deblocking offsets, explicit and implicit weights, 16 references, 8x8 transform, JVT scaling lists, MBAFF), `paff_*` (Chapter 10.1 generator: PAFF with lone fields, IDR repeats, POC types 0/1/2), `fref_*` (Chapter 10.2: field reference lists, MMCO, long-term, field deblocking) and `mbaff_g_*` (Chapter 10.4 generator: MBAFF, PCM, weights, temporal direct). The generators are run through their command line from the sibling `spoiler_reference` folders; if a folder is missing its streams are skipped.

`--self-test` (about 25 seconds) uses `ref_decoder.py`, a wrapper around `ffmpeg -threads 1` that implements the interface of section 5.1, as the stand-in for your decoder, and checks:

1. the stand-in passes the sweep, the fuzz pass, a `.yuv` pair, an `.md5` pair, a directory with a container input;
2. a wrong expected yuv (one sample changed) fails and is located as frame 5, plane Y, MB(4,2), x=70, y=40, and a wrong md5 fails;
3. eleven malformed-input situations exit with 2;
4. **23 broken stand-ins** (`python3 ref_decoder.py --list-mutants`) are all caught with exit 1: one wrong luma/Cb/Cr sample, swapped chroma, grey chroma, first/last frame missing, duplicated frame, swapped frames, partial last frame, trailing bytes, empty output, no output file, non-zero exit status on a valid stream, SIGSEGV on damaged input, exit status 139, an endless loop, unbounded output, giving up after the damage, one wrong sample after recovery, one extra frame, a half-written last frame, and a decoder stuck repeating a stale frame.

You can use `ref_decoder.py` as a model for your own wrapper (it is only a short wrapper around ffmpeg; no part of the harness depends on its internals).

## 8. What's next

Chapter 10.7 (Performance) takes the working, hardened decoder and makes it fast. It reuses the sweep as its safety net: after every optimisation the whole built-in set must still be byte-identical (`--mode sweep`), and the fuzz pass must still be clean, because the usual optimisations (table lookups, SIMD, threads) are also where bounds checks get dropped. Its harness measures frames per second on a 1080p30 High-profile file and re-runs the comparison.
