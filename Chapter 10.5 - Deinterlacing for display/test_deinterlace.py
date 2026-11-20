#!/usr/bin/env python3
"""Chapter 10.5 harness: deinterlacing for display (headless).  Python 3 standard library only (+ ffmpeg / ffprobe / x264).

Your program reads a stream (.ts or Annex B .264), decodes it with your decoder, and writes

  (A) a presentation LOG (text): per decoded frame (output order) a FRAME line, and per displayed picture an OUT line;
  (B) a YUV file: every OUT picture as one cropped yuv420p frame, in OUT order - the bob-deinterlaced field (or the whole frame).

  python3 test_deinterlace.py --make DIR                  write the test inputs (DIR/<case>.ts|.264) for your program to read
  python3 test_deinterlace.py --check DIR OUTDIR          check OUTDIR/<case>.log and OUTDIR/<case>.yuv for every case in DIR
  python3 test_deinterlace.py --suite DIR --reader 'CMD {stream} {log} {yuv}'
                                                          --make, then run CMD for every case (stdin closed, 300 s timeout;
                                                          outputs go to DIR/out), then --check
  python3 test_deinterlace.py --stream X --log L --yuv Y  check ANY stream (a 1080i recording, a conformance stream, ...)
  python3 test_deinterlace.py --show-expected X           print the expected log of a stream (what your FRAME / OUT lines must say)
  options: --only a,b   restrict to some cases (names without extension)
  python3 test_deinterlace.py --self-test                 verify this harness: good outputs pass, 20+ wrong rules are caught

Truth: frames = ffmpeg's decode (-fps_mode passthrough, cropped yuv420p); structure (field pictures, POC, pic_struct) = ffmpeg's
trace_headers log; the rules of the Readme are applied to them by spoiler_reference/plan_ref.py and spoiler_reference/bob_ref.py;
ffprobe's top_field_first / repeat_pict are cross-checked (disagreements are printed as notes).
Exit codes: 0 pass, 1 fail, 2 missing / malformed input.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import bob_ref  # noqa: E402
import mkstreams  # noqa: E402
import plan_ref  # noqa: E402


class Bad(Exception):
    """Unusable input: exit 2."""


FRAME_RE = re.compile(r"^FRAME (\d+) coded=(F|TB|BT) ps=(-|\d+) seq=([TBF]+)$")
OUT_RE = re.compile(r"^OUT (\d+) frame=(\d+) field=([TBF]) tick=(\d+)$")


# ------------------------------------------------------------------------------------------------ truth
class Truth:
    def __init__(self, path, work):
        self.path, self.work = path, work
        self.name = os.path.splitext(os.path.basename(path))[0]
        if not os.path.isfile(path):
            raise Bad(f"stream not found: {path}")
        ffref.require_tools()
        tr = os.path.join(work, self.name + ".trace.txt")
        if ffref.trace_headers(path, tr) != 0 and not os.path.exists(tr):
            raise Bad(f"ffmpeg could not read {path}")
        self.blocks = plan_ref.parse_trace(tr)
        yuv = os.path.join(work, self.name + ".ff.yuv")
        if yuvref.reference_yuv(path, yuv) == 0:
            raise Bad(f"ffmpeg decoded no frames from {path}")
        info = yuvref.stream_info(path)
        self.w, self.h = info["width"], info["height"]
        self.fsize = bob_ref.frame_size(self.w, self.h)
        raw = open(yuv, "rb").read()
        if len(raw) % self.fsize:
            raise Bad(f"{path}: ffmpeg output is not a whole number of {self.w}x{self.h} frames")
        self.src = [raw[i:i + self.fsize] for i in range(0, len(raw), self.fsize)]
        self.build()
        self._exp = None

    def build(self, blocks=None):
        try:
            pics, _ = plan_ref.scan(None, blocks or self.blocks)
            self.stores = plan_ref.frames_of(pics)
        except plan_ref.Unsupported as e:
            raise Bad(f"{self.path}: {e}")
        except (KeyError, ValueError) as e:
            raise Bad(f"{self.path}: cannot interpret the trace_headers log ({e!r})")
        if len(self.stores) != len(self.src):
            raise Bad(f"{self.path}: the stream has {len(self.stores)} frames by the harness's reading but ffmpeg decodes {len(self.src)}")
        self.flines, self.olines, self.outs = plan_ref.plan(self.stores)

    def expected_frames(self):
        if self._exp is None:
            self._exp = [bob_ref.bob(self.src[k], self.w, self.h, ch) for k, ch, _ in self.outs]
        return self._exp

    def crosscheck(self):
        """Notes where ffprobe's own opinion (top_field_first, repeat_pict) differs from the plan."""
        j = ffref.probe_json(self.path, "-select_streams", "v:0", "-show_frames",
                             "-show_entries", "frame=top_field_first,repeat_pict")
        fr = j.get("frames", [])
        notes = []
        if len(fr) != len(self.stores):
            return [f"ffprobe lists {len(fr)} frames, expected {len(self.stores)}"]
        for k, (s, f) in enumerate(zip(self.stores, fr)):
            seq = plan_ref.sequence(s)
            if "top_field_first" in f and len(seq) >= 2 and set(seq) <= {"T", "B"}:
                if s["top"] != s["bot"]:
                    exp = s["top"] < s["bot"]            # libavcodec derives it from the POCs when they differ (even against the SEI)
                elif s["ps"] is not None:
                    exp = seq[0] == "T"
                else:
                    exp = None                           # equal POCs, no SEI: ffmpeg's flag is a default, not information
                if exp is not None and (f["top_field_first"] == 1) != exp:
                    notes.append(f"frame {k}: ffprobe top_field_first={f['top_field_first']}, harness expects {int(exp)} (seq {seq})")
            if "repeat_pict" in f:
                exp = {3: 1}.get(len(seq), 0) if seq[0] != "F" else {1: 0, 2: 2, 3: 4}[len(seq)]
                if f["repeat_pict"] != exp:
                    notes.append(f"frame {k}: ffprobe repeat_pict={f['repeat_pict']}, harness expects {exp} (seq {seq})")
        return notes


