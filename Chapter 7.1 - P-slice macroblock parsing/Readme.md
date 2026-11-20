# Chapter 7.1 - P-slice macroblock parsing

> **Spoiler warning: do not open `common/inter_ref.py` or `spoiler_reference/`.** `common/inter_ref.py` is the reference
> parser the harness compares you with: a complete solution of this chapter. `spoiler_reference/pgen.py` is the
> synthetic-stream generator; it writes every P macroblock in syntax order, so it gives away the same rules (Table 9-4 inter
> column, te(v), nC). Read neither before your own parser passes. Everything you need is in this Readme.

## 1. Goal and the project

After Phase 6 you have a complete **intra** decoder. CAVLC I slices decode, byte for byte, to what ffmpeg produces on T1-T4: macroblock syntax, intra prediction, inverse
transforms, deblocking. You also parse every header (SPS, PPS, slice header with `num_ref_idx_active`, the
`ref_pic_list_modification` fields, `pred_weight_table`, `dec_ref_pic_marking`) and you compute picture order count.

This chapter teaches your parser to read **P slices**: the macroblock syntax of inter-coded pictures. You will parse `mb_skip_run`, the P
`mb_type`s, `sub_mb_type`, `ref_idx_l0`, `mvd_l0`, the inter coded-block-pattern mapping, and intra macroblocks that appear
inside P slices. You do **not** compute motion vectors or pixels yet. The output is a text dump of every macroblock's
syntax elements, which the harness compares with a reference parser. The goal is that every P slice ends exactly on its
`rbsp_trailing_bits`. That is the strongest check available for syntax parsing: a single wrong bit anywhere desynchronises the slice.

## 2. Big picture

An I slice describes every macroblock from scratch. A **P slice** (predictive) describes most macroblocks as "copy a
block of pixels from an earlier picture, shifted by a motion vector, then add a small correction". Per macroblock the
bitstream therefore carries:

* **whether to copy at all**: a macroblock can be **skipped**: no data, meaning "copy from the first reference picture,
  shifted by a predicted motion vector, no correction". Static areas cost almost nothing. Skips are coded in runs (`mb_skip_run`).
* **how the macroblock is cut into partitions** (one 16x16 block, two 16x8, two 8x16, or four 8x8 which may be cut further into 8x4, 4x8, 4x4). Each
  partition has its own motion vector.
* **which earlier picture each partition copies from** (`ref_idx_l0`, an index into the reference list built in Chapter 7.2).
* **a motion vector difference `mvd_l0`** per partition (x and y in quarter samples). The actual vector is a prediction from
  neighbours plus this difference, which is Chapter 7.3. For now `mvd` is just two signed numbers to read.
* the same residual as in I slices (coded block pattern, `mb_qp_delta`, coefficient blocks).
* alternatively an **intra macroblock**: a P slice may still contain I macroblocks (new content, scene changes). They use the
  I `mb_type` table shifted by 5.

What you must get exactly right is the order and the conditions of the syntax elements, because CAVLC has no markers: the
only way to know where macroblock n+1 starts is to parse macroblock n correctly.

## 3. Concepts in detail

### 3.1 slice_data() in P slices (7.3.4)

```
loop:
    mb_skip_run                    ue(v)    (P slices only; read before EVERY coded macroblock, may be 0)
    skip that many macroblocks     (CurrMbAddr advances)
    if mb_skip_run > 0 and not more_rbsp_data():  the slice is over (a run can END the slice)
    macroblock_layer()             (the next, non-skipped macroblock)
    if not more_rbsp_data(): slice is over
    CurrMbAddr advances
```

`more_rbsp_data()` is true while bits remain before the `rbsp_stop_one_bit` (the last 1 bit of the NAL unit, after which
only zero bits follow). Consequences:

* the number of skip runs equals the number of coded macroblocks, plus one more if the slice ends with skipped macroblocks;
* a picture that is entirely skipped is one slice with one `mb_skip_run` = number of macroblocks in the slice and nothing else;
* a run must not go past the end of the picture; a coded macroblock must not start beyond it.

