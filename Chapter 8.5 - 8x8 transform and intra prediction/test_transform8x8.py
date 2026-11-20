#!/usr/bin/env python3
"""Harness for Chapter 8.5 (High profile: transform_size_8x8_flag, the 8x8 transform, Intra 8x8 prediction, CAVLC / CABAC 8x8
residuals, deblocking of 8x8-transformed macroblocks).

Usage (grammars: Readme, section 5):
  python3 test_transform8x8.py INPUT [--dump DUMP] [--yuv OUT.yuv] [--yuv-nodeblock OUT_ND.yuv]
        check your output for a High-profile .ts / Annex B .264 file (at least one of the three outputs):
        --dump           your syntax dump (slice by slice, macroblock by macroblock)           -> section 5.1
        --yuv            your decoded frames, cropped planar yuv420p, display order            -> section 5.2
        --yuv-nodeblock  the same decoded WITHOUT the deblocking filter anywhere (diagnosis)   -> section 5.2
  python3 test_transform8x8.py --print-expected-dump INPUT OUT   write the expected dump (to diff against yours)
  python3 test_transform8x8.py --make-variants DIR               write test streams (synthetic + x264) with expected dumps
  python3 test_transform8x8.py --self-test                       verify this harness (takes about a minute)

Truth.
  * Pixels: ffmpeg (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`; with `-skip_loop_filter all` for --yuv-nodeblock),
    compared with common/yuvcmp.py (first differing frame / plane / macroblock / pixel).
  * Syntax dump: spoiler_reference/stream8.py, a reference parser for CAVLC (common/inter_ref.py) and CABAC (parse8.py) slices.
    A parse only counts if every slice ends exactly where the data ends (CABAC: end_of_slice_flag = 1 at the last macroblock and the
    stop bit right after; CAVLC: no data left after the last macroblock).  The reference decoder built on the same parse
    (spoiler_reference/hdec.py) reproduces ffmpeg's pixels bit for bit on every x264 High stream of this chapter, which proves the
    dump's t8 flags, modes and 8x8 coefficients are the real ones.  Synthetic streams (spoiler_reference/t8gen.py, an independent
    CABAC ENCODER) have their expected dump from the construction.
Exit 0 = pass, 1 = your output differs, 2 = unusable input / output or a harness problem.
"""
import argparse
import collections
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SPOIL = os.path.join(HERE, "spoiler_reference")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, SPOIL)
from common import ffref, testfiles, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import stream8  # noqa: E402
import parse8  # noqa: E402
import intra8ref  # noqa: E402
import hdec  # noqa: E402
import t8gen  # noqa: E402
import bsref_b  # noqa: E402
import transref  # noqa: E402

FIELDS = ["mbt", "t8", "modes", "imode", "cbp", "qp", "coef"]
FIELD_HELP = {
    "mbt": "mb_type of the slice's own table (Tables 7-11 / 7-13 / 7-14), 'skip' for skipped macroblocks; a wrong value here means the parse "
           "went wrong earlier (slice header, mb_skip, a previous macroblock)",
    "t8": "transform_size_8x8_flag: I_NxN reads it right after mb_type (before the modes); an inter macroblock reads it after "
          "coded_block_pattern only if the luma cbp is non-zero, transform_8x8_mode_flag is 1, no sub-macroblock has a partition "
          "smaller than 8x8 (a direct sub-macroblock counts as small unless direct_8x8_inference_flag) and, for B_Direct_16x16, "
          "direct_8x8_inference_flag is 1; CABAC context 399 + condTermFlagA + condTermFlagB (a neighbour counts if available and its "
          "own flag is 1); skipped, I_PCM and Intra16x16 macroblocks have '-', other macroblocks 0 when the flag is not coded",
    "modes": "prev_intra4x4_pred_mode_flag / rem_intra4x4_pred_mode (16 elements) or, with t8 = 1, the prev_intra8x8 / rem_intra8x8 "
             "elements (only FOUR); 'p' = prev flag set, a digit = rem value",
    "imode": "the final Intra4x4PredMode (16 values) or Intra8x8PredMode (4 values, t8 = 1): prediction = Min(A, B) of the neighbouring "
             "blocks, 2 (DC) when a neighbouring macroblock is unavailable, an unavailable macroblock beats everything; a neighbour "
             "that is not I_NxN counts as 2; Intra 8x8 reads an Intra 4x4 neighbour at blk8 * 4 + 1 (left) / + 2 (above), Intra 4x4 "
             "reads an Intra 8x8 neighbour's mode of the 8x8 block containing the 4x4 block",
    "cbp": "coded_block_pattern",
    "qp": "QP_Y after mb_qp_delta (carried through skipped / I_PCM / cbp = 0 macroblocks)",
    "coef": "residual: with t8 = 1 an 8x8 block is ONE entry 'luma8<b8>:pos=value,...' with positions 0..63 in the 8x8 zig-zag scan "
            "(CABAC: ctxBlockCat 5; CAVLC: four 4x4 blocks whose coefficient i goes to position 4 * i + k of the 8x8 block, k = 0..3 "
            "the 4x4 block); other blocks as in Chapters 4.4 / 8.3",
}


