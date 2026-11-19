# Chapter 4.1 - Slice data loop and mb_type

## 1. Goal & the project

Phase 3 left you with everything *around* the pictures: parameter sets, slice headers, picture order. Phase 4 reads what is *inside* them: the macroblock data, in CAVLC (the variable-length coding that Baseline and CAVLC-Main streams use).

In this chapter you build the outer loop of `slice_data()` for I slices and read the first syntax element of every macroblock, `mb_type`. You decode it into its meaning (which kind of macroblock, which intra prediction mode, which coded block pattern) and you handle the one macroblock kind that carries raw pixels, `I_PCM`. Your program prints one line per macroblock it reads and a histogram of the `mb_type` values.

Honest scope: after `mb_type` a normal macroblock continues with syntax you build in chapters 4.2 to 4.4 (prediction modes, QP delta, coefficients). So this chapter's reader **reads macroblocks until it meets one it cannot finish yet, prints that one, and stops the slice**. Section 4 defines "cannot finish yet" precisely, so the output is deterministic and checkable. In chapter 4.4 the same reader will never stop early, and the whole-slice check ("the last macroblock ends exactly on `rbsp_trailing_bits`") takes over.

What you have from Phases 1 to 3, and use here:

- The Annex B NAL unit list (Phase 2), either from a transport stream (Phase 1) or from a raw `.264` file.
- Each NAL unit's RBSP bytes, with emulation prevention removed, and a bit reader with `u(n)`, `ue(v)`, `se(v)`, a way to test byte alignment, and `more_rbsp_data()` (Chapter 2.2).
- Parsed SPS, PPS and slice headers (Phase 3). From them you need: `slice_type`, `first_mb_in_slice`, `entropy_coding_mode_flag`, the picture size in macroblocks (`PicSizeInMbs`), the chroma format and bit depth, and **where `slice_data()` starts**: the bit position right after the last slice-header field (`header_end_bit`; with CAVLC there is no alignment padding before the data, so it is simply the next bit). You need it, and `PicSizeInMbs`, for every slice, including the P, B and CABAC slices this chapter skips.

## 2. Big picture

A slice is a run of macroblocks. A macroblock is a 16x16 block of luma samples plus its two 8x8 chroma blocks (4:2:0), 384 samples in all. The bits of a slice after the header are the macroblocks one after another, with no length fields and no markers between them. The only way to find where macroblock 5 starts is to parse macroblocks 0 to 4 completely. That is why one parsing mistake destroys the rest of the slice, and why Phase 4 is built as a chain of small verified steps.

Every macroblock begins with `mb_type`, a `ue(v)` number that says what kind of macroblock follows:

- **I_NxN**: "intra, predicted in 4x4 (or 8x8) pieces". Sixteen (or four) small prediction modes follow, then the coded residual.
- **I_16x16**: "intra, predicted as one 16x16 block". The single `mb_type` value also packs three facts: the 16x16 prediction mode, whether the chroma has coefficients, and whether the luma has coefficients.
- **I_PCM**: "no prediction, no transform": the samples follow as raw bytes. An escape hatch for content that compresses badly.

(P and B slices add skipping and inter types; that is Phase 7.)

## 3. Concepts in detail

### 3.1 slice_data() for an I slice (7.3.4)

In pseudo-steps, for a CAVLC I slice with no FMO/ASO (the only kind in scope):

1. `CurrMbAddr = first_mb_in_slice`. (With MBAFF it would be doubled; MBAFF and field pictures are out of scope here, see section 4.)
2. Read one `macroblock_layer()` for `CurrMbAddr`.
3. Ask `more_rbsp_data()`. If it is false the slice is over.
4. Otherwise `CurrMbAddr = CurrMbAddr + 1` (with FMO it would follow the slice-group map; without FMO it is just +1), and go to step 2.

`more_rbsp_data()` (7.2) is true if there are bits left before the `rbsp_stop_one_bit`, the last 1 bit of the RBSP. It is your Chapter 2.2 function. Note what is *not* in the loop: no macroblock counter. A slice ends because the data ends. Its macroblock count is a result, and a useful sanity check: `first_mb_in_slice + count` must not exceed `PicSizeInMbs`, and the last slice of a picture must end at exactly `PicSizeInMbs`.

In P and B slices the loop starts with `mb_skip_run` (a count of skipped macroblocks). I slices never skip. Ignore it until Phase 7.

Macroblock addresses count in raster order: address = row * width_in_MBs + column.

### 3.2 mb_type in I slices (Table 7-11)

`mb_type` is `ue(v)`, so I slices use values 0 to 25:

| mb_type | name | meaning |
| --- | --- | --- |
| 0 | I_NxN | 4x4 prediction (8x8 with `transform_size_8x8_flag`, High profile) |
| 1 to 24 | I_16x16_*predmode*_*chroma*_*lumaflag* | 16x16 prediction, see below |
| 25 | I_PCM | raw samples |

For 1 to 24, let `t = mb_type - 1`. Then:

| field | formula | values |
| --- | --- | --- |
| Intra16x16PredMode | `t mod 4` | 0 vertical, 1 horizontal, 2 DC, 3 plane |
| CodedBlockPatternChroma | `(t div 4) mod 3` | 0 no chroma coefficients, 1 only chroma DC, 2 chroma DC and AC |
| CodedBlockPatternLuma | `0` if `t < 12`, else `15` | 0 no luma AC coefficients, 15 all sixteen 4x4 luma blocks may have AC |

So `mb_type` 1 to 4 are the four prediction modes with no residual flags, 5 to 8 add chroma class 1, 9 to 12 chroma class 2, and 13 to 24 repeat all that with luma pattern 15. The name uses the spec's spelling: `I_16x16_<predmode>_<chromaCBP>_<lumaflag>` with the luma flag 0 or 1 (1 means pattern 15). Examples: type 1 is `I_16x16_0_0_0`, type 7 is `I_16x16_2_1_0`, type 24 is `I_16x16_3_2_1`.

Why the packing? Intra16x16 does not send a separate `coded_block_pattern` syntax element later; the two CBP values live in `mb_type`. Chapter 4.2 builds the separate CBP element that I_NxN uses.

(Reference only, for Phase 7: in a P slice the I types are offset by 5, in a B slice by 23. `mb_type` 5 in a P slice is `I_NxN`. Never apply this offset to I slices.)

### 3.3 I_PCM (7.3.5, 7.4.5)

After `mb_type` = 25:

1. `pcm_alignment_zero_bit`s: zero bits until the bit position is a multiple of 8 (counted from the first bit of the RBSP/NAL unit, so after emulation prevention is removed). Between 0 and 7 bits; they must all be 0.
2. 256 luma samples (16x16, raster order), then 64 Cb samples (8x8), then 64 Cr samples (8x8). For this book's 8-bit 4:2:0 content each is one byte. 384 bytes in total.
3. Nothing else: the macroblock is finished, and the next macroblock (or the stop bit) follows immediately, at a byte-aligned position.

The raw samples are not scaled or filtered by the transform path, which is what makes `I_PCM` such a good test: what you read is exactly the picture content (Phase 5 will copy it straight into the frame).

### 3.4 Tail of a macroblock: the zero-residual Intra16x16 "skeleton"

To read more than one macroblock per slice we need at least one non-PCM macroblock we can finish today. The simplest is an Intra16x16 macroblock whose chroma pattern and luma pattern are both 0 (`mb_type` 1 to 4: `I_16x16_p_0_0`). After `mb_type` it contains exactly three more things:

1. `intra_chroma_pred_mode`: `ue(v)`, value 0 to 3. (Chapter 4.2 explains the modes.)
2. `mb_qp_delta`: `se(v)`. (Chapter 4.2.)
3. The `Intra16x16DCLevel` block, which every Intra16x16 macroblock has even when it has no coefficients: it is a `coeff_token`. With `TotalCoeff = 0` and a neighbourhood count `nC` below 2 (chapter 4.3 defines `nC`), that token is the single bit `1`.

You do not need to understand 2 and 3 yet, only to consume them. If the bit where the `coeff_token` should be is `0`, the block has coefficients and you cannot finish the macroblock yet (see the stop rule below). In the real streams `x264` makes from a flat picture, essentially every macroblock is such a skeleton, so whole slices parse; in the synthetic streams from this chapter's generator, they are mixed with the other kinds.

## 4. The project: exactly what to build

Write a program that takes a `.ts` or raw `.264` file and writes a dump file (section 5).

**Which slices you parse.** Slice NAL units are those with `nal_unit_type` 1 and 5. Number them 0, 1, 2, ... in stream order (the *slice index*). You parse a slice only if all of these hold, otherwise you report it as `skipped` (a `SLICE` line and an `END` line with `mbs=0`):

- `slice_type` mod 5 is 2 (an I slice: values 2 or 7)
- CAVLC (`entropy_coding_mode_flag` = 0)
- a frame picture, not a field picture and not MBAFF
- 8-bit 4:2:0 (`chroma_format_idc` = 1, both bit depths 8)