An I slice has no `mb_skip_run` at all. The same slice_data loop therefore serves both; only the first line depends on the slice type.
(Slice types 5-9 are the same types as 0-4: "all slices of this picture have the same type".)

### 3.2 mb_type in P slices (Table 7-13)

`mb_type` is ue(v). In a P slice:

| mb_type | name | partitions | `ref_idx_l0` coded | `mvd_l0` pairs |
|---|---|---|---|---|
| 0 | P_L0_16x16 | 1 (16x16) | 1 | 1 |
| 1 | P_L0_L0_16x8 | 2 (top, bottom) | 2 | 2 |
| 2 | P_L0_L0_8x16 | 2 (left, right) | 2 | 2 |
| 3 | P_8x8 | 4 sub-macroblocks (8x8 each) | 4 | 4 to 16 (see sub types) |
| 4 | P_8x8ref0 | 4 sub-macroblocks | 0 (all `ref_idx` are 0) | 4 to 16 |
| 5 .. 30 | intra: mb_type - 5 is the I-slice mb_type (Table 7-11): 5 = I_NxN, 6..29 = Intra16x16, 30 = I_PCM | | | |

"L0" means "predicted from reference list 0" (a P slice only has list 0; B slices add L1 in 7.8).

**P_Skip is not an mb_type**: it is what a macroblock inside a skip run is. It has no syntax element at all, its
`ref_idx` is 0, its vector is inferred (7.3), its coded block pattern is 0 and its QP_Y is the current QP_Y (no `mb_qp_delta`).

`P_8x8ref0` (mb_type 4) exists only in CAVLC: it saves the four `ref_idx` when all four are 0. x264 does emit it (T6 contains
hundreds). In the syntax table `P_8x8ref0` is exactly `P_8x8` without the `ref_idx` elements.

### 3.3 sub_mb_type (Table 7-17)

Only P_8x8 / P_8x8ref0 have it: four `sub_mb_type` values, ue(v), one per 8x8 block (numbered in raster order: top-left,
top-right, bottom-left, bottom-right).

| sub_mb_type | name | prediction blocks in the 8x8 | `mvd_l0` pairs |
|---|---|---|---|
| 0 | P_L0_8x8 | one 8x8 | 1 |
| 1 | P_L0_8x4 | two 8x4 (top, bottom) | 2 |
| 2 | P_L0_4x8 | two 4x8 (left, right) | 2 |
| 3 | P_L0_4x4 | four 4x4 (raster order) | 4 |

A P macroblock with 4 sub-macroblocks all of type 0 carries 4 vectors; with four P_L0_4x4 it carries 16.
All four sub-macroblocks of a macroblock share nothing: each has its own `ref_idx` and own list of `mvd`s.

### 3.4 Order of syntax elements: all ref_idx first, then all mvd (7.3.5.1, 7.3.5.2)

For mb_type 0, 1, 2 (`mb_pred`):

```
for each partition:           ref_idx_l0[part]          (if the condition below holds)
for each partition:           mvd_l0[part][x], mvd_l0[part][y]
```

For mb_type 3, 4 (`sub_mb_pred`):

```
sub_mb_type[0..3]
for each 8x8 block 0..3:      ref_idx_l0[blk]           (not for P_8x8ref0, and under the condition below)
for each 8x8 block 0..3:
    for each sub-partition of that block (in the order of the table above):
                              mvd_l0[x], mvd_l0[y]      (x first, then y)
```

The mistake that costs most people an afternoon is interleaving (ref_idx and mvd of partition 0, then those of partition 1). It is **all
ref_idx, then all mvd**. `mvd_l0` is se(v); the horizontal component comes first.

### 3.5 ref_idx_l0 as te(v), and why it needs num_ref_idx_active

`ref_idx_l0` is coded with `te(v)` (9.1.2), a truncated Exp-Golomb code. It exists because the largest possible
value is known: `ref_idx_l0` is between 0 and `num_ref_idx_l0_active_minus1`. If the range has only two possible values, a whole
ue(v) code (1 bit for 0, 3 bits for 1) is wasteful, so:

