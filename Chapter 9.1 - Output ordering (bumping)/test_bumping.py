#!/usr/bin/env python3
"""Chapter 9.1 harness: output ordering ("bumping", C.4.5.3).  Python 3 standard library only (+ ffmpeg / ffprobe).

Two channels, use either or both:

 (A) BUMPING TRACE (no pixel decoding needed).
     python3 test_bumping.py --make-trace IN.ts|IN.264 [-o trace.txt]     write the per-picture trace your reader consumes
     python3 test_bumping.py IN.ts|IN.264 --bump bump.txt [--lenient]     check your reader's output for that stream
     python3 test_bumping.py --suite DIR [--reader 'CMD {trace} {out}'] [--robust]
            write the synthetic suite (traces + streams) into DIR; with --reader run CMD for each case (stdin closed, 120 s
            timeout); otherwise check the existing DIR/<case>.bump files.  Exit 2 when outputs are missing.
 (B) WHOLE-FILE YUV (your decoder's output vs ffmpeg).
     python3 test_bumping.py IN.ts|IN.264 --yuv out.yuv [--noout-rule ffmpeg|spec]

 python3 test_bumping.py --self-test      verifies this harness: good outputs pass, >= 12 wrong bumping rules are caught.

Exit codes: 0 pass, 1 fail, 2 missing / malformed input.
"""
import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles, yuvcmp  # noqa: E402
from common.report import Report  # noqa: E402
import frontend  # noqa: E402
import bumping_ref  # noqa: E402
import cases  # noqa: E402
import streamgen  # noqa: E402

CACHE = os.path.join(testfiles.CACHE, "ch9_1")


class Bad(Exception):
    """Unusable input: exit 2."""


# ---------------------------------------------------------------------------------------------------------- stream -> pictures

