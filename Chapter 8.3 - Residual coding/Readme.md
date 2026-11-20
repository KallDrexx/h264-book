# Chapter 8.3 - Residual coding (CABAC)

## 1. Goal and the project

Chapter 8.2 decoded everything in a CABAC macroblock except the **residual**: the quantised transform coefficients. That was the bulk of the bits, and 8.2's HINT file jumped over it with an `end` hint. This chapter removes those hints. You decode the residual yourself, down to the coefficient values, and each slice must end with `end_of_slice_flag` = 1 at exactly the last macroblock.

The syntax elements are `coded_block_flag`, `significant_coeff_flag`, `last_significant_coeff_flag`, `coeff_abs_level_minus1` and `coeff_sign_flag`, for the block categories `ctxBlockCat` 0 to 4 (Intra16x16 DC and AC, Luma 4x4, chroma DC and AC).

Your program prints the macroblock dump of 8.2 with one more key, `coef`. It holds the coefficients of every residual block in the same shape as the CAVLC chapters' `coef` key. So everything after parsing (dequantisation, transforms, prediction, Chapters 5 to 7) does not care which entropy coder produced the numbers.

What stays: the `cbp` hints. For inter macroblocks, `sub_mb_type`, `ref_idx` and `mvd` still stand between `mb_type` and `coded_block_pattern`. Chapter 8.4 removes them.

Out of scope: `ctxBlockCat` 5, the blocks of the 8x8 transform, is deferred to Chapter 8.5 (the harness refuses PPSs with `transform_8x8_mode_flag` = 1). Field pictures and MBAFF are also out (their significance contexts differ).

## 2. Big picture

A residual block is a list of up to 16 numbers in zig-zag scan order, mostly zeros. CAVLC (Chapter 4) sent it as "how many non-zeros, then their values, then the zero runs". CABAC sends it in **three passes**, each made of context-coded bins:

1. **`coded_block_flag`**: is there anything in this block at all? One bin. 0 means all-zero and the block ends here.
2. **Significance map**: walk the scan positions from 0. For each, one bin "is this coefficient non-zero?" (`significant_coeff_flag`). After every non-zero one, a second bin "was that the last non-zero one?" (`last_significant_coeff_flag`). That tells the decoder where the non-zeros are and where the block ends, with no run lengths.
3. **Levels**, in **reverse** scan order (last non-zero first): the magnitude minus 1 (`coeff_abs_level_minus1`, a short context-coded prefix and, for big values, an Exp-Golomb escape in bypass mode), then the sign as one bypass bin.

Why reverse order? The magnitudes tend to be largest at low frequencies (the start of the scan) and 1 at the end. Coding the 1s first lets the contexts learn "so far only ones", and counting how many ones and larger-than-one levels were already seen is exactly what the level contexts use.

The contexts are chosen as before: `ctxIdx = ctxIdxOffset + ctxBlockCatOffset + ctxIdxInc`. What is new compared with 8.2 is the amount of bookkeeping: `coded_block_flag` looks at **per-block** flags of the neighbours, so you store one flag per 4x4 block (and per DC block) for every macroblock.

## 3. Concepts in detail

### 3.1 Which blocks a macroblock has, and in what order

(`residual()` in 7.3.5, 8-bit 4:2:0.) `CBP luma` bit `b8` and `CBP chroma` are those of 8.2 (for I_16x16 they come from `mb_type`). A macroblock has residual syntax if it is I_16x16 or its CBP is not 0. Blocks, in decoding order:

| Order | Block | Present when | ctxBlockCat | Coefficients | Dump kind, idx |
|---|---|---|---|---|---|
| 1 | Intra16x16 DC | I_16x16 (always) | 0 | 16 | `i16dc`, 0 |
| 2 | Intra16x16 AC, 16 blocks | I_16x16 and luma CBP = 15 | 1 | 15 | `i16ac`, 0..15 |
| 2' | Luma 4x4, for each 8x8 quadrant `b8` = 0..3 its four 4x4 blocks | not I_16x16, CBP bit `b8` set | 2 | 16 | `luma`, 0..15 |
| 3 | Chroma DC Cb, then Cr | chroma CBP >= 1 | 3 | 4 | `cbdc`, `crdc`, 0 |
| 4 | Chroma AC: Cb blocks 0..3, then Cr blocks 0..3 | chroma CBP = 2 | 4 | 15 | `cbac`, `crac`, 0..3 |

