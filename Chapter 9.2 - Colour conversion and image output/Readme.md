# Chapter 9.2 - Colour conversion and image output

## 1. Goal and the project

Your decoder now writes correctly ordered planar YUV frames (9.1). Nobody can look at those directly. This chapter turns a frame into an ordinary image: **YCbCr 4:2:0 to RGB**, then a **PPM** file that any image viewer opens. You build a small stand-alone program that reads a decoded yuv420p file (the format of 5.1 / 9.1) and writes RGB images of the frames you choose. The harness compares them with a reference conversion, with a small tolerance, since the standard does not define this step bit-exactly and real converters differ in the last digit.

You are done when your PPMs agree with the reference on synthetic frames (flat colour patches, ramps, chroma edges, noise), on real decoded streams with different VUI colour tags (BT.601, BT.709, limited and full range), and when the harness can tell you *which* mistake you made if they do not.

## 2. Big picture

H.264 stores a picture as **Y** (luma, brightness) plus **Cb** and **Cr** (chroma, "blueness" and "redness" differences), because eyes resolve brightness better than colour: in 4:2:0 each chroma sample covers a 2x2 block of luma samples. A screen needs **R, G, B** for every pixel. So to display a frame you must

1. know **which YCbCr to RGB formula** the encoder assumed (the *matrix*: BT.601 for SD, BT.709 for HD),
2. know **which numeric range** the samples use (*limited*: black = 16, white = 235; or *full*: 0 to 255),
3. **upsample** chroma to one sample per pixel,
4. compute, **round and clamp** to 0..255, and write the result.

The decoding standard only *carries* the choices in the VUI (Annex E); it does not say how to convert. That is why this step is checked with tolerances and not byte-exactly.

## 3. Concepts in detail

### 3.1 Where the choices come from (VUI video_signal_type, E.1.1)

Inside the SPS VUI (parsed in Chapter 3.1) the optional block `video_signal_type_present_flag` carries:

| field | meaning |
| --- | --- |
| `video_full_range_flag` | 0: limited range, 1: full range. Absent block: **limited** |
| `colour_description_present_flag` | if 1, three 8-bit fields follow: `colour_primaries`, `transfer_characteristics`, `matrix_coefficients` |
| `matrix_coefficients` | which matrix (Table E-5): **1 = BT.709**, **5 = BT.601 (625-line, PAL)**, **6 = BT.601 (525-line, NTSC)**, 2 = unspecified, 0 = identity (RGB), 4 FCC, 7 SMPTE 240M, 9 and 10 BT.2020 |

(`colour_primaries` and `transfer_characteristics` do not affect the YCbCr to RGB matrix; ignore them. `chroma_loc_info_present_flag` and its two fields give the chroma siting, 3.5.)

**Unspecified** means `matrix_coefficients = 2`, no colour description, or no VUI at all (x264 writes none unless you ask). Value 0 (identity: the planes already hold G, B, R) is only allowed for 4:4:4 video, so it cannot occur in this book's 4:2:0 scope; treat it, and any value you do not support, as unspecified. Real players guess; this book uses one stated rule:

> **Unspecified: BT.601 if the (cropped) picture height is 576 lines or fewer, otherwise BT.709.**

