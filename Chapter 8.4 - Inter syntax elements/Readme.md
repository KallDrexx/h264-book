# Chapter 8.4 - Inter syntax elements (CABAC)

## 1. Goal and the project

Chapter 8.2 decoded the macroblock-level elements, including the P and B `mb_type`. Chapter 8.3 decoded the residual. One hole is left: in an inter macroblock, between `mb_type` and `coded_block_pattern`, stand three more syntax elements that both chapters jumped over with a `cbp` hint:

* `sub_mb_type` (only for P_8x8 and B_8x8),
* `ref_idx_l0` / `ref_idx_l1` (which reference picture),
* `mvd_l0` / `mvd_l1` (the motion vector *difference*, x and y).

This chapter fills the hole. **There are no hints any more.** Your program gets a CABAC stream and nothing else, and every slice must end with `end_of_slice_flag` = 1 at exactly the last macroblock. That is the first time your parser reads complete P and B streams on its own.

The dump of 8.3 gets five new keys: `sub`, `refidx`, `refidx1`, `mvd`, `mvd1`.

Out of scope, as before: field pictures and MBAFF, `transform_size_8x8_flag` (8.5), and what the numbers *mean* (the motion vector is predictor + `mvd`, Chapter 7.3 in the CAVLC world; this chapter only reads `mvd`).

## 2. Big picture

An inter macroblock says: "predict this block from picture X, moved by vector V". Three pieces of information:

1. **How is the macroblock split?** `mb_type` (8.2) names a 16x16, 16x8, 8x16 or 8x8 split and, in B slices, which list(s) each part uses. For the 8x8 split, each 8x8 quadrant says its own sub-split with `sub_mb_type`: 8x8, 8x4, 4x8 or 4x4 (and in B slices again which list(s), or *direct*).
2. **Which picture?** `ref_idx`, one per partition (per 8x8 quadrant for the 8x8 split) and list. Not sent when the slice has only one active reference in that list.
3. **Which vector?** `mvd`, one pair of numbers per prediction block and list. The decoder predicts the vector from its neighbours and adds `mvd`; the encoder sends only the (usually small) difference.

CABAC codes all three with the machinery you already have: binarisation, one context per bin, `ctxIdx = offset + inc`. What is new:

* **Neighbour-dependent contexts at partition level.** In 8.2 and 8.3 neighbours were whole macroblocks or 4x4 residual blocks. Here the neighbours are **partitions**, and you must remember per macroblock what each 4x4 cell of it was coded with: its `ref_idx` (per list) and its absolute `mvd` (per list, per component).
* **A new binarisation, UEG3** for `mvd`: a short unary prefix, and for big values an Exp-Golomb escape in bypass mode, plus a sign. Same idea as the coefficient levels of 8.3 (UEG0), different parameters.
* **Syntax order matters more.** The elements of one macroblock are not interleaved partition by partition but grouped by element type (section 3.5). Contexts adapt, so reading in the wrong order breaks everything after it.

## 3. Concepts in detail

### 3.1 Tables you need (prediction shapes)

The partitions are given in the spec's Tables 7-13, 7-14 (macroblock types, 8.2 already decoded the number), 7-17 (P sub types) and 7-18 (B sub types). A **prediction block** is the unit that has one `mvd` per list: a partition of a 16x16, 16x8 or 8x16 macroblock, or a sub-partition of an 8x8 quadrant. Positions below are in 4x4 units (a macroblock is 4x4 cells).

P `sub_mb_type` (all use list 0):

| Value | Name | Prediction blocks in the quadrant |
|---|---|---|
| 0 | P_L0_8x8 | one 8x8 |
| 1 | P_L0_8x4 | two 8x4 (top, bottom) |
| 2 | P_L0_4x8 | two 4x8 (left, right) |
| 3 | P_L0_4x4 | four 4x4 (raster) |

B `sub_mb_type`:

| Value | Name | Value | Name |
|---|---|---|---|
| 0 | B_Direct_8x8 | 7 | B_L1_4x8 |
| 1 | B_L0_8x8 | 8 | B_Bi_8x4 |
| 2 | B_L1_8x8 | 9 | B_Bi_4x8 |
| 3 | B_Bi_8x8 | 10 | B_L0_4x4 |
| 4 | B_L0_8x4 | 11 | B_L1_4x4 |
| 5 | B_L0_4x8 | 12 | B_Bi_4x4 |
| 6 | B_L1_8x4 | | |