def die(msg, code=2):
    print(f"error: {msg}")
    sys.exit(code)


# ================================================================ dump grammar
MB_VALUE = {
    "mbt": re.compile(r"(skip|\d{1,2})$"),
    "t8": re.compile(r"([01]|-)$"),
    "modes": re.compile(r"([p0-7]{16}|[p0-7]{4}|-)$"),
    "imode": re.compile(r"(-|[0-8](,[0-8]){15}|[0-8](,[0-8]){3})$"),
    "cbp": re.compile(r"(\d{1,2}|-)$"),
    "qp": re.compile(r"\d{1,2}$"),
    "coef": re.compile(r"(-|[a-z0-9]+:(-|\d+=-?\d+(,\d+=-?\d+)*)(;[a-z0-9]+:(-|\d+=-?\d+(,\d+=-?\d+)*))*)$"),
}


def parse_dump(text, who):
    """-> dict slice index -> dict(type, first_mb, mbs=[(addr, {field: value}, lineno)], end).  Malformed -> exit 2."""
    slices, cur, cur_idx = {}, None, None
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        w = line.split()

        def bad(msg):
            die(f"malformed {who}, line {no}: {msg}: {line!r}")

        def kv(tokens):
            d = {}
            for t in tokens:
                if "=" not in t:
                    bad(f"expected key=value, found {t!r}")
                k, v = t.split("=", 1)
                d[k] = v
            return d
        if w[0] == "SLICE":
            if len(w) < 2 or not w[1].isdigit():
                bad("SLICE needs a slice index")
            d = kv(w[2:])
            if d.get("type") not in ("I", "P", "B") or not d.get("first_mb", "x").isdigit():
                bad("SLICE needs type=I|P|B and first_mb=<number>")
            if int(w[1]) in slices:
                bad("slice index repeated")
            cur_idx = int(w[1])
            cur = dict(type=d["type"], first_mb=int(d["first_mb"]), mbs=[], end=None)
            slices[cur_idx] = cur
        elif w[0] in ("MB", "END"):
            if cur is None or cur["end"] is not None:
                bad(f"{w[0]} line outside a SLICE ... END block")
            if len(w) < 2 or not w[1].isdigit() or int(w[1]) != cur_idx:
                bad(f"slice index must be {cur_idx} inside the block of slice {cur_idx}")
            if w[0] == "END":
                d = kv(w[2:])
                if not d.get("mbs", "x").isdigit():
                    bad("END needs mbs=<number>")
                cur["end"] = int(d["mbs"])
            else:
                if len(w) < 3 or not w[2].isdigit():
                    bad("MB needs a macroblock address")
                d = kv(w[3:])
                extra = [k for k in d if k not in FIELDS]
                if extra:
                    bad(f"unknown field {extra[0]}= (allowed: {', '.join(FIELDS)})")
                for f in FIELDS:
                    if f not in d:
                        bad(f"missing field {f}=")
                    if not MB_VALUE[f].match(d[f]):
                        bad(f"bad value {d[f]!r} for {f}")
                cur["mbs"].append((int(w[2]), {f: d[f] for f in FIELDS}, no))
        else:
            bad("unknown line type (allowed: SLICE, MB, END, # comments)")
    if cur is not None and cur["end"] is None:
        die(f"malformed {who}: the block of slice {cur_idx} has no END line")
    if not slices:
        die(f"{who} contains no SLICE block")
    return slices