(FFmpeg's own scaler assumes BT.601 for every unspecified size, so the harness never lets ffmpeg decide: it always passes the matrix explicitly.) Support at least 1, 5, 6 and the unspecified rule; the others (4, 7, 9) differ only in Kr and Kb (3.2), and the harness never tests them.

### 3.2 The conversion

Each matrix is defined by two numbers **Kr** and **Kb** (and Kg = 1 - Kr - Kb):

| matrix | Kr | Kb | R = Y + a Cr | B = Y + b Cb | G = Y - c Cb - d Cr |
| --- | --- | --- | --- | --- | --- |
| BT.601 | 0.299 | 0.114 | a = 1.4020 | b = 1.7720 | c = 0.34414, d = 0.71414 |
| BT.709 | 0.2126 | 0.0722 | a = 1.5748 | b = 1.8556 | c = 0.18732, d = 0.46812 |

Here Y, Cb, Cr are *normalised* (below): Y in 0..255 and Cb, Cr centred on 0 (roughly -128..127). In general a = 2(1-Kr), b = 2(1-Kb), c = b Kb / Kg, d = a Kr / Kg.

**Normalising by range:**

| | Y | Cb, Cr |
| --- | --- | --- |
| limited | (Y - 16) * 255 / 219 (1.16438) | (C - 128) * 255 / 224 (1.13839) |
| full | Y | C - 128 |

Limited range exists for historic reasons: Y has head-room 1..15 and 236..254, chroma is 16..240. Conversion stretches 16..235 to 0..255.

**Reference formula used by the harness**, in double precision: compute R, B from the table, then G = (Y - Kr R - Kb B) / Kg; each channel becomes `clamp(floor(v + 0.5), 0, 255)`. You may use integer arithmetic (for example 16.16 fixed point, or the classic BT.601 8.8 constants 298, 409, 100, 208, 516) as long as you round to nearest and clamp; the tolerance in section 6 allows +-1 to 2 steps. **Clamp is required**: legal-range input still yields values below 0 or above 255 (pure red limited is already close to the edge, and chroma noise goes beyond).

### 3.3 Worked examples

Limited range. Forward direction (RGB to YCbCr, to see where the numbers come from): pure red.

| | Y | Cb | Cr |
| --- | --- | --- | --- |
| BT.601 red | 81 | 90 | 240 |
| BT.709 red | 63 | 102 | 240 |
| white | 235 | 128 | 128 |
| black | 16 | 128 | 128 |

Decoding with the right matrix gives back (254,0,0) for BT.601 (81,90,240) (one off from 255 because 81 is the rounded Y), (255,1,0) for BT.709 (63,102,240), (255,255,255) for white and (0,0,0) for black, both matrices.

Decoding with the **wrong** matrix: the BT.601 sample (81,90,240) read as BT.709 gives (255, 24, 0): R clips, G becomes clearly positive. This "green creeps into reds" is the classic symptom of a BT.601/BT.709 mix-up. Grey is unaffected (Cb = Cr = 128): Y=180 gives (191,191,191) limited but (180,180,180) full - a range mix-up makes everything slightly too bright or too dull.

Step by step for BT.601 limited (81, 90, 240): Yn = 65 * 1.16438 = 75.68; Cbn = -38 * 1.13839 = -43.26; Crn = 112 * 1.13839 = 127.5. R = 75.68 + 1.402 * 127.5 = 254.4 -> 254. B = 75.68 + 1.772 * (-43.26) = -0.97 -> rounds to -1 -> clamp 0. G = (75.68 - 0.299 * 254.44 - 0.114 * (-0.97)) / 0.587 = -0.48 -> rounds to 0.

### 3.4 Chroma upsampling

The chroma planes are (W/2) x (H/2). For luma pixel (x, y) you need a Cb and Cr value.

* **Nearest** (the required method): use chroma sample (x >> 1, y >> 1). Each chroma sample is copied to its 2x2 pixels. Fast, blocky at colour edges.
* **Bilinear** (optional): interpolate between the neighbouring chroma samples. The result depends on where the chroma sample *sits* relative to luma (3.5). With "centre" siting, chroma sample (cx, cy) sits at luma position (2cx + 0.5, 2cy + 0.5), so luma pixel x uses chroma coordinate (x - 0.5) / 2: weights 3/4 and 1/4 between the two nearest samples, clamped at the picture edge; vertically the same. Compute in floating point or with a few extra fractional bits and round once at the end, after the RGB computation (do not round the interpolated chroma first).
* Better filters (bicubic, Lanczos) exist; they are not tested.

Upsample *chroma*, not RGB: converting once per 2x2 block and copying RGB gives the same result for nearest but is a trap for bilinear.

### 3.5 Chroma siting

Where chroma samples sit relative to luma is the "chroma location". H.264 default (`chroma_loc_info_present_flag = 0`, or `chroma_sample_loc_type` 0) is: horizontally at the *left* luma sample of the pair (co-sited, x = 2cx), vertically *halfway* between two rows (y = 2cy + 0.5). Type 1 is centred in both directions (x = 2cx + 0.5; JPEG/MPEG-1 style). For nearest upsampling siting is ignored. The harness offers both for bilinear: `bilinear_left` (type 0, horizontal weights 1 and 0 at even x, 1/2-1/2 at odd x) and `bilinear` (type 1).

### 3.6 PPM (P6)

The simplest image format (Netpbm): ASCII header, then raw bytes. Write exactly

```
P6\n<width> <height>\n255\n<width*height*3 bytes>
```

Rows top to bottom, pixels left to right, each pixel R, G, B (one byte each). Header: magic `P6`; width and height as decimal; maxval 255; one single whitespace byte after maxval; then the raster. The harness rejects maxval other than 255 (16-bit PPMs), a wrong raster size (truncated or trailing bytes), and a width/height that differs from the YUV frame. Viewers (`feh`, GIMP, ImageMagick, a browser through conversion) open them; for a quick look `ffmpeg -i frame.ppm frame.png`.

## 4. The project: exactly what to build

A command-line program (any language) that takes the decoded yuv420p file (the cropped, planar, 8-bit output of 5.1 / 9.1: for each frame the Y plane W x H, then Cb and Cr, each (W/2) x (H/2), no headers, frames back to back) and these parameters:

* width, height of the frames in the file,
* matrix (`bt709` or `bt601`), range (`limited` or `full`), upsample (`nearest`; optional `bilinear`, `bilinear_left`),
* a list of 0-based frame indices to dump, and an output directory.

For every listed frame it writes `<outdir>/frame_<NNNNN>.ppm` with NNNNN the 5-digit zero-padded frame index (`frame_00007.ppm`).

Also wire it into your decoder: read `matrix_coefficients` and `video_full_range_flag` from your VUI parser, apply the rule of 3.1 for unspecified, and let the command line override them. (Optional check: write one line `VUI <case> mc=<n|-> full=<0|1|->` per case, `-` when the VUI field is absent, and pass the file with `--vui`.)

The plan's verify step: compare frame N with ffmpeg's conversion of the same stream; this chapter's harness does it for you.

## 5. Harness input and output format

`python3 test_colour.py --make-variants DIR` writes into `DIR` (outside the chapter folder, use a temp dir):

* `cases.txt`, one line per case, order of keys fixed:
  `CASE <name> yuv=<file> width=<n> height=<n> frames=<i,j,..> matrix=<bt709|bt601> range=<limited|full> upsample=<nearest|bilinear|bilinear_left> expect=<formula|ffmpeg> [ts=<file>] [vui_mc=<n|->] [vui_full=<0|1|->] [optional=1]`
  Example: `CASE sd_bt709 yuv=sd_bt709.yuv width=352 height=288 frames=0,12,31 matrix=bt709 range=limited upsample=nearest expect=ffmpeg ts=sd_bt709.ts vui_mc=1 vui_full=0`
* the YUV files named there (paths relative to `DIR`) and, for stream-based cases, the `.ts` they were decoded from (so you can run your whole decoder on it and compare with `ffmpeg`'s YUV by 9.1).

Cases:

| case(s) | content | expected from |
| --- | --- | --- |
| `syn_709_limited`, `syn_709_full`, `syn_601_limited`, `syn_601_full` | synthetic 96x64, 4 frames: 0 = 96 flat 8x8 patches (Y, Cb, Cr extremes, red/green/blue/cyan/magenta/yellow/white/black as encoded by both matrices, random legal values), 1 = Y, Cb, Cr and diagonal ramps, 2 = chroma edges (steps, 1-sample stripes, checkerboard, diagonal), 3 = noise 0..255 in every plane (clamping) | the formula of 3.2 |
| `syn_bilinear_left` (BT.709 limited), `syn_bilinear_center` (BT.601 full, `upsample=bilinear`) | the same frames, optional (checked only if you produce them) | formula of 3.4 / 3.5 |
| `sd_untagged`, `sd_bt709`, `sd_bt601_full`, `sd_bt470bg` | 352x288 x264 streams (the first is the common T9 recipe, no colour tags; the others add `colormatrix=bt709`, `colormatrix=smpte170m:fullrange=on`, `colormatrix=bt470bg`). Untagged SD must come out as BT.601 limited | ffmpeg |
| `hd_untagged`, `hd_bt709_full` | 1280x720: untagged HD must come out as **BT.709**; the second is tagged BT.709 full range | ffmpeg |
| `sd_bt709_bilinear` | optional: frame 12 of `sd_bt709.yuv` with `upsample=bilinear_left` | ffmpeg bilinear |

`frames=` lists the frames you must write. `vui_mc` / `vui_full` are the values the stream's VUI carries (`-` = field absent); they feed the optional `--vui` check. A case whose output directory is missing is an error (exit 2) unless it is marked optional.

**What your program must write** for case `<name>`: `<OUT>/<name>/frame_<NNNNN>.ppm` for every index in `frames=`, NNNNN the 5-digit zero-padded 0-based frame index (`frame_00007.ppm`), each a P6 file exactly as in 3.6 with the case's width and height.

## 6. Running the harness

```
python3 test_colour.py --make-variants /tmp/c92
python3 test_colour.py /tmp/c92 --out /tmp/c92_out --reader 'myreader {yuv} {width} {height} {matrix} {range} {upsample} {frames} {outdir}'
python3 test_colour.py /tmp/c92 --out /tmp/c92_out [--case NAME ...] [--vui vui.txt]   # check existing PPMs
python3 test_colour.py --self-test
```

`--reader` runs your command once per case through the shell (stdin closed, 300 s limit). Before each run the harness creates `<OUT>/<case>` and substitutes: `{yuv}` the YUV path, `{width}` `{height}` decimal, `{matrix}` `bt709` or `bt601`, `{range}` `limited` or `full`, `{upsample}` `nearest`, `bilinear` or `bilinear_left`, `{frames}` the comma list from `cases.txt` (`0,12,31`), `{outdir}` = `<OUT>/<case>`, `{case}` the case name. Optional cases run only if the template contains `{upsample}`. Your program decides nothing itself: matrix and range come from the command line.

Exit 0 pass, 1 colours outside tolerance, 2 missing or malformed input (no output directory or frame file, invalid PPM, wrong PPM size, unreadable `cases.txt`).

`--vui FILE` (optional) checks one line per stream case, `VUI <case> mc=<n|-> full=<0|1|->`, from your own VUI parser; for an absent field you may write `-` or the default (`mc=2`, `full=0`).

**What it compares and the tolerance.** For each frame and each channel R, G, B the harness computes the maximum absolute error, the mean absolute error and the mean signed error (bias) of your PPM against the expected image. A frame passes when all three channels satisfy

| expected from | max abs | mean abs | \|bias\| |
| --- | --- | --- | --- |
| formula (any upsample) or ffmpeg, nearest | 2 | 0.6 | 0.25 |
| ffmpeg, bilinear | 4 | 1.0 | 0.4 |

Why these numbers: a correct converter differs from the reference only by rounding, so by at most 1 in a few pixels (16.16 fixed point and the classic BT.601 8.8 constants both pass). Truncating instead of rounding shifts every value by about -0.5, which the bias limit catches; wrong matrices and ranges are off by 10 and more, which the max limit catches. Bilinear through ffmpeg gets more room because swscale's interpolation rounds differently.

**The ffmpeg reference.** For stream cases the expected image is `scale=in_range=tv|pc:out_range=pc:in_color_matrix=bt709|bt601[:in_chroma_loc=left|center]:flags=neighbor+accurate_rnd+full_chroma_int` (`bilinear+...` for bilinear). It agrees with the formula of 3.2 to within 1 (the self-test re-measures this). Do not compare against ffmpeg's *default* scaler (the plan's `ref.png` command): it converts at half chroma resolution, is off by up to 3 with a bias, and blurs chroma edges; use it only for a "looks right" check. Synthetic cases use the formula because swscale wraps instead of clamping on the out-of-range noise of frame 3.

For stream-based cases the harness also re-derives matrix and range from the stream with ffprobe and the rule of 3.1 (matrix 1 -> BT.709, 5 and 6 -> BT.601, otherwise by height) and fails if `cases.txt` disagrees.

## 7. Reading failures

On the first failing frame of a case the harness prints the first pixel outside tolerance (position, YCbCr input, expected and actual RGB) and then tries alternative interpretations of the *same YUV* on a 96x16 window around it, singly and in pairs, and reports the one that reproduces your output (mean error <= 0.6):

* `looks like BT.601 where BT.709 was expected`: wrong matrix. Check the HD/SD rule (3.1) and that you map matrix_coefficients 5 and 6 to BT.601.
* `looks like full-range conversion where limited was expected` (or the reverse): everything slightly too contrasty or too flat; black not black. You forgot the 16 / 235 stretch, or applied it to full-range material.
* `Cb and Cr look swapped`: red and blue exchanged in hue (skin blue).
* `chroma looks shifted ... one row`: colour bleeds one row up or down, visible at the horizontal chroma edges of synthetic frame 2. Typical cause: planes at the wrong offsets (Cb plane starts at W*H, Cr at W*H + (W/2)*(H/2)) or `(y+1)/2`.
* `truncated instead of rounded`, `unclamped`, `B,G,R`, `upsampling looks like nearest where bilinear expected`.
* `no common mistake ...`: something else; start from the synthetic patch frame 0, whose 8x8 patches are flat, and compare one patch by hand with the numbers of 3.3.
* `invalid PPM`: the message names the header or size problem. Remember the newline after `255`, and that the raster is exactly W*H*3 bytes.
* All frames wrong by tiny, uniform amounts (bias): rounding. All frames wrong at luma edges: a stride bug.

## 8. What's next

9.3 puts these RGB frames on screen: a minimal native window (Win32, Cocoa or X11/Wayland) that blits the RGB buffer, with keys to pause and step one frame; each stepped frame must match the PPM you write here. Timing from PTS follows in 9.4.
