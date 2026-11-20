# Chapter 10.3 - MBAFF parsing and neighbour derivation

## 1. Goal and the project

Chapter 10.1 coded interlaced video as **field pictures** (PAFF). The other tool for interlaced video is **MBAFF** (macroblock-adaptive frame/field coding): the picture is a normal *frame*, but its macroblocks come in vertically stacked **pairs**, and every pair is coded either as two *frame* macroblocks or as two *field* macroblocks (one per field). x264, broadcast encoders and most 1080i streams use it.

In this chapter you teach your slice-data parser to read MBAFF frames **without decoding any pixels yet**: pair addressing, `mb_field_decoding_flag` (and its inference), the neighbour derivation of clause 6.4.12.2 (Table 6-4) that feeds every context and every `nC`, and the MBAFF special rules of CABAC and CAVLC parsing. Reconstruction (field prediction inside frames, deblocking) is Chapter 10.4.

You write a **reader**: a program that parses every slice of an MBAFF stream (I, P and B slices; CAVLC and CABAC; transform 8x8; several slices; I_PCM) and writes a dump with one line per macroblock (section 5). The harness compares it with a reference parser. The milestone: **every slice lands exactly on its end** (the CABAC `end_of_slice_flag` with the stop bit where the arithmetic decoder ended, or `more_rbsp_data()` false exactly at `rbsp_stop_one_bit`) and **every pair's field flag, `mb_type`, `cbp`, QP, intra modes, `ref_idx`, `mvd` and every coefficient is what the reference says**.

## 2. Big picture

A frame picture of `PicWidthInMbs x FrameHeightInMbs` macroblocks is cut into **pairs**: two vertically adjacent macroblocks, `16 x 32` luma samples. A pair is either

* a **frame pair**: the top macroblock is the upper 16 lines, the bottom macroblock the lower 16 lines - what you already know; or
* a **field pair**: the top macroblock holds the **even** lines of the 32 (the top field part), the bottom macroblock the **odd** lines. Prediction, scan order and CABAC contexts of a field macroblock are those of field coding (Chapter 10.1).

The choice is made per pair and sent as `mb_field_decoding_flag`. The macroblocks are decoded in pair order: top, bottom, next pair to the right, ... so `mbAddr = 2 * pairIdx + {0 top, 1 bottom}`.

The one thing that gets hard: *who is my neighbour?* A macroblock's left, upper, upper-right and upper-left neighbours are no longer simply `mbAddr - 1` and friends. A frame macroblock next to a field pair has, for the same column of 4x4 blocks, neighbours alternating between the two macroblocks of that pair; a field macroblock under a frame pair "sees" a macroblock two lines up. Everything that looks at neighbours must go through one function, **6.4.12.2**: the CAVLC `nC`, **every** CABAC `ctxIdxInc`, the intra prediction mode prediction, the intra sample neighbours, the motion vector prediction. Get that function right once and the rest of the chapter is bookkeeping.

## 3. Concepts in detail

### 3.1 Addressing and slices

* Applies when `frame_mbs_only_flag = 0`, `mb_adaptive_frame_field_flag = 1` and `field_pic_flag = 0` (`MbaffFrameFlag = 1`). `PicSizeInMbs = PicWidthInMbs * FrameHeightInMbs` (even number of macroblock rows; the SPS counts map units = pair rows).
* Pair `p` lies at pair column `p % PicWidthInMbs`, pair row `p / PicWidthInMbs`. Macroblocks `2p` (top) and `2p + 1` (bottom). Frame pair: MB `2p` covers frame lines `32*row .. +15`, MB `2p+1` lines `+16 .. +31`. Field pair: MB `2p` covers the even lines of the 32, `2p+1` the odd ones.
* **`first_mb_in_slice` counts pairs**: the first macroblock of the slice is `first_mb_in_slice * 2`. A slice contains whole pairs; the macroblock after the last one of the pair `p` is `2p + 2`.
* **Availability** (6.4.10): the neighbouring *pairs* of pair `p` are `A = p - 1` (not available at column 0), `B = p - W`, `C = p - W + 1` (not available at the last column), `D = p - W - 1` (not at column 0). A pair is available when it exists, is decoded already and lies in **the same slice**. In this chapter "the pair A" always means the whole pair; `A0`, `A1` are its top and bottom macroblocks.