**Parsing a slice** (start at `header_end_bit`, address `first_mb_in_slice`):

- Read `mb_type` with `ue(v)`. A value above 25 is an error: stop, status `error`. A macroblock address at or beyond `PicSizeInMbs` is also an error, and so are a read past the end of the NAL unit, a `pcm_alignment_zero_bit` that is 1, and an `intra_chroma_pred_mode` above 3. The macroblock at which an error happens gets no `MB` line. (None of this happens in the test streams; it only keeps a buggy reader from looping forever.)
- Print the `MB` line for this macroblock (the bit position is where its `mb_type` started).
- If `mb_type` is 25 (`I_PCM`): read the alignment bits and the 384 samples; print the `PCM` line (alignment bit count and the sums of the samples per plane); the macroblock is finished.
- Otherwise, **the stop rule**. A non-PCM macroblock is finished only if it is an Intra16x16 skeleton (`mb_type` 1 to 4) whose three elements of 3.4 are present, with the `coeff_token` bit being `1` (consume all of it). Everything else (`I_NxN`; Intra16x16 with either pattern non-zero; a skeleton whose DC `coeff_token` bit is `0`) cannot be finished: **stop** the slice with status `stopped`. The `MB` line of the macroblock you stopped at *is* printed and counted. Do not look at any bit after it.
- One more stopping case, so that you never have to compute `nC` next to I_PCM: **once an `I_PCM` macroblock has been read in this slice, any later macroblock that is not `I_PCM` stops the slice** (print its `MB` line, status `stopped`). The reason: a block next to an `I_PCM` macroblock counts as having 16 coefficients, which changes the `coeff_token` code table. Chapter 4.3 removes this restriction.
- After a finished macroblock call `more_rbsp_data()`. If false, status `complete`. If true, go to the next address.

In summary: the macroblocks you read are some skeletons and PCMs, then possibly one macroblock you stop at. In a stream with a real `x264` encode of normal video you usually stop at the very first macroblock of each slice; in the other test streams you read up to hundreds.

**Histogram.** After the last slice print the `mb_type` histogram over every `MB` line you printed (including those you stopped at).