def read_annexb(path):
    if path.lower().endswith((".264", ".h264")):
        with open(path, "rb") as f:
            return f.read()
    tmp = tempfile.mkdtemp(prefix="bump_")
    try:
        raw = os.path.join(tmp, "x.264")
        ffref.raw_h264(path, raw)
        if not os.path.exists(raw):
            raise Bad(f"ffmpeg could not demux {path}")
        with open(raw, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pictures_of(path):
    try:
        return frontend.load_pictures(read_annexb(path))
    except frontend.Unsupported as e:
        raise Bad(f"{path}: not supported by this chapter ({e})")


def expected_events(pics, lenient=False):
    bumping_ref.LENIENT = lenient
    try:
        return bumping_ref.run(pics)
    finally:
        bumping_ref.LENIENT = False


def ffmpeg_frame_order(ts):
    """ids (decoding-order picture index) of ffmpeg's output frames, from the packet pts (TS input only)."""
    pk = ffref.probe_packets(ts, "pts")
    fr = ffref.probe_frames(ts, "pts")
    idx = {p["pts"]: i for i, p in enumerate(pk)}
    if len(idx) != len(pk):
        return None
    try:
        return [idx[f["pts"]] for f in fr]
    except KeyError:
        return None


# ------------------------------------------------------------------------------------------------------------ channel A

def parse_bump(path):
    """-> (after: {id: (out list, full or None)}, order of ids as listed, end out list)."""
    if not os.path.exists(path):
        raise Bad(f"reader output {path} not found")
    after, seq, end = {}, [], None
    with open(path, errors="replace") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            t = line.split()
            try:
                kv = dict(x.split("=", 1) for x in t[1 if t[0] == "END" else 2:])
                outs = [] if kv["out"] == "-" else [int(x) for x in kv["out"].split(",")]
                if t[0] == "AFTER":
                    pid = int(t[1])
                    if pid in after:
                        raise Bad(f"{path}:{n}: picture {pid} listed twice")
                    after[pid] = (outs, int(kv["full"]) if "full" in kv else None)
                    seq.append(pid)
                elif t[0] == "END":
                    end = outs
                else:
                    raise ValueError
            except (ValueError, KeyError, IndexError):
                raise Bad(f"{path}:{n}: cannot parse line {line!r}; expected 'AFTER <id> out=<ids|-> [full=<n>]' or 'END out=<ids|->'")
    if end is None:
        raise Bad(f"{path}: no END line (the flush at the end of the stream)")
    return after, seq, end


def check_bump(rep, label, pics, bump_path, lenient=False):
    try:
        events, final = expected_events(pics, lenient)
    except bumping_ref.DpbOverflow as e:
        raise Bad(f"{label}: this stream overflows its own DPB size ({e}); the strict rule cannot process it; "
                  f"re-run with --lenient (Readme: robust rule)")
    after, seq, end = parse_bump(bump_path)
    ids = [e[0] for e in events]
    rep.check(seq == ids, f"{label}: AFTER lines must list the pictures in decoding order 0..{len(ids) - 1}; got {seq[:12]}...")
    for i, out, full in events:
        if i not in after:
            rep.check(False, f"{label}: picture {i}: no AFTER line")
            continue
        got, gfull = after[i]
        rep.check(got == out, f"{label}: after picture {i}: expected out={out or '-'}, got out={got or '-'}")
        if gfull is not None:
            rep.check(gfull == full, f"{label}: after picture {i}: expected full={full}, got full={gfull}")
    rep.check(end == final, f"{label}: END: expected out={final or '-'}, got out={end or '-'}")
    exp_flat = [x for _, o, _ in events for x in o] + final
    got_flat = [x for i in seq for x in after[i][0]] + end
    if exp_flat != got_flat and sorted(exp_flat) == sorted(got_flat):
        # the same pictures came out but in another order: say where
        for k, (a, b) in enumerate(zip(exp_flat, got_flat)):
            if a != b:
                rep.check(False, f"{label}: output order differs at output #{k}: expected picture {a}, got picture {b}")
                break
    elif exp_flat != got_flat:
        miss = sorted(set(exp_flat) - set(got_flat))
        extra = sorted(set(got_flat) - set(exp_flat))
        if miss or extra:
            rep.check(False, f"{label}: pictures never output {miss[:10]}; pictures output that should not be {extra[:10]}")


def sanity_vs_ffmpeg(ts, pics):
    """For TS inputs of conformant streams the reference output order must equal ffmpeg's frame order."""
    order = ffmpeg_frame_order(ts)
    if order is None:
        return None
    try:
        mine = bumping_ref.output_order(pics)
    except bumping_ref.DpbOverflow:
        bumping_ref.LENIENT = True
        try:
            mine = bumping_ref.output_order(pics)
        finally:
            bumping_ref.LENIENT = False
    return mine == order


# ------------------------------------------------------------------------------------------------------------ channel B

def ffmpeg_yuv(path, out):
    fmt = ["-f", "h264"] if path.lower().endswith((".264", ".h264")) else []
    r = ffref.run(["ffmpeg", "-v", "error", "-y", "-strict", "1", *fmt, "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
                   "-f", "rawvideo", "-pix_fmt", "yuv420p", out], timeout=600)
    if not os.path.exists(out):
        raise Bad(f"ffmpeg could not decode {path}: {r.stderr.decode()[-200:]}")


def stream_size(path):
    import json
    fmt = ["-f", "h264"] if path.lower().endswith((".264", ".h264")) else []
    r = ffref.run(["ffprobe", "-v", "error", *fmt, "-select_streams", "v:0", "-show_entries", "stream=width,height",
                   "-of", "json", path])
    try:
        st = json.loads(r.stdout.decode())["streams"][0]
        return int(st["width"]), int(st["height"])
    except (ValueError, KeyError, IndexError):
        raise Bad(f"ffprobe could not read the picture size of {path}")


def frame_bytes(w, h):
    return w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)


def read_frames(path, fb):
    with open(path, "rb") as f:
        data = f.read()
    return [data[i:i + fb] for i in range(0, len(data) - fb + 1, fb)], len(data) % fb


def expected_yuv_frames(path, work, noout_rule):
    """-> (frames list, w, h).  ffmpeg ignores no_output_of_prior_pics_flag; 'spec' removes the frames C.4.4 discards."""
    ref = os.path.join(work, "ref.yuv")
    ffmpeg_yuv(path, ref)
    w, h = stream_size(path)
    frames, rest = read_frames(ref, frame_bytes(w, h))
    if noout_rule == "spec":
        pics = pictures_of(path)
        nof = [dict(p, noout=0) for p in pics]
        order = bumping_ref.output_order_lenient(nof) if hasattr(bumping_ref, "output_order_lenient") else _order_lenient(nof)
        by_id = {i: frames[k] for k, i in enumerate(order)}
        spec = _order_lenient(pics)
        frames = [by_id[i] for i in spec]
    return frames, w, h