def block_diff(x, y):
    """x, y: '<kind><idx>:<list>' strings of the same block -> the first differing coefficient position, in words."""
    def pos(b):
        lst = b.split(":", 1)[1]
        return {} if lst == "-" else {int(t.split("=")[0]): int(t.split("=")[1]) for t in lst.split(",")}
    ex, gx = pos(x), pos(y)
    for k in sorted(set(ex) | set(gx)):
        if ex.get(k) != gx.get(k):
            fmt = lambda v: "0 (not listed)" if v is None else str(v)
            n = sum(1 for j in set(ex) | set(gx) if ex.get(j) != gx.get(j))
            return (f"block {x.split(':')[0]}, first differing position {k}: expected {fmt(ex.get(k))}, got {fmt(gx.get(k))} "
                    f"({n} position(s) of this block differ)")
    return f"expected {x[:100]!r} got {y[:100]!r}"


def coef_diff(e, g):
    eb = [] if e == "-" else e.split(";")
    gb = [] if g == "-" else g.split(";")
    for j in range(max(len(eb), len(gb))):
        x = eb[j] if j < len(eb) else None
        y = gb[j] if j < len(gb) else None
        if x != y:
            where = f"block #{j} in decoding order"
            if x is None:
                return f"{where}: you print an extra block {y[:80]!r} (expected only {len(eb)} blocks)"
            if y is None:
                return f"{where}: expected {x[:80]!r}, your list ends after {len(gb)} blocks"
            if x.split(":")[0] == y.split(":")[0]:
                return f"{where}: {block_diff(x, y)}"
            return f"{where}: expected block {x.split(':')[0]!r} ({x[:60]!r}) got block {y.split(':')[0]!r} ({y[:60]!r})"
    return f"expected {e[:100]!r} got {g[:100]!r}"


def check_dump(exp, got, rep):
    """exp, got: parse_dump results.  One check per slice; the first wrong macroblock of each slice is described."""
    for i in sorted(got):
        if i not in exp:
            rep.check(False, f"slice {i}: not a slice of this stream (slices are numbered from 0 in stream order): remove it")
    for i, e in exp.items():
        g = got.get(i)
        if g is None:
            rep.check(False, f"slice {i} ({e['type']}, first_mb {e['first_mb']}): no SLICE block in your output")
            continue
        if g["type"] != e["type"] or g["first_mb"] != e["first_mb"]:
            rep.check(False, f"slice {i}: expected type={e['type']} first_mb={e['first_mb']}, got type={g['type']} first_mb={g['first_mb']}")
            continue
        first_bad, nbad = None, 0
        for k, (addr, f, ln) in enumerate(e["mbs"]):
            if k >= len(g["mbs"]):
                nbad += 1
                first_bad = first_bad or (f"slice {i} ({e['type']}): your output stops after {len(g['mbs'])} macroblocks, the slice has "
                                          f"{len(e['mbs'])} (next is macroblock {addr}); the parse failed or ended too early")
                break
            ga, gf, gln = g["mbs"][k]
            diffs = [x for x in FIELDS if gf[x] != f[x]]
            if ga == addr and not diffs:
                continue
            nbad += 1
            if first_bad is None:
                if ga != addr:
                    first_bad = f"slice {i}, macroblock #{k} in the slice: expected address {addr}, got {ga} (your line {gln})"
                else:
                    d0 = diffs[0]
                    what = coef_diff(f["coef"], gf["coef"]) if d0 == "coef" else f"expected {f[d0]!r} got {gf[d0]!r}"
                    first_bad = (f"slice {i} ({e['type']}), macroblock {addr} (#{k}), field {d0}: {what}"
                                 + (f" (also differs: {', '.join(diffs[1:])})" if len(diffs) > 1 else "")
                                 + f" [your line {gln}]  hint: {FIELD_HELP[d0]}")
        if first_bad is None and len(g["mbs"]) != len(e["mbs"]):
            first_bad = f"slice {i}: you print {len(g['mbs'])} macroblocks, expected {len(e['mbs'])}"
        elif first_bad is None and g["end"] != e["end"]:
            first_bad = f"slice {i}: END mbs={g['end']} but the slice has {e['end']} macroblocks"
        rep.check(first_bad is None, (first_bad or "") + (f" ... and {nbad - 1} more macroblock(s) of this slice differ" if nbad > 1 else ""))
    return rep