`Bi` means both lists: the block has a `ref_idx` and an `mvd` for list 0 **and** for list 1. A **direct** quadrant (value 0) has no `ref_idx` and no `mvd` at all; its vectors are derived (Chapter 7.9), and for every context rule below it counts as "no information" (zero).

The B macroblock types 1..21 are, in order: the three 16x16 types (L0, L1, Bi), then pairs of 16x8 / 8x16 types (even `mb_type` = 16x8, odd = 8x16) with the list pairs (L0,L0), (L1,L1), (L0,L1), (L1,L0), (L0,Bi), (L1,Bi), (Bi,L0), (Bi,L1), (Bi,Bi); type 22 is B_8x8, type 0 B_Direct_16x16 (nothing of this chapter). P types: 0 = 16x16, 1 = 16x8, 2 = 8x16, 3 = P_8x8. (P_8x8ref0, value 4, does not exist in CABAC.)

### 3.2 sub_mb_type (9.3.2.5, 9.3.3.1.2)

Read once per quadrant, four times in a row, for P_8x8 and B_8x8 only.

**P slices.** Bins and contexts (offset 21):

| Value | Bins | ctxIdx of bins 0, 1, 2 |
|---|---|---|
| 0 | `1` | 21 |
| 1 | `0 0` | 21, 22 |
| 2 | `0 1 1` | 21, 22, 23 |
| 3 | `0 1 0` | 21, 22, 23 |

**B slices.** Offset 36. Bin 0 uses 36, bin 1 uses 37, bin 2 uses 38 if bin 1 was 1 and **39 if bin 1 was 0**, bins 3 to 5 use 39.

| Value | Bins | Value | Bins |
|---|---|---|---|
| 0 | `0` | 7 | `1 1 1 0 0 0` |
| 1 | `1 0 0` | 8 | `1 1 1 0 0 1` |
| 2 | `1 0 1` | 9 | `1 1 1 0 1 0` |
| 3 | `1 1 0 0 0` | 10 | `1 1 1 0 1 1` |
| 4 | `1 1 0 0 1` | 11 | `1 1 1 1 0` |
| 5 | `1 1 0 1 0` | 12 | `1 1 1 1 1` |
| 6 | `1 1 0 1 1` | | |

No neighbour dependence: these contexts simply adapt.

**Worked example 1.** B sub type 7 (B_L1_4x8): bins `1 1 1 0 0 0` with ctxIdx 36, 37, 38, 39, 39, 39. B sub type 2: `1 0 1` with 36, 37, 39 (bin 2 follows a 0 in bin 1, so 39, not 38).

### 3.3 ref_idx (9.3.2.3, 9.3.3.1.1.6)

**When it is present.** `ref_idx_lX` for a partition (for the 8x8 split: for the quadrant) exists only if

* the partition uses list X (a P partition uses list 0; B: by its `mb_type` / `sub_mb_type`; direct quadrants and B_Direct_16x16 and skipped macroblocks have none), **and**
* the slice's `num_ref_idx_lX_active` is greater than 1 (`num_ref_idx_active_minus1` > 0 in the syntax). Otherwise the value is 0 and **nothing is read**. (The number comes from the slice header or, without override, from the PPS default; Chapter 3.)

**Binarisation.** Unary: the value `v` is `v` ones followed by a zero. No upper bound is signalled; the bin string just ends at the zero.

**Contexts** (offset 54): bin 0: `54 + ctxIdxInc`; bin 1: `54 + 4`; bins 2 and up: `54 + 5`.

**ctxIdxInc** = `condTermFlagA + 2 * condTermFlagB`. A is the partition covering the 4x4 cell **left of the current partition's top-left cell**, B the cell **above** it (for the 8x8 split: relative to the quadrant's top-left cell). `condTermFlagN` is 1 only when that neighbour partition has `refIdx` **greater than 0** *in the same list* as the element being decoded. It is 0 when any of these holds:

* N is outside the picture or in another slice (unavailable);
* N is in an intra macroblock;
* N is P_Skip, B_Skip, B_Direct_16x16 or a direct quadrant (even though the decoder derives a reference for it, the context ignores it);
* N does not use this list (`predFlagLX` = 0), for example a B_L1_16x16 neighbour when decoding `ref_idx_l0`;
* N has `refIdx` 0 (including "inferred 0" because the list had a single reference).

