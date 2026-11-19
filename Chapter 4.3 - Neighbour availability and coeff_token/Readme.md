# Chapter 4.3 - Neighbour availability and coeff_token

## 1. Goal & the project

Chapters 4.1 and 4.2 read everything of an intra macroblock up to its residual. This chapter reads the **first element of every residual block: `coeff_token`**. It tells you how many non-zero coefficients the block has (`TotalCoeff`) and how many of them are "trailing ones" (`TrailingOnes`). To decode it you must know `nC`, a number computed from the blocks to the left of and above the current block. So the real work of this chapter is **bookkeeping**: for every 4x4 block of every macroblock you remember how many coefficients it had, and for every new block you find the right two neighbours, across macroblock and slice edges.

You will build:

- the numbering of 4x4 blocks and the "who is my left / above neighbour" function (6.4.11.4 for luma, 6.4.11.5 for chroma);
- the per-slice store of `total_coeff` per 4x4 block, with the special cases (I_PCM counts as 16, uncoded blocks count as 0, Intra16x16 DC is not stored);
- `nC` (9.2.1) and the choice of one of five code tables;
- a `coeff_token` decoder for Table 9-5 (four ranges of `nC`, plus the chroma DC table).

Honest scope. The syntax *after* `coeff_token` (levels, `total_zeros`, `run_before`) is Chapter 4.4. Without it your reader cannot know where the next block starts. So for this chapter the harness hands your reader a small file listing **the bit position where each residual block starts and ends** (section 5). Your reader jumps to each start, decodes the `coeff_token` there with the `nC` it derived itself, and prints what it found. Everything this chapter teaches is exercised: positions are the only thing you are given for free. In Chapter 4.4 you replace the file by the syntax you learn there.

You build on Chapter 4.2's reader (slice loop, `mb_type`, prediction syntax, CBP, `mb_qp_delta`). The "stop at the first coefficient" rule of 4.2 disappears: every slice is now read to its end.

## 2. Big picture

A coefficient block (a 4x4 grid of transform coefficients after quantisation) is mostly zeros. CAVLC (context-adaptive variable-length coding) sends it as a count, then the non-zero values, then where the zeros are. The count comes first and is coded with a **variable-length code**: short code words for likely counts, long for unlikely ones.

What is likely depends on the picture content right there. A block next to busy blocks probably has many coefficients; a block in a flat region has few. The decoder already knows its neighbours' counts, so the encoder and decoder both use them to choose **which code table** to read the count with. That choice is `nC`:

- `nC` 0 or 1 (flat area): a table with very short code words for "0 coefficients" (one bit);
- `nC` 2 to 3, 4 to 7: tables that spread the short codes over larger counts;
- `nC` 8 or more (busy area): a fixed 6-bit code, because every count is about equally likely.

If you pick the wrong table, the bits are still "valid" for some other count, so you do not get an error. You get **garbage and a desynchronised slice**, usually much later. That is why this step is checked by reading whole slices to their end, not just by looking at tokens.

## 3. Concepts in detail

### 3.1 4x4 block numbering

A macroblock has 16 luma 4x4 blocks, `luma4x4BlkIdx` 0 to 15. The numbering is **not** raster order. It follows the four 8x8 quadrants (6.4.3): the four blocks of the top-left quadrant first, then top-right, bottom-left, bottom-right; inside a quadrant, again top-left, top-right, bottom-left, bottom-right.

```
 luma4x4BlkIdx at each position (x, y in 4x4 units)
        x=0  x=1  x=2  x=3
 y=0     0    1    4    5
 y=1     2    3    6    7
 y=2     8    9   12   13
 y=3    10   11   14   15
```

Conversions: position to index: `idx = 8*(y/2) + 4*(x/2) + 2*(y%2) + (x%2)` with `x`, `y` in 4x4 units; index to position: `x = 2*((idx>>2)&1) + (idx&1)`, `y = 2*((idx>>3)&1) + ((idx>>1)&1)`. Build both as 16-entry tables and test them against each other.

Chroma (4:2:0): each of Cb and Cr is 8x8 samples, so it has four 4x4 blocks, `chroma4x4BlkIdx` 0 to 3 in plain raster order (0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right). Cb and Cr are separate planes with separate counts.

