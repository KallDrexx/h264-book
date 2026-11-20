#!/usr/bin/env python3
"""Chapter 9.4 harness: timing from PTS, verified with a SIMULATED clock.  Python 3 standard library only (+ ffmpeg / ffprobe).

This harness never opens a window, never reads stdin, never sleeps for long, and runs your player only under a hard time limit
(process group killed).  Truth = ffprobe frame PTS values (pts_time) of the test streams + the documented scheduling rule.

  python3 test_timing.py --make-cases DIR       write DIR/cases.txt and, per case, <case>.ts (rewritten PES timestamps), <case>.cost, <case>.truth
  python3 test_timing.py DIR --out OUT [--case NAME ...] [--timeout S]
                                                check OUT/<case>/timing.log for every case
  python3 test_timing.py DIR --out OUT --reader 'CMD' [--realtime]
                                                run  CMD <ts> --virtual-clock <cost> --late-ms <N> --timing-log OUT/<case>/timing.log  first
                                                (--realtime: also run the short real-clock clip, ~5 s, optional / not deterministic)
  python3 test_timing.py --self-test            verify this harness: the reference player passes, >= 12 wrong behaviours are caught

Exit codes: 0 pass, 1 wrong behaviour, 2 missing / malformed input (log, cases, cost files, reader failed to run).
"""
import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402
import timing as T  # noqa: E402
import tsrewrite  # noqa: E402
import player  # noqa: E402

MOD = 1 << 33
TOL_MS = 0.0015           # log values are printed with 3 decimals


class Fatal(Exception):
    """Input missing / malformed -> exit 2."""


# ----------------------------------------------------------------------------------------------- stream + case generation
def encode(path, rate, seconds):
    r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=64x48:rate={rate}:duration={seconds}",
                   "-pix_fmt", "yuv420p", "-c:v", "libx264", "-profile:v", "main", "-qp", "28",
                   "-x264-params", "bframes=2:keyint=50:cabac=0:scenecut=0", "-f", "mpegts", path], timeout=240)
    if r.returncode or not os.path.exists(path):
        raise Fatal("ffmpeg could not encode a test stream: " + r.stderr.decode(errors="replace")[-300:])


def probe_pts(ts):
    """-> (decoding-order packet PTS list, output-order frame PTS list, indices of key packets); ffprobe values are unwrapped by ffmpeg."""
    pk = ffref.probe_packets(ts, "pts,flags")
    fr = ffref.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=pts", "-of", "csv=p=0", ts],
                   timeout=120).stdout.decode().splitlines()
    frames = []
    for line in fr:
        f = line.split(",")[0].strip()
        if f.lstrip("-").isdigit():
            frames.append(int(f))
    pks = [int(p["pts"]) if p["pts"].lstrip("-").isdigit() else None for p in pk]
    keys = [i for i, p in enumerate(pk) if "K" in p["flags"]]
    return pks, frames, keys


def cost_text(default, over):
    return "# decode-cost model (ms per output picture)\ndefault %g\n" % default + "".join(f"{k} {v:g}\n" for k, v in sorted(over.items()))


