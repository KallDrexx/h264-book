# Chapter 7.3 - Motion vector prediction

> **SPOILER WARNING - do not open `spoiler_reference/` until your own predictor passes.**
> `spoiler_reference/mvpred_ref.py` is a complete solution of this chapter and `spoiler_reference/mvgen.py` (the generator of the synthetic
> streams) contains the same predictor. `test_mvpred.py` and `common/ffmv_helper.py` are plumbing (reference checks, file formats, comparison) and are safe to read.

## 1. Goal and the project

Chapter 7.1 taught your parser to read the `mvd_l0` of every inter partition and Chapter 7.2 told you which picture each `ref_idx_l0` names. This chapter
computes the **motion vector** itself. A P partition does not carry its vector. It carries only a correction, and the decoder *predicts* the vector
from vectors it already knows:

```
mv  =  mvp (the prediction from neighbouring partitions)  +  mvd (the difference read from the stream)
```

You build the predictor (8.4.1) and a per-picture store of vectors that it reads. Output: for every macroblock of every I and P slice, the predicted
vectors, the final vectors and the reference indices, as a text dump. Still no pixels (Chapter 7.4 does the copying and interpolation), but the
dump is checked against ffmpeg's own vectors, value for value.

## 2. Big picture

Neighbouring blocks of a picture usually move together (the same object, the same camera pan). So the best guess for a block's vector is a mix of the
vectors of its already-decoded neighbours, and the encoder only has to send the small error `mvd`. Cheap vectors are the reason P frames are small.

Three ingredients:

1. **Which neighbours.** For each partition: A = the block to its left, B = above, C = above-right, and D = above-left, which stands in when C does not exist.
   "Exist" is the hard part: the neighbour must be inside the picture, in the same slice, and already decoded. Inside a macroblock that last condition
   depends on the order in which its partitions are decoded.
2. **How to mix.** Normally the component-wise **median** of A, B, C, but with an overriding rule (one neighbour uses the same reference picture),
   and two shape-specific shortcuts (16x8 and 8x16 partitions).
3. **The skipped macroblock** (P_Skip): no `mvd`, no `ref_idx`. Its vector is inferred, with a rule that gives zero in the common "static background" case.

The predictor never looks at pixels, only at stored vectors and reference indices. If you store them right, the rest is a handful of short rules.

## 3. Concepts in detail

### 3.1 Units, ranges, what is stored

* A vector is a pair (x, y) of integers in **quarter luma samples**: (4, -8) means 1 sample right and 2 samples up (y grows downwards). `mvd` uses the same unit.
  `mv = mvp + mvd` is plain integer addition; there is no wrap-around or clipping. (Chroma uses the same numbers as eighth-sample offsets of the chroma
  plane; that is Chapter 7.4.)
* Allowed range of the final vector (Annex A, Table A-1): horizontal -2048 to +2047.75 luma samples (-8192..+8191 in quarter samples); vertical limits depend on the
  level: -64..+63.75 samples for levels 1 / 1b, -128..+127.75 for 1.1-2, -256..+255.75 for 2.1-3, -512..+511.75 for 3.1 and above (levels 5.x
  etc. keep -512..+511.75). A conforming stream never exceeds them, so you need no clamping; store vectors in 16-bit signed integers at least. Vectors may point
  **outside the picture** (the reference is padded by repeating the edge samples, Chapter 7.4); that is legal and common near edges.
* Per partition you know: `refIdxL0` (0..num_ref_idx_active-1, or **-1 for "no inter prediction here", i.e. an intra block**) and the vector.

**Storage.** Keep two picture-level arrays at 4x4-block granularity, `(4 * PicWidthInMbs) x (4 * PicHeightInMbs)` entries each:

| array | content |
|---|---|
| `slice_id` | which slice decoded this 4x4 block; -1 (initialised at the start of every picture) = "not decoded yet" |
| `ref` | `refIdxL0` of the partition containing the block; -1 for intra |
| `mv` | the vector (x, y) of that partition; (0, 0) for intra |

When a partition is decoded, write its `ref` and final `mv` into **every** 4x4 block it covers (a 16x16 partition fills 16 entries, an 8x4 two, a
4x4 one) and set `slice_id` to the current slice. P_Skip fills all 16 entries with refIdx 0 and its inferred vector; an intra macroblock fills all 16
with ref -1, mv (0, 0). The **neighbour lookup** is then a pure function of a luma sample position (X, Y) in the picture:

```
available(X, Y)  =  0 <= X < width  and  0 <= Y < height  and  slice_id[block of (X, Y)] == current slice
```

That single test covers everything: the picture edge, a different slice, the macroblock to the right (not decoded yet, so still -1), the
macroblock below, and a partition of the *current* macroblock that comes later in decoding order (its entries are still -1 because you write a
partition only after you have computed its vector). The spec words it as 6.4.11.7 plus 6.4.12 (neighbouring locations in macroblock terms: mbAddrA,
B, C, D and "marked as not available"); the array formulation is equivalent for frame pictures, no FMO, no ASO.

### 3.2 The neighbours A, B, C, D of a partition (6.4.11.7)

Take the partition's **top-left luma sample** (x, y) inside the picture and its width `w` (the *prediction* width, see below). The four neighbour positions are:

| neighbour | luma position |
|---|---|
| A (left) | (x - 1, y) |
| B (above) | (x, y - 1) |
| C (above right) | (x + w, y - 1) |
| D (above left) | (x - 1, y - 1) |

`w` is: 16 for P_L0_16x16 and P_Skip; 16 for each 16x8 partition; 8 for each 8x16 partition; and for the partitions of a P_8x8 macroblock the width of the
**sub-macroblock partition** (8 for 8x8 and 8x4, 4 for 4x8 and 4x4) - *not* 8 for the whole sub-macroblock. This `w` only moves C.

Each position falls into some 4x4 block. If `available` is false the neighbour is **unavailable**; otherwise its `ref` and `mv` are read from your arrays. The
result for each neighbour N is the triple (available, refIdxN, mvN) with these three cases:

| case | refIdxN | mvN |
|---|---|---|
| unavailable (outside, other slice, not decoded yet) | -1 | (0, 0) |
| available but intra | -1 | (0, 0) |
| available inter | its refIdxL0 | its vector |

The unavailable and the intra case look identical to the *median* (both have refIdx -1, mv 0), but they differ in two places below (the C->D substitution and the "B and C
unavailable" rule). So keep the `available` flag next to the values. Intra neighbours are available.

**C replaced by D.** If C is **unavailable**, use D instead (and then C *is* D: its availability, refIdx and vector). If C is available but intra, it is *not* replaced.

**Which C positions are unavailable inside the macroblock?** The picture-level test gives these automatically; this table is for checking your understanding. Coordinates
are relative to the macroblock (x right, y down, 0..15). "edge" = C lies in the row above the macroblock (macroblock B or C above: availability by the picture/slice test).
"yes" = a partition of the same macroblock already decoded. "NO" = unavailable, D is used.

| partition | C position | C |
|---|---|---|
| 16x16 | (16, -1) | edge (macroblock above-right) |
| 16x8 upper | (16, -1) | edge |
| 16x8 lower | (16, 7) | **NO** (right of the macroblock: to the right is not decoded yet; D = (-1, 7)) |
| 8x16 left | (8, -1) | edge (macroblock above) |
| 8x16 right | (16, -1) | edge (macroblock above-right) |
| sub-macroblock 0 (top left 8x8 at (0,0)): 8x8 / 8x4 upper | (8, -1) | edge |
| sub-MB 0, 8x4 lower at (0,4) | (8, 3) | **NO** (inside sub-macroblock 1, later) |
| sub-MB 0, 4x8 left / right | (4, -1) / (8, -1) | edge / edge |
| sub-MB 0, 4x4 at (0,0), (4,0), (0,4), (4,4) | (4,-1), (8,-1), (4,3), (8,3) | edge, edge, **yes** (the 4x4 at (4,0)), **NO** |
| sub-MB 1 (at (8,0)): 8x8 / 8x4 upper | (16, -1) | edge (macroblock above-right) |
| sub-MB 1, 8x4 lower at (8,4) | (16, 3) | **NO** |
| sub-MB 1, 4x8 left / right | (12, -1) / (16, -1) | edge / edge |
| sub-MB 1, 4x4 at (8,0), (12,0), (8,4), (12,4) | (12,-1), (16,-1), (12,3), (16,3) | edge, edge, **yes**, **NO** |
| sub-MB 2 (at (0,8)): 8x8 / 8x4 upper | (8, 7) | **yes** (sub-macroblock 1) |
| sub-MB 2, 8x4 lower at (0,12) | (8, 11) | **NO** (sub-macroblock 3, later) |
| sub-MB 2, 4x8 left / right | (4, 7) / (8, 7) | yes / yes |
| sub-MB 2, 4x4 at (0,8), (4,8), (0,12), (4,12) | (4,7), (8,7), (4,11), (8,11) | yes, yes, yes, **NO** |
| sub-MB 3 (at (8,8)): 8x8 / 8x4 upper / 8x4 lower | (16, 7) / (16, 7) / (16, 11) | **NO** / **NO** / **NO** |
| sub-MB 3, 4x8 left / right | (12, 7) / (16, 7) | yes / **NO** |
| sub-MB 3, 4x4 at (8,8), (12,8), (8,12), (12,12) | (12,7), (16,7), (12,11), (16,11) | yes, **NO**, yes, **NO** |

Every sub-macroblock partition's A, B and D are always already decoded when they lie inside the macroblock (they are left of or above the partition; with
the decoding order sub-macroblock 0, 1, 2, 3 and sub-partitions in raster order nothing left of or above is later). Only C is ever "later".

