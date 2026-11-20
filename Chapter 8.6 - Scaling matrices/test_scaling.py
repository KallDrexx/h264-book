#!/usr/bin/env python3
"""Harness for Chapter 8.6 (scaling matrices: SPS / PPS scaling lists, fall-back rules A and B, default tables, and their use in the
4x4 and 8x8 dequantisation).

Usage (grammars: Readme, section 5):
  python3 test_scaling.py INPUT [--dump DUMP] [--yuv OUT.yuv]
        check your output for a High-profile .ts / Annex B .264 file (at least one of the two outputs):
        --dump   the effective weight matrices of every SPS and PPS of the stream     -> section 5.1
        --yuv    your decoded frames, cropped planar yuv420p, display order            -> section 5.2
  python3 test_scaling.py --print-expected-dump INPUT OUT   write the expected dump (to diff against yours)
  python3 test_scaling.py --make-variants DIR                write test streams (rewritten x264 streams + real x264 --cqm streams)
                                                             with their expected dumps
  python3 test_scaling.py --self-test                        verify this harness (takes a few minutes)

Truth.
  * Pixels: ffmpeg (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`), compared with common/yuvcmp.py (first differing frame /
    plane / macroblock / pixel).
  * Matrices: spoiler_reference/scaling_ref.py parses the SPS / PPS scaling syntax straight from the stream and resolves the
    fall-back rules.  It is cross-checked two ways: (1) scalegen.py builds test streams by REWRITING the SPS / PPS of x264 streams
    with every fall-back path and computes the expected matrices from the construction; (2) the reference decoder built on the
    parsed matrices (spoiler_reference/hdec.py) reproduces ffmpeg's pixels bit for bit on every one of those streams and on real
    x264 streams made with --cqm=jvt / --cqm4i* / --cqmfile, which proves the matrices are the ones ffmpeg really uses.
Exit 0 = pass, 1 = your output differs, 2 = unusable input / output or a harness problem.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SPOIL = os.path.join(HERE, "spoiler_reference")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, SPOIL)
sys.path.insert(0, HERE)
from common import ffref, testfiles, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import scaling_ref as sr  # noqa: E402
import scalegen  # noqa: E402
import hdec  # noqa: E402
import stream8  # noqa: E402

TRANSPOSE_HINT = "hint: matrices are printed in RASTER order (row by row); a scaling list is stored in zig-zag scan order"


def die(msg, code=2):
    print(f"error: {msg}")
    sys.exit(code)


# ================================================================ dump grammar
def parse_dump(text, who):
    """-> list of blocks dict(kind, n, id, sps, m4=[6 lists of 16], m8=[2 lists of 64], line).  Malformed -> exit 2."""
    blocks, cur = [], None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        w = line.split()

        def bad(msg):
            die(f"malformed {who}, line {no}: {msg}: {line!r}")
        if w[0] in ("SPS", "PPS"):
            if len(w) < 3 or not w[1].isdigit():
                bad(f"{w[0]} needs an index and id=<n>" + (" sps=<n>" if w[0] == "PPS" else ""))
            kv = dict(t.split("=", 1) for t in w[2:] if "=" in t)
            if not kv.get("id", "x").isdigit() or (w[0] == "PPS" and not kv.get("sps", "x").isdigit()):
                bad("expected id=<number>" + (" sps=<number>" if w[0] == "PPS" else ""))
            cur = dict(kind=w[0], n=int(w[1]), id=int(kv["id"]), sps=int(kv["sps"]) if w[0] == "PPS" else None, m4={}, m8={}, line=no)
            blocks.append(cur)
        elif w[0] in ("M4", "M8"):
            if cur is None:
                bad("matrix line before the first SPS / PPS line")
            size, cnt = (16, 6) if w[0] == "M4" else (64, 2)
            if len(w) != 2 + size or not w[1].isdigit() or int(w[1]) >= cnt:
                bad(f"{w[0]} needs an index 0..{cnt - 1} and exactly {size} integers")
            try:
                vals = [int(x) for x in w[2:]]
            except ValueError:
                bad("matrix entries must be decimal integers")
            if int(w[1]) in cur["m4" if w[0] == "M4" else "m8"]:
                bad(f"{w[0]} {w[1]} appears twice in this block")
            cur["m4" if w[0] == "M4" else "m8"][int(w[1])] = vals
        else:
            bad("line must start with SPS, PPS, M4 or M8")
    if not blocks:
        die(f"malformed {who}: no SPS / PPS block found (the dump is empty)")
    return blocks


def label(b):
    return f"{b['kind']} #{b['n']} (id {b['id']})"


MNAME4 = ["Intra Y", "Intra Cb", "Intra Cr", "Inter Y", "Inter Cb", "Inter Cr"]
MNAME8 = ["Intra Y", "Inter Y"]


def hint_for(got, e_blk, key, k, size):
    """Say what a wrong matrix looks like (flat, another list, a default table, raster-laid scan list ...)."""
    n = 4 if size == 16 else 8
    tab = e_blk[key]
    if got == [16] * size:
        return "your matrix is Flat_16 (is a list or the whole matrix being ignored?)"
    for kk, v in tab.items():
        if kk != k and v == got:
            nm = (MNAME4 if size == 16 else MNAME8)[kk]
            return f"your matrix equals the expected {key.upper()} {kk} ({nm}): a fall-back / list-index mistake"
    dflt = {16: [sr.DEF4_INTRA, sr.DEF4_INTER], 64: [sr.DEF8_INTRA, sr.DEF8_INTER]}[size]
    for d in dflt:
        r = [0] * size
        for s, (i, j) in enumerate(sr.ZZ4 if n == 4 else sr.ZZ8):
            r[i * n + j] = d[s]
        if r == got:
            return "your matrix is a Default_* table: expected an explicit / inherited list here (useDefault / fall-back confusion)"
    exp = tab[k]
    scan = [exp[i * n + j] for (i, j) in (sr.ZZ4 if n == 4 else sr.ZZ8)]
    if scan == got:
        return "your matrix is the expected one in SCAN order: apply the inverse zig-zag before printing (raster order wanted)"
    tr = [exp[j * n + i] for i in range(n) for j in range(n)]
    if tr == got:
        return "your matrix is the expected one transposed: row and column are swapped"
    if any(v < 1 or v > 255 for v in got):
        return "an entry is outside 1..255: check the modulo 256 in nextScale = (lastScale + delta_scale + 256) % 256"
    return ""


def check_dump(exp, got, rep):
    rep.check(len(got) == len(exp), f"expected {len(exp)} SPS / PPS blocks (a repeated identical copy of a parameter set is not "
              f"listed twice), you print {len(got)}")
    for e, g in zip(exp, got):
        who = f"{label(e)}"
        if (e["kind"], e["id"], e["sps"]) != (g["kind"], g["id"], g["sps"]) or e["n"] != g["n"]:
            rep.check(False, f"{who}: block header differs: expected {e['kind']} {e['n']} id={e['id']}"
                      + (f" sps={e['sps']}" if e["kind"] == "PPS" else "") + f", got {g['kind']} {g['n']} id={g['id']}"
                      + (f" sps={g['sps']}" if g["kind"] == "PPS" else "") + f" [your line {g['line']}]")
            continue
        for key, cnt, size, names in (("m4", 6, 16, MNAME4), ("m8", 2, 64, MNAME8)):
            for k in range(cnt):
                nm = f"{who}, {key.upper()} {k} ({names[k]})"
                if k not in g[key]:
                    rep.check(False, f"{nm}: matrix line missing [block starts at your line {g['line']}]")
                    continue
                ev, gv = e[key][k], g[key][k]
                if ev == gv:
                    rep.check(True, "")
                    continue
                n = 4 if size == 16 else 8
                pos = next(i for i in range(size) if ev[i] != gv[i])
                h = hint_for(gv, e, key, k, size)
                rep.check(False, f"{nm}: first difference at row {pos // n}, column {pos % n}: expected {ev[pos]}, got {gv[pos]}"
                          f"  (expected: {' '.join(map(str, ev[:n]))} ...; yours: {' '.join(map(str, gv[:n]))} ...)"
                          + (f"  {h}" if h else f"  {TRANSPOSE_HINT}"))


# ================================================================ truth
def raw_stream(path, workdir):
    if path.lower().endswith((".264", ".h264")):
        return open(path, "rb").read()
    out = os.path.join(workdir, "ref_raw.264")
    try:
        ffref.raw_h264(path, out)
    except Exception:
        out = None
    if not out or not os.path.exists(out) or not os.path.getsize(out):
        die(f"cannot read {path} as an H.264 .ts / Annex B stream (ffmpeg found no H.264 video in it)")
    return open(out, "rb").read()


def expected_dump(path, workdir):
    try:
        return sr.render_dump(raw_stream(path, workdir))
    except SystemExit:
        raise
    except Exception as e:
        die(f"the reference cannot read the parameter sets of {path}: {type(e).__name__}: {e}")


def ffmpeg_yuv(path, out):
    ffref.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "yuv420p", out], timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")
    return out


def stream_wh(path):
    info = yuvref.stream_info(path)
    if not info["width"]:
        die(f"cannot read the picture size of {path}")
    return info["width"], info["height"]


def check_yuv(path, mine, rep, workdir):
    ref = ffmpeg_yuv(path, os.path.join(workdir, "ref.yuv"))
    w, h = stream_wh(path)
    if not os.path.exists(mine):
        die(f"cannot read {mine!r}")
    res = yuvcmp.compare_yuv(ref, mine, w, h, quiet=True)
    msg = ("--yuv: " + res["text"].replace("\n", "\n    ")) if not res["ok"] else ""
    if not res["ok"] and not res.get("size_problems"):
        msg += ("\n    hint: if --dump passes, look at WHICH matrix the first wrong block uses: Intra Y / Cb / Cr and Inter Y / Cb / Cr "
                "(4x4: list index = component + 3 * inter; 8x8: Intra Y or Inter Y), and remember that the Intra16x16 DC and the "
                "chroma DC levels use the matrix entry (0, 0) too.  If only streams WITHOUT matrices pass, the weights are not applied.")
    rep.check(res["ok"], msg)


def run_checks(path, dump=None, yuv=None, quiet=False):
    """-> exit code (0 pass / 1 differs / 2 unusable).  Prints the report (nothing if quiet)."""
    if quiet:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return run_checks(path, dump, yuv, False)
    wd = tempfile.mkdtemp(prefix="sc86chk_")
    try:
        rep = Report(f"Chapter 8.6 checks of {os.path.basename(path)}", max_shown=10)
        if dump:
            try:
                got = open(dump, "r", errors="replace").read()
            except OSError as e:
                die(f"cannot read dump {dump!r}: {e}")
            check_dump(parse_dump(expected_dump(path, wd), "expected dump"), parse_dump(got, "your dump"), rep)
        if yuv:
            check_yuv(path, yuv, rep, wd)
        try:
            rep.finish()
        except SystemExit as e:
            return e.code
    except SystemExit as e:                 # die(): the message is printed already
        return e.code
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ================================================================ test streams
def ramp(lo, hi, n):
    return ",".join(str(lo + (hi - lo) * k // (n - 1) + (k % 3)) for k in range(n))


def make_x264_cqm(out, kind, tmp):
    base = "keyint=8:bframes=2:ref=2"
    if kind == "jvt_cabac":
        params, qp = base + ":cqm=jvt", 24
    elif kind == "jvt_cavlc":
        params, qp = base + ":cabac=0:cqm=jvt", 24
    elif kind == "custom":
        params = (base + f":cqm4iy={ramp(8, 30, 16)}:cqm4ic={ramp(10, 36, 16)}:cqm4py={ramp(12, 34, 16)}:cqm4pc={ramp(9, 40, 16)}"
                  f":cqm8i={ramp(9, 46, 64)}:cqm8p={ramp(12, 40, 64)}")
        qp = 24
    elif kind == "cqmfile":
        cf = os.path.join(tmp, "my.cqm")
        with open(cf, "w") as f:
            for name, lo, hi, n in (("INTRA4X4_LUMA", 6, 40, 16), ("INTRA4X4_CHROMA", 7, 33, 16), ("INTER4X4_LUMA", 10, 38, 16),
                                    ("INTER4X4_CHROMA", 9, 31, 16), ("INTRA8X8_LUMA", 8, 50, 64), ("INTER8X8_LUMA", 11, 44, 64)):
                f.write(f"{name} =\n{ramp(lo, hi, n)}\n")
        params, qp = base + ":cabac=0:cqmfile=" + cf, 30
    else:
        raise ValueError(kind)
    ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", scalegen.SMALL_SRC, "-frames:v", "8", "-pix_fmt", "yuv420p",
               "-c:v", "libx264", "-profile:v", "high", "-qp", str(qp), "-x264-params", params, "-f", "h264", out], timeout=300)
    if not os.path.exists(out) or not os.path.getsize(out):
        die("ffmpeg/libx264 could not make an x264 --cqm test stream")
    return out


X264_KINDS = ["jvt_cabac", "jvt_cavlc", "custom", "cqmfile"]
VARIANT_BASES = ["b_cabac", "b_cavlc", "b_no8x8"]        # bases written by --make-variants (the self-test uses all of scalegen.BASES)


def make_variants(d):
    os.makedirs(d, exist_ok=True)
    wd = tempfile.mkdtemp(prefix="sc86var_")
    try:
        for b in VARIANT_BASES:
            data = open(scalegen.make_base(os.path.join(wd, b + ".264"), b), "rb").read()
            for name, (_, _, note) in scalegen.variants().items():
                stream, exp = scalegen.build(data, name)
                p = os.path.join(d, f"{b}__{name}.264")
                open(p, "wb").write(stream)
                open(os.path.join(d, f"{b}__{name}.expected.txt"), "w").write(exp)
                print(f"wrote {p} (+ .expected.txt)  {note}")
        for k in X264_KINDS:
            p = make_x264_cqm(os.path.join(d, f"x264_{k}.264"), k, wd)
            open(os.path.join(d, f"x264_{k}.expected.txt"), "w").write(expected_dump(p, wd))
            print("wrote", p, "(+ .expected.txt)")
        p = os.path.join(d, "T11_cqm.ts")
        shutil.copyfile(testfiles.get("T11_cqm"), p)
        open(os.path.join(d, "T11_cqm.expected.txt"), "w").write(expected_dump(p, wd))
        print("wrote", p, "(+ .expected.txt)  x264 cqm=jvt, CIF, 50 frames")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ================================================================ self-test
class Stream:
    def __init__(self, name, path, expected=None):
        self.name, self.path, self.expected = name, path, expected
        self.cache, self.ref, self.dump, self.raw = {}, None, None, None


def decode_bytes(st, mutant=None, fresh=False):
    hdec.MUTANT = mutant
    try:
        pics = hdec.decode(st.path, cache=None if fresh else st.cache)
        return b"".join(hdec.frame_bytes(p) for p in pics)
    finally:
        hdec.MUTANT = None


def self_test():
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="sc86selftest_")
    ok_all = True

    def say(msg):
        print(msg, flush=True)

    def verdict(cond, msg):
        nonlocal ok_all
        say(("  ok    " if cond else "  FAIL  ") + msg)
        ok_all = ok_all and cond
    try:
        streams, bases = [], {}
        for b in scalegen.BASES:
            bases[b] = open(scalegen.make_base(os.path.join(tmp, b + ".base"), b), "rb").read()
            for name in scalegen.variants():
                data, exp = scalegen.build(bases[b], name)
                p = os.path.join(tmp, f"{b}__{name}.264")
                open(p, "wb").write(data)
                streams.append(Stream(f"{b}__{name}", p, exp))
        for k in X264_KINDS:
            streams.append(Stream("x264_" + k, make_x264_cqm(os.path.join(tmp, f"x264_{k}.264"), k, tmp)))
        streams.append(Stream("T11_cqm", testfiles.get("T11_cqm")))
        say(f"== 1. reference vs truth on {len(streams)} streams ({len(scalegen.variants())} header combinations x "
            f"{len(scalegen.BASES)} x264 bases, {len(X264_KINDS)} x264 --cqm streams, T11_cqm)")
        nbad = nparse = 0
        for st in streams:
            st.raw = raw_stream(st.path, tmp)
            st.dump = sr.render_dump(st.raw)
            if st.expected is not None and st.dump != st.expected:
                nparse += 1
                verdict(False, f"{st.name}: reference dump differs from the construction")
            st.ref = open(ffmpeg_yuv(st.path, os.path.join(tmp, "r.yuv")), "rb").read()
            from_ref = decode_bytes(st)
            if from_ref != st.ref:
                nbad += 1
                verdict(False, f"{st.name}: reference decoder differs from ffmpeg")
        verdict(nparse == 0, f"parameter-set parse = the generator's construction on all {sum(1 for s in streams if s.expected)} rewritten streams")
        verdict(nbad == 0, f"reference decoder = ffmpeg pixels on all {len(streams)} streams")
        sig = {}
        for st in streams:
            if st.expected and st.name.startswith("b_cabac__"):
                matrices = st.dump.split("PPS 0")[1]                    # the effective matrices the slices use
                sig.setdefault(matrices, set()).add(st.ref)
        pix = [next(iter(v)) for v in sig.values()]
        verdict(all(len(v) == 1 for v in sig.values()) and len(set(pix)) == len(sig),
                f"on b_cabac the {len(sig)} distinct effective-matrix sets give {len(set(pix))} distinct pictures "
                f"(identical matrices <=> identical pixels: nothing is hidden, nothing is untested)")

        say("== 2. the checker accepts good output and rejects damaged output")
        base = next(s for s in streams if s.name == "b_cabac__pps_ruleB")
        dpath = os.path.join(tmp, "good.txt")
        open(dpath, "w").write(base.dump)
        ypath = os.path.join(tmp, "good.yuv")
        open(ypath, "wb").write(base.ref)
        verdict(run_checks(base.path, dump=dpath, yuv=ypath, quiet=True) == 0, "known-good dump + YUV -> PASS")
        lines = base.dump.splitlines()
        m4 = [i for i, ln in enumerate(lines) if ln.startswith("M4 2 ")]

        def corrupt(name, fn, rc_want=1):
            ls = list(lines)
            fn(ls)
            bp = os.path.join(tmp, name + ".txt")
            open(bp, "w").write("\n".join(ls) + "\n")
            verdict(run_checks(base.path, dump=bp, quiet=True) == rc_want, f"dump damaged ({name}) -> {'FAIL' if rc_want == 1 else 'exit 2'}")

        def bump(ls, i, pos=5):
            w = ls[i].split()
            w[2 + pos] = str(int(w[2 + pos]) + 1)
            ls[i] = " ".join(w)
        corrupt("one_weight_off", lambda ls: bump(ls, m4[1]))
        corrupt("matrix_missing", lambda ls: ls.pop(m4[0]))
        corrupt("block_missing", lambda ls: [ls.pop() for _ in range(9)])
        corrupt("pps_id_changed", lambda ls: ls.__setitem__(9, ls[9].replace("id=0 ", "id=3 ", 1)))
        corrupt("short_matrix", lambda ls: ls.__setitem__(m4[0], " ".join(ls[m4[0]].split()[:-1])), 2)
        corrupt("garbage_line", lambda ls: ls.append("hello world"), 2)
        bad = bytearray(base.ref)
        bad[len(bad) // 3] ^= 1
        bp = os.path.join(tmp, "bad.yuv")
        open(bp, "wb").write(bytes(bad))
        verdict(run_checks(base.path, yuv=bp, quiet=True) == 1, "one wrong pixel in --yuv -> FAIL")
        flatp = next(s for s in streams if s.name == "b_cabac__sps_full")
        open(bp, "wb").write(flatp.ref)
        verdict(run_checks(base.path, yuv=bp, quiet=True) == 1, "the pixels of a DIFFERENT matrix combination -> FAIL")
        jp = os.path.join(tmp, "junk.ts")
        open(jp, "wb").write(bytes(range(256)) * 40)
        verdict(run_checks(jp, dump=dpath, quiet=True) == 2, "an input that is not an H.264 stream -> exit code 2, not a crash")

        say("== 3. deliberately wrong rules are rejected")
        caught = {}

        def hunt_parse(name):
            """A header-level wrong rule must change the dump of some stream (the pixel effect is reported alongside)."""
            for st in streams:
                sr.MUTANT = name
                try:
                    bad = sr.render_dump(st.raw) != st.dump
                except Exception:               # a wrong rule may crash the reader (bit position lost ...): also a rejection
                    bad = True
                finally:
                    sr.MUTANT = None
                if bad:
                    sr.MUTANT = name
                    try:
                        try:
                            pix = decode_bytes(st, None, fresh=True) != st.ref
                        except Exception:
                            pix = True
                    finally:
                        sr.MUTANT = None
                    return st.name, pix
            return None, False

        def hunt_dec(name):
            for st in streams:
                try:
                    bad = decode_bytes(st, name) != st.ref
                except Exception:
                    bad = True
                if bad:
                    return st.name
            return None
        for name, desc in sr.MUTANTS.items():
            by, pix = hunt_parse(name)
            caught[name] = by
            verdict(by is not None, f"{name}: {desc}" + (f"  [dump changes on {by}; pixels {'change' if pix else 'unchanged'}]" if by else "  [NOT CAUGHT]"))
        for name, desc in hdec.SCALING_MUTANTS.items():
            by = hunt_dec(name)
            caught[name] = by
            verdict(by is not None, f"{name}: {desc}" + (f"  [pixels change on {by}]" if by else "  [NOT CAUGHT]"))
        n_caught = sum(1 for v in caught.values() if v)
        say(f"   {n_caught} of {len(caught)} wrong rules rejected")
        verdict(len(caught) >= 14 and n_caught == len(caught), "every wrong rule rejected (at least 14 required)")
        say(f"== {'SELF-TEST PASSED' if ok_all else 'SELF-TEST FAILED'} in {time.time() - t0:.0f} s")
        return 0 if ok_all else 1
    finally:
        hdec.MUTANT = None
        sr.MUTANT = None
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="High-profile .ts or Annex B .264 file")
    ap.add_argument("--dump", help="your effective-matrix dump (Readme 5.1)")
    ap.add_argument("--yuv", help="your decoded frames (Readme 5.2)")
    ap.add_argument("--print-expected-dump", nargs=2, metavar=("INPUT", "OUT"))
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    ffref.require_tools()
    if a.self_test:
        sys.exit(self_test())
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if a.print_expected_dump:
        wd = tempfile.mkdtemp(prefix="sc86exp_")
        try:
            text = expected_dump(a.print_expected_dump[0], wd)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        with open(a.print_expected_dump[1], "w") as f:
            f.write(text)
        print(f"wrote {a.print_expected_dump[1]} ({len(text.splitlines())} lines)")
        return
    if not a.input or not (a.dump or a.yuv):
        ap.print_usage()
        die("give INPUT and at least one of --dump / --yuv (or --self-test)")
    if not os.path.exists(a.input):
        die(f"cannot read input {a.input!r}")
    sys.exit(run_checks(a.input, a.dump, a.yuv))


if __name__ == "__main__":
    main()