| num_ref_idx_l0_active_minus1 (the range) | what is in the stream |
|---|---|
| 0 | **nothing**: `ref_idx_l0` is not present and is 0 |
| 1 | **one bit, inverted**: bit 1 means ref_idx 0, bit 0 means ref_idx 1 |
| 2 or more | an ordinary ue(v) |

(The condition in the syntax table is: present if `num_ref_idx_l0_active_minus1 > 0` - plus a field-macroblock-in-MBAFF case that
Chapter 10.3 handles.) So you cannot parse a P macroblock without knowing the slice's `num_ref_idx_l0_active_minus1`: it is the
slice header value if `num_ref_idx_active_override_flag` is 1, otherwise the PPS `num_ref_idx_l0_default_active_minus1`. The
same macroblock bits mean different things under different values. It is also the reason a slice header error shows up here as garbage.

### 3.6 coded_block_pattern, transform_size_8x8_flag, mb_qp_delta

After the prediction syntax of an inter macroblock (P_Skip excluded):

1. `coded_block_pattern` as me(v): read ue(v) `codeNum`, map it with **Table 9-4, the Inter column** (the I slices of Phase 4 used the Intra column).
   Value = luma bits (bit k set = 8x8 block k has coefficients) + 16 * chroma (0 none, 1 DC only, 2 DC and AC).

   | codeNum | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | cbp | 0 | 16 | 1 | 2 | 4 | 8 | 32 | 3 | 5 | 10 | 12 | 15 | 47 | 7 | 11 | 13 |

   | codeNum | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 | 30 | 31 |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | cbp | 14 | 6 | 9 | 31 | 35 | 37 | 42 | 44 | 33 | 34 | 36 | 40 | 39 | 43 | 45 | 46 |

   | codeNum | 32 | 33 | 34 | 35 | 36 | 37 | 38 | 39 | 40 | 41 | 42 | 43 | 44 | 45 | 46 | 47 |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | cbp | 17 | 18 | 20 | 24 | 19 | 21 | 26 | 28 | 23 | 27 | 29 | 30 | 22 | 25 | 38 | 41 |

   (This column is for ChromaArrayType 1 and 2, i.e. 4:2:0 and 4:2:2; monochrome and 4:4:4 use another one. Note codeNum 0 is cbp 0: the most common inter macroblock has no residual at all, which is why the inter table differs from the intra one.)
2. `transform_size_8x8_flag`, u(1), **only if** all of: the luma part of the CBP is non-zero; the PPS has `transform_8x8_mode_flag` = 1; and
   `noSubMbPartSizeLessThan8x8Flag` is 1, meaning: for 16x16, 16x8, 8x16 always; for P_8x8 / P_8x8ref0 only if all four
   `sub_mb_type` are 0 (P_L0_8x8). (B slices add the direct case in 7.8.) The flag is not in the stream otherwise and is 0. With
   the flag set the residual is parsed as 8x8 blocks, but in CAVLC each 8x8 block is still coded as four interleaved 4x4 blocks, so for **parsing** nothing changes
   (Phase 8 handles the transform itself).
3. `mb_qp_delta` se(v) if the CBP (luma or chroma) is non-zero. Otherwise absent, and QP_Y is unchanged. (Intra
   macroblocks in P slices follow the Phase 4 rule: Intra16x16 always has `mb_qp_delta`, I_NxN only with CBP > 0.)
4. `residual()`: exactly as in Chapter 4.3/4.4: blocks in the order luma 8x8 blocks that are in the CBP (four 4x4 blocks each), chroma DC, chroma AC.
   Intra16x16 macroblocks in a P slice have their DC/AC structure as before. (An inter macroblock never has an Intra16x16 DC block.)

### 3.7 Neighbour data when neighbours are inter or skipped

The only parsing state that crosses macroblock borders is `total_coeff`, used for nC when reading coeff_token (9.2.1):

* a **skipped** macroblock has `total_coeff` 0 in every block. It is *available*, not "unavailable": it counts in the
  average with the value 0. (Treating it as unavailable is the classic bug; it only shows up when the other neighbour has a
  large count.)