# ------------------------------------------------------------------------------------------------ reader output
def parse_log(path):
    if not os.path.isfile(path):
        raise Bad(f"log file missing: {path}")
    frames, outs = [], []
    with open(path, "r", errors="replace") as f:
        for ln, raw in enumerate(f, 1):
            line = " ".join(raw.split())
            if not line or line.startswith("#"):
                continue
            m = FRAME_RE.match(line)
            if m:
                frames.append((int(m.group(1)), m.group(2), m.group(3), m.group(4)))
                continue
            m = OUT_RE.match(line)
            if m:
                outs.append((int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))))
                continue
            raise Bad(f"{path}:{ln}: cannot parse log line {raw.strip()[:80]!r} (expected 'FRAME k coded=.. ps=.. seq=..' or "
                      f"'OUT n frame=k field=T|B|F tick=t')")
    if not frames and not outs:
        raise Bad(f"{path}: the log has no FRAME or OUT lines")
    return frames, outs


def read_yuv(path, fsize):
    if not os.path.isfile(path):
        raise Bad(f"yuv file missing: {path}")
    raw = open(path, "rb").read()
    if len(raw) % fsize:
        raise Bad(f"{path}: size {len(raw)} is not a multiple of the frame size {fsize}")
    return [raw[i:i + fsize] for i in range(0, len(raw), fsize)]


