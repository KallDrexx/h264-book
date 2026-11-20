# Chapter 5.1 - Frame buffers and YUV writer

## 1. Goal and the project

Phases 1-4 turned a transport stream into parsed macroblocks. Phase 5 turns macroblocks into pixels. This chapter builds the container for those pixels (a frame buffer), the arithmetic that says where each macroblock's samples live in it, and a writer that saves frames as a raw `.yuv` file. The only macroblock type you can reconstruct today is I_PCM, whose samples are stored raw in the bitstream. Every other macroblock gets a neutral gray placeholder until Chapters 5.2-5.4.

What you already have from Phases 1-4: a demuxed Annex B stream, NAL units with their RBSP, parsed SPS, PPS and slice headers (picture size in macroblocks, cropping, `first_mb_in_slice`), and a CAVLC I-slice macroblock parser that yields `mb_type`, I_PCM sample bytes and per-block coefficients. Phase 5 ends when T1, T2 and T4 decode byte-identical to ffmpeg's `ref.yuv`. You need a way to see exactly where you differ, so this chapter also ships the shared tool `common/yuvcmp.py`.

You will build:

1. Three sample planes per frame (Y, Cb, Cr) with macroblock addressing.
2. An I_PCM path that copies the stored samples into the planes.
3. A writer that appends each finished, cropped frame to `out.yuv` as planar yuv420p.
4. A reader program that runs the whole pipeline: `.ts` in, `.yuv` out.

## 2. Big picture

A decoded picture is a grid of samples, and the decoder fills it in macroblock by macroblock. Video uses the Y'CbCr colour model: **Y** (luma) is brightness, **Cb** and **Cr** (chroma) are two colour-difference signals. Our target format is **4:2:0**. Chroma has half the width and half the height of luma, so each chroma sample covers a 2x2 square of luma samples. A 16x16 luma macroblock therefore comes with an 8x8 block of Cb and an 8x8 block of Cr, 384 samples in all. Every sample is 8 bits (one byte).

A raw YUV file has no header, no frame markers and no padding. It is just frames back to back. The file does not say how large the frames are, so you must remember the size (this is why the comparison tool takes W and H on its command line).

Why can you simply write frames as they are decoded? H.264 allows pictures to be decoded in a different order from the one they are displayed in (B pictures, Phase 7). The test files of Phase 5 (T1, T2, T4) contain only I pictures, so decode order equals display order and ffmpeg's output order is the same as yours. Output reordering is a Phase 9 topic. For now: **one frame per decoded picture, written as soon as the picture is complete.**

## 3. Concepts in detail

### 3.1 Planar yuv420p layout

For a frame of W x H samples (the size *after* cropping, see 3.4), with CW = (W+1)/2 and CH = (H+1)/2 (integer division):

| Part | Samples | Byte offset in the frame |
| --- | --- | --- |
| Y plane, row after row, top to bottom | W x H | 0 |
| Cb plane | CW x CH | W x H |
| Cr plane | CW x CH | W x H + CW x CH |

Frame size is W x H + 2 x CW x CH bytes, and frame number n starts at n x frame size. For even W and H (the only case a 4:2:0 SPS can produce, as 3.4 explains for luma) the frame size is W x H x 3/2. Odd luma sizes can occur in other contexts, hence the rounded-up chroma size, and the chroma size can be odd even when luma is even: a 198x118 picture has 99x59 chroma planes. ffmpeg and the harness use the rounded-up formula, so use it too.

### 3.2 Frame buffer, stride and padding

In memory, plane row y starts at `y x stride`. The **stride** (also called pitch or linesize) is the distance in samples between the starts of consecutive rows. It may be larger than the plane width, for alignment or padding. Two simple options for your decoder:

- Allocate planes at the full macroblock-aligned size (luma 16 x PicWidthInMbs by 16 x FrameHeightInMbs, chroma half of that) with stride equal to that width. This is what you want: every macroblock then fits entirely, even when the picture is cropped.
- Add extra borders later for motion compensation if you wish (Phase 7); you do not need that now.

The **file** has no stride: the writer must copy only the visible W samples of each row, row by row. Writing the whole padded buffer is the classic bug.

### 3.3 Macroblock to pixel addressing

A macroblock has an address `mbAddr` counted in raster order, 0 at the top left. With PicWidthInMbs = w:

| Quantity | Formula |
| --- | --- |
| MB column and row | mbX = mbAddr mod w, mbY = mbAddr / w |
| Top-left luma sample | (16 x mbX, 16 x mbY) |
| Top-left chroma sample (each of Cb, Cr) | (8 x mbX, 8 x mbY) |
| Luma sample (i, j) of the MB, 0 <= i, j < 16 | plane position (16 x mbX + i, 16 x mbY + j) |
| Chroma sample (i, j), 0 <= i, j < 8 | plane position (8 x mbX + i, 8 x mbY + j) |

Phase 5 predicts and transforms in 4x4 blocks. Luma blocks are numbered `luma4x4BlkIdx` 0..15, but **not in raster order**: the macroblock is split into four 8x8 quadrants in raster order (top-left, top-right, bottom-left, bottom-right), and each quadrant's four 4x4 blocks are again in raster order. The position of block `b` inside the macroblock (6.4.3):

- x = InverseRasterScan(b / 4, 8, 8, 16, 0) + InverseRasterScan(b mod 4, 4, 4, 8, 0), which in plain words is x = 8 x ((b / 4) mod 2) + 4 x (b mod 2)
- y = InverseRasterScan(b / 4, 8, 8, 16, 1) + InverseRasterScan(b mod 4, 4, 4, 8, 1), which is y = 8 x (b / 8) + 4 x ((b / 2) mod 2)

| b | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| x | 0 | 4 | 0 | 4 | 8 | 12 | 8 | 12 | 0 | 4 | 0 | 4 | 8 | 12 | 8 | 12 |
| y | 0 | 0 | 4 | 4 | 0 | 0 | 4 | 4 | 8 | 8 | 12 | 12 | 8 | 8 | 12 | 12 |

Chroma 4x4 blocks (`chroma4x4BlkIdx` 0..3 per component) are plain raster order: x = 4 x (b mod 2), y = 4 x (b / 2). Put this mapping in a table now. You will use it in every later chapter.

### 3.4 Cropping

The coded picture is always a whole number of macroblocks, but the intended picture is often smaller (1080p is coded as 1088 rows). The SPS carries `frame_cropping_flag` and four offsets (Chapter 3.1). For 4:2:0 (spec 7.4.2.1.1):

- CropUnitX = 2, and CropUnitY = 2 x (2 - frame_mbs_only_flag)
- output width = 16 x PicWidthInMbs - CropUnitX x (left + right)
- output height = 16 x FrameHeightInMbs - CropUnitY x (top + bottom)
- the top-left output luma sample is the decoded sample at (CropUnitX x left, CropUnitY x top); the chroma planes start at half of those coordinates (the units are even, so this is exact)

Here FrameHeightInMbs = (2 - frame_mbs_only_flag) x PicHeightInMapUnits. Without the flag all four offsets are 0.

**Convention for all of Phase 5:** ffmpeg's reference output is cropped per the SPS, so **your writer writes the cropped frame** (the visible rectangle, not the full macroblock-aligned buffer). Keep decoding into the full-size buffer and crop only when writing. Cropping on the left or top also shifts which decoded samples land in the file, so copy from the offset position, not just fewer columns.

### 3.5 I_PCM macroblocks in the bitstream

I_PCM (`mb_type` 25 in an I slice) stores raw samples. After the `mb_type` code, `pcm_alignment_zero_bit`s pad to the next byte boundary, then comes the sample data, then (if the slice continues) the next macroblock starts on the byte boundary right after the last sample (7.3.5):

| Order | Samples | Layout |
| --- | --- | --- |
| 1 | 256 luma (`pcm_sample_luma`) | raster order of the 16x16 block: row 0 left to right, then row 1, ... |
| 2 | 64 Cb | raster order of the 8x8 block |
| 3 | 64 Cr | raster order of the 8x8 block |

Each sample is 8 bits (bit depth 8). Note that the samples are read from the RBSP: emulation prevention bytes have been removed already (Chapter 2.2), and the parser needs a "skip to byte alignment" operation on the bit reader. Writing the PCM macroblock is then a plain copy into the planes: no prediction, no transform. (The deblocking filter would also touch I_PCM edges, but the test streams disable it, and it comes in Phase 6.)

### 3.6 When is a frame finished?

