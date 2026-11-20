# Chapter 4.4 - Levels, total_zeros and run_before

## 1. Goal & the project

Chapter 4.3 read the first element of every residual block, `coeff_token`, and was handed the bit positions where blocks start and end. This chapter removes that crutch. You read the **rest of every block**: the signs of the trailing ones, the coefficient **levels**, `total_zeros` and the `run_before` values. With those, your reader knows exactly where each block ends, so it parses a whole slice, macroblock after macroblock, with no help, and it ends **exactly on the `rbsp_stop_one_bit`**. As a by-product you now hold the real coefficient values, in the order the transform stage (Phase 5) needs.

You will build:

- the full `residual_block_cavlc` of 7.3.5.3.2: `coeff_token`, trailing-ones signs, levels (9.2.2), `total_zeros` (9.2.3, Tables 9-7 to 9-9) and `run_before` (Table 9-10);
- the placement of the decoded values into **scan positions**, including the offset of the 15-coefficient blocks;
- the block order of a macroblock and the rule that decides which blocks are present at all;
- a dump of every macroblock's coefficients, and the end-of-slice check.

This chapter ends Phase 4. You build on 4.3's reader (neighbour bookkeeping, `nC`, `coeff_token`) and keep its output format intact; the dump only gains one key on the macroblock line and one on the `END` line.

## 2. Big picture

After `coeff_token` the decoder knows *how many* non-zero coefficients a block has (`TotalCoeff`) and that the last (highest-frequency) up to three of them are ±1 (`TrailingOnes`). It still needs three things:

1. **The values.** Trailing ones need only a sign bit each. The other levels are coded with an adaptive scheme: a unary `level_prefix` (a run of zero bits ended by a one) plus a few `level_suffix` bits. The number of suffix bits, `suffixLength`, **adapts**: it starts small and grows once big levels appear, because big levels tend to follow big levels. This is the most intricate part of CAVLC.
2. **How many zeros sit in the block before the last non-zero coefficient** (`total_zeros`). Together with `TotalCoeff` this fixes how far the block reaches.
3. **How those zeros are distributed** between the coefficients (`run_before`, once per coefficient, with the number of zeros still unplaced as context). The last coefficient needs no code: it gets whatever is left.

Everything is coded **backwards**: from the highest-frequency coefficient down to the lowest. The reason is that high frequencies are usually zero or ±1, so the cheap trailing ones come first and the adaptive level coding starts with small suffixLength.

Because every table is prefix-free and every value is derived by arithmetic, a wrong table entry or a wrong rule almost always shows up as a **desynchronised slice**: the next `coeff_token` is garbage, and the slice does not end on the stop bit. That is the main check of this chapter. It is not enough alone, though (section 6.3): a bug that changes a *value* but not a *length* (a wrong sign, a wrong offset) leaves the slice aligned. So the harness has independent checks of the values too.

## 3. Concepts in detail

### 3.1 The syntax of one block (7.3.5.3.2)

Input of the process: `maxNumCoeff` (16, 15 or 4, as in Chapter 4.3; summary table in 3.8) and `nC` (from 4.3). Output: a list `coeffLevel[0 .. maxNumCoeff-1]`, all zero except the decoded values.

1. Read `coeff_token` with the table selected by `nC`: gives `TotalCoeff` and `TrailingOnes`. If `TotalCoeff` is 0, the block is all zeros: stop.
2. `suffixLength` starts at **1 if `TotalCoeff` > 10 and `TrailingOnes` < 3**, else at 0.
3. For `i` = 0 .. `TotalCoeff`-1 (`i` = 0 is the **highest-frequency** coefficient):
   - if `i` < `TrailingOnes`: read one bit, `trailing_ones_sign_flag`: `levelVal[i]` = +1 if 0, -1 if 1;
   - otherwise decode a level (3.2).
4. If `TotalCoeff` < `maxNumCoeff`, read `total_zeros` (3.4); else it is 0. Set `zerosLeft` = `total_zeros`.
5. For `i` = 0 .. `TotalCoeff`-2: if `zerosLeft` > 0 read `run_before` (3.5) into `runVal[i]`, else `runVal[i]` = 0; then `zerosLeft` -= `runVal[i]`. For `i` = `TotalCoeff`-1: `runVal[i]` = `zerosLeft`.
6. Place the coefficients (3.6).