def make_cases(d):
    ffref.require_tools()
    os.makedirs(d, exist_ok=True)
    bases = {}
    for name, rate, secs in (("base", "25", 4), ("ntsc", "30000/1001", 3), ("long60", "25", 60), ("long180", "25", 180), ("rt5", "25", 5)):
        bases[name] = os.path.join(d, f"_{name}.ts")
        encode(bases[name], rate, secs)
    late_cost = {10: 90, 30: 120, 31: 5, 60: 78}
    cases = []

    def add(name, base, dur, late=40, default=5, over=None, start=None, jump=None, strip=(), strip_all=False, merge=(), mode="virtual"):
        ts = os.path.join(d, f"{name}.ts")
        pks, frames, keys = probe_pts(bases[base])
        n = len(frames)
        if len(pks) != n or n == 0:
            raise Fatal(f"{base}: {len(pks)} packets but {n} frames from ffprobe")
        order = sorted(range(n), key=lambda i: pks[i])               # decoding index -> output rank
        rank = {i: r for r, i in enumerate(order)}
        base_min = min(pks)
        off = 0 if start is None else start - base_min
        at, dj = (None, 0)
        if jump:
            at, dj = keys[1], jump                                     # jump at the second IDR
        tsrewrite.rewrite(bases[base], ts, lambda i: off + (dj if at is not None and i >= at else 0), set(strip), strip_all, set(merge))
        rel = [p - frames[0] for p in frames]
        if jump:
            rel = [r + (dj if k >= rank[at] else 0) for k, r in enumerate(rel)]
        if not strip_all and not strip and not merge:                   # cross-check the rewriter against ffprobe of the NEW file
            _p, fr2, _k = probe_pts(ts)
            if [p - fr2[0] for p in fr2] != rel:
                raise Fatal(f"generator error in case {name}: ffprobe of the rewritten stream disagrees with the expected timestamps")
        missing = set(strip) | {i + 1 for i in merge}
        src = ["vui"] * n if strip_all else ["inferred" if i in missing else "pes" for i in sorted(range(n), key=lambda i: rank[i])]
        pts0 = 0 if strip_all else (frames[0] + off) % MOD
        open(os.path.join(d, f"{name}.truth"), "w").write("".join(f"{k} {r} {s}\n" for k, (r, s) in enumerate(zip(rel, src))))
        open(os.path.join(d, f"{name}.cost"), "w").write(cost_text(default, over or {}))
        cases.append(f"CASE {name} ts={name}.ts cost={name}.cost truth={name}.truth late_ms={late} dur={dur} pts0={pts0} mode={mode}")

    add("ontime", "base", 3600)
    add("late", "base", 3600, over=late_cost)
    add("wrap", "base", 3600, over=late_cost, start=MOD - 180000)
    add("nopts", "base", 3600, over=late_cost, strip=[7, 14, 21, 28, 35, 42, 49, 56, 63, 70], merge=[20, 40, 41, 80])
    add("discont_fwd", "base", 3600, over={52: 70}, jump=900000)
    add("discont_back", "base", 3600, over={52: 70}, jump=-150000)
    add("vui_only", "base", 3600, over=late_cost, strip_all=True)
    add("ntsc_vui", "ntsc", 3003, strip_all=True)
    add("ntsc", "ntsc", 3003, over={20: 100})
    long_over = {k: 60 for k in range(25, 1500, 50)}
    long_over[700] = 200
    add("long60", "long60", 3600, default=8, over=long_over)
    long_over2 = {k: 55 for k in range(50, 4500, 100)}
    long_over2.update({1000: 300, 3000: 150})
    add("long180_wrap", "long180", 3600, default=8, over=long_over2, start=MOD - 90 * 90000)
    add("rt5", "rt5", 3600, mode="realtime")
    open(os.path.join(d, "cases.txt"), "w").write("# CASE <name> ts= cost= truth= late_ms= dur= pts0= mode=\n" + "\n".join(cases) + "\n")
    for b in bases.values():
        os.unlink(b)
    return d


KEYS = ("ts", "cost", "truth", "late_ms", "dur", "pts0", "mode")


def load_cases(d):
    path = os.path.join(d, "cases.txt")
    try:
        lines = open(path).read().splitlines()
    except OSError as e:
        raise Fatal(f"cannot read {path} (run --make-cases first): {e}")
    out = []
    for ln, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        t = line.split()
        if t[0] != "CASE" or len(t) < 2:
            raise Fatal(f"{path}:{ln}: expected 'CASE <name> key=value ...'")
        c = {"name": t[1]}
        for kv in t[2:]:
            k, _, v = kv.partition("=")
            if k not in KEYS:
                raise Fatal(f"{path}:{ln}: unknown key {k!r}")
            c[k] = v
        try:
            c["late"], c["dur_ticks"], c["pts0_raw"] = float(c["late_ms"]), int(c["dur"]), int(c["pts0"])
            for k in ("ts", "cost", "truth"):
                c[k + "_path"] = os.path.join(d, c[k])
            rows = [l.split() for l in open(c["truth_path"]).read().splitlines()]
            c["rel"] = [int(r[1]) for r in rows]
            c["src"] = [r[2] for r in rows]
            c["costs"] = T.parse_cost(open(c["cost_path"]).read())
        except (KeyError, ValueError, OSError, IndexError) as e:
            raise Fatal(f"{path}:{ln}: case {c['name']}: {e}")
        out.append(c)
    if not out:
        raise Fatal(f"{path}: no cases")
    return out


def expected(case):
    """Expected FRAME rows (as dicts) and END values, from ffprobe truth + the documented rule."""
    T.MUTANT = None
    rel = case["rel"]
    rows, span = T.schedule(rel, case["dur_ticks"], case["costs"], case["late"])
    pics = [{"raw": (case["pts0_raw"] + r) % MOD, "rel": r, "src": s} for r, s in zip(rel, case["src"])]
    return pics, rows, span


