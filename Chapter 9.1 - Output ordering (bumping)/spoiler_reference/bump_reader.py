#!/usr/bin/env python3
"""SPOILER: a complete 'reader' for channel A: python3 bump_reader.py in.trace out.bump [--lenient]
It is bumping_ref.py behind the trace format; used to test the harness's --suite --reader mode."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bumping_ref  # noqa: E402

if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    text = open(a[0]).read()
    bumping_ref.LENIENT = "--lenient" in sys.argv or "robust rule" in text
    pics = bumping_ref.parse_trace(text)
    ev, fin = bumping_ref.run(pics)
    open(a[1], "w").write(bumping_ref.format_output(ev, fin))
