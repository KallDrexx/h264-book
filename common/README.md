# common/

Shared stdlib-only helpers for chapter test harnesses (not part of any chapter's text).

- `testfiles.py` - builds the Summary's T1..T12 `.ts` recipes into `.cache/` (git-ignored); extra recipes `T4_nodeblock` (T2 options + `slices=4`) and `default_intra` (Main, CAVLC, intra-only, no deblocking) were added in 5.4; (5.5) `T2_crf30`, `T2_aq`, `T4_crf30` (QP deltas via x264 adaptive quantisation) and `T2_cip` (PPS constrained_intra_pred_flag = 1). A recipe may carry an optional third element, a list of rate-control arguments replacing the default `-qp 26`. `python3 -m common.testfiles [names]`.
- `ffref.py` - timeout-protected ffmpeg/ffprobe wrappers (packets, frames, trace_headers, raw .264, reference YUV).
- `cavlc_ref.py` - reference parser for CAVLC macroblock data (Phase 4 harness truth) plus ffmpeg macroblock-map reader (`ffmpeg_mb_grid`) and QP-map reader (`ffmpeg_qp_grid`, from 4.2). Chapters 4.2-4.4 extend it by adding a parsing `level` (level 3, from 4.3, parses complete residuals via `cavlc_resid.py`; `render_blocks()` writes 4.3's block boundary file; level 4, from 4.4, only adds the `coef` key and the END `end_bit`) (each harness passes its own, so earlier chapters' expected output never changes).
- `cavlc_tables.py` - CAVLC code tables as bit strings (Table 9-5 coeff_token, total_zeros 9-7..9-9, run_before 9-10) with structural checks and a best-effort comparison against the installed libavcodec (4.3).
- `cavlc_resid.py` - reference residual parser (neighbour bookkeeping, nC, coeff_token, levels, total_zeros, run_before) used at level 3; has a `MUTANT` switch for harness self-tests (4.3, more mutants for levels / total_zeros / run_before in 4.4) and a `STATS` hook that collects coverage (4.4).
- `mbdump.py` - Phase 4 harness pieces shared by the chapters: the macroblock-dump parser (`parse_reader`), the ffmpeg macroblock-class cross-check, and the decode / I_PCM-pixel check of synthetic streams.
- `bitwriter.py` - bit writer (`u`/`ue`/`se`, RBSP trailing bits, emulation prevention, Annex B NAL) for the Phase 4 synthetic stream generators; shares no code with `cavlc_ref.py`.
- `yuvcmp.py` - (5.1) `compare_yuv(expected, actual, W, H)` and CLI `python3 -m common.yuvcmp a.yuv b.yuv W H`: first differing frame / plane / macroblock (x, y) / pixel with both values, per-frame difference counts, whole-plane hints (Cb/Cr swapped, only one plane wrong), a compact ASCII macroblock map of the first bad frame, size and truncation handling. `--origin X0 Y0` (`origin=`) = left/top crop in luma samples, so macroblock numbers match the decoder's. 8-bit 4:2:0 only; W, H are the cropped frame size. Exit 0 identical, 1 differs, 2 unusable input.
- `yuvref.py` - (5.1) reference-YUV helpers: `stream_info` (ffmpeg's cropped size and frame count), `frame_bytes`, `cropped_size` (SPS cropping formula). ffmpeg's reference YUV is cropped per the SPS; Phase 5+ readers write cropped frames too.
- `report.py` - `Report` class: collect checks, print first failures, PASS/FAIL, exit code 0/1.

Chapter harnesses add the repo root to `sys.path` (`sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`) and `from common import ...`.