### 3.2 `mb_field_decoding_flag`: syntax, order and inference

The flag belongs to the pair; it is sent at most once per pair (7.3.4):

* **CABAC**: `mb_skip_flag` (P/B slices only) is read first for every macroblock. The flag is sent
  * with the **top** macroblock if it is not skipped (in I slices always);
  * with the **bottom** macroblock if the top macroblock was skipped and the bottom one is not (after the bottom's `mb_skip_flag`).
  If both are skipped it is not sent. If the bottom is skipped but the top is not, the top's flag counts.
* **CAVLC**: `mb_skip_run` counts skipped macroblocks in decoding order. The flag is read by a non-skipped macroblock that is a top macroblock, **or** a bottom macroblock whose `mb_skip_run` (read just before it) was greater than 0 - then its top macroblock was the last macroblock of that run. A bottom macroblock after a non-skipped top macroblock never reads it.
* **`end_of_slice_flag` (CABAC) is read after the bottom macroblock of every pair only**, not after the top one. (The slice ends after a bottom macroblock.)

**Inference** (7.4.4) for a pair whose flag is not sent (both skipped), and for the *context derivation of a skip flag read before the flag is known*:

1. if the pair `A` (left) is available: copy its flag;
2. else if the pair `B` (above) is available: copy its flag;
3. else the pair is a frame pair (0).

Order matters in CABAC when the top macroblock is skipped: the decoder reads the bottom's `mb_skip_flag` **before** it knows the flag - for the neighbour derivation of that skip flag (and of the top's own skip flag) the current pair is treated as having the *inferred* value. If the bottom macroblock is then not skipped, the flag is read and **replaces the inferred value for both macroblocks** (the skipped top macroblock becomes a field/frame macroblock retroactively; your reconstruction of its motion in 10.4 needs this, so a real decoder peeks ahead). In CAVLC the flag is simply the bit that follows the skip run. *Example*: pair at column 0 of its row, above pair a field pair, both macroblocks skipped: inferred field. The same pair with the left pair a frame pair: frame (left wins even when the pair above is a field pair).

**Contexts** (9.3.3.1.1.1 and 9.3.3.1.1.2): `mb_skip_flag`: `ctxIdxInc = condTermFlagA + condTermFlagB` over the *macroblock* neighbours A and B from 6.4.12.2 (see 3.3: `A` contains luma location (-1, 0), `B` contains (0, -1)), `condTermFlagN = 0` if unavailable or skipped; base 11 (P) / 24 (B). `mb_field_decoding_flag`: ctxIdx `70 + condTermFlagA + condTermFlagB` where `condTermFlagN = 1` iff the **pair** N (A = left, B = above, section 3.1) is available and is a field pair.

### 3.3 Neighbour derivation 6.4.12.2 (Table 6-4)

**Input**: current macroblock `CurrMbAddr` (top or bottom of its pair, frame or field) and a luma location `(xN, yN)` relative to its upper-left sample (for chroma the same with `maxW = maxH = 8`). **Output**: `mbAddrN` (or "not available") and the location `(xW, yW)` inside it. Used with `(-1, 0)` and `(0, -1)` for the macroblock-level neighbours A and B, with `(x - 1, y)` / `(x, y - 1)` for a block at `(x, y)`, with `(16, -1)` and `(-1, -1)` for C and D.

**First rule set** (always, MBAFF or not): inside the macroblock (`0 <= xN < maxW`, `0 <= yN < maxH`): `CurrMbAddr`, `(xN, yN)`. `yN > maxH - 1`, or `xN > maxW - 1` with `yN >= 0`: not available.

**The principle** (this is Table 6-4, restated): *the neighbour of a sample is the sample that is next to it in the same kind of lattice*.
* A **frame** macroblock lives in the frame lattice: its left neighbour is the frame sample at the left, its upper neighbour the frame sample one line up.
* A **field** macroblock lives in its own field lattice: lines are 2 frame lines apart. Its upper neighbour is the sample 2 frame lines up (same parity), its left neighbour the sample on the same frame line.
* Then look up which macroblock of the neighbouring pair holds that frame line: a frame pair holds line `r` (0..31 within the pair) in MB `r / 16` at row `r % 16`; a field pair in MB `r % 2` at row `r / 2`.
The standard cannot say "geometry", so it lists the cases. The cases for the current pair at position `X` and macroblock `top`/`bottom`, `frame`/`field` (`yN` is relative to the current macroblock; `A0/A1` top/bottom MB of pair A; `yM` is the row in the result, `yW = (yM + maxH) % maxH`, `xW = (xN + maxW) % maxW`):

