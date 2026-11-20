#!/usr/bin/env python3
"""Harness for Chapter 7.3 (motion vector prediction, CAVLC P slices).

    python3 test_mvpred.py <input.ts|input.264> <mv_dump.txt>       # check your reader's output
    python3 test_mvpred.py --print-expected <input> <out.txt>       # write the expected dump (diff it against yours)
    python3 test_mvpred.py --make-variants <out_dir>                # extra test inputs (see Readme)
    python3 test_mvpred.py --self-test                              # verify the harness itself

<mv_dump.txt> is your program's output (grammar: Readme section 5).  Per slice a SLICE line, one MB line for EVERY macroblock
(skipped ones too), an END line.  The MB line must carry the keys  name refidx mvd mvp mv  (the Chapter 7.1 dump with the two
new keys mvp and mv appended is accepted as it is; other keys, PCM lines and HIST are ignored).

Truth: an independent reference (spoiler_reference/mvpred_ref.py on top of common/inter_ref.py's syntax parser) which is itself
checked, before your output is compared, against sources that do not share its code:
  1. ffmpeg's own motion vectors.  libavcodec exports them as side data (`-flags2 +export_mvs`) but no command-line tool prints the
     values, so ffmv_helper.py loads the libavcodec shared library with ctypes (no headers or compiler needed), decodes the
     stream and writes every exported vector.  ffmpeg exports one vector per macroblock partition (16x16 / skip, 16x8, 8x16)
     and one per 8x8 block of a P_8x8 macroblock (the vector of the block's first 4x4 sub-block); the reference must reproduce
     every exported (position, size, vector) exactly.  If the library cannot be loaded this check is skipped with a message.
  2. synthetic streams (spoiler_reference/mvgen.py): residual-free P pictures whose partitions copy whole-pixel blocks; the
     decoded pixels must equal the reference picture shifted by the reference's vector for EVERY 4x4 block (including each
     sub-macroblock partition and P_Skip), so mvp + mvd is proved by ffmpeg's pixels, not by any parser.
  3. every I and P slice must parse to its rbsp_stop_one_bit (syntax is the Chapter 7.1 parser).
Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output, unusable input or a failure of the reference itself.
"""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import inter_ref as ir, ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
from common.mbdump import manifest_path, decode_synth  # noqa: E402
import mvpred_ref  # noqa: E402
import mvgen  # noqa: E402

LEVEL = 2
KEYS_CMP = ["name", "refidx", "mvd", "mvp", "mv"]
SRC = "testsrc2=size=352x288:rate=25"
VARIANTS = {   # name -> (codec options, rate control)
    "x264_mv_slices4": ("-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=3:slices=4", ["-qp", "26"]),
    "x264_mv_ref4": ("-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=4:subme=9", ["-qp", "26"]),
    "x264_mv_small": ("-profile:v main -bf 0 -x264-params cabac=0:8x8dct=0:keyint=50:partitions=p8x8,p4x4,i4x4:ref=2:subme=9",
                      ["-qp", "24"]),
    "x264_mv_high": ("-profile:v high -bf 0 -x264-params cabac=0:8x8dct=1:keyint=50:partitions=all:ref=3:weightp=0", ["-qp", "26"]),
    "x264_mv_noskip": ("-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2:fast-pskip=0", ["-qp", "22"]),
    "x264_mv_wide": ("-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2:me=umh:merange=48", ["-qp", "28"]),
    "x264_mv_cip": ("-profile:v baseline -bf 0 -x264-params keyint=40:partitions=all:ref=2:constrained-intra=1:slices=3", ["-qp", "34"]),
    "x264_mv_intra_heavy": ("-profile:v baseline -bf 0 -x264-params keyint=50:ref=1:scenecut=100:intra-refresh=1", ["-qp", "30"]),
}
SYNTH = ["synth_mv", "synth_mvslices", "synth_mvsub", "synth_mvskip"]