def describe(t, i, a):
    """Why output i's pixels differ: first differing plane/position plus hints."""
    k, ch, tick = t.outs[i]
    e = t.expected_frames()[i]
    pos = next(j for j in range(len(e)) if e[j] != a[j])
    for name, pw, ph, off in bob_ref.planes_of(t.w, t.h):
        if off <= pos < off + pw * ph:
            y, x = divmod(pos - off, pw)
            break
    txt = f"{name} plane row {y} column {x}: expected {e[pos]}, got {a[pos]}"
    hints = []
    if ch in "TB":
        other = bob_ref.bob(t.src[k], t.w, t.h, "B" if ch == "T" else "T")
        if a == other:
            hints.append("this is the bob of the OTHER parity (field kept on the wrong lines)")
    if a == t.src[k]:
        hints.append("this is the unmodified source frame (no interpolation)")
    exp = t.expected_frames()
    for d in (-1, 1):
        if 0 <= i + d < len(exp) and a == exp[i + d]:
            hints.append(f"this equals the expected output {i + d} (order / repeat problem)")
    if ch in "TB" and not hints:
        par = 0 if ch == "T" else 1
        rows_ok = all(e[off + y2 * pw: off + (y2 + 1) * pw] == a[off + y2 * pw: off + (y2 + 1) * pw]
                      for _, pw, ph, off in bob_ref.planes_of(t.w, t.h)[:1] for y2 in range(par, ph, 2))
        hints.append("the kept rows of luma are right, the interpolated rows are wrong" if rows_ok
                     else "the kept (field) rows of luma are wrong too")
        for name2, pw, ph, off in bob_ref.planes_of(t.w, t.h)[1:]:
            if e[off:off + pw * ph] != a[off:off + pw * ph]:
                hints.append(f"{name2} differs")
    return f"output {i} (frame {k}, field {ch}, tick {tick}): {txt}" + ("  [" + "; ".join(hints) + "]" if hints else "")


def check_case(t, log_path, yuv_path, rep):
    """Compare one reader output with the truth; failures go to `rep`. Raises Bad for unusable files."""
    frames, outs = parse_log(log_path)
    yuvs = read_yuv(yuv_path, t.fsize)
    pre = f"{t.name}: "
    # FRAME lines
    exp_frames = []
    for k, l in enumerate(t.flines):
        m = FRAME_RE.match(l)
        exp_frames.append((int(m.group(1)), m.group(2), m.group(3), m.group(4)))
    rep.check(len(frames) == len(exp_frames), pre + f"expected {len(exp_frames)} FRAME lines, got {len(frames)}")
    for i, (e, a) in enumerate(zip(exp_frames, frames)):
        if e != a:
            diffs = [f"{n} expected {x}, got {y}" for n, x, y in zip(("k", "coded", "ps", "seq"), e, a) if x != y]
            rep.check(False, pre + f"FRAME line {i}: " + "; ".join(diffs))
        else:
            rep.check(True, "")
    # OUT lines
    exp_outs = [(n, k, ch, tick) for n, (k, ch, tick) in enumerate(t.outs)]
    rep.check(len(outs) == len(exp_outs), pre + f"expected {len(exp_outs)} OUT lines, got {len(outs)}")
    for i, (e, a) in enumerate(zip(exp_outs, outs)):
        if e != a:
            diffs = [f"{n} expected {x}, got {y}" for n, x, y in zip(("n", "frame", "field", "tick"), e, a) if x != y]
            rep.check(False, pre + f"OUT line {i}: " + "; ".join(diffs))
        else:
            rep.check(True, "")
    # pixels
    exp = t.expected_frames()
    rep.check(len(yuvs) == len(exp), pre + f"yuv has {len(yuvs)} frames, expected {len(exp)} (one per OUT line)")
    detailed = 0
    for i, (e, a) in enumerate(zip(exp, yuvs)):
        if e == a:
            rep.check(True, "")
        elif detailed < 4:
            detailed += 1
            rep.check(False, pre + describe(t, i, a))
        else:
            rep.check(False, pre + f"output {i}: pixels differ")


# ------------------------------------------------------------------------------------------------ modes
def case_files(directory, only):
    names = [n for n in mkstreams.case_names() if os.path.isfile(os.path.join(directory, n))]
    if only:
        names = [n for n in names if os.path.splitext(n)[0] in only]
    if not names:
        raise Bad(f"no test streams in {directory} (run --make first)")
    return names


