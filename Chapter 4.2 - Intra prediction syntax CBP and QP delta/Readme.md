# Chapter 4.2 - Intra prediction syntax, CBP and QP delta

## 1. Goal & the project

Chapter 4.1 read the first element of each macroblock, `mb_type`. This chapter reads **everything else that comes before the coefficients** in an intra macroblock:

- for `I_NxN`: the optional `transform_size_8x8_flag` and the 16 (or 4) intra prediction mode codes;
- `intra_chroma_pred_mode`;
- `coded_block_pattern` (which parts of the macroblock have coefficients), for `I_NxN`;
- `mb_qp_delta` (the quantiser change), and the running quantiser `QP_Y` it produces.

Your program extends the dump of Chapter 4.1 with six new keys per macroblock line. These are *syntax* elements only. What the prediction modes mean for the pixels is Phase 5; what QP does to coefficients is Phase 5 as well. Here you only read the numbers correctly and keep the QP bookkeeping right.

Honest scope, as in 4.1: the residual (coefficients) is chapters 4.3 and 4.4, so the reader still **reads macroblocks until it meets one it cannot finish and stops the slice there**. What changed is how far that gets: the stop is now at the *residual*, so every macroblock line you print is complete, and macroblocks without coefficients (or with only empty blocks) no longer stop anything. Section 4 defines the rule exactly.

You build on Chapter 4.1's reader (slice loop, `mb_type`, `I_PCM`, the bit reader) and on the parsed SPS/PPS/slice header of Phase 3. New inputs from them: `transform_8x8_mode_flag` (PPS; absent means 0), `pic_init_qp_minus26` (PPS) and `slice_qp_delta` (slice header).

## 2. Big picture

Intra prediction guesses a block from already decoded neighbours; the residual (coefficients) corrects the guess. The decoder has to be told *how* to guess, and how much of the correction follows.

- **I_NxN macroblocks** are predicted in small blocks. There are sixteen 4x4 blocks, or four 8x8 blocks when the High-profile 8x8 transform is in use. Each block has its own mode (vertical, horizontal, DC, a diagonal ...). Sending a full mode number for every block would be wasteful, because neighbouring blocks tend to pick similar modes. So each block sends either "use the mode you would guess from your neighbours" (one bit) or "something else, here is which" (one bit plus three).
- **Intra16x16 macroblocks** have one mode for the whole macroblock; it lives inside `mb_type` (Chapter 4.1). Nothing extra is sent for luma.
- **Chroma** always has one mode per macroblock, for both chroma planes together: `intra_chroma_pred_mode`.
- **Coded block pattern (CBP)** is a small bitmask saying which 8x8 luma quadrants and which chroma parts contain coefficients, so empty regions cost no bits. `I_NxN` sends it as its own element. Intra16x16 does not, because its CBP is inside `mb_type`.
- **QP delta** changes the quantiser for the macroblock. It is only sent when the macroblock has coefficients (or is Intra16x16, which always has a DC block). The quantiser is predicted from the previous macroblock, so only the *change* is coded.

## 3. Concepts in detail

### 3.1 macroblock_layer() after mb_type, I slices, CAVLC (7.3.5)

For a macroblock that is not `I_PCM`, in this order:

| step | element | when present | coding |
| --- | --- | --- | --- |
| 1 | `transform_size_8x8_flag` | `mb_type` is `I_NxN` **and** PPS `transform_8x8_mode_flag` = 1 | u(1) |
| 2 | mode codes (`mb_pred`) | `I_NxN` only | per block: `prev_intra4x4_pred_mode_flag` u(1), then if it is 0 `rem_intra4x4_pred_mode` u(3). 16 blocks, or 4 blocks (`prev_intra8x8_pred_mode_flag`, `rem_intra8x8_pred_mode`, same bit layout) if `transform_size_8x8_flag` = 1 |
| 3 | `intra_chroma_pred_mode` | always (4:2:0) | ue(v), 0 to 3 |
| 4 | `coded_block_pattern` | `I_NxN` only | me(v) |
| 5 | `mb_qp_delta` | CBP > 0 (luma or chroma part), or Intra16x16 | se(v) |
| 6 | `residual()` | see section 3.6 | chapters 4.3-4.4 |

Notes:

