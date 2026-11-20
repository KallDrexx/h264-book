# Chapter 7.9 - Direct modes and implicit weights

> **SPOILER WARNING - do not open `spoiler_reference/` until your own decoder passes.**
> It holds a complete solution of this chapter: `ddec.py` is the reference decoder the harness compares you with, `direct_ref.py` the direct rules,
> DistScaleFactor and implicit weights, `dmvpred_ref.py` the vector derivation, `dgen.py` the generator of the synthetic test streams; the other files are
> earlier chapters' solutions. `common/inter_ref.py` (the shared reference parser) is a spoiler too. Everything you need is in this Readme.
> Safe to read: `test_direct.py` (comparison, diagnosis, self-test) and `ffmv_helper.py` (asks libavcodec for the vectors ffmpeg exports).

## 1. Goal and the project

Chapter 7.8 gave you B pictures whose macroblocks *say* which pictures to predict from and with which vectors. Real encoders save most of those bits: for a
large share of the macroblocks of a B picture they send **nothing**, and the decoder *derives* the vectors from information it already has. That is **direct prediction**.
This chapter adds:

1. the three macroblock kinds that use it: **B_Skip** (a skipped macroblock of a B slice), **B_Direct_16x16** (`mb_type` 0) and **B_Direct_8x8** (`sub_mb_type` 0 inside `B_8x8`);
2. **spatial direct** (8.4.1.2.2): reference indices and vectors from the neighbouring macroblocks, with a "this region is standing still" test on the next picture (`colZeroFlag`);
3. **temporal direct** (8.4.1.2.3): the vectors of the co-located block of the next picture, scaled by picture order count distances;
4. `direct_8x8_inference_flag`: which blocks of the co-located macroblock are read;
5. per-picture **motion storage**, because later pictures read it as "co-located data" (and B pictures that are references, the *pyramid*, are read too);
6. **implicit weighted bi-prediction** (`weighted_bipred_idc = 2`, 8.4.3): bi-prediction weights computed from picture order counts, no tables in the slice header.

Milestone: recipes **T9_spatial** and **T9_temporal** (`-profile:v main -x264-params cabac=0:bframes=3:direct=spatial|temporal:weightb=1:b-pyramid=normal`, 50 frames) decode
**byte-identical** to ffmpeg. They contain `B_Skip`, `B_Direct_16x16`, `B_8x8` with direct sub-macroblocks, implicit weights, B pictures used as references, and `memory_management_control_operation 1`
to manage the pyramid's reference set (Chapter 7.6's marking). With this chapter the Phase 7 decoder is complete (section 8).

**What T9 really contains** (counted with the reference): in each file about 9,800 to 10,000 `B_Skip` macroblocks (about half of all macroblocks of the file), 160 to 210 `B_Direct_16x16`, 210 to 240 `B_8x8` macroblocks with at least one direct
sub-macroblock, and between 1,800 and 2,300 bi-predicted prediction blocks whose implicit weights are *not* 32/32 (the rest are symmetric, 32/32). `direct_8x8_inference_flag` is 1 (x264 sets it in these streams). The B pictures with `nal_ref_idc != 0` are read as co-located pictures.
Several situations are rare or absent in T9 (`direct_8x8_inference_flag = 0`, long-term pictures, extreme POC distances, duplicate list entries): the synthetic streams of section 6 exercise them.

## 2. Big picture

A B picture sits between two pictures that are already decoded: usually one in the past and one in the future (list 0 and list 1 of Chapter 7.8). Two observations make "no data" possible:

* **Neighbours move together.** An object is larger than a macroblock. If the macroblocks to the left and above are predicted from picture X with vector v, the current one very probably is as well. *Spatial direct* copies that guess, per list.
* **Motion is steady in time.** If the block at the same position in the *next* picture moved by `mvCol` over a distance of `td` pictures, then over a distance of `tb` the same object moved about `mvCol * tb / td`. *Temporal direct* applies that proportion.
  One more fact helps the spatial mode: if the co-located block did not move, the current block is probably static background too, so its vector is forced to 0 (`colZeroFlag`).

In both modes **both lists are normally used** (a spatial direct block is predicted from one list only when the neighbours have no reference in the other list; a temporal one always uses both), the co-located picture is the first entry of RefPicList1 (and in temporal mode also the list-1 reference), and **nothing is read from the bitstream** except, for `B_Direct_16x16`, the coded block pattern and the residual.
After the derivation a direct block is an ordinary prediction block: the interpolation, weighting, residual and deblocking you have are unchanged.

```
                 spatial direct                                   temporal direct
 refIdxL0/L1 :   smallest valid index of the neighbours A, B, C   L0: the picture the co-located block pointed to, found in RefPicList0
                 (per list, once per macroblock; 3.4)             L1: 0
 mvL0/mvL1   :   16x16 median of the neighbours (as 7.3 / 7.8),  mvL0 = scaled mvCol, mvL1 = mvL0 - mvCol
                 0 where the block is static (colZeroFlag)
 co-located  :   only for colZeroFlag                             always (it is the source of the motion)
```