# ----------------------------------------------------------------------------------------------- log parsing
NUM = r"-?\d+(?:\.\d+)?"
RE_H = re.compile(r"HEADER frames=(\d+) dur_ticks=(\d+) late_ms=(" + NUM + r") clock=(virtual|real)")
RE_F = re.compile(r"FRAME (\d+) pts=(\d+) rel=(-?\d+) src=(pes|inferred|vui) sched_ms=(" + NUM + r") ready_ms=(" + NUM + r") shown_ms=(" + NUM +
                  r"|-) lag_ms=(" + NUM + r") decision=(present|drop) dur_ms=(" + NUM + r") event=(none|reanchor)")
RE_E = re.compile(r"END presented=(\d+) dropped=(\d+) span_ms=(" + NUM + ")")


def parse_log(path):
    try:
        text = open(path, encoding="ascii").read()
    except (OSError, UnicodeDecodeError) as e:
        raise Fatal(f"cannot read log {path}: {e}")
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 2:
        raise Fatal(f"{path}: log too short")
    m = RE_H.fullmatch(lines[0])
    if not m:
        raise Fatal(f"{path}:1: expected 'HEADER frames=N dur_ticks=T late_ms=L clock=<virtual|real>', got {lines[0]!r}")
    hdr = dict(frames=int(m[1]), dur_ticks=int(m[2]), late=float(m[3]), clock=m[4])
    frames = []
    for ln, line in enumerate(lines[1:-1], 2):
        m = RE_F.fullmatch(line)
        if not m:
            raise Fatal(f"{path}:{ln}: expected 'FRAME <k> pts=<raw> rel=<ticks> src=<pes|inferred|vui> sched_ms=<x> ready_ms=<x> shown_ms=<x|-> "
                        f"lag_ms=<x> decision=<present|drop> dur_ms=<x> event=<none|reanchor>', got {line!r}")
        g = m.groups()
        frames.append(dict(k=int(g[0]), pts=int(g[1]), rel=int(g[2]), src=g[3], sched=float(g[4]), ready=float(g[5]),
                           shown=None if g[6] == "-" else float(g[6]), lag=float(g[7]), decision=g[8], dur=float(g[9]), event=g[10], line=line))
    m = RE_E.fullmatch(lines[-1])
    if not m:
        raise Fatal(f"{path}:{len(lines)}: last line must be 'END presented=<n> dropped=<n> span_ms=<x>', got {lines[-1]!r}")
    return hdr, frames, dict(presented=int(m[1]), dropped=int(m[2]), span=float(m[3]))


# ----------------------------------------------------------------------------------------------- checking
def near(a, b):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= TOL_MS)


def check_case(rep, case, log, realtime=False):
    hdr, frames, end = parse_log(log)
    pics, rows, span = expected(case)
    name = case["name"]
    n = len(pics)
    rep.compare(f"{name}: HEADER frames", n, hdr["frames"])
    rep.compare(f"{name}: HEADER dur_ticks (frame duration from VUI, 90 kHz)", case["dur_ticks"], hdr["dur_ticks"])
    rep.compare(f"{name}: HEADER late_ms (echo of --late-ms)", case["late"], hdr["late"])
    rep.compare(f"{name}: HEADER clock", "real" if realtime else "virtual", hdr["clock"])
    rep.compare(f"{name}: number of FRAME lines", n, len(frames))
    bad_hints = []
    for k, (p, r, f) in enumerate(zip(pics, rows, frames)):
        if f["k"] != k:
            rep.check(False, f"{name}: FRAME line {k} carries index {f['k']} (output order, starting at 0)")
            break
        exp_dec = r["decision"]
        diffs = []
        for fld, e, a in (("pts (raw 33-bit PES value)", p["raw"], f["pts"]), ("rel (unwrapped ticks since first PTS)", p["rel"], f["rel"]),
                          ("src", p["src"], f["src"]), ("sched_ms", r["sched"], f["sched"]), ("dur_ms", r["dur"], f["dur"]),
                          ("event", r["event"], f["event"])):
            if (e != a) if isinstance(e, (int, str)) else not near(e, a):
                diffs.append((fld, e, a))
        if not realtime:
            for fld, e, a in (("decision", exp_dec, f["decision"]), ("ready_ms", r["ready"], f["ready"]), ("shown_ms", r["shown"], f["shown"]),
                              ("lag_ms", r["lag"], f["lag"])):
                if (e != a) if isinstance(e, str) else not near(e, a):
                    diffs.append((fld, e, a))
        else:
            if f["decision"] == "present" and f["shown"] is not None and abs(f["shown"] - f["sched"]) > 60.0:
                diffs.append(("shown_ms vs sched_ms (real clock, tolerance 60 ms)", f["sched"], f["shown"]))
        if diffs:
            fld, e, a = diffs[0]
            more = f" (+{len(diffs) - 1} more field(s) wrong: {', '.join(d[0] for d in diffs[1:3])})" if len(diffs) > 1 else ""
            rep.check(False, f"{name}: output picture {k}: {fld}: expected {e!r}, got {a!r}{more}")
            bad_hints.append((k, diffs))
        else:
            rep.check(True, "")
    if not realtime:
        rep.compare(f"{name}: END presented", sum(1 for r in rows if r["decision"] == "present"), end["presented"])
        rep.compare(f"{name}: END dropped", sum(1 for r in rows if r["decision"] == "drop"), end["dropped"])
        rep.check(near(span, end["span"]), f"{name}: END span_ms (last sched + last frame duration): expected {span:.3f}, got {end['span']:.3f}")
    else:
        rep.check(abs(end["span"] - span) <= 0.05 * span, f"{name}: END span_ms {end['span']:.1f} not within 5% of {span:.1f} (wall-clock playback length)")
    if len(case["rel"]) > 1000 and not realtime and frames:
        last = frames[-1]
        rep.check(abs(last["sched"] - rows[-1]["sched"]) < 1.0, f"{name}: no drift: last frame scheduled at {last['sched']:.3f} ms, expected {rows[-1]['sched']:.3f}")
    return hints(name, bad_hints, pics, frames)