This is the order of the blocks in the residual, too. For an `I_NxN` macroblock: for each quadrant whose CBP luma bit is set, its four blocks in index order (the quadrants with a clear bit contribute nothing). With `transform_size_8x8_flag` = 1 nothing changes in CAVLC: each 8x8 block is sent as four 4x4 blocks (their coefficients are interleaved into the 8x8 block, Chapter 4.4), each with its own `coeff_token`, and each of them counts as one 4x4 block for the rules below.

### 3.2 Which blocks have a coeff_token, and their `maxNumCoeff`

| block | when | count of blocks | max coefficients | `nC` |
| --- | --- | --- | --- | --- |
| `i16dc`: Intra16x16 DC | Intra16x16, always | 1 | 16 | from luma block 0 (section 3.5) |
| `i16ac`: Intra16x16 AC | Intra16x16 with `luma_cbp` = 15 | 16, index order | 15 | from the block's own neighbours |
| `luma`: I_NxN 4x4 block | CBP luma bit of its quadrant set | up to 16, index order | 16 | same |
| `cbdc`, `crdc`: chroma DC | CBP chroma 1 or 2 | one for Cb, then one for Cr | 4 | -1 |
| `cbac`, `crac`: chroma AC | CBP chroma 2 | 4 for Cb, then 4 for Cr, index order | 15 | from the block's own neighbours (chroma) |

Order inside a macroblock: Intra16x16 DC, then the luma blocks (AC or 4x4), then Cb DC, Cr DC, then the four Cb AC and the four Cr AC. The `kind` names in the table are the ones used in the harness files. `maxNumCoeff` matters for 4.4 and for one check here: a `TotalCoeff` larger than the block's `maxNumCoeff` (for example 16 in an AC block, whose maximum is 15) is a stream error. Table 9-5 does contain `TotalCoeff` 16 entries; they are only legal in the 16-coefficient blocks.

### 3.3 Neighbouring 4x4 blocks (6.4.11.4, 6.4.12, 6.4.13.1)

For a luma block at position (x, y) in 4x4 units inside the macroblock:

- the **left neighbour A** is the block at (x-1, y);
- the **above neighbour B** is the block at (x, y-1).

If the neighbour position is inside the current macroblock (x-1 >= 0, respectively y-1 >= 0), it is simply that block of the same macroblock. It always has a smaller index, so it is already read. If it is outside:

- A with x = 0: the block at (3, y) of the **left macroblock** (`mbAddr - 1`);
- B with y = 0: the block at (x, 3) of the **macroblock above** (`mbAddr - PicWidthInMbs`).

The two cases of 6.4.12 you need are only these (the corner macroblocks of 6.4.12 are for intra *prediction*, not here). Then convert the position inside that macroblock to an index with the formula of section 3.1.

Chroma blocks (6.4.11.5, neighbouring 4x4 chroma blocks): the same, in the 2x2 arrangement, within the same plane. A block at (x, y) in {0,1}x{0,1}: A is (x-1, y) or, if x = 0, block (1, y) of the left macroblock's same plane; B is (x, y-1) or, if y = 0, block (x, 1) of the macroblock above, same plane.

**Availability.** A neighbouring macroblock is *available* only if all of these hold (6.4.8 and 6.4.9):

1. it is inside the picture (for A: the current macroblock is not in the leftmost column; for B: the current macroblock is not in the top row);
2. it has already been decoded (its address is not greater than the current one);
3. it belongs to **the same slice** as the current macroblock.

Condition 3 is the one people forget: a macroblock in a different slice of the same picture is unavailable even though it is decoded. Slices are independent, so a decoder can lose or reorder them. With one slice per macroblock row (T4 uses 4 slices per picture), every top-row macroblock of a slice has no B. With plain slices and no FMO/ASO, "decoded and same slice" simply means: you read it earlier in the *current* slice. So keep the bookkeeping **per slice** (reset it at each slice start), or store a slice number per macroblock and compare.

Blocks *inside* the current macroblock are always available when they are neighbours. An empty (uncoded) block of the current macroblock is available with count 0.

### 3.4 What is stored per 4x4 block

After each macroblock you keep, for each of its 16 luma blocks and 4 + 4 chroma blocks, one number: `total_coeff`, the value `nN` that a later neighbour will read.