## 3. Concepts in detail

### 3.1 Where direct appears and what is (not) in the bitstream

| macroblock | how it appears | syntax after it |
|---|---|---|
| `B_Skip` | `mb_skip_run > 0` in a B slice (7.8 section 3.2) | nothing; no residual, `mb_qp_delta` not sent (QP_Y stays that of the previous macroblock) |
| `B_Direct_16x16` | `mb_type` 0 | `coded_block_pattern`, `mb_qp_delta` if cbp > 0, residual (all as for other inter macroblocks); no `ref_idx`, no `mvd` |
| `B_8x8` with `sub_mb_type` 0 (`B_Direct_8x8`) | the four `sub_mb_type` values are read first | that sub-macroblock has no `ref_idx` and no `mvd`; the others read theirs as in 7.8; then cbp and residual |

Your parser already handles all three (7.8). New: you must produce motion for them. Everything downstream treats the result like any other macroblock: the vector predictor of **later** macroblocks reads the stored `refIdx` and vectors of a direct block like those of any inter block,
the loop filter's bS (7.5, 7.8) compares them, and a `B_Skip` has no coefficients, so its `total_coeff` is 0 for the `nC` of its neighbours.

**Granularity.** The motion of a direct macroblock may differ between its four 8x8 quarters (and, with `direct_8x8_inference_flag = 0`, between its sixteen 4x4 blocks). Think of the macroblock as 16 blocks of 4x4 with their own `refIdxL0, mvL0, refIdxL1, mvL1`
(exactly the storage of 7.8), predict each sample from the block it belongs to, and merge equal neighbours if you want speed: interpolation is per sample, so the cut does not change the result.
In a `B_8x8` macroblock a direct sub-macroblock is derived like a quarter of a direct macroblock; the other sub-macroblocks are decoded as before.

### 3.2 What a picture must remember

Direct prediction reads data of **another picture**: the co-located picture. Keep, for every picture that can still be used as `RefPicList1[0]`, for every 4x4 block:

| stored | why |
|---|---|
| `refIdxL0`, `mvL0`, `refIdxL1`, `mvL1` (`-1` and 0 for an unused list; both `-1` = intra block) | the motion to copy or scale; `colZeroFlag` tests `refIdxCol` and `mvCol` |
| for each list, **the picture that `refIdx` named** (an identity, e.g. the index of the coded picture) | temporal direct maps "the picture the co-located block used" into the *current* list 0. The co-located picture's slices had **their own** lists; an index alone is meaningless |

and for each picture its POC. A picture's long-term / short-term status is *not* stored: it is looked up when needed, in the **current** slice's lists (a picture can change from short- to long-term between two uses).
Store the data of every picture you keep as a reference (a non-reference B picture is never a co-located picture, because it is never in a list). Data is stored **after** the picture's own direct derivation: a `B_Skip` stores the vectors it derived.

### 3.3 The co-located block (8.4.1.2.1, frame pictures)

`colPic` is `RefPicList1[0]`, the first entry of list 1 of the current slice (this chapter: frames only; complementary field pairs and MBAFF are Phase 10).
For a macroblock at address `A`, the co-located macroblock is the macroblock at the same address in `colPic`. Which 4x4 block inside it is read:

| `direct_8x8_inference_flag` | block read for the 4x4 block at position (bx, by) of 8x8 quarter q |
|---|---|
| 0 | the block at the same position (bx, by) |
| 1 | the **corner** 4x4 block of that quarter: quarter 0 reads block (0,0), quarter 1 block (3,0), quarter 2 block (0,3), quarter 3 block (3,3) (luma4x4BlkIdx 0, 5, 10, 15). All four 4x4 blocks of a quarter use that one block, so the quarter has one motion |