def hints(name, bad, pics, frames):
    out = []
    if not bad:
        return out
    flds = {d[0].split()[0] for _k, ds in bad for d in ds}
    if "sched_ms" in flds and frames and pics[-1]["rel"]:
        ratio = frames[-1]["sched"] / (pics[-1]["rel"] / 90.0) if pics[-1]["rel"] > 0 else 0
        if ratio and (abs(ratio - 0.09) < 0.005 or abs(ratio - 11.11) < 0.3):
            out.append(f"{name}: sched_ms is off by a factor {ratio:.3f}: one tick is 1/90000 s, i.e. ticks / 90 = ms (not / 1000)")
    if "rel" in flds and any(d[2] < 0 or abs(d[2]) > 1 << 31 for _k, ds in bad for d in ds if d[0].startswith("rel")):
        out.append(f"{name}: rel is negative or huge: the 33-bit PTS was not unwrapped (use the signed 33-bit difference)")
    if "pts" in flds:
        out.append(f"{name}: pts mismatch: check which PES field / access unit the value comes from and that all 33 bits are kept")
    if "shown_ms" in flds and "sched_ms" not in flds and "decision" not in flds:
        out.append(f"{name}: only shown_ms differs: the target time must be anchor + (PTS - first PTS), never previous shown time + step (drift)")
    return out


# ----------------------------------------------------------------------------------------------- running a reader
def run_reader(cmd, case, out_dir, timeout, realtime=False):
    os.makedirs(out_dir, exist_ok=True)
    log = os.path.join(out_dir, "timing.log")
    if os.path.exists(log):
        os.unlink(log)
    args = shlex.split(cmd) + [case["ts_path"], "--late-ms", "%g" % case["late"], "--timing-log", log]
    if not realtime:
        args += ["--virtual-clock", case["cost_path"]]
    t0 = time.monotonic()
    with open(os.path.join(out_dir, "reader_output.txt"), "wb") as fo:
        pr = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = pr.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(pr.pid, signal.SIGKILL)
            pr.wait()
            raise Fatal(f"{case['name']}: your player did not finish within {timeout} s and was killed (see {out_dir}/reader_output.txt)")
    wall = time.monotonic() - t0
    if rc != 0:
        raise Fatal(f"{case['name']}: your player exited with status {rc} (see {out_dir}/reader_output.txt)")
    return log, wall


def main_check(a):
    cases = load_cases(a.dir)
    sel = [c for c in cases if (not a.case or c["name"] in a.case) and (c["mode"] == "virtual")]
    if a.case:
        unknown = set(a.case) - {c["name"] for c in cases}
        if unknown:
            raise Fatal(f"unknown case(s): {', '.join(sorted(unknown))}")
    rep = Report("Chapter 9.4 timing from PTS")
    all_hints = []
    for c in sel:
        out = os.path.join(a.out, c["name"])
        log = run_reader(a.reader, c, out, a.timeout)[0] if a.reader else os.path.join(out, "timing.log")
        all_hints += check_case(rep, c, log)
    if a.realtime:
        if not a.reader:
            raise Fatal("--realtime needs --reader")
        rt = [c for c in cases if c["mode"] == "realtime"]
        for c in rt:
            out = os.path.join(a.out, c["name"])
            log, wall = run_reader(a.reader, c, out, 40, realtime=True)
            all_hints += check_case(rep, c, log, realtime=True)
            exp = c["rel"][-1] / 90.0 + c["dur_ticks"] / 90.0 + 2000
            rep.check(wall >= 0.95 * (c["rel"][-1] / 90000.0), f"{c['name']}: wall time {wall:.2f} s is shorter than the clip: your player did not wait")
            rep.check(wall <= exp / 1000.0 + 8.0, f"{c['name']}: wall time {wall:.2f} s is far longer than the clip ({exp / 1000.0:.1f} s allowed incl. start-up)")
            print(f"  realtime: {c['name']} played in {wall:.2f} s wall time (clip {c['rel'][-1] / 90000.0 + c['dur_ticks'] / 90000.0:.2f} s)")
    for h in all_hints[:5]:
        print("  hint:", h)
    rep.finish()