(Field and MBAFF adjustments of the rule do not apply to frame pictures.) Note the asymmetry with 8.3: here "unavailable" is 0 for *every* macroblock type.

A neighbour inside the **current macroblock** is a partition decoded earlier; because the elements are grouped by type (3.5), when you read `ref_idx_l1` all `ref_idx_l0` of the macroblock are known, but only the `ref_idx_l1` of earlier partitions.

**Worked example 2.** B_L0_Bi_16x8 slice with two references per list. Reading `ref_idx_l0` of the first partition (top): A is the macroblock to the left, its cell at row 0, a partition with `ref_idx_l0` = 2, so condA = 1; B is the macroblock above, a B_Skip macroblock: condB = 0. inc = 1, bin 0 ctxIdx = 55. Value 2: bins `1` (55), `1` (58), `0` (59). The bottom partition has the top one as neighbour B (inside the macroblock); if its `ref_idx_l0` was 2, condB = 1, and A is the left macroblock's row 2 cell.

### 3.4 mvd (9.3.2.3 UEG3, 9.3.3.1.1.7)

`mvd_lX` has two components, x then y, each its own syntax element, one pair per prediction block using list X. The value is signed, in quarter-sample units.

**Binarisation: UEG3 with uCoff = 9, signedValFlag = 1.** Let `a` = absolute value.

1. **Prefix**: truncated unary of `Min(a, 9)`: `a` ones then a zero, except when `a` >= 9 where there are exactly nine ones and no zero. Context-coded.
2. **Suffix**, only if `a` >= 9: the value `a - 9` as Exp-Golomb order **3**, all bypass: while the remaining value is at least `2^k` (k starts at 3): emit a 1, subtract `2^k`, increase k; emit a 0; then k bits of the remainder, most significant first.
3. **Sign**, only if `a` > 0: one bypass bin, 1 = negative.

**Contexts.** x uses offset **40**, y uses offset **47** (separate sets: 40..46 and 47..53). Bin 0: `offset + inc0`, with `inc0` from the neighbours (below); bins 1, 2, 3, 4 use `offset + 3, 4, 5, 6`, and bins 5 to 8 also `offset + 6`. The suffix and sign have no context.

**inc0**: let `absMvdComp` of a neighbour be the absolute value of **its mvd component of the same kind** (x with x, y with y) **in the same list**. Sum A and B:

| `sum = absMvdCompA + absMvdCompB` | inc0 | ctxIdx of bin 0 (x / y) |
|---|---|---|
| less than 3 | 0 | 40 / 47 |
| 3 to 32 | 1 | 41 / 48 |
| greater than 32 | 2 | 42 / 49 |

(A sum of exactly 3 or exactly 32 still falls in the middle row.) A and B are found as for `ref_idx`: the cells left of and above the prediction block's **top-left 4x4 cell** (for a sub-partition: that sub-partition's own top-left cell, not the quadrant's). A neighbour contributes 0 if unavailable, intra, skipped, B_Direct_16x16, a direct quadrant, or when it does not use the list. These are exactly the cells for which you never stored an `mvd`, so initialise every macroblock's `mvd` store to 0 and these rules fall out.

**What to store (this is the "mvd per partition" requirement).** After reading a prediction block's `mvd` (both components), write its absolute components into **every 4x4 cell the block covers**, for that list. A 16x8 partition fills 16 / 2 = 8 cells. Neighbours always read a single cell. Store the *difference* the stream sent, not the final vector: the context depends on `mvd`, never on the predicted vector, which is why parsing needs no motion vector prediction at all.

**Worked example 3.** P_L0_16x16, x component. A has `mvd` x = -2, B has x = 1: sum = 3, inc0 = 1, ctxIdx 41. Value -5: prefix `1 1 1 1 1 0` on ctxIdx 41, 43, 44, 45, 46, 46; sign bypass `1`. Stored absolute x for all 16 cells: 5.

**Worked example 4 (escape).** y component, sum of neighbours 40 (inc0 = 2, base 47 + 2 = 49), value +14. Prefix: nine ones on ctxIdx 49, 50, 51, 52, 53, 53, 53, 53, 53 (no terminating zero). Suffix: 14 - 9 = 5 in Exp-Golomb order 3: 5 < 8, so a 0, then three bits of 5: `1 0 1`. Bypass bins `0 1 0 1`, then the sign bypass `0`.