def compare_dump_text(exp_text, got_text):
    """-> list of failure messages (empty = equal)."""
    rep = Report("dump", max_shown=10 ** 6)
    check_dump(parse_dump(exp_text, "expected dump"), parse_dump(got_text, "dump"), rep)
    return rep.failures


# ================================================================ truth
def reference_parse(path, workdir, infos=None):
    """-> (infos, results).  Exit 2 if the reference cannot parse the stream completely."""
    try:
        infos = infos or stream8.load(path, workdir)
        results = stream8.parse_all(infos)
    except stream8.Unsupported as e:
        die(f"this stream uses a feature outside Chapter 8.5 ({e})")
    except SystemExit:
        raise
    except Exception as e:                  # not an H.264 stream ffmpeg can demux, truncated file, ...
        why = "ffmpeg found no H.264 video in it" if isinstance(e, FileNotFoundError) else f"{type(e).__name__}: {e}"
        die(f"cannot read {path} as an H.264 .ts / Annex B stream ({why})")
    for r in results:
        if r.status != "complete":
            die(f"the reference parser cannot parse slice {r.info.index} of {path}: {r.error or r.note} "
                f"(is this a frame-coded 8-bit 4:2:0 H.264 stream without scaling matrices?)")
    return infos, results


def expected_dump(path, workdir):
    infos, results = reference_parse(path, workdir)
    return stream8.render_dump(results)


def ffmpeg_yuv(path, out, nodeblock=False):
    args = ["ffmpeg", "-v", "error", "-y"] + (["-skip_loop_filter", "all"] if nodeblock else []) + [
        "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    r = ffref.run(args, timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")
    return out


def stream_wh(path):
    info = yuvref.stream_info(path)
    if not info["width"]:
        die(f"cannot read the picture size of {path}")
    return info["width"], info["height"]


def check_yuv(path, mine, nodeblock, rep, workdir):
    ref = ffmpeg_yuv(path, os.path.join(workdir, "ref_nd.yuv" if nodeblock else "ref.yuv"), nodeblock)
    w, h = stream_wh(path)
    if not os.path.exists(mine):
        die(f"cannot read {mine!r}")
    res = yuvcmp.compare_yuv(ref, mine, w, h, quiet=True)
    label = "--yuv-nodeblock" if nodeblock else "--yuv"
    msg = (f"{label}: " + res["text"].replace("\n", "\n    ")) if not res["ok"] else ""
    if not res["ok"] and not nodeblock and not res.get("size_problems"):
        msg += ("\n    hint: if --yuv-nodeblock matches ffmpeg but this does not, only the deblocking is wrong (Readme 3.9); "
                "if both differ, look at the first wrong macroblock: Intra 8x8 prediction / the 8x8 transform / the syntax dump.")
    rep.check(res["ok"], msg)


# ================================================================ main check
def run_checks(path, dump=None, yuv=None, yuv_nd=None, quiet=False):
    """-> exit code (0 pass / 1 differs / 2 unusable).  Prints the report (nothing if quiet)."""
    if quiet:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return run_checks(path, dump, yuv, yuv_nd, False)
    wd = tempfile.mkdtemp(prefix="t8chk_")
    try:
        rep = Report(f"Chapter 8.5 checks of {os.path.basename(path)}", max_shown=10)
        if dump:
            try:
                got = open(dump, "r", errors="replace").read()
            except OSError as e:
                die(f"cannot read dump {dump!r}: {e}")
            infos, results = reference_parse(path, wd)
            check_dump(parse_dump(stream8.render_dump(results), "expected dump"), parse_dump(got, "your dump"), rep)
        for p, nd in ((yuv, False), (yuv_nd, True)):
            if p:
                check_yuv(path, p, nd, rep, wd)
        try:
            rep.finish()
        except SystemExit as e:
            return e.code
    except SystemExit as e:                 # die(): the message is printed already
        return e.code
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ================================================================ test streams
SMALL_SRC = {"mandel": "mandelbrot=size=128x96:rate=25,noise=alls=12:allf=t",
             "test": "testsrc2=size=128x96:rate=25,noise=alls=20:allf=t"}


def make_x264(out, src, qp, params, frames=8):
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SMALL_SRC[src], "-frames:v", str(frames), "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-profile:v", "high", "-qp", str(qp), "-x264-params", params, "-f", "mpegts", out]
    ffref.run(args, timeout=300)
    if not os.path.exists(out):
        die("ffmpeg/libx264 could not make a test stream")
    return out