**Verify (replacing the Summary's "compare the histogram with the JM trace").** JM is not available here and ffmpeg's `trace_headers` ends at the slice header, so the ground truth is built differently; section 6 explains the layers. Note two things the Summary got wrong or could not know: on `T1` (`partitions=none`) x264 still produces `I_NxN` macroblocks (mixed with Intra16x16; ffmpeg's own macroblock map shows both), so "every macroblock should be I_16x16" does not hold; and a whole-slice `mb_type` histogram needs the residual parser, so it arrives in chapter 4.4, in this same output format.

## 5. Harness input format

One text file, UTF-8/ASCII, one record per line, fields separated by single spaces. Blank lines and lines starting with `#` are ignored. Numbers are decimal, no leading `+`. Every `key=value` pair has no spaces inside. **Extra keys on `SLICE`, `MB`, `PCM` and `END` lines are allowed and ignored** (later chapters add keys), and the keys of one line may come in any order. An unknown line type is an error.

```
SLICE <slice_index> type=<I|P|B|SP|SI> first_mb=<n> pic_size_in_mbs=<n> header_end_bit=<n>
MB <slice_index> <mb_addr> mb_type=<raw> name=<NAME> i16_mode=<0-3|-> chroma_cbp=<0-2|-> luma_cbp=<0|15|-> bit=<n>
PCM <slice_index> <mb_addr> align_bits=<0-7> sum_y=<n> sum_cb=<n> sum_cr=<n>
END <slice_index> mbs=<count> status=<complete|stopped|skipped|error>
HIST <mb_type>:<count> <mb_type>:<count> ...
```

- Per slice, in slice index order: one `SLICE` line, then its `MB` lines in decoding order (each `I_PCM` macroblock's `PCM` line right after its `MB` line), then one `END` line. Every slice NAL unit (type 1 and 5) has a block, including skipped ones.
- `SLICE`: `type` is the slice type name for `slice_type mod 5` (0 P, 1 B, 2 I, 3 SP, 4 SI). `first_mb` is the first macroblock address, `first_mb_in_slice * (1 + MbaffFrameFlag)` (just `first_mb_in_slice` except in MBAFF streams). `pic_size_in_mbs` is `PicSizeInMbs` = `PicWidthInMbs * FrameHeightInMbs / (1 + field_pic_flag)`. `header_end_bit` is the bit position right after the last slice-header field, counted in bits from the first bit of the NAL unit header byte, in the unescaped bytes (if your reader numbers bits from the start of the RBSP after the one-byte header, add 8). This is the same number as in Chapter 3.3: for CAVLC it is the first bit of `slice_data()`; for a CABAC slice (skipped here, but its `SLICE` line is still checked) it is the position *after* the `cabac_alignment_one_bit`s, i.e. the next multiple of 8.
- `MB`: `mb_addr` is the macroblock address. `mb_type` is the raw value. `name` is `I_NxN`, `I_PCM` or `I_16x16_<predmode>_<chromaCBP>_<lumaflag>` (3.2). `i16_mode`, `chroma_cbp`, `luma_cbp` are the decoded values for `I_16x16` (`luma_cbp` is 0 or 15), and the single character `-` for `I_NxN` and `I_PCM`. `bit` is the position (same numbering as `header_end_bit`) of the first bit of that macroblock's `mb_type`.
- `PCM`: `align_bits` is how many `pcm_alignment_zero_bit`s you skipped. `sum_y`, `sum_cb`, `sum_cr` are the plain sums of the 256, 64 and 64 sample bytes.
- `END`: `mbs` is the number of `MB` lines of the slice; `status` is one of: `complete` (read until `more_rbsp_data()` was false), `stopped` (the stop rule), `skipped` (slice not handled, `mbs=0`), `error` (see section 4).
- `HIST`: one line, last line of the file. Pairs `<raw mb_type>:<count>` in ascending `mb_type` order, only non-zero counts, over all `MB` lines. With no macroblocks at all the line is just `HIST`.

Example (first lines of the expected output for `T1`, then for a flat picture where whole slices parse, then an `I_PCM` from the synthetic stream):

```
SLICE 0 type=I first_mb=0 pic_size_in_mbs=396 header_end_bit=32
MB 0 0 mb_type=0 name=I_NxN i16_mode=- chroma_cbp=- luma_cbp=- bit=32
END 0 mbs=1 status=stopped
...
HIST 0:50

SLICE 0 type=I first_mb=0 pic_size_in_mbs=396 header_end_bit=32
MB 0 0 mb_type=3 name=I_16x16_2_0_0 i16_mode=2 chroma_cbp=0 luma_cbp=0 bit=32
MB 0 1 mb_type=3 name=I_16x16_2_0_0 i16_mode=2 chroma_cbp=0 luma_cbp=0 bit=40
...
MB 0 395 mb_type=1 name=I_16x16_0_0_0 i16_mode=0 chroma_cbp=0 luma_cbp=0 bit=2446
END 0 mbs=396 status=complete

MB 25 7 mb_type=25 name=I_PCM i16_mode=- chroma_cbp=- luma_cbp=- bit=98
PCM 25 7 align_bits=5 sum_y=32785 sum_cb=8147 sum_cr=8727
MB 25 8 mb_type=23 name=I_16x16_2_2_1 i16_mode=2 chroma_cbp=2 luma_cbp=15 bit=3184
END 25 mbs=9 status=stopped
```

(Bit 3184 is 98 + 5 + 3072 + 9: the `mb_type` of the PCM macroblock took 9 bits (`ue(25)`), 5 alignment bits and 384 bytes follow.)

## 6. Running the harness

```
python3 test_mbtype.py <input.ts|input.264> <mb_dump.txt>
python3 test_mbtype.py --make-variants <out_dir>          # extra inputs, see below
python3 test_mbtype.py --print-expected <input> <out.txt> # write the expected dump, to `diff` with yours
python3 test_mbtype.py --self-test                        # the harness checks itself
```

Exit code 0 pass, 1 fail (the first ten differences are listed with slice, address, field, expected and actual), 2 malformed or missing output or a harness problem.

The inputs to try, in order: `.cache/T1.ts` (x264 starts every slice with an `I_NxN` macroblock, so you read one macroblock per slice: a PASS here proves your slice list, `header_end_bit` and the first `ue(v)`, not much more), `T2`, `T4` (4 slices per picture, so `first_mb` is not 0), `default.ts` (P and B slices must be `skipped`; only its I slices are read), `T10_full.ts` (CABAC: `skipped`), and from `--make-variants`:

- `mb_synth.264`: a synthetic stream (96x80, Baseline, CAVLC) with 41 IDR pictures: all `mb_type` values 0 to 25, PCM at every alignment 0 to 7, skeleton runs, mixed slices, and a skeleton right after an `I_PCM` (tests the "after an `I_PCM`, stop" rule). It is a raw Annex B file (like the Chapter 2.1 raw output), so read the NAL units straight from it. 26 of its pictures put a macroblock of `mb_type` t, for each t from 0 to 25, right after a run of skeletons, so your decomposition of every type is tested.
- `mb_flat.ts` and `mb_flat_slices.ts`: x264 encodes of a flat gray picture, 1 and 4 slices per picture. All macroblocks are zero-residual Intra16x16, so every slice ends `complete`: your loop, `more_rbsp_data()` and the slice end are tested on real encoder output.

**What the harness compares against, and why you can trust it.** The expected output comes from `common/cavlc_ref.py`, a reference parser written for this book. Nothing in this repository can prove such a parser correct by itself, so before it is used the harness cross-checks it against three outside witnesses. Each gives a different guarantee:

| check | applies to | guarantee |
| --- | --- | --- |
| ffmpeg's macroblock map (`ffmpeg -debug mb_type`, parsed by the harness) | every input, every macroblock the reference printed | the class (Intra4x4 / Intra16x16 / PCM) of every macroblock, at its address, agrees with a mature decoder. This is as close to a "JM trace" as ffmpeg offers; it does not show the 16x16 mode, the CBP or the exact `mb_type` value |
| generator manifest | `mb_synth.264` | the exact `mb_type`, the 16x16 mode and CBP, the bit position of each macroblock, the `header_end_bit` and the PCM samples, as written by an independent bit writer (it shares no code with the reference parser) |
| ffmpeg decoding of the synthetic stream | `mb_synth.264` | the stream is valid: ffmpeg prints no error (it fails loudly on any misplaced bit, tested by corrupting the generator on purpose), and every `I_PCM` macroblock's decoded pixels equal its raw samples, so the PCM position and alignment are right |

A fourth, strongest guarantee comes later: from chapter 4.4 on, every slice of every file must end exactly on `rbsp_trailing_bits`, which no wrong parser achieves by accident. For this chapter the exact-end property is covered where it can be: every `complete` slice of the synthetic stream must end after exactly the macroblocks the generator wrote, and the `complete` slices of `mb_flat*.ts` must cover whole pictures (396 macroblocks) with `more_rbsp_data()` false only after the last one.

If one of the cross-checks fails, the harness prints "HARNESS PROBLEM" and exits 2: that is a bug in the book's tooling, not in your reader.

To look at ffmpeg's macroblock map yourself: `ffmpeg -v repeat+debug -threads 1 -debug mb_type -i T1.ts -f null - 2> map.txt`. It prints one character per macroblock per picture: `i` Intra4x4, `I` Intra16x16, `P` PCM (`S`, `d`, `<`, `>`, `X` are inter classes, Phase 7). Pictures appear in output order, and the first few appear twice because ffmpeg decodes them once while probing. `-debug qp` prints every macroblock's QP (useful in chapter 4.2). No ffmpeg or ffprobe option shows the `mb_type` number, the 16x16 prediction mode, the CBP or any residual, which is why the reference parser is needed.

## 7. Reading failures / debugging tips

- **Everything is off from the very first `SLICE` line**: check `header_end_bit` first. A wrong value (8 bits too small if you number from the RBSP) makes every later field wrong.
- **First `MB` line right, later ones wrong**: your skeleton tail is the suspect. Count the bits: `ue(3)` for the type takes 5 bits, so the flat stream's first macroblock is 8 bits in all (5 + 1 + 1 + 1); `bit=32`, `bit=40`, `bit=48`, ... in the example. A `bit` mismatch at the second macroblock tells you how many bits your parse of the first one consumed.
- **`mb_type` right but `name`, `i16_mode`, `chroma_cbp`, `luma_cbp` wrong**: re-check the formulas in 3.2. A frequent slip is using `mb_type` instead of `mb_type - 1`. `chroma_cbp` is `(t div 4) mod 3`, not `t div 4`.
- **PCM `sum_*` wrong**: usually alignment. The alignment is relative to the start of the NAL unit's bytes. Check `align_bits` first; the `bit` of the *next* `MB` line must be the PCM macroblock's `bit` + `ue` length + `align_bits` + 3072.
- **`END` says `stopped` where the expected says `complete` (or the reverse)**: the stop rule in section 4. `complete` is only for slices that ran to `more_rbsp_data()` being false.
- **Slice count wrong**: slice NAL units are types 1 and 5 only; every one gets a block, even a skipped one.
- Use `--print-expected` and `diff` for a full picture.

## 8. What's next

Chapter 4.2 adds the rest of the intra macroblock syntax: prediction modes for I_NxN, `intra_chroma_pred_mode`, `coded_block_pattern` and `mb_qp_delta`, so the `I_NxN` macroblocks no longer have to stop the slice.