**Left neighbour** (`xN < 0`, `0 <= yN < maxH`; pair A must be available):

| current MB | pair A frame | pair A field |
|---|---|---|
| top frame | `A0`, `yM = yN` | `yN` even: `A0`, odd: `A1`; `yM = yN >> 1` |
| bottom frame | `A1`, `yM = yN` | `yN` even: `A0`, odd: `A1`; `yM = (yN + maxH) >> 1` |
| top field | `yN < maxH/2`: `A0`, `yM = yN << 1`; else `A1`, `yM = (yN << 1) - maxH` | `A0`, `yM = yN` |
| bottom field | `yN < maxH/2`: `A0`, `yM = (yN << 1) + 1`; else `A1`, `yM = (yN << 1) + 1 - maxH` | `A1`, `yM = yN` |

**Above neighbour** (`0 <= xN < maxW`, `yN < 0`), **above-right** (`xN >= maxW`, `yN < 0`) and **above-left** (`xN < 0`, `yN < 0`). The B column is for `xN` in `0..maxW-1`; C and D are the same with the pair C (above right) / D (above left) in place of B, **except where noted**:

| current MB | neighbour pair frame | neighbour pair field |
|---|---|---|
| top frame | `B1`, `yM = yN` | `B1`, `yM = yN` |
| bottom frame | the **top MB of the current pair**, `yM = yN`  (C: not available; D: pair A: A frame `A0`, `yM = yN`; A field `A1`, `yM = (yN + maxH) >> 1`) | same |
| top field | `B1`, `yM = 2 * yN` | `B0`, `yM = yN` |
| bottom field | `B1`, `yM = yN` | `B1`, `yM = yN` |

(For D of a bottom frame macroblock the table row uses the *left* pair A, as in the parentheses; every other D / C entry uses pair D / C.) All of these are the "same lattice" rule: e.g. a top **field** MB under a **frame** pair looks at frame line 30 of the pair above (`B1`, row `2 * -1 + 16 = 14`) because line 31 belongs to the other field.

**Worked examples** (luma, `maxH = 16`, all results `(MB, xW, yW)`; `xN = -1` for left):

| situation | query | result |
|---|---|---|
| top frame, A field | `yN = 4` / `yN = 5` | `A0, 15, 2` / `A1, 15, 2` |
| bottom frame, A field | `yN = 4` | `A0, 15, 10` (frame line 20 of the pair is the even line 10 of the top field) |
| bottom frame, A field | D `(-1,-1)` | `A1, 15, 7` (frame line 15 is the odd line 7 of the bottom field) |
| top field, A frame | `yN = 4` / `yN = 12` | `A0, 15, 8` / `A1, 15, 8` |
| bottom field, A frame | `yN = 4` / `yN = 12` | `A0, 15, 9` / `A1, 15, 9` |
| top field, A field | `yN = 12` | `A0, 15, 12` |
| top frame, B field | `(0, -1)` | `B1, 0, 15` |
| bottom frame | `(0, -1)` | current pair's top MB, `0, 15` |
| top field, B frame / B field | `(0, -1)` | `B1, 0, 14` / `B0, 0, 15` |
| bottom field, B frame / B field | `(0, -1)` | `B1, 0, 15` / `B1, 0, 15` |
| bottom frame | C `(16, -1)` | not available |

Consequence for the macroblock-level neighbours: `A` = MB at `(-1, 0)`: top frame MB -> `A0`; bottom frame MB -> `A1` if A is a frame pair, **`A0`** if a field pair; top field MB -> `A0`; bottom field MB -> **`A0`** if A is a frame pair, `A1` if a field pair. `B` = MB at `(0, -1)`: top frame -> `B1`; bottom frame -> the top MB of its own pair; top field -> `B1` (B frame) or `B0` (B field); bottom field -> `B1`.

