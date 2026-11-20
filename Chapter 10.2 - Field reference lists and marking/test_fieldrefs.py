#!/usr/bin/env python3
"""Chapter 10.2 harness: reference lists, marking, direct prediction and weights for P / B FIELD pictures (PAFF).
Python 3 standard library only (+ ffmpeg).

Your program reads an Annex B .264 stream and writes (A) a text dump (pictures, final reference lists of every P / B slice, the
reference marking state after every picture) and (B) the decoded frames as raw yuv420p (80x96, no cropping, output order).

  python3 test_fieldrefs.py --make DIR                    write the synthetic suite (DIR/<case>.264) for your program to read
  python3 test_fieldrefs.py --check DIR OUTDIR            check OUTDIR/<case>.dump and OUTDIR/<case>.yuv for every case
  python3 test_fieldrefs.py --suite DIR --reader 'CMD {stream} {dump} {yuv}'
                                                          --make, then run CMD for every case (stdin closed, 120 s timeout), then --check
  python3 test_fieldrefs.py --truth DIR                   write DIR/<case>.expected.dump / .expected.yuv (what --check compares with)
  python3 test_fieldrefs.py --stream X.264 --yuv mine.yuv ad-hoc pixel check of ANY stream against ffmpeg (-threads 1)
  python3 test_fieldrefs.py --list                        the cases
  options: --only a,b   restrict to some cases     --no-dump / --no-yuv   check one channel only
  python3 test_fieldrefs.py --self-test                   verify this harness (a few minutes): good outputs pass, wrong rules are caught

Exit codes: 0 pass, 1 fail, 2 missing / malformed input.
"""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles, yuvcmp  # noqa: E402
from common.report import Report  # noqa: E402
import direct_ref  # noqa: E402
import fbs  # noqa: E402
import fdpb  # noqa: E402
import fpdec  # noqa: E402
import fpgen  # noqa: E402

CACHE = os.path.join(testfiles.CACHE, "ch10_2")
W, H = 16 * 5, 16 * 6
FRAME_BYTES = W * H * 3 // 2


class Bad(Exception):
    """Unusable input: exit 2."""


def source_key():
    h = hashlib.sha1()
    for fn in ("fpgen.py", "fdpb.py", "fpdec.py", "direct_ref.py", "dmvpred_ref.py", "fbs.py", "interp_ref.py", "intraref.py",
               "deblock_ref.py", "transref.py"):
        h.update(open(os.path.join(HERE, "spoiler_reference", fn), "rb").read())
    return h.hexdigest()[:12]


# ------------------------------------------------------------------------------------------------ truth
class Case:
    def __init__(self, name):
        self.name = name
        self.sc = fpgen.SCENARIOS[name]
        key = source_key()
        os.makedirs(CACHE, exist_ok=True)
        self.base = os.path.join(CACHE, f"{name}.{key}")
        if not os.path.exists(self.base + ".264.json"):
            fpgen.write_stream(name, self.base + ".264")
        self.data = open(self.base + ".264", "rb").read()
        self.man = json.load(open(self.base + ".264.json"))
        self._ref = None
        self._ff = None
        self._lines = None

    def lines(self):
        """The expected dump lines (reference decoder, no sample work)."""
        if self._lines is None:
            self._lines = fpdec.decode(self.man, pixels=False).lines
        return self._lines

    def reference(self):
        if self._ref is None:
            self._ref = fpdec.decode(self.man)
        return self._ref

    def ffmpeg_frames(self):
        if self._ff is None:
            out = self.base + ".ff.yuv"
            if not os.path.exists(out):
                run_ffmpeg(self.base + ".264", out)
            self._ff = split_frames(open(out, "rb").read())
        return self._ff

    def expected_frames(self):
        """The truth for the pixel channel: ffmpeg, or (ffmpeg_ok = False) the reference decoder."""
        if self.sc.get("ffmpeg_ok", True):
            return self.ffmpeg_frames()
        return [b for _, b in self.reference().frames()]


def run_ffmpeg(stream, out):
    p = ffref.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-y", "-i", stream, "-fps_mode", "passthrough",
                   "-pix_fmt", "yuv420p", "-f", "rawvideo", out], timeout=120)
    if p.returncode != 0 or not os.path.exists(out):
        raise Bad(f"ffmpeg failed on {stream}: {p.stderr.decode()[-300:]}")


