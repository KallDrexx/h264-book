# common/

Shared stdlib-only helpers for chapter test harnesses (not part of any chapter's text).

- `testfiles.py` - builds the Summary's T1..T12 `.ts` recipes into `.cache/` (git-ignored). `python3 -m common.testfiles [names]`.
- `ffref.py` - timeout-protected ffmpeg/ffprobe wrappers (packets, frames, trace_headers, raw .264, reference YUV).
- `report.py` - `Report` class: collect checks, print first failures, PASS/FAIL, exit code 0/1.

Chapter harnesses add the repo root to `sys.path` (`sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))`) and `from common import ...`.