* an inter macroblock stores what it parsed, and 0 for 8x8 blocks whose CBP bit is 0, as in I slices.
* an I_PCM macroblock (also inside P slices) counts as 16 everywhere.
* with `constrained_intra_pred_flag` = 1 there is a special nC rule (an intra macroblock treats inter neighbours' `total_coeff`
  as 0), but **only** when slice data partitioning is used (NAL unit types 2-4, Extended profile). In ordinary slices (NAL type 1 or 5) the
  flag does not touch nC. Since this book decodes NAL types 1 and 5 only, the flag does not affect your parser.
* availability is "already decoded in the same slice", as before.

Where `constrained_intra_pred_flag` **does** matter is intra *prediction* from inter neighbours (Chapter 5.5's rule, used
in reconstruction, not in parsing): with the flag set, inter neighbours count as unavailable for intra sample prediction,
and an inter neighbour makes the predicted Intra4x4 mode DC. The prediction-mode syntax elements are parsed the same either way.

Intra macroblocks in P slices otherwise parse exactly like in I slices after subtracting 5 from `mb_type`.

### 3.8 QP

`mb_qp_delta` updates QP_Y exactly as in 7.4.5 / Chapter 5.5. A skipped macroblock, or an inter macroblock with CBP = 0, keeps the
QP_Y of the previous macroblock in decoding order (a skip does **not** reset it to the slice QP).

## 4. The project: exactly what to build

Extend your slice-data parser so that for every **I and P slice of a CAVLC, frame-coded, 8-bit 4:2:0 stream**:

1. you determine `num_ref_idx_l0_active` (override or PPS default) and `transform_8x8_mode_flag`;
2. you run the loop of 3.1, with all of 3.2-3.8, until `more_rbsp_data()` is false;
3. you write the dump of section 5.

B, SP and SI slices, CABAC slices, field pictures, MBAFF and anything but 8-bit 4:2:0 are not handled: print their `SLICE` line and an `END` line with status
`skipped`, so that a stream with B frames (T8, T9) still produces a complete dump. The check of this chapter is: **the
P slices of T5 and T6 end with `end_bit` equal to the position of the rbsp_stop_one_bit** (a `status=complete` slice), and your
histogram matches the expected one. (There is no JM
here; the harness checks the reference against ffmpeg instead, see 6.)

Suggested internal structure for the later chapters: keep, per macroblock, a list of **prediction blocks** (x, y, width, height inside the macroblock,
`ref_idx`, `mvd`) in syntax order. Chapter 7.3 needs exactly that, and the dump prints it as `refidx` / `mvd`.

## 5. Harness input format

One text file, UTF-8, one record per line, tokens separated by single spaces, `key=value` tokens without spaces in the value.
Blank lines and lines starting with `#` are ignored. This is the Phase 4 grammar (SLICE / MB / PCM / END / HIST) with new keys (the book-wide rules: `common/README.md`, "Macroblock dump grammar").

```
SLICE <i> type=<I|P|B|SP|SI> first_mb=<n> pic_size_in_mbs=<n> header_end_bit=<n>
MB <i> <addr> <keys...>                (one per macroblock, ascending addr, INCLUDING skipped macroblocks)
PCM <i> <addr> align_bits=<n> sum_y=<n> sum_cb=<n> sum_cr=<n>       (after the MB line of every I_PCM macroblock)
END <i> mbs=<n> status=<complete|stopped|skipped|error> end_bit=<n or ->
...
HIST <name>:<count> <name>:<count> ...
```

`<i>` is the 0-based index of the slice NAL unit (types 1 and 5) in stream order, `<addr>` the macroblock address
(`first_mb_in_slice` is in macroblocks, non-MBAFF). `header_end_bit`: bit position, counted from the first bit of the NAL unit
header byte, where `slice_data()` starts. Every slice NAL unit gets a SLICE and an END line, also the ones you skip
(`status=skipped`, `mbs=0`, `end_bit=-`).

`mbs` is the number of MB lines of the slice, skipped macroblocks included.
`status=complete` means the macroblocks ended exactly at the `rbsp_stop_one_bit`; `end_bit` is then the position of that bit
(the bit position after the last parsed element: counted the same way as `header_end_bit`). If your parser fails, print
what you have so far with `status=error` (the harness then reports a mismatch).