- The flags and rems of step 2 are interleaved per block: flag, [rem], flag, [rem] ... (not sixteen flags and then the rems). Block order is `luma4x4BlkIdx` 0 to 15 (the z-order of 6.4.3: the four 4x4 blocks of the top-left 8x8 quadrant first, then top-right, bottom-left, bottom-right) or `luma8x8BlkIdx` 0 to 3 (top-left, top-right, bottom-left, bottom-right).
- Intra16x16 has no step 1, 2 or 4: its mode and CBP are in `mb_type`. Steps 3 and 5 are present, step 5 always.
- There is a second `transform_size_8x8_flag` in the syntax, after the CBP. It exists for *inter* macroblocks only. Intra macroblocks never have it there. Do not read one.
- `intra_chroma_pred_mode` is only present for 4:2:0 and 4:2:2 (ChromaArrayType 1 or 2), which is all this book handles.
- A value above 3 for `intra_chroma_pred_mode` or above 47 for the CBP code number is a stream error.

### 3.2 The prediction mode codes (7.3.5.1, 7.4.5.1)

For each block the stream gives either a flag of 1 ("use the predicted mode") or a flag of 0 followed by a 3-bit `rem` from 0 to 7. The decoder derives the real mode as follows (8.3.1.1; this is the full story, you need it in Phase 5, not here):

1. Look at the left neighbour block A and the above neighbour block B. `predIntra4x4PredMode` is the smaller of their two modes. If either of them is not available (picture edge, other slice), the prediction is DC (mode 2). A neighbour that is available but lies in a macroblock that is not `I_NxN` (Intra16x16, I_PCM) counts as DC (2).
2. Flag 1: the mode is the prediction. Flag 0: the mode is `rem` if `rem` is below the prediction, else `rem + 1`. (The prediction itself can never be the explicit choice, so its value is skipped. That is why 3 bits cover 9 modes.)

The nine 4x4 modes (and the same nine for 8x8):

| mode | name | mode | name |
| --- | --- | --- | --- |
| 0 | Vertical | 5 | Vertical-Right |
| 1 | Horizontal | 6 | Horizontal-Down |
| 2 | DC | 7 | Vertical-Left |
| 3 | Diagonal-Down-Left | 8 | Horizontal-Up |
| 4 | Diagonal-Down-Right | | |

For this chapter's output you print the **coded** values, not the derived modes: `-1` for flag 1, otherwise the `rem` value. That is all the syntax you need and it needs no neighbour bookkeeping.

When `transform_size_8x8_flag` is 1 there are only 4 entries instead of 16: each 8x8 block has one mode. (Intra 8x8 prediction itself, with its reference-sample filtering, is Phase 8.) A 4x4 macroblock next to an 8x8 one still derives its predicted mode from the neighbour's mode, which is another reason Phase 5 will keep a per-4x4-block mode array.

Other mode numbers, for reading Phase 5 later:

| element | 0 | 1 | 2 | 3 |
| --- | --- | --- | --- | --- |
| Intra16x16PredMode (from `mb_type`) | Vertical | Horizontal | DC | Plane |
| `intra_chroma_pred_mode` | DC | Horizontal | Vertical | Plane |

Note the different order for chroma: its mode 1 is horizontal and 2 vertical, the reverse of the 16x16 table.

### 3.3 coded_block_pattern (7.4.5, 9.1.2, Table 9-4)

The CBP is a number from 0 to 47: `CodedBlockPatternLuma + 16 * CodedBlockPatternChroma`.

- **Luma part (bits 0 to 3).** Bit *i* set means: the 8x8 luma quadrant *i* may contain coefficients, and its four 4x4 blocks (or its one 8x8 block) will have a `coeff_token` in the residual. Quadrant 0 is top-left, 1 top-right, 2 bottom-left, 3 bottom-right. A clear bit means all four 4x4 blocks of that quadrant are empty and nothing is sent for them.
- **Chroma part (value 0 to 2).** 0: no chroma coefficients at all. 1: chroma DC coefficients only (one 2x2 DC block per chroma plane). 2: chroma DC and AC.

For `I_NxN` the CBP is sent as `me(v)`: a `ue(v)` code number, mapped through a table. Short code numbers go to the CBP values that are most common in intra macroblocks (47 = everything coded, 31, 15, 0 ...). For ChromaArrayType 1 or 2 and intra macroblocks, the mapping is (Table 9-4, column "Intra_4x4, Intra_8x8"):