# ----------------------------------------------------------------------------------------------- self-test
def self_test():
    ffref.require_tools()
    tmp = tempfile.mkdtemp(prefix="t94_")
    try:
        d = make_cases(os.path.join(tmp, "cases"))
        cases = [c for c in load_cases(d) if c["mode"] == "virtual"]
        print(f"self-test: {len(cases)} cases generated")
        py = sys.executable
        ref = os.path.join(HERE, "spoiler_reference", "player.py")
        r = Report("self-test good player (reference, subprocess)")
        for c in cases:
            log, _w = run_reader(f"{shlex.quote(py)} {shlex.quote(ref)}", c, os.path.join(tmp, "out", c["name"]), 120)
            check_case(r, c, log)
        if r.failures:
            for m in r.failures[:10]:
                print("  FAIL:", m)
            print("SELF-TEST FAILED: the reference player does not pass")
            return 1
        print(f"  good player: PASS ({r.checks} checks)")
        # truth sanity: the long wrap stream really wraps and 60 s really is 60 s
        w = next(c for c in cases if c["name"] == "long180_wrap")
        assert (w["pts0_raw"] + w["rel"][-1]) > MOD > w["pts0_raw"], "long180_wrap does not cross 2^33"
        span60 = expected(next(c for c in cases if c["name"] == "long60"))[2]
        assert abs(span60 - 60000) < 1, span60
        missed = []
        for bug in T.MUTANTS:
            caught = None
            for c in cases:
                lg = os.path.join(tmp, "mut.log")
                try:
                    player.main([c["ts_path"], "--timing-log", lg, "--virtual-clock", c["cost_path"], "--late-ms", "%g" % c["late"], "--bug", bug])
                    rr = Report("mutant")
                    rr.max_shown = 0
                    check_case(rr, c, lg)
                except SystemExit as e:                  # the mutant crashed: counts as caught only if it is an input error
                    caught = f"{c['name']} (player error: {e})"
                    break
                if rr.failures:
                    caught = f"{c['name']}: {rr.failures[0][:110]}"
                    break
            print(f"  {'caught' if caught else 'MISSED'}: {bug:22s} {T.MUTANTS[bug][:60]:60s} -> {caught or ''}")
            if not caught:
                missed.append(bug)
        T.MUTANT = None
        # a malformed log must give exit 2 semantics (Fatal)
        bad = os.path.join(tmp, "bad.log")
        open(bad, "w").write("HEADER nonsense\n")
        try:
            parse_log(bad)
            print("MISSED: malformed log accepted")
            missed.append("malformed")
        except Fatal:
            print("  caught: malformed log -> exit 2")
        if missed:
            print("SELF-TEST FAILED: not caught:", ", ".join(missed))
            return 1
        print(f"SELF-TEST PASSED: good player passes, {len(T.MUTANTS)} wrong behaviours caught")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", help="directory made by --make-cases")
    ap.add_argument("--make-cases", metavar="DIR")
    ap.add_argument("--out", help="directory holding <case>/timing.log (written there when --reader is used)")
    ap.add_argument("--case", action="append")
    ap.add_argument("--reader", help="command prefix to run your player, e.g. 'python3 myplayer.py'")
    ap.add_argument("--timeout", type=int, default=300, help="seconds per reader run (default 300)")
    ap.add_argument("--realtime", action="store_true", help="also run the 5 s real-clock clip (needs --reader; not deterministic)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        if a.self_test:
            sys.exit(self_test())
        if a.make_cases:
            print("wrote", make_cases(a.make_cases))
            return
        if not a.dir or not a.out:
            ap.error("need DIR and --out (or --make-cases / --self-test)")
        main_check(a)
    except Fatal as e:
        print("ERROR:", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
