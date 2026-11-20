# Chapter 8.2 - Macroblock-level syntax elements (CABAC)

## 1. Goal and the project

Chapter 8.1 gave you the arithmetic decoding engine and the context tables. This chapter puts meaning on the bins: you decode the **macroblock-level syntax elements** of a CABAC slice, macroblock after macroblock, using the neighbouring macroblocks to choose contexts:

`mb_skip_flag`, `mb_type` (I, P and B slices), `prev_intra4x4_pred_mode_flag`, `rem_intra4x4_pred_mode`, `intra_chroma_pred_mode`, `coded_block_pattern`, `mb_qp_delta`, `end_of_slice_flag`, and the I_PCM escape.

Your program reads a CABAC stream and prints one line per macroblock with these values. A harness compares the lines with a reference. One problem has to be solved first: real macroblocks continue with syntax from later chapters (residual coefficients in 8.3, `sub_mb_type` / `ref_idx` / `mvd` in 8.4), and in CABAC you cannot skip bits, because the engine state depends on every bin. Section 4 explains the **HINT file**, which hands you the engine state at those points so you can still test this chapter on complete real streams. Chapters 8.3 and 8.4 delete the hints one by one.

Scope note: the plan (Summary) puts the P and B `mb_type` binarisations into Chapter 8.4. They are decoded **here** instead, because they share contexts with the intra types (section 3.4, last paragraph); Chapter 8.4 keeps `sub_mb_type`, `ref_idx` and `mvd`.

Out of scope here: `mb_field_decoding_flag` (it exists only in MBAFF streams, which the harness refuses), `transform_size_8x8_flag` (needs the High-profile transform, Chapter 8.5; PPSs with `transform_8x8_mode_flag` = 1 are refused) and field pictures.

## 2. Big picture

Every syntax element is turned into a string of **bins** (its *binarisation*). Each bin is decoded with `DecodeDecision` using one context, `ctxIdx = ctxIdxOffset + ctxIdxInc`:

* `ctxIdxOffset` depends on the syntax element (and slice type), for example 60 for `mb_qp_delta`. It is the start of that element's block of contexts (the orientation table in Chapter 8.1, section 3.5).
* `ctxIdxInc` depends on the **bin number** within the string and, for the first bin of several elements, on the **neighbouring macroblocks**. That neighbour dependence is what this chapter is about.

Three binarisation shapes occur: **unary** (`mb_qp_delta`: n ones then a zero), **fixed length** (`rem_intra4x4_pred_mode`: 3 bins, `coded_block_pattern` luma: 4 bins) and **hand-made trees** (`mb_type`, `intra_chroma_pred_mode`).

Contexts adapt, so one wrong `ctxIdx` for even one bin changes the state of that context for every later use. Errors do not stay local: the next macroblocks decode as garbage. That is why the harness reports the **first wrong macroblock** of each slice.

## 3. Concepts in detail

### 3.1 The slice data loop

In a CABAC slice, after the slice header and the alignment bits (Chapter 8.1), repeat for `CurrMbAddr = first_mb_in_slice, +1, ...` (no FMO, so addresses simply count up):

1. In P and B slices: `mb_skip_flag`. If 1 the macroblock is skipped (P_Skip / B_Skip) and has no other syntax.
2. Otherwise: `mb_type` and the rest of the macroblock layer (3.4 to 3.9).
3. **`end_of_slice_flag`** (a terminate bin, Chapter 8.1) after **every** macroblock, skipped ones included. 1 ends the slice.

### 3.2 Neighbours A and B and what "available" means