### 3.2 Decoding one level (9.2.2.1)

A non-trailing-one level is made of two parts.

**level_prefix**: count zero bits until the first 1 bit (the 1 is consumed). `level_prefix` is the number of zeros: `1` is prefix 0, `01` is prefix 1, `0001` is prefix 3. (Baseline, Main and Extended streams never exceed 15; High-profile streams may use 16 and more for very large levels, so a reader should accept them.)

**level_suffix**: `levelSuffixSize` bits, read as an unsigned number (absent when the size is 0):

| condition | `levelSuffixSize` |
| --- | --- |
| `level_prefix` = 14 and `suffixLength` = 0 | 4 |
| `level_prefix` >= 15 | `level_prefix` - 3 |
| otherwise | `suffixLength` |

Then compute `levelCode` (equations 9-5 to 9-8, in this order):

1. `levelCode` = (Min(15, `level_prefix`) << `suffixLength`) + `level_suffix` (suffix 0 if absent).
2. If `level_prefix` >= 15 **and** `suffixLength` = 0: `levelCode` += 15.
3. If `level_prefix` >= 16: `levelCode` += (1 << (`level_prefix` - 3)) - 4096.
4. If this is the **first level after the trailing ones** (`i` = `TrailingOnes`) **and `TrailingOnes` < 3**: `levelCode` += 2.

Why step 4: if fewer than three trailing ones were found, the encoder stopped counting because the next level was *not* ±1. So that level has magnitude at least 2 and the two smallest codes (for +1 and -1) are free; the code space is shifted by 2.

Then the sign and magnitude: if `levelCode` is even, `levelVal` = (`levelCode` + 2) >> 1; if odd, `levelVal` = (-`levelCode` - 1) >> 1. So the codes 0, 1, 2, 3, 4, 5 are the levels +1, -1, +2, -2, +3, -3.

Finally **adapt `suffixLength`** for the next level:

1. If `suffixLength` = 0, set it to 1.
2. If |`levelVal`| > (3 << (`suffixLength` - 1)) and `suffixLength` < 6, increment it.

Both steps are applied in this order after every non-trailing-one level, so one level can move `suffixLength` from 0 straight to 2 (a level of magnitude 4 or more). The thresholds are 3, 6, 12, 24, 48, 96 for `suffixLength` 1 to 6. Compare with `>`, not `>=`: |level| = 3 at suffixLength 1 does not increment.

What the escapes mean: with `suffixLength` = 0, prefixes 0..13 code levelCode 0..13 directly, prefix 14 adds a 4-bit suffix (levelCode 14..29), prefix 15 a 12-bit suffix (levelCode 30..4125), prefix 16 a 13-bit suffix and so on. With `suffixLength` > 0, prefix 15 is the escape: levelCode = (15 << `suffixLength`) + a 12-bit suffix. Levels reach ±2063 with prefix 15 at suffixLength 0; larger ones need prefix 16 and up.

### 3.3 Worked examples

Example 1, `nC` 0..1, a 16-coefficient block with scan positions 0..8 = 0, 3, -1, 0, 0, -1, 1, 0, 1 and zeros after:

| step | bits | meaning |
| --- | --- | --- |
| `coeff_token` | `0000100` | `TotalCoeff` 5, `TrailingOnes` 3 (Table 9-5, column 0..1) |
| trailing ones | `0` `0` `1` | highest first: +1 (position 8), +1 (position 6), -1 (position 5) |
| level (`i` = 3) | `01` | `suffixLength` 0: prefix 1, no suffix: levelCode 1: level -1 (position 2). `TrailingOnes` = 3, so no +2. `suffixLength` becomes 1 |
| level (`i` = 4) | `0010` | `suffixLength` 1: prefix 2, 1 suffix bit `0`: levelCode 4: level +3 (position 1). 3 > 3 is false: `suffixLength` stays 1 |
| `total_zeros` | `110` | `tzVlcIndex` 5, value 4 (zeros at positions 0, 3, 4, 7) |
| `run_before` | `10` | `zerosLeft` 4, run 1: one zero between positions 8 and 6 |
| `run_before` | `11` | `zerosLeft` 3, run 0 |
| `run_before` | `01` | `zerosLeft` 3, run 2 (positions 3 and 4) |
| `run_before` | `1` | `zerosLeft` 1, run 0 |
| (last coefficient) | none | `zerosLeft` 1: one zero below position 1 (position 0) |

