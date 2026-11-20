# Chapter 5.4 - Intra 4x4 prediction

## 1. Goal and the project

Chapter 5.3 predicted whole macroblocks (Intra16x16) and the chroma of every macroblock. One kind of luma remained a placeholder: **I_NxN** macroblocks, which code luma as sixteen separately predicted 4x4 blocks (Intra 4x4; the same syntax element also carries Intra 8x8 in High profile, Phase 8). x264 uses them for about a quarter of the macroblocks even when you ask for none, which is why T1 could not be exact until now.

You will build:

1. The nine **Intra4x4 prediction modes** (spec 8.3.1.2), with exact formulas.
2. The neighbour rules of a 4x4 block: which of the left, above, above-right and above-left samples exist, and what replaces the above-right samples when they do not. These depend on the **block index** inside the macroblock.
3. **Mode prediction** (8.3.1.1): the stream does not send each block's mode directly; it sends "same as predicted" or a small correction. You rebuild the actual modes.
4. The per-macroblock loop for I_NxN: for each of the 16 blocks in decoding order, predict, add the residual, store, and only then move to the next block.
5. A complete reader: parse, reconstruct every macroblock, write the frame.

**Milestone: the first full-file byte-exact decode.** After this chapter, T1 and T2 (and the no-deblocking recipes below) decode **byte-identical** to ffmpeg's reference YUV, over every frame. The Summary says the same for T1 in Chapter 5.3; Chapter 5.3's Readme explains why that was not possible (T1 contains I_NxN macroblocks). T1 is the proper milestone here.