For macroblock `addr` in a picture `W` macroblocks wide: **A** is the left neighbour `addr - 1` (not existing when `addr % W == 0`), **B** is the one above, `addr - W`. A neighbour is *available* only if it exists and belongs to the **same slice** (that is, its address is not smaller than the slice's `first_mb_in_slice`). At a slice start or a picture edge it is unavailable, and every rule below says what that means. Keep one small record per macroblock of the current slice: skipped?, I_PCM?, I_NxN?, intra?, B_Skip/B_Direct_16x16?, `intra_chroma_pred_mode`, luma and chroma CBP.

### 3.3 mb_skip_flag (9.3.3.1.1.1)

One bin, `ctxIdxOffset` 11 in P slices, 24 in B slices. `ctxIdxInc = condTermFlagA + condTermFlagB`, where `condTermFlagN` is **0** if N is unavailable or skipped, otherwise **1**.

Example: P slice, A skipped, B coded: inc = 0 + 1 = 1, so ctxIdx 12 (B slice: 25).

### 3.4 mb_type

**I slices** (offset 3). Bin 0 uses `ctxIdx = 3 + condTermFlagA + condTermFlagB`; here `condTermFlagN` is 0 if N is unavailable or I_NxN, else 1 (so I_16x16 and I_PCM count 1). Table of bins (the first macroblock rows of Chapter 8.1 are the same):

| Bin | Meaning | ctxIdx |
|---|---|---|
| b0 | 0 = I_NxN (`mb_type` 0), done | 3 + inc (0..2) |
| b1 | terminate bin; 1 = I_PCM (`mb_type` 25), done | (terminate) |
| b2 | luma CBP is 15 | 3 + 3 = 6 |
| b3 | chroma CBP is not 0 | 3 + 4 = 7 |
| b4 | only if b3 = 1: chroma CBP is 2 (else 1) | 8 |
| next two | Intra16x16 prediction mode, most significant bin first | 9, 10 (3 + 6, 3 + 7) |

`mb_type` = 1 + prediction mode + 4 * chroma CBP + 12 * (luma CBP is 15 ? 1 : 0). Example: bins `1, 0, 1, 1, 0, 1, 0` (terminate bin is the second) mean luma 15, chroma nonzero, chroma = 1, mode `10` = 2: `mb_type` = 1 + 2 + 4 + 12 = **19**. For I_16x16 the CBP is *not* coded separately; it is read off the type.

**P slices.** Table 7-13 (inter types, CABAC never sends P_8x8ref0, value 4). Bin 0 (ctx 14) = 1 means "intra macroblock, an I `mb_type` follows as a suffix". Inter types:

| mb_type | name | bins (b0 b1 b2) |
|---|---|---|
| 0 | P_L0_16x16 | 0 0 0 |
| 1 | P_L0_L0_16x8 | 0 1 1 |
| 2 | P_L0_L0_8x16 | 0 1 0 |
| 3 | P_8x8 | 0 0 1 |

Contexts: b0 = 14, b1 = 15, b2 = 16 if b1 = 0 else 17. **Intra suffix** (offset 17): s0 = ctx 17 (0 = I_NxN); then the terminate bin (1 = I_PCM); then luma s2 = 17 + 1; chroma-nonzero s3 = 17 + 2; if chroma nonzero: chroma-is-2 = 17 + 2; the two mode bins 17 + 3 and 17 + 3. Their meaning is as for the I slice. The dump reports `mbt` = 5 + the I value (Table 7-13: intra types are 5 to 30).

**B slices** (Table 7-14). Bin 0 (`ctxIdx = 27 + condTermFlagA + condTermFlagB`; here `condTermFlagN` is 0 if N is unavailable, B_Skip or B_Direct_16x16, else 1). Then:

* b0 = 0: B_Direct_16x16 (`mb_type` 0).
* b0 = 1, b1 (ctx 27 + 3) = 0: one more bin b2 (ctx 27 + 5): `mb_type` = 1 + b2 (B_L0_16x16 / B_L1_16x16).
* b0 = 1, b1 = 1: read four bins `bits` = b2 b3 b4 b5, most significant first, b2 with ctx 27 + 4, b3 to b5 with ctx 27 + 5. Then: `bits` < 8: `mb_type` = bits + 3. `bits` = 13: intra, an I suffix follows (offset 32, same shape as the P suffix). `bits` = 14: `mb_type` 11. `bits` = 15: `mb_type` 22 (B_8x8). `bits` 8..12: read one more bin (ctx 27 + 5) and `mb_type` = 2 * bits + that bin - 4.

Example: bins `1 1 0 1 0 1` give bits = 5, `mb_type` 8 (B_L0_L1_16x8). The dump reports intra suffixes as 23 + the I value. The partition and prediction-list meaning of each type is Chapter 8.4's business; here you only need the number.

**Why the reader decodes all of P/B `mb_type`, not just the intra part:** the inter bins and the intra suffix share contexts (for instance ctx 17 serves both P_L0_L0 types and the intra suffix). If you skipped the inter bins, a context you later use for an intra macroblock would hold the wrong state.

### 3.5 I_PCM

After the I_PCM terminate bin = 1 (in I, P or B slices) come `pcm_alignment_zero_bit`s up to the next byte boundary (position counted as in 8.1), then 384 bytes of samples (256 luma, 64 + 64 chroma; 8-bit 4:2:0), then the engine is initialised again (range 510, 9 bits) at the next bit. Context states are untouched. An I_PCM macroblock has no `cbp`, no `mb_qp_delta`; for QP prediction the macroblock keeps the previous QP_Y (dump `qp` repeats it; ffmpeg's QP map shows 0 there because that is the value deblocking uses, Chapter 6).

### 3.6 Intra prediction syntax

I_NxN (4x4 only here) sends, for each of the 16 luma 4x4 blocks in block-index order (the zig-zag-of-8x8 order of Chapter 4.2): `prev_intra4x4_pred_mode_flag` (ctx **68**). If it is 0, `rem_intra4x4_pred_mode` follows: three bins, ctx **69** for all, **least significant bin first** (value = b0 + 2 b1 + 4 b2). These contexts have no neighbour dependence. (What the values mean, including the predicted mode, is Chapter 5.4; the dump just shows the syntax.)

`intra_chroma_pred_mode` (I_NxN and I_16x16, after the luma modes): truncated unary, max 3: bin 0 with ctx 64 + inc, bins 1 and 2 with ctx **64 + 3**. `ctxIdxInc = condTermFlagA + condTermFlagB`; `condTermFlagN` = 0 if N is unavailable, **inter** coded, I_PCM, or has `intra_chroma_pred_mode` 0; otherwise 1. Value: bin 0 = 0 gives 0; else bin 1 = 0 gives 1; else bin 2 = 0 gives 2, else 3.

### 3.7 coded_block_pattern (every macroblock except I_16x16 and I_PCM)

CBP = luma bits (bit `b8` set = the 8x8 block `b8` has coefficients, blocks numbered 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right) + 16 * chroma (0 none, 1 DC only, 2 DC and AC). It has two parts.

**Prefix**: four bins, one per 8x8 luma block in order 0..3, ctx `73 + condTermFlagA + 2 * condTermFlagB`. For block `b8` look at the 8x8 block to its left (A) and above (B):

* if it lies **in the current macroblock**, `condTermFlagN` = 0 if the bin just decoded for it was 1, else 1;
* if it lies in a neighbouring macroblock, `condTermFlagN` = **0** if that macroblock is unavailable or I_PCM, or if its CBP bit for that block is 1; **1** if its bit is 0. A skipped macroblock has all bits 0, so it gives 1.

Block 0 uses bit 1 of A and bit 2 of B; block 1 uses block 0 (inside) and bit 3 of B; block 2 uses bit 3 of A and block 0 (inside); block 3 uses blocks 2 and 1 (inside).

**Suffix** (chroma): bin 0 with ctx `77 + condTermFlagA + 2 * condTermFlagB`, where `condTermFlagN` = 1 if N is available and I_PCM, or available with chroma CBP != 0; else 0 (unavailable and skipped give 0). If bin 0 = 0, chroma = 0. Otherwise bin 1 with ctx `77 + 4 + condTermFlagA + 2 * condTermFlagB`, where now `condTermFlagN` = 1 if N is available and I_PCM, or available with chroma CBP **= 2**; chroma = 1 + bin 1.

Worked example: A has luma CBP `0001` (bit 0 set only), B has luma CBP `0100` (bit 2 set). Block 0: A's bit 1 = 0 so condA = 1; B's bit 2 = 1 so condB = 0; ctx = 73 + 1 = **74**. If block 0's bin was 1, block 1: A is block 0 (bin 1, so condA = 0), B's bit 3 = 0 so condB = 1, ctx = 73 + 0 + 2 = **75**. Chroma: A has chroma 2, B unavailable: bin 0 ctx = 77 + 1 + 0 = **78**; if it is 1, bin 1 ctx = 77 + 4 + 1 + 0 = **82**.

### 3.8 mb_qp_delta

Present when the macroblock is I_16x16 or has CBP != 0 (luma or chroma). Binarisation: map the signed value to a code number `k` with Table 9-3 (+1 maps to 1, -1 to 2, +2 to 3, -2 to 4, ..., 0 to 0), then write `k` ones and a zero. Contexts: bin 0 uses ctx `60 + inc` where inc = 1 if the **previous macroblock in decoding order, in the same slice,** exists, is not skipped / I_PCM / (non-I_16x16 with CBP 0), and had `mb_qp_delta` != 0; else 0. Bin 1 uses ctx 62 and all further bins ctx 63.

Examples: +2 gives k = 3, bins `1 1 1 0` on ctx (60 + inc), 62, 63, 63. -1 gives k = 2, bins `1 1 0`. 0 gives the single bin `0`.

QP_Y update: `QP_Y = (QP_Y,pred + mb_qp_delta + 52) % 52` (8-bit video), with `QP_Y,pred` = the QP_Y of the previous macroblock of the slice, and SliceQP_Y at the slice's first macroblock. Skipped, I_PCM and no-`mb_qp_delta` macroblocks keep it. The "previous nonzero delta" memory also starts as 0 at every slice start.

### 3.9 Order inside the macroblock layer

I_NxN: `mb_type`, 16 x (prev flag [, rem]), `intra_chroma_pred_mode`, CBP, `mb_qp_delta` if CBP != 0, residual. I_16x16: `mb_type`, `intra_chroma_pred_mode`, `mb_qp_delta`, residual. Inter (not B_Direct_16x16): `mb_type`, [`sub_mb_type`s], `ref_idx`s, `mvd`s, CBP, `mb_qp_delta` if CBP != 0, residual. B_Direct_16x16: `mb_type`, CBP, `mb_qp_delta` if CBP != 0, residual. (The residual of I_16x16 always begins with its DC block, so I_16x16 always has residual syntax.)

## 4. The project: exactly what to build

Extend your Chapter 8.1 code into a **slice parser** that walks a whole CABAC slice and prints one line per macroblock (section 5.2).

1. Slice start as in 8.1 (alignment, contexts from slice type / `cabac_init_idc` / SliceQP_Y, engine at the first byte). Reset the QP and the "previous `mb_qp_delta`" memory to the slice's values.
2. Macroblock loop of 3.1, with the neighbour records of 3.2.
3. Implement every element of section 3 with the contexts given.
4. **Resynchronise with the HINT file** wherever syntax of later chapters stands in the way:

| When | Which hint | What you do |
|---|---|---|
| An inter macroblock other than B_Direct_16x16 (P `mbt` 0..3, B `mbt` 1..22) | `cbp` of that macroblock | Right after `mb_type`, load the engine state from the hint (this jumps over `sub_mb_type`, `ref_idx` and `mvd`), then decode `coded_block_pattern` and `mb_qp_delta` yourself |
| I_16x16, or any macroblock with CBP != 0 | `end` of that macroblock | After `mb_qp_delta`, load the state from the hint (this jumps over the residual), then read `end_of_slice_flag` |

So: macroblocks that are skipped, I_PCM, I_NxN with CBP 0, or B_Direct_16x16 with CBP 0 need no hint at all; an `end` hint exists exactly when the macroblock has residual syntax (I_16x16, or CBP != 0), a `cbp` hint exactly for inter macroblocks other than B_Direct_16x16. A hint carries the engine state exactly as Chapter 8.1 defines it: `codIRange` (256..510) and `codIOffset` (9 bits, less than `codIRange`), plus `bit`, the position of the next bit the engine will read. `bit` counts bits in the NAL unit **after removing emulation prevention bytes**, starting at 0 for the most significant bit of the NAL header byte. So right after engine initialisation `bit` = 8 * (byte offset of the first slice-data byte in the NAL unit) + 9, and it grows by one for every bit the engine reads (renormalisation, and the restart after I_PCM). Loading a hint means setting `codIRange`, `codIOffset` and your bit reader's position to these three values; the next bit you read is bit number `bit`. (If your engine keeps a different internal representation, as optimised decoders do, convert from these values.) Contexts need no transfer: everything the skipped syntax touches (contexts 21 to 23, 36 to 59 and 85 to 459) is never used by this chapter, and every context this chapter uses is decoded by you.

5. Stop a slice when `end_of_slice_flag` = 1; the macroblock that decoded it is the last line of the slice.

**Verify** (the Summary's check, adapted): your `(element, value)` sequence for every macroblock of every slice of the test streams equals the reference's, including slices where `end_of_slice_flag` = 1 comes at exactly the last macroblock. Where the Summary says "diff against the JM trace", the harness uses a reference parser that is itself validated against ffmpeg and against synthetic streams (section 6).

## 5. Harness input and output formats

Plain text, one record per line, decimal numbers, `#` starts a comment, blank lines are ignored. `python3 test_cabac_mb.py --make-variants DIR` writes the test streams (four synthetic `syn_mb_N.264`, with a `.manifest.json` that you can ignore, and seven x264 `.ts` files (recipes `T10_intra`, `T10_p`, `T10_b`, `T10_slices_aq`, `T10_qp46`, `T10_qp8`, `T10_full` of `common/testfiles.py`): intra only, P only, B pyramid, four slices with adaptive quantisation, QP 46, QP 8, and default Main) together with, for each, `<name>.hints.txt` and `<name>.expected.txt`. For any other CABAC stream: `python3 test_cabac_mb.py --print-expected hints INPUT hints.txt`.

### 5.1 Input: the HINT file

```
HINT <slice> <addr> cbp range=<codIRange> offset=<codIOffset> bit=<bits consumed>
HINT <slice> <addr> end range=<codIRange> offset=<codIOffset> bit=<bits consumed>
```

`slice` is the 0-based ordinal of the slice NAL unit among all slice NAL units of the stream (types 1 and 5); `addr` is the macroblock address. A `cbp` line gives the engine state **just before `coded_block_pattern`**; an `end` line the state **just before `end_of_slice_flag`** of that macroblock (after its residual). Example: `HINT 3 17 end range=402 offset=233 bit=81234`.

### 5.2 Output: the macroblock dump

One block per CABAC slice NAL unit in stream order (slices with CAVLC are not printed):

```
SLICE <slice> type=<I|P|B> first_mb=<first_mb_in_slice>
MB <slice> <addr> mbt=<..> cpred=<..> modes=<..> cbp=<..> qpd=<..> qp=<..> eos=<0|1>
...
END <slice> mbs=<number of MB lines>
```

Field order is fixed; all seven keys must be present on every MB line (unknown extra keys are errors). Macroblocks appear in decoding order, including skipped ones; the last one has `eos=1`.

| Field | Value |
|---|---|
| `mbt` | `skip` for P_Skip / B_Skip, otherwise the `mb_type` of the slice's own table: I slice 0..25 (Table 7-11); P slice 0..3 inter, 5..30 intra (5 + I value); B slice 0..22 inter, 23..48 intra (23 + I value) |
| `cpred` | `intra_chroma_pred_mode` 0..3 for I_NxN and I_16x16, else `-` |
| `modes` | I_NxN: 16 characters, block index order, `p` if `prev_intra4x4_pred_mode_flag` = 1, else the digit `rem_intra4x4_pred_mode` (0..7); otherwise `-` |
| `cbp` | `coded_block_pattern` = luma + 16 * chroma (0..47). For I_16x16 it is derived from `mb_type` (luma 0 or 15, chroma 0..2). `-` for skipped and I_PCM |
| `qpd` | `mb_qp_delta` (signed) when the syntax element is present, even if 0 (I_16x16, CBP != 0), else `-` |
| `qp` | QP_Y after the macroblock (on every line, skipped and I_PCM included) |
| `eos` | the `end_of_slice_flag` decoded right after this macroblock |

Examples:

```
SLICE 0 type=I first_mb=0
MB 0 0 mbt=0 cpred=0 modes=p10p115706042ppp cbp=47 qpd=0 qp=23 eos=0
MB 0 1 mbt=19 cpred=2 modes=- cbp=31 qpd=-3 qp=20 eos=0
MB 0 2 mbt=25 cpred=- modes=- cbp=- qpd=- qp=20 eos=0
END 0 mbs=3
SLICE 2 type=B first_mb=0
MB 2 0 mbt=skip cpred=- modes=- cbp=- qpd=- qp=26 eos=0
MB 2 1 mbt=8 cpred=- modes=- cbp=4 qpd=1 qp=27 eos=0
MB 2 2 mbt=0 cpred=- modes=- cbp=0 qpd=- qp=27 eos=0
```

(The first example is abbreviated: a real slice ends with `eos=1` on its last macroblock.)

## 6. Running the harness

```
python3 test_cabac_mb.py INPUT.ts your_dump.txt
python3 test_cabac_mb.py --print-expected dump INPUT.ts expected.txt     # to diff against yours
python3 test_cabac_mb.py --self-test
```

Exit 0 = pass, 1 = your dump differs (the first wrong macroblock of each slice, the differing fields, and a hint on which rule to look at), 2 = unusable input or output (malformed line, stream the reference cannot handle: CAVLC-only, MBAFF/field, High with 8x8 transform).

What it compares against, since there is no JM here: (1) a reference parser that decodes **everything** in the slices (residual and inter syntax included); a parse only counts when `end_of_slice_flag` = 1 comes at exactly the last macroblock and the `rbsp_stop_one_bit` follows (x264 may place that bit a few zero bits later than the minimal flush; the reference accepts that). (2) ffmpeg's `-debug mb_type` map (I_NxN / I_16x16 / I_PCM / skip / inter and partition shape of every macroblock) and `-debug qp` map (QP_Y of every non-I_PCM macroblock). (3) Synthetic streams written by an independent CABAC **encoder**: random macroblocks of every kind with neighbour-dependent contexts; the expected dump comes from the construction, ffmpeg decodes them without errors and reproduces their I_PCM samples. Where a macroblock owes residual, these streams code every `coded_block_flag` as 0 (legal and cheap), so `cbp != 0` appears next to any neighbour. `--self-test` runs all of this and then 39 deliberately wrong rules in the reader (wrong context offsets, wrong neighbour conditions, bin order, QP handling, PCM alignment, hint use), each of which must be rejected; it lists which streams catch each one (rules about I_PCM, for example, only show up in the synthetic streams, since x264 almost never writes I_PCM).

**Spoiler warning:** `spoiler_reference/` holds the complete reference parser, the encoder and the stream generator. Don't open it before your own parser passes.

## 7. Reading failures

* **First macroblock of the first slice wrong in `mbt`:** engine start or the bin-0 context (8.1); for I slices check that unavailable neighbours give 0.
* **`mbt` right, `modes` or `cpred` wrong:** contexts 68 / 69 (flag and rem), rem is least significant bin first; `cpred` bins 1 and 2 use ctx 67.
* **`cbp` wrong only at macroblocks that have a left or upper neighbour:** the neighbour conditions in 3.7. Mix-ups to look for: unavailable (0) versus skipped (1), I_PCM (0 for the luma prefix, 1 for the chroma suffix), and the inner-macroblock neighbours using the bins you just decoded.
* **`qpd` or `qp` wrong, `cbp` right:** the "previous macroblock" rule for bin 0 (skipped / I_PCM / no delta all reset it), the sign mapping, the modulo wrap, the reset at a slice start.
* **Everything right until a macroblock after a P or B inter macroblock, or after an I_16x16:** the hint was loaded with a wrong field (bit count, or range/offset swapped), or you did not load it at all. **A slice that ends early or never ends:** `end_of_slice_flag` after skipped macroblocks, or the restart after I_PCM (alignment, 384 bytes).
* **Wrong only in P or B slices, I slices fine:** the `mb_type` prefix contexts (14 to 17, 27 to 32), the intra suffix offsets (17 and 32) and the neighbour conditions for `mb_skip_flag` / B `mb_type`.

## 8. What's next

Chapter 8.3 decodes the **residual**: `coded_block_flag` (its context reads the neighbouring blocks' flags, so you will keep a flag per block), `significant_coeff_flag`, `last_significant_coeff_flag`, `coeff_abs_level_minus1` and the bypass-coded signs, for every block category. That replaces every `end` hint of this chapter; the `cbp` hints go in 8.4 with `sub_mb_type`, `ref_idx` and `mvd`.