A picture can consist of several slices (T4 has four). Allocate and initialise the buffer when the first slice of a new picture arrives (use the new-picture detection of Chapter 3.4), fill the macroblocks each slice covers starting from its `first_mb_in_slice`, and write the frame out when the next picture starts or the stream ends. Fill the buffer with 128 at the start of each picture, so that macroblocks you cannot reconstruct yet are a neutral gray.

## 4. The project: exactly what to build

A command-line reader:

```
reader <in.ts> <out.yuv>
```

It must run the full Phase 1-4 pipeline on `in.ts` and write `out.yuv`:

1. Allocate Y, Cb, Cr planes for the macroblock-aligned size, from the active SPS.
2. For each I slice macroblock: if it is I_PCM, copy its 384 samples to the planes using 3.3. For any other macroblock type, parse it fully (so the slice stays in sync) but write 128 into all its luma and chroma samples. This placeholder disappears in 5.3 and 5.4.
3. When a picture is complete, write the cropped frame to `out.yuv` in planar yuv420p (3.1, 3.4). Overwrite an existing file; do not append across runs.
4. Frames are written in decode order. One output frame per primary coded picture (no frame is dropped or duplicated).
5. Exit non-zero with a message on a stream you cannot handle; do not write a partial or wrong-sized file silently.

Keep the planes, the macroblock addressing helpers and the writer separate from the parsing code. Later chapters add prediction and the transform in between.

**Verify.** The Summary's check is "a gray or noise frame opens correctly in the viewer at the right size" and, if you have them, the CAVLC PCM conformance streams (e.g. CVPCMNL1_SVA_C) matching exactly. This chapter's harness gives you something stronger that needs nothing downloaded: synthetic I_PCM streams with different sizes and cropping, for which ffmpeg's decode is known to equal the raw samples (the harness checks that too), plus structure checks on any file.

Viewing tip (manual only, the harness never opens a window): run your reader on T1 and look at the all-gray result, or on a synthetic variant to see noise:

```
ffplay -f rawvideo -pixel_format yuv420p -video_size 200x120 out.yuv
```

With the correct size you see a steady picture of noise (or gray). With a wrong size the picture shears diagonally or flickers: that tells you your frame size or crop is off. A bad chroma size shows a green or magenta tint.

## 5. Harness input format

Only `out.yuv` is read: raw bytes, planar yuv420p, no header, as in 3.1. Nothing is printed by the reader that the harness needs. For the synthetic streams the harness also uses a `<name>.manifest.json` next to the `.ts` file (written by `--make-variants`; you do not need to read it).

## 6. Running the harness

Create the synthetic streams (they are written to the directory you give; use `.cache/variants` or any scratch folder):

```
python3 test_yuv.py --make-variants ../.cache/variants
```

This writes six streams (`.ts` plus the raw `.264`) and the manifests:

| Stream | Coded size | Output size | Frames | Slices per picture | Tests |
| --- | --- | --- | --- | --- | --- |
| pcm_16x16 | 16x16 | 16x16 | 4 | 1 | smallest picture |
| pcm_176x144 | 176x144 | 176x144 | 3 | 1 | plain QCIF |
| pcm_200x120 | 208x128 | 200x120 | 3 | 2 | crop right and bottom |
| pcm_198x118 | 208x128 | 198x118 | 3 | 3 | odd chroma size 99x59 |
| pcm_100x60_lt | 112x64 | 100x60 | 3 | 2 | crop left and top too |
| pcm_1920x1080 | 1920x1088 | 1920x1080 | 2 | 4 | the real 1080p crop, performance |

Run your reader on each, then check it:

```
python3 test_yuv.py ../.cache/variants/pcm_200x120.ts out.yuv
```

What is compared:

- **Structure** (always): the file size equals frames x (W x H + 2 x ((W+1)/2) x ((H+1)/2)) for ffmpeg's cropped W x H and ffprobe's frame count. On a mismatch the harness says how many whole frames plus leftover bytes you wrote and, when it recognises the size, the likely cause (for example "exactly 3 uncropped 208x128 frames").
- **Pixels** (for the synthetic streams, or when you add `--pixels`; skipped while the file is not a whole number of frames): `out.yuv` must be byte-identical to `ffmpeg -fps_mode passthrough -f rawvideo -pix_fmt yuv420p`. For the synthetic streams the harness additionally checks that ffmpeg's output equals the generator's own raw samples, so the reference is trustworthy.