| codeNum | CBP | codeNum | CBP | codeNum | CBP | codeNum | CBP |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 47 | 12 | 39 | 24 | 28 | 36 | 24 |
| 1 | 31 | 13 | 43 | 25 | 35 | 37 | 6 |
| 2 | 15 | 14 | 45 | 26 | 37 | 38 | 9 |
| 3 | 0 | 15 | 46 | 27 | 42 | 39 | 22 |
| 4 | 23 | 16 | 16 | 28 | 44 | 40 | 25 |
| 5 | 27 | 17 | 3 | 29 | 1 | 41 | 32 |
| 6 | 29 | 18 | 5 | 30 | 2 | 42 | 33 |
| 7 | 30 | 19 | 10 | 31 | 4 | 43 | 34 |
| 8 | 7 | 20 | 12 | 32 | 8 | 44 | 36 |
| 9 | 11 | 21 | 19 | 33 | 17 | 45 | 40 |
| 10 | 13 | 22 | 21 | 34 | 18 | 46 | 38 |
| 11 | 14 | 23 | 26 | 35 | 20 | 47 | 41 |

Tips: type it as an array of 48 numbers and check that it is a permutation of 0 to 47 (every CBP value appears exactly once). A single typo usually makes it a non-permutation. Code number 3 (one bit pattern `00100`) is "CBP 0": an `I_NxN` macroblock with nothing coded.

The table has other columns that this book does not use: the *inter* column (a different order, because inter macroblocks favour small CBPs; Phase 7) and the columns for ChromaArrayType 0 or 3 (monochrome and 4:4:4: only 16 code numbers, no chroma part; for the intra column they are `15 0 7 11 13 14 3 5 10 12 1 2 4 8 6 9`). Do not mix them up.

### 3.4 mb_qp_delta and QP_Y (7.3.5, 7.4.5, equation 7-37)

`mb_qp_delta` is `se(v)`, range -26 to +25 for 8-bit video (the general range is `-(26 + QpBdOffsetY/2)` to `+(25 + QpBdOffsetY/2)`, with `QpBdOffsetY` = 0 for 8 bits). Stream error otherwise.

It is present when the macroblock has any coded coefficients: CBP luma or chroma non-zero, or Intra16x16 (always present there, even if the value is 0, because every Intra16x16 macroblock has a DC block). So an `I_NxN` macroblock with CBP 0 has **no** `mb_qp_delta`, and neither does `I_PCM`. When it is absent it counts as 0.

The luma quantiser of a macroblock:

`QP_Y = ((QP_Y,PRED + mb_qp_delta + 52 + 2*QpBdOffsetY) mod (52 + QpBdOffsetY)) - QpBdOffsetY`

which for 8 bits is `(QP_Y,PRED + mb_qp_delta + 52) mod 52`. It wraps around at both ends: from QP 50, a delta of +5 gives 3; from QP 2, a delta of -10 gives 44. Wrapping is legal and an encoder may use it, so test it.

`QP_Y,PRED` is the `QP_Y` of the previous macroblock **in decoding order within the same slice**. For the first macroblock of a slice it is `SliceQP_Y = 26 + pic_init_qp_minus26 + slice_qp_delta`. Every slice restarts the chain. Macroblocks that carry no `mb_qp_delta` simply keep the previous value, and that includes `I_PCM`: its `QP_Y` is the inherited one. (The deblocking filter of Phase 6 treats an `I_PCM` macroblock as QP 0 anyway; that is a deblocking rule, not a change to the chain.)

The chroma QP is derived from `QP_Y` plus the PPS chroma offsets through a table; that is Phase 5, you only track `QP_Y` here.

### 3.5 transform_size_8x8_flag

This flag only exists when the PPS has `transform_8x8_mode_flag` = 1, which means High profile (x264 turns it on for High with `8x8dct`, the default). For an `I_NxN` macroblock it chooses between sixteen 4x4 blocks and four 8x8 blocks, for both prediction and the residual transform. Read it immediately after `mb_type`. If the PPS has no extension (Baseline, Main) or `transform_8x8_mode_flag` is 0, the flag is absent and the macroblock uses 4x4 blocks. In your dump print `t8x8=0` in that case.