**Worked example 5.** Value -30: prefix nine ones; suffix 21: 21 >= 8 so a 1 (remainder 13, k = 4); 13 < 16 so a 0; four bits of 13: `1 1 0 1`. Bypass `1 0 1 1 0 1`, sign `1`. Decoding check: 8 + 13 = 21, plus 9 = 30.

### 3.5 Order of the syntax in a macroblock

Every inter macroblock other than B_Direct_16x16 (7.3.5.1, 7.3.5.2):

**16x16, 16x8, 8x16 (`mb_pred`)**

1. `ref_idx_l0` for each partition that uses list 0 (partition order: top to bottom or left to right),
2. `ref_idx_l1` for each partition that uses list 1,
3. `mvd_l0` for each partition that uses list 0 (x then y),
4. `mvd_l1` likewise.

**8x8 (`sub_mb_pred`)**

1. `sub_mb_type` for quadrants 0, 1, 2, 3,
2. `ref_idx_l0` for each quadrant that is not direct and uses list 0,
3. `ref_idx_l1` likewise,
4. `mvd_l0` for each quadrant in order, and inside it each sub-partition in order (x then y),
5. `mvd_l1` likewise.

After that: `coded_block_pattern`, `mb_qp_delta`, residual (8.2, 8.3). P_Skip and B_Skip macroblocks have none of this; neither does B_Direct_16x16.

So for B_Bi_Bi_16x8 the order is `ref_idx_l0` (top), `ref_idx_l0` (bottom), `ref_idx_l1` (top), `ref_idx_l1` (bottom), `mvd_l0` (top), `mvd_l0` (bottom), `mvd_l1` (top), `mvd_l1` (bottom), where each `mvd` is its x element followed by its y element.

### 3.6 Bookkeeping per macroblock

For every macroblock of the current slice keep, besides 8.2 / 8.3's records: per list, `refIdx` of each of the four 8x8 quadrants (0 when the list is not used, or not coded, or the macroblock is skipped / intra / direct), and per list the absolute `mvd` x and y of each of the 16 4x4 cells (0 likewise). Flags "B_Skip or B_Direct_16x16" (you have it from 8.2) and "this quadrant is direct" make the `ref_idx` rule easy if you store 0 for direct partitions, as above. A neighbour in another macroblock is always looked up by the cell on the shared edge: A is cell (3, row) of the left macroblock, B is cell (column, 3) of the macroblock above.

## 4. The project: exactly what to build

Extend your Chapter 8.3 parser:

1. After `mb_type` of an inter macroblock other than B_Direct_16x16, decode `sub_mb_type`, `ref_idx`, `mvd` in the order of 3.5 with the contexts of 3.2 to 3.4. Remove the `cbp` hints: nothing is loaded from outside any more.
2. Keep the per-macroblock `ref_idx` and `mvd` stores of 3.6, reset per slice (neighbours in other slices are unavailable).
3. Print the five new keys (section 5.1).
4. **Verify** (the Summary's check, in this chapter's terms): on `T10_p` and `T10_b` (and the other generated streams), your dump equals the expected one; every slice ends with `end_of_slice_flag` = 1 on exactly its last macroblock. With that, **a default x264 Main stream (`-profile:v main`: CABAC, B frames, several references) parses completely.**

**What this gives you for decoding.** Parsing is now complete for Main-profile CABAC streams: you have every `mb_type`, `sub_mb_type`, `ref_idx`, `mvd` and every coefficient. Everything after parsing (motion vector prediction from the `mvd`, Chapter 7.3 and the direct modes, reference lists, interpolation, weighting, deblocking, Phases 5 to 7) is entropy-coder independent: feed it the numbers you parsed here. A CABAC P or B stream should then decode to ffmpeg's pixels.

5. **Verify end to end** (the Summary's check): decode `T10_full` (x264's default `-profile:v main`: CABAC, B frames, several references) with your whole decoder, write the cropped yuv420p file of Chapter 5.1 (all frames, output order) and run `python3 test_cabac_inter.py --yuv T10_full.ts your.yuv`. It must be byte-identical to ffmpeg's decode (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`); a mismatch is reported as the first differing frame, plane, macroblock and pixel (Chapter 5.1's `common/yuvcmp.py`). This works on any Main-profile CABAC stream, so try `T10_b`, `T10_ref` and `T10_manyb` too. If your dump passes but `--yuv` fails, the bug is after parsing (Phases 5 to 7), not in this chapter's syntax.

## 5. Harness input and output formats

