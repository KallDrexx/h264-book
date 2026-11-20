# Chapter 10.4 - MBAFF reconstruction and deblocking

## 1. Goal and the project

Chapter 10.3 taught your parser to read MBAFF frames (pairs, `mb_field_decoding_flag`, Table 6-4 neighbours, CABAC / CAVLC contexts). This chapter makes the decoder **reconstruct** them: a complete MBAFF frame decoder for I, P and B slices, CAVLC and CABAC, transform 8x8, several slices, weighted prediction and both direct modes, ending in the deblocking filter with its mixed frame / field edge rules.

You extend your Chapter 8.5 decoder (frames) and Chapter 10.3 parser. The program reads a stream and writes the decoded frames in output order. The **milestone**: `T12` and the MBAFF suite below decode **byte-identical** to ffmpeg, with and without the loop filter. Note that T12 itself contains *no* field pairs (its source is progressive and x264 chooses frame pairs everywhere); the suite adds streams in which up to 98 % of the pairs are field pairs.

## 2. Big picture

A frame picture is cut into 16x32 **pairs**; every pair is coded as two frame macroblocks or two field macroblocks. A field pair covers the same 32 lines, but the *top field macroblock* owns the even lines of the pair and the *bottom field macroblock* the odd lines. Almost everything in the decoder keeps working "per macroblock", and only five things change, all in how a field macroblock sees the picture:

1. **Where its samples are** (line step 2 instead of 1).
2. **Which neighbours exist and how they are scaled** (intra availability, motion vector prediction).
3. **What it predicts from** (reference *fields* cut out of the reference *frames*).
4. **How direct modes find the co-located block** and convert vectors between frame and field units.
5. **How its edges are filtered** (mixed edges, extra top-edge filtering).

Good news from Chapter 10.3: the hard part, Table 6-4, you already have. This chapter only needs it for the *availability* of neighbouring samples and the neighbour *partitions*; the sample *positions* are simply "the nearest line of the same parity" (section 3.2).

## 3. Concepts in detail

### 3.1 Where the samples of a macroblock live

Take the pair in pair row `R` (luma lines `32R .. 32R+31`), column `C`.

| macroblock | luma line `r` (0..15) of the MB is picture line | chroma line `c` (0..7) is chroma line |
|---|---|---|
| frame, top | `32R + r` | `16R + c` |
| frame, bottom | `32R + 16 + r` | `16R + 8 + c` |
| field, top (top field MB) | `32R + 2r` | `16R + 2c` |
| field, bottom (bottom field MB) | `32R + 2r + 1` | `16R + 2c + 1` |

Worked example, pair row 2 (lines 64..95): the bottom field MB's luma line 5 is picture line `64 + 10 + 1 = 75`; its chroma line 3 is chroma line `32 + 6 + 1 = 39`. Prediction, residual addition and PCM samples all use this mapping, nothing else about "writing a block" changes.

### 3.2 Intra prediction

* **Positions.** The neighbouring samples of a field MB are the nearest lines of the *same parity*: above the MB that is picture line `first line - 2`, left are the samples at the same picture line. A frame MB uses the line directly above / the same line. (Table 6-4 gives exactly these positions; only *which macroblock* the sample belongs to varies with the pair types, and that matters for availability only.)
* **Availability.** Ask Table 6-4 (chapter 10.3) for every neighbouring sample: not available if the macroblock is not available (picture edge, other slice, not yet decoded) or, with `constrained_intra_pred_flag = 1`, is not an intra macroblock. A *side* of a block (left column, top row) counts as available only if **all** its samples are.
* **What "all" means across mixed pairs.** Frame MB next to a field pair: the left samples alternate between the two left field MBs, so under constrained intra prediction one inter MB on the left makes the whole left side unavailable. Field MB next to a frame pair: field lines 0..7 come from the top left MB, lines 8..15 from the bottom left MB. Consequence for the chroma DC predictor: each 4x4 chroma block checks *its own* four left samples, so with one intra and one inter left MB the upper two and the lower two chroma blocks may predict differently. Intra16x16 and the other chroma modes need all 16 / 8 left samples.
* **Intra 4x4 / 8x8** "later block" rule is unchanged: a neighbouring block inside the MB is available if it is earlier in decoding order; the above-right of a bottom *frame* MB is never available (the pair to its right is not decoded yet); the above-right of a bottom *field* MB comes from the pair above-right.

