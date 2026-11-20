# common/

Shared stdlib-only helpers for chapter test harnesses (not part of any chapter's text).

> **SPOILER WARNING.** Three files here are complete solutions of whole chapters, kept in `common/` only because several harnesses share them: `cavlc_ref.py` and `cavlc_resid.py` (the CAVLC macroblock and residual parser of Phase 4) and `inter_ref.py` (the CAVLC P / B macroblock parser of Phase 7). Do not open them before your own parser passes the corresponding chapters. Everything else here (stream recipes, ffmpeg wrappers, comparison and report helpers, bit writer, the CAVLC code tables) is plumbing and safe to read.

- `testfiles.py` - builds the Summary's T1..T12 `.ts` recipes into `.cache/` (git-ignored); extra recipes `T4_nodeblock` (T2 options + `slices=4`) and `default_intra` (Main, CAVLC, intra-only, no deblocking) were added in 5.4; (5.5) `T2_crf30`, `T2_aq`, `T4_crf30` (QP deltas via x264 adaptive quantisation) and `T2_cip` (PPS constrained_intra_pred_flag = 1). A recipe may carry an optional third element, a list of rate-control arguments replacing the default `-qp 26`. `python3 -m common.testfiles [names]`.
  (7.4) Recipe `T6_nodeblock` = T6's options plus `no-deblock=1` (all partition sizes, ref=4, no deblocking): exactly decodable after 7.4.
  (8.2) CABAC Main-profile recipes without 8x8 transform: `T10_p` (P only), `T10_b` (B pyramid), `T10_slices_aq` (4 slices, adaptive quantisation, -crf 28), `T10_qp46`, `T10_qp8` (very high / low QP; the last with B frames).
  (8.4) `T10_ref` (ref=5, all partition sizes, B pyramid: ref_idx and sub_mb_type) and `T10_manyb` (eight B frames, temporal direct, ref=3).
  (8.5) High-profile recipes (8x8 transform): `T11_cavlc`, `T11_intra`, `T11_intra_cavlc`, `T11_p`, `T11_p_cavlc`, `T11_slices_aq`, `T11_slices_aq_cavlc`.
- `ffref.py` - timeout-protected ffmpeg/ffprobe wrappers (packets, frames, trace_headers, raw .264, reference YUV).
- `ffmv_helper.py` - (7.3; shared by 7.3, 7.8, 7.9, 10.4) prints the motion vectors libavcodec exports (`AVMotionVector` side data) via `ctypes`; run only as a subprocess with a timeout (`python3 common/ffmv_helper.py in.264 out.txt`, exit 3 = libavcodec not usable).
- `cavlc_ref.py` - reference parser for CAVLC macroblock data (Phase 4 harness truth) plus ffmpeg macroblock-map reader (`ffmpeg_mb_grid`) and QP-map reader (`ffmpeg_qp_grid`, from 4.2). Chapters 4.2-4.4 extend it by adding a parsing `level` (level 3, from 4.3, parses complete residuals via `cavlc_resid.py`; `render_blocks()` writes 4.3's block boundary file; level 4, from 4.4, only adds the `coef` key and the END `end_bit`) (each harness passes its own, so earlier chapters' expected output never changes).
- `cavlc_tables.py` - CAVLC code tables as bit strings (Table 9-5 coeff_token, total_zeros 9-7..9-9, run_before 9-10) with structural checks and a best-effort comparison against the installed libavcodec (4.3).
- `cavlc_resid.py` - reference residual parser (neighbour bookkeeping, nC, coeff_token, levels, total_zeros, run_before) used at level 3; has a `MUTANT` switch for harness self-tests (4.3, more mutants for levels / total_zeros / run_before in 4.4) and a `STATS` hook that collects coverage (4.4).
- `mbdump.py` - Phase 4 harness pieces shared by the chapters: the macroblock-dump parser (`parse_reader`), the ffmpeg macroblock-class cross-check, and the decode / I_PCM-pixel check of synthetic streams; also the key-spelling aliases of the dump grammar below (`cabac_aliases`, `long_spelling`), used by the CABAC-phase harnesses.
- `bitwriter.py` - bit writer (`u`/`ue`/`se`, RBSP trailing bits, emulation prevention, Annex B NAL) for the Phase 4 synthetic stream generators; shares no code with `cavlc_ref.py`.
- `inter_ref.py` - (7.1) reference parser for CAVLC I and P slices, built on `cavlc_ref.py` / `cavlc_resid.py` without modifying them: `load()` / `parse_all()` -> per-slice results (skip runs, P mb_types, sub_mb_types, ref_idx te(v), mvd, inter CBP, t8x8 rules, intra macroblocks in P slices; `rec.parts` lists every prediction block for later chapters), `render_dump(results, level)` / `parse_dump()` (keys per level via `keys_mb(level)`; the Phase 7 dump format: Phase 4's MB line plus `skip_run sub refidx mvd`, name-based HIST), ffmpeg cross-checks (`check_vs_ffmpeg_cells` partition-shape map, `check_vs_ffmpeg_qp`, `check_vs_ffmpeg_mv_counts` via `-flags2 +export_mvs`) and a `MUTANT` switch for harness self-tests. Level 2 (7.3) declares the keys `mvp` / `mv`; the values are computed by 7.3's `spoiler_reference/mvpred_ref.py`. Later Phase 7 chapters add levels / keys here (see its docstring). (7.8) Level 3 parses B slices (`load(path, workdir, 3)`; Table 7-14 / 7-18, `refidx1` / `mvd1` keys, `rec.parts` with per-list `mode` / `ref` / `ref1` / `mvd` / `mvd1`); `expected_cell` knows the B names and `check_vs_ffmpeg_cells` / `check_vs_ffmpeg_qp` take `out_rank` (decode index -> output index) for streams with reordered pictures, because ffmpeg prints its maps in output order. Levels 1 and 2 print exactly what they printed before.
- `yuvcmp.py` - (5.1) `compare_yuv(expected, actual, W, H)` and CLI `python3 -m common.yuvcmp a.yuv b.yuv W H`: first differing frame / plane / macroblock (x, y) / pixel with both values, per-frame difference counts, whole-plane hints (Cb/Cr swapped, only one plane wrong), a compact ASCII macroblock map of the first bad frame, size and truncation handling. `--origin X0 Y0` (`origin=`) = left/top crop in luma samples, so macroblock numbers match the decoder's. 8-bit 4:2:0 only; W, H are the cropped frame size. Exit 0 identical, 1 differs, 2 unusable input.
- `yuvref.py` - (5.1) reference-YUV helpers: `stream_info` (ffmpeg's cropped size and frame count), `frame_bytes`, `cropped_size` (SPS cropping formula). ffmpeg's reference YUV is cropped per the SPS; Phase 5+ readers write cropped frames too.
- `report.py` - `Report` class: collect checks, print first failures, PASS/FAIL, exit code 0/1.

## Macroblock dump grammar (Chapters 4.1 - 4.4, 7.1 - 7.9, 8.2 - 8.5, 10.3)

Every chapter that asks for a per-macroblock text dump uses the same grammar; each chapter's Readme only lists the keys it needs.

* One record per line: a keyword (`SLICE`, `MB`, `END`, and in Phases 4 and 7 also `PCM` and `HIST`), the slice index (and on `MB` lines the macroblock address), then `key=value` words separated by single spaces, no spaces inside a value. Numbers are decimal; `-` means "not present". Blank lines and `#` comments are ignored. An unknown keyword is an error (exit 2).
* Keys may come in any order. **Every key the chapter requires must be present** (a missing key or a malformed value is exit 2). **Extra keys are allowed and ignored**, so one program can print the union of everything it knows and feed every harness.
* **Two spellings.** Phases 4 and 7 (CAVLC) introduced long key names, the CABAC chapters (8.2 - 8.5) and 10.3 print short ones. Same meaning, different names:

| long (Phases 4, 7) | short (8.2 - 8.5, 10.3) | value |
|---|---|---|
| `mb_type` | `mbt` | the slice's own `mb_type` number, `skip` for P_Skip / B_Skip |
| `chroma_pred` | `cpred` | `intra_chroma_pred_mode` or `-` |
| `qp_delta` | `qpd` | `mb_qp_delta` or `-` |
| `t8x8` | `t8` | `transform_size_8x8_flag` or `-` |
| `modes=-1,1,0,...` (`i16:<m>` for Intra16x16) | `modes=p10...` (`-` for Intra16x16) | per mode element: predicted (`-1` / `p`) or the `rem` value |

  The **long spelling is the canonical one**: the CABAC-phase harnesses (8.2 - 8.5, 10.3) accept it as an alias for the short one (`mbdump.cabac_aliases`), so a dump printed by your Phase 4 / 7 code keeps working; the Phase 4 / 7 harnesses know only the long names. A line that carries both spellings of a key with different values is malformed. Keys with the same name in both phases (`cbp`, `qp`, `coef`, `sub`, `refidx`, `refidx1`, `mvd`, `mvd1`) have the same meaning.

Chapter harnesses add the repo root to `sys.path` (`sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`) and `from common import ...`.