> **Spoiler warning.** `intra4ref.py` and `refdecode.py` in this folder are a complete solution (shipped for the harness's self-test), and `intraref.py` / `transref.py` are copies of the solutions of Chapters 5.3 / 5.2. **Do not open them** before you have written your own and a test has failed. `predgen.py` (the stream generator) computes no pictures, but it contains its own implementation of the availability and mode-prediction rules because it must write legal streams.

## 2. Big picture

Intra 4x4 trades side information for accuracy. A 16x16 predictor must pick one direction for 256 samples. A 4x4 block can pick its own direction, so it follows edges and textures closely; the cost is 16 mode choices per macroblock (cheaply coded, because the mode of a block is usually close to that of its neighbours).

What the decoder must get exactly right:

- **Z-order.** The 16 blocks are not predicted from the macroblock's outside only: block 1 reads block 0's finished samples, and so on. Prediction, residual, clip and store must be finished for block `k` before block `k+1` is predicted. Reconstructed samples before deblocking are used (there is no deblocking in Phase 5).
- **The above-right samples.** Many modes read 4 samples beyond the block's right edge. Whether those exist depends on where the block is in the macroblock and in the picture. Misjudging this is the classic Intra 4x4 bug.
- **Mode prediction.** One wrong mode derivation corrupts a block, and, because later blocks predict from it, a whole stretch of the picture.
- **Arithmetic.** The 3-tap filter `(a + 2b + c + 2) >> 2` and the 2-tap average `(a + b + 1) >> 1` appear in different places per mode; the exact tap positions are in the spec and below.

## 3. Concepts in detail

Notation. For the 4x4 block being predicted, `(x, y)` with `0 <= x, y <= 3` is a sample of the block (x is the column). The neighbouring samples are

- `p[x, -1]` for `x = 0..3`: the row above the block,
- `p[x, -1]` for `x = 4..7`: the **up-right** samples, the row above the block's right neighbour,
- `p[-1, y]` for `y = 0..3`: the column to the left,
- `p[-1, -1]`: the corner above-left.

All values are 8-bit; `>>` is an arithmetic shift. Spec sections: 8.3.1 for everything here.

### 3.1 Which samples exist

Every neighbouring sample belongs either to another macroblock or to the current one.

**Macroblock level.** For the current macroblock you already have A (left), B (above), D (above-left) from Chapter 5.3: inside the picture, and in the **same slice**. Intra 4x4 adds **C**, the macroblock above-right (`mbAddr - w + 1`): available when `mbY > 0`, `mbX < w - 1` (not the last column), and in the same slice. (When a slice begins in the middle of a row, B is unavailable for its first `w` macroblocks, but C is already available for the last of them; see the note below.)

**Block level.** Let the block sit at `(bx, by)` (each 0..3, in units of 4 samples) inside the macroblock; the table of block indexes is:

```
        bx=0  bx=1  bx=2  bx=3
by=0     0     1     4     5
by=1     2     3     6     7
by=2     8     9    12    13
by=3    10    11    14    15
```

(`luma4x4BlkIdx` is the 8x8 quadrant number times 4 plus the position inside the quadrant, again in z-order: `(bx, by) = (2 * ((idx >> 2) & 1) + (idx & 1), 2 * (idx >> 3) + ((idx >> 1) & 1))`.)

| Neighbour | Available when |
| --- | --- |
| left column `p[-1, y]` | `bx > 0` (then it is a block of the current macroblock, always already decoded), else macroblock A is available |
| above row `p[0..3, -1]` | `by > 0`, else macroblock B is available |
| corner `p[-1, -1]` | `bx > 0` and `by > 0`; or `bx = 0, by > 0`: A; or `by = 0, bx > 0`: B; or `bx = 0, by = 0`: D |
| up-right `p[4..7, -1]` | see below |

**Up-right availability** (6.4.11.4 together with the rule at the end of 8.3.1.2). The up-right samples are the bottom row of the block diagonally up and to the right, `(bx + 1, by - 1)`. They can be used when that block exists **and has already been decoded**:

- Top row of the macroblock (`by = 0`): the samples lie in macroblock B for blocks 0, 1 and 4 (`bx` = 0, 1, 2), so they are available exactly when B is. For **block 5** (`bx = 3`) they lie in macroblock **C**: available when C is.
- Inside the macroblock (`by > 0`): the block up and right must have a smaller index than the current one. Block 3's up-right neighbour is block 4: decoded later, so not available. Block 11's is block 12: later, not available. For `bx = 3` (blocks 7, 13, 15) the up-right samples would lie in the macroblock to the right, which is not decoded yet: not available.

| blkIdx | up-right block | up-right samples available when |
| --- | --- | --- |
| 0, 1, 4 | in macroblock B | B available |
| 5 | in macroblock C | C available |
| 2 | block 1 | always (inside the macroblock, decoded) |
| 6 | block 5 | always |
| 8 | block 3 | always |
| 9 | block 6 | always |
| 10 | block 9 | always |
| 12 | block 7 | always |
| 14 | block 13 | always |
| **3, 7, 11, 13, 15** | later block / next macroblock | **never** |

(Block 5 is not in the "never" group. It is a common mistake to put it there because it is in the same column as 7, 13 and 15; it reads macroblock C. Five blocks never have up-right samples: 3, 7, 11, 13, 15.)

A note on C. When B is available, C is available exactly when `mbX < w - 1`, because both are earlier in the same slice. When B is not available but C is (a slice that starts at C), no up-right sample is ever read, since modes that read up-right samples also read the row above. So whether you check C's slice or only the picture edge makes no observable difference; compute it correctly anyway.

**Substitution (8.3.1.2).** If the up-right samples are not available but the above row `p[0..3, -1]` is, set `p[x, -1] = p[3, -1]` for `x = 4..7`. After that, modes simply read `p[0..7, -1]` whenever the above row exists. Only modes 3 and 7 read `x >= 4`. Note this means block 3, say, is predicted with a "flat" extension of its own above row.

**Which modes need what.** A mode that requires an unavailable neighbour never appears in a valid stream (treat it as a stream error and stop with a message):

| Mode | Name | Needs |
| --- | --- | --- |
| 0 | Vertical | above row |
| 1 | Horizontal | left column |
| 2 | DC | nothing (rules for every combination) |
| 3 | Diagonal down-left | above row (up-right substituted if missing) |
| 4 | Diagonal down-right | above row, left column, corner |
| 5 | Vertical-right | above row, left column, corner |
| 6 | Horizontal-down | above row, left column, corner |
| 7 | Vertical-left | above row (up-right substituted if missing) |
| 8 | Horizontal-up | left column |

### 3.2 The nine modes

In every formula `pred[x, y]` is the predicted sample for `0 <= x, y <= 3`. `p[x, -1]` with `x = -1` and `p[-1, y]` with `y = -1` both mean the corner `p[-1, -1]`.

**Mode 0, vertical.** `pred[x, y] = p[x, -1]`.

**Mode 1, horizontal.** `pred[x, y] = p[-1, y]`.

**Mode 2, DC.** With `S_top = sum of p[0..3, -1]` and `S_left = sum of p[-1, 0..3]`:

| left available | above available | pred (the same for every sample) |
| --- | --- | --- |
| yes | yes | `(S_top + S_left + 4) >> 3` |
| yes | no | `(S_left + 2) >> 2` |
| no | yes | `(S_top + 2) >> 2` |
| no | no | 128 |

The up-right samples are never part of DC.

**Mode 3, diagonal down-left** (along the anti-diagonal, towards the lower left; reads `p[0..7, -1]`).

```
pred[3, 3] = (p[6, -1] + 3 * p[7, -1] + 2) >> 2
otherwise:   pred[x, y] = (p[x+y, -1] + 2 * p[x+y+1, -1] + p[x+y+2, -1] + 2) >> 2
```

All samples with the same `x + y` are equal. The special case is the generic formula with `p[8, -1]` replaced by `p[7, -1]`.

**Mode 4, diagonal down-right** (along the main diagonal; reads above row, left column, corner).

```
x > y:   pred[x, y] = (p[x-y-2, -1] + 2 * p[x-y-1, -1] + p[x-y, -1] + 2) >> 2
x < y:   pred[x, y] = (p[-1, y-x-2] + 2 * p[-1, y-x-1] + p[-1, y-x] + 2) >> 2
x == y:  pred[x, y] = (p[0, -1] + 2 * p[-1, -1] + p[-1, 0] + 2) >> 2
```

Samples with the same `x - y` are equal. (For `x - y = 1` the first formula's first term is `p[-1, -1]`, the corner.)

**Mode 5, vertical-right.** Let `zVR = 2 * x - y`.

```
zVR even (0, 2, 4, 6):  pred = (p[x-(y>>1)-1, -1] + p[x-(y>>1), -1] + 1) >> 1
zVR odd  (1, 3, 5):     pred = (p[x-(y>>1)-2, -1] + 2 * p[x-(y>>1)-1, -1] + p[x-(y>>1), -1] + 2) >> 2
zVR == -1:              pred = (p[-1, 0] + 2 * p[-1, -1] + p[0, -1] + 2) >> 2
zVR == -2 or -3:        pred = (p[-1, y-1] + 2 * p[-1, y-2] + p[-1, y-3] + 2) >> 2
```

**Mode 6, horizontal-down.** Let `zHD = 2 * y - x`.

```
zHD even (0, 2, 4, 6):  pred = (p[-1, y-(x>>1)-1] + p[-1, y-(x>>1)] + 1) >> 1
zHD odd  (1, 3, 5):     pred = (p[-1, y-(x>>1)-2] + 2 * p[-1, y-(x>>1)-1] + p[-1, y-(x>>1)] + 2) >> 2
zHD == -1:              pred = (p[-1, 0] + 2 * p[-1, -1] + p[0, -1] + 2) >> 2
zHD == -2 or -3:        pred = (p[x-1, -1] + 2 * p[x-2, -1] + p[x-3, -1] + 2) >> 2
```

Modes 5 and 6 are transposes of each other (swap the roles of above row and left column, `x` and `y`).

**Mode 7, vertical-left** (reads `p[0..7, -1]`).

```
y = 0 or 2:  pred = (p[x+(y>>1), -1] + p[x+(y>>1)+1, -1] + 1) >> 1
y = 1 or 3:  pred = (p[x+(y>>1), -1] + 2 * p[x+(y>>1)+1, -1] + p[x+(y>>1)+2, -1] + 2) >> 2
```

**Mode 8, horizontal-up** (reads the left column only). Let `zHU = x + 2 * y`.

```
zHU even (0, 2, 4):  pred = (p[-1, y+(x>>1)] + p[-1, y+(x>>1)+1] + 1) >> 1
zHU odd  (1, 3):     pred = (p[-1, y+(x>>1)] + 2 * p[-1, y+(x>>1)+1] + p[-1, y+(x>>1)+2] + 2) >> 2
zHU == 5:            pred = (p[-1, 2] + 3 * p[-1, 3] + 2) >> 2
zHU > 5 (6, 7, 8, 9): pred = p[-1, 3]
```

A worked example, to check your reading: above row `p[0..3, -1] = 10 20 30 40`, up-right `p[4..7, -1] = 50 60 70 80`, left column `p[-1, 0..3] = 100 90 80 70`, corner `p[-1, -1] = 110`, all available. Rows of `pred`, top to bottom:

| Mode | Row 0 | Row 1 | Row 2 | Row 3 |
| --- | --- | --- | --- | --- |
| 3 diagonal down-left | 20 30 40 50 | 30 40 50 60 | 40 50 60 70 | 50 60 70 78 |
| 4 diagonal down-right | 83 38 20 30 | 100 83 38 20 | 90 100 83 38 | 80 90 100 83 |
| 5 vertical-right | 60 15 25 35 | 83 38 20 30 | 100 60 15 25 | 90 83 38 20 |
| 6 horizontal-down | 105 83 38 20 | 95 100 105 83 | 85 90 95 100 | 75 80 85 90 |
| 7 vertical-left | 15 25 35 45 | 20 30 40 50 | 25 35 45 55 | 30 40 50 60 |
| 8 horizontal-up | 95 90 85 80 | 85 80 75 73 | 75 73 70 70 | 70 70 70 70 |

Spot checks: mode 3, `pred[3, 3] = (70 + 3 * 80 + 2) >> 2 = 78` and `pred[0, 0] = (10 + 2 * 20 + 30 + 2) >> 2 = 20`. Mode 4, `pred[0, 0] = (10 + 2 * 110 + 100 + 2) >> 2 = 83`. Mode 8, `pred[0, 0] = (100 + 90 + 1) >> 1 = 95`, `pred[3, 1] = (80 + 3 * 70 + 2) >> 2 = 73`, `zHU = 5`. With the up-right samples unavailable, mode 3 reads `p[4..7, -1] = 40` instead and its last row becomes `40 40 40 40`.

### 3.3 Mode prediction (8.3.1.1)

An I_NxN macroblock without the 8x8 transform carries, for each of its 16 blocks in `luma4x4BlkIdx` order, `prev_intra4x4_pred_mode_flag` (1 bit) and, only when that flag is 0, `rem_intra4x4_pred_mode` (3 bits). Chapter 4.2 parsed them. The actual mode `Intra4x4PredMode[blk]` is rebuilt as follows.

1. Find the left neighbour block (the block holding the sample left of the block's top-left sample) and the above neighbour block. Inside the macroblock these are the earlier blocks `(bx-1, by)` and `(bx, by-1)`. At the macroblock's left edge the left neighbour is block `(3, by)` of macroblock A; at the top edge the above neighbour is block `(bx, 3)` of macroblock B.
2. Mode of each neighbour, `intraMxMPredModeA` and `intraMxMPredModeB`:
   - If a neighbour block lies in a macroblock that is **not available** (outside the picture or in another slice), a flag `dcPredModePredictedFlag` is set. With the flag set, **both** values are 2 (DC). It does not matter that only one neighbour was missing.
   - Otherwise, if the neighbouring macroblock is not I_NxN (Intra16x16 or I_PCM), its value is **2** (DC). Only that neighbour counts as DC; the other keeps its real mode.
   - Otherwise it is the `Intra4x4PredMode` of that neighbouring block. (An I_NxN neighbour using the 8x8 transform would use its 8x8 mode of the block `luma4x4BlkIdx >> 2`; that is Phase 8. Inter neighbours with `constrained_intra_pred_flag = 1` also set the flag; they exist only in P slices, Chapter 5.5 / Phase 7.)
3. `predIntra4x4PredMode = Min(intraMxMPredModeA, intraMxMPredModeB)`.
4. If `prev_intra4x4_pred_mode_flag` is 1: `Intra4x4PredMode = predIntra4x4PredMode`. Otherwise: `Intra4x4PredMode = rem` if `rem < predIntra4x4PredMode`, else `rem + 1`. (The 8 values of `rem` skip the one value that the flag already covers, so the 9 modes fit 3 bits.)

Examples. Left neighbour I_NxN block with mode 8, above neighbour I_NxN block with mode 5: pred = 5. Left in an unavailable macroblock, above mode 0: pred = 2 (not 0). Left is an Intra16x16 macroblock, above mode 0: pred = `Min(2, 0)` = 0. With `pred = 5`: `rem = 4` gives mode 4, `rem = 5` gives mode 6.

Keep the 16 final modes of every I_NxN macroblock of the picture until the picture is finished (the next macroblocks and the next row read them). For other macroblock types store nothing, or an "other" marker.

### 3.4 Prediction, residual and the z-order loop

For one I_NxN macroblock:

1. Compute the macroblock availability (A, B, C, D) as in 3.1. Derive the 16 modes (3.3).
2. For `blk = 0 .. 15`: compute the block's availability (3.1), build the 4x4 prediction (3.2), compute the 4x4 residual, store `Clip1(pred + residual)` into the luma plane. The next block reads those stored samples.
3. Chroma (Cb, Cr): exactly as in Chapter 5.3, from `intra_chroma_pred_mode`, the chroma CBP and the chroma levels. This does not depend on the luma part.

The luma residual of an I_NxN block comes from `residual4x4` of Chapter 5.2 with **all 16 levels** of the block (the DC is an ordinary coefficient, `dc_pre_scaled` false, unlike Intra16x16 where the DC comes pre-transformed) and the macroblock's `QP_Y`. A block has levels only when the bit of its 8x8 quadrant (`blkIdx >> 2`) is set in the luma part of `coded_block_pattern` (`cbp & 15`); then the Phase 4 parser delivers 16 levels (possibly all zero) in scan order. Otherwise its residual is zero and the block is pure prediction.

`QP_Y`: an I_NxN macroblock carries `mb_qp_delta` only if `coded_block_pattern > 0`. With `cbp = 0` the macroblock's QP is the running QP unchanged. (No residual is decoded in that case, so the value only matters as the running QP for the following macroblock, which Chapter 5.3's chain already handles.)

**Transform size.** If the PPS enables `transform_8x8_mode_flag`, an I_NxN macroblock starts with `transform_size_8x8_flag`; when it is 1 the macroblock is Intra 8x8 (Phase 8). Baseline streams, and everything in this chapter, have the flag at 0. A reader that meets a 1 should stop with a clear message.

**Do not** predict all 16 blocks first and add the residuals afterwards: block `k` must see the finished samples of the blocks before it.

## 4. The project: exactly what to build

Extend your reader (`reader <in.ts|in.264> <out.yuv>`) so that it decodes a whole intra-only CAVLC file without deblocking. The complete flow, so that nothing is left to guess:

1. **Parse.** Phases 1 to 4: demux, find SPS/PPS and slices, parse slice data macroblock by macroblock (mb_type, the 16 mode syntax elements of I_NxN, chroma mode, CBP, QP delta, residual levels). Keep per picture: a per-macroblock slice number, the 16 final modes of I_NxN macroblocks, and the macroblock type.
2. **Per macroblock, reconstruct immediately.** I_PCM: copy samples. Intra16x16: Chapter 5.3. I_NxN: derive modes (3.3), 16 blocks in z-order (3.4), then chroma (5.3). Prediction needs the neighbours in the frame buffer, so reconstruction cannot wait for the whole picture.
3. **Write the frame** (cropped, planar yuv420p, Chapter 5.1) when all macroblocks of the picture are done.

Build, in this order:

1. The nine prediction functions: `(plane, x0, y0, mode, a, b, c, d)` in, a 4x4 block out; keep them pure.
2. Block availability (3.1) as a function of `(blkIdx, A, B, C, D)`. Write the table of 3.1 first and test it by hand.
3. Mode derivation (3.3).
4. The I_NxN reconstruction loop (3.4).

Verify (the Summary says "T2 decodes byte-identical"): T1, T2, T4_nodeblock and `default_intra` decode **byte-identical**, and so do all synthetic streams. T3 and T4 contain the deblocking filter (Phase 6) and cannot match yet; see the table in section 6.

## 5. Harness input format

The only file your reader produces is `out.yuv`, exactly as in Chapters 5.1 and 5.3: raw planar yuv420p, no header, one **cropped** frame per decoded picture in decode order. The harness derives everything else itself. The reader's input may be the `.ts` file or the raw `.264`.

## 6. Running the harness

Create the synthetic and x264 test streams once (into the git-ignored `.cache`, or any folder):

```
cd "<repo>/Chapter 5.4 - Intra 4x4 prediction"
python3 test_intra4.py --make-variants ../.cache/v54                # writes <repo>/.cache/v54/*.ts and *.264
cd .. && python3 -m common.testfiles T1 T2 T4_nodeblock default_intra   # must run from <repo>; writes <repo>/.cache/T1.ts etc.
```

New recipes this chapter adds to `common/testfiles.py`, backward compatibly:

| Recipe | `<options>` (after the standard `-qp 26` command of the Summary) | Why |
| --- | --- | --- |
| `T4_nodeblock` | `-profile:v baseline -x264-params keyint=1:partitions=i4x4:no-deblock=1:slices=4` | T4 without the deblocking filter: the Summary's multi-slice recipe in a form Phase 5 can decode exactly |
| `default_intra` | `-profile:v main -x264-params cabac=0:keyint=1:no-deblock=1` | "default-style" input: Main profile, x264's default partitions and RD choices, intra only, CAVLC, no deblocking |

The synthetic streams are written by `predgen.py`, an encoder-side bit writer. It draws, for every I_NxN macroblock, a legal Intra4x4 mode for each block (given the block's neighbour availability), encodes it through `prev_intra4x4_pred_mode_flag` / `rem_intra4x4_pred_mode` using its own mode prediction, and writes random CBPs, levels, QPs and chroma QP offsets with valid CAVLC. It mixes in Intra16x16 and I_PCM macroblocks and uses slice layouts with every availability case. It does not compute pictures; ffmpeg's decode is the truth.

| Stream | Content | Frames | Tests |
| --- | --- | --- | --- |
| `i4_edges` | 6x5 macroblocks, one slice, 85% I_NxN | 10 | picture edges, every mode on every block index |
| `i4_rows` | one slice per macroblock row | 8 | B, C, D unavailable at every row start |
| `i4_ragged` | 7x6, slices of random length (start mid-row), a few I_PCM | 12 | above macroblock in another slice, C cases |
| `i4_only` | 100% I_NxN, ragged slices | 6 | mode prediction between I_NxN macroblocks only |
| `i4_mixed` | 50% I_NxN, Intra16x16 and I_PCM neighbours | 10 | mode prediction against non-I_NxN neighbours |
| `i4_qp` | 5x4, every QP 0..51 | 12 | QP chain and residual scaling |
| `i4_pcm` | 25% I_PCM | 12 | steep neighbours, clipping |
| `i4_crop` | 5x4 coded, cropped on all sides | 4 | cropping |
| `i4_big` | 22x18, ragged slices | 3 | a CIF-sized sanity run |
| `x264_smooth`, `x264_smooth_slices`, `x264_qp14`, `x264_qp44` | real x264 content, intra only, no deblocking; smooth gradients, 3 slices, QP 14, QP 44 with 2 slices | 25 each | real-encoder mode choices at several bit rates |

`--make-variants` also checks that the generated synthetic streams cover every case that matters: every Intra4x4 mode under every block availability pattern; every mode on every block index in an interior macroblock; real and substituted up-right samples on every block index that can have each; and mode prediction with every combination of unavailable / I_NxN / Intra16x16 / I_PCM neighbour on both sides, with all three syntax cases (`flag = 1`, `rem < pred`, `rem >= pred`).

Run your reader on a stream, then the harness:

```
<your reader> ../.cache/T2.ts out.yuv
python3 test_intra4.py ../.cache/T2.ts out.yuv
```

What is compared: ffmpeg's output (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`, cropped) against `out.yuv`, via `common/yuvcmp.py`. On failure the harness adds a table of wrong macroblock types (luma per Intra16x16 mode, per Intra4x4 mode counted in blocks, I_PCM; chroma per chroma mode), the first wrong luma and chroma macroblock with their availability, modes, CBP and QP, and for an I_NxN macroblock the 16-block table: per block in decoding order, the correct mode, `predIntra4x4PredMode` (column `pred`), the syntax (`flag=1` or `rem=r`), which of its left / above / up-right / corner samples exist (`A B C D`, 1 = available), and `ok` / `WRONG`. Exit code 0 pass, 1 fail, 2 missing or unusable output.

Expected results: **everything passes**: all synthetic streams, `x264_*`, T1, T2, T4_nodeblock, `default_intra`. A stream with P/B slices, CABAC, 8x8 transform or deblocking is outside Phase 5 (the harness says so and falls back to a plain frame comparison, which will fail if deblocking is on).

### Which Phase 5 chapters unlock which recipes

| Recipe | 5.1 | 5.2 | 5.3 | 5.4 | Remaining |
| --- | --- | --- | --- | --- | --- |
| I_PCM-only streams (5.1's generator, CVPCMNL1) | exact | | | | |
| Single-macroblock transform vectors (5.2) | | unit-tested | | | |
| Synthetic Intra16x16-only streams; x264 `-preset ultrafast` | | | exact | exact | |
| T1 (I16x16, but ~25% I_NxN anyway) | | | chroma only (`--only-i16`) | **exact** | |
| T2 (adds Intra 4x4) | | | | **exact** | |
| `default_intra` (Main, intra-only, no deblocking) | | | | **exact** | |
| T4_nodeblock (T2 + 4 slices) | | | | **exact** (needs the slice-aware availability of 5.3) | 5.5 re-verifies it together with QP deltas |
| T3, T3 with deblock offsets, T4 (deblocking on) | | | | not yet | Phase 6 |
| T5 and later (P/B slices, CABAC, High) | | | | | Phases 7 to 8 |

The Summary's verify bullet of 5.5 says "T4 decodes byte-identical". T4 as defined there has deblocking on; the Phase 5 equivalent is `T4_nodeblock`, and the real T4 matches after Phase 6.

**How the harness checks itself.** `python3 test_intra4.py --self-test` (a few minutes) runs the reference reader (`refdecode.py`) on every stream, including T1, T2, T4_nodeblock and `default_intra`, and requires byte-identical output. It requires these to fail: one wrong luma sample, one wrong Cr sample, Cb and Cr swapped, a truncated file. It then plants twenty-one deliberately wrong rules, one at a time, in the reference reader, and requires each to be caught by the streams: seven wrong prediction formulas (DC rounding with and without the left column, the corner of diagonal-down-left, 2-tap versus 3-tap in vertical-right and vertical-left, 2-tap instead of 3-tap in horizontal-down, the zHU = 5 case), eight wrong up-right rules (substitution by 128, block 5 never, block 5 ignoring the picture edge, blocks 3 and 11 allowed, block 7, blocks 13 and 15, block 2 and block 1 denied), five wrong mode-prediction rules (max instead of min, unavailable neighbour as DC only for that neighbour, Intra16x16 / I_PCM neighbour as vertical, `rem <=` instead of `rem <`, availability ignoring slices) and predicting all 16 blocks before storing any. Four of them are also tried on T2 itself.

> **Spoiler warning.** `intra4ref.py` (the formulas of section 3 in executable form) and `refdecode.py` (the reference reader built on `common/cavlc_ref.py`, `intraref.py`, `intra4ref.py` and `transref.py`) are complete solutions, needed for `--self-test`. Do not open them until you have tried, and then only to compare after a test has failed and the debugging tips did not help.

## 7. Reading failures / debugging tips

Start from the first wrong luma macroblock line and the 16-block table. The first `WRONG` block in decoding order is the cause; later blocks of that macroblock, and later macroblocks, inherit its error. Print your own mode, `pred` and availability for that block and compare them with the table's columns: a different mode points to 3.3, a different `C` to 3.1, and the same mode and availability to the formulas of 3.2.

| Symptom | Likely cause |
| --- | --- |
| Whole picture wrong from macroblock 0, I_NxN blocks | residual not added, the z-order violated (blocks predicted before earlier blocks are finished), or `residual4x4` fed the Intra16x16 style (DC pre-scaled) |
| First wrong block is 5 and it uses mode 3 or 7, at the last column | up-right of block 5 read beyond the picture, or substituted wrongly |
| First wrong block is 5 elsewhere, modes 3 / 7 | block 5 treated as having no up-right, but it reads macroblock C |
| First wrong block is 3, 11, 7, 13 or 15 with mode 3 or 7 | you used samples that are not decoded yet (they read stale or zero data) instead of substituting `p[3, -1]` |
| Mode 3 or 7 wrong only where up-right is missing | the substitute: copy `p[3, -1]` into `p[4..7, -1]`, do not use 128 or leave it at 0 |
| Modes 5, 6 wrong in few samples | the `zVR == -1` / `zHD == -1` corner case, or odd/even taps swapped; compare against a hand calculation of 3.2 |
| Mode 8 wrong in the bottom right | the `zHU == 5` and `zHU > 5` cases |
| DC wrong in only some blocks | availability of the left or above row at macroblock edges; rounding `+ 4 >> 3` with both, `+ 2 >> 2` with one |
| Wrong only in macroblocks at the start of a mid-row slice | availability: B above belongs to the previous slice |
| Wrong blocks only next to I16x16 / I_PCM macroblocks | mode prediction: such a neighbour counts as 2, but does not turn the other neighbour into 2 |
| Wrong blocks only at picture / slice edges | mode prediction: either neighbour unavailable gives `pred = 2` regardless of the other |
| Right modes visually but wrong by the mode number | `rem` handling: `rem < pred ? rem : rem + 1` |
| Only chroma of I_NxN macroblocks wrong | Chapter 5.3's chroma path for I_NxN (separate from luma) |
| Everything right except some blocks that have `cbp` bit set but no levels | you applied `residual4x4` to stale levels of a block of a previous macroblock: clear level arrays per macroblock |
| T2 passes but `x264_qp14` or `i4_qp` fails | 16-bit overflow or clipping in the transform (Chapter 5.2) or `mb_qp_delta` wrap |

Use `common/yuvcmp.py`'s map to see the pattern: one wrong macroblock then a spread right and down is normal. For the first wrong block, dump its neighbours (8 above, 4 left, corner) and your prediction, and recompute the prediction by hand.

## 8. What's next

Chapter 5.5 completes Phase 5: QP tracking with the wrap-around, and neighbour availability under `constrained_intra_pred_flag` and multiple slices. Then Phase 6 adds the deblocking filter, which T3 and T4 need.