def split_frames(raw):
    if len(raw) % FRAME_BYTES:
        raise Bad(f"yuv size {len(raw)} is not a whole number of {W}x{H} yuv420p frames ({FRAME_BYTES} bytes)")
    return [raw[i:i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]


def cases(only=None):
    ffref.require_tools()
    names = list(fpgen.SCENARIOS)
    if only:
        bad = [o for o in only if o not in names]
        if bad:
            raise Bad(f"unknown case(s) {bad}; --list shows them")
        names = [n for n in names if n in only]
    return [Case(n) for n in names]


# ------------------------------------------------------------------------------------------------ dump channel
def parse_dump(text, label):
    """-> dict[(kind, n, slice or None)] = (line number, {key: value})"""
    out = {}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] not in ("PIC", "LIST", "DPB") or len(parts) < 2 or not parts[1].isdigit():
            raise Bad(f"{label} line {no}: expected 'PIC|LIST|DPB <picture number> key=value ...', got {line[:70]!r}")
        kv = {}
        for tok in parts[2:]:
            if "=" not in tok:
                raise Bad(f"{label} line {no}: token {tok!r} is not key=value")
            k, v = tok.split("=", 1)
            kv[k] = v
        key = (parts[0], int(parts[1]), int(kv["slice"]) if parts[0] == "LIST" and kv.get("slice", "").isdigit() else None)
        if parts[0] == "LIST" and key[2] is None:
            raise Bad(f"{label} line {no}: LIST line without slice=<k>")
        for lk in ("l0", "l1") if parts[0] == "LIST" else ():
            if lk in kv and not all(t == "-" or (t[:-1].isdigit() and t[-1] in "TBF") for t in kv[lk].split(",")):
                raise Bad(f"{label} line {no}: {lk}={kv[lk][:40]} is not a comma-separated list of <store><T|B|F> or - tokens")
        if key in out:
            raise Bad(f"{label} line {no}: second line for {parts[0]} {parts[1]}" + (f" slice {key[2]}" if key[2] is not None else "")
                      + f" (first on line {out[key][0]})")
        out[key] = (no, kv)
    return out


def picinfo(case_lines, n):
    for l in case_lines:
        if l.startswith(f"PIC {n} "):
            return " ".join(t for t in l.split()[2:] if t.split("=")[0] in ("struct", "frame_num", "idr", "ref", "store", "second", "poc"))
    return ""


def compare_dump(rep, case, actual_text):
    exp = parse_dump("\n".join(case.lines()), "expected")
    act = parse_dump(actual_text, f"{case.name}.dump")
    n_missing = 0
    for key, (_, ekv) in exp.items():
        kind, n, sl = key
        where = f"{case.name} {kind} {n}" + (f" slice {sl}" if sl is not None else "")
        if key not in act:
            n_missing += 1
            if n_missing <= 3:
                rep.check(False, f"{where}: line missing in your dump   [{picinfo(case.lines(), n)}]")
            continue
        ln, akv = act[key]
        for k, ev in ekv.items():
            if k == "slice":
                continue
            av = akv.get(k)
            if av is None and kind == "LIST" and k == "l1" and ev == "-":
                continue
            ok = av == ev
            msg = f"{where} (your line {ln}): {k} expected {ev}, got {av if av is not None else '<missing>'}   [{picinfo(case.lines(), n)}]"
            if not ok and kind == "LIST" and av is not None:
                e, a = ev.split(","), av.split(",")
                diff = [i for i in range(max(len(e), len(a))) if (e[i] if i < len(e) else None) != (a[i] if i < len(a) else None)]
                msg += f"\n      first differing list index: {diff[0]} (expected {e[diff[0]] if diff[0] < len(e) else 'nothing'}, got {a[diff[0]] if diff[0] < len(a) else 'nothing'})"
            rep.check(ok, msg)
    if n_missing > 3:
        rep.check(False, f"{case.name}: {n_missing} expected lines are missing in total")
    extra = [k for k in act if k not in exp]
    rep.check(not extra, f"{case.name}: your dump has lines that do not exist in the stream: {sorted(extra)[:3]}")


# ------------------------------------------------------------------------------------------------ pixel channel
def frame_hint(case, k):
    """Which pictures made output frame k."""
    d = case.reference()
    ids = [i for i, _ in d.frames()]
    if k >= len(ids):
        return ""
    sid = ids[k]
    pics = [p for p in case.man["pics"] if p["store"] == sid]
    kind = {"F": "frame", "T": "top field", "B": "bottom field"}
    desc = " + ".join(f"PIC {p['n']} ({kind.get(p['struct'], p['struct'])}, {'IDR ' if p['idr'] else ''}{'ref' if p['ref_idc'] else 'non-ref'})" for p in pics)
    return f"output frame {k} = store {sid}: {desc}"


