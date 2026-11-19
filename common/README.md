# common/

Shared stdlib-only helpers for chapter test harnesses (not part of any chapter's text).

- `testfiles.py` - builds the Summary's T1..T12 `.ts` recipes into `.cache/` (git-ignored). `python3 -m common.testfiles [names]`.
- `ffref.py` - timeout-protected ffmpeg/ffprobe wrappers (packets, frames, trace_headers, raw .264, reference YUV).
- `cavlc_ref.py` - reference parser for CAVLC macroblock data (Phase 4 harness truth) plus ffmpeg macroblock-map reader. Chapters 4.2-4.4 extend it by adding a parsing `level` (each harness passes its own, so earlier chapters' expected output never changes).
- `report.py` - `Report` class: collect checks, print first failures, PASS/FAIL, exit code 0/1.

Chapter harnesses add the repo root to `sys.path` (`sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`) and `from common import ...`.