The 4x4 luma block index `luma4x4BlkIdx` runs in the zig-zag-of-8x8 order of Chapter 4.2: block `i` sits at 4x4 position x = `(i % 2) + 2 * ((i / 4) % 2)`, y = `(i / 2) % 2 + 2 * (i / 8)`. The 4 chroma AC blocks of a plane are in raster order (0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right).

The 15-coefficient blocks leave out position 0 of the scan: the DC value of those blocks travels in the Intra16x16 DC or chroma DC block. So list entry `k` of such a block is the coefficient at scan position `k + 1`. A CBP bit may be set while all four of its blocks have `coded_block_flag` 0; that is legal.

### 3.2 Context index layout

Every element has a base (`ctxIdxOffset`) and, per `ctxBlockCat`, an extra offset. For frame-coded macroblocks (the only ones here):

| Element | Base | Offset by ctxBlockCat 0, 1, 2, 3, 4 |
|---|---|---|
| `coded_block_flag` | 85 | 0, 4, 8, 12, 16 |
| `significant_coeff_flag` | 105 | 0, 15, 29, 44, 47 |
| `last_significant_coeff_flag` | 166 | 0, 15, 29, 44, 47 |
| `coeff_abs_level_minus1` | 227 | 0, 10, 20, 30, 39 |

Use the context-initialisation tables of Chapter 8.1 for these indices (85 to 104, 105 to 226 and 227 to 275). Field-coded macroblocks use a different set of significance contexts, never needed here. Context 276 is not one of them (it is the terminate bin).

### 3.3 coded_block_flag (9.3.3.1.1.9)

One bin, `ctxIdx = 85 + offset(cat) + ctxIdxInc`, with `ctxIdxInc = condTermFlagA + 2 * condTermFlagB`. A and B are the blocks of **the same kind** to the left and above (the left and above 4x4 luma blocks, the left and above chroma AC block of the same plane, and for DC blocks the same DC block of the left and above **macroblock**). Inside the current macroblock a neighbour is simply a block you decoded earlier in this macroblock, with its flag (0 if that block was not coded because its CBP bit is 0). When the neighbour lies in another macroblock `N`, `condTermFlagN` is:

| Situation | condTermFlagN |
|---|---|
| `N` does not exist or is in another slice (unavailable), current macroblock **intra** | **1** |
| `N` unavailable, current macroblock **inter** | 0 |
| `N` is I_PCM | **1** |
| `N` is skipped | 0 |
| `N` is available but that block is not coded: luma block whose CBP bit is 0 (an I_16x16 `N` counts as luma CBP 0 or 15); chroma AC with chroma CBP < 2; chroma DC with chroma CBP = 0; Intra16x16 DC when `N` is not I_16x16 | 0 |
| otherwise | the `coded_block_flag` that was decoded for that block |

Intra macroblocks therefore see the picture edge as "full" and start with `coded_block_flag` contexts that expect 1; inter macroblocks the opposite.

A subtle point is that an Intra16x16 AC block and a Luma 4x4 block share the 4x4 luma flag store: the neighbouring 4x4 block's flag is the flag of whichever of those it was coded as. The Intra16x16 **DC** flag is separate and only the DC blocks of neighbouring I_16x16 macroblocks count for it. The two chroma planes are separate: Cr looks at the Cr flags of the neighbours.