Two details parsing never sees but the next chapters do: (a) the rows of the left neighbour of a frame macroblock next to a field pair *alternate* between `A0` and `A1` (row parity) - a 4-row block's left column comes from both field macroblocks; (b) `yM` is not always a multiple of the block height (`yM = 2` for a block at `yN = 4`), so the neighbouring **block** is found from `(xW, yW)`: 4x4 block `(xW / 4, yW / 4)`, 8x8 block `(xW / 8, yW / 8)`.

### 3.4 What uses the neighbours

| consumer | which neighbours (all through 3.3) | MBAFF peculiarity |
|---|---|---|
| CAVLC `nC` (9.2.1) | the 4x4 blocks containing `(x-1, y)` and `(x, y-1)`; chroma AC: chroma locations with `maxW = maxH = 8` | the left block may belong to the other macroblock of the left pair than the "obvious" one; a skipped MB counts 0, I_PCM 16 |
| `mb_skip_flag`, `mb_type` (I, B), `transform_size_8x8_flag`, `intra_chroma_pred_mode`, `coded_block_pattern` chroma bins, `coded_block_flag` of DC blocks | macroblocks A and B (3.3) | the current pair's field flag decides A/B, so for a skip flag read before the flag is known the *inferred* flag is used |
| `coded_block_pattern` luma bins | the 8x8 block containing `(x-1, y)` / `(x, y-1)` of each 8x8 block | the neighbour 8x8 block index comes from `(xW, yW)`: block at `y = 8` of a top frame MB next to a field pair: `yN = 8` (even) is in `A0`, `yW = 4`: 8x8 block **1**, not 3 |
| `coded_block_flag` of 4x4 / chroma AC blocks | blocks containing `(x-1, y)`, `(x, y-1)` | as above; a block of an 8x8-transformed MB counts as coded if its `cbp` bit is set |
| `ref_idx` (9.3.3.1.1.6) | the 8x8 block containing `(x-1, y)`, `(x, y-1)` of the partition | `refIdxZeroFlagN = (refIdx > 1 ? 0 : 1)` when the current MB is a **frame** macroblock and the neighbour a **field** macroblock (a field refIdx 1 is the same frame as frame refIdx 0), else `refIdx > 0 ? 0 : 1`; neighbours that are intra, skipped/direct, or do not use the list count 0 |
| `mvd` (9.3.3.1.1.7) | 4x4 blocks containing `(x-1, y)`, `(x, y-1)` of the partition | vertical component only: `absMvdComp * 2` if the neighbour is a field MB and the current one a frame MB; `absMvdComp / 2` (integer) if the current is field and the neighbour frame. Example: neighbour `mvd_y = -5` (field), current frame: 10; neighbour `5` (frame), current field: 2 |
| Intra4x4 / 8x8 mode prediction (8.3.1.1, 8.3.2.1) | blocks containing `(x-1, y)`, `(x, y-1)` | neighbour not available, or Inter with `constrained_intra_pred_flag = 1`: `dcPredModePredictedFlag`; neighbour not I_NxN: mode 2; 4x4 current / 8x8 neighbour: the 8x8 block's mode; 8x8 current / 4x4 neighbour: 4x4 block `luma8x8BlkIdxN * 4 + n` of the neighbour, `n = 2` for B; for A `n = 1`, except **`n = 3`** when the current MB is a frame MB, A a field MB and the current block is 8x8 block 2 (simply: the 4x4 block holding `(xW, yW)`) |
| intra prediction **samples** (Chapter 10.4) | every sample `p[x, y]` with `x = -1` or `y = -1` is looked up through 3.3 on its own | the left column of a frame MB next to a field pair mixes both macroblocks of the pair; with `constrained_intra_pred_flag = 1` each *sample* is "not available" if its macroblock is Inter, and Intra16x16 / chroma modes need all their samples |
| motion vector prediction (8.4.1.3, Chapter 10.4) | neighbouring partitions A, B, C (else D) through 3.3 | neighbour **field**, current **frame**: `mvLXN[1] * 2`, `refIdxLXN / 2`; neighbour **frame**, current **field**: `mvLXN[1] / 2` (truncating towards zero), `refIdxLXN * 2`, applied before the median; a bottom frame MB has no C |

### 3.5 Other MBAFF rules of the macroblock layer