def run_check(directory, outdir, only, work):
    rep = Report("deinterlacing for display")
    n = 0
    for fn in case_files(directory, only):
        base = os.path.splitext(fn)[0]
        t = Truth(os.path.join(directory, fn), work)
        for note in t.crosscheck():
            print(f"  note ({base}): {note}")
        check_case(t, os.path.join(outdir, base + ".log"), os.path.join(outdir, base + ".yuv"), rep)
        n += 1
    print(f"checked {n} stream(s)")
    rep.finish()


def run_reader(directory, cmd, only):
    out = os.path.join(directory, "out")
    os.makedirs(out, exist_ok=True)
    for fn in case_files(directory, only):
        base = os.path.splitext(fn)[0]
        argv = shlex.split(cmd.format(stream=shlex.quote(os.path.join(directory, fn)),
                                      log=shlex.quote(os.path.join(out, base + ".log")),
                                      yuv=shlex.quote(os.path.join(out, base + ".yuv"))))
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise Bad(f"your program timed out (300 s) on {fn}")
        except OSError as e:
            raise Bad(f"cannot run your program: {e}")
        if r.returncode != 0:
            raise Bad(f"your program exited with {r.returncode} on {fn}: {r.stderr.decode(errors='replace')[-300:]}")
    return out


# ------------------------------------------------------------------------------------------------ self-test
def good_output(t, mutant=None, kind=None):
    """What a (possibly wrong) reader would write: (log text, yuv bytes). Uses the reference modules with a MUTANT switch."""
    plan_ref.MUTANT = mutant if kind == "plan" else None
    bob_ref.MUTANT = mutant if kind == "bob" else None
    try:
        pics, _ = plan_ref.scan(None, t.blocks)
        stores = plan_ref.frames_of(pics)
        fl, ol, outs = plan_ref.plan(stores)
        yuv = b"".join(bob_ref.bob(t.src[min(k, len(t.src) - 1)], t.w, t.h, ch) for k, ch, _ in outs)
    finally:
        plan_ref.MUTANT = bob_ref.MUTANT = None
    return "\n".join(fl + ol) + "\n", yuv


def check_texts(t, log, yuv, tmp):
    lp, yp = os.path.join(tmp, "x.log"), os.path.join(tmp, "x.yuv")
    open(lp, "w").write(log)
    open(yp, "wb").write(yuv)
    rep = Report("selftest", max_shown=0)
    check_case(t, lp, yp, rep)
    return len(rep.failures)


def mmco5_unit():
    """Synthetic traces (no ffmpeg) for the memory_management_control_operation 5 branch of plan_ref (8.2.1). -> list of problems."""
    def sps(t):
        return ("Sequence Parameter Set", [("seq_parameter_set_id", 0), ("log2_max_frame_num_minus4", 0), ("pic_order_cnt_type", t),
                                           ("log2_max_pic_order_cnt_lsb_minus4", 0), ("frame_mbs_only_flag", 1),
                                           ("pic_struct_present_flag", 0)])

    def sl(idr, fn, lsb=None, dbot=None, mmco5=False):
        it = [("nal_unit_type", 5 if idr else 1), ("nal_ref_idc", 1), ("first_mb_in_slice", 0), ("pic_parameter_set_id", 0),
              ("frame_num", fn)]
        if lsb is not None:
            it.append(("pic_order_cnt_lsb", lsb))
        if dbot is not None:
            it.append(("delta_pic_order_cnt_bottom", dbot))
        if mmco5:
            it.append(("memory_management_control_operation", 5))
        return ("Slice Header", it)
    pps = ("Picture Parameter Set", [("pic_parameter_set_id", 0), ("seq_parameter_set_id", 0)])
    # POC type 0, MaxPicOrderCntLsb 16. Picture 2 (mmco5): raw POCs 13 / 12 -> 1 / 0 after subtracting PicOrderCnt = 12, so
    # prevPicOrderCntLsb = 1 for picture 3, whose lsb 9 then gives POC 9 (with prevLsb 0 it would be -7, with 13 it would be 25).
    # POC type 2, MaxFrameNum 16: picture 2 (mmco5) gets POC 0 and resets FrameNumOffset / frame_num, so picture 3 (frame_num 1) is 2.
    cases = [("type 0", [sps(0), pps, sl(1, 0, 0, 0), sl(0, 1, 8, 0), sl(0, 2, 13, -1, True), sl(0, 3, 9, 0)],
              [(0, 0), (8, 8), (1, 0), (9, 9)]),
             ("type 2", [sps(2), pps, sl(1, 0), sl(0, 1), sl(0, 2, mmco5=True), sl(0, 1)], [(0, 0), (2, 2), (0, 0), (2, 2)])]
    bad = []
    for label, blocks, want in cases:
        pics, _ = plan_ref.scan(None, blocks)
        got = [(p["top"], p["bot"]) for p in pics]
        if got != want:
            bad.append(f"mmco5 {label}: POCs {got}, expected {want}")
        order = [next(i for i, p in enumerate(pics) if p is s["fields"][0]) for s in plan_ref.frames_of(pics)]
        if order != list(range(len(pics))):                 # the mmco5 picture starts a new output period: no reordering here
            bad.append(f"mmco5 {label}: output order {order}, expected {list(range(len(pics)))}")
    return bad