On any other file, such as T1, T2 or T4, only the structure is checked, because your reader cannot decode their prediction and residual yet:

```
python3 test_yuv.py ../.cache/T4.ts out.yuv
```

(Generate the T-files with `python3 -m common.testfiles T1 T2 T4` from the repository root.) Use `--structure-only` to skip the pixels on a synthetic stream. Exit code 0 is a pass, 1 a failure and 2 a missing or empty reader output. `--self-test` verifies the harness itself: correct output must pass, and a corrupted sample, swapped Cb and Cr, a truncated file and uncropped output must fail.

### The comparison tool you keep for all of Phase 5

Run it from the repository root:

```
python3 -m common.yuvcmp expected.yuv actual.yuv WIDTH HEIGHT [--origin X0 Y0] [--frames N]
```

WIDTH and HEIGHT are the frame size in the files (after cropping). To make the reference yourself: `ffmpeg -i in.ts -fps_mode passthrough -f rawvideo -pix_fmt yuv420p ref.yuv` (also cropped). The report contains:

- size problems: different frame counts, or a file that is not a whole number of frames (whole frames present in both files are still compared);
- the first difference: frame, plane (Y, Cb or Cr), macroblock (x, y) and its address, pixel (x, y) within that plane, and the expected and actual values;
- per differing frame (the first N, default 5): how many samples and macroblocks differ;
- a hint when it recognises a whole-plane mistake (Cb and Cr swapped; only one plane wrong);
- a map of the first bad frame, one character per macroblock, `#` = differs, `.` = identical. Wide pictures are shrunk, one cell standing for 2x2 or more macroblocks.

Pixel positions count from the top-left of the cropped file. Macroblocks are those of the decoded picture: luma pixel (x, y) is in macroblock ((X0 + x) / 16, (Y0 + y) / 16) and chroma pixel (cx, cy) in ((X0/2 + cx) / 8, (Y0/2 + cy) / 8). `--origin X0 Y0` gives the left and top crop in luma samples (3.4); leave it out when nothing is cropped on the left or top, which is the usual case (1080p crops only at the bottom). The harness passes it for you on the synthetic streams.

Exit code 0 means identical, 1 different, 2 a file is missing. From Python: `common.yuvcmp.compare_yuv(expected, actual, W, H, origin=(X0, Y0))` prints the same report and returns it as a dict.

## 7. Reading failures and debugging tips

- **"exactly N uncropped frames"**: you wrote the macroblock-aligned buffer instead of the cropped picture. Check 3.4.
- **File size off by a few bytes per frame**: wrong chroma plane size. It is (W+1)/2 x (H+1)/2, for example 99x59 for 198x118, not 100x60.
- **Pixel difference at the very first sample of a cropped stream** (pcm_100x60_lt): you copy from (0, 0) rather than from the crop offset.
- **Picture shifted or sheared by a few samples**: stride mistakes, usually using the cropped width as stride in the buffer, or writing the padded rows.
- **First bad macroblock is at an odd place on a multi-slice stream** (198x118 has 3 slices): `first_mb_in_slice` not applied, or a slice starting a new picture by mistake.
- **From one macroblock to the end of its slice everything is wrong, as if shifted by a few samples**: the I_PCM alignment. The sample data starts at the next byte boundary after `mb_type`, after 0-7 zero bits depending on the bit position; the next macroblock starts right after the 384th sample. Check that your bit reader's "align to byte" really skips to the boundary (and does nothing when already aligned).
- **Every macroblock wrong from luma pixel (1, 0) on, chroma right** (hint "only Y differs"): the 256 PCM luma samples are row by row; you stored them column by column.
- **Chroma wrong but luma right**: Cb and Cr swapped, or the chroma 8x8 blocks addressed with 16x16 numbers (use 8 x mbX).
- **Everything after some macroblock wrong**: your parser lost sync after a non-PCM macroblock in a real stream: that is a Phase 4 problem, not a writer problem.
- Print the yuvcmp macroblock map. A single `#` is a local bug; a whole column or row of `#` is an addressing bug.

## 8. What's next

Chapter 5.2 builds inverse quantisation and the inverse transforms, which turn the coefficients you already parse into residual samples. They are tested on small matrices first, since the frame buffer is not enough to check them.