**Worked example 1.** Current macroblock I_NxN, luma 4x4 block index 1 (x = 1, y = 0). A is block 0 of this macroblock, decoded with flag 1, so `condTermFlagA` = 1. B is in the macroblock above, an I_NxN macroblock with luma CBP bit 2 set; the block at x = 1, y = 3 lies in that macroblock's 8x8 quadrant 2, and its flag was 0, so `condTermFlagB` = 0. `ctxIdxInc` = 1, `ctxIdx` = 85 + 8 + 1 = **94**.

**Worked example 2.** Cb DC block of an I_NxN macroblock at the left picture edge (A unavailable, intra: 1), B available with chroma CBP 0 (not coded: 0). `ctxIdxInc` = 1, `ctxIdx` = 85 + 12 + 1 = **98**.

### 3.4 Significance map (9.3.3.1.3)

Let `numCoeff` be the block's coefficient count (16, 15 or 4) and `i` the index in the block's list (0 to `numCoeff - 1`). For `i` from 0 to `numCoeff - 2`:

1. Decode `significant_coeff_flag` with `ctxIdx = 105 + offset(cat) + ctxIdxInc`.
2. If it is 1, decode `last_significant_coeff_flag` with `ctxIdx = 166 + offset(cat) + ctxIdxInc`. If that is 1, stop: no more coefficients.

If the loop gets to `i = numCoeff - 1` without a "last", that final coefficient is **inferred** significant; no bins are sent for it. (So a block whose only non-zero is the final one costs `numCoeff - 1` significance bins with value 0.)

`ctxIdxInc` (the same for the two flags): for categories 0, 1, 2 and 4 it is simply `i`, the index in the list: 0..14 for the 16-coefficient blocks (Intra16x16 DC, Luma 4x4) and 0..13 for the 15-coefficient blocks (the last list position never gets a context). For **chroma DC** (category 3) it is `Min(i / NumC8x8, 2)`, with `NumC8x8` = 1 for 4:2:0, so the increments are 0, 1, 2 for the three coded positions. Note that for the 15-coefficient blocks `i` counts from the list start, not from the scan position, so Intra16x16 AC list entry 0 (scan position 1) uses increment 0.

**Worked example 3 (Luma 4x4, frame).** Non-zero scan positions 0, 2 and 5, all later ones 0. Bins, with `ctxIdx` = 134 + `i` for significance (105 + 29) and 195 + `i` for last (166 + 29): i = 0: significant 1 (ctx 134), last 0 (195); i = 1: significant 0 (135); i = 2: significant 1 (136), last 0 (197); i = 3, 4: significant 0 (137, 138); i = 5: significant 1 (139), last 1 (200). Stop.

### 3.5 Levels and signs (9.3.2.3, 9.3.3.1.3)

Levels are decoded for the significant positions from the **last** one back to the first. For each:

**`coeff_abs_level_minus1`** is binarised as a *prefix* (truncated unary, cMax = 14) and, only when the prefix is all ones (14 of them), a *suffix* in Exp-Golomb of order 0 using **bypass** bins. Let `numDecodAbsLevelEq1` and `numDecodAbsLevelGt1` be the number of levels already decoded **in this block** whose absolute value was exactly 1, respectively greater than 1. Contexts, base `227 + offset(cat)`:

| Bin | ctxIdxInc |
|---|---|
| bin 0 | `(numDecodAbsLevelGt1 != 0) ? 0 : Min(4, 1 + numDecodAbsLevelEq1)` |
| bins 1 to 13 | `5 + Min(4 - (ctxBlockCat == 3 ? 1 : 0), numDecodAbsLevelGt1)` |

So bin 0 means "is the magnitude above 1?" and there are four contexts for it while only ones were seen so far, then context 0 once a bigger level has appeared. Bins 1 to 13 use five contexts, four for chroma DC. (For 4:2:0 chroma DC has only four coefficients, so `numDecodAbsLevelGt1` is at most 3 when a bin 1+ is decoded and the cap never actually bites; implement it anyway.)

The prefix value is the number of ones before the first 0 (or 14 if there are 14 ones). **Suffix** (when the prefix is 14): Exp-Golomb order 0, all bypass: count ones until a 0, say `k` ones; add `2^j` for each of the ones (that is `2^k - 1`), then read `k` more bits, most significant first, and add them. Then `coeff_abs_level_minus1` = 14 + suffix value, and the level's magnitude is that plus 1.