def _order_lenient(pics):
    bumping_ref.LENIENT = True
    try:
        return bumping_ref.output_order(pics)
    finally:
        bumping_ref.LENIENT = False


def check_yuv(rep, label, path, yuv, noout_rule="ffmpeg"):
    if not os.path.exists(yuv):
        raise Bad(f"YUV file {yuv} not found")
    work = tempfile.mkdtemp(prefix="bumpyuv_")
    try:
        frames, w, h = expected_yuv_frames(path, work, noout_rule)
        fb = frame_bytes(w, h)
        got, rest = read_frames(yuv, fb)
        expfile = os.path.join(work, "exp.yuv")
        with open(expfile, "wb") as f:
            f.write(b"".join(frames))
        rep.check(len(got) == len(frames),
                  f"{label}: frame count: expected {len(frames)} frames of {w}x{h} ({fb} bytes), got {len(got)}"
                  + (f" plus {rest} stray bytes" if rest else "") +
                  " (too few: a picture was never output / flushed at the end or dropped by a wrong no_output rule; too many: "
                  "a picture output twice, or the 'cropped' size is wrong)")
        rep.check(rest == 0, f"{label}: file size is not a multiple of the frame size {fb} ({w}x{h} cropped yuv420p): {rest} stray bytes")
        res = yuvcmp.compare_yuv(expfile, yuv, w, h, quiet=True)
        rep.check(res["ok"], f"{label}: pixels differ from ffmpeg:\n" + "\n".join("      " + l for l in res["text"].splitlines()[:14]))
        if not res["ok"] and res.get("first_diff") is not None:
            # ORDER diagnosis: is the first wrong frame identical to some other expected frame?
            k = res["first_diff"]["frame"]
            if k < len(got):
                where = [j for j, e in enumerate(frames) if e == got[k]]
                if where:
                    rep.check(False, f"{label}: DIAGNOSIS: your output frame {k} is byte-identical to expected frame {where[0]}"
                                     f" -> the frames are right, the OUTPUT ORDER (or a dropped/duplicated frame) is wrong")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------------------ suite

OPTIONAL_ROBUST = ("b1_asis", "nopyr_asis")


def build_suite(outdir):
    """-> list of (name, pics, lenient, stream path or None).  Streams are cached in CACHE, copied into outdir."""
    os.makedirs(CACHE, exist_ok=True)
    ffref.require_tools()
    out = []

    def from_stream(name, case, edit=None, lenient=False, noout=None):
        ts = streamgen.encode(case, CACHE)
        raw = os.path.join(CACHE, case + ".264")
        if not os.path.exists(raw):
            ffref.raw_h264(ts, raw)
        data = open(raw, "rb").read()
        if edit:
            data = streamgen.set_restriction(data, *edit)
        if noout is not None:
            data = streamgen.set_noout(data, noout)
        path = os.path.join(outdir, name + ".264")
        with open(path, "wb") as f:
            f.write(data)
        out.append((name, frontend.load_pictures(data), lenient, path))

    from_stream("pyr3", "pyr3")
    from_stream("pyr7", "pyr7")
    from_stream("manyidr", "manyidr")
    from_stream("opengop", "opengop")
    from_stream("ip", "ip")
    from_stream("intra_mdfb0", "intra")
    from_stream("intra_mdfb1", "intra", (0, 1))
    from_stream("intra_mdfb3", "intra", (0, 3))
    from_stream("b1_mdfb5", "b1", (1, 5))
    from_stream("pyr3_mdfb16", "pyr3", (2, 16))
    from_stream("pyr3_reorder4_mdfb8", "pyr3", (4, 8))
    from_stream("pyr3_level", "pyr3", (None, None))
    from_stream("broadcast_level", "broadcast", (None, None))
    from_stream("manyidr_mdfb6", "manyidr", (2, 6))
    from_stream("manyidr_noout_mdfb6", "manyidr", (2, 6), noout=None)
    from_stream("pyr3_noout_first_gop", "pyr3", (2, 6), noout={1})
    from_stream("b1_asis", "b1", lenient=True)
    from_stream("nopyr_asis", "nopyr", lenient=True)
    for name, pics in cases.hand_cases().items():
        out.append((name, pics, False, None))
    return out