All bits: `00001000010100101101011011` (26 bits). The result placed in scan order: positions 1, 2, 5, 6, 8 hold 3, -1, -1, 1, 1.

Example 2, `nC` 0..1: a block whose levels in decoding order are 2, -5, 8, 1 (scan positions 3, 2, 1, 0), so `TotalCoeff` 4, `TrailingOnes` 0 (the first level is not ±1), and no zeros:

| step | bits | meaning |
| --- | --- | --- |
| `coeff_token` | `0000000111` | `TotalCoeff` 4, `TrailingOnes` 0 |
| level 2 | `1` | level +2 is code 2; it is the first level after fewer than three trailing ones, so the coded value is 2 - 2 = 0: prefix 0. `suffixLength` 0 becomes 1 (2 > 3 is false) |
| level -5 | `00001` `1` | levelCode 9, `suffixLength` 1: prefix 9 >> 1 = 4, suffix 9 & 1 = 1 (one bit). 5 > 3: `suffixLength` 2 |
| level 8 | `0001` `10` | levelCode 14, `suffixLength` 2: prefix 3, suffix 2 (two bits). 8 > 6: `suffixLength` 3 |
| level 1 | `1` `000` | levelCode 0, `suffixLength` 3: prefix 0, suffix 0 (three bits) |
| `total_zeros` | none | `TotalCoeff` 4 < 16, so it is read: `00011` (`tzVlcIndex` 4, value 0) |

All bits: `00000001111000011000110100000011` (32 bits). (The encoder sees the same thing in reverse: from the level it computes levelCode, subtracts the shift, splits it into prefix and suffix for the current `suffixLength`.) Decode each of these two examples with a pencil before you code them.

### 3.4 total_zeros (9.2.3, Tables 9-7 to 9-9)