With the flag set, the modes of step 2 are 4 instead of 16. The CBP and the QP delta read exactly the same. The residual's block layout changes only in Chapter 4.4.

### 3.6 The residual, and what it means for "finishing" a macroblock

`residual()` is the next chapter's subject, but you need to know where it starts and what blocks it holds, because that decides when you can stop reading:

- `I_NxN`: for each 8x8 quadrant whose CBP luma bit is set, four luma blocks (also with the 8x8 transform in CAVLC: the 8x8 block is coded as four interleaved 4x4 blocks, each with its own `coeff_token`). Then, if CBP chroma is 1 or 2: the Cb DC block and the Cr DC block. If CBP chroma is 2: four Cb AC blocks, then four Cr AC blocks. With CBP 0, nothing at all.
- Intra16x16: first the DC block of luma (always, one `coeff_token`), then, if the luma flag in `mb_type` is set, sixteen luma AC blocks. Then the same chroma blocks as above, from the chroma value in `mb_type`.

Every coded block starts with a `coeff_token`, which says how many coefficients it has. The code used for it depends on `nC`, a count derived from the neighbouring blocks (Chapter 4.3). Two facts make this chapter possible:

- A block with **zero coefficients** is just a `coeff_token`, nothing else. With `nC` below 2 (luma, chroma AC) it is the single bit `1`; for the chroma DC blocks (a fixed table with `nC` = -1) it is the two bits `01`.
- `nC` is below 2 if all neighbouring blocks have zero coefficients. That is the case while every block read earlier in the slice was a zero token and no `I_PCM` macroblock has appeared (a block next to `I_PCM` counts as having 16 coefficients).

## 4. The project: exactly what to build

Extend your Chapter 4.1 reader. Slice selection, `SLICE` and `END` lines, `I_PCM` handling and the histogram stay as they were. For every macroblock other than `I_PCM`, after `mb_type`, read steps 1 to 5 of section 3.1 and keep the QP chain.

**State per slice.** `QP_Y` (the running value), starting at `SliceQP_Y`; and the flag "an `I_PCM` macroblock has been read in this slice".

**Per macroblock** (fill the six keys of section 5 as you go). An `I_PCM` macroblock is read as in 4.1; its new keys are all `-` except `qp`, which is the current `QP_Y`. For every other macroblock, after `mb_type`:

1. `I_NxN`: if `transform_8x8_mode_flag` is 1, read `transform_size_8x8_flag`; read 16 or 4 mode codes (flag, then `rem` if the flag is 0).
2. Read `intra_chroma_pred_mode` (error above 3).
3. `I_NxN`: read the CBP code number (error above 47) and map it through Table 9-4. Intra16x16: take the CBP from `mb_type` (luma 0 or 15, plus 16 times the chroma value).
4. If CBP is not 0 or the macroblock is Intra16x16: read `mb_qp_delta` (error outside -26..25) and update `QP_Y`. Otherwise `qp_delta` is absent and `QP_Y` unchanged.

As in 4.1, a macroblock at which an error happens gets no `MB` line, and the slice ends with status `error`.