### 3.3 Median prediction (8.4.1.3.1)

Inputs: the three neighbours A, B, C (C after the D substitution), each as (available, refIdxN, mvN), and the `refIdx` of the **current** partition.

1. **Missing B and C.** If B **and** C are both unavailable *and* A is available, then B := A and C := A (refIdx and vector). (Typical: the first
   macroblock row of a picture, or a slice.) Note this is about *unavailable*, not intra.
2. **One matching neighbour.** Count the neighbours among A, B, C whose refIdxN **equals the current refIdx**. If exactly one does, mvp = that neighbour's vector.
3. **Otherwise** (zero, two or three matches): mvp = the **component-wise median** of the three vectors - median of the three x values, median of the three y values,
   independently. (Median of three = the middle value; equivalently sum minus min minus max.) Vectors of unavailable and intra neighbours are (0, 0) and take part in the median.

Rule 2 compares with the current refIdx, not with 0 and not with "is inter". A neighbour with refIdx -1 never matches. After rule 1, A, B and C are all the same neighbour, so
either all three match or none does, and the median returns A's vector either way.

### 3.4 Directional prediction for 16x8 and 8x16 (8.4.1.3)

These shapes try one specific neighbour first and fall back to the median:

| partition | try | if its refIdx equals the current refIdx |
|---|---|---|
| 16x8 upper | B | mvp = mvB |
| 16x8 lower | A | mvp = mvA |
| 8x16 left | A | mvp = mvA |
| 8x16 right | C | mvp = mvC (C after the D substitution) |

If the try fails (different refIdx, unavailable, intra), use the median of 3.3 *with the neighbours exactly as derived in 3.2* (the "B and C unavailable" copy of 3.3 happens only inside the median).
Sub-macroblock partitions (8x8, 8x4, 4x8, 4x4) and 16x16 always use the median. The first partition of a macroblock is predicted and stored before the second is predicted, so the 16x8 lower partition's B is the 16x8 upper one (with its *final* vector), and the 8x16 right partition's A is the left one.

### 3.5 P_Skip (8.4.1.1)

A skipped macroblock has refIdxL0 = 0 and a vector derived as follows (neighbours A, B, C/D exactly as for a 16x16 partition, w = 16):

* mv = **(0, 0)** if macroblock A (left) is unavailable, **or** macroblock B (above) is unavailable, **or** (refIdxA == 0 **and** mvA == (0, 0)), **or** (refIdxB == 0 **and** mvB == (0, 0));
* otherwise mv = the median prediction of 3.3 with the current refIdx 0 (including the one-matching-neighbour rule).

"Unavailable" here is real unavailability (picture edge, other slice); an *intra* A is available, with refIdx -1, so it does not trigger the zero rule. Both the zero condition and the median use the neighbour
vectors **as stored**, which for a previous P_Skip is its inferred vector - that is why the storage must contain skipped macroblocks. The skip vector is final (no `mvd`).

### 3.6 Processing order

For each macroblock in decoding order, with its prediction blocks in syntax order (the order the `mvd` were read, Chapter 7.1). In P_8x8 the refIdx is coded once per sub-macroblock, so all partitions of one sub-macroblock share it:

```
for each prediction block (partition or sub-macroblock partition), in order:
    compute neighbours A, B, C/D from the arrays
    mvp = directional rule or median      (with the block's own refIdx)
    mv  = mvp + mvd
    write refIdx and mv into the 4x4 blocks it covers        <- before the next block is predicted
P_Skip: mv = skip rule, write refIdx 0 and mv into 16 entries
intra macroblock (also in P slices): write refIdx -1, mv (0,0) into 16 entries
```

Writing before predicting the next block is what makes the "yes/NO" of the table in 3.2 work without any special case: a block that has not been written still has `slice_id` -1.
The refIdx of later sub-macroblocks is known from the syntax but never consulted: those blocks are not written yet, so they read as unavailable.

### 3.7 Worked examples

All examples use quarter-sample units; "ref" is refIdxL0. The picture is three macroblocks wide; macroblocks are numbered 0 1 2 / 3 4 5 / 6 7 8. All macroblocks belong to one slice unless said otherwise.

**Example 1: median.** Current: 16x16 partition of macroblock 4, ref 0. A = macroblock 3 (ref 0, (4,-2)); B = macroblock 1 (ref 0, (8,6)); C = macroblock 2 (ref 1, (-4,2)).
Two neighbours match (A, B), so the median: x = median(4, 8, -4) = 4, y = median(-2, 6, 2) = 2. mvp = (4, 2). With `mvd` = (1, -3): mv = (5, -1).

**Example 2: one match.** Same position, ref 0. A: ref 1, (10, 10). B: ref 0, (-6, 2). C: intra.
Only B matches, so mvp = (-6, 2) - not the median of (10,10), (-6,2), (0,0), which would be (0, 2).