def run_suite(args):
    d = args.suite
    os.makedirs(d, exist_ok=True)
    suite = build_suite(d)
    rep = Report("bumping suite")
    missing, ran = [], 0
    for name, pics, lenient, path in suite:
        optional = name in OPTIONAL_ROBUST and not args.robust
        trace = os.path.join(d, name + ".trace")
        with open(trace, "w") as f:
            f.write(("# robust rule needed (stream overflows its DPB size)\n" if lenient else "") + bumping_ref.format_trace(pics))
        outp = os.path.join(d, name + ".bump")
        if args.reader:
            cmd = [c.replace("{trace}", trace).replace("{out}", outp) for c in shlex.split(args.reader)]
            if os.path.exists(outp):
                os.remove(outp)
            try:
                subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
            except subprocess.TimeoutExpired:
                rep.check(False, f"{name}: reader timed out")
                continue
            except OSError as e:
                raise Bad(f"cannot run reader: {e}")
        if not os.path.exists(outp):
            if not optional:
                missing.append(name)
            continue
        ran += 1
        sub = Report(name)
        try:
            check_bump(sub, name, pics, outp, lenient)
        except Bad as e:
            sub.check(False, str(e))
        if optional:
            print(f"  (optional, not counted) {name}: {'ok' if not sub.failures else 'differs'}")
            continue
        for fmsg in sub.failures:
            rep.check(False, fmsg)
        rep.checks += sub.checks - len(sub.failures)
        print(f"  {name}: {'ok' if not sub.failures else 'FAIL'}")
    print(f"suite files are in {d}: <case>.trace (reader input), <case>.264 (stream, for channel B), <case>.bump (reader output)")
    if missing and not ran:
        raise Bad("no reader outputs found: run your reader on each DIR/<case>.trace to write DIR/<case>.bump, or pass --reader 'CMD {trace} {out}'")
    if missing:
        raise Bad("missing reader outputs: " + ", ".join(missing))
    rep.finish()


# ------------------------------------------------------------------------------------------------------------------ self test

