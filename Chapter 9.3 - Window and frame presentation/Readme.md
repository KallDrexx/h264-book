# Chapter 9.3 - Window and frame presentation

## 1. Goal and the project

You can now turn a decoded frame into RGB (9.2). This chapter puts those pixels on a screen: a **minimal native window** that shows one RGB buffer, with keys to **pause** and **step one frame**. It is the one place in the book where you call an operating-system API directly (Win32, Cocoa, X11 or Wayland). The OS part is small. The part that goes wrong is everything around it: pixel byte order, row stride, upside-down images, wrong aspect ratio, a pause that does not freeze, a step that skips a frame.

A test program cannot open a window (and must never hang), so the chapter is built in two layers:

1. **The presenter** (pure logic, machine-checked): aspect ratio, fit-to-window, pixel format, stride, origin, and the pause/step/resize state machine. In *headless mode* your player writes a **presentation log** and **dumps every buffer it would have handed to the OS**. The harness checks them.
2. **The window** (OS glue, manual checklist in section 7): a thin layer that takes exactly the buffer the presenter built and blits it.

If layer 1 is right, layer 2 is a few dozen lines per platform.

## 2. Big picture

```
decoder -> output-order frames -> 9.2 conversion -> native RGB picture (W x H)
        -> presenter:  display size (SAR) -> fit into window -> scale -> pack (format, stride, origin)
        -> OS blit (StretchDIBits / CGImage / XPutImage / wl_shm)
events: key, resize, expose, close  -> presenter state machine (play / paused, step)
```

Two rules keep the design simple. **The presenter owns the pixels**: it hands the OS a buffer that is already window-sized, correctly padded and correctly ordered, so the OS never scales or reinterprets anything. **Events are data**: keys, resizes and exposes are turned into abstract events (`pause`, `step`, `resize WxH` ...) and fed to a state machine. The same state machine is driven by a script file in headless mode, which is what makes it testable.

## 3. Concepts in detail

### 3.1 Pixel buffers: format, stride, origin

A buffer is a block of bytes holding `height` rows. Three properties describe it.

**Format** (memory order of the bytes of one pixel, lowest address first):

| name | bytes/pixel | memory order | typical use |
| --- | --- | --- | --- |
| `bgrx` | 4 | B, G, R, unused | Windows 32-bit bitmap; X11 depth-24 image on a little-endian machine; Wayland `XRGB8888` |
| `bgra` | 4 | B, G, R, alpha | macOS with `kCGBitmapByteOrder32Little` + alpha first; Wayland `ARGB8888` |
| `rgbx` / `rgba` | 4 | R, G, B, unused / alpha | macOS CGImage with `kCGImageAlphaNoneSkipLast` (default byte order); Wayland `XBGR8888` / `ABGR8888` |
| `rgb24` | 3 | R, G, B | PPM raster, some toolkits |
| `bgr24` | 3 | B, G, R | Windows 24-bit bitmap |