The MB line has **all** the following keys, in this order. `-` means "does not apply".

| key | value |
|---|---|
| `mb_type` | the value as coded: `skip` for a skipped macroblock; in P slices 0..4 for inter types and 5..30 for intra (the raw coded value, not reduced by 5); in I slices 0..25 |
| `name` | `P_Skip`, `P_L0_16x16`, `P_L0_L0_16x8`, `P_L0_L0_8x16`, `P_8x8`, `P_8x8ref0`, or the intra name as in Phase 4: `I_NxN`, `I_PCM`, `I_16x16_<predmode>_<chroma_cbp>_<0 or 1>` (last digit: luma AC present, i.e. cbp luma 15) |
| `i16_mode`, `chroma_cbp`, `luma_cbp` | as Phase 4 (only for Intra16x16: prediction mode, chroma CBP 0..2, luma CBP 0 or 15); `-` for everything else, inter included |
| `bit` | bit position of the first bit of `mb_type` (after the `mb_skip_run` that precedes the macroblock); `-` for skipped macroblocks |
| `t8x8` | `transform_size_8x8_flag` value (0 if absent) for I_NxN and for inter macroblocks that are not skipped; `-` for Intra16x16, I_PCM, skipped |
| `modes` | Phase 4: intra prediction mode syntax of I_NxN (`-1` for prev flag = 1, else rem_intra_pred_mode, comma separated), `i16:<mode>` for Intra16x16; `-` for inter, I_PCM, skipped |
| `chroma_pred` | `intra_chroma_pred_mode` for intra macroblocks (not PCM); `-` otherwise |
| `cbp` | coded_block_pattern = luma + 16 * chroma, for every macroblock with the syntax or an implied value (all but I_PCM and skipped) |
| `qp_delta` | `mb_qp_delta` if present, else `-` |
| `qp` | QP_Y of the macroblock (a skipped macroblock: the inherited one) |
| `nc`, `tc` | as Chapter 4.3: nC of every coded residual block in decoding order, `TotalCoeff/TrailingOnes` of every coded block; `-` when the macroblock has no residual block |
| `tcl`, `tccb`, `tccr` | as Chapter 4.3: total_coeff stored for the 16 luma and 4 + 4 chroma blocks after the macroblock (a skipped macroblock: all zeros; I_PCM: 16 each) |
| `coef` | as Chapter 4.4: the coefficient lists; `-` when the macroblock has no residual block |
| `skip_run` | the `mb_skip_run` value read immediately before this macroblock (0 is common); `-` in I slices and for skipped macroblocks |
| `sub` | the four `sub_mb_type` values, comma separated, for P_8x8 and P_8x8ref0; else `-` |
| `refidx` | `ref_idx_l0` per partition (1, 2 or 4 values, comma separated, in syntax order; the value 0 where the element is not coded: `num_ref_idx_active` = 1 or P_8x8ref0); `-` for intra and skipped macroblocks |
| `mvd` | `x:y` per prediction block in syntax order, comma separated, in quarter-sample units: one per partition (16x16: 1, 16x8 / 8x16: 2) and, for 8x8 types, the sub-partitions of block 0, then block 1 ... in the order of 3.3; `-` for intra and skipped |

`HIST`: one `name:count` token per macroblock name that occurs, over the whole file, alphabetical (ASCII) order, skipped macroblocks
counted under `P_Skip`; intra macroblocks in P slices counted under their intra names like those in I slices. (The same classes as a JM trace's mb_type
histogram: P_Skip, the four inter classes, and the intra types.)

Example (two macroblocks of a P slice, line breaks added for display):