**The stop rule (replaces 4.1's, in the same replaceable function; Chapter 4.3 swaps it for the block boundary file, 4.4 deletes it).** After step 4, look at the residual:

- `I_NxN` with CBP 0: nothing to read. The macroblock is **finished**.
- Otherwise it has coded blocks, listed in section 3.6. If an `I_PCM` macroblock has already been read in this slice, **stop** (the neighbour-based `nC` is Chapter 4.3). Else read the `coeff_token`s in order: every one must be the zero token (luma/chroma AC: bit `1`; chroma DC: bits `01`). The first one that is not means the block has coefficients: **stop**. If all are zero tokens, the macroblock is finished.
- "Stop" means: the `MB` line of this macroblock is printed and counted (all its keys are known, because everything printed comes before the residual), the slice ends with status `stopped`, and you do not look at any later bit.
- After a finished macroblock call `more_rbsp_data()`: false means `complete`, true means next address.

Why the "all zero tokens" reading is honest: the zero token needs `nC`, which Chapter 4.3 computes; here it is justified because every block read so far in the slice was empty and no `I_PCM` was seen, so `nC` is 0 everywhere. It makes streams parse that have coded-but-empty blocks (synthetic ones, and a few real ones). Real encoder output at normal quality has coefficients almost at once, so you will typically stop at the first macroblock that has them. Chapter 4.4 removes the stop.

**What to expect.**

- `T1`, `T2`, `T3`, `T4`: every slice stops at its first macroblock, because its residual starts with a non-zero `coeff_token`. In `T1` to `T3` that macroblock is always an `I_NxN` with CBP 47; in `T4` (4 slices per picture) it is often Intra16x16 or an `I_NxN` with another CBP. You still print all of its keys, so these files check everything in this chapter on the first macroblock of every slice; the long runs come from the other inputs below.
- Flat or smooth, high-QP content: most macroblocks are Intra16x16 with CBP 0 or `I_NxN` with CBP 0 and no residual at all, so whole slices parse.
- Streams with `I_PCM` stop at the first macroblock after it that needs a `coeff_token`.

**Verify (replacing the Summary's "match the JM trace for the first 20 macroblocks").** JM is not available. T2 stops at its first macroblock per slice, so "the first 20 macroblocks" do not exist there; instead the harness uses inputs where long runs parse (section 6) and checks QP of every macroblock against ffmpeg's own QP map and the modes, CBP and QP chain of the synthetic stream against an independent bit writer.

## 5. Harness input format

Chapter 4.1's grammar (one record per line, space-separated `key=value` pairs, `#` comments and blank lines ignored, extra keys allowed, keys in any order, same `SLICE`, `PCM`, `END`, `HIST` lines) with six more keys on every `MB` line:

```
MB <slice_index> <mb_addr> mb_type=<raw> name=<NAME> i16_mode=<0-3|-> chroma_cbp=<0-2|-> luma_cbp=<0|15|-> bit=<n> \
   t8x8=<0|1|-> modes=<list|i16:<0-3>|-> chroma_pred=<0-3|-> cbp=<0-47|-> qp_delta=<n|-> qp=<0-51>
```

(One line; the backslash above only wraps it.) The six keys, exactly:

| key | for `I_NxN` | for Intra16x16 | for `I_PCM` |
| --- | --- | --- | --- |
| `t8x8` | `0` or `1`: the `transform_size_8x8_flag` value (`0` when the PPS does not enable it) | `-` | `-` |
| `modes` | the coded mode entries in block order, joined by commas with no spaces: 16 entries if `t8x8=0`, 4 if `t8x8=1`. Entry `-1` = prev flag was 1; entry `0`..`7` = flag was 0 and this is `rem` | `i16:<m>` with `m` = Intra16x16PredMode (the same number as `i16_mode`) | `-` |
| `chroma_pred` | `intra_chroma_pred_mode`, 0 to 3 | same | `-` |
| `cbp` | the coded block pattern after the Table 9-4 mapping, 0 to 47 | `luma_cbp + 16 * chroma_cbp` (from `mb_type`) | `-` |
| `qp_delta` | the decoded `mb_qp_delta` (may be negative), or `-` if absent (CBP 0) | the decoded `mb_qp_delta` (always present) | `-` |
| `qp` | `QP_Y` after this macroblock, 0 to 51 | same | the inherited `QP_Y` |

Example lines (the first from `T2`, the second a real High CAVLC `I_NxN` with the 8x8 transform and CBP 0, the next two from the synthetic stream: a macroblock where QP wraps past 51, and an `I_PCM`; the `SLICE` and `END` lines of the T2 example belong together, the others are excerpts):

```
SLICE 0 type=I first_mb=0 pic_size_in_mbs=396 header_end_bit=32
MB 0 0 mb_type=0 name=I_NxN i16_mode=- chroma_cbp=- luma_cbp=- bit=32 t8x8=0 modes=-1,1,0,-1,1,1,5,7,0,6,0,4,2,-1,-1,-1 chroma_pred=0 cbp=47 qp_delta=0 qp=23
END 0 mbs=1 status=stopped

MB 0 4 mb_type=0 name=I_NxN i16_mode=- chroma_cbp=- luma_cbp=- bit=68 t8x8=1 modes=-1,-1,-1,-1 chroma_pred=0 cbp=0 qp_delta=- qp=32

MB 1 23 mb_type=22 name=I_16x16_1_2_1 i16_mode=1 chroma_cbp=2 luma_cbp=15 bit=265 t8x8=- modes=i16:1 chroma_pred=0 cbp=47 qp_delta=25 qp=2
MB 43 6 mb_type=25 name=I_PCM i16_mode=- chroma_cbp=- luma_cbp=- bit=230 t8x8=- modes=- chroma_pred=- cbp=- qp_delta=- qp=32
PCM 43 6 align_bits=1 sum_y=33058 sum_cb=8039 sum_cr=7795
```

The third example line is slice 1, macroblock 23 of the synthetic stream. The previous macroblock of that slice had `qp=29`, and 29 + 25 = 54 wraps to `qp=2`, that is `(29 + 25 + 52) mod 52`.

The first `T2` line reads: mode entries `-1,1,0,...`: the first block uses its predicted mode, the second block has `rem` 1, the third `rem` 0. CBP 47 means all four luma quadrants and chroma 2. `qp_delta=0` (present because CBP is non-zero) and `qp=23`: the slice QP.

Forward compatibility: chapters 4.3 and 4.4 will add further keys to the same lines and, in 4.4, remove the stop rule; these six keys keep their meaning.

## 6. Running the harness

```
python3 test_intrasyntax.py <input.ts|input.264> <mb_dump.txt>
python3 test_intrasyntax.py --make-variants <out_dir>          # extra inputs, see below
python3 test_intrasyntax.py --print-expected <input> <out.txt> # write the expected dump, to `diff` with yours
python3 test_intrasyntax.py --self-test                        # the harness checks itself
```

Exit code 0 pass, 1 fail (the first ten differences are listed with slice, address, key, expected and actual, plus the expected values of the six new keys), 2 malformed or missing output, or a "HARNESS PROBLEM" (see below).

The inputs to try, in order:

- `.cache/T2.ts` and `T1`, `T3`, `T4`: first macroblock of each slice (see section 4). A PASS proves the modes, chroma mode, CBP, QP delta and the slice QP of one macroblock per slice.
- `mb_flat.ts` (from `--make-variants`): flat gray, whole slices parse, QP constant.
- `mb_qp.ts`: real Baseline x264 with adaptive quantisation: Intra16x16 with zero residual, `qp_delta` non-zero at some macroblocks, QP between 34 and 50; 6 of its 16 slices parse to the end, the others stop at the first macroblock with coefficients.
- `mb_high_cavlc.ts`: real High-profile CAVLC: PPS with `transform_8x8_mode_flag` = 1, 225 `I_NxN` macroblocks with `transform_size_8x8_flag` = 1 (four modes) and CBP 0 (no `mb_qp_delta`).
- `intra_synth.264`: synthetic, 96x80, 33 IDR pictures: Baseline pictures and High pictures with the 8x8 transform flag on or off; all 48 CBP codes, all `rem` values, `mb_qp_delta` over its full range with QP wrapping at both ends, I_NxN macroblocks with CBP 0 and with CBP > 0 (empty blocks and, in some pictures, blocks with one coefficient, where the reader stops), `I_PCM`, several slices per picture. `intra_synth.manifest.json` next to it is for the harness.
- `default.ts` (P and B slices must be `skipped`) and `T10_full.ts` (CABAC: `skipped`).

**What the harness compares against, and why you can trust it.** The expected output is `common/cavlc_ref.py` at parsing level 2, a reference parser written for this book. Before it is used, five independent witnesses check it:

| check | applies to | guarantee |
| --- | --- | --- |
| ffmpeg macroblock map (`-debug mb_type`) | every input | the macroblock class at every address (as in 4.1) |
| ffmpeg QP map (`-debug qp`) | every input, every macroblock the reference printed | `QP_Y` of each macroblock, hence slice QP, `mb_qp_delta`, the wrap and (via CBP) the presence of the delta, agree with a mature decoder. For `I_PCM` ffmpeg shows 0 (the deblocking value) and the harness expects exactly that |
| generator manifest | `intra_synth.264` | modes, CBP, `transform_size_8x8_flag`, deltas, QP chain, bit positions and the stop position of every slice, written by an independent bit writer that shares no code with the reference. The writer derives each block's predicted intra mode from the neighbours and only emits legal modes |
| ffmpeg decoding of the synthetic stream | `intra_synth.264` | no error message (ffmpeg rejects desynchronised or illegal data), the QP map of *every* macroblock (also those after the reader's stop) equals the generator's, and `I_PCM` pixels are unchanged |
| slices that parse to the end | `mb_qp.ts`, `mb_high_cavlc.ts`, synthetic pictures | a wrong mode count or CBP or delta would desynchronise the rest of the slice, so a slice that ends exactly on the stop bit after hundreds of macroblocks is strong evidence that the syntax of all those macroblocks was read correctly |

What is **not** independently confirmed: ffmpeg offers no debug output for the intra prediction modes, the chroma mode or the CBP value itself. For real streams those are covered by the "slices parse to the end" argument and by `mb_high_cavlc.ts`; for exact values only the synthetic stream, whose values come from the independent writer, proves them. Chapter 5.x will prove the modes by pixel comparison.

If a cross-check fails the harness prints "HARNESS PROBLEM" and exits 2: a bug in the book's tooling, not in your reader.

`--self-test` builds the extra inputs, runs the checks on known-good dumps of nine inputs (must PASS), then runs deliberately wrong dumps (a wrong `rem`, two swapped Table 9-4 entries, a `mb_qp_delta` with the wrong sign, a QP that was not wrapped, a wrong chroma mode, a wrong `t8x8` flag, 16 modes where 4 belong, a missing key, a wrong status; all must FAIL), and finally breaks the reference and the generator on purpose (a wrong QP, mode or CBP in the reference; two swapped Table 9-4 entries in the generator) and requires the ffmpeg and manifest checks to notice.

## 7. Reading failures / debugging tips

- **First macroblock wrong, modes OK but the rest after it not**: count bits. `I_NxN` without the 8x8 transform: 16 flags plus 3 bits for each flag of 0. A reader that reads all sixteen flags first and then the rems is a very common slip.
- **`t8x8` wrong on High streams**: read the flag only if the PPS flag is 1 and only for `I_NxN`; it comes before the modes. On Baseline streams it does not exist (no bit consumed). Check you take `transform_8x8_mode_flag` from the PPS with default 0 when the PPS has no extension.
- **`modes` has 16 entries but expected 4 (or the reverse)**: the same flag. Block count is 4 with `t8x8=1`.
- **`cbp` wrong but `modes` right**: the table. Check it is a permutation of 0..47; check the entry against the one in section 3.3 (a typo of one entry usually breaks only some macroblocks). Make sure Intra16x16 takes its CBP from `mb_type`.
- **`qp_delta` present when `-` expected (or the reverse)**: `I_NxN` with CBP 0 has none; Intra16x16 always has one.
- **`qp` off after a negative delta or at the edge**: use a mathematical modulo. In C-like languages `%` of a negative number is negative; add 52 *before* the modulo as the formula does, never after.
- **`qp` correct for a slice's first macroblock but wrong in later slices**: the QP chain must restart from `SliceQP_Y` at every slice; `SliceQP_Y` uses that slice's own `slice_qp_delta`.
- **QP of a macroblock after an `I_PCM` wrong**: `I_PCM` does not reset QP; the chain passes through.
- **Wrong `END` status or macroblock count only**: your stop rule. The zero-token test is one bit (`1`) for luma blocks and chroma AC, two bits (`01`) for chroma DC; in an Intra16x16 macroblock the first block is the DC block, then luma AC only if `luma_cbp` is 15.
- To look at ffmpeg's QP map yourself: `ffmpeg -v repeat+debug -threads 1 -debug qp -i T2.ts -f null - 2> qp.txt`. Each macroblock is two characters (its QP, space-padded), rows are labelled with the pixel row (0, 16, 32 ...). The first few pictures appear twice (ffmpeg decodes them once while probing). Add `-debug mb_type` to the same command and each macroblock becomes five characters: two for QP, then the three of the type map.
- Use `--print-expected` and `diff`.

## 8. What's next

Chapter 4.3 computes the neighbour block availability and `nC`, and decodes `coeff_token` from Table 9-5. That removes the "all tokens zero" scaffold and the `I_PCM` restriction, and it is the first step towards reading real residuals.