* **`ref_idx` range doubles for field macroblocks**: a field macroblock refers to *fields*, two per reference frame. With `num_ref_idx_lX_active_minus1 = n - 1` the range is `0 .. n - 1` for a frame MB and `0 .. 2n - 1` for a field MB. CAVLC `te(v)` uses `cMax = 2n - 1` (`cMax = 1` is one inverted bit, larger: `ue(v)`), CABAC has no bound. **`ref_idx` is present when `n > 1` or the macroblock is a field macroblock** (`mb_field_decoding_flag != field_pic_flag`): a field MB of a slice with `n = 1` still reads a one-bit `ref_idx` (0 or 1: same parity or opposite parity field of the one frame). *Example*: `n = 2`: frame MB `cMax = 1` (1 bit), field MB `cMax = 3` (`ue(v)`: codes 1, 010, 011, 00100).
* **Field macroblocks use field coding for the residual**: field scan (zig-zag replaced) and, in CABAC, the field context offsets for `significant_coeff_flag` / `last_significant_coeff_flag` (277 / 338 / 436 / 451, and the field column of the 8x8 `significant_coeff_flag` table) exactly as Chapter 10.1. `coeff_abs_level_minus1` contexts are the same. For the **dump** nothing changes: coefficients are reported by scan position.
* **QP prediction** is the *previous macroblock in decoding order* of the slice (`QP_Y,PRED`), i.e. for a top macroblock the bottom macroblock of the previous pair, for a bottom macroblock its top macroblock (skipped macroblocks keep the QP). The CABAC `mb_qp_delta` context uses the same "previous macroblock" rule (it is not a spatial neighbour).
* **Skipped macroblocks in MBAFF**: `P_Skip` / `B_Skip` motion is derived (Chapter 10.4) with the pair's final field flag, so a skipped top macroblock waits for the bottom's flag (3.2). For parsing a skipped macroblock is: QP unchanged, all `coded_block_flag` 0, `total_coeff` 0, `mb_type` skip (for B also "direct" for the `mb_type` and `ref_idx` contexts).
* `transform_size_8x8_flag`, `I_PCM` (CABAC: terminate bin, byte alignment, 384 bytes, restart the engine; the MB counts as `cbp` 47, `coded_block_flag` 1 everywhere, `nC` 16), `mb_skip_run` + `end_of_slice` conditions are as in Chapters 7 and 8.

## 4. The project: exactly what to build

Starting from your CAVLC and CABAC parsers for I, P and B slices with transform 8x8 (Chapters 4, 7, 8):

1. **Slice start**: `first_mb_in_slice * 2`, `PicSizeInMbs`; pair loop.
2. **The neighbour function** of 3.3 with the pair availability of 3.1, working for luma and chroma sizes. Route every neighbour lookup of your CAVLC and CABAC code through it (3.4); keep per-macroblock records with the arrays you need (`total_coeff`, `coded_block_flag`, `mvd`, `ref_idx`, intra modes, `cbp`, `qp`, flags) and a *per-pair* field flag. Test the function alone first (section 5.3).
3. **The pair syntax** of 3.2 (CABAC and CAVLC), the inference, the retroactive flag of a skipped top macroblock, `end_of_slice_flag` only after bottom macroblocks.
4. **The macroblock-layer rules** of 3.5: `ref_idx` range and presence, `mvd` and `ref_idx` context adjustments, field contexts and scan for field macroblocks, intra mode prediction through the neighbour function, QP prediction.
5. **The dump** (section 5).