def make_variant(name, out_ts):
    opts, rc = VARIANTS[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC, "-t", "1.2", "-pix_fmt", "yuv420p", "-c:v", "libx264", *rc,
           *opts.split(), "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg failed for {name}: {r.stderr.decode()[-500:]}")


def make_synth(name, out_264):
    data, man = mvgen.make_stream(name.replace("synth_", ""))
    open(out_264, "wb").write(data)
    json.dump(man, open(manifest_path(out_264), "w"))


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for t in ("T5", "T6"):
        shutil.copy(testfiles.get(t), os.path.join(out_dir, t + ".ts"))
        print("wrote", os.path.join(out_dir, t + ".ts"))
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for m in SYNTH:
        p = os.path.join(out_dir, m + ".264")
        make_synth(m, p)
        print("wrote", p, "and", manifest_path(p))


# ================================================================ reference
def load_reference(path, td):
    infos = ir.load(path, td)
    if not infos:
        raise SystemExit("input problem: no slice NAL units found")
    results = ir.parse_all(infos)
    mvpred_ref.fill_results(results)
    return infos, results


def structs(results):
    """-> list of dict(index, type, first_mb, status, mbs=[(addr, {key: str})]) from reference results."""
    out = []
    for r in results:
        i = r.info
        mbs = [(rec.addr, {k: str(rec.f[k]) for k in KEYS_CMP}) for rec in r.mbs]
        out.append(dict(index=i.index, type=ir.ref.SLICE_NAMES[i.slice_type], first_mb=str(i.first_mb), status=r.status, mbs=mbs))
    return out


def check_complete(results):
    bad, n = [], 0
    for r in results:
        if r.status == "skipped":
            continue
        n += 1
        if r.status != "complete":
            bad.append(f"slice {r.info.index}: reference status {r.status} ({r.error})")
    return bad, f"{n} I/P slices parse exactly to their rbsp_stop_one_bit"


# ================================================================ truth 1: ffmpeg's motion vectors
_CACHE = {}


def ffmpeg_vectors(path, td):
    """-> dict picture -> Counter of (w, h, dst_x, dst_y, mvx, mvy), or (None, reason)."""
    key = (os.path.abspath(path), os.path.getmtime(path))
    if key in _CACHE:
        return _CACHE[key]
    raw = path
    if not path.lower().endswith((".264", ".h264")):
        raw = os.path.join(td, "mvtruth.264")
        ffref.raw_h264(path, raw)
    outp = os.path.join(td, "mvtruth.txt")
    try:
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "..", "common", "ffmv_helper.py"), raw, outp], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        res = (None, "ffmpeg motion-vector export timed out")
        _CACHE[key] = res
        return res
    if r.returncode != 0 or not os.path.exists(outp):
        res = (None, "libavcodec could not be loaded through ctypes (exit code %d)" % r.returncode)
        _CACHE[key] = res
        return res
    pics = collections.defaultdict(collections.Counter)
    for line in open(outp):
        n, src, w, h, dx, dy, mx, my, sc = map(int, line.split())
        if sc != 4:
            res = (None, f"unexpected motion_scale {sc}")
            _CACHE[key] = res
            return res
        pics[n][(w, h, dx, dy, mx, my)] += 1
    _CACHE[key] = (dict(pics), "")
    return _CACHE[key]