def self_test():
    ok_all = True
    ffref.require_tools()
    os.makedirs(CACHE, exist_ok=True)
    work = tempfile.mkdtemp(prefix="bump_self_")
    try:
        # 0. frontend + reference validated against ffmpeg's frame order on x264 streams
        names = ["T9_spatial", "T11_high", "T10_b", "T6", "T12"]
        for n in names:
            ts = testfiles.get(n)
            pics = pictures_of(ts)
            good = sanity_vs_ffmpeg(ts, pics)
            print(f"  reference output order == ffmpeg frame order on {n}: {good}")
            ok_all &= bool(good)
        for case in ("pyr3", "pyr7", "manyidr", "opengop", "ip", "intra", "b1", "nopyr", "broadcast"):
            ts = streamgen.encode(case, CACHE)
            pics = pictures_of(ts)
            good = sanity_vs_ffmpeg(ts, pics)
            print(f"  reference output order == ffmpeg frame order on {case}: {good}")
            ok_all &= bool(good)
        # SPS rewriting leaves ffmpeg's output untouched (conformant restriction values)
        ffref.raw_h264(os.path.join(CACHE, "pyr3.ts"), os.path.join(work, "pyr3.264"))
        data = open(os.path.join(work, "pyr3.264"), "rb").read()

        def md5s(b):
            p = os.path.join(work, "m.264")
            open(p, "wb").write(b)
            r = ffref.run(["ffmpeg", "-v", "error", "-strict", "1", "-f", "h264", "-i", p, "-fps_mode", "passthrough", "-f", "framemd5", "-"])
            return [l.split(b",")[-1] for l in r.stdout.splitlines() if l and not l.startswith(b"#")]
        def md5s_plain(b):
            p = os.path.join(work, "m.264")
            open(p, "wb").write(b)
            r = ffref.run(["ffmpeg", "-v", "error", "-f", "h264", "-i", p, "-fps_mode", "passthrough", "-f", "framemd5", "-"])
            return [l.split(b",")[-1] for l in r.stdout.splitlines() if l and not l.startswith(b"#")]
        base = md5s(data)
        for ed in ((2, 16), (4, 8), (None, None), (2, 4)):
            same = md5s(streamgen.set_restriction(data, *ed)) == base
            print(f"  ffmpeg output unchanged after rewriting bitstream_restriction to {ed}: {same}")
            ok_all &= same
        nr = streamgen.set_restriction(data, None, None)
        print(f"  (info) without -strict 1 ffmpeg {'changes' if md5s_plain(nr) != base else 'keeps'} its output when the VUI restriction is missing")
        print(f"  ffmpeg ignores no_output_of_prior_pics_flag (same frames): {md5s(streamgen.set_noout(data)) == base}")
        ok_all &= md5s(streamgen.set_noout(data)) == base

        # 1. channel A: good output passes, corrupted outputs fail
        suite = build_suite(work)
        for name, pics, lenient, path in suite:
            events, final = expected_events(pics, lenient)
            outp = os.path.join(work, name + ".bump")
            open(outp, "w").write(bumping_ref.format_output(events, final))
            r = Report(name)
            check_bump(r, name, pics, outp, lenient)
            if r.failures:
                ok_all = False
                print("  FAIL (good output rejected):", name, r.failures[:3])
        print(f"  channel A: good reader output accepted on {len(suite)} cases: {ok_all}")

        # 2. mutation tests: each wrong bumping rule must be caught by at least one case
        strict_cases = [(n, p, l) for n, p, l, _ in suite if not l]
        caught_by = {}
        for mut in bumping_ref.MUTANTS:
            caught_by[mut] = []
            for name, pics, _ in strict_cases:
                bumping_ref.MUTANT = mut
                try:
                    bumping_ref.LENIENT = False
                    ev, fin = bumping_ref.run(pics)
                    text = bumping_ref.format_output(ev, fin)
                except Exception:
                    text = None                 # the wrong rule crashes: the reader would fail on this case too
                finally:
                    bumping_ref.MUTANT = None
                if text is None:
                    caught_by[mut].append(name)
                    continue
                p = os.path.join(work, "mut.bump")
                open(p, "w").write(text)
                r = Report("mut")
                check_bump(r, name, pics, p)
                if r.failures:
                    caught_by[mut].append(name)
            n = len(caught_by[mut])
            print(f"  mutant {mut:26s} caught by {n:2d} case(s)" + (f"   e.g. {caught_by[mut][0]}" if n else "   NOT CAUGHT"))
            ok_all &= n > 0
        print(f"  {len(bumping_ref.MUTANTS)} wrong bumping rules, all caught: {all(caught_by.values())}")

        # 3. channel B: ffmpeg frames re-ordered by wrong rules / generic corruptions
        for case, tsname in (("T9_spatial", testfiles.get("T9_spatial")), ("broadcast", os.path.join(CACHE, "broadcast.ts")),
                             ("manyidr", os.path.join(CACHE, "manyidr.ts"))):
            ref = os.path.join(work, "r.yuv")
            ffmpeg_yuv(tsname, ref)
            w, h = stream_size(tsname)
            fb = frame_bytes(w, h)
            frames, _ = read_frames(ref, fb)
            pics = pictures_of(tsname)
            order = _order_lenient(pics)
            by_id = {i: frames[k] for k, i in enumerate(order)}
            good = os.path.join(work, "good.yuv")
            open(good, "wb").write(b"".join(frames))
            r = Report("yuv")
            check_yuv(r, case, tsname, good)
            print(f"  channel B {case}: ffmpeg's own output accepted: {not r.failures}")
            ok_all &= not r.failures
            bad = {
                "frame dropped": b"".join(frames[:-1]),
                "frame duplicated": b"".join(frames[:5] + frames[4:5] + frames[5:]),
                "two frames swapped": b"".join(frames[:3] + [frames[4], frames[3]] + frames[5:]),
                "truncated mid-frame": b"".join(frames)[:-fb // 2],
                "luma byte off by one": b"".join(frames[:7] + [bytes([frames[7][0] ^ 1]) + frames[7][1:]] + frames[8:]),
                "uncropped (zero-padded) frames": b"".join(f + bytes(fb2 - fb) for f in frames) if (fb2 := frame_bytes(w, h + 8)) else b"",
                "Cb/Cr planes swapped": b"".join(f[:w * h] + f[w * h + (w + 1) // 2 * ((h + 1) // 2):] + f[w * h:w * h + (w + 1) // 2 * ((h + 1) // 2)] for f in frames),
            }
            for mut in ("bump_max_poc", "bump_fifo", "no_end_flush", "end_flush_decode_order", "idr_no_flush"):
                bumping_ref.MUTANT = mut
                try:
                    o = bumping_ref.output_order(pics)
                except Exception:
                    o = None
                finally:
                    bumping_ref.MUTANT = None
                if o is not None and o != order:
                    bad[f"wrong rule {mut}"] = b"".join(by_id[i] for i in o if i in by_id)
            for k, v in bad.items():
                p = os.path.join(work, "bad.yuv")
                open(p, "wb").write(v)
                r = Report("yuv")
                check_yuv(r, case, tsname, p)
                print(f"    corruption '{k}' detected: {bool(r.failures)}")
                ok_all &= bool(r.failures)
        # no_output_of_prior_pics: ffmpeg rule vs spec rule on the synthetic stream
        path = os.path.join(work, "noout.264")
        ffref.raw_h264(os.path.join(CACHE, "manyidr.ts"), os.path.join(work, "m0.264"))
        d0 = streamgen.set_noout(streamgen.set_restriction(open(os.path.join(work, "m0.264"), "rb").read(), 2, 6))
        open(path, "wb").write(d0)
        frames, w, h = expected_yuv_frames(path, work, "ffmpeg")
        sframes, _, _ = expected_yuv_frames(path, work, "spec")
        print(f"  no_output_of_prior_pics stream: ffmpeg rule {len(frames)} frames, spec rule {len(sframes)} frames")
        ok_all &= len(sframes) < len(frames)
        for rule, fr in (("ffmpeg", frames), ("spec", sframes)):
            p = os.path.join(work, "n.yuv")
            open(p, "wb").write(b"".join(fr))
            for check_rule in ("ffmpeg", "spec"):
                r = Report("yuv")
                check_yuv(r, "noout", path, p, check_rule)
                expect_pass = rule == check_rule
                print(f"    output made with {rule} rule, checked with {check_rule} rule: {'pass' if not r.failures else 'fail'}")
                ok_all &= (not r.failures) == expect_pass
    finally:
        bumping_ref.MUTANT = None
        shutil.rmtree(work, ignore_errors=True)
    print("SELF-TEST", "PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)


# ------------------------------------------------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="H.264 stream: .ts or Annex B .264")
    ap.add_argument("--make-trace", metavar="IN", help="write the bumping trace of IN (to -o or stdout)")
    ap.add_argument("-o", "--output")
    ap.add_argument("--bump", help="channel A: your reader's output for the input stream")
    ap.add_argument("--yuv", help="channel B: your decoder's whole-file YUV (cropped yuv420p, output order)")
    ap.add_argument("--noout-rule", choices=("ffmpeg", "spec"), default="ffmpeg",
                    help="channel B, streams with no_output_of_prior_pics_flag = 1: ffmpeg outputs everything (default), spec discards")
    ap.add_argument("--lenient", action="store_true", help="channel A: expected output uses the robust rule for streams that overflow their DPB size")
    ap.add_argument("--suite", metavar="DIR")
    ap.add_argument("--reader", help="with --suite: command template, {trace} and {out} are replaced")
    ap.add_argument("--robust", action="store_true", help="with --suite: also count the two robust-rule cases")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        if a.self_test:
            self_test()
        if a.suite:
            run_suite(a)
            return
        if a.make_trace:
            ffref.require_tools()
            pics = pictures_of(a.make_trace)
            text = bumping_ref.format_trace(pics)
            try:
                bumping_ref.run(pics)
            except bumping_ref.DpbOverflow:
                text = "# NOTE: this stream overflows its own DPB size under the strict rule; use the robust rule (--lenient)\n" + text
                print("warning: stream overflows its DPB size under the strict rule", file=sys.stderr)
            if a.output:
                with open(a.output, "w") as f:
                    f.write(text)
                print(f"{len(pics)} pictures -> {a.output}")
            else:
                sys.stdout.write(text)
            return
        if not a.input or not (a.bump or a.yuv):
            ap.error("give an input stream with --bump and/or --yuv (or --make-trace / --suite / --self-test)")
        ffref.require_tools()
        rep = Report(f"output order of {os.path.basename(a.input)}")
        if a.bump:
            check_bump(rep, "bump", pictures_of(a.input), a.bump, a.lenient)
        if a.yuv:
            check_yuv(rep, "yuv", a.input, a.yuv, a.noout_rule)
        rep.finish()
    except Bad as e:
        print("error:", e, file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired as e:
        print("error: a tool timed out:", e.cmd[0], file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