SYN_SPECS = [  # name, seed, small, direct_8x8_inference
    ("syn_t8_1", 101, True, 1), ("syn_t8_2", 102, True, 0), ("syn_t8_3", 103, True, 1), ("syn_t8_4", 104, True, 0),
    ("syn_t8_5", 105, False, 1), ("syn_t8_6", 106, False, 0)]
X264_SMALL = [  # name, source, qp, params
    ("tex_cabac", "mandel", 24, "keyint=8:bframes=2:ref=2"), ("tex_cavlc", "mandel", 24, "keyint=8:bframes=2:ref=2:cabac=0"),
    ("tex_intra", "test", 20, "keyint=1"), ("tex_intra_cavlc", "test", 20, "keyint=1:cabac=0"),
    ("tex_parts", "mandel", 22, "keyint=8:bframes=2:ref=3:partitions=all:subme=7"),
    ("tex_cip", "mandel", 24, "keyint=8:bframes=1:ref=2:constrained-intra=1"), ("tex_qp40", "mandel", 40, "keyint=8:bframes=2"),
    ("tex_p_cavlc", "mandel", 30, "keyint=8:bframes=0:ref=3:cabac=0")]
X264_RECIPES = ["T11_high", "T11_cavlc", "T11_intra", "T11_intra_cavlc", "T11_p", "T11_p_cavlc", "T11_slices_aq",
                "T11_slices_aq_cavlc"]


def write_syn(dirpath, name, seed, small, inference):
    data, man = t8gen.build(seed, small, inference)
    p = os.path.join(dirpath, name + ".264")
    with open(p, "wb") as f:
        f.write(data)
    with open(os.path.join(dirpath, name + ".expected.txt"), "w") as f:
        f.write(man["expected"])
    return p, man["expected"]


def make_variants(d):
    os.makedirs(d, exist_ok=True)
    wd = tempfile.mkdtemp(prefix="t8var_")
    try:
        for name, seed, small, inf in SYN_SPECS:
            p, _ = write_syn(d, name, seed, small, inf)
            print("wrote", p, "(+ .expected.txt)", "" if small else "[huge levels: check --dump only, ffmpeg's pixels are not defined]")
        for name, src, qp, params in X264_SMALL:
            p = make_x264(os.path.join(d, name + ".ts"), src, qp, params)
            with open(os.path.join(d, name + ".expected.txt"), "w") as f:
                f.write(expected_dump(p, wd))
            print("wrote", p, "(+ .expected.txt)")
        for r in X264_RECIPES:
            p = os.path.join(d, r + ".ts")
            shutil.copyfile(testfiles.get(r), p)
            with open(os.path.join(d, r + ".expected.txt"), "w") as f:
                f.write(expected_dump(p, wd))
            print("wrote", p, "(+ .expected.txt)")
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ================================================================ self-test
def frames_bytes(pics):
    return b"".join(hdec.frame_bytes(p) for p in pics)


class Stream:
    def __init__(self, name, path, expected=None, pixels=True, cabac=True):
        self.name, self.path, self.expected, self.pixels, self.cabac = name, path, expected, pixels, cabac
        self.infos = None
        self.cache = {}
        self.ref = {}


def decode_bytes(st, mutant=None, deblock=True):
    hdec.MUTANT = mutant
    hdec.DEBLOCK = deblock
    try:
        return frames_bytes(hdec.decode(st.path, cache=st.cache))
    finally:
        hdec.MUTANT = None
        hdec.DEBLOCK = True