(with (bx, by) in units of 4 samples, 0..3, from the macroblock's top-left.) From the chosen block take `mvCol`, `refIdxCol` and `refPicCol` (the picture it named):

* the block is **intra** (both lists unused): `refIdxCol = -1`, `mvCol = (0, 0)`;
* else if it **uses list 0** (even if it also uses list 1): `refIdxCol = refIdxL0`, `mvCol = mvL0`, `refPicCol` = the picture list 0 entry named;
* else (list 1 only): the same with list 1.

The preference for list 0 is part of the rule; a block that uses both lists is read through list 0.

### 3.4 Spatial direct (8.4.1.2.2)

**Step 1, once per macroblock** (also for `B_Skip`, `B_Direct_16x16` and *each* direct sub-macroblock of a `B_8x8`: the same numbers for all of them). Look at the 16x16 neighbours exactly as for a 16x16 partition: A = left, B = above, C = above-right (D = above-left if C is not available), each given by the
4x4 block touching the macroblock's corner/edge (as in 7.3). For each list X take the neighbour's `refIdxLX`, using `-1` when the neighbour is not available, is intra, or does not use list X (as in 7.8).

```
MinPositive(x, y) = Min(x, y) if both are >= 0, else Max(x, y)          (so a negative value only wins when both are negative)
refIdxLX = MinPositive(refIdxLXA, MinPositive(refIdxLXB, refIdxLXC))
```

* **both refIdxL0 and refIdxL1 are negative:** `refIdxL0 = refIdxL1 = 0`, both vectors `(0, 0)`, both lists used (a bi-prediction from `RefPicList0[0]` and `RefPicList1[0]`). This is *directZeroPrediction*. (A median of neighbours that are all "refIdx -1" is 0 anyway.)
* otherwise a list with `refIdxLX < 0` is **not used** by the block (predFlagLX = 0); the other list is.
* `mvpLX` = the 16x16 median prediction of that list with the derived `refIdxLX` as the current reference index (8.4.1.3 = Chapter 7.3 / 7.8: the neighbours' `refIdxLX` are compared with it; exactly one match takes that vector; if B and C are unavailable and A is, A is copied; else the component-wise median).
  Take the neighbours of the whole macroblock, **not** of the sub-macroblock, and **not** the other partitions of the current macroblock.

**Step 2, per 4x4 block (per 8x8 quarter with `direct_8x8_inference_flag`).** Read the co-located block (3.3) and compute

```
colZeroFlag = 1   iff   RefPicList1[0] is a SHORT-term picture   and   refIdxCol == 0   and   both components of mvCol are in -1..1
mvLX        = (0, 0)  if   refIdxLX == 0 and colZeroFlag == 1      (for each used list separately)
            = mvpLX   otherwise
```

`refIdxCol == 0` is about the *index* in the co-located slice's list (the block pointed to its first list entry), not about which picture that is. An intra co-located block gives `refIdxCol = -1`, so `colZeroFlag = 0`. A list whose `refIdxLX` is 1 or more keeps `mvpLX` even in a static block.
The reference indices are the same for all blocks of the macroblock; only the vectors differ between static and moving parts.

**Worked example** (T9_spatial; coded picture #11 = POC 22; macroblock 61; its slice has list 0 = pictures #6, #7, #4 and list 1 = picture #10 (POC 26), so colPic is #10):

| neighbour | list 0: refIdx, mv | list 1: refIdx, mv |
|---|---|---|
| A (left) | -1 (it uses list 1 only), (0,0) | 0, (24,0) |
| B (above) | 0, (0,0) | -1 |
| C (above-right) | 0, (0,0) | -1 |

List 0: `MinPositive(-1, MinPositive(0, 0)) = MinPositive(-1, 0) = 0`. List 1: `MinPositive(0, MinPositive(-1, -1)) = MinPositive(0, -1) = 0`. Both lists are used, with `refIdxL0 = refIdxL1 = 0`.
`mvpL0`: B and C match refIdx 0 (two matches, so the median): median of (A: (0,0), B: (0,0), C: (0,0)) = (0,0). `mvpL1`: only A matches refIdx 0, so `mvpL1 = (24, 0)`.
Co-located macroblock in #10 (inference on, corners): the top two corner blocks have `refIdxCol = 0`, `mvCol = (0,0)` (list 0): `colZeroFlag = 1`, so `mvL0 = mvL1 = (0,0)` in the **upper two quarters**. The lower two corners have `mvCol = (-48, 0)`: `colZeroFlag = 0`, so
there `mvL0 = (0,0)` (its predictor is 0) and `mvL1 = (24, 0)`. The reference dump line for this macroblock therefore has, for the lower 8 blocks, `l1=0:24:0` and for the upper 8 blocks `l1=0:0:0`; `l0=0:0:0` everywhere.

### 3.5 Temporal direct (8.4.1.2.3)

For each 4x4 block (8x8 quarter with inference) read the co-located block (3.3). Then

```
refIdxL0 = 0                                         if refIdxCol < 0 (intra), and mvCol = (0, 0)
         = lowest index i with RefPicList0[i] = refPicCol     otherwise  ("MapColToList0")
refIdxL1 = 0
pic0 = RefPicList0[refIdxL0]     pic1 = RefPicList1[0]     cur = the current picture
```

`MapColToList0` looks for the **picture** `refPicCol`, the one the co-located block used, in the *current slice's* list 0 (frames: the frame itself). If it occurs several times (modification commands can duplicate entries) the **lowest** index is taken. A conforming stream always has it in the list; if it
does not, the stream is not conforming (the harness's synthetic streams never do this).

```
if RefPicList0[refIdxL0] is a long-term picture or DiffPicOrderCnt(pic1, pic0) == 0:      mvL0 = mvCol,  mvL1 = (0, 0)
else:
   tb = Clip3(-128, 127, DiffPicOrderCnt(cur, pic0))        td = Clip3(-128, 127, DiffPicOrderCnt(pic1, pic0))
   tx = (16384 + Abs(td / 2)) / td                          ("/" truncates toward zero, also for negative numbers)
   DistScaleFactor = Clip3(-1024, 1023, (tb * tx + 32) >> 6)
   mvL0 = (DistScaleFactor * mvCol + 128) >> 8              (each component; ">>" is an arithmetic shift, rounding toward minus infinity)
   mvL1 = mvL0 - mvCol
```

`DiffPicOrderCnt(a, b)` = `PicOrderCnt(a) - PicOrderCnt(b)` (for frames: the POC of Chapter 7.8). The block uses **both lists** with `refIdxL1 = 0`. (In a conforming stream the `td == 0` case only happens with an intra co-located block, `mvCol = 0`; it matters for the implicit weights of 3.6.)
Two details that bite: `/` in `tx` truncates toward zero (flooring it is a common bug), while `>>` on negative numbers floors.

**Worked example** (T9_temporal; coded picture #5 = POC 8; macroblock 163, quarter 0; list 0 = [#1 (POC 6), #2 (POC 2)], list 1 = [#4 (POC 10)]; colPic = #4). The co-located corner block (0,0) has `refIdxCol = 2` in picture #4's own list 0, which named picture **#2**; `mvCol = (8, -40)`.

| step | value |
|---|---|
| `refIdxL0` | picture #2 is entry 1 of the *current* list 0, so `refIdxL0 = 1` (the number 2 means nothing here), `refIdxL1 = 0` |
| `pic0` = #2 (POC 2), `pic1` = #4 (POC 10), cur = POC 8 | `tb = 8 - 2 = 6`, `td = 10 - 2 = 8` |
| `tx` | `(16384 + 4) / 8 = 16388 / 8 = 2048` |
| DistScaleFactor | `(6 * 2048 + 32) >> 6 = 12320 >> 6 = 192` |
| `mvL0` | x: `(192 * 8 + 128) >> 8 = 1664 >> 8 = 6`; y: `(192 * -40 + 128) >> 8 = -7552 >> 8 = -30` (floor of -29.5) |
| `mvL1 = mvL0 - mvCol` | `(6 - 8, -30 + 40) = (-2, 10)` |

The reference dump has `1:6:-30` for list 0 and `0:-2:10` for list 1 on those four blocks.

An intra co-located block (I pictures and intra macroblocks of P/B pictures) gives `refIdxL0 = 0` and a zero vector for both lists: a bi-prediction of `RefPicList0[0]` and `RefPicList1[0]` without motion.

### 3.6 Implicit weights (`weighted_bipred_idc = 2`, 8.4.3)

With `weighted_bipred_idc = 2` the slice header carries **no** weight table. Instead every **bi-predicted** prediction block (both lists used) gets weights from the POC distances of its two reference pictures; blocks that use one list are predicted exactly as without weighting
(this is not the explicit mode of 7.7, where one-list blocks were weighted too). The weights depend on the *pair* `(pic0, pic1)` of that block, i.e. on its `refIdxL0` and `refIdxL1` (so two partitions of one macroblock can have different weights).

```
pic0 = RefPicList0[refIdxL0]    pic1 = RefPicList1[refIdxL1]
if DiffPicOrderCnt(pic1, pic0) == 0, or pic0 or pic1 is long-term:                    w0 = w1 = 32
else   DistScaleFactor as in 3.5 (tb from the CURRENT picture and pic0, td from pic1 and pic0, with the clips)
       if (DistScaleFactor >> 2) < -64 or (DistScaleFactor >> 2) > 128:               w0 = w1 = 32
       else:   w1 = DistScaleFactor >> 2      w0 = 64 - w1
logWD = 5, o0 = o1 = 0
sample = Clip1( ((pred0 * w0 + pred1 * w1 + 2^logWD) >> (logWD + 1)) )        ( = (pred0 * w0 + pred1 * w1 + 32) >> 6 )
```

`pred0` and `pred1` are the interpolated 8-bit predictions as in 7.8; luma and both chroma planes use the same weights. With w0 = w1 = 32 the formula equals the default average `(pred0 + pred1 + 1) >> 1`. The weights are computed from the **POC distances**, not from the order of the lists:
a B picture halfway between its references gets 32/32 (and so most blocks of T9: its POCs step by 2). The weights can be negative or above 64 when both references are on the same side of the current picture (extrapolation) - `w1 = 128` and `w0 = -64` are still allowed.

| cur | pic0 | pic1 | tb | td | tx | DistScaleFactor | DistScaleFactor >> 2 | (w0, w1) |
|---|---|---|---|---|---|---|---|---|
| 8 | 2 | 10 | 6 | 8 | 2048 | 192 | 48 | (16, 48) |
| 2 | 0 | 6 | 2 | 6 | 2731 | 85 | 21 | (43, 21) |
| 20 | 0 | 16 | 20 | 16 | 1024 | 320 | 80 | (-16, 80) (extrapolation) |
| 4 | 0 | 2 | 4 | 2 | 8192 | 512 | 128 | (-64, 128) (the largest allowed) |
| 5 | 0 | 2 | 5 | 2 | 8192 | 640 | 160 | outside -64..128: (32, 32) |
| 10 | 4 | 2 | 6 | -2 | -8192 | -768 | -192 | outside: (32, 32) (`tx` of a negative `td`: `(16384 + 1) / -2 = -8192`) |
| 6 | 6 | 6 | 0 | 0 | - | - | - | td = 0: (32, 32), the formula is not evaluated |

(The first row is the block of the temporal example: `w = (16, 48)` there; the second row is what the `WT` lines of T9_temporal show for coded picture 2: `43:21`.)

A direct block uses implicit weights too, with *its* derived `refIdxL0 / refIdxL1`; for spatial direct that is the same pair for the whole macroblock.

### 3.7 Order of derivation and neighbours

* Decode a macroblock's partitions in order. For a `B_8x8`, sub-macroblocks 0..3 in order; a direct sub-macroblock stores its derived `refIdx` and vectors when its turn comes, and a later *non-direct* sub-macroblock sees them as neighbours (like those of any earlier partition). A neighbour that is not decoded yet is "not available" as always.
* Spatial direct always uses the **macroblock's** neighbours A, B, C (D), which lie outside the macroblock, so its result does not depend on the order inside the macroblock.
* After deriving a direct macroblock **store** its `refIdx` and vectors (both lists, per 4x4 block, with the pictures they name): neighbours read them, the loop filter compares them, and a later picture may read them as co-located data.
* Deblocking: the bS rules of 7.5 / 7.8 are unchanged and use the derived data; a `B_Skip` has no coefficients; a `B_Direct_16x16` or `B_8x8` with residual has coefficients in the usual 4x4 blocks.
* Intra macroblocks of B pictures store "both lists unused" (they are the intra co-located blocks of later pictures).

### 3.8 Things that can go wrong without being a bug of yours

* **Frame-threaded ffmpeg** decodes temporal direct streams with several slices per picture differently from the standard. The harness always asks ffmpeg for **one** thread (`-threads 1`); do the same when you compare by hand.
* ffmpeg keeps only **one** table "reference number to picture" per picture. If a co-located picture's slices had **different reference lists** and a later slice uses temporal direct, ffmpeg and the standard disagree. The harness's synthetic stream `synth_colslices` is exactly this case, so its expected frames come from the reference decoder (the harness says so), and no other test stream reads such a picture in temporal mode.
* In a picture that mixes P and B slices ffmpeg's loop filter compares only list 0 across a slice boundary if the current macroblock is in the P slice. The synthetic stream with mixed slice types therefore uses `disable_deblocking_filter_idc = 2`.

## 4. The project: exactly what to build

Extend your Chapter 7.8 decoder:

1. **Motion storage per picture** (3.2): per 4x4 block both lists' `refIdx`, vector and *the picture each `refIdx` named*; POC per picture; keep it with every picture you may use as a reference. Intra blocks store "unused".
2. **Slice context**: for each B slice, a table `refIdx -> (picture, POC, is-long-term)` for both lists, `direct_spatial_mv_pred_flag`, the SPS's `direct_8x8_inference_flag`, and `colPic = RefPicList1[0]` with its stored data.
3. **Direct derivation** for `B_Skip`, `B_Direct_16x16` and each direct sub-macroblock: co-located block (3.3), then spatial (3.4) or temporal (3.5). Store the results as ordinary motion (3.7) before the next partition / macroblock.
4. **Prediction**: per prediction block as in 7.8; a direct block can be bi-predicted from `RefPicList0[refIdxL0]` and `RefPicList1[refIdxL1]`, or from one list (spatial, one `refIdx` negative).
5. **Implicit weights** (3.6) for bi-predicted blocks when the slice's PPS has `weighted_bipred_idc = 2`. Other slices keep the default average (idc 0) or the explicit tables (idc 1, Chapter 7.7 / 7.8).
6. **Residual and skip bookkeeping**: `B_Skip` has no residual; the QP and `nC` rules are those of P_Skip. `B_Direct_16x16` and `B_8x8` parse cbp and residual as usual.
7. **Output** as in 7.8 (display order = POC order inside each IDR period).

Verify: **T9_spatial** and **T9_temporal** decode byte-identical to ffmpeg. The x264 and synthetic variants of section 6 test the cases T9 never reaches.
Recommended order: first `T9_spatial` with the inference and colZeroFlag rules, then `T9_temporal`, then the implicit weights (T9 uses them in both). Use the dumps below to separate *derivation* mistakes from *reconstruction* mistakes.

## 5. Harness input format

**Primary: the pixels.** `your.yuv`: every frame in display order, 8-bit planar yuv420p, cropped as the SPS says, no padding, no headers (Chapter 5.1 format).

Two **optional text dumps**: line oriented, tokens separated by single spaces, `key=value` without spaces in the value, blank lines and lines starting with `#` ignored, all numbers decimal. Write the lines in ascending `<pic>` then `<addr>` (the harness matches lines by `(<pic>, <addr>)`, so the order is not checked; a duplicate, missing or extra line is an error).
A dump with a wrong number of entries or an unparsable token makes the harness stop with exit 2 and the line number.

### 5.1 `--mv-dump`: final motion vectors of both lists (the 7.8 grammar; now it also contains direct macroblocks)

```
MV <pic> <addr> l0=<16 entries> l1=<16 entries>        one line per INTER macroblock of every P and B slice (P_Skip, P and B types, B_Skip, B_Direct_16x16, B_8x8 incl. direct sub-macroblocks); no line for intra macroblocks
```

`<pic>` = the 0-based index of the coded picture in **decoding order** (every picture of the stream counts, including non-reference pictures); `<addr>` = macroblock address. Each list has exactly 16 comma-separated entries, one per 4x4 luma block in **raster order**
(row by row, not luma4x4BlkIdx order): `refIdx:mvx:mvy` (vector in quarter samples; `refIdx` = the index in that slice's list) or `-` if the block does not use that list. Example (the macroblock of 3.4: upper 8 blocks static, lower 8 blocks moving in list 1):

```
MV 11 61 l0=0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0 l1=0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:0:0,0:24:0,0:24:0,0:24:0,0:24:0,0:24:0,0:24:0,0:24:0,0:24:0
```

For a direct macroblock **all 16 entries are given even when the derivation is per 8x8** (with inference: four equal entries per quarter).

### 5.2 `--weights-dump`: implicit weights of bi-predicted blocks

```
WT <pic> <addr> w=<16 entries>        one line per macroblock of a B slice with weighted_bipred_idc = 2 that has at least one bi-predicted 4x4 block
```

Entries in raster order as above: `w0:w1` for a block that uses both lists (including `32:32` where a fallback applies) or `-` for a block that uses one list. Example (T9_temporal, coded picture 2 = POC 2, references POC 0 and 6): `WT 2 3 w=43:21,43:21,...` (16 entries; 43:21 is the (2, 0, 6) row of the table in 3.6).
A line of the same picture with one-list blocks: `WT 2 5 w=-,-,43:21,43:21,-,-,43:21,43:21,-,-,43:21,43:21,-,-,43:21,43:21`.

`python3 test_direct.py --print-expected mv|weights T9_temporal.ts out.txt` writes the whole expected text (about 18,800 `MV` lines and 9,700 to 10,300 `WT` lines for T9).

## 6. Running the harness

```
python3 -m common.testfiles T9_spatial T9_temporal                         # builds .cache/T9_spatial.ts and .cache/T9_temporal.ts
python3 test_direct.py .cache/T9_spatial.ts your.yuv                       # pixels (the milestone); likewise T9_temporal
python3 test_direct.py .cache/T9_spatial.ts your.yuv --mv-dump mv.txt --weights-dump wt.txt
python3 test_direct.py .cache/T9_spatial.ts --mv-dump mv.txt               # only one channel
python3 test_direct.py --print-expected mv|weights .cache/T9_spatial.ts out.txt     # the expected text, to diff against yours
python3 test_direct.py --make-variants DIR                                 # writes the test inputs below into DIR (about 1.5 minutes)
python3 test_direct.py --self-test                                         # checks the harness itself (about 2 minutes on 12 cores, about 10 CPU-minutes in total: it uses all cores)
```

Exit 0 = identical, 1 = different, 2 = unusable input or output (missing file, malformed dump, a harness-level disagreement between the reference and an independent source - that one is the harness's problem).

**Truth.** Pixels: ffmpeg (`-threads 1 -fps_mode passthrough -f rawvideo -pix_fmt yuv420p`), compared with `common/yuvcmp.py`. The dumps come from the reference implementation in `spoiler_reference/`; before a dump is compared, the harness checks the reference against independent sources:
its decoded pixels equal ffmpeg's, and the motion vectors ffmpeg exports (`-flags2 +export_mvs`; one vector per 8x8 quarter for direct macroblocks, per partition for the others) equal the reference's in both directions. The weights have no independent export; they are validated by the whole-picture pixel equality (a wrong weight changes pixels).
The reference decoder is pure Python: T9 takes about 20 to 25 seconds.

Inputs from `--make-variants` (x264 files are 1 second of 352x288, `cabac=0`; the synthetic streams are 96x80, POC type 0 with a 10-bit lsb, no residual, I_PCM / Intra16x16 / P / non-direct B macroblocks mixed with direct ones):

| file | what it exercises |
|---|---|
| `T9_spatial.ts`, `T9_temporal.ts` | the milestone |
| `d_auto.ts` | `direct=auto`: x264 picks spatial or temporal per picture |
| `d_sp_bf1.ts`, `d_tmp_bf2.ts`, `d_sp_bf4.ts`, `d_tmp_bf4.ts` | 1, 2 and 4 consecutive B pictures; `bframes=4` with `b-adapt=2`, 5 references |
| `d_sp_strict.ts`, `d_tmp_strict.ts` | `b-pyramid=strict`; `d_sp_wb0.ts`, `d_tmp_wb0.ts`: `weightb=0` (direct blocks with the default average) |
| `d_sp_slices.ts`, `d_tmp_slices.ts` | `slices=3`: co-located data across slices (identical lists, so ffmpeg agrees with the standard) |
| `d_sp_part.ts`, `d_tmp_part.ts` | `partitions=all`, 4 references: co-located blocks of every size, `refIdxCol` / `refIdxL0 > 0` |
| `d_nodeblock.ts`, `d_fade.ts`, `d_idr.ts` | no loop filter; a fade (explicit weights in P, implicit in B); `keyint=10` (several IDR periods: POC restarts) |
| `synth_sp_inf1.264`, `synth_sp_inf0.264` | spatial direct with `direct_8x8_inference_flag` 1 and **0** (per-4x4 co-located blocks) |
| `synth_tmp_inf1.264`, `synth_tmp_inf0.264` | temporal direct, inference 1 and 0, POC distances 2..5 |
| `synth_mixed.264` | three slices per picture, spatial and temporal slices next to each other, P and B slices mixed (no deblocking across slices) |
| `synth_lt.264`, `synth_lt_tmp.264` | long-term pictures (MMCO 4 / 6 / 3, `long_term_pic_num` in list modification): `RefPicList1[0]` long-term (colZeroFlag), long-term `pic0` (temporal fallback, implicit weights 32/32) |
| `synth_poc_far.264`, `synth_poc_far_tmp.264` | POC steps up to 140: clipping of `tb` / `td` and of DistScaleFactor, the 32/32 fallbacks, extrapolating weights, negative `td` |
| `synth_static.264`, `synth_moving.264` | nearly static content (`colZeroFlag` almost always 1) and vectors around the +-1 limit |
| `synth_intra.264` | many intra macroblocks: intra co-located blocks, intra neighbours of direct blocks |
| `synth_tail.264` | B pictures whose references are all in the past: both lists hold the same pictures (`pic0 == pic1` happens, so `td = 0`) |
| `synth_map.264`, `synth_dup_w.264` | many references, modification commands with repeated targets: `MapColToList0` takes the lowest index; with explicit weights (idc 1) that index selects the weights |
| `synth_colslices.264` | the case of 3.8: a co-located picture with different lists per slice (expected frames from the reference decoder, flagged by the harness) |
| `synth_wp.264`, `synth_idc1.264` | implicit weights next to explicit P weights; `weighted_bipred_idc = 1` with direct blocks |

Every synthetic stream carries a JSON manifest next to it (`<name>.264.json`: the macroblock types the generator wrote, whether ffmpeg is the truth). A synthetic stream is only used when the reference decoder accepts it: every temporal-direct slice must find the co-located block's reference picture in `RefPicList0`;
the generator re-writes slices that would not, as spatial or direct-free slices.

## 7. Reading failures / debugging tips

**Pixels.** On a difference the harness decodes the stream with the reference up to the first wrong frame and prints: the picture, the first wrong macroblock, its type, whether it is direct (spatial / temporal) and `direct_8x8_inference_flag`, the slice's lists (as coded picture numbers), every prediction block with lists, `ref_idx`, the *picture* each index names, vectors and
(implicit) weights, for a direct macroblock the four corner co-located blocks it read, and whether **your whole frame equals the reference with one deliberately wrong rule** (it then names it). Example of the last part:

```
your frame is EXACTLY what the reference decoder produces with the wrong rule(s):
  - d_col_mv_zero_only: colZeroFlag needs mvCol == (0, 0) exactly, not both components in [-1, 1]
```

The wrong rules (46 are tried; the harness's self-test shows that each changes the pictures of at least one test stream):

| wrong rule (name) | visible as |
|---|---|
| `d_minpos_min`, `d_minpos_a_only`, `d_no_c_to_d`, `d_nb_per_part` | spatial direct: wrong reference index or vector predictor (MinPositive vs Min, only neighbour A, C not replaced by D, neighbours of the sub-macroblock instead of the macroblock) |
| `d_zero_pred_l0_only` | both neighbour indices negative: only list 0 used (should be bi-prediction with refIdx 0 / 0, zero vectors) |
| `d_col_mv_zero_only`, `d_col_mv_pm2`, `d_col_ref_any`, `d_col_lt_ignored`, `d_col_zero_any_ref`, `d_col_zero_never` | the colZeroFlag test: vector range (+-1), `refIdxCol == 0`, short-term `RefPicList1[0]`, which refIdx is zeroed, never applied |
| `d_col_l0_only`, `d_col_l1_first`, `d_col_mv_ref_swapped` | which list of the co-located block is read (list 0 first; L1-only blocks must be used; do not mix list 0 data with list 1 data) |
| `d_infer_ignored`, `d_infer_topleft`, `d_infer_forced` | which co-located 4x4 block is read: corner blocks exactly when `direct_8x8_inference_flag` is 1 |
| `d_t_map_none`, `d_t_map_last`, `d_t_ref1_col`, `d_t_intra_ref` | temporal `refIdxL0`: mapping through the pictures (lowest index), `refIdxL1` = 0, intra co-located block |
| `d_t_dsf_round`, `d_t_tx_floor`, `d_t_tb_clip_missing`, `d_t_dsf_clip_missing`, `d_t_mv_noround`, `d_t_mvl1_sign`, `d_t_pic0_is_col_ref`, `d_t_lt_ignored` | temporal vector scaling: +32, truncating division, the clips, +128, `mvL1 = mvL0 - mvCol`, `pic0 = RefPicList0[refIdxL0]`, long-term fallback |
| `d_direct_not_stored`, `d_sub_direct_as_l0` | storage and bookkeeping: direct macroblocks must store their motion; a direct sub-macroblock is not a list-0 block with mvd 0 |
| `w_swapped`, `w_shift_3`, `w_logwd_6`, `w_round_missing`, `w_poc_cur_ignored`, `w_clip_tb_td_missing` | implicit weights: w0 / w1 swapped, `>> 2`, logWD 5, rounding term, tb from the current picture, the clips |
| `w_no_range_fallback`, `w_range_symmetric`, `w_no_td0_fallback`, `w_no_lt_fallback`, `w_idc2_as_default`, `w_single_list` | the 32/32 fallbacks (range -64..128, td = 0, long-term), idc 2 treated as 0, weights applied to one-list blocks |
| `skip_mv_zero`, `skip_is_coded`, `ref_idx_compare` (from 7.5 / 7.8) | deblocking of skipped / direct blocks (bS uses the derived vectors, not coefficients, not `ref_idx` numbers) |

If your frame is the right frame at the wrong place ("your frame N is exactly ffmpeg's frame M") it is an output-order problem (7.8).

**Debugging order.** (1) `--mv-dump` for the first stream that fails: the first differing line says which macroblock and which 4x4 block; look at its name (`B_Skip`, direct, which mode) in the message. A difference in a direct macroblock's *reference indices* is a MinPositive / mapping problem, in *vectors* a predictor, colZero or scaling problem.
(2) A correct mv dump with wrong pixels: `--weights-dump`, then the prediction itself (implicit weights, one-list blocks unweighted), then the deblocking. (3) A wrong mv dump in an *ordinary* macroblock next to a direct one: your direct macroblock stored something different from what it predicted from (or did not store at all).

| symptom | likely cause |
|---|---|
| T9_spatial wrong from the first B picture, in `B_Skip` | MinPositive / D substitution, or you used the neighbours of each 8x8 instead of the macroblock |
| spatial direct right, but moving areas wrong | the co-located block (corner blocks; list 0 before list 1), the `+-1` range, `refIdxCol == 0` |
| only the lower / right quarters of a direct macroblock wrong | `direct_8x8_inference_flag`: the quarter's corner block, not its first block |
| temporal direct wrong from the first B picture | `refIdxL0` mapping (an index of the co-located slice used as an index of the current slice) or `tx` truncation / the `+32`, `+128` rounding |
| temporal direct right until the pyramid starts | the co-located block is a block of a B picture: list 0 first, else list 1 with *its* picture; the stored picture identities |
| only bi-predicted blocks slightly off (+-1 .. few) | implicit weights: a 32/32 case you computed, `>> 2`, the rounding term |
| P pictures after a B reference picture wrong | you stored (or not) motion data of non-reference pictures, or the pyramid's marking |
| the pixels are right, `--mv-dump` differs only in `B_Skip` rows | you print the 4x4 grid of the *prediction partitions* instead of the 16 stored blocks |

## 8. Phase 7 recap and what's next

**Phase 7 is complete.** After this chapter your decoder reads CAVLC streams with every picture type of the Baseline and Main profiles that a progressive 8-bit 4:2:0 file can contain, and **every CAVLC test file decodes byte-identical to ffmpeg**:
T1 to T4 (intra), T5, T6, T6_ref16 (P pictures, all partition sizes, up to 16 references), T7 (explicit weighted prediction in P pictures), T8 (B pictures, bi-prediction, reference lists) and T9_spatial / T9_temporal (direct prediction, implicit weights, B pictures as references). That is a complete Baseline decoder plus CAVLC Main (except interlace and FMO / ASO / redundant slices):
slice types I, P, B, multiple slices per picture, the loop filter, reference list modification and marking, weighted prediction in all three modes, output in display order within each IDR period (the full output order logic - bumping - comes in Chapter 9.1).

**Phase 8** adds **CABAC** and the **High profile**: the arithmetic decoder, context modelling for every syntax element you now parse with Exp-Golomb and CAVLC code tables (mb_skip_flag instead of mb_skip_run, mb_type, ref_idx, mvd, cbp, the residual), then the 8x8 transform, Intra 8x8 prediction and scaling matrices. CABAC only replaces the **entropy decoding**; prediction,
motion vectors, direct modes, weights, reference management and the loop filter of Phases 5 to 7 are reused unchanged. Most real-world progressive `.ts` files (broadcast, IPTV, HLS) are High-profile CABAC, so Phase 8 is where your decoder starts to work on them.