def expected_exports(results):
    """What ffmpeg should export, from the reference: dict picture -> Counter."""
    pics = collections.defaultdict(collections.Counter)
    quad = [(0, 0, 8, 8), (8, 0, 8, 8), (0, 8, 8, 8), (8, 8, 8, 8)]
    shapes = {"P_Skip": [(0, 0, 16, 16)], "P_L0_16x16": [(0, 0, 16, 16)], "P_L0_L0_16x8": [(0, 0, 16, 8), (0, 8, 16, 8)],
              "P_L0_L0_8x16": [(0, 0, 8, 16), (8, 0, 8, 16)], "P_8x8": quad, "P_8x8ref0": quad}
    for r in results:
        if r.info.skip_reason:
            continue
        wm = r.info.pic_width_in_mbs
        for rec in r.mbs:
            sh = shapes.get(rec.f["name"])
            if not sh:
                continue
            X0, Y0 = (rec.addr % wm) * 16, (rec.addr // wm) * 16
            for (x, y, w, h) in sh:
                _, mx, my = rec.grid[(y // 4) * 4 + x // 4]
                pics[r.info.picture][(w, h, X0 + x + w // 2, Y0 + y + h // 2, mx, my)] += 1
    return pics


def check_vs_ffmpeg_vectors(path, infos, results, td):
    if any(i.slice_type not in (0, 2) for i in infos):
        return [], "ffmpeg motion-vector value check skipped: the stream has B slices (output order differs from decoding order)"
    truth, why = ffmpeg_vectors(path, td)
    if truth is None:
        return [], f"ffmpeg motion-vector value check SKIPPED: {why}; falling back to the pixel checks of the synthetic streams"
    mine = expected_exports(results)
    npics = infos[-1].picture + 1
    problems, total = [], 0
    for p in range(npics):
        a, b = mine.get(p, collections.Counter()), truth.get(p, collections.Counter())
        total += sum(b.values())
        if a != b:
            only_a, only_b = list((a - b).items())[:2], list((b - a).items())[:2]
            problems.append(f"picture {p}: reference vs ffmpeg export differ; reference-only (w,h,dst_x,dst_y,mvx,mvy): {only_a}; "
                            f"ffmpeg-only: {only_b}")
    return problems, f"ffmpeg's exported vectors ({total} vectors in {npics} pictures) equal the reference's, value for value"


# ================================================================ truth 2: pixels of synthetic streams
def pixel_check(results, manifest, frames):
    wm, hm = manifest["width_mbs"], manifest["height_mbs"]
    W_, H_ = wm * 16, hm * 16
    problems, nblk, nsens = [], 0, 0
    for r in results:
        p = r.info.picture
        if p == 0:
            continue
        for rec in r.mbs:
            if rec.grid is None:
                continue
            X0, Y0 = (rec.addr % wm) * 16, (rec.addr // wm) * 16
            for k, (rf, mx, my) in enumerate(rec.grid):
                bx, by = X0 + (k % 4) * 4, Y0 + (k // 4) * 4
                if mx % 4 or my % 4:
                    problems.append(f"picture {p} addr {rec.addr}: fractional vector {mx}:{my} in a whole-pixel stream")
                    continue
                if p - 1 - rf < 0:
                    problems.append(f"picture {p} addr {rec.addr}: refIdx {rf} before the first picture")
                    continue
                refpic = frames[p - 1 - rf]

                def blk(dx, dy):
                    return bytes(refpic[min(max(by + yy + dy, 0), H_ - 1) * W_ + min(max(bx + xx + dx, 0), W_ - 1)]
                                 for yy in range(4) for xx in range(4))
                got = bytes(frames[p][(by + yy) * W_ + bx + xx] for yy in range(4) for xx in range(4))
                exp = blk(mx // 4, my // 4)
                nblk += 1
                if got != exp:
                    problems.append(f"picture {p} MB {rec.addr} 4x4 block {k}: decoded pixels are not the block of picture "
                                    f"{p - 1 - rf} displaced by the vector {mx}:{my} (refIdx {rf})")
                elif got != blk(mx // 4 + 1, my // 4) and got != blk(mx // 4, my // 4 + 1):
                    nsens += 1
    return problems, f"{nblk} 4x4 blocks of inter macroblocks equal their reference picture displaced by the vector ({nsens} blocks are sensitive to a 1-pixel error)"


def synth_checks(path, results, td):
    man = json.load(open(manifest_path(path)))
    problems, frames = decode_synth(path, man, td)
    if problems:
        return problems, ""
    return pixel_check(results, man, frames)


def reference_trust(path, infos, results, td, quiet=False):
    say = (lambda *a: None) if quiet else (lambda *a: print(" ", *a))
    problems = []
    steps = [(check_complete, (results,)), (check_vs_ffmpeg_vectors, (path, infos, results, td))]
    if path.lower().endswith(".264") and os.path.exists(manifest_path(path)):
        steps.append((synth_checks, (path, results, td)))
    for fn, args in steps:
        p, msg = fn(*args)
        problems += p
        say(msg if not p else f"{fn.__name__} FAILED")
    return problems


# ================================================================ reader output
def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def parse_reader(path):
    try:
        text = open(path).read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    slices, cur = [], None
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        kv = {}
        for t in tok[2:]:
            if "=" in t:
                k, v = t.split("=", 1)
                kv[k] = v
        kind = tok[0]
        if kind == "SLICE":
            if len(tok) < 2 or not tok[1].isdigit():
                die(f"malformed reader output, line {no}: SLICE needs a numeric index: {line!r}")
            cur = dict(index=int(tok[1]), type=kv.get("type"), first_mb=kv.get("first_mb"), status=None, mbs=[])
            slices.append(cur)
        elif kind == "MB":
            if cur is None:
                die(f"malformed reader output, line {no}: MB line before any SLICE line")
            if len(tok) < 3 or not tok[2].isdigit():
                die(f"malformed reader output, line {no}: MB needs <slice index> <address>: {line!r}")
            miss = [k for k in KEYS_CMP if k not in kv]
            if miss:
                die(f"malformed reader output, line {no}: MB line lacks the key(s) {', '.join(miss)}: {line[:100]!r}")
            cur["mbs"].append((int(tok[2]), {k: kv[k] for k in KEYS_CMP}))
        elif kind == "END":
            if cur is None:
                die(f"malformed reader output, line {no}: END before SLICE")
            cur["status"] = kv.get("status")
        elif kind in ("PCM", "HIST"):
            continue
        else:
            die(f"malformed reader output, line {no}: unknown line type {kind!r} (allowed: SLICE MB END, and ignored PCM HIST)")
    if not slices:
        die("malformed reader output: no SLICE lines")
    return slices


def grid_diff(exp, got):
    e, g = exp.split(","), (got or "").split(",")
    for i in range(max(len(e), len(g))):
        a = e[i] if i < len(e) else "<none>"
        b = g[i] if i < len(g) else "<none>"
        if a != b:
            return f"4x4 block {i} (row {i // 4}, column {i % 4}): expected {a}, got {b}"
    return ""


def compare(rep, exp, act):
    rep.check(len(act) == len(exp), f"number of SLICE blocks: expected {len(exp)} (one per slice NAL unit, in stream order), got {len(act)}")
    for e, a in zip(exp, act):
        k = e["index"]
        rep.check(a["index"] == k, f"slice block #{k}: got index {a['index']}")
        rep.check(a["type"] == e["type"], f"SLICE {k} type: expected {e['type']}, got {a['type']}")
        rep.check(a["first_mb"] == e["first_mb"], f"SLICE {k} first_mb: expected {e['first_mb']}, got {a['first_mb']}")
        rep.check(len(a["mbs"]) == len(e["mbs"]), f"SLICE {k}: expected {len(e['mbs'])} MB lines (every macroblock, skipped too), got {len(a['mbs'])}")
        rep.check(a["status"] == e["status"] or e["status"] != "skipped" and a["status"] == "complete",
                  f"SLICE {k} END status: expected {e['status']}, got {a['status']}")
        for j, ((ea, ekv), (aa, akv)) in enumerate(zip(e["mbs"], a["mbs"])):
            rep.check(ea == aa, f"SLICE {k} MB #{j}: expected macroblock address {ea}, got {aa}")
            for key in KEYS_CMP:
                if akv[key] == ekv[key]:
                    rep.check(True, "")
                    continue
                ctx = f" (macroblock {ekv['name']}, refidx={ekv['refidx']} mvd={ekv['mvd']})"
                if key == "mv":
                    rep.check(False, f"SLICE {k} MB addr {ea} mv: first differing {grid_diff(ekv['mv'], akv['mv'])}{ctx}; expected mvp={ekv['mvp']}")
                elif key == "mvp" and ekv[key] != "-":
                    e_, g_ = ekv[key].split(","), akv[key].split(",")
                    j_ = next((n for n in range(max(len(e_), len(g_))) if (e_ + [None] * 99)[n] != (g_ + [None] * 99)[n]), 0)
                    rep.check(False, f"SLICE {k} MB addr {ea} mvp: expected {ekv[key]}, got {akv[key]} (first differing prediction block: "
                                     f"entry {j_}, 0-based in mvd order){ctx}")
                else:
                    rep.check(False, f"SLICE {k} MB addr {ea} {key}: expected {ekv[key]}, got {akv[key]}{ctx}")


def run_check(path, dump, quiet=False):
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        probs = reference_trust(path, infos, results, td, quiet=quiet)
        if probs:
            print("HARNESS PROBLEM (the reference disagrees with an independent source; this is not your bug):")
            for p in probs[:10]:
                print("  ", p)
            sys.exit(2)
        exp = structs(results)
    act = parse_reader(dump)
    rep = Report(f"motion vector prediction of {os.path.basename(path)}")
    compare(rep, exp, act)
    nmb = sum(len(s["mbs"]) for s in exp)
    print(f"  {len(exp)} slices, {nmb} macroblocks compared (keys: {' '.join(KEYS_CMP)})")
    rep.finish()


# ================================================================ self-test
MUT_INPUTS = ["T6", "x264_mv_slices4", "x264_mv_small", "x264_mv_cip", "synth_mv", "synth_mvslices", "synth_mvsub", "synth_mvskip"]
MUTANTS = [
    ("median_is_a", "no median: mvp = mvA"),
    ("mean_not_median", "average of A, B, C instead of the median"),
    ("no_single_ref_rule", "'only one neighbour has refIdx == refIdx' rule missing"),
    ("ref_ge", "single-neighbour rule tests 'refIdx >= 0' instead of equality with the current refIdx"),
    ("no_bc_copy", "B and C unavailable with A available: mvB = mvC = mvA rule missing"),
    ("no_c_to_d", "D does not replace an unavailable C"),
    ("c_inside_mb_available", "C inside the macroblock but later in decoding order treated as available (intra-like)"),
    ("sub_width8", "predPartWidth of sub-macroblock partitions taken as 8 (C too far right)"),
    ("dir16x8_swap", "16x8: upper partition uses A, lower uses B (swapped)"),
    ("dir8x16_swap", "8x16: left partition uses C, right uses A (swapped)"),
    ("skip_ignores_availability", "P_Skip: zero vector when A or B is unavailable missing"),
    ("skip_zero_a_only", "P_Skip: zero test looks at A only"),
    ("skip_zero_any_ref", "P_Skip: zero test ignores refIdx == 0"),
    ("intra_unavailable", "intra neighbours treated as unavailable instead of refIdx -1"),
    ("slice_edge_ignored", "neighbours in other slices of the picture count as available"),
    ("skip_mv_not_stored", "a skipped macroblock stores refIdx -1 (not its inferred vector with refIdx 0)"),
]


def self_test():
    ok_all = True

    def verdict(label, ok, detail=""):
        nonlocal ok_all
        ok_all &= ok
        print(f"  {'ok  ' if ok else 'BAD '} {label}{(': ' + detail) if detail else ''}")
    with tempfile.TemporaryDirectory() as td:
        print("[1] inputs and the reference against independent truth (ffmpeg's vectors; pixels of synthetic streams)")
        inputs = {t: testfiles.get(t) for t in ("T5", "T6")}
        for n in VARIANTS:
            p = os.path.join(td, n + ".ts")
            make_variant(n, p)
            inputs[n] = p
        for m in SYNTH:
            p = os.path.join(td, m + ".264")
            make_synth(m, p)
            inputs[m] = p
        loaded = {}
        for name, p in inputs.items():
            sub = os.path.join(td, "w_" + name)
            os.makedirs(sub)
            infos, results = load_reference(p, sub)
            loaded[name] = (p, infos, results, sub)
            probs = reference_trust(p, infos, results, sub, quiet=True)
            nmb = sum(len(r.mbs) for r in results)
            nvec = sum(sum(c.values()) for c in expected_exports(results).values())
            truth, why = ffmpeg_vectors(p, sub) if all(i.slice_type in (0, 2) for i in infos) else (None, "B slices")
            src = "ffmpeg vectors" if truth is not None else ("pixels" if name.startswith("synth") else f"no independent source ({why})")
            verdict(f"{name}: {len(infos)} slices, {nmb} macroblocks, {nvec} exported-vector positions, truth: {src}", not probs,
                    "; ".join(probs[:2]))
        have_ff = ffmpeg_vectors(inputs["T6"], loaded["T6"][3])[0] is not None
        verdict("ffmpeg motion-vector values were available (libavcodec loaded through ctypes)", have_ff,
                "" if have_ff else "not available on this machine: the synthetic pixel checks are the only independent truth")
        # coverage
        names, nsub, sk0, skA = collections.Counter(), collections.Counter(), 0, 0
        for n_, (p, infos, results, _) in loaded.items():
            for r in results:
                for rec in r.mbs:
                    names[rec.f["name"]] += 1
                    if rec.f["sub"] != "-":
                        nsub.update(rec.f["sub"].split(","))
                    if rec.f["name"] == "P_Skip":
                        sk0 += rec.f["mvp"] == "0:0"
                        skA += rec.f["mvp"] != "0:0"
        need = ["P_Skip", "P_L0_16x16", "P_L0_L0_16x8", "P_L0_L0_8x16", "P_8x8", "P_8x8ref0", "I_NxN", "I_PCM"]
        verdict(f"coverage: partition kinds {dict(names)}; sub_mb_types {dict(nsub)}; skipped macroblocks with zero / non-zero vector {sk0} / {skA}",
                all(names.get(k) for k in need) and len(nsub) == 4 and sk0 > 0 and skA > 0)

        print("[2] the harness on correct and on corrupted reader output")
        name = "T6"
        p, infos, results, sub = loaded[name]
        text = ir.render_dump(results, LEVEL)
        good = os.path.join(td, "good.txt")
        open(good, "w").write(text)
        exp = structs(results)

        def judge(txt, label, want_pass):
            f = os.path.join(td, "x.txt")
            open(f, "w").write(txt)
            rep = Report(label)
            compare(rep, exp, parse_reader(f))
            verdict(f"{label} -> {'PASS' if not rep.failures else 'FAIL'}", (not rep.failures) == want_pass,
                    "" if (not rep.failures) == want_pass else (rep.failures[0] if rep.failures else "unexpected pass"))
        judge(text, "correct output", True)
        lines = text.splitlines()
        i = next(k for k, l in enumerate(lines) if l.startswith("MB") and "name=P_8x8 " in l)
        bad = lines[:]
        bad[i] = re.sub(r" mv=(-?\d+):(-?\d+):(-?\d+)", lambda m: f" mv={m.group(1)}:{int(m.group(2)) + 1}:{m.group(3)}", bad[i], count=1)
        judge("\n".join(bad) + "\n", "one component of one mv grid entry changed", False)
        bad = lines[:]
        bad[i] = re.sub(r" mvp=(-?\d+):(-?\d+)", lambda m: f" mvp={m.group(1)}:{int(m.group(2)) - 1}", bad[i], count=1)
        judge("\n".join(bad) + "\n", "one mvp changed", False)
        j = max(k for k, l in enumerate(lines) if l.startswith("MB"))
        judge("\n".join(lines[:j] + lines[j + 1:]) + "\n", "last macroblock line missing", False)
        k = next(k for k, l in enumerate(lines) if "name=P_Skip" in l)
        bad = lines[:]
        bad[k] = re.sub(r" mv=\S+", " mv=-", bad[k])
        judge("\n".join(bad) + "\n", "skipped macroblock without a stored vector grid", False)
        k = next(k for k, l in enumerate(lines) if "name=I_" in l and l.startswith("MB"))
        bad = lines[:]
        bad[k] = re.sub(r" mv=\S+", " mv=" + ",".join(["0:0:0"] * 16), bad[k])
        judge("\n".join(bad) + "\n", "intra macroblock printed with refIdx 0 vectors", False)
        mini = "\n".join(re.sub(r" (mb_type|i16_mode|chroma_cbp|luma_cbp|bit|t8x8|modes|chroma_pred|cbp|qp_delta|qp|nc|tc|tcl|tccb|tccr|coef|skip_run|sub)=\S+", "", l)
                         for l in lines if not l.startswith(("PCM", "HIST"))) + "\n"
        judge(mini, "minimal dump (only name refidx mvd mvp mv) is accepted", True)

        print("[3] deliberately wrong prediction rules: each must be caught by the dump comparison")
        truth_cache = {n: ffmpeg_vectors(loaded[n][0], loaded[n][3])[0] if all(i.slice_type in (0, 2) for i in loaded[n][1]) else None
                       for n in loaded}
        synth_frames = {}
        for n in SYNTH:
            man = json.load(open(manifest_path(loaded[n][0])))
            pr, fr = decode_synth(loaded[n][0], man, td)
            synth_frames[n] = (man, fr)
        base = {n: structs(loaded[n][2]) for n in loaded}
        for mut, desc in MUTANTS:
            mvpred_ref.MUTANT = mut
            caught, by_truth = None, []
            try:
                for n in MUT_INPUTS:
                    pth, infos, results, _ = loaded[n]
                    mvpred_ref.fill_results(results)
                    s = structs(results)
                    if s != base[n]:
                        rep = Report(mut)
                        compare(rep, base[n], s)
                        if rep.failures and caught is None:
                            caught = (n, rep.failures[0])
                    if truth_cache.get(n) is not None:
                        mine, tr = expected_exports(results), truth_cache[n]
                        if any(mine.get(pp, collections.Counter()) != tr.get(pp, collections.Counter()) for pp in range(infos[-1].picture + 1)):
                            by_truth.append("ffmpeg:" + n)
                    if n in synth_frames and pixel_check(results, *synth_frames[n])[0]:
                        by_truth.append("pixels:" + n)
            finally:
                mvpred_ref.MUTANT = None
                for n in MUT_INPUTS:
                    mvpred_ref.fill_results(loaded[n][2])
            verdict(f"mutant {mut} ({desc})", caught is not None,
                    (f"caught on {caught[0]}: {caught[1][:100]}" if caught else "NOT CAUGHT")
                    + (f" | independent truth also flags it ({len(by_truth)} inputs, e.g. {by_truth[0]})" if by_truth else " | independent truth: not flagged"))
    print("SELF-TEST " + ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true", help="verify the harness itself (references, corruptions, mutants)")
    ap.add_argument("--make-variants", metavar="OUT_DIR", help="write extra test inputs (x264 P streams and synthetic streams)")
    ap.add_argument("--print-expected", action="store_true", help="write the expected dump of <input> to the second argument")
    ap.add_argument("args", nargs="*", help="<input.ts|.264> <reader_output.txt>")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if len(a.args) != 2:
        ap.error("need <input> and <reader output> (or --self-test / --make-variants / --print-expected)")
    inp, out = a.args
    if not os.path.exists(inp):
        print(f"input file not found: {inp}", file=sys.stderr)
        sys.exit(2)
    if a.print_expected:
        with tempfile.TemporaryDirectory() as td:
            infos, results = load_reference(inp, td)
            open(out, "w").write(ir.render_dump(results, LEVEL))
        print("wrote", out)
        return
    run_check(inp, out)


if __name__ == "__main__":
    main()
