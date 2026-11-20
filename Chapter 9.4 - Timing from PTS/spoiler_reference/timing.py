"""SPOILER: reference timing logic (Chapter 9.4): PTS carried to output pictures, 33-bit unwrap, gap filling, first-PTS epoch,
re-anchoring on discontinuities and the virtual-clock presentation schedule.  Python 3 stdlib only.

MUTANT (set by the harness' self-test) selects one deliberately wrong behaviour from MUTANTS.
"""
from fractions import Fraction

MOD = 1 << 33
HALF = 1 << 32
DISCONT_TICKS = 90000                      # an output-order PTS step <= 0 or > 1 s is a discontinuity
DEFAULT_DUR = 3600                         # last resort frame duration (25 fps) when nothing else is known
MUTANT = None
MUTANTS = {
    "drift_accumulate": "target = previous shown time + frame step (delays accumulate) instead of anchor + (PTS - first PTS)",
    "wrap_no_unwrap": "33-bit wrap not unwrapped: plain subtraction of raw PTS values",
    "pts_mask32": "PTS read with only 32 bits (top bit of the 33-bit value lost)",
    "first_not_subtracted": "schedule uses the raw PTS, first PTS never subtracted",
    "first_is_second": "first-PTS anchor taken from the second output picture",
    "pts_decode_order": "PTS list assigned to pictures in decoding order (wrong with B-frames)",
    "use_dts": "DTS used instead of PTS when both are present",
    "never_drop": "late frames are never dropped",
    "drop_any_late": "any lateness (ready after its target time) drops the frame",
    "drop_free_time": "a dropped frame is assumed to cost no decode time",
    "units_1000": "ticks converted to ms with 1000 instead of 90",
    "vui_fps_doubled": "VUI fps taken as time_scale / num_units_in_tick (frame duration halved)",
    "vui_fps_halved": "VUI fps taken as time_scale / (4 * num_units_in_tick) (frame duration doubled)",
    "missing_copy_prev": "an access unit without PTS gets the previous access unit's PTS (decoding order)",
    "no_reanchor": "discontinuities ignored: schedule follows the jump (or goes backwards)",
    "reanchor_zero_gap": "after a discontinuity the next frame is scheduled with zero gap instead of one frame duration",
    "last_dur_zero": "duration of the last frame reported as 0",
}


def sdiff(a, b):
    """Signed difference a - b of two 33-bit values, in (-2^32, 2^32]."""
    d = (a - b) % MOD
    return d - MOD if d > HALF else d


def dur_from_vui(timing):
    """Frame duration in 90 kHz ticks from VUI timing_info (frame_mbs_only: 2 ticks per frame): 90000 * 2 * nu / ts, or None."""
    if not timing or not timing[0] or not timing[1]:
        return None
    nu, ts, _fixed = timing
    k = {"vui_fps_doubled": 1, "vui_fps_halved": 4}.get(MUTANT, 2)
    return Fraction(90000 * k * nu, ts)


def pictures(aus, timing, order):
    """-> list (output order) of dict(raw, u, src, rel) ; u = unwrapped PTS in ticks, rel = u - first.  aus: decoding order."""
    n = len(aus)
    raws = []
    for a in aus:
        v = a["dts"] if (MUTANT == "use_dts" and a["dts"] is not None) else a["pts"]
        if v is not None and MUTANT == "pts_mask32":
            v &= HALF - 1
        raws.append(v)
    if MUTANT == "missing_copy_prev":
        for i in range(1, n):
            if raws[i] is None:
                raws[i] = raws[i - 1]
    u, prev_raw, prev_u = [], None, None                # unwrap in decoding order (steps are a few frames, far below 2^32)
    for v in raws:
        if v is None:
            u.append(None)
            continue
        if prev_raw is None or MUTANT == "wrap_no_unwrap":
            cur = v
        else:
            cur = prev_u + sdiff(v, prev_raw)
        u.append(cur)
        prev_raw, prev_u = v, cur
    dur = dur_from_vui(timing)
    seq = [u[i] for i in order] if MUTANT != "pts_decode_order" else list(u)
    src = ["pes" if x is not None else "inferred" for x in seq]
    if all(x is None for x in seq):
        d = dur or DEFAULT_DUR
        seq = [int(k * d) for k in range(n)]
        src = ["vui"] * n
    else:
        known = [i for i, x in enumerate(seq) if x is not None]
        j0 = known[0]
        last_step = next((seq[known[t + 1]] - seq[known[t]] for t in range(len(known) - 1) if known[t + 1] == known[t] + 1), None)
        d = int(round(dur)) if dur else (last_step or DEFAULT_DUR)
        for k in range(j0 - 1, -1, -1):
            seq[k] = seq[k + 1] - d
        for k in range(j0 + 1, n):
            if seq[k] is None:
                seq[k] = seq[k - 1] + d
    first = seq[1] if (MUTANT == "first_is_second" and n > 1) else seq[0]
    out = []
    for k in range(n):
        rel = seq[k] - first
        if MUTANT == "first_not_subtracted":
            rel = seq[k]
        out.append({"raw": seq[k] % MOD, "rel": rel, "src": src[k]})
    return out