Plain text, one record per line, decimal numbers, `#` starts a comment, blank lines are ignored. There is **no input file besides the stream**. `python3 test_cabac_inter.py --make-variants DIR` writes test streams, each with its expected dump `<name>.expected.txt`: eight synthetic `syn_inter_N.264` (with a `.manifest.json` you can ignore) and eight x264 `.ts` files from the recipes `T10_p`, `T10_b`, `T10_ref` (five references, all partition sizes), `T10_manyb` (eight B frames, temporal direct), `T10_slices_aq`, `T10_qp46`, `T10_qp8`, `T10_full`. For another CABAC stream: `python3 test_cabac_inter.py --print-expected INPUT expected.txt`.

### 5.1 Output: the macroblock dump

```
SLICE <slice> type=<I|P|B> first_mb=<first_mb_in_slice>
MB <slice> <addr> mbt=<..> cpred=<..> modes=<..> sub=<..> refidx=<..> refidx1=<..> mvd=<..> mvd1=<..> cbp=<..> qpd=<..> qp=<..> coef=<..> eos=<0|1>
...
END <slice> mbs=<number of MB lines>
```

All thirteen keys on every MB line; this order is the one the expected dump uses (`common/README.md`, "Macroblock dump grammar": keys in any order, extra keys ignored, Chapter 4.2's long spellings such as `mb_type` / `qp_delta` accepted). `mbt`, `cpred`, `modes`, `cbp`, `qpd`, `qp`, `coef`, `eos` are exactly as in Chapters 8.2 and 8.3. New keys:

| Key | Value |
|---|---|
| `sub` | For P_8x8 and B_8x8: the four `sub_mb_type` values, comma separated (`2,0,3,1`); otherwise `-` |
| `refidx` | `ref_idx_l0` of every partition (for the 8x8 split: every non-direct quadrant) that uses list 0, in syntax order, comma separated. An entry is 0 when the element is not coded because `num_ref_idx_l0_active` is 1. `-` if no partition uses list 0 (or the macroblock is intra, skipped, B_Direct_16x16) |
| `refidx1` | The same for list 1 (always `-` in P slices) |
| `mvd` | `mvd_l0` of every prediction block that uses list 0, in syntax order, comma separated, each as `x:y` with **signed** integers in quarter samples; `-` as above |
| `mvd1` | The same for list 1 |

So the number of `refidx` entries is the number of partitions (quadrants) using list 0, the number of `mvd` entries the number of prediction blocks using list 0 (a P_L0_4x4 quadrant contributes four). A partition that does not use a list has no entry in that list's keys (there is no placeholder), and B direct quadrants and macroblocks contribute nothing.

Example lines (real, from `T10_b`; `coef` shortened to `...`):

```
MB 1 4 mbt=3 cpred=- modes=- sub=0,0,0,0 refidx=0,0,0,0 refidx1=- mvd=0:0,-4:0,0:0,-11:-8 mvd1=- cbp=40 qpd=0 qp=26 coef=... eos=0
MB 2 46 mbt=3 cpred=- modes=- sub=- refidx=0 refidx1=0 mvd=40:16 mvd1=-112:0 cbp=1 qpd=0 qp=27 coef=... eos=0
MB 13 257 mbt=22 cpred=- modes=- sub=0,3,0,1 refidx=0,1 refidx1=0 mvd=0:0,-9:19 mvd1=0:0 cbp=42 qpd=0 qp=28 coef=... eos=0
```

The first is a P_8x8 macroblock (slice 1, a P slice) whose four quadrants are all `P_L0_8x8`: four `refidx` entries (all 0, because that slice has one active reference, so nothing was read) and four `mvd` entries. The second is a B_Bi_16x16 (`mbt=3` in a B slice): one entry in each list. The third is a B_8x8: reading `sub=0,3,0,1` with the table of 3.1, quadrants 0 and 2 are direct (no entries), quadrant 1 is `B_Bi_8x8` (one entry in each list) and quadrant 3 is `B_L0_8x8` (one `refidx`, one `mvd`). So `refidx` = `0,1` (quadrant 1, then 3), `refidx1` has one entry, `mvd` two (quadrant 1, then 3), `mvd1` one.

## 6. Running the harness

```
python3 test_cabac_inter.py INPUT.ts your_dump.txt
python3 test_cabac_inter.py --yuv INPUT.ts your.yuv                    # end to end (step 5 of section 4)
python3 test_cabac_inter.py --print-expected INPUT.ts expected.txt     # to diff against yours
python3 test_cabac_inter.py --make-variants DIR
python3 test_cabac_inter.py --self-test
```

Exit 0 = pass, 1 = your dump differs (the first wrong macroblock of each slice, the differing fields, the first differing entry of a `refidx` / `mvd` list, and a hint about the rule), 2 = unusable input or output. With `--yuv`, 1 means your frames differ from ffmpeg's (first differing frame / plane / macroblock / pixel, frame count, a hint when Cb and Cr are swapped).

What it compares against: (1) a reference parser that decodes **everything** in the slices, accepted only if `end_of_slice_flag` = 1 comes exactly at the last macroblock of every slice and the stop bit follows; with real x264 streams that is a strong proof for the whole parse, since one wrong context anywhere derails everything after it; (2) ffmpeg's `-debug mb_type` map (class and partition shape of every macroblock) and `-debug qp` map; (3) synthetic streams from an independent CABAC **encoder**: every P / B macroblock type, every sub type (direct quadrants included), `ref_idx` up to 2 with one to three active references per list, `mvd` values around every context threshold (sums 2, 3, 32, 33) and the escape (8, 9, 10) and as large as 2500, all neighbour combinations (skipped, direct, intra, I_PCM, other slice, either list). Their expected `sub` / `refidx` / `mvd` come from the construction, and ffmpeg decodes them without errors and reproduces their I_PCM samples, which only works if ffmpeg stayed in sync through all the `ref_idx` / `mvd` bins.

`--self-test` also checks the `--yuv` channel on `T10_full`: ffmpeg's own frames pass, and a single sample off by one, two frames in decode instead of output order, a missing last frame and swapped chroma planes are rejected. It runs 37 deliberately wrong rules in the reader (wrong `sub_mb_type` contexts and mappings; `ref_idx` neighbour conditions, contexts and gating; `mvd` thresholds, contexts, suffix, sign, neighbour storage; wrong syntax order) and requires each to be rejected on at least one stream; it says on how many.

**Not verified by the dump:** the motion vectors themselves (predictor + `mvd`). The `--yuv` check covers them end to end (a wrong vector changes pixels); for a precise location, the Phase 7 chapters' harnesses check vectors against ffmpeg's exported ones, and once you combine this parser with your Chapter 7.3 / 7.9 code those checks apply to CABAC streams unchanged.

**Spoiler warning:** `spoiler_reference/` holds the reference parser, the encoder and the stream generator. Don't open it before your own parser passes.

## 7. Reading failures

* **Everything right up to the first inter macroblock, then garbage:** the order of 3.5, or a missing element (`ref_idx` forgotten with `num_ref_idx_active` > 1, or read when it is 1).
* **First wrong field is `sub`:** the bin table of 3.2, mostly bin 2 of B (38 versus 39) or P types 2 and 3 swapped.
* **`refidx` wrong, only where a neighbour exists:** the `condTermFlag` rule of 3.3: skipped / direct / intra / unavailable count 0; list 1 looks at list 1 of the neighbours; the neighbour inside the macroblock of an 8x8 split is the previous quadrant.
* **`mvd` correct for small values, wrong for big ones:** prefix cut at 9 ones (not 8, not unbounded), then Exp-Golomb of order **3**, most significant bits first.
* **`mvd` x right, y wrong:** y has its own contexts (47..53) and uses the neighbours' **y** values.
* **`mvd` wrong only after partitioned macroblocks (16x8 / 8x16 / 8x8):** the store: write the absolute `mvd` to every cell of the partition; the neighbour is the cell next to the **top-left** cell of the block.
* **Sign flipped:** 1 = negative; stored values for contexts are absolute, the dump value is signed.
* **Wrong only in B slices:** `Bi` blocks need both lists; `mvd_l1` neighbours are `mvd_l1` (the sum for list 1 must not see list 0 values).
* **Slice ends early or late, fields all right:** look at `end_of_slice_flag` after skipped macroblocks (8.2) and the residual (8.3).

## 8. What's next

Chapter 8.5 adds the High profile's **8x8 transform**: `transform_size_8x8_flag` (a CABAC syntax element with neighbour contexts), the 8x8 inverse transform, Intra 8x8 prediction with its reference-sample filter, and `ctxBlockCat` 5 residual blocks with their own significance-map context tables. That is what x264's default High profile files need, on top of everything you now parse.
