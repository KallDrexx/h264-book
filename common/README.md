# common/

Shared stdlib-only helpers for chapter test harnesses (not part of any chapter's text).

- `testfiles.py` - builds the Summary's T1..T12 `.ts` recipes into `.cache/` (git-ignored). `python3 -m common.testfiles [names]`.
- `ffref.py` - timeout-protected ffmpeg/ffprobe wrappers (packets, frames, trace_headers, raw .264, reference YUV).
- `cavlc_ref.py` - reference parser for CAVLC macroblock data (Phase 4 harness truth) plus ffmpeg macroblock-map reader (`ffmpeg_mb_grid`) and QP-map reader (`ffmpeg_qp_grid`, from 4.2). Chapters 4.2-4.4 extend it by adding a parsing `level` (each harness passes its own, so earlier chapters' expected output never changes).
- `mbdump.py` - Phase 4 harness pieces shared by the chapters: the macroblock-dump parser (`parse_reader`), the ffmpeg macroblock-class cross-check, and the decode / I_PCM-pixel check of synthetic streams.
- `bitwriter.py` - bit writer (`u`/`ue`/`se`, RBSP trailing bits, emulation prevention, Annex B NAL) for the Phase 4 synthetic stream generators; shares no code with `cavlc_ref.py`.
- `report.py` - `Report` class: collect checks, print first failures, PASS/FAIL, exit code 0/1.

Chapter harnesses add the repo root to `sys.path` (`sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`) and `from common import ...`.