### 3.3 Inter prediction in field macroblocks

**Reference fields.** A field MB does not use the frame list directly. With `RefPicList` of *frames* `[F0, F1, ...]` the list of a field MB is

`refIdx 0: F0 same parity, 1: F0 opposite, 2: F1 same, 3: F1 opposite, ...`

"Same parity" means the parity of the *macroblock* (top MB of the pair = top field). So `refIdx >> 1` selects the frame and `refIdx & 1` the parity, and `ref_idx` is coded with `2 * num_ref_idx_active` values. The prediction block comes from the field plane (every second line of the reference frame, height H/2); the vertical position of the MB is `16R + y` *in that field* and the vector is in **field** quarter samples. Out-of-picture clamping uses the field height.

Example: `RefPicList0 = [A, B]`, bottom field MB, `refIdx 3` = the **top** field of B; top field MB, `refIdx 2` = the top field of B.

**Chroma vector.** A field MB that refers to the field of the *opposite* parity adds a vertical offset to the chroma vector (units 1/8 chroma sample): current bottom, reference top: `+2`; current top, reference bottom: `-2`; same parity: `0`. (This is Table 8-9 as in chapter 10.2.) Frame MBs never add an offset.

**Vector prediction between frame and field MBs** (8.4.1.3.2). A neighbouring partition's `refIdx` and vector are converted to the current MB's kind *before* they enter median prediction, P_Skip's zero test and direct prediction:

| current | neighbour | `refIdx` | vertical vector |
|---|---|---|---|
| field | frame | `refIdx * 2` | `mvy / 2`, **division toward zero** |
| frame | field | `refIdx >> 1` | `mvy * 2` |
| same kind | | unchanged | unchanged |

Example: frame neighbour `refIdx 1, mv (3, -5)` seen from a field MB: `refIdx 2, mv (3, -2)` (`-5 / 2 = -2`, not `-3`: a shift `>> 1` is wrong). Field neighbour `refIdx 3, mv (4, 7)` seen from a frame MB: `refIdx 1, mv (4, 14)`. libavcodec does the same (`/= 2` in C, truncating). `mvd` values are added in the current MB's units.

P_Skip, the median rule, the 16x8 / 8x16 directional rules and the C-then-D substitution are as in frames, applied to the converted values.

### 3.4 Direct prediction

`direct_8x8_inference_flag` is 1 in every interlaced stream, so direct works on the four 8x8 quadrants with corner 4x4 blocks. The co-located macroblock lies in `RefPicList1[0]` (a frame, itself an MBAFF frame). What matters is whether the current pair and the pair at the **same position** in the co-located frame are frame or field pairs. The table below also gives `vertMvScale` (8.4.1.2.1), which says how temporal direct converts the co-located vector: `One_To_One` (same kind, no change), `Frm_To_Fld` (frame vector used by a field MB), `Fld_To_Frm` (field vector used by a frame MB).

| cur | col pair | co-located block (quadrant `y8`, `x8`) | `vertMvScale` |
|---|---|---|---|
| frame | frame | same MB address, 4x4 block `(3*x8, 3*y8)` | One_To_One |
| field | field | same MB address (same parity MB), block `(3*x8, 3*y8)` | One_To_One |
| field | frame | MB `top` for `y8 = 0`, `bottom` for `y8 = 1` of the col pair; 4x4 row 0 (`y8 = 0`) / row 2 (`y8 = 1`), column `3*x8` | Frm_To_Fld |
| frame | field | the field MB of parity `p` of the col pair, 4x4 rows `2*bot + y8` (`bot` = 1 for the bottom frame MB, else 0), column `3*x8` | Fld_To_Frm |