Beware of names that describe a 32-bit *word* (Wayland's `XRGB8888`, X11 masks): on a little-endian CPU the word `0xXXRRGGBB` is stored as bytes B, G, R, X, i.e. `bgrx`. This book always names the **byte order in memory**. For `*a` formats write alpha = 255 everywhere; for `*x` formats the fourth byte may hold anything.

(A Windows *DIB*, device-independent bitmap, is a pixel buffer plus a small header describing it; 3.6.)

**Stride** (also pitch, bytes per row) is the distance from the start of one row to the start of the next. It is at least `width * bytes_per_pixel` and is usually **rounded up** to a multiple of an alignment `A` (4 for Windows DIBs, 16 or 64 for some macOS and GPU paths):

```
stride = ceil(width * bytes_per_pixel / A) * A
```

The extra bytes at the end of each row are **padding**; their value does not matter. Row y starts at `y * stride`, never at `y * width * bpp`. With 4-byte pixels and A = 4 the padding is always zero (Windows writes the same rule as `((width*bits_per_pixel + 31) / 32) * 4`), which is why stride bugs only show up with 3-byte pixels, odd widths or A = 16.

**Origin**: `topdown` stores the top picture row first (normal for X11, Wayland, Cocoa, PPM). `bottomup` stores the bottom row first. Windows DIBs are bottom-up by default; a **negative `biHeight`** in the header declares top-down. Whichever you pick, the log must say which one you used.

### 3.2 Aspect ratio

H.264 pixels need not be square. The VUI (E.1.1) can carry `aspect_ratio_info_present_flag`; if 1, an 8-bit **`aspect_ratio_idc`** follows, and when it is 255 (`Extended_SAR`) two 16-bit fields `sar_width`, `sar_height` follow. The **sample aspect ratio** (SAR) is the width:height of one *pixel*. Table E-1:

| idc | SAR | idc | SAR | idc | SAR |
| --- | --- | --- | --- | --- | --- |
| 0 | unspecified | 6 | 24:11 | 12 | 64:33 |
| 1 | 1:1 | 7 | 20:11 | 13 | 160:99 |
| 2 | 12:11 | 8 | 32:11 | 14 | 4:3 |
| 3 | 10:11 | 9 | 80:33 | 15 | 3:2 |
| 4 | 16:11 | 10 | 18:11 | 16 | 2:1 |
| 5 | 40:33 | 11 | 15:11 | 17-254 | reserved |
| | | | | 255 | `sar_width`:`sar_height` |

This book's rule: **idc 0, no VUI, reserved values 17-254, and an extended SAR with a zero field all mean 1:1.** The SAR applies to the *cropped* output picture (9.1). The **display size** is

```
display_width  = round(width * SAR_w / SAR_h)    (nearest, halves up: (2*width*SAR_w + SAR_h) / (2*SAR_h), integer division)
display_height = height
```

Example: 352x288 with SAR 16:11 -> 352 * 16 / 11 = 512, display 512x288 (ratio 16:9, which ffprobe reports as `display_aspect_ratio=16:9`, with `sample_aspect_ratio=16:11`). Example: 64x48 with idc 255, extended 7:5 -> 64 * 7 / 5 = 89.6 -> 90x48.

### 3.3 Fitting into the window

The window can have any size. You show the picture as large as possible without distortion, centred, with black bars (**letterbox**: bars above and below; **pillarbox**: left and right). With window `ww x wh` and display `dw x dh` (all integer arithmetic, `/` = integer division):

```
if ww*dh <= wh*dw:   (width limited)   rw = ww;   rh = (2*dh*ww + dw) / (2*dw)
else:                (height limited)  rh = wh;   rw = (2*dw*wh + dh) / (2*dh)
rw = clamp(rw, 1, ww);  rh = clamp(rh, 1, wh)
rx = (ww - rw) / 2;     ry = (wh - rh) / 2          (floor)
```

Example: 64x48 picture, SAR 16:11, window 200x100. Display = 64*16/11 = 93.09 -> 93x48. Is 200*48 = 9600 <= 100*93 = 9300? No: height limited. rh = 100, rw = (2*93*100 + 48) / 96 = 18648/96 = 194. rx = (200-194)/2 = 3, ry = 0. The rect is (3, 0, 194, 100).

**Scaling** is nearest neighbour from the native picture (`sw x sh`) into the rect. For rect pixel column i (0-based) and row j:

```
src_x = ((2*i + 1) * sw) / (2*rw)      src_y = ((2*j + 1) * sh) / (2*rh)       (integer division, pixel-centre mapping)
```

Everything outside the rect is black (0,0,0). Real players use bilinear or the GPU; nearest is the book's definition because it can be checked exactly.

### 3.4 The event loop

A GUI program is a loop that waits for events and reacts. Every platform has one: Win32 `GetMessage`/`DispatchMessage`, Cocoa `NSApplication` run loop, X11 `XNextEvent`, Wayland `wl_display_dispatch`. Events you care about:

| OS event | abstract event |
| --- | --- |
| space (or `p`) | `toggle` (pause <-> play) |
| `.` or right arrow | `step` |
| Esc, `q`, window close | `quit` |
| window resized | `resize WxH` |
| window needs repainting (Win32 `WM_PAINT`, X11 `Expose`, Cocoa `drawRect:`, Wayland frame callback / configure) | `expose` |

Rules of thumb:

* **Never block the event loop** with decoding or sleeping. Either decode on a second thread and hand finished frames over (a small queue), or decode one frame per loop iteration and wait for events with a timeout. A window that stops answering events is reported "not responding" by the OS.
* **The window can ask for a repaint at any time** (uncovered, un-minimised, resized). Keep the **last presented frame** so you can redraw it without decoding. This is why paused video still repaints.
* **Double buffering**: draw into your own buffer, then blit it in one call, so the user never sees a half-drawn picture. Waiting for the display's refresh (vsync) avoids tearing; timing is 9.4's job, here you just present when told.
* Minimised windows may report size 0x0: skip presenting while a dimension is 0.

### 3.5 The pause / step state machine

State: `next` (index of the next frame to show, output order from 9.1), `paused` (starts false), `last` (the last frame shown, none at start), `window`.

| event | effect |
| --- | --- |
| (playing, no event pending) | show frame `next` (reason `play`), `next += 1`, until the stream ends |
| `pause` | `paused = true`. Nothing is shown or consumed while paused |
| `resume` | `paused = false` |
| `toggle` | flips `paused` |
| `step` | if paused and frames remain: show frame `next` (reason `step`), `next += 1`. **While playing, or at the end of the stream, `step` does nothing** |
| `resize WxH` | set the window size; if `last` exists, show frame `last` again at the new size (reason `redraw`); `next` unchanged |
| `expose` | if `last` exists, show frame `last` again (reason `redraw`) |
| `quit` | stop immediately |

Each "show" is one **present**. Frames are shown strictly in order, each exactly once as a `play` or `step`; a `redraw` repeats the previous frame and never advances.

### 3.6 Per-OS pointers

**Windows (Win32).** Register a window class, `CreateWindowEx`, loop on `GetMessage`. In `WM_PAINT` call `BeginPaint`, then `StretchDIBits(hdc, 0,0,ww,wh, 0,0,ww,wh, pixels, &bitmapinfo, DIB_RGB_COLORS, SRCCOPY)` (or `SetDIBitsToDevice`, since your buffer is already window-sized), then `EndPaint`. `BITMAPINFOHEADER`: `biSize=40`, `biWidth=ww`, **`biHeight=-wh` for top-down** (positive = bottom-up), `biPlanes=1`, `biBitCount=32` (or 24), `biCompression=BI_RGB`. Pitfalls: **a DIB row must be a multiple of 4 bytes** (24-bit: `((ww*3 + 3) & ~3)`); pixels are B, G, R (`bgrx`); with a positive height the image is upside down; handle `WM_SIZE` (size in `lParam`) and `WM_ERASEBKGND` (return 1 to avoid flicker); `InvalidateRect` to request a repaint.

**macOS (Cocoa / CoreGraphics).** Create an `NSWindow` with a custom `NSView` and implement `drawRect:`. Wrap your buffer in a `CGDataProvider`, make a `CGImage` with `CGImageCreate` (width, height, 8 bits per component, 32 bits per pixel, `bytesPerRow` = your stride, `CGColorSpaceCreateDeviceRGB`, bitmap info `kCGImageAlphaNoneSkipLast` for `rgbx` or `kCGBitmapByteOrder32Little | kCGImageAlphaNoneSkipFirst` for `bgrx`), then `CGContextDrawImage` into the current context. Pitfalls: CoreGraphics' origin is bottom-left (a `CGImage` itself is read top-down, but drawing into a flipped view needs care); `bytesPerRow` must be what you actually used; call `setNeedsDisplay:` to repaint; the UI must run on the main thread, so decode on another thread. Alternative: a `CGBitmapContext` over your buffer and `CGBitmapContextCreateImage`.

**Linux, X11.** `XOpenDisplay`, `XCreateSimpleWindow`, `XSelectInput` (`ExposureMask | KeyPressMask | StructureNotifyMask`), `XMapWindow`, loop on `XNextEvent`. Create an `XImage` with `XCreateImage(display, visual, depth, ZPixmap, 0, data, ww, wh, 32, bytes_per_line)` and draw it with `XPutImage` on `Expose`. Pitfalls: a **depth-24 visual almost always stores 32 bits per pixel** (the server's pixmap format, `XListPixmapFormats`; pass `bitmap_pad` 32); with the usual masks (`red_mask` 0xff0000) and a little-endian image byte order (`ImageByteOrder(display)` = `LSBFirst`) the memory layout is B, G, R, unused (`bgrx`); check both instead of assuming; `XDestroyImage` calls `free()` on your data, so allocate it with `malloc` (or set `image->data = NULL` first); `bytes_per_line` is your stride; `ConfigureNotify` carries the new size, and **re-create the XImage and buffer when it changes**. `XShmPutImage` (MIT-SHM) avoids a copy through the X socket but is only an optimisation; XPutImage is enough.

**Linux, Wayland.** Connect with `wl_display_connect`, bind `wl_compositor`, `wl_shm` and `xdg_wm_base`, create a surface and an `xdg_toplevel`. Allocate a shared-memory file (`memfd_create` + `mmap`), make a `wl_shm_pool`, then a `wl_buffer` with `wl_shm_pool_create_buffer(pool, offset, width, height, stride, WL_SHM_FORMAT_XRGB8888)` (stride in bytes, normally `width * 4`), write your pixels into the mapping, `wl_surface_attach`, `wl_surface_damage`, `wl_surface_commit`. Pitfalls: XRGB8888 is the little-endian *word* `0xXXRRGGBB`, so in memory it is `bgrx` (ARGB8888: `bgra`); only these two formats are guaranteed by every compositor, so convert to `bgrx` rather than relying on `XBGR8888`; first commit the surface *without* a buffer, wait for `xdg_surface.configure` and answer it with `ack_configure`, and only then attach a buffer; the compositor tells you the size in `xdg_toplevel.configure` and you re-create the buffer; do not write into a buffer the compositor still holds (wait for `wl_buffer.release`, or use two buffers).

## 4. The project: exactly what to build

Build a **player** (your own decoder plus the presenter and window) with two modes:

* **Window mode**: opens the window of section 3.6 and plays a file, with keys from 3.4.
* **Headless mode**: no window, no keyboard. A **script** replays events, a **log** records every present and **dump files** hold every buffer. This is what the harness runs.

For the harness the frame source is a decoded `yuv420p` file (the cropped output of 9.1, frames in output order), exactly like 9.2, together with the colour matrix, range and aspect ratio on the command line. (In your real player the same values come from your decoder and VUI parser; wire both in.) Build in this order:

1. **Aspect ratio**: Table E-1 and the display size of 3.2. Optional but recommended: read `aspect_ratio_idc` and the extended SAR from your VUI parser and check against `ffprobe -show_streams` (`sample_aspect_ratio`, `display_aspect_ratio`).
2. **Pack**: the six formats, stride and origin of 3.1.
3. **Fit and scale**: 3.3.
4. **State machine**: 3.5, driven by a script. Test it before any window exists.
5. **Window glue** for your platform: on each `expose` or `resize`, rebuild the buffer at the window size and blit it; map keys as in 3.4.

Plan's verify step: step through T9 frame by frame, each frame matches the PPM from 9.2. The harness automates it with synthetic frames (frame number visible in the picture) and two real x264 streams.

## 5. Harness input and output format

### 5.1 Command-line contract (headless mode)

Your player must accept equivalents of these options (the harness passes them through a command template, so the names are yours to choose; the reference uses these):

```
player INPUT.yuv --size WxH --aspect-idc N --sar-ext W:H --matrix bt709|bt601 --range limited|full
       --window auto|WxH --format bgrx|bgra|rgbx|rgba|rgb24|bgr24 --align A --origin topdown|bottomup
       --script FILE --log FILE --dump-presented DIR
```

`--window auto` = initial window equals the display size of 3.2. `--sar-ext` is only meaningful when `--aspect-idc 255`. The player must **exit by itself** (it plays to the end, or until `quit`, or until the script is exhausted while paused) and **never wait for real input**.

### 5.2 Script grammar

One event per line; `#` starts a comment; blank lines are ignored.

```
line  := "after" SP K SP event [SP "WxH"]
event := pause | resume | toggle | step | resize | expose | quit
```

`K` is the number of new frames shown so far (frames 0..K-1 are on screen; redraws do not count). The player runs in play mode until exactly K frames are shown (nothing to do if already there), then applies the event. Lines are in file order and K never decreases; several lines with the same K are applied one after the other, with no frame played in between. Only `resize` takes an argument (`resize 201x75`). Valid scripts only: the harness never generates one that cannot be followed (for example `after 5 ...` while paused at frame 3). When the script is exhausted: if playing, play to the end of the stream; if paused, exit.

Example:

```
after 2 pause      # frames 0,1 played; now paused
after 2 step       # shows frame 2
after 3 step       # shows frame 3
after 4 resume     # plays 4, 5 ...
after 6 quit
```

### 5.3 Log grammar (`--log FILE`, ASCII, `\n` line ends, one line per record)

```
HEADER src=<W>x<H> frames=<N> sar=<n>:<d> display=<DW>x<DH> format=<fmt> align=<A> origin=<topdown|bottomup>
PRESENT <idx> frame=<f> reason=<play|step|redraw> window=<WW>x<WH> rect=<x>,<y>,<w>,<h> format=<fmt> stride=<bytes> origin=<topdown|bottomup>
...
END presents=<count> reason=<quit|eos|script_end>
```

* `idx`: 0-based, consecutive, counts every present including redraws. `frame`: 0-based output-order index of the frame shown. `window`: the window size at that moment (the size of the buffer). `rect`: where the picture sits inside the window (3.3). `stride`: bytes per row of the buffer handed to the OS.
* `frames`: number of frames in the input YUV file. `align`: the `--align` value.
* `sar` is the SAR after the Table E-1 rule (`1:1` for unspecified); `display` is 3.2.
* `END reason`: `quit` (a `quit` event), `eos` (all frames shown, not paused), `script_end` (script exhausted while paused).

Worked log for the script above (64x48, SAR 1:1, window 128x96, `bgrx`, A = 4, 8 frames):

```
HEADER src=64x48 frames=8 sar=1:1 display=64x48 format=bgrx align=4 origin=topdown
PRESENT 0 frame=0 reason=play window=128x96 rect=0,0,128,96 format=bgrx stride=512 origin=topdown
PRESENT 1 frame=1 reason=play ...
PRESENT 2 frame=2 reason=step ...
PRESENT 3 frame=3 reason=step ...
PRESENT 4 frame=4 reason=play ...
PRESENT 5 frame=5 reason=play ...
END presents=6 reason=quit
```

### 5.4 Dump files (`--dump-presented DIR`)

For present number `idx` write two files, `present_<idx:05d>.ppm` and `present_<idx:05d>.raw` (for example `present_00007.ppm`):

* `.ppm`: the presented picture as a P6 PPM exactly as in 9.2, **window-sized**: scaled, with black bars. Always RGB, top-down, no padding, whatever the native format is.
* `.raw`: the **exact bytes of the buffer you hand to the OS**: `stride * window_height` bytes, pixel format, stride and origin as logged.

### 5.5 Cases (`python3 test_present.py --make-variants DIR`)

Writes `DIR/cases.txt` (one line per case; fixed key order, example below), `<case>.script`, `<case>.expected.log` (what a correct player logs; diff against yours), the YUV files and two `.ts` streams.

```
CASE pause_step yuv=syn.yuv size=64x48 aspect_idc=1 sar_ext=1:1 matrix=bt601 range=limited window=128x96 format=bgrx align=4 origin=topdown script=pause_step.script expect=formula
```

| cases | what they test |
| --- | --- |
| `play_all` | 8 synthetic frames, no events; the frame number is visible in the picture |
| `pause_step`, `step_from_start` | pause freezes, step shows exactly the next frame, step while playing and at end of stream is ignored, toggle |
| `resize_redraw` | expose before any frame, resize and expose while paused and while playing (redraw of the same frame, never an advance), odd and extreme window sizes |
| `fmt_rgb24_odd`, `fmt_bgra_bottomup`, `fmt_rgbx_a16`, `fmt_bgr24_bottomup` | formats, strides (e.g. 99*3 = 297 -> 300), alignment 16, bottom-up |
| `sar_16_11`, `sar_ext_7_5`, `sar_narrow_10_11` | non-square pixels, fit with pillarbox / letterbox |
| `sar_idc_0` ... `sar_idc_17`, `sar_idc_255`, `sar_idc_255_zero` | every Table E-1 row on a 16x16 picture, reserved 17, extended SAR, extended SAR with zero fields |
| `st_sar_16_11`, `st_sar_ext_7_5` | real x264 streams 176x144 with `setsar` 16:11 (idc 4) and 7:5 (idc 255), decoded by ffmpeg (`expect=ffmpeg`), SAR checked against ffprobe |

## 6. Running the harness

```
python3 test_present.py --make-variants /tmp/c93
python3 test_present.py /tmp/c93 --out /tmp/c93_out --reader 'myplayer {yuv} --size {size} --aspect-idc {aspect_idc} --sar-ext {sar_ext} --matrix {matrix} --range {range} --window {window} --format {format} --align {align} --origin {origin} --script {script} --log {log} --dump-presented {dumpdir}'
python3 test_present.py /tmp/c93 --out /tmp/c93_out [--case NAME ...]     # check outputs you already produced
python3 test_present.py --self-test
```

Placeholders (all substituted per case; paths shell-quoted): `{yuv}` `{size}` (`WxH`) `{width}` `{height}` `{aspect_idc}` `{sar_ext}` `{matrix}` `{range}` `{window}` (`auto` or `WxH`) `{format}` `{align}` `{origin}` `{script}` `{log}` `{dumpdir}` `{ts}` (the stream, for players that decode it themselves) `{case}`. The harness runs your command through the shell with stdin closed and a 120 s limit (the whole process group is killed on timeout). It writes `OUT/<case>/log.txt` and `OUT/<case>/dump/` paths for you to use.

**What is compared**

1. **Log** against the simulation of section 3.5: HEADER fields, the number of PRESENT lines, and for each one `frame`, `reason`, `window`, `rect`, `format`, `stride` (= formula of 3.1), `origin`, and the END line. Mismatches name the script line that governs them.
2. **Sequence invariants** on your log: new frames are consecutive (no skip, no repeat), a redraw repeats the last frame.
3. **Aspect ratio**: `sar` and `display` against Table E-1; for the stream cases also against ffprobe's `sample_aspect_ratio` and `display_aspect_ratio`.
4. **Raw buffer**: `len(.raw) == stride * window_height`; decoding it with the logged format, stride and origin must give exactly the `.ppm` pixels; alpha = 255 for `bgra`/`rgba`.
5. **Picture**: the `.ppm` must be window-sized; outside `rect` every pixel is exactly (0,0,0); inside `rect` it is compared with the expected picture, which is the 9.2 conversion of the decoded frame (formula for synthetic cases; ffmpeg `scale` with `neighbor+accurate_rnd+full_chroma_int` for stream cases, as in 9.2) scaled with the nearest rule of 3.3. Tolerance, same as 9.2 (per channel R, G, B): **max abs error <= 2, mean abs error <= 0.6, |mean signed error| <= 0.25**. A correct 16.16 fixed-point converter differs only by rounding (at most 1).

If the log is wrong, image checks for that case are skipped (the dump files are matched by present index, so they would be meaningless).

Exit codes: **0** pass, **1** wrong behaviour or wrong pixels, **2** missing or malformed input (no log, bad log line, missing dump file, invalid PPM, PPM not window-sized, unreadable cases).

## 7. Optional manual checklist (not machine-checked)

Run your real window and tick off:

- [ ] The picture appears with correct colours, upright, left-right correct (the synthetic frames have a bright box top-left).
- [ ] Resize larger and smaller: the picture refits with black bars, no distortion, no crash, no flicker.
- [ ] Drag another window over it, then away: it repaints (expose) without advancing a frame.
- [ ] Minimise and restore: no crash, playback resumes (or stays paused).
- [ ] While paused, the window still answers: move it, resize it, repaint; `step` shows exactly one new frame per key press.
- [ ] Hold the step key: frames advance one per repeat, none skipped.
- [ ] Space pauses, space resumes; Esc, `q` and the close button all exit cleanly, with no process left running.
- [ ] An SD 16:11 stream looks 16:9, not squeezed.

## 8. Reading failures

* `stride expected 300, got 297`: you forgot to round up (3.1). With `rgb24` and an odd width, only the stride tells.
* `.raw is N bytes, expected stride*height`: padding missing, or you wrote padding but logged an unpadded stride. `DIAGNOSIS: raw rows are stored bottomup`: you flipped the rows but logged the other origin.
* `DIAGNOSIS: red and blue are swapped`: the memory order of `bgrx` is B, G, R.
* `DIAGNOSIS: the picture is stretched` / `SAR applied inverted` / `top-left aligned`: your `rect` is wrong; recompute 3.2 and 3.3 by hand with the examples.
* `frame expected 3, got 4` with `[script line 2: 'after 2 step']`: the event on that line did the wrong thing (step skipped, pause ignored, resize advanced).
* `frames must be consecutive`: you skipped or repeated a frame.
* `PPM is 64x48, the window was 90x48`: the dump must be the window-sized picture, not the native one.
* A scaling off by half a pixel passes in flat areas and fails on edges: use the centre mapping of 3.3.

## 9. What's next

9.4 adds **timing**: each picture carries the PES PTS of its access unit, and the player presents it when the wall clock reaches PTS minus the first PTS, handling the 33-bit wrap and dropping frames when decoding falls behind. The `play` present of this chapter will then wait for its time instead of coming immediately.
