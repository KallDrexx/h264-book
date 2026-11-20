#!/usr/bin/env python3
"""Reference player for Chapter 9.4 (spoiler).  Headless; never reads stdin; no GUI.

  player.py INPUT.ts --timing-log LOG [--virtual-clock COSTFILE] [--late-ms 40]

Without --virtual-clock the player really waits (time.monotonic, anchored on the first presentation) and measures its own times.
It does not decode pixels: output order comes from the POC of every access unit, the 'decode cost' from the cost model.
"""
import argparse
import functools
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timing as T
import tsdemux


@functools.lru_cache(maxsize=32)
def _demux(path):
    aus, vui = tsdemux.access_units(path)
    return aus, vui, tsdemux.output_order(aus)


def run(a):
    T.MUTANT = a.bug
    try:
        aus, vui, order = _demux(a.input)
    except (OSError, ValueError, IndexError) as e:
        sys.exit(f"{a.input}: {e}")
    if not aus:
        sys.exit(f"{a.input}: no video access units")
    pics = T.pictures(aus, vui, order)
    dur = T.frame_dur_ticks(vui, pics)
    rel = [p["rel"] for p in pics]
    if a.virtual_clock:
        try:
            cost = T.parse_cost(open(a.virtual_clock).read())
        except (OSError, ValueError) as e:
            sys.exit(str(e))
        rows, span = T.schedule(rel, dur, cost, a.late_ms)
        clock = "virtual"
    else:
        rows, span = realtime(rel, dur, a.late_ms)
        clock = "real"
    lines = T.fmt_lines(pics, rows, span, dur, a.late_ms, clock)
    open(a.timing_log, "w").write("\n".join(lines) + "\n")


def realtime(rel, dur, late_ms):
    """Same rules as T.schedule, but 'ready' and 'shown' are measured on a monotonic clock anchored at the first presentation."""
    t0 = time.monotonic()
    now = lambda: (time.monotonic() - t0) * 1000.0
    rows = []
    base_sched, base_rel, prev_sched = 0.0, rel[0], 0.0
    for k, r in enumerate(rel):
        event = "none"
        if k == 0:
            t0 = time.monotonic()                      # the clock starts when the first picture is shown
            sched = ready = shown = lag = 0.0
            decision = "present"
        else:
            step = r - rel[k - 1]
            if step <= 0 or step > T.DISCONT_TICKS:
                base_sched, base_rel, event = prev_sched + dur / 90.0, r, "reanchor"
            sched = base_sched + (r - base_rel) / 90.0
            ready = now()
            lag = ready - sched
            if lag > late_ms:
                decision, shown = "drop", None
            else:
                decision = "present"
                while now() < sched:                    # wait for the TARGET time, anchored on t0: no drift
                    time.sleep(min(0.01, max(0.0, (sched - now()) / 1000.0)))
                shown = now()
        rows.append({"sched": sched, "ready": ready, "shown": shown, "lag": lag, "decision": decision, "event": event})
        prev_sched = sched
    for k, row in enumerate(rows):
        row["dur"] = (rows[k + 1]["sched"] - row["sched"]) if k + 1 < len(rows) else dur / 90.0
    last = rows[-1]
    span = (last["shown"] if last["shown"] is not None else last["sched"]) + last["dur"]
    return rows, span


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input")
    p.add_argument("--timing-log", required=True)
    p.add_argument("--virtual-clock", metavar="COSTFILE")
    p.add_argument("--late-ms", type=float, default=40.0)
    p.add_argument("--bug", help=argparse.SUPPRESS)
    run(p.parse_args(argv))


if __name__ == "__main__":
    main()