```
SLICE 1 type=P first_mb=0 pic_size_in_mbs=396 header_end_bit=33
MB 1 0 mb_type=skip name=P_Skip i16_mode=- chroma_cbp=- luma_cbp=- bit=- t8x8=- modes=- chroma_pred=- cbp=- qp_delta=- qp=26 nc=- tc=- tcl=0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0 tccb=0,0,0,0 tccr=0,0,0,0 coef=- skip_run=- sub=- refidx=- mvd=-
MB 1 1 mb_type=3 name=P_8x8 i16_mode=- chroma_cbp=- luma_cbp=- bit=36 t8x8=0 modes=- chroma_pred=- cbp=0 qp_delta=- qp=26 nc=- tc=- tcl=0,... tccb=0,0,0,0 tccr=0,0,0,0 coef=- skip_run=1 sub=0,1,3,0 refidx=1,0,2,0 mvd=0:0,2:-1,-4:0,1:1,0:0,0:0,0:3,0:0
```

(The skip run `mb_skip_run` = 1 starts at bit 33 and is the 3-bit code `010`, so `mb_type` starts at bit 36. `refidx=1,0,2,0`
needs at least three active references. `mvd` has 1 + 2 + 4 + 1 = 8 entries. The example abbreviates `tcl`; your line must have all 16 values.)

The values `nc` ... `coef` are the same as in Phase 4, so your Phase 4 residual code can be reused unchanged.

## 6. Running the harness

```
python3 test_pslice.py <input.ts|input.264> <mb_dump.txt>        # compare your output with the reference
python3 test_pslice.py --print-expected <input> <expected.txt>   # write the expected dump (diff it against yours)
python3 test_pslice.py --make-variants <out_dir>                 # extra inputs
python3 test_pslice.py --self-test                               # verify the harness itself (about 20 s)
```

Exit codes: 0 pass, 1 fail (first ~10 mismatches printed with slice / macroblock address / key / expected / got), 2 unreadable or malformed
output, bad input, or the reference itself failing one of its independent checks.

Inputs to use: `T5` and `T6` (`python3 -m common.testfiles T5 T6` builds them into `.cache/`), then T7 (weighted prediction: the extra
slice-header fields change `header_end_bit`, the macroblock syntax is the same), and `--make-variants`, which writes into `<out_dir>`:

* `T5.ts`, `T6.ts`, `T7.ts`;
* x264 streams: `x264_p_slices4` (4 slices per picture, so skip runs and nC at slice boundaries), `x264_p_cip`
  (`constrained_intra_pred_flag` = 1 with intra macroblocks in P slices), `x264_p_ref16` (ref_idx up to 15: ue(v) branch),
  `x264_p_high8x8` (High profile, CAVLC, `transform_size_8x8_flag` in inter macroblocks), `x264_p_qpdelta` (varying QP in inter macroblocks),
  `x264_p_noskip` (`fast-pskip=0`), `x264_p_allskip` (every P picture is one skip run), `x264_pb_main` (B slices present: you must skip them cleanly);
* synthetic streams `synth_p_zero.264`, `synth_p_mvd.264`, `synth_p_high.264` (+ `.manifest.json`): every mb_type and sub_mb_type, ref_idx with 1 to 4 active
  references (PPS default and override), skip runs of all lengths, runs ending the slice, intra macroblocks, several slices, High-profile `transform_size_8x8_flag`.

What the harness compares: slice lines, every key of every macroblock line, PCM lines, END lines, the HIST line (against your own MB lines, and against the expected one).

**How the reference is validated (so you can trust the expected dump):**

1. every I and P slice ends exactly on its `rbsp_stop_one_bit` (all of T5, T6, T7 and the variants);
2. ffmpeg's `-v repeat+debug -threads 1 -debug mb_type` map shows for every macroblock a class and shape: `S` skip, `>` inter (list 0), `i`/`I`/`P` intra 4x4 / 16x16 /
   PCM, and the second character `' '` 16x16, `-` 16x8, `|` 8x16, `+` 8x8. Every cell agrees with the reference.
   (`P_8x8` and `P_8x8ref0` look the same there, and sub-partition shapes are not visible.)