`total_zeros` is the number of zero coefficients **before the last non-zero coefficient in scan order** (counting from the block's first coefficient). It is read only if `TotalCoeff` < `maxNumCoeff`; the table is selected by `tzVlcIndex` = `TotalCoeff`. The coded value is at most `maxNumCoeff` - `TotalCoeff`.

- For blocks of 16 coefficients **and** of 15 coefficients (Intra16x16 AC, chroma AC) the **same tables** (9-7 and 9-8 together) are used, indexed by `TotalCoeff` 1..15. The 15-coefficient block just never reaches the entries that would need a 16th position.
- Chroma DC (4 coefficients, 4:2:0 only) uses its own Table 9-9, `tzVlcIndex` 1..3.

Rows are `tzVlcIndex`; the codes are listed for `total_zeros` 0, 1, 2, ... in order (typed from the standard; type them as strings and generate your lookup):

**Tables 9-7 / 9-8 (4x4 blocks)**

| tzVlcIndex | codes for total_zeros = 0, 1, 2, ... |
| --- | --- |
| 1 | `1` `011` `010` `0011` `0010` `00011` `00010` `000011` `000010` `0000011` `0000010` `00000011` `00000010` `000000011` `000000010` `000000001` |
| 2 | `111` `110` `101` `100` `011` `0101` `0100` `0011` `0010` `00011` `00010` `000011` `000010` `000001` `000000` |
| 3 | `0101` `111` `110` `101` `0100` `0011` `100` `011` `0010` `00011` `00010` `000001` `00001` `000000` |
| 4 | `00011` `111` `0101` `0100` `110` `101` `100` `0011` `011` `0010` `00010` `00001` `00000` |
| 5 | `0101` `0100` `0011` `111` `110` `101` `100` `011` `0010` `00001` `0001` `00000` |
| 6 | `000001` `00001` `111` `110` `101` `100` `011` `010` `0001` `001` `000000` |
| 7 | `000001` `00001` `101` `100` `011` `11` `010` `0001` `001` `000000` |
| 8 | `000001` `0001` `00001` `011` `11` `10` `010` `001` `000000` |
| 9 | `000001` `000000` `0001` `11` `10` `001` `01` `00001` |
| 10 | `00001` `00000` `001` `11` `10` `01` `0001` |
| 11 | `0000` `0001` `001` `010` `1` `011` |
| 12 | `0000` `0001` `01` `1` `001` |
| 13 | `000` `001` `1` `01` |
| 14 | `00` `01` `1` |
| 15 | `0` `1` |

**Table 9-9 (chroma DC 2x2)**

| tzVlcIndex | codes for total_zeros = 0, 1, 2, 3 |
| --- | --- |
| 1 | `1` `01` `001` `000` |
| 2 | `1` `01` `00` |
| 3 | `1` `0` |

(These strings are the ones in `common/cavlc_tables.py`, which the harness checks for prefix-freeness and completeness, and compares with libavcodec.)

### 3.5 run_before (9.2.3, Table 9-10)

`run_before` is the number of zero coefficients **immediately below** the current coefficient in scan order, before the next non-zero one. It is read for each coefficient except the last, **only while `zerosLeft` > 0**. The table is selected by `zerosLeft` (capped at 7: the last row serves 7 and more); the value is at most `zerosLeft`.

| zerosLeft | codes for run_before = 0, 1, 2, ... |
| --- | --- |
| 1 | `1` `0` |
| 2 | `1` `01` `00` |
| 3 | `11` `10` `01` `00` |
| 4 | `11` `10` `01` `001` `000` |
| 5 | `11` `10` `011` `010` `001` `000` |
| 6 | `11` `000` `001` `011` `010` `101` `100` |
| > 6 | `111` `110` `101` `100` `011` `010` `001` `0001` `00001` `000001` `0000001` `00000001` `000000001` `0000000001` `00000000001` |

Note that `zerosLeft` is the *current* count, after subtracting the runs already read; and that `total_zeros` is used only to start it. A run larger than `zerosLeft`, or `total_zeros` larger than `maxNumCoeff` - `TotalCoeff`, is a stream error.

### 3.6 Placing the coefficients, scan positions and the AC offset

After step 5 you have `levelVal[i]` and `runVal[i]` for `i` = 0 (highest frequency) .. `TotalCoeff`-1. Starting from `coeffNum` = -1 and going from `i` = `TotalCoeff`-1 **down to 0** (lowest frequency first):

- `coeffNum` += `runVal[i]` + 1;
- `coeffLevel[coeffNum]` = `levelVal[i]`.

The list `coeffLevel` is in **scan order**: entry `k` is the coefficient the encoder's zig-zag scan visited `k`-th. To use it you must map `k` to a position in the 4x4 matrix with the scan table (8.5.6). For frame macroblocks it is the zig-zag scan; as (row, column) pairs by scan position 0..15:

`(0,0) (0,1) (1,0) (2,0) (1,1) (0,2) (0,3) (1,2) (2,1) (3,0) (3,1) (2,2) (1,3) (2,3) (3,2) (3,3)`

(Field macroblocks use a different scan, 8.5.6 Table 8-12 second column; that arrives with interlaced video in Phase 10. Nothing in this chapter changes: only the position table differs.)

**The AC offset.** Blocks of 15 coefficients (Intra16x16 AC and chroma AC) do not contain the DC coefficient, which was sent separately in the DC block. Their `coeffLevel[0]` is therefore **scan position 1**, `coeffLevel[k]` is scan position `k` + 1. The matrix entry at position 0 comes from the DC block (after the DC transform, Phase 5). Forgetting the +1 puts every AC coefficient one scan position too early. Chroma DC blocks (4 coefficients) are not scanned in a zig-zag at all: `coeffLevel[k]` is the DC of chroma block `k` in raster order (`c0 c1 / c2 c3` of the 2x2 matrix).

For an Intra16x16 DC block (16 coefficients) the scan is the same zig-zag, applied to the 4x4 matrix of the 16 DC values (one per luma block, arranged by block position).

**Transform size 8x8** (High profile, `transform_size_8x8_flag` = 1). In CAVLC an 8x8 block is coded as **four 4x4 blocks** (`luma4x4BlkIdx` 4q..4q+3 of quadrant q), each with its own `coeff_token`, `nC`, levels and so on, exactly as in this chapter. Afterwards the four 16-coefficient lists are *interleaved* into the 64 coefficients of the 8x8 block: `lumaLevel8x8[64]` entry `4*k + i` is entry `k` of the 4x4 block `i`. That interleaving belongs to the 8x8 reconstruction (Phase 8); in this chapter's dump you print the four blocks as parsed, positions 0..15 of each.

### 3.7 Order and presence of blocks (7.3.5.3)

Which blocks exist is decided by the macroblock type and `coded_block_pattern` (CBP), read in 4.2. The CBP luma part has 4 bits, one per 8x8 quadrant (bit q set: quadrant q has coefficients); the chroma part is 0 (none), 1 (DC only), 2 (DC and AC). The order inside a macroblock:

1. **Intra16x16 only:** the DC block (`i16dc`, 16 coefficients). Always present.
2. **Luma.** Intra16x16: if CBP luma is 15, sixteen AC blocks (`i16ac`, 15 coefficients) in `luma4x4BlkIdx` order; if it is 0, none. I_NxN: for each quadrant q = 0..3 whose CBP bit is set, its four 4x4 blocks (`luma`, 16 coefficients), `luma4x4BlkIdx` 4q..4q+3. Quadrants whose bit is clear contribute nothing.
3. **Chroma DC:** if CBP chroma is 1 or 2: Cb DC (`cbdc`), then Cr DC (`crdc`), 4 coefficients each.
4. **Chroma AC:** if CBP chroma is 2: the four Cb blocks (`cbac`), then the four Cr blocks (`crac`), 15 coefficients each, `chroma4x4BlkIdx` 0..3.

`luma4x4BlkIdx` runs in the 8x8-quadrant order of Chapter 4.3 (0 1 4 5 / 2 3 6 7 / 8 9 12 13 / 10 11 14 15 in raster layout). An I_PCM macroblock has no residual at all, and an I_NxN macroblock with CBP 0 has no blocks and no `mb_qp_delta` (4.2).

### 3.8 Summary of the block kinds

| name | blocks | `maxNumCoeff` | `total_zeros` table | positions in the dump |
| --- | --- | --- | --- | --- |
| `i16dc` | 1 | 16 | 9-7 / 9-8 | scan positions 0..15 |
| `i16ac` | 16 | 15 | 9-7 / 9-8 | 1..15 |
| `luma` | up to 16 | 16 | 9-7 / 9-8 | 0..15 |
| `cbdc`, `crdc` | 1 each | 4 | 9-9 | 0..3 (2x2 raster index) |
| `cbac`, `crac` | 4 each | 15 | 9-7 / 9-8 | 1..15 |

## 4. The project: exactly what to build

Extend your Chapter 4.3 reader:

1. Replace the "jump to the start position" logic by the real thing: after `coeff_token`, decode trailing-ones signs, levels with the adaptive `suffixLength` (3.2), `total_zeros` (3.4) and `run_before` (3.5), then place the coefficients (3.6). Check the errors listed in 3.5 and `TotalCoeff` <= `maxNumCoeff`.
2. Process the blocks of each macroblock in the order of 3.7, with the `nC` bookkeeping of 4.3 (the stored value of a block is its `TotalCoeff`; Intra16x16 DC and chroma DC are not stored).
3. After the last macroblock of a slice (`more_rbsp_data()` is false), check that the reader stands **exactly on the `rbsp_stop_one_bit`**: report `status=complete` and its bit position as `end_bit`. If it overshoots the stop bit, or `more_rbsp_data()` stays true with no more macroblocks fitting, that is an error, not a success.
4. Print the new `coef` key and `end_bit` (section 5).

Verify: every I slice in T1 and T2 lands exactly on the stop bit (the Summary's property), and so does every CAVLC I slice of the other test inputs. The Summary's second check, "arrays match the JM trace", is replaced here by independent references, because JM is not part of this book's toolchain: see section 6.3 for what each check guarantees.

Slices you cannot parse (P and B slices, CABAC, field and MBAFF pictures: later chapters) keep the 4.1 convention: a `SLICE` block with `status=skipped` and no `MB` lines.

## 5. Harness input format

Your reader prints the dump of Chapters 4.1 to 4.3 (`SLICE`, `MB`, `PCM`, `END`, `HIST`, with all keys of 4.2 and 4.3), and **no block boundary file is read any more**. Two additions:

1. **`coef=`**: the last key of every `MB` line.
2. **`end_bit=`**: the last key of every `END` line.

### `coef`

`coef=<block>;<block>;...` lists the **coded blocks of the macroblock in decoding order** (3.7: the `kind` names are the ones in section 3.8). It is `coef=-` when the macroblock has no residual block (I_PCM, or I_NxN with CBP 0). A block is `<kind><idx>:<list>`:

- `<kind>` is one of `i16dc`, `i16ac`, `luma`, `cbdc`, `crdc`, `cbac`, `crac`; `<idx>` is `luma4x4BlkIdx` (0..15) for `i16ac` and `luma`, `chroma4x4BlkIdx` (0..3) for `cbac` and `crac`, and 0 for the three DC kinds. So the kind and index are written together, with no separator: `luma5`, `i16dc0`, `crac3`.
- `<list>` is `-` if every coefficient of the block is zero, otherwise the **non-zero** coefficients as comma-separated `pos=value` pairs in **ascending** `pos`, no spaces.
- `pos` is the **scan position** (3.6): 0..15 for `i16dc` and `luma`; **1..15** for `i16ac`, `cbac`, `crac` (the AC offset); 0..3 for `cbdc` and `crdc`. `value` is a signed decimal integer.
- A block that was *read* but has `TotalCoeff` 0 is listed with `-`; a block that is *not present* (CBP bit clear) is not listed at all.
- For an 8x8-transform macroblock the four 4x4 blocks of a quadrant are listed as parsed (`pos` 0..15 of each); no interleaving.

Example (first an Intra16x16 macroblock with CBP chroma 0 and luma 0, so only its DC block is present; then an I_NxN macroblock with CBP luma 1 (quadrant 0) and chroma 1, where `luma0` holds the block of Example 1 above, and the chroma DC of Cb is read, Cr's is all zero; then an I_PCM macroblock):

```
MB 0 17 mb_type=3 name=I_16x16_2_0_0 ... tcl=0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0 tccb=0,0,0,0 tccr=0,0,0,0 coef=i16dc0:0=-23,1=4,4=-1
MB 0 18 mb_type=0 name=I_NxN ... coef=luma0:1=3,2=-1,5=-1,6=1,8=1;luma1:-;luma2:0=2;luma3:-;cbdc0:0=5;crdc0:-
MB 0 19 mb_type=25 name=I_PCM ... coef=-
```

(The `...` stand for the keys of 4.2 and 4.3 in their usual order; the line is one line of text.)

### `end_bit`

`END <i> mbs=<n> status=<status> end_bit=<n>`. For `status=complete`, `end_bit` is the bit position, counted from the first bit of the NAL unit header like every position in this phase, **where the last macroblock ended, which is the position of the `rbsp_stop_one_bit`**. For any other status (`skipped`, `stopped`, `error`) it is a single `-`. A slice that is not exactly consumed is not `complete`.

The grammar of everything else (`SLICE`, `PCM`, `HIST`, the numbers, comments) is unchanged; see Chapter 4.1. The harness compares **the entire dump**: every key of every macroblock and every `END` line.

## 6. Running the harness

```
python3 test_levels.py <input.ts|input.264> <your_dump.txt>
python3 test_levels.py --print-expected <input> <expected.txt>   # what a correct reader prints; diff it against yours
python3 test_levels.py --make-variants <dir>                     # extra inputs, below
python3 test_levels.py --self-test                               # the harness checks itself (takes a few minutes)
```

Exit codes: 0 pass, 1 mismatch, 2 missing or malformed reader output or a problem with the input or the reference.

Inputs to run: `T1` and `T2` (the Summary's headline: I slices ending on the stop bit), `T3`, `T4` (4 slices per picture), `default` (its first I slice; P and B slices are skipped), and the files from `--make-variants`:

| file | content |
| --- | --- |
| `levels_synth.264` | synthetic stream with **known coefficient arrays**, High profile CAVLC, one macroblock per slice (section 6.3) |
| `real_1mb_slices.ts` | x264 Baseline where every macroblock is its own slice, deblocking off |
| `real_noise_1mb.ts` | the same on noise at QP 20: dense blocks, large levels |
| `real_high_cavlc.ts` | x264 High with the 8x8 transform (four-4x4-block syntax), 3 slices per picture |
| `real_lowqp.ts` | x264 at QP 4: large levels, escapes |
| `real_noise.ts` | noise at QP 12: the densest blocks |

### 6.1 What your output is compared with

The reference parser in `common/cavlc_ref.py` (parsing level 4) with `common/cavlc_resid.py`. Before the comparison the harness cross-checks the reference itself and exits 2 if any check fails (it would be the harness's fault, not yours).

### 6.2 Reading failures

The harness prints the first ten differences with slice, macroblock address, key, expected and actual. For `coef` it prints only the first differing block (expected and yours, with its index in decoding order) and the macroblock's `mb_type`, `cbp`, `qp`, `nc` and `tc`; use `--print-expected` to see whole lines. A few patterns:

| symptom | likely cause |
| --- | --- |
| `tc` / `nc` of later macroblocks wrong, `END status` not `complete`, `coef` correct for the first blocks only | one table entry or rule wrong: the first block whose `coef` differs is where to look (the harness prints it) |
| the first non-trailing-one level of a block is off by exactly 1 in magnitude (e.g. +1 instead of +2), often followed by a desynchronised slice | the +2 adjustment (3.2 step 4) forgotten, or applied although `TrailingOnes` = 3 |
| the levels of dense blocks wrong after the third or fourth level | `suffixLength` adaptation: `>` versus `>=`, or the 0 to 1 step, or the cap at 6 |
| only blocks with `TotalCoeff` > 10 wrong | `suffixLength` must start at 1 when `TotalCoeff` > 10 and `TrailingOnes` < 3 |
| a huge level wrong, everything else fine | escapes: prefix 14 with `suffixLength` 0 (4-bit suffix), prefix 15 (+15 when `suffixLength` is 0, 12-bit suffix), prefix >= 16 |
| every AC block (`i16ac`, `cbac`, `crac`) shifted by one position | the AC offset (3.6) |
| everything right but a stop-bit mismatch | you stop at `more_rbsp_data()` but check one bit off: `end_bit` is the position *of* the stop bit, i.e. the first bit after the last macroblock |
| coefficients in a block mirror-reversed | you placed the lowest-frequency coefficient first (3.6) |

### 6.3 What each check guarantees (honest accounting)

There is no JM reference decoder here, so the truth is established otherwise. The harness checks the reference in four independent ways before it trusts it.

1. **Every I slice runs exactly to the stop bit (all real and synthetic inputs).** This is strong evidence that every table entry, every code length and every syntax rule *that affects how many bits are consumed* is right, on a lot of real x264 data. It does *not* prove values: a wrong sign, a wrong level offset that does not change the code length, wrong scan positions or a wrong AC offset all leave a slice aligned.
2. **A synthetic stream with known coefficient arrays (`levels_synth.264`).** `levgen.py` is an *encoder-side* implementation: it starts from coefficient arrays that it chooses (every `TotalCoeff`/`total_zeros` pair, every `zerosLeft`/`run_before` pair of Table 9-10, `level_prefix` 0 to 16, `suffixLength` 0 to 6 with levels on both sides of every threshold, the +2 rule, the start at 1 for `TotalCoeff` > 10) and writes the bits. The reference's decoded arrays must equal them exactly. And ffmpeg, a third implementation, must decode the stream without errors and produce **pixels that equal an independent mini reconstruction** (`minirecon.py`: dequantisation and inverse transform with flat scaling, the Phase 5 maths in miniature) of those arrays. To make this possible every macroblock is alone in its slice, so every intra prediction is the trivial DC value; QP is picked so that few samples clip. About 97 % of the non-zero coefficients change the decoded pixels when altered by 1, so the pixel comparison really tests the values (very large levels at low QP are less sensitive; they are validated by alignment and by the generator equality). So on this stream: *reference = generator = ffmpeg's decoder* for the values, including the escapes and the AC offset.
3. **Real data, real values.** The first macroblock of a slice has no neighbours, so its prediction is 128 (and DC): for such a macroblock the chroma samples, the Intra16x16 luma samples and the first 4x4 luma block of an I_NxN macroblock are exactly mini-reconstruction(coefficients). The harness applies this to the first macroblock of every slice of any input whose deblocking is off (T1, T2, and `real_1mb_slices.ts` / `real_noise_1mb.ts` where *every* macroblock is a slice-first macroblock): hundreds of real x264 macroblocks whose decoded coefficients reproduce ffmpeg's pixels. Not covered by pixels: luma blocks 1..15 of I_NxN macroblocks (their prediction uses neighbouring blocks: Phase 5) and 8x8-transform macroblocks' luma; those rely on checks 1 and 2.
4. **ffmpeg's own views:** its macroblock-type map and its QP map equal the reference's, and the code tables are checked for prefix-freeness, completeness and against libavcodec.

ffmpeg's `-debug dct_coeff` was checked and prints nothing for H.264, so there is no coefficient dump to diff against; the checks above replace it. A mistake that can still slip through: a value error in a luma block 1..15 of a real I_NxN macroblock that is *also* absent from the synthetic stream's patterns. The synthetic stream was built to make that very unlikely, and Phase 5 closes the gap: when your reconstruction matches `ref.yuv` bit for bit, every coefficient of every real macroblock is confirmed.

The `--self-test` makes the harness prove its own teeth: known-good dumps must pass; edited dumps (a value off by one, a flipped sign, a coefficient at the wrong scan position, a dropped or swapped block, a missing key, a wrong `end_bit`) must fail; a reference with any of twelve classic bugs (+2 adjustment, `suffixLength` start, `>=` threshold, no increment, prefix 14, escape offsets, prefix 16, trailing-ones sign, run table index, chroma DC `total_zeros` table, placement order, AC offset) must be rejected by the cross-checks; table typos in the reference and encoder-side bugs in the generator must be detected through ffmpeg; and a wrong zig-zag entry in the mini reconstruction must be detected by the pixel comparison.

## 7. Debugging tips

- Test the block decoder alone first: write Examples 1 and 2 of section 3.3 as bit strings and check that you decode exactly those coefficients, and that your decoder consumes exactly 26 and 32 bits.
- Make the reader print, for the first macroblock with a mismatch, every block's bit start, `nC`, `TotalCoeff`, `TrailingOnes` and, per level, `level_prefix`, `suffixLength` and the level. Compare with `--print-expected`: the `coef` of the first wrong block names it.
- Keep `levelCode` and `levelVal` in separate variables and write the four steps of 3.2 in the order of the Readme; most level bugs are step-order bugs.
- Test the tables by round trip: build each table as strings, then check that decoding every code word returns its own entry and that no code word is a prefix of another.
- A slice that ends a few bits early or late almost always means a wrong `nC` or a wrong table, not a wrong level rule; look at the first block whose `tc` differs from the expected `tc` list.

## 8. What's next and a recap of Phase 4

**Phase 4 recap.** You can now take an I slice of a CAVLC stream and parse everything inside it: the slice-data loop and `mb_type` (4.1), prediction modes, CBP and QP deltas (4.2), neighbour availability, `nC` and `coeff_token` (4.3), and the levels, `total_zeros` and `run_before` that complete each block (4.4). Per macroblock you know its type, prediction modes, QP and the exact coefficients in scan order, and every slice ends on its stop bit. Nothing has been turned into pixels yet; the reader only *parses*.

**Phase 5** turns this into the first picture. It takes the coefficient lists and the prediction modes from this phase and reconstructs the samples: frame buffers and a YUV writer (5.1, with I_PCM samples copied straight in), dequantisation and the inverse transforms (5.2: the 4x4 transform, the Intra16x16 DC and chroma DC transforms, chroma QP from its table), Intra16x16 and chroma prediction (5.3, the first picture: T1), Intra 4x4 prediction (5.4: T2), and QP tracking with multiple slices per picture (5.5: T4). The mini reconstruction used by this chapter's harness is a preview of 5.2; the goal of Phase 5 is T1, T2 and T4 decoding byte-identical to `ref.yuv`, ffmpeg's output.