def compare_pixels(rep, case, yuv_path):
    raw = open(yuv_path, "rb").read()
    exp = case.expected_frames()
    if len(raw) % FRAME_BYTES:
        raise Bad(f"{yuv_path} has {len(raw)} bytes, not a whole number of {W}x{H} yuv420p frames ({FRAME_BYTES} bytes each)")
    ep = os.path.join(tempfile.gettempdir(), f"ch10_2_{os.getpid()}_{case.name}.expected.yuv")
    try:
        with open(ep, "wb") as f:
            f.write(b"".join(exp))
        res = yuvcmp.compare_yuv(ep, yuv_path, W, H, quiet=True)
    finally:
        if os.path.exists(ep):
            os.remove(ep)
    if res["ok"]:
        rep.check(True, "")
        return
    hint = ""
    fd = res["first_diff"]
    if fd is not None:
        hint = f"\n      {frame_hint(case, fd['frame'])}; first difference on {fd['plane']} line {fd['y']} = line {fd['y'] // 2} of the " \
               f"{'bottom' if fd['y'] & 1 else 'top'} field"
    src = "ffmpeg" if case.sc.get("ffmpeg_ok", True) else "the reference decoder (ffmpeg decodes this case differently, see the Readme)"
    rep.check(False, f"{case.name}: pixels differ from {src} ({W}x{H} frames, output order)\n" + "\n".join(
        "      " + ln for ln in res["text"].splitlines()[:14]) + hint)


def check_case(rep, case, dump_path, yuv_path, use_dump, use_yuv):
    if use_dump:
        if not os.path.exists(dump_path):
            raise Bad(f"missing {dump_path}")
        compare_dump(rep, case, open(dump_path).read())
    if use_yuv:
        if not os.path.exists(yuv_path):
            raise Bad(f"missing {yuv_path}")
        compare_pixels(rep, case, yuv_path)


# ------------------------------------------------------------------------------------------------ commands
def make_suite(directory, only=None):
    os.makedirs(directory, exist_ok=True)
    cs = cases(only)
    for c in cs:
        with open(os.path.join(directory, c.name + ".264"), "wb") as f:
            f.write(c.data)
    return cs


def run_check(streams_dir, out_dir, only, use_dump, use_yuv):
    try:
        cs = cases(only)
        rep = Report("Chapter 10.2 field reference lists and marking")
        for c in cs:
            p = os.path.join(streams_dir, c.name + ".264")
            if os.path.exists(p) and open(p, "rb").read() != c.data:
                raise Bad(f"{p} differs from the suite this harness generates; rewrite it with --make")
            check_case(rep, c, os.path.join(out_dir, c.name + ".dump"), os.path.join(out_dir, c.name + ".yuv"), use_dump, use_yuv)
    except Bad as e:
        print("error:", e)
        return 2
    rep.finish()


def run_suite(directory, reader, only, use_dump, use_yuv):
    try:
        cs = make_suite(directory, only)
    except Bad as e:
        print("error:", e)
        return 2
    for c in cs:
        s, d, y = (os.path.join(directory, c.name + ext) for ext in (".264", ".dump", ".yuv"))
        for p in (d, y):
            if os.path.exists(p):
                os.remove(p)
        cmd = [x.format(stream=s, dump=d, yuv=y) for x in shlex.split(reader)]
        try:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            print(f"error: reader timed out on {c.name}")
            return 2
        except OSError as e:
            print(f"error: cannot run the reader: {e}")
            return 2
    return run_check(directory, directory, only, use_dump, use_yuv)


def run_truth(directory, only):
    try:
        os.makedirs(directory, exist_ok=True)
        for c in cases(only):
            with open(os.path.join(directory, c.name + ".expected.dump"), "w") as f:
                f.write("\n".join(c.lines()) + "\n")
            with open(os.path.join(directory, c.name + ".expected.yuv"), "wb") as f:
                f.write(b"".join(c.expected_frames()))
            print("wrote", c.name)
    except Bad as e:
        print("error:", e)
        return 2
    return 0


def run_adhoc(stream, yuv):
    if not os.path.exists(stream) or not os.path.exists(yuv):
        print("error: stream or yuv file missing")
        return 2
    tmp = tempfile.mkdtemp(prefix="fieldrefs_")
    try:
        ff = os.path.join(tmp, "ff.yuv")
        try:
            run_ffmpeg(stream, ff)
        except Bad as e:
            print("error:", e)
            return 2
        info = ffref.probe_json(stream, "-show_streams", "-select_streams", "v:0")
        s = (info.get("streams") or [{}])[0]
        w, h = int(s.get("width", 0)), int(s.get("height", 0))
        if not w:
            print("error: ffmpeg could not read the stream")
            return 2
        print(f"comparing {w}x{h} frames: expected {os.path.getsize(ff) // (w * h * 3 // 2)}, yours {os.path.getsize(yuv) / (w * h * 1.5):g}")
        r = yuvcmp.compare_yuv(ff, yuv, w, h, quiet=True)
        print(r["text"])
        return 0 if r["ok"] else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ self test