3. ffmpeg's `-debug qp` map agrees on QP_Y of every macroblock, skipped ones included (ffmpeg prints 0 for I_PCM; the harness allows for that). This verifies that skipped and CBP-0 macroblocks do not change QP.
4. **Motion-vector counts.** `ffmpeg -flags2 +export_mvs ... -vf showinfo` prints `side data - Motion vectors: (N bytes)` per frame, 40 bytes per
   vector. The exported list has one vector per macroblock partition (skip and 16x16 = 1, 16x8 / 8x16 = 2, an 8x8 macroblock = 4: ffmpeg exports at 8x8
   granularity, so sub-partitions do not show). The reference's partition count per picture equals this for every picture of every stream without B slices.
   The values themselves cannot be read back from the CLI (ffprobe shows only that side data exists; `codecview=mv=pf` draws arrows); that is for 7.3.
5. synthetic streams: the bit writer's manifest equals the reference field by field, ffmpeg decodes without any message, I_PCM macroblocks are bit exact, and in
   `synth_p_zero` (all `mvd` 0, so every motion vector is 0) each partition's decoded pixels are an exact copy of the picture its `ref_idx` names. That validates the
   *meaning* of `ref_idx` (te(v) inversion included) and of the 16x8 / 8x16 / 8x8 geometry, not just the bit count.

**What these checks do not prove.** Nothing independent of this book shows mvd values: the x-before-y order and the order of vectors
inside a sub-macroblock are checked only against the generator, which was written from the same reading of 7.3.5. Sub-partition
shapes (8x4 vs 4x8, the 4x4 raster order) are not checked at all: the pixel copy test works per 8x8 block (one `ref_idx` per 8x8),
and ffmpeg's map and vector count stop at 8x8. These get their independent proof when vectors move pixels (Chapters 7.3 and 7.4).
In the x264 streams `ref_idx` is only checked for bit count; which picture it names is Chapter 7.2.

`--self-test` runs all of these on 14 inputs, then shows that four kinds of corrupted reader output FAIL (a changed mvd, a missing last macroblock line, a missing skipped-macroblock line, a wrong HIST count), then runs 17 deliberately wrong parsing rules
through the same comparison; each must be caught: te range 1 read as ue, te bit not inverted, `ref_idx` read when only one reference, `ref_idx` read in P_8x8ref0, mvd y before x, ref_idx
and mvd interleaved, no end-of-slice test after a skip run, off-by-one skip run, intra CBP column for inter, P_L0_4x4 with two vectors, 16x8/8x16 swapped, wrong intra offset, skipped macroblocks unavailable
for nC, QP reset after skip, `mb_qp_delta` read with CBP 0, transform_size_8x8_flag ignoring sub-partitions or never read.

## 7. Reading failures

* **The first mismatch in a slice is the cause; everything after it is noise.** Look at the first reported macroblock and the `bit` of the previous one.
* `SLICE k: expected N MB lines, got M` with a status `error`: your parser desynchronised. Print every syntax element's bit position and compare with `--print-expected` (it has `bit=` per macroblock).
  The expected `bit` of the first wrong macroblock tells you which macroblock is the last one parsed correctly.
* `refidx` wrong on T5 (one reference): you read a `ref_idx` that is not there. Wrong only where `num_ref_idx_active` = 2: you read ue(v) instead of an inverted bit.
  Wrong on a slice with override: you used the PPS default instead of the slice header value.
* `mvd` wrong but the slice still ends correctly: swapped x / y, or wrong number of vectors per sub_mb_type that happens to cancel out. (Swapping `8x4` and `4x8`
  is invisible here: both have two vectors and the dump prints the raw `sub` values. Keep the shapes right anyway; Chapter 7.3 is where that shows.)
* `cbp` wrong: you used the intra table. `qp` wrong after skips: QP must carry over.
* `nc` wrong next to skipped macroblocks: skipped = available with 0.
* `END status` error in `x264_p_allskip`: the slice is just one `mb_skip_run`; test `more_rbsp_data()` after every non-zero run.
* `t8x8` problems appear only in `x264_p_high8x8` / `synth_p_high`: the flag is conditional on `noSubMbPartSizeLessThan8x8Flag`.
* ffmpeg's `-debug mb_type` is a quick way to see your classes on a real file; `-debug qp` the QPs.

## 8. What's next

Chapter 7.2 builds the decoded picture buffer and the default P reference list, so that your `refidx` values point at actual pictures; 7.3 then turns `mvd` into motion vectors.