After the magnitude, **`coeff_sign_flag`**: one bypass bin, 1 = negative.

**Worked example 4 (levels of example 3).** Levels in decoding order: position 5 (+1), position 2 (-1), position 0 (+3). Block base 227 + 20 = 247.
- Position 5: bin 0 ctxIdxInc = `Min(4, 1 + 0)` = 1 (ctx 248), value 0, so magnitude 1. Sign bypass 0. Now Eq1 = 1.
- Position 2: bin 0 ctxIdxInc = `Min(4, 1 + 1)` = 2 (ctx 249), value 0. Sign bypass 1. Eq1 = 2.
- Position 0: bin 0 ctxIdxInc = 3 (ctx 250), value 1, so magnitude > 1. Bin 1 ctxIdxInc = `5 + Min(4, 0)` = 5 (ctx 252), value 1; bin 2 (ctx 252) value 0. The prefix is two ones then a zero, so `coeff_abs_level_minus1` = 2 and the magnitude is 3. Sign bypass 0.

Result: coefficients at 0, 2, 5 = 3, -1, 1: dump text `luma0:0=3,2=-1,5=1` (for luma block 0).

**Worked example 5 (escape).** Magnitude 21 gives `coeff_abs_level_minus1` = 20: prefix = 14 ones, suffix value 6. Exp-Golomb: 6 >= 1 so a 1 and 6 - 1 = 5; 5 >= 2 so a 1 and 3; 3 < 4 so a 0; then 2 bits for 3: `1 1`. Bypass bins `1 1 0 1 1`. Check: `(2^2 - 1) + 3 = 6`.

### 3.6 Assembling the coefficient array

For a block with `coded_block_flag` = 1 you now know the significant list indices `p1 < p2 < ... < pm` (the last one is `pm`) and the levels `v_m, ..., v_1` in the order they arrived. The coefficient array has zeros everywhere except list index `p_j` = `v_j`. **Scan order is kept**: placing the values into the 4x4 matrix (inverse zig-zag) is dequantisation's business (Chapter 5.2), exactly as for CAVLC. A block with `coded_block_flag` = 0 is all zero.

### 3.7 Bookkeeping that lives across macroblocks

Keep, per macroblock of the current slice: its kind (I_PCM? skipped? I_16x16?), luma CBP and chroma CBP (from 8.2), and the decoded flags: 16 luma 4x4 flags, the Intra16x16 DC flag, 2 chroma DC flags, 2 x 4 chroma AC flags. A flag of a block that was not coded is 0. An I_PCM macroblock needs no stored flags if your neighbour code tests "is I_PCM" first (table in 3.3). Flags of skipped macroblocks are never needed for the same reason. Nothing else about the residual changes the engine; after the macroblock's last block, `end_of_slice_flag` follows as in 8.2 and QP handling is unchanged.

## 4. The project: exactly what to build

Extend your Chapter 8.2 slice parser:

1. After `mb_qp_delta` (every I_16x16 has one; other macroblocks have one when CBP != 0), decode the macroblock's residual blocks in the order of 3.1. Do **not** use `end` hints any more: they no longer exist.
2. Implement 3.3 to 3.7. Print every coded block (with `coded_block_flag` 1 or 0) in the `coef` key.
3. Keep the `cbp` hints exactly as in 8.2 for inter macroblocks other than B_Direct_16x16.
4. **Verify** (the Summary's check): on a T10 intra-only file the `coef` keys are right, and each slice ends with `end_of_slice_flag` = 1 at exactly the last macroblock. Then the same on T10 P and B streams.

**Optional end-to-end check.** If your decoder already reconstructs intra pictures (Chapters 5 and 6), feed it these coefficients: for an **intra-only** CABAC stream (for example `T10_intra`, which has no deblocking) your output YUV must equal ffmpeg's. `python3 test_cabac_resid.py --yuv INPUT.ts your.yuv` does this comparison with `common/yuvcmp.py` (first differing frame, plane, macroblock and pixel); `your.yuv` is the cropped yuv420p file of Chapter 5.1, all frames in output order. This check is optional and not needed to pass the chapter. It works only on intra-only streams: for P and B streams it cannot pass yet, because `ref_idx` and `mvd` are only decoded in Chapter 8.4.

## 5. Harness input and output formats

Plain text, one record per line, decimal numbers, `#` starts a comment, blank lines are ignored. `python3 test_cabac_resid.py --make-variants DIR` writes the test streams (four synthetic `syn_res_N.264` with a `.manifest.json` you can ignore, and seven x264 `.ts` files from the recipes `T10_intra`, `T10_p`, `T10_b`, `T10_slices_aq`, `T10_qp46`, `T10_qp8`, `T10_full`) with, for each, `<name>.hints.txt` and `<name>.expected.txt`. For another CABAC stream: `python3 test_cabac_resid.py --print-expected hints INPUT hints.txt`.

### 5.1 Input: the HINT file

```
HINT <slice> <addr> cbp range=<codIRange> offset=<codIOffset> bit=<bits consumed>
```

Same as Chapter 8.2, but **only** `cbp` lines exist now (no `end`). `slice` is the 0-based ordinal of the slice NAL unit (types 1 and 5) and `addr` the macroblock address. A `cbp` line is the engine state just before `coded_block_pattern` of an inter macroblock other than B_Direct_16x16 (after `mb_type`, jumping over `sub_mb_type`, `ref_idx` and `mvd`). `bit` counts bits of the NAL unit after removing emulation prevention bytes, from the first bit of the NAL header byte; loading a hint sets `codIRange`, `codIOffset` and your bit position. I-only streams have an empty hint file. Contexts need no transfer: the skipped syntax (`sub_mb_type`, `ref_idx`, `mvd`) uses contexts 21 to 23 and 36 to 59 only, which this chapter never touches; the residual contexts (85 to 275) are all decoded by you.

### 5.2 Output: the macroblock dump

```
SLICE <slice> type=<I|P|B> first_mb=<first_mb_in_slice>
MB <slice> <addr> mbt=<..> cpred=<..> modes=<..> cbp=<..> qpd=<..> qp=<..> coef=<..> eos=<0|1>
...
END <slice> mbs=<number of MB lines>
```

The first six keys and `eos` are exactly those of Chapter 8.2 (same values, same rules); `coef` sits between `qp` and `eos` (it is decoded before the `end_of_slice_flag`). All eight keys must be present, in this order.

`coef` is `-` when the macroblock has no residual syntax (skipped, I_PCM, I_NxN or inter with CBP 0). Otherwise it is the list of the macroblock's blocks in decoding order, separated by `;`:

```
<kind><idx>:<list>
```

`kind` and `idx` are those of the table in 3.1. `<list>` is `-` if `coded_block_flag` was 0, else the non-zero coefficients as comma separated `pos=value` pairs in ascending `pos`, no spaces. `pos` is the **scan position** 0..15 for blocks of 16 coefficients; for the 15-coefficient blocks (`i16ac`, `cbac`, `crac`) it is the list index plus 1 (1..15); for chroma DC (`cbdc`, `crdc`) it is the index 0..3 of the DC coefficient. A coded block always has at least one pair. Every block that has `coded_block_flag` syntax appears, so an I_16x16 macroblock with luma CBP 0 and chroma CBP 0 prints `i16dc0:...` only.

Example:

```
SLICE 0 type=I first_mb=0
MB 0 0 mbt=0 cpred=0 modes=p10p115706042ppp cbp=15 qpd=0 qp=23 coef=luma0:0=3,2=-1,5=1;luma1:-;luma2:0=1;luma3:15=-2;luma4:-;luma5:-;luma6:-;luma7:-;luma8:-;luma9:-;luma10:-;luma11:-;luma12:-;luma13:-;luma14:-;luma15:- eos=0
MB 0 1 mbt=19 cpred=2 modes=- cbp=31 qpd=-3 qp=20 coef=i16dc0:0=-17,1=24;i16ac0:1=2,3=1;... eos=0
```

(Abbreviated; a real slice ends with `eos=1` on its last macroblock.) A real I_16x16 with luma CBP 15 lists 16 `i16ac` blocks after the DC block and the chroma blocks after those.

## 6. Running the harness

```
python3 test_cabac_resid.py INPUT.ts your_dump.txt
python3 test_cabac_resid.py --yuv INPUT.ts your.yuv              # optional, intra-only streams
python3 test_cabac_resid.py --print-expected dump INPUT.ts expected.txt
python3 test_cabac_resid.py --self-test
```

Exit 0 = pass, 1 = your dump differs (the first wrong macroblock of each slice, the differing fields and a hint about the rule to look at), 2 = unusable input or output.

What it compares against: (1) a reference parser that decodes **everything** in the slices, accepted only if `end_of_slice_flag` = 1 comes exactly at the last macroblock and the stop bit follows. A single wrong context or level anywhere breaks that, so this is a strong proof for every x264 slice; (2) ffmpeg's `-debug mb_type` and `-debug qp` maps; (3) synthetic streams from an independent CABAC **encoder**: random residual in every block category, `coded_block_flag` next to I_PCM, skipped, intra, inter and unavailable neighbours, escape levels up to 3000, blocks whose last position is significant; their expected `coef` comes from the construction, and ffmpeg decodes them cleanly; (4) for intra-only streams without deblocking, the coefficients are reconstructed with the arithmetic of Chapters 5.2 to 5.4 and the pixels must equal ffmpeg's byte for byte. `--self-test` also runs 32 deliberately wrong rules in the reader (wrong ctxBlockCat offsets, neighbour conditions, significance and level contexts, UEG0 details, sign, order); each must be rejected, and the output lists which streams catch each.

**Spoiler warning:** `spoiler_reference/` holds the reference parser, the encoder, the stream generator and the reconstruction. Don't open it before your own parser passes.

## 7. Reading failures

* **First macroblock wrong in `coef`, but `mbt`, `cbp` and `qp` right:** decide whether it is the first block (coded_block_flag or significance contexts) or a later one (usually the neighbour rule). The `coef` of a block that decoded wrongly usually looks plausible for a while, then absurd.
* **First block right, the next one wrong:** the flag store (3.7), or the level counters not reset per block (`numDecodAbsLevelEq1` and `Gt1` start at 0 for **each** block).
* **Wrong only at picture edges or slice starts:** unavailable neighbours: intra counts 1, inter counts 0 (3.3).
* **Wrong next to an I_PCM macroblock:** I_PCM neighbours count 1 for every flag.
* **Values right but signs wrong, or all signs flipped:** 1 means negative; order: the sign follows each magnitude, in the reverse scan order.
* **Big values wrong, small ones right:** the escape. Prefix is exactly 14 ones before the suffix; the suffix is Exp-Golomb order 0, most significant bits first after the ones.
* **Chroma AC / Intra16x16 AC positions shifted by one:** list index versus scan position (`pos` = list index + 1); the significance contexts still use the list index.
* **Only chroma DC wrong:** `Min(i, 2)` increments, level bins 1+ capped at `5 + 3`.
* **Slice ends early or runs on:** any of the above; also check that I_16x16 always decodes its DC block, and that AC blocks of an I_16x16 appear only when luma CBP = 15.
* **`--yuv` mismatch but `coef` right:** not this chapter; look at dequantisation, prediction and QP (Chapters 5 and 6).

## 8. What's next

Chapter 8.4 decodes the remaining inter syntax: the P and B `sub_mb_type` binarisations, `ref_idx` and `mvd` (UEG3, with contexts from the neighbours' absolute `mvd` sums). That removes the `cbp` hints. After it your decoder reads complete default x264 Main streams without any help.
