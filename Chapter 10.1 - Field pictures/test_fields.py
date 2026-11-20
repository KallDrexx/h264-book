#!/usr/bin/env python3
"""Chapter 10.1 harness: field pictures (PAFF, intra only).  Python 3 standard library only (+ ffmpeg / ffprobe).

Your program reads an Annex B .264 stream and writes (A) a slice-level dump and (B) the decoded frames as cropped yuv420p.

  python3 test_fields.py --make DIR                      write the synthetic suite (DIR/<case>.264) for your program to read
  python3 test_fields.py --check DIR OUTDIR              check OUTDIR/<case>.dump and OUTDIR/<case>.yuv for every case
  python3 test_fields.py --suite DIR --reader 'CMD {stream} {dump} {yuv}'
                                                         --make, then run CMD for every case (stdin closed, 120 s timeout), then --check
  python3 test_fields.py --stream X.264 --yuv out.yuv [--expected-yuv ref.yuv] [--ignore-frames 3,7]
                                                         ad-hoc pixel check of ANY stream (e.g. a conformance stream); the truth is
                                                         ffmpeg's decode unless --expected-yuv (the suite's .yuv) is given.
                                                         --ignore-frames drops those frames (0-based) from YOUR file first (ffmpeg
                                                         does not output lone fields, see the Readme)
  options: --only a,b   restrict to some cases       --no-dump / --no-yuv   check one channel only
  python3 test_fields.py --self-test                     verify this harness: good outputs pass, wrong rules are caught

Exit codes: 0 pass, 1 fail, 2 missing / malformed input.
"""
import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import fgen  # noqa: E402
import fdec  # noqa: E402

CACHE = os.path.join(testfiles.CACHE, "ch10_1")


class Bad(Exception):
    """Unusable input: exit 2."""


# ------------------------------------------------------------------------------------------------ truth
class Case:
    def __init__(self, name, scenario, mutant=None):
        self.name = name
        self.data, self.man = fgen.build(scenario, mutant)
        self.expected = fgen.expected_dump(self.man)          # truth by construction
        self.ref = None                                      # fdec result (computed on demand)
        self._ff = None

    def reference(self):
        if self.ref is None:
            self.ref = fdec.decode(self.man)
        return self.ref

    def ffmpeg_frames(self, tmp):
        """ffmpeg's decode of the stream: list of frame byte strings (output order) and (w, h)."""
        if self._ff is None:
            s, y = os.path.join(tmp, self.name + ".264"), os.path.join(tmp, self.name + ".ff.yuv")
            with open(s, "wb") as f:
                f.write(self.data)
            yuvref.reference_yuv(s, y)
            info = yuvref.stream_info(s)
            fb = yuvref.frame_bytes(info["width"], info["height"])
            raw = open(y, "rb").read()
            if len(raw) % fb:
                raise Bad(f"{self.name}: ffmpeg output is not a whole number of frames")
            self._ff = ([raw[i:i + fb] for i in range(0, len(raw), fb)], (info["width"], info["height"]))
        return self._ff


def cases(only=None):
    out = []
    for name, sc in fgen.scenarios().items():
        if only is None or name in only:
            out.append(Case(name, sc))
    return out


def is_lone(frame):
    return len(frame["order"]) == 1 and frame["order"] != "F"


