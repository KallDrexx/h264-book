# Chapter 8.6 - Scaling matrices

## 1. Goal and the project

Until now every coefficient was dequantised with a flat weight of 16, so all frequencies were scaled alike. High profile lets the encoder send **scaling matrices**: tables that make some frequencies coarser than others (a typical matrix is small for low frequencies and large for high ones, because the eye notices high-frequency error less).

You build three things: (1) the SPS/PPS **syntax** that carries the matrices, (2) the **resolution** of that syntax, with defaults and fall-back rules, into six 4x4 and two 8x8 *effective* weight matrices per picture parameter set, and (3) the **use** of the right matrix in every dequantisation you already have. Nothing in the slice data changes: only pixels do. Verify with x264 streams that carry matrices (x264's `cqm=jvt` option sends the standard default tables; other options send custom lists) and with streams whose SPS/PPS the harness rewrites in every possible way; ffmpeg gives the pixels.

## 2. Big picture

A stream can say "use matrices" in the SPS (for the whole sequence) and again in the PPS (overriding per picture). Each of the eight lists can be sent, can be marked "use the standard table", or can be left out and **inherit**. The result is always one set of eight matrices.

| list i | block | used by | 4:2:0 |
|---|---|---|---|
| 0, 1, 2 | 4x4 | **Intra** Y, Cb, Cr | yes |
| 3, 4, 5 | 4x4 | **Inter** Y, Cb, Cr | yes |
| 6 | 8x8 | Intra Y | only with transform 8x8 |
| 7 | 8x8 | Inter Y | only with transform 8x8 |

(4:4:4 has six 8x8 lists; this book stays at 4:2:0, so lists 8..11 are out of scope, but a conforming parser still skips them: the SPS count is 8, or 12 if `chroma_format_idc` is 3.)

"Intra" means the macroblock is intra-predicted (I_NxN, Intra16x16, also inside P/B slices); "Inter" means P/B prediction. A matrix entry is a **weight**; 16 means "no change".

## 3. Concepts in detail

### 3.1 Syntax (7.3.2.1.1, 7.3.2.2)

You may already parse this syntax since Phase 3; now keep the values. SPS, High-profile branch (profile_idc 100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135): after `chroma_format_idc`, bit depths and `qpprime_y_zero_transform_bypass_flag` comes `seq_scaling_matrix_present_flag`; if 1, for i = 0..7 a `seq_scaling_list_present_flag[i]`, and when it is 1 a `scaling_list()` of 16 (i < 6) or 64 (i >= 6) values follows.

PPS: the optional tail after `redundant_pic_cnt_present_flag` (present only if there is more RBSP data) holds `transform_8x8_mode_flag`, `pic_scaling_matrix_present_flag`, then `6 + 2 * transform_8x8_mode_flag` present flags with their lists (4:2:0), then `second_chroma_qp_index_offset`. A PPS without the tail (Baseline/Main) has no matrices and no 8x8 transform.

### 3.2 scaling_list() (7.3.2.1.1.1)

Two variables start at 8: `lastScale` and `nextScale`. For j = 0..size-1: if `nextScale` is not 0, read `delta_scale` se(v) (range -128..127) and set `nextScale = (lastScale + delta_scale + 256) % 256`; `useDefaultScalingMatrixFlag` becomes 1 if this happened at j = 0 and `nextScale` is 0. Then entry j = `lastScale` if `nextScale` is 0, else `nextScale`; `lastScale` takes entry j.

Consequences: values are deltas from the previous entry, **modulo 256** (a delta of -128 from 8 gives 136); a 0 in the middle means "**the rest repeats the last value**" and no more deltas are read; a 0 at the very first entry means "**use the default table**" and no matrix values are sent at all.

*Example.* Deltas -2, +7, 0, +7, 0, 0, +8, -28: nextScale 6, 13, 13, 20, 20, 20, 28, then (28-28) = 0: the list is 6, 13, 13, 20, 20, 20, 28 and the other nine entries are 28. A first delta of -8 gives 0 at j = 0: default table.

### 3.3 Scan order and the four default tables

The values arrive in **zig-zag scan order** (4x4 frame zig-zag of Chapter 5.2, and its 8x8 analogue from Chapter 8.5), not raster. Placing them uses the inverse zig-zag to get `weightScale4x4[i][j]` / `weightScale8x8[i][j]`. The frame scan is used even for field macroblocks (not relevant here). In the scan order the defaults are:

- Default_4x4_Intra: 6 13 13 20 20 20 28 28 28 28 32 32 32 37 37 42
- Default_4x4_Inter: 10 14 14 20 20 20 24 24 24 24 27 27 27 30 30 34
- Default_8x8_Intra: 6 10 10 13 11 13 16 16 16 16 18 18 18 18 18 23 23 23 23 23 23 25 25 25 25 25 25 25 27 27 27 27 27 27 27 27 29 29 29 29 29 29 29 31 31 31 31 31 31 33 33 33 33 33 36 36 36 36 38 38 38 40 40 42
- Default_8x8_Inter: 9 13 13 15 13 15 17 17 17 17 19 19 19 19 19 21 21 21 21 21 21 22 22 22 22 22 22 22 24 24 24 24 24 24 24 24 25 25 25 25 25 25 25 27 27 27 27 27 27 28 28 28 28 28 30 30 30 30 32 32 32 33 33 35

Default_4x4_Intra placed in raster order is the matrix 6 13 20 28 / 13 20 28 32 / 20 28 32 37 / 28 32 37 42 (rows).

### 3.4 Fall-back: what a missing list becomes (Table 7-2)

A list that is "not present" is not an error; it inherits. Flat_16 is the matrix when **no** matrix is signalled at all (SPS flag 0 and PPS flag 0).

**SPS** (`seq_scaling_matrix_present_flag` = 1), rule **A**:

| list | not present -> | present with useDefault -> |
|---|---|---|
| 0 | Default_4x4_Intra | Default_4x4_Intra |
| 1, 2 | the previous list (0 resp. 1) | Default_4x4_Intra |
| 3 | Default_4x4_Inter | Default_4x4_Inter |
| 4, 5 | the previous list (3 resp. 4) | Default_4x4_Inter |
| 6 | Default_8x8_Intra | Default_8x8_Intra |
| 7 | Default_8x8_Inter | Default_8x8_Inter |

An SPS flag of 1 with no list present therefore gives the **default tables, not flat**. SPS flag 0 gives Flat_16 for all eight.

**PPS** (`pic_scaling_matrix_present_flag` = 1): useDefault behaves as above. For a list that is not present:

| SPS `seq_scaling_matrix_present_flag` | rule | lists 0, 3, 6, 7 | lists 1, 2, 4, 5 |
|---|---|---|---|
| 0 | A | the Default_* table (not flat) | the previous list of the PPS |
| 1 | **B** | the **SPS's resolved list** of the same index | the previous list of the PPS |

A PPS with `transform_8x8_mode_flag` 0 sends no 8x8 lists; they resolve by the same rules but are never used. `pic_scaling_matrix_present_flag` = 0 means the SPS matrices apply unchanged.

The effective set can differ per slice because slices name different PPSs; resolve against the SPS the PPS refers to.

### 3.5 Applying the matrices (8.5.9, 8.5.6 to 8.5.11)

`LevelScale4x4[m][i][j] = weightScale4x4[i][j] * normAdjust4x4[m][i][j]` and the 8x8 counterpart (the normAdjust tables you already have; with a flat 16 you got the old LevelScale). Every place that used a fixed 16 uses the matrix entry now; everything else, including shifts and rounding, is unchanged.

Which matrix: 4x4 index = (0, 1, 2 for Y, Cb, Cr) + (3 if the macroblock is inter). 8x8: 6 for an intra macroblock, 7 for an inter one (luma only). I_PCM has no residual. Two **special** cases use the 4x4 matrix's entry (0, 0), since those coefficients are DC values of whole blocks: the **Intra16x16 luma DC** levels (intra Y matrix) and the **chroma DC** levels (Cb resp. Cr matrix, intra or inter by macroblock type). Chroma AC uses the full chroma matrix; a Cb and a Cr block of one macroblock usually use different matrices.

*Worked examples* (the matrices are the defaults):
- 4x4 intra Y, qP 28 (m = 4, qP/6 = 4), level 3 at row 0, column 1: weight 13, normAdjust 20, LevelScale 260, d = (3 * 260) << 0 = 780. Flat would give 960.
- 4x4 inter Y, qP 14 (m = 2, qP/6 = 2), level 2 at (3, 3): weight 34, normAdjust 20, LevelScale 680, d = (2 * 680 + 2) >> 2 = 340.
- Intra16x16 DC, qP 30, transformed DC value 10: LevelScale(0, 0, 0) = 6 * 10 = 60, dcY = (10 * 60 + 1) >> 1 = 300 (flat: 800).
- Chroma DC, Cb intra, qP'c 25 (m = 1, qP/6 = 4), transformed DC 3: LevelScale = 6 * 11 = 66, dcC = ((3 * 66) << 4) >> 5 = 99.
- 8x8 intra Y, qP 38 (m = 2, qP/6 = 6), level 2 at (0, 0): weight 6, normAdjust8x8 26, LevelScale 156, d = (2 * 156) << 0 = 312.

Mind 16-bit overflow: large weights with low QP can leave the signed 16-bit range of Chapter 5.2; such streams are non-conforming and ffmpeg's result is then undefined. The harness streams keep weights below 60.

## 4. The project: what to build

1. Parse `seq_scaling_matrix_present_flag`, the eight `seq_scaling_list_present_flag[i]` and the lists in the SPS, and `pic_scaling_matrix_present_flag` with `6 + 2 * transform_8x8_mode_flag` lists in the PPS (a PPS tail may be absent). Implement `scaling_list()` exactly, including the modulo 256, the early nextScale = 0, and useDefault.
2. Build the four default tables; place any list through the inverse zig-zag.
3. Resolve SPS (rule A, or flat), then each PPS (inherit from the SPS, or rule A / rule B).
4. Make the dequantisers take the weight from the matrix selected by (macroblock class, colour component) and by block size, including the two DC special cases.
5. Verify: the dump (5.1) matches; then streams with matrices decode byte-identical (5.2). Milestone: T11 with `cqm=jvt` (recipe `T11_cqm`) and a few real High-profile progressive `.ts` files decode like ffmpeg, CAVLC and CABAC.

## 5. Harness input format

### 5.1 Matrix dump (`--dump FILE`)

Text, one item per line; `#` starts a comment, blank lines are ignored. For every SPS and PPS NAL unit **in stream order** print a header line and then its **effective** matrices, resolved as in 3.4 (an SPS block shows the SPS matrices; a PPS block resolved against the SPS with the id it names, as that SPS was last sent *before* it). A NAL unit byte-identical to the last listed one of the same kind and id (x264 repeats the headers at every IDR) is not listed again; a changed one is listed again with the next index. Weights are decimal integers in **raster order** (row 0 first, row after row), after the inverse zig-zag.

```
SPS <index> id=<sps_id>
PPS <index> id=<pps_id> sps=<sps_id>
M4 <k> <16 integers>      k = 0..5   (Intra Y, Intra Cb, Intra Cr, Inter Y, Inter Cb, Inter Cr)
M8 <k> <64 integers>      k = 0..1   (Intra Y, Inter Y)
```

`<index>` counts listed SPS (resp. PPS) NAL units from 0. Every block has all six `M4` and two `M8` lines, also for streams without matrices (then all 16) and for PPSs without 8x8 transform (print what the rules give, 3.4). Example, a PPS with `cqm=jvt` over a flat SPS (lines shortened):

```
PPS 0 id=0 sps=0
M4 0 6 13 20 28 13 20 28 32 20 28 32 37 28 32 37 42
M4 3 10 14 20 24 14 20 24 27 20 24 27 30 24 27 30 34
M8 0 6 10 13 16 18 23 25 27 10 11 16 18 23 25 27 29 ...
```

### 5.2 Decoded frames (`--yuv FILE`)

Exactly as in Chapter 8.5: cropped planar yuv420p, all frames, display order, no headers. `--print-expected-dump INPUT OUT` writes the expected dump for any input.

## 6. Running the harness

```
python3 test_scaling.py INPUT [--dump DUMP] [--yuv OUT.yuv]
python3 test_scaling.py --make-variants DIR      # writes the test streams below + their .expected.txt dumps
python3 test_scaling.py --self-test              # a minute or two
```

`DIR` receives two kinds of stream, each with its `.expected.txt` dump:

- **Rewritten streams** `<base>__<combination>.264`. The bases are three x264 streams: CABAC, CAVLC, and `8x8dct=0` (a Main-profile SPS/PPS that the generator upgrades to High). Slice data does not depend on the lists, so the harness rewrites only the SPS/PPS bits; ffmpeg's decode of the rewritten stream is the pixel truth. The 13 combinations:

| combination | what the headers say |
|---|---|
| `sps_full` | SPS sends all 8 lists, no PPS matrix |
| `sps_fallA` | SPS sends lists 0, 3, 6 only (rule A) |
| `sps_empty` | SPS flag 1, no list: all defaults |
| `sps_alldefault` | every SPS list is useDefault |
| `sps_mixed` | SPS mixes explicit, useDefault and absent |
| `pps_only` | flat SPS, PPS with some lists (rule A; what x264 produces) |
| `pps_empty_A` | flat SPS, PPS flag 1 with no list: all defaults |
| `pps_ruleB` | full SPS, PPS sends list 1 only (rule B) |
| `pps_empty_B` | full SPS, PPS flag 1 with no list: the SPS matrices |
| `pps_override` | SPS lists 0, 3, 6; PPS lists 0, 2, 6 |
| `pps_default` | full SPS, every PPS list useDefault |
| `wrap` | delta_scale of +-128, weights 255 and 1, an early nextScale = 0 |
| `trunc` | PPS lists ending early (runs of the last value) |

- **Real x264 streams**: `x264_jvt_cabac`, `x264_jvt_cavlc`, `x264_custom` (x264's `cqm4iy`, `cqm4ic`, `cqm4py`, `cqm4pc`, `cqm8i`, `cqm8p` options), `x264_cqmfile`, plus `T11_cqm.ts`.

Start with `pps_only`, then the SPS combinations, then rule B.

The checker compares matrix by matrix and tells you the block, the list, the first wrong row/column, and a diagnosis (flat, another list's values, a default table, scan order instead of raster, transposed, or an entry outside 1..255). Exit 0 pass, 1 mismatch, 2 unusable input or output.

## 7. Reading failures

- **Only the `wrap` streams fail, with a negative or huge weight**: you forgot the `+ 256) % 256`.
- **List ends early and the rest of it is 8 or 0**: the end marker (nextScale 0) means *repeat the last value*, not stop and zero.
- **All defaults when you expect explicit values, or the reverse**: useDefault (first delta -8) versus "list not present"; mixing the two paths in the SPS flag logic.
- **PPS-only dump right, SPS combinations wrong**: fall-back rule A chains (1 and 2 follow 0, 4 and 5 follow 3; 3, 6 and 7 do NOT follow anything). **`pps_ruleB` wrong**: lists 0, 3, 6, 7 of the PPS inherit from the SPS, not from the defaults.
- **Dump passes, pixels fail**: the wrong matrix is selected. Test chroma separately (Cr must not use Cb's), then inter versus intra macroblocks, then 8x8 versus 4x4, then the DC cases; the harness reports the first wrong macroblock, and `sps_full` makes every matrix different.
- **Only flat streams pass**: weights parsed but not used. **Streams after the first IDR drift**: a PPS re-sent with new lists was not re-resolved.
- A parse that loses its position (garbage in the last PPS fields, a wrong `second_chroma_qp_index_offset`) usually means the PPS list count (6 + 2 * transform_8x8_mode_flag) is wrong.

## 8. Phase 8 recap

Phase 8 turned the decoder from a Main-profile-only reader into a High-profile one. CABAC (8.1) added the arithmetic decoding engine and context initialisation; 8.2 to 8.4 decoded every macroblock-level syntax element, the residual blocks (coded_block_flag, significance map, levels) and the inter syntax (mvd, reference indices, B sub-macroblocks) with context selection from neighbours; 8.5 added the 8x8 transform with Intra 8x8 prediction, in CAVLC and CABAC; and this chapter makes dequantisation configurable. The decoder now decodes progressive, 8-bit 4:2:0 High-profile streams in CAVLC and CABAC, with and without scaling matrices (every x264 stream and rewritten stream of this book is bit-exact against ffmpeg). What is still missing for real broadcasts is interlace (field pictures and MBAFF, Phase 10), and the output is still a sorted-by-POC YUV file.

## 9. What's next: Phase 9

Phase 9 makes it a player. 9.1 replaces the "sort by POC" shortcut with the real output ordering (the bumping process: DPB size from the VUI or the level, flushing at IDR and MMCO 5, cropping). 9.2 converts YCbCr to RGB (BT.601 / BT.709, limited range, chroma upsampling) and writes images; 9.3 opens a native window and blits frames, with pause and single-step; 9.4 paces the frames from their presentation timestamps; 9.5 reads a live UDP transport stream, so the same decoder follows a stream as it arrives.