**Example 3: top row.** Macroblock 1 (first row), 16x16, ref 1. A = macroblock 0 (ref 0, (12, -4)). B and C are outside the picture: unavailable, A available, so B := C := A. Whatever refIdx matches, all three are the same neighbour, so the result is A's vector: mvp = (12, -4).
(The reference index of A is irrelevant here: with B = C = A the answer is always A's vector.)

**Example 4: C replaced by D.** Macroblock 5 (right picture edge), 16x16, ref 0. C would be outside the picture: unavailable, so D = macroblock 1. A = macroblock 4 (ref 1, (0,0)), B = macroblock 2 (ref 1, (0,0)), D = macroblock 1 (ref 0, (-8, 4)).
C := D. Only C (= D) has ref 0, so mvp = (-8, 4). Without the substitution there would be no match and the median of three zero vectors would give (0, 0).

**Example 5: 16x8 and 8x16.** Macroblock 4, all neighbours ref 0: left macroblock 3 (4, 0), above macroblock 1 (-8, 12), above-right macroblock 2 (20, -4), above-left macroblock 0 (100, 100).
* 16x8 upper, ref 0: try B (ref 0 matches): mvp = (-8, 12). Say its `mvd` makes the final vector (-8, 12). Lower, ref 0: try A (ref 0): mvp = (4, 0).
* 8x16 left, ref 0: try A: mvp = (4, 0). Right, ref 0: try C = (16, -1), macroblock 2: mvp = (20, -4).
* 8x16 right with ref 1 instead: C has ref 0, no match, so the median of A (the left partition, say ref 0 (4,0)), B (ref 0, (-8,12)), C (ref 0, (20,-4)): none matches ref 1, so x = median(4,-8,20) = 4, y = median(0,12,-4) = 0, mvp = (4, 0).

**Example 6: P_Skip.**
* Macroblock 0 (top-left corner, A and B unavailable): mv = (0, 0).
* Macroblock 4: A = macroblock 3 has ref 0 and mv (0, 0): zero rule fires, mv = (0, 0) even if B moves wildly.
* Macroblock 4: A is ref 1 (0,0) (not ref 0, no zero), B = ref 0 (8, 6) (not zero), C = macroblock 2 (ref 0, (12, 2)). Two neighbours (B, C) match ref 0, so the median of A (0,0), B (8,6), C (12,2): mv = (8, 2). A's vector is (0, 0) but its refIdx is 1, so it does **not** trigger the zero rule.
* Macroblock 1 in the first row with A = macroblock 0 moving (8, 6): B is unavailable (above the picture), so mv = (0, 0).

**Example 7: sub-macroblock partitions and C.** A P_8x8 macroblock; sub-macroblock 2 (bottom left) is P_L0_4x4, so it has four 4x4 partitions at (0,8), (4,8), (0,12), (4,12). Predict the last one, at (4, 12), ref 0:
A = (3, 12): partition at (0,12). B = (4, 11): partition at (4,8). C = (8, 11): lies in sub-macroblock 3, which is decoded later: unavailable, so use D = (3, 11): partition at (0,8).
All four partitions share sub-macroblock 2's refIdx (0), so A, B and C (= D) all match and the median decides. Suppose the final vectors so far are (0,8): (4, 0), (4,8): (8, -4), (0,12): (-4, 4).
Then mvp = (median(-4, 8, 4), median(4, -4, 0)) = (4, 0). (Treating the not-yet-decoded C as an available intra-like neighbour with ref -1, mv (0,0) would give A, B, that C = (median(-4, 8, 0), median(4, -4, 0)) = (0, 0): wrong.)
For the *first* partition of sub-macroblock 3, at (8, 8) as an 8x8, C = (16, 7) is right of the macroblock, never available; D = (7, 7) is in sub-macroblock 0.

**Example 8: slice edges.** The same 16x16 partition in macroblock 4 (ref 0), A = macroblock 3 (ref 0, (6, 2)), B = macroblock 1 (ref 0, (-2, 8)), C = macroblock 2 (ref 0, (10, -6)).
* One slice for the whole picture: all match, mvp = median = (6, 2).
* The current slice starts at macroblock 2 (macroblocks 0 and 1 belong to the previous slice): B is unavailable, A and C are available. Unavailable B reads as ref -1, mv (0,0): A and C match, median of (6,2), (0,0), (10,-6) = (6, 0).
* The current slice starts at macroblock 3: B and C are unavailable, D (macroblock 0) is unavailable too, A is available: B := C := A, mvp = A's vector (6, 2).
* The current slice starts at macroblock 4: nothing is available, all three read ref -1, no match, median of three zero vectors = (0, 0).

### 3.8 Things that surprise people

* Skipped macroblocks must write refIdx 0 and their inferred vector. Intra macroblocks must write refIdx -1. Both are visible to later neighbours.
* Unavailable and intra both read as (-1, 0, 0) but only intra is *available* (C->D substitution; B/C copy; P_Skip zero rule).
* The refIdx comparison is with the *current partition's* refIdx; a neighbour's vector is used as is (no scaling) in frame pictures, even if it refers to a different picture - that is exactly why the "same refIdx" test exists.
* `mvd` is per prediction block, so in P_8x8 with 4x4 sub-partitions you have up to 16 predictions per macroblock, each reading its already-final neighbours in the same macroblock.
* Your dump is compared with the reference for every prediction block. (Only the *reference's own* cross-check against ffmpeg is coarser: ffmpeg exports one vector per 8x8 block of a P_8x8 macroblock, so sub-partitions beyond the first are proved by the synthetic streams' pixels, section 6.)
* Vectors are not clipped to the picture. Do not reject vectors that point outside it.

## 4. The project: exactly what to build

For every **I and P slice** of a CAVLC, frame-coded, 8-bit 4:2:0 stream (the same scope as Chapter 7.1; other slices are `skipped` as before):

1. keep the three picture-level arrays of 3.1 (reset at the first slice of every picture; slices of one picture share them);
2. for every inter prediction block compute mvp (3.2-3.4), set mv = mvp + mvd, store it; for every P_Skip macroblock compute its vector (3.5) and store; for every intra macroblock store ref -1;
3. print the dump of section 5.

Chapter 7.1's parser and Chapter 7.2's reference lists are inputs; the new work is only the arrays and the predictor. The refIdx is just the number, no picture lookup is needed here.

**Verify** (Summary step 7.3: compare vectors with ffmpeg's visualisation). The harness does the exact comparison instead of eyeballing arrows: ffmpeg's decoder exports its vectors and the harness compares them value for value with the reference that your dump is compared with (section 6). If you also want to look: `ffmpeg -flags2 +export_mvs -i T6.ts -vf codecview=mv=pf out.mp4`.
Exact pixel-level proof of the vectors comes in Chapter 7.4.

## 5. Harness input format

One text file, UTF-8, one record per line, tokens separated by single spaces, `key=value` tokens without spaces in the value. Blank lines and `#` lines are ignored.
This is Chapter 7.1's grammar with two new MB keys. **A Chapter 7.1 dump with `mvp=...` and `mv=...` appended to every MB line is accepted unchanged.** The harness reads only the keys
below (other keys, PCM lines and the HIST line are ignored), so a minimal output is fine too.

```
SLICE <i> type=<I|P|B|SP|SI> first_mb=<n> [...]       one per slice NAL unit (types 1 and 5), stream order, the ones you skip too
MB <i> <addr> name=<..> refidx=<..> mvd=<..> mvp=<..> mv=<..> [other keys]     one per macroblock (skipped ones too), ascending address
END <i> mbs=<n> status=<complete|stopped|skipped|error> [end_bit=..]
```

`<i>` is the 0-based index of the slice NAL unit; slices you skip (B, CABAC ...) get a SLICE line, no MB lines and `status=skipped`.

| key | value |
|---|---|
| `name` | as in Chapter 7.1: `P_Skip`, `P_L0_16x16`, `P_L0_L0_16x8`, `P_L0_L0_8x16`, `P_8x8`, `P_8x8ref0`, or an intra name (`I_NxN`, `I_PCM`, `I_16x16_...`) |
| `refidx` | as in 7.1: `ref_idx_l0` per partition in syntax order (the coded value; 0 where not coded); `-` for intra and skipped |
| `mvd` | as in 7.1: `x:y` per prediction block in syntax order, comma separated; `-` for intra and skipped |
| `mvp` | the predicted vector of every **prediction block**, in the same order as `mvd`, as `x:y` comma separated, quarter-sample units, plain decimal with `-` sign for negatives. A P_Skip macroblock has **one** entry: its inferred vector. Intra: `-` |
| `mv` | the final vector grid: exactly **16** entries `refIdx:mvx:mvy`, comma separated, one per 4x4 luma block in **raster order** (row 0 left to right, then row 1, ...; **not** the zig-zag luma4x4BlkIdx order). For an inter macroblock the entry is the refIdx and final vector of the partition covering that block (P_Skip: `0:mvx:mvy` everywhere); for an intra macroblock the whole key is `-` |

Example excerpt (`...` = lines and keys left out): a 16x8 macroblock, a skipped macroblock and an intra macroblock. A P_8x8 macroblock with sub types (0,1,2,3) would carry 1 + 2 + 2 + 4 = 9 `mvd` and 9 `mvp` entries:

```
SLICE 7 type=P first_mb=0 pic_size_in_mbs=396 header_end_bit=41
...
MB 7 39 name=P_L0_L0_16x8 refidx=0,1 mvd=4:0,-2:1 mvp=4:-4,6:-5 mv=0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,1:4:-4,1:4:-4,1:4:-4,1:4:-4,1:4:-4,1:4:-4,1:4:-4,1:4:-4
MB 7 40 name=P_Skip refidx=- mvd=- mvp=8:-4 mv=0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4,0:8:-4
MB 7 41 name=I_NxN refidx=- mvd=- mvp=- mv=-
...
END 7 mbs=396 status=complete end_bit=...
```

`mv` of the first 8x8 quarter (blocks 0, 1, 4, 5) holds the vector of sub-macroblock 0, blocks 2, 3, 6, 7 the one of sub-macroblock 1, and so on; sub-partitions split those four blocks further
(an 8x4 sub-macroblock 0 gives blocks 0,1 one vector and 4,5 another).

## 6. Running the harness

```
python3 test_mvpred.py <input.ts|input.264> <mv_dump.txt>           # compare your output with the reference
python3 test_mvpred.py --print-expected <input> <expected.txt>      # write the expected dump (all keys; diff it against yours)
python3 test_mvpred.py --make-variants <out_dir>                    # extra inputs
python3 test_mvpred.py --self-test                                  # verify the harness itself (a few minutes)
```

Exit codes: 0 pass; 1 fail (the first ~10 mismatches with slice, macroblock address, key, expected, got, and for `mv` the first differing 4x4 block); 2 unreadable or malformed output, bad
input, or the reference itself failing one of its independent checks.

Inputs: `T5` and `T6` (`python3 -m common.testfiles T5 T6`), then `--make-variants`: `T5.ts`, `T6.ts`; x264 streams `x264_mv_slices4` (4 slices per picture: slice-edge availability), `x264_mv_ref4` (4 references),
`x264_mv_small` (CAVLC, partitions down to 4x4, 8x8 transform off), `x264_mv_high` (High, CAVLC, 8x8 transform), `x264_mv_noskip` (`fast-pskip=0`), `x264_mv_wide` (long vectors), `x264_mv_cip` (intra
neighbours, 3 slices) and `x264_mv_intra_heavy`; synthetic streams `synth_mv`, `synth_mvslices` (5-8 slices per picture), `synth_mvsub` (sub-partition heavy), `synth_mvskip`, each with a `.manifest.json`.

**What the reference is checked against (so you can trust the expected dump):**

1. **ffmpeg's vectors.** libavcodec exports the vectors it decodes (`-flags2 +export_mvs`, `AVMotionVector` side data), but no command-line tool prints the values (`showinfo` prints only a byte count and `ffprobe` only
   the side data's name), and a C helper would need libavcodec's development headers. `common/ffmv_helper.py` (shared with Chapters 7.8, 7.9 and 10.4) therefore loads the shared library with
   Python's `ctypes`, decodes the stream (parser + decoder, one thread) and writes every exported vector (block size, block centre, vector, always in quarter-sample units). It runs as a subprocess with a
   timeout so a crash cannot kill the harness. ffmpeg exports one vector per macroblock partition (skip and 16x16: one; 16x8 and 8x16: two) and, for P_8x8, one per 8x8 block - the vector of that block's first 4x4 sub-block,
   so sub-partition vectors beyond the first are not visible there. The reference's (size, position, vector) multiset per picture must equal ffmpeg's exactly. If the library cannot be loaded the check is skipped with a
   message (and the synthetic-stream check below remains).
2. **Pixels of synthetic streams.** `mvgen.py` writes residual-free P pictures whose partitions use whole-pixel vectors. It chooses the target vector, then writes `mvd` = target - mvp. ffmpeg adds the *true* predictor, so
   if the generator's (reference) predictor were wrong, ffmpeg would copy another block. The harness checks that **every 4x4 luma block** of every inter macroblock (sub-partitions and P_Skip included) in ffmpeg's decoded output
   equals the reference picture named by its refIdx, displaced by the reference vector (edge samples replicated). The textures are random, so a wrong vector almost always copies different pixels; the harness
   reports how many blocks would change under a 1-pixel error (about 85%). Intra neighbours (I_PCM and I_NxN), multi-slice pictures, `ref_idx` > 0 and all sub-macroblock shapes occur.
3. every I and P slice parses to its `rbsp_stop_one_bit` (the Chapter 7.1 checks).

`--self-test` runs these on 14 inputs, shows that five kinds of corrupted reader output fail (one `mv` entry changed, one `mvp` changed, a missing macroblock line, a skipped macroblock without its vector
grid, an intra macroblock printed with refIdx 0 vectors) while the correct dump and a minimal dump pass, and runs 16 deliberately wrong prediction rules through the comparison; each must be caught: no median (mvA only), mean instead of median, the
one-matching-neighbour rule missing, that rule testing "ref >= 0" instead of equality, the B/C-copy-of-A rule missing, no D for C, a not-yet-decoded C treated as available, sub-partition `w` = 8, 16x8 and 8x16
directional rules swapped, P_Skip: missing neighbour not giving zero / zero test on A only / zero test ignoring refIdx 0, intra neighbours treated as unavailable, other slices' blocks available, skipped macroblocks storing ref -1. The self-test also reports for each wrong rule whether ffmpeg's vectors or the pixels flag it too.

## 7. Reading failures

* **The first mismatch in a slice is the cause**; later ones copy its wrong vector through the neighbours.
* `mvp` wrong in the first macroblock row only: the "B and C unavailable, A available" rule (3.3 step 1). Wrong at the right picture edge: D for C. Wrong at the first macroblock of a slice: slice test.
* `mvp` right for 16x16 but wrong for 16x8 / 8x16: the directional rules; check that the first partition was *stored with its final mv* before the second is predicted.
* Wrong only inside P_8x8: C of sub-partitions (table in 3.2) and `w` of the sub-partition. A wrong 4x4 order shows as a wrong `mv` grid with the right `mvp`.
* Wrong P_Skip: print A and B with their stored refIdx, check "refIdx == 0 **and** mv == 0", and that previous skips stored their vectors with refIdx 0.
* Right `mvp`, wrong `mv` grid: store the vector into all covered 4x4 blocks, raster order of the grid, refIdx next to it.
* `mv` of intra macroblocks printed as 16 entries: print `-` (the arrays hold refIdx -1 for them, but the dump key is `-`).

## 8. What's next

Chapter 7.4 uses the vectors: it interpolates luma and chroma samples from the reference picture and adds the inter residual, so T5 decodes byte for byte. That is the exact proof of everything in this chapter.