| macroblock / block | stored value |
| --- | --- |
| a block that was read | its `TotalCoeff` from `coeff_token` |
| a block that was **not coded** (its quadrant's CBP luma bit is 0, CBP chroma < 2 for chroma AC, `I_NxN` with CBP 0, Intra16x16 with `luma_cbp` 0) | 0 |
| Intra16x16 | the `TotalCoeff` of each **AC** block. The **DC** block's `TotalCoeff` is *not* stored anywhere (it is "a separate block"): block 0's stored value is its AC count, or 0 |
| chroma DC blocks | not stored (the DC token has nC = -1 and no neighbours) |
| `I_PCM` | 16 for every luma and chroma block |
| 8x8-transform `I_NxN` (CAVLC) | each of its four 4x4 blocks keeps its own `TotalCoeff` |

(In P and B slices a skipped macroblock also stores 0. It is not needed for this chapter.)

### 3.5 nN, nC and the table choice (9.2.1)

For the block being read, derive the neighbours A and B as above. Then `nA` is the stored value of neighbour A (if A is available), `nB` the same for B. The **rule**:

| A available | B available | `nC` |
| --- | --- | --- |
| yes | yes | `(nA + nB + 1) >> 1` (the average, rounded up) |
| yes | no | `nA` |
| no | yes | `nB` |
| no | no | 0 |

An unavailable neighbour is *not* "0 coefficients": with only one neighbour, `nC` is that neighbour's count, not its half. (With a count of 0 you would get `(nA + 0 + 1) >> 1`.) Test this at picture and slice edges.

Which block's neighbours: for a luma, Intra16x16 AC or chroma AC block, its own. **For the Intra16x16 DC block, use luma block 0** (position (0,0)): its neighbours A and B are in the left and above macroblocks. For chroma DC blocks, `nC` is **-1** and no neighbours are looked at.

The range of `nC` selects the code table:

| `nC` | table | codes |
| --- | --- | --- |
| -1 | D (chroma DC, 2x2) | variable length |
| 0, 1 | A | variable length |
| 2, 3 | B | variable length |
| 4 to 7 | C | variable length |
| 8 or more (up to 16) | fixed 6-bit | see 3.6 |

Special cases and what is out of scope:

- An `I_PCM` neighbour has stored value 16 in all its blocks. Next to an `I_PCM` macroblock, with the other neighbour 0, `nC = (16 + 0 + 1) >> 1 = 8`: the 6-bit table. Next to two PCM neighbours, nC = 16.
- If the current macroblock is intra, `constrained_intra_pred_flag` is 1, the neighbouring macroblock is *inter*, and the slice uses data partitioning (`nal_unit_type` 2 to 4), then `nN` is 0 instead of the stored value. Data partitioning does not occur in Baseline, Main or High; it is Extended profile. This chapter's I slices have no inter neighbours, so you can ignore the rule, but write it down in your notes for P slices.
- `nC` can be at most 16 (both neighbours 16).

### 3.6 Table 9-5: coeff_token

The code word depends on the table and the pair (`TotalCoeff`, `TrailingOnes`). `TrailingOnes` is how many of the *last* (highest-frequency) non-zero coefficients, up to three, have magnitude exactly 1; so `TrailingOnes` is at most `min(3, TotalCoeff)`. Each table therefore has 62 entries (1 + 2 + 3 + 14 x 4), the chroma DC table has 14 (`TotalCoeff` at most 4).

Reading: read bits one at a time and stop when the bits so far are a code word of the table. Because no code word is the beginning of another one (the codes are *prefix-free*), this is unambiguous. The longest code word has 16 bits. A faster implementation builds a lookup table indexed by the next 16 bits (as the Summary suggests: **type the table as strings of bits and generate your lookup from them**, never as hand-written numbers).

The fixed-length table (`nC` >= 8) is regular: `TotalCoeff` 0 with `TrailingOnes` 0 is `000011`; every other entry is the 6-bit number `((TotalCoeff - 1) << 2) | TrailingOnes`. For example `TotalCoeff` 1, `TrailingOnes` 1 is `000001`, `TotalCoeff` 2, `TrailingOnes` 0 is `000100`, `TotalCoeff` 16, `TrailingOnes` 3 is `111111`.

The three variable-length tables and the chroma DC table (typed from the standard; bits as printed, first bit first; `-` means the combination does not exist):

**nC from 0 to 1 (table A)**

| TotalCoeff | T1 = 0 | T1 = 1 | T1 = 2 | T1 = 3 |
| --- | --- | --- | --- | --- |
| 0 | `1` | - | - | - |
| 1 | `000101` | `01` | - | - |
| 2 | `00000111` | `000100` | `001` | - |
| 3 | `000000111` | `00000110` | `0000101` | `00011` |
| 4 | `0000000111` | `000000110` | `00000101` | `000011` |
| 5 | `00000000111` | `0000000110` | `000000101` | `0000100` |
| 6 | `0000000001111` | `00000000110` | `0000000101` | `00000100` |
| 7 | `0000000001011` | `0000000001110` | `00000000101` | `000000100` |
| 8 | `0000000001000` | `0000000001010` | `0000000001101` | `0000000100` |
| 9 | `00000000001111` | `00000000001110` | `0000000001001` | `00000000100` |
| 10 | `00000000001011` | `00000000001010` | `00000000001101` | `0000000001100` |
| 11 | `000000000001111` | `000000000001110` | `00000000001001` | `00000000001100` |
| 12 | `000000000001011` | `000000000001010` | `000000000001101` | `00000000001000` |
| 13 | `0000000000001111` | `000000000000001` | `000000000001001` | `000000000001100` |
| 14 | `0000000000001011` | `0000000000001110` | `0000000000001101` | `000000000001000` |
| 15 | `0000000000000111` | `0000000000001010` | `0000000000001001` | `0000000000001100` |
| 16 | `0000000000000100` | `0000000000000110` | `0000000000000101` | `0000000000001000` |

**nC from 2 to 3 (table B)**

| TotalCoeff | T1 = 0 | T1 = 1 | T1 = 2 | T1 = 3 |
| --- | --- | --- | --- | --- |
| 0 | `11` | - | - | - |
| 1 | `001011` | `10` | - | - |
| 2 | `000111` | `00111` | `011` | - |
| 3 | `0000111` | `001010` | `001001` | `0101` |
| 4 | `00000111` | `000110` | `000101` | `0100` |
| 5 | `00000100` | `0000110` | `0000101` | `00110` |
| 6 | `000000111` | `00000110` | `00000101` | `001000` |
| 7 | `00000001111` | `000000110` | `000000101` | `000100` |
| 8 | `00000001011` | `00000001110` | `00000001101` | `0000100` |
| 9 | `000000001111` | `00000001010` | `00000001001` | `000000100` |
| 10 | `000000001011` | `000000001110` | `000000001101` | `00000001100` |
| 11 | `000000001000` | `000000001010` | `000000001001` | `00000001000` |
| 12 | `0000000001111` | `0000000001110` | `0000000001101` | `000000001100` |
| 13 | `0000000001011` | `0000000001010` | `0000000001001` | `0000000001100` |
| 14 | `0000000000111` | `00000000001011` | `0000000000110` | `0000000001000` |
| 15 | `00000000001001` | `00000000001000` | `00000000001010` | `0000000000001` |
| 16 | `00000000000111` | `00000000000110` | `00000000000101` | `00000000000100` |

**nC from 4 to 7 (table C)**

| TotalCoeff | T1 = 0 | T1 = 1 | T1 = 2 | T1 = 3 |
| --- | --- | --- | --- | --- |
| 0 | `1111` | - | - | - |
| 1 | `001111` | `1110` | - | - |
| 2 | `001011` | `01111` | `1101` | - |
| 3 | `001000` | `01100` | `01110` | `1100` |
| 4 | `0001111` | `01010` | `01011` | `1011` |
| 5 | `0001011` | `01000` | `01001` | `1010` |
| 6 | `0001001` | `001110` | `001101` | `1001` |
| 7 | `0001000` | `001010` | `001001` | `1000` |
| 8 | `00001111` | `0001110` | `0001101` | `01101` |
| 9 | `00001011` | `00001110` | `0001010` | `001100` |
| 10 | `000001111` | `00001010` | `00001101` | `0001100` |
| 11 | `000001011` | `000001110` | `00001001` | `00001100` |
| 12 | `000001000` | `000001010` | `000001101` | `00001000` |
| 13 | `0000001101` | `000000111` | `000001001` | `000001100` |
| 14 | `0000001001` | `0000001100` | `0000001011` | `0000001010` |
| 15 | `0000000101` | `0000001000` | `0000000111` | `0000000110` |
| 16 | `0000000001` | `0000000100` | `0000000011` | `0000000010` |

**nC = -1, chroma DC (table D)**

| TotalCoeff | T1 = 0 | T1 = 1 | T1 = 2 | T1 = 3 |
| --- | --- | --- | --- | --- |
| 0 | `01` | - | - | - |
| 1 | `000111` | `1` | - | - |
| 2 | `000100` | `000110` | `001` | - |
| 3 | `000011` | `0000011` | `0000010` | `000101` |
| 4 | `000010` | `00000011` | `00000010` | `0000000` |


How to check your typing of the tables:

- every table must be prefix-free (no code word begins another): test every pair;
- the number of entries must be 62 (14 for table D) and every (`TotalCoeff`, `TrailingOnes`) with `TrailingOnes <= min(3, TotalCoeff)` must be present;
- the harness's reference tables pass the same checks, and they equal the tables found inside ffmpeg's decoding library (the harness compares them when it can find the library).

A typo in the long codes of rarely used entries only shows up on streams that use them; that is why one of the harness inputs (section 6) contains **every** entry of every table.

### 3.7 After the coeff_token (preview of 4.4)

If `TotalCoeff` is 0 the block ends right after the token. Otherwise the block continues with the signs of the trailing ones (1 bit each), then the other levels, `total_zeros` (unless the block is full) and the `run_before` values. The harness's block file hides all that: it tells you where the *next* block begins.

## 4. The project: exactly what to build

Extend your Chapter 4.2 reader. Slice selection, `SLICE` / `END` / `PCM` / `HIST` lines, `I_PCM` handling and everything before the residual stay as they were.

**Inputs.** The stream, and the block boundary file produced by the harness (`--print-blocks`, section 5). Read the boundary file into a table keyed by (slice index, macroblock address): the list of its blocks in order, each with `kind`, `idx`, `start`, `end`.

**State per slice.** Reset at the start of every slice: the stored `total_coeff` per 4x4 block for every macroblock read so far in the slice (luma 16, Cb 4, Cr 4), and which macroblocks of the slice have been read. (A picture-wide array plus a slice number per macroblock works too.)

**Per macroblock.** Read `mb_type`, the prediction syntax, CBP and `mb_qp_delta` as in 4.2. For `I_PCM` store 16 in every block and continue after the samples as before. Otherwise:

1. Work out the list of blocks of this macroblock in decoding order (section 3.2) from the macroblock type and the CBP. Start with all stored values of this macroblock at 0.
2. For each block of the list, in order:
   - derive `nC` (section 3.5; chroma DC: -1);
   - set the read position to the block's `start` from the boundary file (for the first block this equals the position right after `mb_qp_delta`; checking that is a good test of your 4.2 part);
   - decode the `coeff_token` with the table selected by `nC`; record `nC`, `TotalCoeff`, `TrailingOnes`;
   - if it is a luma, Intra16x16 AC or chroma AC block, store its `TotalCoeff` (Intra16x16 DC and chroma DC: do not store);
   - treat a `TotalCoeff` above the block's `maxNumCoeff` as an error.
3. After the last block, set the read position to that block's `end`. If the macroblock has no block at all (`I_NxN` with CBP 0), the position stays where `mb_qp_delta` / CBP ended, as in 4.2.
4. Print the macroblock line (section 5).

After each macroblock, `more_rbsp_data()` decides as before whether another macroblock follows. A slice you could read ends with status `complete`. There is no "stopped" any more. If the input has a slice that is not an I slice, or uses CABAC, handle it as in 4.1 (`skipped`).

Cheap self-checks against the boundary file: a `coeff_token` with `TotalCoeff` 0 must end exactly at the block's `end`; one with `TotalCoeff` > 0 must end before it. When either fails, the cause is almost always a wrong `nC` (hence the wrong table).

**Verify.** The Summary's "diff the first slice against the JM trace" cannot be done (JM is not available here). Instead, the harness compares `nC`, `TotalCoeff` and `TrailingOnes` of **every block of every macroblock** of every I slice with a reference parser, and the stored counts after every macroblock. Section 6 says why that reference can be trusted.

## 5. Harness input format

### 5.1 The block boundary file (you read it)

Produced by `python3 test_coeftoken.py --print-blocks <input> <blocks.txt>`. Text, one record per line:

```
SLICE <slice_index> blocks=<n>
BLK <slice_index> <mb_addr> <kind> <idx> start=<bit> end=<bit>
```

- One `SLICE` line per slice the reference parsed (slices that are not CAVLC I slices are absent), followed by its `BLK` lines in decoding order (macroblock by macroblock, blocks in the order of section 3.2).
- `<kind>` is one of `i16dc luma i16ac cbdc crdc cbac crac`; `<idx>` is `luma4x4BlkIdx` 0 to 15 for `luma` and `i16ac`, `chroma4x4BlkIdx` 0 to 3 for `cbac` and `crac`, and 0 for the DC kinds.
- `start` is the bit position (counted from the first bit of the NAL unit header, the same convention as `bit=` and `header_end_bit=`) of the first bit of the block's `coeff_token`. `end` is the position of the first bit after the block. Block *k+1* starts at the `end` of block *k*; the next macroblock starts at the `end` of the last block of this one.
- Macroblocks without any coded block have no `BLK` line. `I_PCM` macroblocks have none.

Example, the first macroblock of T2 (slice 0; `I_NxN`, CBP 47; the first 3 of 26 blocks, and the last Cr AC block):

```
SLICE 0 blocks=2219
BLK 0 0 luma 0 start=85 end=143
BLK 0 0 luma 1 start=143 end=151
BLK 0 0 luma 2 start=151 end=166
...
BLK 0 0 crac 3 start=1467 end=1520
BLK 0 1 luma 0 start=1576 end=1577
```

(Macroblock 0 spans bits 32 to 1520. The next macroblock begins at bit 1520, its header from `mb_type` to `mb_qp_delta` takes 56 bits, and its first block starts at 1576.)

### 5.2 Your output

Chapter 4.2's grammar (one record per line, space-separated `key=value`, `#` comments and blank lines ignored, extra keys allowed, keys in any order, the same `SLICE`, `PCM`, `END`, `HIST` lines) with five more keys on every `MB` line:

```
MB <slice_index> <mb_addr> mb_type=.. name=.. i16_mode=.. chroma_cbp=.. luma_cbp=.. bit=.. \
   t8x8=.. modes=.. chroma_pred=.. cbp=.. qp_delta=.. qp=.. \
   nc=<list|-> tc=<list|-> tcl=<16 numbers> tccb=<4 numbers> tccr=<4 numbers>
```

(One line; the backslashes only wrap it.) The five keys, exactly:

| key | meaning | format |
| --- | --- | --- |
| `nc` | the `nC` you used for each coded block of the macroblock, in decoding order (so one number per `BLK` line of the macroblock; chroma DC blocks give `-1`) | integers joined by commas, no spaces; `-` if the macroblock has no coded block (`I_PCM`, or `I_NxN` with CBP 0) |
| `tc` | the decoded `coeff_token` of each coded block, same order | `TotalCoeff/TrailingOnes` pairs joined by commas, e.g. `8/3,1/1,0/0`; `-` as for `nc` |
| `tcl` | the `total_coeff` stored for the 16 luma blocks **after** the macroblock, in `luma4x4BlkIdx` order 0 to 15 | 16 integers joined by commas. 0 for uncoded blocks; AC counts only for Intra16x16; 16 everywhere for `I_PCM` |
| `tccb` | stored values of the 4 Cb blocks, `chroma4x4BlkIdx` 0 to 3 | 4 integers joined by commas (0 if no AC; 16 for `I_PCM`) |
| `tccr` | the same for Cr | 4 integers |

Notes: `nc` and `tc` list **every** `coeff_token` of the macroblock, including those with `TotalCoeff` 0 and the Intra16x16 DC and chroma DC ones. `tcl`, `tccb`, `tccr` are always present (all `0`s when nothing is coded). The earlier keys keep their meaning from 4.1 and 4.2, including `qp` and `bit`.

Example lines: the first macroblock of T2 (all 16 luma blocks read; blocks 1 and 2 have `nC` 8 because their only neighbour, block 0, has 8 coefficients; block 3 has `nC = (2 + 1 + 1) >> 1 = 2`, from blocks 2 and 1; chroma DC is -1; and so on); the second macroblock's first block (`nC` 0: its left neighbour is block 5 of macroblock 0, which has 0 coefficients, and there is no macroblock above); an Intra16x16 macroblock with CBP chroma 2 and luma 0 (`nc` = DC, Cb DC, Cr DC, 8 chroma AC blocks; `tcl` is all 0: no AC); and an `I_PCM`:

```
MB 0 0 mb_type=0 name=I_NxN i16_mode=- chroma_cbp=- luma_cbp=- bit=32 t8x8=0 modes=-1,1,0,-1,1,1,5,7,0,6,0,4,2,-1,-1,-1 chroma_pred=0 cbp=47 qp_delta=0 qp=23 nc=0,8,8,2,1,3,9,8,2,8,1,8,16,14,13,12,-1,-1,0,14,14,10,0,12,12,10 tc=8/3,1/1,2/2,15/0,3/2,0/0,16/0,15/0,1/0,15/1,0/0,13/0,12/1,12/1,11/0,11/0,4/0,4/0,14/2,6/1,13/0,7/0,12/3,7/2,12/0,7/0 tcl=8,1,2,15,3,0,16,15,1,15,0,13,12,12,11,11 tccb=14,6,13,7 tccr=12,7,12,7
MB 259 19 mb_type=11 name=I_16x16_2_2_0 i16_mode=2 chroma_cbp=2 luma_cbp=0 bit=40 t8x8=- modes=i16:2 chroma_pred=0 cbp=32 qp_delta=0 qp=29 nc=0,-1,-1,0,6,6,9,0,10,10,12 tc=5/1,2/2,3/1,6/2,14/1,3/0,7/2,10/0,12/3,12/0,11/0 tcl=0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0 tccb=6,14,3,7 tccr=10,12,12,11
MB 281 7 mb_type=25 name=I_PCM i16_mode=- chroma_cbp=- luma_cbp=- bit=1389 t8x8=- modes=- chroma_pred=- cbp=- qp_delta=- qp=20 nc=- tc=- tcl=16,16,16,16,16,16,16,16,16,16,16,16,16,16,16,16 tccb=16,16,16,16 tccr=16,16,16,16
```

(The Intra16x16 line comes from the synthetic stream, where the macroblock is the first of its slice: its DC block has `nC` 0.) The numbers in the first line can be checked by hand: `tcl` shows the `TotalCoeff` of blocks 0 to 15 of the `tc` list, the first 16 entries (block 3 is `15/0`, so `tcl[3] = 15`).

Forward compatibility: Chapter 4.4 adds coefficient keys to the same line; these five keep their meaning.

## 6. Running the harness

```
python3 test_coeftoken.py --print-blocks <input.ts|input.264> <blocks.txt>     # 1. the boundary file for your reader
<your reader> <input> <blocks.txt> > <mb_dump.txt>                              # 2. your program
python3 test_coeftoken.py <input.ts|input.264> <mb_dump.txt>                    # 3. check
python3 test_coeftoken.py --print-expected <input> <out.txt>                    # the expected dump, to `diff` with yours
python3 test_coeftoken.py --make-variants <out_dir>                             # extra inputs, see below
python3 test_coeftoken.py --self-test                                           # the harness checks itself (about 30 s)
```

Exit code 0 pass, 1 fail (the first ten differences are listed with slice, address, key, expected and actual, plus the expected `nc` / `tc` / `tcl` of the macroblock), 2 malformed or missing output, or a "HARNESS PROBLEM" (a bug in the book's tooling, see below).

The inputs to try, in order:

- `.cache/T2.ts`, then `T1`, `T3`, `T4`: real Baseline I pictures from x264, 4 slices per picture in `T4`. Whole slices are read, with about 120 000 blocks per file. A wrong `nC` shows up within a few macroblocks.
- `tokens_synth.264` (from `--make-variants`): 96x80, 31 IDR pictures written by a generator that chooses every block's `TotalCoeff` and `TrailingOnes`. It contains **every entry of Table 9-5 in all four `nC` ranges and of the chroma DC table** (the coverage pictures: one macroblock per slice so that `nC` is under the generator's control), `I_PCM` macroblocks next to coded blocks (`nC` 8, 16), Intra16x16 with and without AC, `I_NxN` with every CBP, the 8x8 transform flag on, and several slices per picture (neighbours across slice edges). `tokens_synth.manifest.json` beside it is for the harness.
- `real_high_cavlc.ts`: real High-profile CAVLC with the 8x8 transform (each 8x8 block as four 4x4 `coeff_token`s).
- `real_lowqp.ts`: Baseline at QP 6, many coefficients, so `nC` >= 8 on real data.
- `real_noise.ts`: noise at QP 22, dense blocks.
- `default.ts`: only its first I slice is read; P and B slices are `skipped`. `T10_full.ts` is CABAC: every slice `skipped`.

**What the harness compares against, and why you can trust it.** The expected output is `common/cavlc_ref.py` at parsing level 3 (with `common/cavlc_resid.py`), a reference parser written for this book. It parses everything of every block, not only the `coeff_token`, because that is the only way to find the next block. ffmpeg offers no debug output of per-block coefficient counts (checked: `-debug dct_coeff`, `-debug bitstream`, `-debug qp` and `-debug mb_type` show no counts), so the reference is validated by properties that cannot be satisfied by accident:

| check | applies to | guarantee |
| --- | --- | --- |
| every slice ends exactly on the `rbsp_stop_one_bit` | every input, every I slice | one wrong `nC`, table entry, block order or neighbour rule desynchronises the following bits; reading 50 slices x 396 macroblocks to the exact end is practically impossible with an error. This is the strongest check |
| ffmpeg macroblock map and QP map | every input | macroblock classes and `QP_Y` at every address (as in 4.1 / 4.2) |
| code table checks | always | Table 9-5 is prefix-free, has the right entry counts, and equals the tables inside the installed libavcodec |
| generator manifest | `tokens_synth.264` | `nC`, `TotalCoeff`, `TrailingOnes`, start/end bit of every block and the stored counts, from an independent bit writer: it derives `nC` with its own neighbour grid, shares no code with the reference except the code tables (which are checked against libavcodec), and encodes every complete block |
| coverage | `tokens_synth.264` | the reference sees all 4 x 62 + 14 `coeff_token` entries |
| ffmpeg decodes the synthetic stream | `tokens_synth.264` | no error message (ffmpeg checks that every slice ends exactly where its syntax ends and rejects illegal codes), ffmpeg's QP map of every macroblock equals the generator's, `I_PCM` pixels are unchanged |

What is **not** confirmed by ffmpeg directly: the individual `TotalCoeff` of a block in real streams. It is confirmed indirectly by the exact slice ends and, for the synthetic stream, by the independent writer. If a cross-check fails the harness prints "HARNESS PROBLEM" and exits 2.

`--self-test` builds the inputs, runs the checks on known-good dumps of eight inputs (must PASS), then runs deliberately wrong dumps (a wrong `nC`, a wrong `TotalCoeff`, `TotalCoeff` and `TrailingOnes` swapped, wrong stored counts, `I_PCM` stored as 0, missing keys, a wrong status), then dumps from readers with six classic bugs (`(nA + nB) >> 1`, `I_PCM` counted as 0, the Intra16x16 DC count stored in block 0, an unavailable neighbour treated as 0, the above neighbour ignored, chroma DC with `nC` 0); all must FAIL. Finally it breaks the reference (the same bugs, a typo in Table 9-5) and the generator (two swapped entries in three of the tables) on purpose and requires the cross-checks to notice.

## 7. Reading failures / debugging tips

- **The very first macroblock of a slice has wrong `nc`**: the first block of a slice has no neighbours (`nC` 0). Block 1 of the first macroblock has only A, block 2 only B: `nC` is the neighbour's count, not an average.
- **`nc` right for the first few blocks, wrong later in the same macroblock**: block order. Use the z-order of section 3.1; a raster loop gives different neighbours for blocks 4 to 7 and others.
- **`nc` wrong only in the top row of slices / macroblocks after a slice edge**: availability across slices. A macroblock above in another slice is unavailable even though you decoded it.
- **`nc` is 8 where you expect 1, next to an `I_PCM`**: that is correct, see section 3.5. If your value is 0 or half of that, you store `I_PCM` blocks as 0.
- **Intra16x16 macroblocks wrong**: the DC block uses block 0's neighbours and is not stored; the AC counts are stored, uncoded AC blocks (when `luma_cbp` is 0) store 0 even though the DC count is large.
- **`tcc?` wrong with chroma CBP 1**: the AC blocks were not coded, so their stored counts are 0.
- **`tc` looks plausible, but the next macroblock is all wrong**: you did not jump to `end`. The harness decodes only the token; the position comes from the file.
- **Slices fail with "end mismatch"-like effects in your own run, tokens look fine for a while and then garbage**: that is the typical sign of one wrong code word in Table 9-5 or a wrong `nC` range boundary (2, 4, 8). Test your table against the one in section 3.6: prefix-free, 62 entries, and decode each of its entries from its own bits.
- **TrailingOnes greater than TotalCoeff or TotalCoeff above 16**: a table typo.
- Use `--print-expected` and `diff` to find the first differing macroblock quickly.

## 8. What's next

Chapter 4.4 reads the rest of every block: trailing-one signs, `level_prefix` / `level_suffix` with the adaptive `suffixLength`, `total_zeros` and `run_before`. It reuses everything here (the block order, `nC`, the stored counts) and lets you drop the block boundary file: the end of one block is then the start of the next, and every slice must land exactly on `rbsp_trailing_bits`.