def self_test():
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        suite = os.path.join(tmp, "suite")
        mkstreams.make(suite, tmp)
        cases = [Truth(os.path.join(suite, n), tmp) for n in case_files(suite, None)]
        print(f"suite: {len(cases)} streams")
        seqs = set()
        for t in cases:
            seqs.update(plan_ref.sequence(s) for s in t.stores)
        for need in ("F", "TB", "BT", "T", "B", "TBT", "BTB", "FF", "FFF"):
            if need not in seqs:
                print(f"FAIL: the suite never exercises display sequence {need}")
                ok = False
        # 1. known-good outputs pass; ffprobe cross-checks agree
        for t in cases:
            log, yuv = good_output(t)
            nf = check_texts(t, log, yuv, tmp)
            notes = t.crosscheck()
            if nf or notes:
                ok = False
                print(f"FAIL: good output of {t.name}: {nf} failure(s) {notes[:3]}")
        print("good outputs: all pass" if ok else "good outputs: PROBLEMS")
        # 2. wrong rules are caught
        mutants = [("plan", m) for m in plan_ref.MUTANTS] + [("bob", m) for m in bob_ref.MUTANTS]
        good = {t.name: good_output(t) for t in cases}
        caught = 0
        for kind, m in mutants:
            where = None
            for t in cases:
                log, yuv = good_output(t, m, kind)
                if (log, yuv) == good[t.name]:
                    continue                                   # this stream does not exercise the rule
                nf = check_texts(t, log, yuv, tmp)
                where = (t.name, nf)
                break
            if where is None:
                ok = False
                print(f"FAIL: mutant {kind}/{m} changes no stream of the suite (not exercised)")
            elif where[1] == 0:
                ok = False
                print(f"FAIL: mutant {kind}/{m} SURVIVED on {where[0]}")
            else:
                caught += 1
                print(f"  caught {kind}/{m:<24} on {where[0]} ({where[1]} failing checks)")
        print(f"mutants: {caught}/{len(mutants)} caught")
        for problem in mmco5_unit():
            ok = False
            print("FAIL:", problem)
        print("mmco5 unit test done")
        # 3. file-level problems: exit codes
        t = cases[0]
        log, yuv = good_output(t)
        probes = []

        def probe(label, log_text, yuv_bytes, want):
            nonlocal ok
            try:
                nf = check_texts(t, log_text, yuv_bytes, tmp)
                got = 1 if nf else 0
            except Bad:
                got = 2
            if got != want:
                ok = False
                print(f"FAIL: {label}: exit {got}, wanted {want}")
            else:
                probes.append(label)
        lines = log.splitlines()
        probe("good", log, yuv, 0)
        probe("yuv one frame short", log, yuv[:-t.fsize], 1)
        probe("yuv truncated mid-frame", log, yuv[:-5], 2)
        probe("yuv empty", log, b"", 1)
        probe("log garbage line", log + "hello world\n", yuv, 2)
        probe("log empty", "", yuv, 2)
        probe("log misspelt key", log.replace("tick=", "time=", 1), yuv, 2)
        probe("two OUT lines swapped", "\n".join(lines[:len(t.flines)] + [lines[len(t.flines) + 1], lines[len(t.flines)]]
                                              + lines[len(t.flines) + 2:]) + "\n", yuv, 1)
        probe("last OUT line missing", "\n".join(lines[:-1]) + "\n", yuv, 1)
        probe("tick off by one", log.replace("tick=3", "tick=4", 1), yuv, 1)
        # real command-line exit codes
        outdir = os.path.join(tmp, "out")
        os.makedirs(outdir)
        one = os.path.join(tmp, "one")
        os.makedirs(one)
        import shutil
        shutil.copy(os.path.join(suite, t.name + os.path.splitext(t.path)[1]), one)
        script = os.path.abspath(__file__)
        open(os.path.join(outdir, t.name + ".log"), "w").write(log)
        open(os.path.join(outdir, t.name + ".yuv"), "wb").write(yuv)
        for label, want in (("cli good", 0), ("cli wrong", 1), ("cli malformed", 2)):
            if label == "cli wrong":
                open(os.path.join(outdir, t.name + ".yuv"), "wb").write(yuv[:-t.fsize])
            if label == "cli malformed":
                open(os.path.join(outdir, t.name + ".log"), "w").write("nonsense\n")
            r = subprocess.run([sys.executable, script, "--check", one, outdir], stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=120)
            if r.returncode != want:
                ok = False
                print(f"FAIL: {label}: exit {r.returncode}, wanted {want}: {r.stdout.decode()[-200:]} {r.stderr.decode()[-200:]}")
            else:
                probes.append(label)
        print(f"file-level probes ok: {len(probes)}")
    print("SELF-TEST PASS" if ok else "SELF-TEST FAIL")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make", metavar="DIR")
    ap.add_argument("--check", nargs=2, metavar=("DIR", "OUTDIR"))
    ap.add_argument("--suite", metavar="DIR")
    ap.add_argument("--reader")
    ap.add_argument("--stream")
    ap.add_argument("--log")
    ap.add_argument("--yuv")
    ap.add_argument("--show-expected", metavar="STREAM")
    ap.add_argument("--only")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    try:
        if a.self_test:
            self_test()
        elif a.make:
            with tempfile.TemporaryDirectory() as tmp:
                names = mkstreams.make(a.make, tmp)
            print(f"wrote {len(names)} streams to {a.make}: " + " ".join(names))
        elif a.show_expected:
            with tempfile.TemporaryDirectory() as tmp:
                t = Truth(a.show_expected, tmp)
                print(f"# {t.w}x{t.h}, {len(t.stores)} frames, {len(t.outs)} outputs")
                print("\n".join(t.flines + t.olines))
        elif a.stream:
            if not a.log or not a.yuv:
                raise Bad("--stream needs --log and --yuv")
            with tempfile.TemporaryDirectory() as tmp:
                t = Truth(a.stream, tmp)
                for note in t.crosscheck():
                    print("  note:", note)
                rep = Report("deinterlacing for display")
                check_case(t, a.log, a.yuv, rep)
                rep.finish()
        elif a.check or a.suite:
            with tempfile.TemporaryDirectory() as tmp:
                if a.suite:
                    if not a.reader:
                        raise Bad("--suite needs --reader 'CMD {stream} {log} {yuv}'")
                    mkstreams.make(a.suite, tmp)
                    out = run_reader(a.suite, a.reader, only)
                    run_check(a.suite, out, only, tmp)
                else:
                    run_check(a.check[0], a.check[1], only, tmp)
        else:
            ap.print_help()
            return 2
    except Bad as e:
        print(f"error: {e}")
        sys.exit(2)
    except subprocess.TimeoutExpired as e:
        print(f"error: a helper tool timed out: {e}")
        sys.exit(2)


if __name__ == "__main__":
    sys.exit(main())