Verify (the Summary's milestone, with ffmpeg instead of JM): every slice ends exactly on its end; the field flag of every pair, and everything else in the dump, equals the reference.

## 5. Harness input format - EXACT output of your reader

### 5.1 The dump: one file per stream

Plain text. Every line starts with a keyword; blank lines and lines starting with `#` are ignored; `name=value` words are separated by single spaces and may come in any order, extra words are ignored. Numbers are decimal. `-` means "not present / not defined".

```
SLICE <i> type=<I|P|B> first_mb=<n>
MB <i> <addr> pair=<p> pos=<T|B> fld=<0|1> mbt=<..> t8=<..> cpred=<..> modes=<..> pm=<..> sub=<..> refidx=<..> refidx1=<..> mvd=<..> mvd1=<..> cbp=<..> qpd=<..> qp=<n> coef=<..> eos=<..>
END <i> mbs=<n>
```

* `SLICE`: `<i>` = ordinal of the slice NAL unit in the stream (all slices of types 1 and 5, from 0), `first_mb` = the **macroblock address** of its first macroblock (`first_mb_in_slice * 2`). One `MB` line per macroblock **in decoding order** (top, bottom, next pair, ...), then `END` with the count.
* `MB <i> <addr>`: slice ordinal and macroblock address. `pair` = `addr / 2`, `pos` = `T` for even addresses, `B` for odd. **`fld`**: the pair's `mb_field_decoding_flag` *after* inference - for a skipped top macroblock whose bottom macroblock reads the flag, that flag; for a fully skipped pair the inferred value.
* `mbt`: `skip`, or `mb_type` in the numbering of the slice's own table: I slice 0..25 (0 = I_NxN, 1..24 = Intra16x16, 25 = I_PCM); P slice 0..4 (3 = P_8x8, 4 = P_8x8ref0) and intra `5 + I value`; B slice 0..22 (0 = B_Direct_16x16, 22 = B_8x8) and intra `23 + I value`.
* `t8`: `transform_size_8x8_flag` (0 when absent), `-` for skipped and I_PCM macroblocks.
* `cpred`: `intra_chroma_pred_mode` (`-` for inter, skipped, I_PCM). `modes`: for I_NxN the `prev_intra_pred_mode_flag` / `rem_intra_pred_mode` syntax of the 16 (`t8 = 0`) or 4 (`t8 = 1`) blocks in order, one character each: `p` (flag 1) or the digit `rem` (0..7); `-` otherwise. **`pm`**: the *derived* `Intra4x4PredMode` / `Intra8x8PredMode` of those blocks, comma separated, in the same order (this tests your neighbour-based mode prediction).
* `sub`: the four `sub_mb_type` values (P_8x8 / B_8x8), comma separated, else `-`. `refidx` / `refidx1`: `ref_idx_l0` / `ref_idx_l1` of every partition that uses the list, in syntax order (the inferred 0 when the syntax element is absent counts, so the count is the number of partitions using the list; direct sub-macroblocks of B_8x8 are not listed); `mvd` / `mvd1`: `mvd_l0` / `mvd_l1` as `x:y`, comma separated, for every prediction block using the list (sub-partitions included), in syntax order; `-` if none.
* `cbp`: `CodedBlockPatternLuma + 16 * CodedBlockPatternChroma` (also for Intra16x16, from `mb_type`); `-` for skipped and I_PCM. `qpd`: `mb_qp_delta` if present, else `-`. `qp`: `QP_Y` of the macroblock after its `mb_qp_delta` (skipped macroblocks and I_PCM: the running QP).
* `coef`: the residual blocks present in the syntax of the macroblock, in decoding order (blocks of an 8x8 quadrant whose `cbp` bit is 0, and AC blocks the `cbp` excludes, are left out), `;` separated, each `<kind><idx>:<list>`. Kinds: `i16dc`, `i16ac` (idx 0..15 = luma4x4BlkIdx), `luma` (4x4, idx 0..15), `luma8` (8x8 transform, idx 0..3), `cbdc` / `crdc` (idx 0), `cbac` / `crac` (idx 0..3). `<list>` = `-` (CABAC `coded_block_flag` 0, or all coefficients zero) or comma separated `pos=value` for the non-zero coefficients in ascending **scan position** (not raster position, so frame and field macroblocks look alike): 4x4 blocks 0..15, the 15-coefficient blocks (`i16ac`, `cbac`, `crac`) list coefficient `k` at position `k + 1`, `luma8` 0..63, chroma DC 0..3. A CAVLC 8x8 block (four interleaved 4x4 blocks) is reported as one `luma8` block, coefficient `k` of 4x4 block `i` at position `4 * k + i`. `-` when the macroblock has no residual block.
* `eos`: the value of `end_of_slice_flag` read after this macroblock (CABAC, **bottom macroblocks only**: `0`, and `1` on the last), `-` for top macroblocks and in CAVLC slices.

Example (P slice of a CABAC stream; the second pair starts with a skipped top macroblock):
```
SLICE 1 type=P first_mb=0
MB 1 0 pair=0 pos=T fld=0 mbt=skip t8=- cpred=- modes=- pm=- sub=- refidx=- refidx1=- mvd=- mvd1=- cbp=- qpd=- qp=25 coef=- eos=-
MB 1 1 pair=0 pos=B fld=0 mbt=1 t8=0 cpred=- modes=- pm=- sub=- refidx=0,0 refidx1=- mvd=2:0,0:2 mvd1=- cbp=33 qpd=0 qp=25 coef=luma0:11=-2,15=-1;luma1:3=4;luma2:-;luma3:0=-1;cbdc0:0=2,1=1,2=2;crdc0:1=-1 eos=0
MB 1 8 pair=4 pos=T fld=1 mbt=3 t8=0 cpred=- modes=- pm=- sub=0,2,2,1 refidx=0,0,1,0 refidx1=- mvd=0:0,0:0,1:1,0:0,1:0,1:-1,0:2 mvd1=- cbp=10 qpd=-1 qp=35 coef=luma4:-;luma5:7=1,15=-1 eos=-
...
END 1 mbs=30
```

### 5.2 The streams

`test_mbaff.py --make DIR` writes (all MBAFF, 4:2:0 8 bit):

| case | what it exercises |
|---|---|
| `g_i_*`, `g_p_*`, `g_b_*` (`cabac` / `cavlc`) | synthetic I / I+P / I+P+B streams, 5 x 6 macroblocks, random field and frame pairs, several slices starting at arbitrary pairs |
| `g_t8_*` | transform 8x8 (intra 8x8 mode prediction across field/frame neighbours) |
| `g_skip_*` | mostly skipped pairs: inferred field flags, flag read with the bottom macroblock |
| `g_field_cabac`, `g_frame_cabac` | 80% / 15% field pairs |
| `g_cip_*` | `constrained_intra_pred_flag = 1`, intra macroblocks in P / B slices |
| `g_pcm_*` | many I_PCM macroblocks |
| `g_ref_*` | up to 4 reference frames: field `ref_idx` up to 7, te(v) ranges |
| `g_pm8_cabac` | Intra8x8 blocks next to Intra4x4 field pairs (the `n = 3` rule of 3.4) |
| `g_mvd_cabac`, `g_wide_*`, `g_qp_cabac` | larger mvd, wider pictures with 3 slices, extreme QPs |
| `x_*` | real x264 streams of scrolling interlaced content (`x_cabac_b` B pyramid; `x_cavlc_main` Main profile CAVLC; `x_cavlc_high`; `x_cabac_slices` 3 slices; `x_cabac_cip` constrained intra; `x_static_*` static content = skipped pairs) |
| `T12` | the Summary's T12 stream (x264 MBAFF on the 352 x 288 test pattern; all of its pairs happen to be frame pairs) |

All synthetic streams: High profile, `pic_order_cnt_type = 2`, `direct_8x8_inference_flag = 1`, spatial direct, I / P / B pictures.

### 5.3 Table 6-4 queries (optional unit test of your neighbour function)

`python3 test_mbaff.py --nb-make q.txt` writes 4536 lines
```
NBQ <id> cur=<top|bottom>-<frame|field> A=<-|frame|field> B=.. C=.. D=.. x=<xN> y=<yN> max=<16|8>
```
(`-` = that neighbouring pair is not available). Your program answers one line per query: `NBA <id> -` (not available) or `NBA <id> <where> <xW> <yW>` with `<where>` one of `CUR`, `PREV` (the top macroblock of the current pair, bottom frame macroblocks only), `A0 A1 B0 B1 C0 C1 D0 D1`. `python3 test_mbaff.py --nb-check q.txt answers.txt` compares. The expected answers are the table of 3.3, validated by the harness against a brute-force geometric derivation.

## 6. Running the harness

```
python3 test_mbaff.py --make DIR                          # writes the suite (x264 + generator streams) to DIR
# run YOUR reader on every DIR/<case>.264|.ts and write OUTDIR/<case>.dump
python3 test_mbaff.py --check DIR OUTDIR [--only a,b]
python3 test_mbaff.py --suite DIR --reader './mydec {stream} {dump}'      # all of it in one go
python3 test_mbaff.py any_stream.ts mine.dump              # any MBAFF stream (x264 -x264-params interlaced=1, ...)
python3 test_mbaff.py --expected any_stream.ts expected.dump    # the reference dump, to diff with yours
python3 test_mbaff.py --self-test                          # about one to two minutes
```

* The truth is a reference parser (`spoiler_reference/mbaff.py`) written from the standard. For an arbitrary stream it is trusted only after its own proof: every slice must end exactly, and its per-macroblock field marker, class (I4x4 / I16x16 / PCM / skip), partition shape and QP must equal ffmpeg's `-debug mb_type` / `-debug qp` maps (ffmpeg has its own, independent MBAFF implementation; there is no JM here). If the reference cannot establish the truth for your stream the harness says so and exits 2.
* The synthetic streams are produced by the same parser run backwards (`spoiler_reference/mbgen.py`: every bin or bit is drawn at random and written by an independent CABAC encoder / bit writer, with rejection of illegal values). Their agreement with the standard is *not* argued from that: ffmpeg must decode every one without a single warning, and its maps must agree, which pins down every context and every neighbour rule that parsing uses, because a wrong context desynchronises ffmpeg. Honest limits: `pm` does not influence parsing, so ffmpeg cannot confirm it in this chapter (it rests on 8.3.1.1 / 8.3.2.1 as read by the reference; Chapter 10.4's pixels confirm it), and the Table 6-4 entries parsing never touches (5.3) are checked against the geometric rule of 3.3 only.
* Failures print slice, macroblock, pair position, `fld`, key, expected and actual (first 10). Exit 0 pass, 1 fail, 2 missing / malformed output or unusable input.
* `--self-test` checks the harness: the table-driven neighbour function equals a geometric oracle on all 4536 queries; the generator manifests equal the reference parse and ffmpeg is silent; ffmpeg's maps agree on all suite streams (tens of thousands of macroblocks); the suite covers every case of 3.2 / 3.3 (printed coverage counts); a correct reader passes and corrupted dumps fail; **43 deliberately wrong rules** (every case family of Table 6-4, skip inference, the order of the skip / field flags, flag contexts, `end_of_slice` after top macroblocks, `nC`/`coded_block_flag`/`cbp`/`mb_type`/chroma-mode/`transform_size_8x8_flag` neighbours, `mvd` scaling in both directions, `ref_idx` context and range, Intra8x8 mode prediction, QP prediction, field context offsets, `first_mb_in_slice`) are each caught.

## 7. Reading failures

* **Slice does not end / "no end_of_slice before the picture ends"** with the first pair row fine: the pair-level syntax (3.2): flag read at the wrong macroblock, `end_of_slice_flag` after the top macroblock, bottom `mb_skip_flag` not read before the flag.
* **First mismatch is `fld` of a skipped pair**: inference order (left pair, then above pair, both in the same slice, else frame) or the retroactive flag of a skipped top macroblock.
* **First wrong value is `qp`/`qpd` after a skipped macroblock**: QP prediction must walk decoding order, not neighbours.
* **CABAC desync a few macroblocks into a P slice**: `mvd` vertical scaling or `ref_idx` rule (3.4); I slices fine but P slices wrong points there. **Desync only next to field pairs** in I slices: neighbour derivation (3.3), or the field context offsets of the significance map.
* **CAVLC desync with the first non-trivial `nC`**: left neighbour block of a frame MB next to a field pair, or the neighbour of a bottom MB (the top MB of its own pair for frame pairs, the pair above for field pairs).
* **`cbp` fine for frame pairs, wrong beside field pairs**: neighbouring 8x8 block index from `(xW, yW)`, not `b8 + 1`.
* **Only B or only P streams with `ref_idx`**: range doubling and presence for field MBs.
* **`pm` wrong but everything else right**: Intra4x4 / 8x8 mode prediction neighbours (8x8 / 4x4 mixture, `dcPredModePredictedFlag`).
* **`--nb-check` fails but parsing passes**: parsing never looks at odd `yN` (row parity) nor at the `yM` of C / D; those entries matter for the intra sample neighbours and for motion vector prediction in Chapter 10.4.

## 8. What's next

Chapter 10.4 reconstructs MBAFF frames on top of this parser and its neighbour function: field macroblocks predicting from reference *fields*, the motion-vector / `refIdx` scaling of 3.4 inside the median, intra prediction with per-sample neighbours, and deblocking across mixed frame/field edges.
