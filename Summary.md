# Build an H.264 decoder from scratch: step-by-step plan

Nov 18, 2026 · @Matthew Shapiro

## How to use this plan

The plan is 48 steps in 10 phases, each sized for 1–2 days, so expect roughly 70–90 working days in total. Milestones: first decoded image at the end of Phase 5, first watchable (Baseline) video at the end of Phase 7, most real-world `.ts` files at the end of Phase 8, live playback in Phase 9.

**The golden rule: H.264 decoding is bit-exact.** Every conforming decoder produces identical output samples. So "verified" always means your YUV output is byte-identical to a reference decoder's, not that it looks right. When a frame differs, the first differing macroblock tells you exactly where your bug is.

**Scope.** The target is 8-bit 4:2:0 video in Constrained Baseline, Main and High profiles, progressive first, interlaced last. That covers nearly all broadcast, IPTV and HLS `.ts` files. Left out: High 10 / 4:2:2 / 4:4:4, SVC/MVC, Extended profile (SP/SI slices, data partitioning) and FMO/ASO.

**Specs (free downloads).** [ITU-T H.264](https://www.itu.int/rec/T-REC-H.264) for the codec (section numbers below refer to it), [ITU-T H.222.0](https://www.itu.int/rec/T-REC-H.222.0) for MPEG-TS, and [ITU-T H.264.1](https://www.itu.int/rec/T-REC-H.264.1) for the conformance bitstreams, each shipped with its expected output.

### Reference tools (run beside your decoder, never linked into it)

| Tool | Command | What it checks |
| --- | --- | --- |
| ffprobe packets | `ffprobe -v error -select_streams v:0 -show_packets -show_entries packet=pts,dts,size,flags -of csv in.ts` | PES sizes, PTS/DTS (Phase 1) |
| ffprobe frames | `ffprobe -v error -select_streams v:0 -show_frames -show_entries frame=pict_type,pts -of csv in.ts` | Frame types in display order (Phase 3, 9) |
| trace\_headers | `ffmpeg -i in.ts -c:v copy -bsf:v trace_headers -f null - 2> headers.txt` | Every NAL, SPS, PPS and slice-header field with its value (Phases 2–3) |
| Raw elementary stream | `ffmpeg -i in.ts -c:v copy -f h264 ref.264` | Your demuxer output (Phase 1) |
| Reference YUV | `ffmpeg -i in.ts -fps_mode passthrough -f rawvideo -pix_fmt yuv420p ref.yuv` | Pixel output, bit-exact (Phase 5 on) |
| Raw YUV viewer | `ffplay -f rawvideo -pixel_format yuv420p -video_size 352x288 out.yuv` | Eyeballing your output before you have a display |
| JM reference decoder | build `ldecod` with `TRACE` set to 1 in `defines.h` → `trace_dec.txt` | Every macroblock-level syntax element and its bit position (Phases 4, 7, 8) |

### Build these yourself on day one

- **yuvcmp**: takes two YUV files plus width and height, and reports the first differing frame, plane, macroblock (x, y) and pixel. You will use it hundreds of times.
- **A debug dump flag** in your decoder that prints per-macroblock `mb_type`, QP, CBP and motion vectors in a format you can diff against the JM trace.

### Test-file recipes

All use `ffmpeg -f lavfi -i testsrc2=size=352x288:rate=25 -t 2 -pix_fmt yuv420p -c:v libx264 -qp 26 <options> -f mpegts out.ts`. Swap the `testsrc2` input for a real camera clip scaled to 352x288 to get natural motion. Each step names the recipe it needs.

| ID | Exercises | `<options>` |
| --- | --- | --- |
| T1 | I16x16 only, CAVLC, no deblocking | `-profile:v baseline -x264-params keyint=1:partitions=none:no-deblock=1` |
| T2 | Adds Intra 4x4 | `-profile:v baseline -x264-params keyint=1:partitions=i4x4:no-deblock=1` |
| T3 | Intra + deblocking (also try `deblock=-3,-3` and `deblock=3,3`) | `-profile:v baseline -x264-params keyint=1:partitions=i4x4` |
| T4 | Multiple slices per picture | T3 + `:slices=4` |
| T5 | P 16x16 only, 1 reference, no deblocking | `-profile:v baseline -bf 0 -x264-params keyint=50:partitions=none:ref=1:no-deblock=1` |
| T6 | All P partitions, 4 references, deblocking | `-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=4` |
| T7 | Explicit weighted P (add `-vf fade=in:0:25`) | `-profile:v main -x264-params cabac=0:bframes=0:weightp=2` |
| T8 | B frames, no direct mode | `-profile:v main -x264-params cabac=0:bframes=2:b-pyramid=none:direct=none:weightb=0` |
| T9 | Direct spatial / temporal, implicit weights, B pyramid | `-profile:v main -x264-params cabac=0:bframes=3:direct=spatial:weightb=1:b-pyramid=normal` (then `direct=temporal`) |
| T10 | CABAC (I-only, then full Main) | T2 options with `-profile:v main` and `cabac=1`; then `-profile:v main` alone |
| T11 | High profile: 8x8 transform, scaling matrices | `-profile:v high` alone; then `-profile:v high -x264-params cqm=jvt` |
| T12 | MBAFF interlaced | `-profile:v high -x264-params interlaced=1:tff=1` |
| Real | Everything | Broadcast/IPTV recordings, HLS `.ts` segments, H.264.1 conformance streams wrapped with `ffmpeg -i X.264 -c copy -f mpegts X.ts` |

## Phase 1: MPEG-TS demuxing (H.222.0)

Phase 1 ends with you extracting a raw H.264 elementary stream byte-identical to ffmpeg's, plus a PTS/DTS for every access unit. Use any real `.ts` file.

**1.1 TS packet reader** (1 day)

- Build: read 188-byte packets and check the 0x47 sync byte (resync by scanning if it is missing). Parse the 4-byte header: transport\_error\_indicator, payload\_unit\_start\_indicator (PUSI), the 13-bit PID, adaptation\_field\_control and continuity\_counter. Skip the adaptation field by its length byte; optionally decode the PCR.
- Verify: packet count equals file size / 188. Print a per-PID packet histogram. A clean file shows no continuity-counter errors except on adaptation-only packets. Note that `.m2ts` files use 192-byte packets with a 4-byte timestamp prefix.

**1.2 PSI tables: PAT and PMT** (1 day)

- Build: on PID 0, honour pointer\_field, reassemble sections that span packets and parse the PAT to find the PMT PID. Parse the PMT and find the elementary stream with stream\_type 0x1B (H.264). Implement CRC-32/MPEG-2 (polynomial 0x04C11DB7, not reflected) and check every section.
- Verify: every section's CRC passes, which proves your section reassembly. Your program number, PMT PID and video PID match `ffprobe -show_programs in.ts`.

**1.3 PES reassembly and timestamps** (1 day)

- Build: for the video PID, collect payloads from one PUSI to the next. Parse the PES header: 00 00 01 prefix, stream\_id 0xE0–0xEF, PES\_packet\_length (often 0 for video, meaning unbounded), PTS\_DTS\_flags and header length. Decode the 33-bit PTS and DTS (90 kHz clock).
- Verify: your list of (PTS, DTS, payload size) matches the ffprobe packets command row for row, in the same 90 kHz units.

**1.4 Elementary stream output** (half day)

- Build: write the concatenated PES payloads to `out.264`. Keep the PTS/DTS list alongside for Phase 9.
- Verify: `cmp out.264 ref.264` reports no difference. `ffplay out.264` plays the video.

## Phase 2: NAL units and bit reading (Annex B, 7.3.1, 9.1)

Phase 2 ends with a list of NAL units, their RBSP payloads and a bit reader you trust. Use any real `.ts` file plus `headers.txt` from trace\_headers.

**2.1 Start-code scanner and NAL header** (1 day)

- Build: split the byte stream on 00 00 01 and 00 00 00 01 start codes. Parse the 1-byte NAL header: forbidden\_zero\_bit, nal\_ref\_idc and nal\_unit\_type (1 non-IDR slice, 5 IDR slice, 6 SEI, 7 SPS, 8 PPS, 9 access unit delimiter).
- Verify: your count of each nal\_unit\_type, in order, matches the NAL list in `headers.txt`.

**2.2 Emulation prevention and bit reader** (1–2 days)

- Build: convert NAL to RBSP by dropping the 03 in every 00 00 03 sequence. Write a bit reader with u(n), ue(v), se(v) (9.1), byte\_aligned(), more\_rbsp\_data() and the rbsp\_trailing\_bits check.
- Verify: unit-test Exp-Golomb against Table 9-2 (bits 1 → 0, 010 → 1, 011 → 2, 00100 → 3) and the se(v) mapping (1 → +1, 2 → −1). Smoke test: the first SPS byte is profile\_idc (66, 77 or 100) and the third is level\_idc (e.g. 40 for level 4.0); both match `ffprobe -show_streams`.

## Phase 3: Parameter sets, slice headers and picture order (7.3.2, 7.3.3, 8.2.1)

Phase 3 ends with every header field in the file parsed and every picture placed in display order. Print field names exactly as trace\_headers does, so verification is a plain `diff`.

**3.1 SPS parser** (1–2 days)

- Build: all of 7.3.2.1.1, including the High-profile branch (chroma\_format\_idc, bit depths, scaling\_list syntax, parsed now and applied in Phase 8), log2\_max\_frame\_num, the three pic\_order\_cnt\_type branches, max\_num\_ref\_frames, picture size in macroblocks, frame\_mbs\_only\_flag, direct\_8x8\_inference\_flag and cropping. Parse the VUI (Annex E): you need its timing info, colour matrix and max\_dec\_frame\_buffering later. Store SPSs in a table by id.
- Verify: `diff` your SPS dump against `headers.txt`. Width = 16 × (pic\_width\_in\_mbs\_minus1 + 1) minus cropping must match ffprobe (1088 coded rows crop to 1080, for example).

**3.2 PPS parser** (1 day)

- Build: all of 7.3.2.2: entropy\_coding\_mode\_flag, slice-group fields (reject more than one group), default ref-index counts, weighted\_pred\_flag, weighted\_bipred\_idc, pic\_init\_qp, chroma\_qp\_index\_offset, deblocking\_filter\_control\_present\_flag, constrained\_intra\_pred\_flag, and the optional trailing transform\_8x8\_mode\_flag, scaling lists and second\_chroma\_qp\_index\_offset (present only if more\_rbsp\_data()).
- Verify: `diff` every PPS against `headers.txt`, on at least one Baseline (T1) and one High (T11) file.

**3.3 Slice header parser** (2 days)

- Build: all of 7.3.3: first\_mb\_in\_slice, slice\_type (mod 5), frame\_num, field flags, idr\_pic\_id, POC fields, direct\_spatial\_mv\_pred\_flag, ref-count override, ref\_pic\_list\_modification (7.3.3.1), pred\_weight\_table (7.3.3.2), dec\_ref\_pic\_marking (7.3.3.3), cabac\_init\_idc, slice\_qp\_delta and deblocking parameters.
- Verify: `diff` all slice headers against `headers.txt`. On CABAC files, the cabac\_alignment\_one\_bits after the header must all be 1; a 0 means you misread a field.

**3.4 Picture boundaries and picture order count** (1–2 days)

- Build: detect the first slice of each new picture (7.4.1.2.4: frame\_num, pps id, nal\_ref\_idc, idr\_pic\_id or POC changing). Compute POC for all three pic\_order\_cnt\_type modes (8.2.1).
- Verify: sort pictures by POC and print their slice types. The sequence matches the pict\_type column of the ffprobe frames command, and the picture count matches. Cross-check: ordering pictures by your Phase 1 PTS gives the same order.

## Phase 4: CAVLC intra macroblock parsing (7.3.4, 7.3.5, 9.2)

Phase 4 ends with every I slice of T1 and T2 parsed down to the last coefficient. There are no pixels yet. Your strongest self-check from here on: if every macroblock parses bit-exactly, the read position lands exactly on rbsp\_trailing\_bits after macroblock PicSizeInMbs − 1. Any bug makes you overrun or stop short.

**4.1 Slice data loop and mb\_type** (1 day)

- Build: the slice\_data loop (no FMO, so macroblock addresses just increment). Map I-slice mb\_type through Table 7-11: I\_NxN; I\_16x16 with its prediction mode, chroma CBP and luma CBP packed into the type; and I\_PCM (align, then read raw samples).
- Verify: on T1, print the mb\_type histogram and compare it with the JM trace. Every macroblock should be an I\_16x16 type.

**4.2 Intra prediction syntax, CBP and QP delta** (1 day)

- Build: mb\_pred for intra (prev\_intra4x4\_pred\_mode\_flag / rem\_intra4x4\_pred\_mode × 16, intra\_chroma\_pred\_mode), coded\_block\_pattern through the me(v) mapping in Table 9-4 (intra column) and mb\_qp\_delta. Read transform\_size\_8x8\_flag when the PPS enables it.
- Verify: on T2, your per-macroblock prediction modes, CBP and QP delta match the JM trace for the first 20 macroblocks.

**4.3 Neighbour availability and coeff\_token** (2 days)

- Build: neighbouring-block derivation for 4x4 blocks A (left) and B (above), across macroblock and slice edges (6.4.11.4). Compute nC from the neighbours' total\_coeff and decode coeff\_token from Table 9-5. Store total\_coeff for every 4x4 block. Tip: type Table 9-5 as strings of bits and generate lookup code from them, since typos there are the most common Phase 4 bug.
- Verify: the JM trace prints coeff\_token (TotalCoeff, TrailingOnes) per block; diff the first slice.

**4.4 Levels, total\_zeros and run\_before** (1–2 days)

- Build: trailing-ones signs, level\_prefix / level\_suffix with adaptive suffixLength (9.2.2), total\_zeros (Tables 9-7 to 9-9, including the 2x2 chroma DC table) and run\_before (Table 9-10). Fill coefficients in zigzag order. Handle block order: Intra16x16 DC, luma 4x4 blocks in 8x8 quadrant order, chroma DC, chroma AC.
- Verify: every I slice in T1 and T2 lands exactly on rbsp\_trailing\_bits. Your coefficient arrays match the JM trace for the first macroblocks.

## Phase 5: Intra reconstruction and the first image (8.3, 8.5)

Phase 5 ends with T1, T2 and T4 decoding byte-identical to `ref.yuv`. These files are intra-only with deblocking off, so decode order equals display order and ffmpeg's output is directly comparable. Optionally jump ahead to step 9.2 here, to see your frames as images.

**5.1 Frame buffers and YUV writer** (1 day)

- Build: Y, Cb and Cr planes (chroma at half width and height), macroblock-to-pixel addressing, and a writer that appends each frame as planar yuv420p. Write I\_PCM samples straight into the planes.
- Verify: a gray or noise frame opens correctly in the ffplay raw viewer at the right size. If you have them, the CAVLC PCM conformance streams (e.g. CVPCMNL1\_SVA\_C) should match exactly.

**5.2 Inverse quantisation and inverse transforms** (2 days)

- Build: LevelScale4x4 from the normAdjust4x4 table with flat weights of 16 (8.5.9), the 4x4 integer inverse transform (8.5.12), the Intra16x16 luma DC Hadamard and its scaling (8.5.10), the 2x2 chroma DC transform (8.5.11), chroma QP via Table 8-15 and chroma\_qp\_index\_offset, the (x + 32) >> 6 rounding and clipping to 0–255.
- Verify: unit-test each transform on small hand-computed matrices. Their real test comes with 5.3.

**5.3 Intra 16x16 and chroma prediction** (1–2 days)

- Build: the four Intra16x16 modes (vertical, horizontal, DC, plane; 8.3.3) and the four chroma modes (8.3.4). Chroma numbers its modes differently (0 = DC), and its DC mode uses per-4x4-block neighbour rules. Add prediction plus residual.
- Verify: **first picture milestone.** T1 decodes byte-identical to `ref.yuv`. On mismatch, yuvcmp names the first wrong macroblock; dump it against the JM trace.

**5.4 Intra 4x4 prediction** (2 days)

- Build: the nine Intra4x4 modes (8.3.1.2), mode prediction (predIntra4x4PredMode = min(A, B), falling back to DC when a neighbour is unavailable or not intra 4x4), and the up-right availability rules, which depend on block index. Substitute unavailable up-right samples with the last available above sample.
- Verify: T2 decodes byte-identical.

**5.5 QP tracking and multiple slices** (1 day)

- Build: QP propagation with the wrap-around in mb\_qp\_delta's semantics (7.4.5), and neighbour availability that respects slice boundaries and constrained\_intra\_pred\_flag.
- Verify: T4 (four slices per picture) decodes byte-identical. A T2 variant encoded with `-crf 30` instead of `-qp 26` exercises QP deltas.

## Phase 6: Deblocking filter for intra pictures (8.7)

Phase 6 ends with T3 decoding byte-identical at three deblocking strengths. Inter boundary strengths come in step 7.5.

**6.1 Edge selection and boundary strength** (1 day)

- Build: per macroblock, choose which vertical and horizontal edges are filtered. Apply disable\_deblocking\_filter\_idc 0, 1 and 2 (2 = do not filter across slice edges). Skip internal 4x4 edges when the macroblock uses the 8x8 transform. Assign bS = 4 on intra macroblock edges and bS = 3 on intra internal edges.
- Verify: dump the bS grid of the first macroblocks and check by hand that the outer edge of the picture is never filtered.

**6.2 Luma and chroma edge filters** (2 days)

- Build: indexA and indexB from the average QP plus the slice alpha / beta offsets. Look up alpha, beta (Table 8-16) and tC0 (Table 8-17). Implement the bS < 4 filter (with the luma p1 / q1 conditions), the bS = 4 strong filter, and the chroma variants using chroma QP. Order matters: all vertical edges left to right, then horizontal edges top to bottom, macroblock by macroblock in raster order, reading already-filtered samples.
- Verify: T3 decodes byte-identical, and also with `deblock=-3,-3` and `deblock=3,3` (offsets) and with `slices=4` (slice-edge handling).

## Phase 7: Inter prediction, reference pictures, P and B slices (8.2.4, 8.2.5, 8.4)

Phase 7 ends with every CAVLC file (T5–T9) decoding byte-identical: a complete Baseline decoder plus CAVLC Main. P-only files decode in display order. For B files, write frames sorted by POC within each IDR period until step 9.1 replaces that shortcut.

**7.1 P-slice macroblock parsing** (1–2 days)

- Build: mb\_skip\_run, P mb\_types (Table 7-13: 16x16, 16x8, 8x16, 8x8, 8x8ref0), sub\_mb\_type (Table 7-17), ref\_idx as te(v), mvd as se(v), the inter column of the CBP mapping, and intra macroblocks inside P slices (mb\_type offset 5).
- Verify: every P slice in T5 and T6 lands exactly on rbsp\_trailing\_bits. The mb\_type histogram matches the JM trace.

**7.2 Decoded picture buffer and basic P reference lists** (1 day)

- Build: a DPB of decoded frames with frame\_num, FrameNumWrap and PicNum (8.2.4.1), sliding-window marking (8.2.5.3) and the default P list in descending PicNum (8.2.4.2.1).
- Verify: log which decoded picture each ref\_idx points to. In T5 (one reference) it is always the previous picture; in T6 the list order matches the PicNum rule.

**7.3 Motion vector prediction** (2 days)

- Build: median prediction from neighbours A, B and C, with D replacing an unavailable C (8.4.1.3). Add the directional rules for 16x8 and 8x16, P\_Skip derivation (zero vector when A or B is unavailable or has a zero vector with reference 0), and neighbour lookup at sub-partition level across macroblock edges.
- Verify: compare your vectors visually with `ffmpeg -flags2 +export_mvs -i T6.ts -vf codecview=mv=pf out.mp4`. Exact proof comes in 7.4.

**7.4 Luma and chroma interpolation and inter residual** (2 days)

- Build: the 6-tap half-sample filter (1, −5, 20, 20, −5, 1), the centre sample from unrounded intermediates, quarter-sample averaging (8.4.2.2.1), clamping of reference coordinates so vectors can point outside the picture, eighth-sample bilinear chroma (8.4.2.2.2), then the inter residual.
- Verify: T5 decodes byte-identical across all frames. Then T6 with `no-deblock=1` added, which exercises every partition size from 7.3.

**7.5 Deblocking for inter edges** (1 day)

- Build: bS = 2 where either side has non-zero coefficients; bS = 1 where references differ, the number of vectors differs, or a vector component differs by 4 or more quarter samples; otherwise bS = 0.
- Verify: T6 as specified (deblocking on) decodes byte-identical.

**7.6 Full reference management** (2 days)

- Build: ref\_pic\_list\_modification (8.2.4.3), adaptive marking with MMCO operations 1–6 (8.2.5.4), long-term references, frame\_num gaps (8.2.5.2) and IDR handling.
- Verify: a T6 variant with `ref=16` decodes byte-identical, plus the multi-reference conformance streams in H.264.1 (the MR\* frame streams, for example).

**7.7 Explicit weighted prediction** (1 day)

- Build: apply pred\_weight\_table weights and offsets with the logWD rounding (8.4.2.3).
- Verify: T7 (fade-in) decodes byte-identical.

**7.8 B-slice parsing, B reference lists and bi-prediction** (2 days)

- Build: B mb\_types (Table 7-14) and sub-types (Table 7-18), the POC-based list 0 and list 1 initialisation (8.2.4.2.3) including the swap rule when both lists are identical, and default bi-prediction averaging.
- Verify: T8 (no direct mode) decodes byte-identical.

**7.9 Direct modes and implicit weights** (2 days)

- Build: spatial direct (8.4.1.2.2, with colZeroFlag from the co-located picture), temporal direct (8.4.1.2.3, with DistScaleFactor), direct\_8x8\_inference, B\_Skip / B\_Direct\_16x16 / B\_Direct\_8x8, and implicit weights (weighted\_bipred\_idc = 2). Store vectors and references per picture, since later pictures read them as co-located data.
- Verify: T9 decodes byte-identical with `direct=spatial` and with `direct=temporal`. The pyramid exercises B pictures used as references.

## Phase 8: CABAC and High profile (9.3, 8.3.2, 8.5.13)

Phase 8 ends with default x264 Main and High files decoding byte-identical. Most progressive real-world `.ts` files (HD broadcast, IPTV, HLS) are High-profile CABAC, so this is where your decoder starts working on them. CABAC only replaces entropy decoding; everything from Phases 5–7 is reused.

**8.1 Arithmetic decoding engine and context initialisation** (2 days)

- Build: engine initialisation (codIRange = 510, 9-bit codIOffset), DecodeDecision with rangeTabLPS (Table 9-44) and the state-transition table (Table 9-45), renormalisation, DecodeBypass and DecodeTerminate (9.3.3.2). Context initialisation from the (m, n) tables 9-12 to 9-33, using cabac\_init\_idc and SliceQPY (9.3.1.1). Typing the m/n tables is a day on its own; copy them as data and count the entries.
- Verify: on a T10 I-only file, the first macroblock's mb\_type decodes to the value in the JM trace, and end\_of\_slice\_flag comes out 0 after it.

**8.2 Macroblock-level syntax elements** (2 days)

- Build: binarisation and ctxIdxInc for mb\_type (I), mb\_skip\_flag, mb\_qp\_delta, prev\_intra4x4\_pred\_mode / rem\_intra4x4\_pred\_mode, intra\_chroma\_pred\_mode, coded\_block\_pattern (prefix and suffix, with neighbour-based increments) and end\_of\_slice\_flag (9.3.2, 9.3.3.1).
- Verify: diff the (element, value) sequence of your first slice against the JM trace.

**8.3 Residual coding** (2 days)

- Build: coded\_block\_flag (its ctxIdxInc reads neighbouring blocks of the same category, so store a flag per block), significant\_coeff\_flag, last\_significant\_coeff\_flag, coeff\_abs\_level\_minus1 (UEG0 with contexts from counts of levels already decoded) and the bypass-coded sign, for ctxBlockCat 0–4.
- Verify: T10 I-only decodes byte-identical, and each slice ends on end\_of\_slice\_flag = 1 at exactly the right macroblock.

**8.4 Inter syntax elements** (1–2 days)

- Build: P and B mb\_type and sub\_mb\_type binarisations, ref\_idx, and mvd (UEG3, with ctxIdxInc from the sum of the neighbours' absolute mvd, so store mvd per partition, not just vectors).
- Verify: T10 with `-profile:v main` alone (x264 defaults: CABAC, B frames, multiple references) decodes byte-identical.

**8.5 8x8 transform and 8x8 intra prediction** (2 days)

- Build: transform\_size\_8x8\_flag, the 8x8 inverse transform (8.5.13), Intra 8x8 prediction with its reference-sample low-pass filter (8.3.2), CAVLC 8x8 residuals (four interleaved 4x4 blocks) and CABAC ctxBlockCat 5 with its own significance-map tables.
- Verify: T11 with `-profile:v high` alone decodes byte-identical, and so does a CAVLC High variant (add `-x264-params cabac=0`).

**8.6 Scaling matrices** (1 day)

- Build: apply SPS and PPS scaling lists, including the default tables and fall-back rules A and B (Table 7-2), to the 4x4 and 8x8 dequantisation.
- Verify: T11 with `cqm=jvt` decodes byte-identical. Then run a few real High-profile progressive `.ts` files end to end.

## Phase 9: Output order, display and live playback (C.4, Annex E)

Phase 9 ends with a player: point it at a `.ts` file or a live UDP stream and watch the video at the right speed. Opening a window is the one place you cannot avoid the operating system's own API (Win32, Cocoa or X11/Wayland). That is a system interface, not a library dependency.

**9.1 Output ordering (bumping)** (1–2 days)

- Build: the bumping process of C.4.5.3. Size the DPB from the VUI's max\_dec\_frame\_buffering, or from the level's MaxDpbMbs (Table A-1) when absent. Output the smallest-POC picture when the DPB is full; flush on IDR and MMCO 5 (respecting no\_output\_of\_prior\_pics\_flag) and at end of stream. Apply SPS cropping on output. This replaces the sort-by-POC shortcut from Phase 7.
- Verify: your whole-file YUV output for T9, T11 and a real broadcast file is byte-identical to `ref.yuv`, with the same frame count.

**9.2 Colour conversion and image output** (1 day)

- Build: YCbCr to RGB using the VUI's matrix\_coefficients (BT.709 for most HD, BT.601 for SD) and limited range 16–235. Upsample chroma, nearest-neighbour first. Write PPM files, a trivial format any image viewer opens.
- Verify: compare frame N with `ffmpeg -i in.ts -vf "select=eq(n\,N)" -frames:v 1 ref.png`. Expect small differences from rounding, not exact equality; wrong colours mean the wrong matrix or range.

**9.3 Window and frame presentation** (1–2 days)

- Build: a minimal native window that blits an RGB buffer (StretchDIBits on Windows, CGImage on macOS, XPutImage or a Wayland shm buffer on Linux). Add keys to pause and step one frame.
- Verify: step through T9 frame by frame; each frame matches the PPM from 9.2.

**9.4 Timing from PTS** (1 day)

- Build: carry each access unit's PES PTS through decoding to the output picture. Present it when the wall clock reaches PTS minus the first PTS, handling 33-bit wrap-around. Drop frames when decoding falls behind.
- Verify: a 60-second clip plays in 60 seconds (within about 1%). Logged presentation times match pts\_time from the ffprobe frames command.

**9.5 Live stream input and recovery** (1–2 days)

- Build: read TS from stdin or a UDP socket. Start decoding at the first IDR (or recovery-point SEI). On continuity-counter errors or missing references, conceal by repeating the last frame and resync at the next IDR. Handle SPS changes mid-stream, such as a resolution switch.
- Verify: send a stream with `ffmpeg -re -i in.ts -c copy -f mpegts udp://127.0.0.1:1234` and watch it. Start the player mid-stream, and kill and restart the sender: the picture must recover within one GOP and the player must not crash.

## Phase 10: Interlaced video, conformance and speed

Phase 10 closes the gap to "any `.ts` file": much broadcast TV is 1080i, coded either as field pictures (PAFF) or as macroblock-adaptive frame/field (MBAFF). MBAFF is the hardest part of the standard, so treat its 2-day estimates as optimistic.

**10.1 Field pictures** (2 days)

- Build: field\_pic\_flag / bottom\_field\_flag, decoding each field into alternate lines of a frame buffer, field POC (top and bottom), and pairing complementary fields into one output frame.
- Verify: the field-picture streams in the H.264.1 suite (its feature table lists which use field coding) decode byte-identical, intra-only ones first.

**10.2 Field reference lists and marking** (2 days)

- Build: field reference list initialisation with alternating parity (8.2.4.2.5), field-based MMCO marking, and temporal direct and implicit weights for fields.
- Verify: all PAFF conformance streams with P and B fields decode byte-identical, plus a real 1080i broadcast recording.

**10.3 MBAFF parsing and neighbour derivation** (2 days)

- Build: macroblock pairs, mb\_field\_decoding\_flag (inferred when both macroblocks of a pair are skipped), and the MBAFF neighbour derivation of 6.4.12.2, which feeds CAVLC nC, CABAC contexts, intra prediction and vector prediction.
- Verify: T12 slices land exactly on their end (trailing bits or end\_of\_slice\_flag), and per-pair field flags match the JM trace.

**10.4 MBAFF reconstruction and deblocking** (2 days)

- Build: field macroblocks predicting from field references inside a frame picture (vector and reference-index scaling), and the deblocking rules for mixed frame/field edges, including the extra top-edge filtering of frame macroblocks over field neighbours.
- Verify: T12 decodes byte-identical, plus the MBAFF conformance streams.

**10.5 Deinterlacing for display** (1 day)

- Build: honour the picture-timing SEI or field order, then display with a simple deinterlacer: bob (line doubling each field) first, optionally a motion-adaptive blend.
- Verify: a 1080i recording plays without combing artefacts on motion, at 50 or 60 fields per second.

**10.6 Conformance sweep and robustness** (1–2 days)

- Build: a script that runs every H.264.1 stream for the profiles you support and reports pass/fail by comparing to its supplied YUV or MD5. Harden the decoder against bad input: bounds-check every table index and vector.
- Verify: the pass list covers all in-scope streams. Feed truncated and randomly corrupted `.ts` files (overwrite bytes with `dd`) for an hour without a crash.

**10.7 Performance** (2 days, open-ended)

- Build: profile, then optimise the hot spots, usually CABAC decoding, interpolation and deblocking. Options: tables instead of branches, decoding by slice on multiple threads, SIMD.
- Verify: measured decode rate for a 1080p30 High-profile file exceeds 30 frames per second on your machine, with output still byte-identical.