class Stop(Exception):
    pass


def reader_files(directory, case, lines, frames):
    with open(os.path.join(directory, case.name + ".dump"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(directory, case.name + ".yuv"), "wb") as f:
        f.write(b"".join(frames))


def rejects(case, lines, frames, use_dump=True, use_yuv=True):
    d = tempfile.mkdtemp(prefix="reader_")
    try:
        reader_files(d, case, lines, frames)
        rep = Report("x")
        try:
            check_case(rep, case, os.path.join(d, case.name + ".dump"), os.path.join(d, case.name + ".yuv"), use_dump, use_yuv)
        except Bad:
            return True
        return bool(rep.failures)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def try_mutant(case, mutant, pixels_only=False):
    """-> 'dump' | 'pixel' | 'crash' | None: how this wrong rule shows up on this case (pixels_only: skip the dump comparison)."""
    exp_lines = case.lines()
    exp_frames = case.expected_frames()
    errs = (fpdec.Unsupported, direct_ref.DirectInvalid, fdpb.StreamError, AttributeError, IndexError, TypeError, KeyError, ValueError)
    fpdec.MUTANT = mutant
    try:
        if not pixels_only:
            try:
                lines = fpdec.decode(case.man, pixels=False).lines
            except errs:
                return "crash"
            if lines != exp_lines:
                return "dump"
        order = {sid: k for k, (sid, _) in enumerate(case.reference().frames())}
        dec = None

        def hook(store, n):
            k = order.get(store.id)
            if k is not None and k < len(exp_frames) and dec.frame_bytes(store) != exp_frames[k]:
                raise Stop()
        try:
            dec = fpdec.Decoder(case.man, True, hook)
            dec.run()
        except Stop:
            return "pixel"
        except errs:
            return "crash"
        if [b for _, b in dec.frames()] != exp_frames:
            return "pixel"
        return None
    finally:
        fpdec.MUTANT = None


def self_test():
    t0 = time.time()
    ok_all = True

    def verdict(ok, text):
        nonlocal ok_all
        ok_all &= bool(ok)
        print(("  ok    " if ok else "  FAIL  ") + text)

    cs = cases()
    print(f"{len(cs)} cases; stream sizes {min(len(c.data) for c in cs)}..{max(len(c.data) for c in cs)} bytes")
    print("1. the reference decoder (spoiler_reference) against ffmpeg -threads 1 on every case whose truth is ffmpeg")
    for c in cs:
        ref = [b for _, b in c.reference().frames()]
        if c.sc.get("ffmpeg_ok", True):
            ff = c.ffmpeg_frames()
            bad = [k for k, (a, b) in enumerate(zip(ref, ff)) if a != b]
            verdict(len(ref) == len(ff) and not bad, f"{c.name}: {len(ref)} frames identical to ffmpeg" if not bad and len(ref) == len(ff)
                    else f"{c.name}: frames differ from ffmpeg: count {len(ref)} vs {len(ff)}, first bad {bad[:3]}")
        else:
            ff = c.ffmpeg_frames()
            n_diff = sum(1 for a, b in zip(ref, ff) if a != b)
            verdict(n_diff > 0, f"{c.name}: truth = reference decoder; ffmpeg differs in {n_diff} of {len(ref)} frames (documented deviation)")
    print("2. a correct reader (the reference decoder's own output) passes; corrupted readers fail")
    for c in cs:
        frames = c.expected_frames()
        verdict(not rejects(c, c.lines(), frames), f"{c.name}: correct dump + yuv accepted")
    c = next(x for x in cs if x.name == "b_fields")
    lines, frames = list(c.lines()), c.expected_frames()
    li = next(i for i, l in enumerate(lines) if l.startswith("LIST") and "type=B" in l and l.split("l0=")[1].split()[0].count(",") >= 2)
    toks = lines[li].split()
    l0 = [t for t in toks if t.startswith("l0=")][0][3:].split(",")
    l0[0], l0[1] = l0[1], l0[0]
    bad = list(lines)
    bad[li] = " ".join(t if not t.startswith("l0=") else "l0=" + ",".join(l0) for t in toks)
    verdict(rejects(c, bad, frames), "two entries of a reference list swapped")
    di = next(i for i, l in enumerate(lines) if l.startswith("DPB") and ":SS:" in l)
    bad = list(lines)
    bad[di] = lines[di].replace(":SS:", ":SL:", 1)
    verdict(rejects(c, bad, frames), "a marking state S changed to L")
    bad = [l for i, l in enumerate(lines) if i != li]
    verdict(rejects(c, bad, frames), "a LIST line missing")
    bad = [l.replace("second=1", "second=0", 1) if i == next(j for j, x in enumerate(lines) if "second=1" in x) else l for i, l in enumerate(lines)]
    verdict(rejects(c, bad, frames), "a PIC line with the wrong second= value")
    fl = list(frames)
    fl[3] = bytes([fl[3][0] ^ 1]) + fl[3][1:]
    verdict(rejects(c, lines, fl), "one luma sample of frame 3 changed")
    fl = list(frames)
    fl[2], fl[3] = fl[3], fl[2]
    verdict(rejects(c, lines, fl), "two output frames swapped")
    verdict(rejects(c, lines, frames[:-1]), "last frame missing")
    ln = len(frames[0]) * 2 // 3
    fl = list(frames)
    fl[4] = fl[4][:ln] + bytes([fl[4][ln] ^ 8]) + fl[4][ln + 1:]
    verdict(rejects(c, lines, fl), "one chroma sample of frame 4 changed")
    verdict(not rejects(c, lines, frames[:], use_dump=False) and rejects(c, lines, frames[:-1], use_dump=False), "--no-dump checks pixels only")
    print("3. wrong rules in the reference decoder (each must change the dump or the pixels of at least one case)")
    mutants = {}
    mutants.update({k: ("lists / marking", v) for k, v in fdpb.MUTANTS.items()})
    mutants.update({k: ("sample / direct rules", v) for k, v in fpdec.MUTANTS.items()})
    mutants.update({k: ("deblocking", v) for k, v in fbs.MUTANTS.items() if k.startswith("field_")})
    n_caught = 0
    for name, (group, text) in mutants.items():
        found = {}
        for c in cs:
            r = try_mutant(c, name)
            if r:
                found["dump" if r in ("dump", "crash") else r] = c.name
                break
        if "dump" in found or "crash" in found:
            for c in cs:                       # does it also change the decoded pictures?  (dump-only wrong rules are fine too)
                r = try_mutant(c, name, pixels_only=True)
                if r == "pixel":
                    found["pixel"] = c.name
                    break
        caught = bool(found)
        n_caught += caught
        how = ", ".join(f"{k}: {v}" for k, v in found.items()) or "NOT CAUGHT"
        verdict(caught, f"{name:26s} [{group}] -> {how}")
    print(f"   {n_caught} of {len(mutants)} wrong rules caught")
    print(f"self-test {'PASSED' if ok_all else 'FAILED'} in {time.time() - t0:.0f} s")
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make", metavar="DIR")
    ap.add_argument("--check", nargs=2, metavar=("DIR", "OUTDIR"))
    ap.add_argument("--suite", metavar="DIR")
    ap.add_argument("--reader")
    ap.add_argument("--truth", metavar="DIR")
    ap.add_argument("--stream")
    ap.add_argument("--yuv")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--no-dump", action="store_true")
    ap.add_argument("--no-yuv", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    only = a.only.split(",") if a.only else None
    try:
        if a.self_test:
            return self_test()
        if a.list:
            for n, sc in fpgen.SCENARIOS.items():
                print(f"{n:14s} units={sc['units']} refs={sc['refs']} maxb={sc['maxb']} pairs={sc['p_pair']}" + ("" if sc.get("ffmpeg_ok", True) else "  (truth = reference decoder)"))
            return 0
        if a.make:
            cs = make_suite(a.make, only)
            print(f"wrote {len(cs)} streams to {a.make}")
            return 0
        if a.check:
            return run_check(a.check[0], a.check[1], only, not a.no_dump, not a.no_yuv)
        if a.suite:
            if not a.reader:
                print("error: --suite needs --reader 'CMD {stream} {dump} {yuv}'")
                return 2
            return run_suite(a.suite, a.reader, only, not a.no_dump, not a.no_yuv)
        if a.truth:
            return run_truth(a.truth, only)
        if a.stream:
            if not a.yuv:
                print("error: --stream needs --yuv")
                return 2
            return run_adhoc(a.stream, a.yuv)
    except Bad as e:
        print("error:", e)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