`p` = bottom field if `|POC(col bottom) - POC(cur)| <= |POC(col top) - POC(cur)|`, else top (POCs of the fields of the co-located frame, `POC(cur)` = min of the current frame's field POCs).

*Spatial direct*: `refIdx` of both lists = MinPositive over A, B, C (converted as in 3.3), vectors from the 16x16 median; `colZeroFlag` (reference idx of the co-located block is 0, `RefPicList1[0]` short-term, both vector components in -1..1) forces zero vectors for `refIdx 0` per quadrant. The co-located vector and `refIdx` are used **as stored, without frame / field conversion**.

*Temporal direct* (8.4.1.2.3): take `mvCol` and the picture `refPicCol` that the co-located block referred to (a frame if the co-located macroblock is a frame MB, a field if it is a field MB). Convert the vector: `Frm_To_Fld`: `mvCol.y / 2` (toward zero); `Fld_To_Frm`: `mvCol.y * 2`. Then `refIdxL0` is the lowest index in the current list 0 that refers to the following picture (`frameIdx` = index of the frame containing `refPicCol` in the frame list `RefPicList0`):

| `vertMvScale` | `refIdxL0` refers to | value |
|---|---|---|
| One_To_One, frame MB | the frame `refPicCol` | `frameIdx` |
| One_To_One, field MB | the field `refPicCol` itself (its own parity) | `2 * frameIdx + (0 if its parity equals the current MB's parity, else 1)` |
| Frm_To_Fld | the field of `refPicCol` with the current MB's parity | `2 * frameIdx` |
| Fld_To_Frm | the frame containing the field `refPicCol` | `frameIdx` |

`refIdxL1 = 0`; an intra co-located block gives `refIdxL0 = 0` and a zero `mvCol`. POCs for the scaling: a field MB uses *field* POCs (the current frame's field of the MB's parity, the field `refIdxL0` selects, the same-parity field of `RefPicList1[0]`), a frame MB the frame POCs.

### 3.5 Weighted prediction

*Explicit*: the table entry of a field MB is `refIdx >> 1` (one entry per frame). *Implicit*: weights come from POC distances of the *fields* actually referenced and the current MB's field for field MBs, frame POCs for frame MBs; bi-predicted blocks only. Rules for the clipped weight range and long-term fallback are those of chapter 7.9.

### 3.6 Residual

Field MBs use the **field scans** (Tables 8-12 / 8-13, field column) for 4x4 and 8x8 blocks, CAVLC and CABAC alike (the CABAC context tables for field MBs were chapter 10.3). The QP chain, dequantisation and transforms are unchanged. I_PCM samples follow the line mapping of 3.1.

### 3.7 Deblocking

The filter runs per macroblock in address order (top MB, bottom MB, next pair, ...): luma vertical edges, luma horizontal edges, then chroma. An edge is **mixed** when the macroblocks on its two sides are in different pairs, one a frame pair and the other a field pair. Everything below is about which samples form `p` and `q` of each line and what `bS` and `qP` of that line are. The simplest correct structure treats **every line of samples across an edge on its own**: find the macroblock that holds `p0` from the picture geometry, then the 4x4 blocks that hold `p0` / `q0`, then `bS`, `qPp`, and apply the unchanged chapter 6.2 filter.

**Edges that exist.** The left MB edge is skipped in the first pair column, and (`disable_deblocking_filter_idc = 2`) when the left pair is in another slice. The top MB edge of a *frame* MB: skipped for the top frame MB of the first pair row, always present for the bottom frame MB (its neighbour is the top MB of its own pair). The top MB edge of a *field* MB (top **and** bottom field MB) is skipped in the first pair row and, with idc 2, when the pair above is in another slice. With `idc = 1` nothing is filtered. `alpha` / `beta` offsets are those of the slice containing `q0` (the current MB). Internal edges: 4x4 edges at 4, 8, 12; with `transform_size_8x8_flag` only 8. Chroma edges (4:2:0): MB edge and the edge at 4 (which takes the `bS` of luma edge 8), regardless of the 8x8 flag.

**Left MB edge, which `p` macroblock** (pair row lines relative to the pair; `k` = line of the current MB):

| current | left pair | line `k` of the current MB has `p0` in |
|---|---|---|
| frame (top) | frame | left top MB, line `k` |
| frame (bottom) | frame | left bottom MB, line `k` |
| field (parity `par`) | field | left field MB of the same parity, line `k` |
| frame, top / bottom MB | **field** | pair line `L = k` (+16 for the bottom MB): left field MB of parity `L & 1`, line `L >> 1` |
| field (parity `par`) | **frame** | pair line `L = 2k + par`: left top MB if `L < 16`, else left bottom MB, line `L & 15` |

Worked example, top frame MB with a field pair on the left: its lines 0, 2, 4, ... are filtered against the left *top* field MB (lines 0, 1, 2, ...), lines 1, 3, 5, ... against the left *bottom* field MB. Each line therefore has its own `qPp` and 4x4-block row, giving up to 8 different `bS` per edge.

**Chroma lines of the left edge.** Chroma line `c` uses the `bS` (and the `p` macroblock) of luma line `2c`, except for a *frame* MB next to a *field* pair, where it is luma line `2c - (c & 1)` (so that the parity of the chroma line matches the left field MB that holds its `p0`).

**Top MB edge.**

| current MB | above pair | what is filtered |
|---|---|---|
| bottom frame MB | (own pair) | top MB of the same pair, lines contiguous |
| top frame MB | frame | bottom MB of the pair above, lines contiguous |
| top frame MB | **field** | **twice**: pass 0 with `q` = lines 0, 2, 4 of the MB and `p` = the lines `-2, -4, -6` (top field MB of the pair above), `bS` from its bottom 4x4 row; pass 1 with `q` = lines 1, 3, 5 and `p` = lines `-1, -3, -5` (bottom field MB above). Intra: `bS 3`, else 2 / 1 (never 0). Chroma likewise. |
| top or bottom field MB | field | `p` = the lines `-2, -4, -6` (top MB) or `-1, -3, -5` (bottom MB) relative to the pair top, i.e. the same-parity field MB above; `q` = the MB's first three lines (pair lines `0, 2, 4` or `1, 3, 5`) |
| top or bottom field MB | frame | same lines (these lie in the *bottom frame MB* above); mixed edge |

Example, frame MB at lines 64..79 below a field pair: pass 0 filters picture line 64 (`q0`) against line 62 (`p0`, last line of the above top field MB; `p1` = line 60); pass 1 filters line 65 against line 63 (last line of the above bottom field MB). The two passes have their own `qPp` and `bS`.

**`bS`** (8.7.2.1):

| condition | bS |
|---|---|
| intra, vertical MB edge | 4 (also when field MBs are involved) |
| intra, horizontal MB edge, both macroblocks frame MBs | 4 |
| intra, horizontal MB edge with any field MB (also the double top edge) | 3 |
| intra, internal edge | 3 |
| 4x4 block (8x8 block with the 8x8 transform) of `p0` or `q0` has non-zero coefficients | 2 |
| mixed edge | 1 |
| otherwise by vectors | 1 if different reference *pictures*, different count of vectors, or `|dx| >= 4`, or `|dy| >= 4` for frame MBs / `>= 2` for field MBs (each in its own quarter-sample units; 4 quarter frame lines = 2 quarter field lines), else 0 |

The reference *picture* of a field MB is a **field** (frame, parity): the two fields of one frame are different pictures; a frame MB refers to the frame. Both macroblocks of a non-mixed edge have the same kind, so they are compared like with like. The vertical limit follows the *current* MB.

`qPp` and `qPq` are the QPs of the macroblocks holding `p0` and `q0` (0 for I_PCM), chroma through the chroma QP table per plane; `qPav = (qPp + qPq + 1) >> 1` as before.

## 4. The project: exactly what to build

Extend the decoder in this order; each step is testable with the harness. Make the output without deblocking (channel B, section 5) right first, then the deblocked one (channel A):

1. Pair geometry and PCM; intra prediction of I pictures with the availability rules (`g_i_*`, `g_pm8_*`, `g_cip_*`).
2. Field scans; P pictures: reference fields, converted neighbour vectors, P_Skip, chroma offset (`g_p_*`, `g_ref_*`).
3. B pictures: spatial direct with all four co-located cases, temporal direct, weighted prediction (`g_b_*`, `g_temporal_*`, `g_wp_*`).
4. Deblocking: first frame pairs only (`x_still`), then the mixed edges.
5. The x264 streams and T12.

Verify: `T12` and the whole suite byte-identical, channel A and B.

## 5. Harness input format (what your program must write)

* **Channel A, `READER_YUV`**: all frames in **output order**, 8-bit 4:2:0 planar (Y, then Cb, then Cr per frame), **cropped** per the SPS (`frame_crop_*`; with `frame_mbs_only_flag = 0` the crop unit is 4 luma lines vertically), no padding or headers. Output order = ascending POC inside each IDR period.
* **Channel B (optional), `--nodeblock-yuv`**: the same, but decoded with the deblocking filter **disabled in every picture** (including the pictures used as references): compare with `ffmpeg -skip_loop_filter all`.
* **Channel C (optional), `--mv-dump`**: text, one line per macroblock, output frame by output frame, addresses ascending (MBAFF addresses: `2 * pair + (0 top, 1 bottom)`):

  `MV <output frame> <addr> fld=<0|1> intra`

  `MV <output frame> <addr> fld=<0|1> l0=<16 cells> l1=<16 cells>`

  Cells are separated by `/`, in raster order of the 4x4 blocks (`y*4 + x`), each `-` (list not used) or `<refIdx>:<mvx>,<mvy>` with `refIdx` and the vector in the macroblock's own units (field MB: field index, field quarter samples), all decimal. Both `l0=` and `l1=` are always written (in P slices `l1` is 16 `-`). Skipped and direct macroblocks appear with their derived values. I_PCM macroblocks are `intra`. Empty lines and lines starting with `#` are ignored; lines are compared in order, so every macroblock of every output frame must be present.

Example line: `MV 3 12 fld=1 l0=0:2,-3/0:2,-3/... l1=-/-/...`.

## 6. Running the harness

```
python3 test_mbaff_recon.py STREAM READER_YUV [--nodeblock-yuv F] [--mv-dump F]
python3 test_mbaff_recon.py --make DIR                       # the suite: 32 synthetic + 19 x264 + T12
python3 test_mbaff_recon.py --check DIR OUTDIR [--only a,b]  # OUTDIR/<case>.yuv (+ .nodeblock.yuv, .mv) for every DIR/<case>
python3 test_mbaff_recon.py --suite DIR --reader './mydec {stream} {yuv} {nodeblock} {mv}' [--only a,b]   # --make, run, --check
python3 test_mbaff_recon.py --expected STREAM OUTDIR         # truth files (ffmpeg yuv, no-deblock yuv, mv dump)
python3 test_mbaff_recon.py --stats STREAM                   # field / frame pair statistics
python3 test_mbaff_recon.py --self-test                      # takes about 5 minutes
```

Exit codes: 0 pass, 1 wrong output, 2 missing or malformed output (a yuv file that is not a whole number of cropped frames, a vector dump line that breaks the grammar of section 5) or a stream the harness cannot use (no MBAFF frames).

`--self-test` proves the harness and the reference: the reference decoder (`spoiler_reference/mbdec.py`, with `mbdeblock.py`) equals ffmpeg on all 32 + 19 streams and T12 with and without the loop filter (about 4900 field and 12700 frame pairs in the quick set), the harness rejects corrupted readers with the right diagnosis, the reference's vectors equal those exported by libavcodec, and each of 40 deliberately wrong rules (`mbdec.ALL_MUTANTS`: line interleave, reference parity, `>> 1` instead of division toward zero, `refIdx` scaling, chroma offset sign, co-located choices, field POCs, `refIdx >> 1` weights, field scan, intra availability, 15 deblocking rules) is caught by the pixel comparison.

The truth is ffmpeg (`-threads 1`; frame- and slice-threaded decodes are byte-identical on these streams). Failures are reported as: first wrong frame (output order and picture types), plane, sample, **macroblock pair, frame or field pair, top / bottom (field) MB and line**, followed by hints derived from where *all* wrong samples are (only field pairs; only odd lines; all within three samples of a macroblock edge; chroma only), and a classification: channel B wrong = reconstruction wrong; B right and A wrong = deblocking wrong; output equal to the no-deblock decode = filter not applied.

The suite streams. *Synthetic* (`spoiler_reference/mbgen.py`, chapter 10.3's random-syntax generator extended with temporal / mixed direct and explicit / implicit weights): every bin is random but legal, so they hit rare paths (PCM, constrained intra with mixed pairs, extreme vectors). *x264*: `-profile high|main -x264-params interlaced=1:tff=1` (and `bff`) with CABAC / CAVLC, 0-5 B frames, pyramid, temporal / spatial / auto direct, `weightp`, `weightb`, several references, 1-5 slices, adaptive quantisation, `qp` 18..38, deblocking strengths, a stream with cropping (160x120) and a still image; sources are interlaced test patterns with different motion per field (so x264 picks field pairs) and noise. Field-pair share: 45-98 % in the moving / noisy streams, 0 % in `x_still`, `x_life` and T12.

## 7. Reading failures

* **Only field pairs wrong, B also wrong**: line mapping (3.1), reference field choice (3.3), vector conversion; compare channel C.
* **Wrong only in the first line(s) of pairs below a field pair**: double top-edge filtering.
* **Wrong columns 0-2 / 13-15 of macroblocks next to a pair of the other kind**: left-edge line mapping, per-line `qP` / `bS`.
* **Whole stream wrong from the first B picture, P fine**: list 1, direct co-located choice, implicit weights.
* **Skip / direct MB wrong in a field pair**: converted neighbour data (division toward zero!) and `refIdx` conversion.
* **Chroma only**: chroma offset of field MBs, chroma left-edge lines.

## 8. Where ffmpeg (8.0.1) deviates from the standard (known; the suite avoids these)

* **Constrained intra prediction + MBAFF + Intra 8x8 next to mixed pairs.** In 80 random streams with `constrained_intra_pred_flag = 1` and the 8x8 transform, 9 differ from ffmpeg (none of 120 without the 8x8 transform). Analysed case: a bottom frame MB, left pair a field pair whose top MB is inter and bottom MB intra, Intra 8x8 block 2: the left column is unavailable, but the above-left sample (line 7 of the MB = odd line = the intra bottom field MB) is available in the standard, which makes the reference-sample filter use it; ffmpeg treats it as unavailable. Other configurations (not analysed) also differ. x264 itself chooses Intra 8x8 modes that are illegal under the standard's reading in such streams (ffmpeg prints "left block unavailable for requested intra4x4 mode"). The reference decoder follows the standard and the suite uses no 8x8 transform with constrained intra (an experiment that made the above-left sample unavailable in the analysed case fixed only some of the differing streams, so ffmpeg's exact rule is not reproduced).
* **Temporal direct, co-located picture with several slices of different list sizes**: libavcodec keeps one table of reference pictures per picture ("FIXME need per slice") and maps the co-located reference wrongly. The generator gives all slices of a picture the same list size when direct is exercised.
* **Weighted prediction with `w0 + w1 > 128`** (forbidden by 7.4.3.2): the SSE2 code saturates in 16 bits. The generator keeps weights <= 64.
* **Coefficient overflow** beyond 16 bits (non-conforming streams): the `g_qp_*` scenario produces some; the suite skips such seeds and the reference counts them.
* Exported motion vectors (`+export_mvs`) double the vertical component of field macroblocks with 16x8 / 8x16 partitions and export a zero vector for the unused list; the self-test accounts for both.

## 9. What's next

Chapter 10.5 shows interlaced video for people: reading the field order and picture timing information, and displaying frames with a bob or motion-adaptive deinterlacer instead of weaving the two fields.