def self_test():
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="t8selftest_")
    ok_all = True

    def say(msg):
        print(msg, flush=True)

    def verdict(cond, msg):
        nonlocal ok_all
        say(("  ok    " if cond else "  FAIL  ") + msg)
        ok_all = ok_all and cond
    try:
        streams = []
        for name, seed, small, inf in SYN_SPECS:
            p, exp = write_syn(tmp, name, seed, small, inf)
            streams.append(Stream(name, p, exp, pixels=small))
        for name, src, qp, params in X264_SMALL:
            streams.append(Stream(name, make_x264(os.path.join(tmp, name + ".ts"), src, qp, params), cabac="cabac=0" not in params))
        say(f"== 1. reference vs truth on {len(streams)} small streams")
        for st in streams:
            st.infos = stream8.load(st.path, tmp)
            res = stream8.parse_all(st.infos)
            complete = all(r.status == "complete" for r in res)
            dump = stream8.render_dump(res)
            st.dump = dump
            if st.expected is not None:
                verdict(complete and dump == st.expected, f"{st.name}: reference parse = the generator's construction ({len(dump.splitlines())} lines)")
            else:
                verdict(complete, f"{st.name}: reference parse complete (every slice ends exactly)")
            if st.pixels:
                st.ref[True] = open(ffmpeg_yuv(st.path, os.path.join(tmp, st.name + ".ref.yuv")), "rb").read()
                st.ref[False] = open(ffmpeg_yuv(st.path, os.path.join(tmp, st.name + ".nd.yuv"), True), "rb").read()
                hdec.STATS = collections.Counter()
                a = decode_bytes(st, None, True)
                stats = hdec.STATS
                hdec.STATS = None
                b = decode_bytes(st, None, False)
                verdict(a == st.ref[True], f"{st.name}: reference decoder = ffmpeg pixels (deblocked), {len(a)} bytes")
                verdict(b == st.ref[False], f"{st.name}: reference decoder = ffmpeg pixels (-skip_loop_filter all)")
                st.modes = sorted(int(k.split()[-1]) for k in stats if k.startswith("i8 mode"))
            else:
                r = ffref.run(["ffmpeg", "-v", "error", "-i", st.path, "-f", "null", "-"], timeout=120)
                verdict(r.returncode == 0 and not r.stderr, f"{st.name}: ffmpeg decodes it without errors (huge levels, pixels not compared)")
        seen = set()
        for st in streams:
            seen |= set(getattr(st, "modes", []))
        verdict(seen == set(range(9)), f"all nine Intra 8x8 modes were reconstructed in the pixel-checked streams (seen {sorted(seen)})")

        say("== 2. the checker accepts good output and rejects damaged output")
        base = next(s for s in streams if s.name == "tex_cabac")
        dpath = os.path.join(tmp, "good.txt")
        open(dpath, "w").write(base.dump)
        rc = run_checks(base.path, dump=dpath, yuv=os.path.join(tmp, "tex_cabac.ref.yuv"), yuv_nd=os.path.join(tmp, "tex_cabac.nd.yuv"), quiet=True)
        verdict(rc == 0, "known-good dump + both YUV files -> PASS")
        lines = base.dump.splitlines()
        t8idx = [i for i, ln in enumerate(lines) if " t8=1 " in ln]
        nxn = [i for i, ln in enumerate(lines) if " modes=" in ln and " modes=- " not in ln and " t8=1 " in ln]
        coefl = [i for i, ln in enumerate(lines) if "luma8" in ln]

        def corrupt(name, fn):
            ls = list(lines)
            fn(ls)
            bp = os.path.join(tmp, name + ".txt")
            open(bp, "w").write("\n".join(ls) + "\n")
            rc = run_checks(base.path, dump=bp, quiet=True)
            verdict(rc == 1, f"dump damaged ({name}) -> FAIL")
        corrupt("t8_flipped", lambda ls: ls.__setitem__(t8idx[3], ls[t8idx[3]].replace(" t8=1 ", " t8=0 ")))
        corrupt("mode_changed", lambda ls: ls.__setitem__(nxn[1], re.sub(r"modes=\S+", "modes=pppp", ls[nxn[1]])))
        corrupt("imode_changed", lambda ls: ls.__setitem__(nxn[2], re.sub(r"imode=\S+", "imode=8,8,8,8", ls[nxn[2]])))
        corrupt("coef_changed", lambda ls: ls.__setitem__(coefl[2], re.sub(r"luma8(\d):(\d+)=", r"luma8\1:\2=-", ls[coefl[2]], count=1)))
        corrupt("macroblock_missing", lambda ls: ls.pop(t8idx[5]))
        corrupt("last_slice_missing", lambda ls: [ls.pop() for _ in range(len(ls) - max(i for i, x in enumerate(ls) if x.startswith("SLICE")))])
        bp = os.path.join(tmp, "malformed.txt")
        open(bp, "w").write("SLICE 0 type=I first_mb=0\nMB 0 0 mbt=0 t8=1 modes=pppp imode=2,2,2,2 cbp=0\nEND 0 mbs=1\n")
        verdict(run_checks(base.path, dump=bp, quiet=True) == 2, "a dump with a missing field -> exit code 2 (malformed), not FAIL")
        jp = os.path.join(tmp, "junk.ts")
        open(jp, "wb").write(bytes(range(256)) * 40)
        verdict(run_checks(jp, dump=dpath, quiet=True) == 2, "an input that is not an H.264 stream -> exit code 2, not a crash")
        # YUV damage
        good = base.ref[True]
        bad = bytearray(good)
        bad[len(good) // 3] ^= 1
        bp = os.path.join(tmp, "bad.yuv")
        open(bp, "wb").write(bytes(bad))
        verdict(run_checks(base.path, yuv=bp, quiet=True) == 1, "one wrong pixel in --yuv -> FAIL")
        open(bp, "wb").write(good[:-1000])
        verdict(run_checks(base.path, yuv=bp, quiet=True) == 1, "truncated --yuv -> FAIL")
        open(bp, "wb").write(base.ref[False])
        verdict(run_checks(base.path, yuv=bp, quiet=True) == 1, "the no-deblock picture offered as --yuv -> FAIL (deblocking matters)")

        say("== 3. deliberately wrong rules are rejected")
        mut_streams_pix = [s for s in streams if s.pixels]
        caught = {}

        def hunt(name, kind):
            if kind == "parse":
                for st in streams:
                    if not st.cabac:
                        continue
                    stream8.CABAC_MUTANT = name
                    try:
                        res = stream8.parse_all(st.infos)
                        bad = any(r.status != "complete" for r in res) or stream8.render_dump(res) != st.dump
                    except Exception as e:     # a wrong rule may crash the reader: also a rejection
                        bad = True
                    finally:
                        stream8.CABAC_MUTANT = None
                    if bad:
                        return st.name
                return None
            if kind == "dec":
                for st in mut_streams_pix:
                    try:
                        bad = decode_bytes(st, name, True) != st.ref[True]
                    except Exception:           # a wrong rule may crash the decoder (read outside the picture ...): a rejection too
                        bad = True
                    if bad:
                        return st.name
                return None
        groups = []
        groups += [(n, d, "parse") for n, d in parse8.MUTANTS.items() if n.startswith(("t8_", "cat5_", "modes8_"))]
        groups += [(n, d, "dec") for n, d in intra8ref.MUTANTS.items()]
        groups += [(n, d, "dec") for n, d in hdec.DECODER_MUTANTS.items() if n.startswith("t8_")]
        groups += [("t8_deblock_internal", bsref_b.MUTANTS["t8_deblock_internal"], "dec")]
        for name, desc, kind in groups:
            by = hunt(name, kind)
            caught[name] = by
            verdict(by is not None, f"{name}: {desc}" + (f"  [caught on {by}]" if by else "  [NOT CAUGHT]"))
        n_caught = sum(1 for v in caught.values() if v)
        say(f"   {n_caught} of {len(caught)} wrong rules rejected")
        verdict(len(caught) >= 14 and n_caught == len(caught), "every wrong rule rejected (at least 14 required)")
        say(f"== {'SELF-TEST PASSED' if ok_all else 'SELF-TEST FAILED'} in {time.time() - t0:.0f} s")
        return 0 if ok_all else 1
    finally:
        hdec.MUTANT = None
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="High-profile .ts or Annex B .264 file")
    ap.add_argument("--dump", help="your syntax dump (Readme 5.1)")
    ap.add_argument("--yuv", help="your decoded frames (Readme 5.2)")
    ap.add_argument("--yuv-nodeblock", help="your decoded frames without any deblocking (Readme 5.2)")
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
        wd = tempfile.mkdtemp(prefix="t8exp_")
        try:
            text = expected_dump(a.print_expected_dump[0], wd)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        with open(a.print_expected_dump[1], "w") as f:
            f.write(text)
        print(f"wrote {a.print_expected_dump[1]} ({len(text.splitlines())} lines)")
        return
    if not a.input or not (a.dump or a.yuv or a.yuv_nodeblock):
        ap.print_usage()
        die("give INPUT and at least one of --dump / --yuv / --yuv-nodeblock (or --self-test)")
    if not os.path.exists(a.input):
        die(f"cannot read input {a.input!r}")
    sys.exit(run_checks(a.input, a.dump, a.yuv, a.yuv_nodeblock))


if __name__ == "__main__":
    main()