def frame_dur_ticks(timing, pics):
    d = dur_from_vui(timing)
    if d is not None:
        return int(round(d))
    steps = [pics[k + 1]["rel"] - pics[k]["rel"] for k in range(len(pics) - 1) if 0 < pics[k + 1]["rel"] - pics[k]["rel"] <= DISCONT_TICKS]
    return steps[-1] if steps else DEFAULT_DUR


def parse_cost(text):
    default, over = None, {}
    for ln, line in enumerate(text.splitlines(), 1):
        t = line.split("#")[0].split()
        if not t:
            continue
        try:
            if t[0] == "default" and len(t) == 2:
                default = float(t[1])
            elif len(t) == 2:
                over[int(t[0])] = float(t[1])
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"cost model line {ln}: expected 'default <ms>' or '<output index> <ms>', got {line!r}")
    if default is None:
        raise ValueError("cost model has no 'default <ms>' line")
    return default, over


def schedule(rel, dur_ticks, cost, late_ms):
    """The virtual-clock model.  rel: list of 'ticks since first PTS' in output order.  cost: (default, overrides).
    Returns rows (dicts) and the END span.  All times in ms relative to the first presentation (= 0)."""
    default, over = cost
    unit = 1000.0 if MUTANT == "units_1000" else 90.0
    dur_ms = dur_ticks / unit
    rows, free = [], 0.0
    base_sched, base_rel = 0.0, rel[0] if rel else 0
    prev_sched = prev_shown = 0.0
    for k, r in enumerate(rel):
        event = "none"
        if k == 0:
            sched = ready = shown = lag = 0.0
            decision = "present"
        else:
            step = r - rel[k - 1]
            if (step <= 0 or step > DISCONT_TICKS) and MUTANT != "no_reanchor":
                base_sched = prev_sched + (0.0 if MUTANT == "reanchor_zero_gap" else dur_ms)
                base_rel = r
                event = "reanchor"
            if MUTANT == "drift_accumulate":
                sched = prev_shown + (r - rel[k - 1]) / unit
            else:
                sched = base_sched + (r - base_rel) / unit
            c = over.get(k, default)
            ready = free + c
            lag = ready - sched
            late = lag > 0 if MUTANT == "drop_any_late" else lag > late_ms
            if MUTANT == "never_drop":
                late = False
            if late:
                decision, shown = "drop", None
                free = free if MUTANT == "drop_free_time" else ready
            else:
                decision, shown = "present", max(ready, sched)
                free = shown
        if k == 0:
            free = 0.0
        rows.append({"sched": sched, "ready": ready, "shown": shown, "lag": lag, "decision": decision, "event": event})
        prev_sched = sched
        if shown is not None:
            prev_shown = shown
    for k, row in enumerate(rows):
        row["dur"] = (rows[k + 1]["sched"] - row["sched"]) if k + 1 < len(rows) else (0.0 if MUTANT == "last_dur_zero" else dur_ms)
    span = (rows[-1]["sched"] + rows[-1]["dur"]) if rows else 0.0
    return rows, span


def fmt_lines(pics, rows, span, dur_ticks, late_ms, clock):
    L = [f"HEADER frames={len(pics)} dur_ticks={dur_ticks} late_ms={late_ms:g} clock={clock}"]
    for k, (p, r) in enumerate(zip(pics, rows)):
        shown = "-" if r["shown"] is None else "%.3f" % r["shown"]
        L.append(f"FRAME {k} pts={p['raw']} rel={p['rel']} src={p['src']} sched_ms={r['sched']:.3f} ready_ms={r['ready']:.3f} "
                 f"shown_ms={shown} lag_ms={r['lag']:.3f} decision={r['decision']} dur_ms={r['dur']:.3f} event={r['event']}")
    pres = sum(1 for r in rows if r["decision"] == "present")
    L.append(f"END presented={pres} dropped={len(rows) - pres} span_ms={span:.3f}")
    return L