def splice_missing_lines(frame_bytes, other_bytes, size, y0, present):
    """Copy, from other_bytes, the lines of the field that is NOT present in a lone-field frame (they are unspecified)."""
    w, h = size
    cw, ch = (w + 1) // 2, (h + 1) // 2
    out = bytearray(frame_bytes)
    planes = [(0, w, h, y0), (w * h, cw, ch, y0 // 2), (w * h + cw * ch, cw, ch, y0 // 2)]
    for off, pw, ph, base in planes:
        for r in range(ph):
            if (base + r) & 1 != present:
                out[off + r * pw: off + (r + 1) * pw] = other_bytes[off + r * pw: off + (r + 1) * pw]
    return bytes(out)


def expected_yuv(case, reader_yuv_bytes, tmp):
    """The expected file: ffmpeg's frames for frames and complementary pairs; for lone fields (ffmpeg drops them) the
    reference decoder's frame, whose missing field lines are replaced by yours (unspecified content, never compared)."""
    dump, frames = case.reference()
    ffs, (w, h) = case.ffmpeg_frames(tmp)
    sc = case.man["sc"]
    y0 = 4 * sc["crop"][2]
    fb = yuvref.frame_bytes(w, h)
    rd = [reader_yuv_bytes[i:i + fb] for i in range(0, len(reader_yuv_bytes), fb)]
    it = iter(ffs)
    out = []
    for k, fr in enumerate(frames):
        if is_lone(fr):
            other = rd[k] if k < len(rd) and len(rd[k]) == fb else fr["bytes"]
            out.append(splice_missing_lines(fr["bytes"], other, (w, h), y0, 1 if fr["order"] == "B" else 0))
        else:
            out.append(next(it))
    rest = list(it)
    if rest:
        raise Bad(f"{case.name}: internal error, ffmpeg produced {len(rest)} frames more than the construction predicts")
    return b"".join(out), (w, h), (sc["crop"][0] * 2, y0)


# ------------------------------------------------------------------------------------------------ dump channel
def parse_dump(text, label):
    pics, frames = [], []
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] not in ("PIC", "FRAME") or len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            raise Bad(f"{label} line {no}: expected 'PIC <n> key=value ...' or 'FRAME <k> key=value ...', got {line[:60]!r}")
        kv = {}
        for tok in parts[2:]:
            if "=" not in tok:
                raise Bad(f"{label} line {no}: token {tok!r} is not key=value")
            k, v = tok.split("=", 1)
            kv[k] = v
        (pics if parts[0] == "PIC" else frames).append((no, int(parts[1]), kv))
    return pics, frames


def compare_dump(rep, name, expected_lines, actual_text):
    ep, ef = parse_dump("\n".join(expected_lines), "expected")
    ap, af = parse_dump(actual_text, f"{name}.dump")
    for kind, e, a in (("PIC", ep, ap), ("FRAME", ef, af)):
        rep.check(len(e) == len(a), f"{name}: expected {len(e)} {kind} lines, your dump has {len(a)}")
        for i, ((_, en, ekv), (ln, an, akv)) in enumerate(zip(e, a)):
            rep.check(en == an, f"{name} {kind} line {ln}: index expected {en}, got {an}")
            for key, ev in ekv.items():
                rep.check(akv.get(key) == ev,
                          f"{name} {kind} {en} (your line {ln}): {key} expected {ev}, got {akv.get(key, '<missing>')}"
                          + (f"   [struct={ekv['struct']} frame_num={ekv['frame_num']}]" if kind == "PIC" else f"   [fields={ekv['fields']}]"))


# ------------------------------------------------------------------------------------------------ pixel channel
def compare_pixels(rep, case, yuv_path, tmp, label=None):
    label = label or case.name
    raw = open(yuv_path, "rb").read()
    exp, (w, h), origin = expected_yuv(case, raw, tmp)
    ep = os.path.join(tmp, case.name + ".expected.yuv")
    with open(ep, "wb") as f:
        f.write(exp)
    res = yuvcmp.compare_yuv(ep, yuv_path, w, h, quiet=True, origin=origin)
    if res["ok"]:
        rep.check(True, "")
    else:
        hint = ""
        fd = res["first_diff"]
        frames = case.reference()[1]
        if fd is not None and fd["frame"] < len(frames):
            kind = frames[fd["frame"]]["order"]
            line = fd["y"] + (origin[1] if fd["plane"] == "Y" else origin[1] // 2)    # line in the uncropped plane
            what = {"F": "a frame picture", "TB": "a top-first field pair", "BT": "a bottom-first field pair",
                    "T": "a lone top field", "B": "a lone bottom field"}[kind]
            hint = f"\n      frame {fd['frame']} is {what}"
            if kind != "F":
                hint += (f"; the first difference is on uncropped {fd['plane']} line {line} = line {line // 2} of the "
                         f"{'bottom' if line & 1 else 'top'} field")
        rep.check(False, f"{label}: pixels differ ({w}x{h} frames, frame order = output order)\n" + "\n".join(
            "      " + ln for ln in res["text"].splitlines()[:14]) + hint)


def check_case(rep, case, dump_path, yuv_path, use_dump, use_yuv, tmp):
    if use_dump:
        if not os.path.exists(dump_path):
            raise Bad(f"missing {dump_path}")
        compare_dump(rep, case.name, case.expected, open(dump_path).read())
    if use_yuv:
        if not os.path.exists(yuv_path):
            raise Bad(f"missing {yuv_path}")
        compare_pixels(rep, case, yuv_path, tmp)


# ------------------------------------------------------------------------------------------------ commands
def make_suite(directory, only=None):
    os.makedirs(directory, exist_ok=True)
    cs = cases(only)
    for c in cs:
        with open(os.path.join(directory, c.name + ".264"), "wb") as f:
            f.write(c.data)
    return cs


def run_check(streams_dir, out_dir, only, use_dump, use_yuv):
    cs = cases(only)
    tmp = tempfile.mkdtemp(prefix="fields_")
    rep = Report("Chapter 10.1 field pictures")
    try:
        for c in cs:
            p = os.path.join(streams_dir, c.name + ".264")
            if os.path.exists(p) and open(p, "rb").read() != c.data:
                raise Bad(f"{p} differs from the suite this harness generates; rewrite it with --make")
            check_case(rep, c, os.path.join(out_dir, c.name + ".dump"), os.path.join(out_dir, c.name + ".yuv"), use_dump, use_yuv, tmp)
    except Bad as e:
        print("error:", e)
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rep.finish()


def run_suite(directory, reader, only, use_dump, use_yuv):
    cs = make_suite(directory, only)
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
    return run_check(directory, directory, only, use_dump, use_yuv)


def run_adhoc(stream, yuv, expected, ignore):
    tmp = tempfile.mkdtemp(prefix="fields_")
    try:
        if not os.path.exists(stream) or not os.path.exists(yuv):
            print("error: stream or yuv file missing")
            return 2
        info = yuvref.stream_info(stream)
        w, h = info["width"], info["height"]
        if not w:
            print("error: ffmpeg could not read the stream")
            return 2
        exp = expected
        if exp is not None and not os.path.exists(exp):
            print(f"error: missing {exp}")
            return 2
        if exp is None:
            exp = os.path.join(tmp, "ff.yuv")
            yuvref.reference_yuv(stream, exp)
        fb = yuvref.frame_bytes(w, h)
        raw = open(yuv, "rb").read()
        if ignore:
            try:
                drop = {int(x) for x in ignore.split(",")}
            except ValueError:
                print(f"error: --ignore-frames wants comma-separated 0-based frame numbers, got {ignore!r}")
                return 2
            raw = b"".join(raw[i:i + fb] for i in range(0, len(raw), fb) if i // fb not in drop)
            yuv = os.path.join(tmp, "yours.yuv")
            open(yuv, "wb").write(raw)
        print(f"comparing {w}x{h} frames: expected {os.path.getsize(exp) // fb}, yours {len(raw) / fb:g}")
        r = yuvcmp.compare_yuv(exp, yuv, w, h, quiet=True)
        print(r["text"])
        return 0 if r["ok"] else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ self test
def write_reader_files(directory, case, dump_lines, frames):
    with open(os.path.join(directory, case.name + ".dump"), "w") as f:
        f.write("\n".join(dump_lines) + "\n")
    with open(os.path.join(directory, case.name + ".yuv"), "wb") as f:
        for fr in frames:
            f.write(fr["bytes"])


def fails(case, dump_lines, frames, tmp, use_dump=True, use_yuv=True):
    """True if the harness rejects this 'reader output' for the case."""
    d = tempfile.mkdtemp(prefix="reader_", dir=tmp)
    write_reader_files(d, case, dump_lines, frames)
    rep = Report("x")
    try:
        check_case(rep, case, os.path.join(d, case.name + ".dump"), os.path.join(d, case.name + ".yuv"), use_dump, use_yuv, tmp)
    except Bad:
        return True
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return bool(rep.failures)


def self_test():
    tmp = tempfile.mkdtemp(prefix="fields_st_")
    ok_all = True

    def verdict(ok, text):
        nonlocal ok_all
        ok_all &= ok
        print(("ok   " if ok else "BAD  ") + text, flush=True)
    try:
        cs = cases()
        by_name = {c.name: c for c in cs}
        print("1. the reference decoder against ffmpeg, and the construction against the reference decoder")
        for c in cs:
            dump, frames = c.reference()
            ffs, size = c.ffmpeg_frames(tmp)
            full = [f["bytes"] for f in frames if not is_lone(f)]
            verdict(full == ffs, f"{c.name}: {len(frames)} output frames ({sum(is_lone(f) for f in frames)} lone-field frames ffmpeg drops), "
                                 f"{len(ffs)} ffmpeg frames, pixels {'identical' if full == ffs else 'DIFFERENT'}")
            verdict(dump == c.expected, f"{c.name}: dump of the reference decoder equals the dump built from the construction")
        print("2. good reader outputs pass")
        for c in cs:
            dump, frames = c.reference()
            verdict(not fails(c, dump, frames, tmp), f"{c.name}: PASS expected")
        print("3. wrong decoder rules (reference decoder with one rule broken) must FAIL")
        for mut, why in fdec.MUTANTS.items():
            caught = None
            for c in cs:
                fdec.MUTANT = mut
                try:
                    dump, frames = fdec.decode(c.man)
                except Exception as e:                       # a crash is a failing reader, too
                    caught = (c.name, f"crash {type(e).__name__}")
                    break
                finally:
                    fdec.MUTANT = None
                if fails(c, dump, frames, tmp):
                    caught = (c.name, "output differs")
                    break
            verdict(caught is not None, f"{mut}: {why}" + (f" -> caught on {caught[0]} ({caught[1]})" if caught else " -> NOT CAUGHT"))
        print("4. streams written with wrong CABAC field rules: ffmpeg's pixels must differ from the correct decode")
        for mut, why, name in (("frame_ctx_in_field", fgen.MUTANTS["frame_ctx_in_field"], "cabac_bff"),
                               ("frame_sig8_in_field", fgen.MUTANTS["frame_sig8_in_field"], "cabac_t8_mixed")):
            good = by_name[name]
            bad = Case(name, fgen.scenarios()[name], mut)
            dump, frames = good.reference()
            shared = good.man["slices"][0]["first_mb"] == bad.man["slices"][0]["first_mb"]
            bad.ref = good.ref
            bad._ff = None
            verdict(shared and fails(bad, dump, frames, tmp, use_dump=False), f"{mut}: {why} -> caught on {name}")
        print("5. corrupted outputs must FAIL")
        c = by_name["cavlc_lone_fields"]
        dump, frames = c.reference()
        verdict(fails(c, dump[:-1], frames, tmp, use_yuv=False), "dump with the last line missing")
        verdict(fails(c, [l.replace("store=", "stor=") for l in dump], frames, tmp, use_yuv=False), "dump with a renamed key")
        verdict(fails(c, dump, frames[:-1], tmp, use_dump=False), "yuv missing the last frame")
        k = next(i for i, f in enumerate(frames) if is_lone(f))
        bad = [dict(f) for f in frames]
        b = bytearray(bad[k]["bytes"])
        row = 0 if bad[k]["order"] == "T" else 1
        b[row * 80 + 5] ^= 1                                 # one sample on a line of the PRESENT field of a lone-field frame
        bad[k]["bytes"] = bytes(b)
        verdict(fails(c, dump, bad, tmp, use_dump=False), "one wrong sample in a lone-field frame (present field)")
        b = bytearray(frames[k]["bytes"])
        row = 1 if frames[k]["order"] == "T" else 0
        b[row * 80 + 5] ^= 1                                 # a line of the MISSING field: unspecified, must be accepted
        ign = [dict(f) for f in frames]
        ign[k]["bytes"] = bytes(b)
        verdict(not fails(c, dump, ign, tmp, use_dump=False), "different content on the missing-field lines of a lone-field frame is accepted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make", metavar="DIR")
    ap.add_argument("--check", nargs=2, metavar=("DIR", "OUTDIR"))
    ap.add_argument("--suite", metavar="DIR")
    ap.add_argument("--reader")
    ap.add_argument("--stream")
    ap.add_argument("--yuv")
    ap.add_argument("--expected-yuv")
    ap.add_argument("--ignore-frames")
    ap.add_argument("--only")
    ap.add_argument("--no-dump", action="store_true")
    ap.add_argument("--no-yuv", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    ffref.require_tools()
    only = set(a.only.split(",")) if a.only else None
    if a.self_test:
        return self_test()
    if a.make:
        cs = make_suite(a.make, only)
        print(f"wrote {len(cs)} streams to {a.make}: " + " ".join(c.name + ".264" for c in cs))
        return 0
    if a.check:
        return run_check(a.check[0], a.check[1], only, not a.no_dump, not a.no_yuv)
    if a.suite:
        if not a.reader:
            print("error: --suite needs --reader 'CMD {stream} {dump} {yuv}'")
            return 2
        return run_suite(a.suite, a.reader, only, not a.no_dump, not a.no_yuv)
    if a.stream:
        if not a.yuv:
            print("error: --stream needs --yuv")
            return 2
        return run_adhoc(a.stream, a.yuv, a.expected_yuv, a.ignore_frames)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
