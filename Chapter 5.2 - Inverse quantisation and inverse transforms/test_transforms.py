#!/usr/bin/env python3
"""Harness for Chapter 5.2 (Inverse quantisation and inverse transforms).

    python3 test_transforms.py --write-vectors <vec_file> [--seed N]
    python3 test_transforms.py --vectors <vec_file> <vec_results>
    python3 test_transforms.py --ffmpeg-check
    python3 test_transforms.py --self-test

--write-vectors writes the deterministic vector file (hand-checkable cases + random cases over qp 0..51).
Your program reads it and writes <vec_results>: one line of integers per vector, in order. --vectors recomputes the
expected results with the reference in transref.py and compares line by line. Exact grammars: see the chapter Readme.

--ffmpeg-check proves the REFERENCE (not your program): it writes a synthetic one-macroblock-per-slice stream with
known coefficients over every QP and many chroma offsets, decodes it with ffmpeg and checks that transref.py
reproduces every decoded sample.  (Needs ffmpeg; no reader output involved.)

Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output or unusable input.
"""
import argparse
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402
import transref as T  # noqa: E402


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ======================================================================= vector grammar and reference
# op -> (list of (keyword, count)) ; optional trailing flag handled for SCALE4X4 / RESIDUAL4X4
SPEC = {
    "LEVELSCALE": None,        # LEVELSCALE <m> <i> <j>
    "CHROMAQP": None,          # CHROMAQP <qpy> <offset>
    "CHROMAQPS": None,         # CHROMAQPS <qpy> <chroma_qp_index_offset> <second_chroma_qp_index_offset>
    "ZIGZAG4X4": [("levels", 16)],
    "SCALE4X4": [("qp", 1), ("coeffs", 16)],
    "IDCT4X4": [("d", 16)],
    "RESIDUAL4X4": [("qp", 1), ("coeffs", 16)],
    "BLOCK4X4": [("qp", 1), ("levels", 16)],
    "LUMADC": [("qp", 1), ("coeffs", 16)],
    "CHROMADC": [("qp", 1), ("coeffs", 4)],
    "RECON4X4": [("pred", 16), ("res", 16)],
    "I16RESID": [("qp", 1), ("dc", 16), ("ac", 240)],
    "CHROMARESID": [("qp", 1), ("dc", 4), ("ac", 60)],
}


def parse_vector(line):
    """-> (op, dict of fields, dc_pre_scaled flag).  Raises ValueError with a message on a malformed line."""
    t = line.split()
    op = t[0]
    if op not in SPEC:
        raise ValueError(f"unknown op {op}")
    if SPEC[op] is None:
        n = 2 if op == "CHROMAQP" else 3
        if len(t) != n + 1:
            raise ValueError(f"{op} wants {n} numbers")
        return op, {"v": [int(x) for x in t[1:]]}, False
    f, p, flag = {}, 1, False
    for kw, cnt in SPEC[op]:
        if p >= len(t) or t[p] != kw:
            raise ValueError(f"expected keyword {kw}")
        vals = t[p + 1:p + 1 + cnt]
        if len(vals) != cnt:
            raise ValueError(f"{kw}: want {cnt} numbers")
        f[kw] = [int(x) for x in vals]
        p += 1 + cnt
    if p < len(t):
        if op in ("SCALE4X4", "RESIDUAL4X4") and t[p:] == ["dc_pre_scaled"]:
            flag = True
        else:
            raise ValueError("trailing garbage")
    return op, f, flag


def compute(line):
    """Expected result (list of ints) of one vector line."""
    op, f, flag = parse_vector(line)
    tr = T.Tracker()
    if op == "LEVELSCALE":
        return [T.level_scale(*f["v"])]
    if op == "CHROMAQP":
        return [T.chroma_qp(*f["v"])]
    if op == "CHROMAQPS":
        qpy, off_cb, off_cr = f["v"]
        return [T.chroma_qp(qpy, off_cb), T.chroma_qp(qpy, off_cr)]
    if op == "ZIGZAG4X4":
        return T.flat(T.inverse_zigzag(f["levels"]))
    if op == "SCALE4X4":
        return T.flat(T.scale4x4(T.unflat(f["coeffs"], 4), f["qp"][0], flag, tr))
    if op == "IDCT4X4":
        return T.flat(T.idct4x4(T.unflat(f["d"], 4), tr))
    if op == "RESIDUAL4X4":
        return T.flat(T.residual4x4(T.unflat(f["coeffs"], 4), f["qp"][0], flag, tr))
    if op == "BLOCK4X4":
        return T.flat(T.residual4x4(T.inverse_zigzag(f["levels"]), f["qp"][0], False, tr))
    if op == "LUMADC":
        return T.flat(T.luma_dc(T.unflat(f["coeffs"], 4), f["qp"][0], tr))
    if op == "CHROMADC":
        return T.flat(T.chroma_dc(f["coeffs"], f["qp"][0], tr))
    if op == "RECON4X4":
        return [T.clip1(p + r) for p, r in zip(f["pred"], f["res"])]
    if op == "I16RESID":
        ac = [f["ac"][15 * b:15 * b + 15] for b in range(16)]
        return T.flat(T.i16_residual(f["qp"][0], f["dc"], ac, tr))
    if op == "CHROMARESID":
        ac = [f["ac"][15 * b:15 * b + 15] for b in range(4)]
        return T.flat(T.chroma_residual(f["qp"][0], f["dc"], ac, tr))


def conforming(line):
    """True when no intermediate value leaves the signed 16-bit range (8.5.10 - 8.5.12)."""
    op, f, flag = parse_vector(line)
    tr = T.Tracker()
    try:
        if op == "SCALE4X4" or op == "RESIDUAL4X4":
            c = T.unflat(f["coeffs"], 4)
            fn = T.scale4x4 if op == "SCALE4X4" else T.residual4x4
            fn(c, f["qp"][0], flag, tr)
        elif op == "BLOCK4X4":
            T.residual4x4(T.inverse_zigzag(f["levels"]), f["qp"][0], False, tr)
        elif op == "IDCT4X4":
            T.idct4x4(T.unflat(f["d"], 4), tr)
        elif op == "LUMADC":
            T.luma_dc(T.unflat(f["coeffs"], 4), f["qp"][0], tr)
        elif op == "CHROMADC":
            T.chroma_dc(f["coeffs"], f["qp"][0], tr)
        elif op == "I16RESID":
            T.i16_residual(f["qp"][0], f["dc"], [f["ac"][15 * b:15 * b + 15] for b in range(16)], tr)
        elif op == "CHROMARESID":
            T.chroma_residual(f["qp"][0], f["dc"], [f["ac"][15 * b:15 * b + 15] for b in range(4)], tr)
    except OverflowError:
        return False
    return tr.range_ok and all(-32768 <= v <= 32767 for k in f for v in f[k] if k not in ("pred", "res"))


# ======================================================================= vector generation
def fmt(*parts):
    out = []
    for p in parts:
        out.append(p if isinstance(p, str) else " ".join(str(x) for x in p) if isinstance(p, (list, tuple)) else str(p))
    return " ".join(out)


def hand_vectors():
    z16 = [0] * 16
    one = lambda i, v: [v if k == i else 0 for k in range(16)]  # noqa: E731
    L = []
    c = lambda s: L.append(("#" + s, None))  # noqa: E731
    v = lambda s: L.append((s, None))  # noqa: E731
    c(" LevelScale4x4 = 16 * normAdjust4x4: all six rows of the v table, the three position classes")
    for m in range(6):
        for (i, j) in ((0, 0), (1, 1), (0, 1), (2, 2), (3, 1), (2, 3)):
            v(fmt("LEVELSCALE", m, i, j))
    c(" chroma QP: identity below 30, the table above, clipping of qPI to 0..51 (8-bit video: QpBdOffsetC = 0)")
    for qpy, off in ((0, 0), (29, 0), (30, 0), (51, 0), (26, 4), (45, 6), (51, 12), (0, -12), (10, -12), (40, -12), (51, -12),
                     (35, 1), (36, 1), (37, 0), (22, 8)):
        v(fmt("CHROMAQP", qpy, off))
    c(" both chroma components: Cb uses chroma_qp_index_offset, Cr second_chroma_qp_index_offset")
    for qpy, ocb, ocr in ((30, 0, 6), (40, -4, 3), (26, 12, -12), (45, 2, 2)):
        v(fmt("CHROMAQPS", qpy, ocb, ocr))
    c(" zig-zag placement: scan position k holds the value k+1; the raster result shows where each one lands")
    v(fmt("ZIGZAG4X4 levels", list(range(1, 17))))
    c(" one-coefficient blocks: DC only at qp 28 (d00 = 256, residual 4 everywhere) and qp 12 (rounding branch, residual 1)")
    v(fmt("RESIDUAL4X4 qp 28 coeffs", one(0, 1)))
    v(fmt("RESIDUAL4X4 qp 12 coeffs", one(0, 1)))
    v(fmt("SCALE4X4 qp 12 coeffs", one(0, 1)))
    v(fmt("RESIDUAL4X4 qp 26 coeffs", [4, 1, 2, 0, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
    v(fmt("SCALE4X4 qp 5 coeffs", [3, -3, 2, -2, 1, -1, 0, 0, 5, -5, 7, -7, 1, 1, 1, 1]))
    v(fmt("SCALE4X4 qp 24 coeffs", [3, -3, 2, -2, 1, -1, 0, 0, 5, -5, 7, -7, 1, 1, 1, 1]))
    v(fmt("SCALE4X4 qp 35 coeffs", [3, -3, 2, -2, 1, -1, 0, 0, 5, -5, 7, -7, 1, 1, 1, 1]))
    c(" the >>1 butterflies: single d coefficients, odd values so that the shifts lose a bit")
    for k in (1, 2, 4, 5, 15):
        v(fmt("IDCT4X4 d", one(k, 64 * 5 + 3)))
    v(fmt("IDCT4X4 d", [64, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
    v(fmt("IDCT4X4 d", [-100, 33, 7, -9, 12, -64, 5, 5, 0, 1, 2, 3, -7, 7, -7, 7]))
    c(" DC pre-scaled: c00 is already d00 (only the other 15 are scaled)")
    v(fmt("RESIDUAL4X4 qp 20 coeffs", one(0, 160), "dc_pre_scaled"))
    v(fmt("RESIDUAL4X4 qp 20 coeffs", [160, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], "dc_pre_scaled"))
    v(fmt("SCALE4X4 qp 20 coeffs", [160, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], "dc_pre_scaled"))
    c(" levels in scan order straight to a residual block")
    v(fmt("BLOCK4X4 qp 26 levels", [4, 1, -1, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, -1]))
    c(" luma DC (Intra16x16): Hadamard then scaling; qp 30 (rounding branch), qp 36 (shift branch)")
    v(fmt("LUMADC qp 30 coeffs", one(0, 1)))
    v(fmt("LUMADC qp 36 coeffs", one(0, 1)))
    v(fmt("LUMADC qp 30 coeffs", one(1, 1)))
    v(fmt("LUMADC qp 30 coeffs", [3, -1, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]))
    c(" chroma DC: qp is QP'c; ((f * LS) << (qP/6)) >> 5")
    v("CHROMADC qp 30 coeffs 1 0 0 0")
    v("CHROMADC qp 30 coeffs 1 1 1 1")
    v("CHROMADC qp 20 coeffs 5 -3 2 -1")
    v("CHROMADC qp 3 coeffs 5 -3 2 -1")
    v("CHROMADC qp 39 coeffs 5 -3 2 -1")
    c(" prediction + residual with clipping")
    v(fmt("RECON4X4 pred", [128] * 16, "res", [-200, -129, -128, -1, 0, 1, 127, 128, 200, 255, -300, 5, 6, 7, 8, 9]))
    c(" whole Intra16x16 luma and chroma residuals from level lists")
    v(fmt("I16RESID qp 28 dc", one(0, 1), "ac", z16[:15] * 16 if False else [0] * 240))
    ac = [0] * 240
    ac[15 * 3 + 0] = 4
    ac[15 * 12 + 2] = -3
    v(fmt("I16RESID qp 28 dc", [2, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1], "ac", ac))
    ac = [0] * 60
    ac[15 * 2 + 1] = 5
    v(fmt("CHROMARESID qp 29 dc", [3, 0, -2, 1], "ac", ac))
    return L


def rand_levels(rng, n, amp, density):
    return [rng.choice((-1, 1)) * rng.randint(1, amp) if rng.random() < density else 0 for _ in range(n)]


def try_make(rng, build, start=None):
    """build(amp) -> line; shrink amp until the vector is conforming."""
    amp = start or rng.choice((1, 2, 5, 20, 100, 600, 2063, 4000, 32767))
    while True:
        line = build(amp)
        if conforming(line):
            return line
        amp = max(1, amp // 3)


def random_vectors(rng):
    L = []
    c = lambda s: L.append(("#" + s, None))  # noqa: E731
    c(" random cases: every qp 0..51 for every op, mixed amplitudes up to the 16-bit limit")
    for qp in range(52):
        for rep in range(3):
            dens = rng.choice((0.1, 0.4, 1.0))
            L.append((try_make(rng, lambda a: fmt("RESIDUAL4X4 qp", qp, "coeffs", rand_levels(rng, 16, a, dens))), None))
            L.append((try_make(rng, lambda a: fmt("RESIDUAL4X4 qp", qp, "coeffs", rand_levels(rng, 16, a, dens), "dc_pre_scaled")), None))
            L.append((try_make(rng, lambda a: fmt("SCALE4X4 qp", qp, "coeffs", rand_levels(rng, 16, a, dens))), None))
            L.append((try_make(rng, lambda a: fmt("BLOCK4X4 qp", qp, "levels", rand_levels(rng, 16, a, dens))), None))
            L.append((try_make(rng, lambda a: fmt("LUMADC qp", qp, "coeffs", rand_levels(rng, 16, a, dens))), None))
            L.append((try_make(rng, lambda a: fmt("CHROMADC qp", qp, "coeffs", rand_levels(rng, 4, a, 0.7))), None))
        L.append((try_make(rng, lambda a: fmt("I16RESID qp", qp, "dc", rand_levels(rng, 16, a, 0.4), "ac", rand_levels(rng, 240, max(1, a // 8), 0.2))), None))
        L.append((try_make(rng, lambda a: fmt("CHROMARESID qp", qp, "dc", rand_levels(rng, 4, a, 0.7), "ac", rand_levels(rng, 60, max(1, a // 8), 0.3))), None))
    c(" random IDCT inputs, small and extreme (including full-range d values)")
    for _ in range(80):
        L.append((try_make(rng, lambda a: fmt("IDCT4X4 d", rand_levels(rng, 16, a, rng.choice((0.2, 1.0))))), None))
    c(" extreme single coefficients: largest level each position tolerates at qp 0, 12, 23, 24, 35, 36, 51")
    for qp in (0, 12, 23, 24, 35, 36, 51):
        for pos in (0, 1, 5, 15):
            for sgn in (1, -1):
                L.append((try_make(rng, lambda a: fmt("SCALE4X4 qp", qp, "coeffs", [sgn * a if k == pos else 0 for k in range(16)]), 32767), None))
                L.append((try_make(rng, lambda a: fmt("RESIDUAL4X4 qp", qp, "coeffs", [sgn * a if k == pos else 0 for k in range(16)]), 32767), None))
    c(" random prediction + residual")
    for _ in range(30):
        L.append((fmt("RECON4X4 pred", [rng.randrange(256) for _ in range(16)], "res", [rng.randint(-400, 400) for _ in range(16)]), None))
    c(" chroma QP: all qpy 0..51 for a spread of offsets")
    for off in (-12, -7, -1, 0, 1, 5, 12):
        for qpy in range(52):
            L.append((fmt("CHROMAQP", qpy, off), None))
    c(" Cb and Cr QP from distinct offsets")
    for _ in range(40):
        ocb, ocr = rng.sample(range(-12, 13), 2)
        L.append((fmt("CHROMAQPS", rng.randrange(52), ocb, ocr), None))
    c(" every normAdjust position")
    for m in range(6):
        for i in range(4):
            for j in range(4):
                L.append((fmt("LEVELSCALE", m, i, j), None))
    return L


def gen_vectors(seed=52):
    rng = random.Random(seed)
    return [l for l, _ in hand_vectors() + random_vectors(rng)]


def vectors_text(lines):
    head = ["# Chapter 5.2 test vectors. Lines starting with # are comments (no result line for them).",
            "# One result line (integers separated by single spaces) per other line, in order."]
    return "\n".join(head + lines) + "\n"


# ======================================================================= checking
def read_vectors(path):
    vecs = []
    for n, raw in enumerate(open(path).read().splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        try:
            parse_vector(s)
        except ValueError as e:
            die(f"error: {path} line {n}: {e}")
        vecs.append(s)
    return vecs


LAYOUT = {"RESIDUAL4X4": 4, "SCALE4X4": 4, "IDCT4X4": 4, "BLOCK4X4": 4, "LUMADC": 4, "CHROMADC": 2, "RECON4X4": 4,
          "I16RESID": 16, "CHROMARESID": 8, "ZIGZAG4X4": 4}


def where(op, k):
    w = LAYOUT.get(op)
    return f"element {k}" + (f" (row {k // w}, column {k % w})" if w else "")


def check_vectors(vec_path, res_path, rep):
    vecs = read_vectors(vec_path)
    if not os.path.exists(res_path):
        die(f"error: no such results file {res_path}")
    raw = [l.strip() for l in open(res_path).read().splitlines()]
    res = []
    for n, l in enumerate(raw, 1):
        if not l or l.startswith("#"):
            continue
        try:
            res.append([int(x) for x in l.split()])
        except ValueError:
            die(f"error: {res_path} line {n}: not a list of integers: {l[:60]!r}")
    rep.check(len(res) == len(vecs), f"results file has {len(res)} result lines, vector file has {len(vecs)} vectors")
    for i, (v, got) in enumerate(zip(vecs, res)):
        exp = compute(v)
        if exp == got:
            rep.check(True, "")
            continue
        short = v if len(v) < 150 else v[:150] + " ..."
        if len(exp) != len(got):
            rep.check(False, f"vector {i} [{short}]: expected {len(exp)} numbers, got {len(got)}")
        else:
            k = next(k for k in range(len(exp)) if exp[k] != got[k])
            nd = sum(1 for a, b in zip(exp, got) if a != b)
            rep.check(False, f"vector {i} [{short}]: first difference at {where(v.split()[0], k)}: expected {exp[k]}, got {got[k]} "
                             f"({nd} of {len(exp)} differ)")


def results_text(lines):
    return "\n".join(" ".join(str(x) for x in compute(l)) for l in lines) + "\n"


# ======================================================================= ffmpeg check of the reference
def ffmpeg_check(rep, seed=52, tmpdir=None):
    import recongen
    data, pics = recongen.make_stream(seed)
    with tempfile.TemporaryDirectory() as tmp:
        s264, yuv = os.path.join(tmp, "s.264"), os.path.join(tmp, "s.yuv")
        open(s264, "wb").write(data)
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-i", s264, "-fps_mode", "passthrough", "-f", "rawvideo",
                       "-pix_fmt", "yuv420p", yuv], timeout=120)
        if r.returncode != 0 or r.stderr.strip():
            die("error: ffmpeg could not decode the synthetic stream: " + r.stderr.decode(errors="replace")[:300])
        out = open(yuv, "rb").read()
    W, H = 16 * recongen.W_MBS, 16 * recongen.H_MBS
    fs = W * H * 3 // 2
    rep.check(len(out) == fs * len(pics), f"ffmpeg produced {len(out)} bytes, expected {fs * len(pics)} ({len(pics)} frames)")
    if len(out) != fs * len(pics):
        return
    kinds = {"i16": 0, "nxn": 0}
    qps = set()
    for p, pic in enumerate(pics):
        fr = out[p * fs:(p + 1) * fs]
        for a, mb in enumerate(pic["mbs"]):
            kinds[mb["kind"]] += 1
            qps.add(mb["qp"])
            exp, _ = T.reconstruct_mb(mb["kind"], mb["qp"], pic["offs"], mb["arrays"])
            mx, my = a % recongen.W_MBS, a // recongen.W_MBS
            got = bytearray(fr[(16 * my + i) * W + 16 * mx + j] for i in range(16) for j in range(16))
            for off in (W * H, W * H + W * H // 4):
                got += bytes(fr[off + (8 * my + i) * (W // 2) + 8 * mx + j] for i in range(8) for j in range(8))
            ok = bytes(got) == exp
            msg = ""
            if not ok:
                k = next(k for k in range(384) if got[k] != exp[k])
                pl, q = ("Y", k) if k < 256 else ("Cb", k - 256) if k < 320 else ("Cr", k - 320)
                n = 16 if pl == "Y" else 8
                msg = (f"picture {p} macroblock {a} ({mb['kind']}, qp {mb['qp']}, chroma offsets {pic['offs']}): "
                       f"{pl} sample ({q % n}, {q // n}) reference {exp[k]} ffmpeg {got[k]}")
            rep.check(ok, msg)
    print(f"ffmpeg check: {len(pics)} pictures, {sum(kinds.values())} macroblocks "
          f"({kinds['i16']} Intra16x16, {kinds['nxn']} I_NxN), {len(qps)} distinct QP_Y values")


# ======================================================================= self-test
def run_sub(*args):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def self_test():
    ok = True

    def expect(tag, want, *args):
        nonlocal ok
        rc, out = run_sub(*args)
        print(f"{tag} -> exit {rc} (want {want})")
        if rc != want:
            print("\n".join("    " + l for l in out.splitlines()[:4]))
        ok &= rc == want

    # hand-derived spot checks (see the Readme worked examples)
    hand = {
        "RESIDUAL4X4 qp 28 coeffs 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0": [4] * 16,
        "RESIDUAL4X4 qp 12 coeffs 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0": [1] * 16,
        "SCALE4X4 qp 12 coeffs 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0": [40] + [0] * 15,
        "LUMADC qp 30 coeffs 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0": [80] * 16,
        "LUMADC qp 36 coeffs 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0": [160] * 16,
        "CHROMADC qp 30 coeffs 1 0 0 0": [160] * 4,
        "CHROMADC qp 30 coeffs 1 1 1 1": [640, 0, 0, 0],
        "CHROMAQP 40 0": [36], "CHROMAQPS 30 0 6": [29, 34], "CHROMAQP 51 12": [39], "CHROMAQP 5 -12": [0], "CHROMAQP 29 1": [29],
        "ZIGZAG4X4 levels 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16": [1, 2, 6, 7, 3, 5, 8, 13, 4, 9, 12, 14, 10, 11, 15, 16],
        "LEVELSCALE 5 1 1": [464], "LEVELSCALE 0 0 0": [160], "LEVELSCALE 3 2 1": [288],
        "RECON4X4 pred " + " ".join(["250"] * 16) + " res 10 -300 0 0 0 0 0 0 0 0 0 0 0 0 0 0": [255, 0] + [250] * 14,
    }
    for line, want in hand.items():
        got = compute(line)
        good = got == want
        ok &= good
        if not good:
            print("hand check FAILED:", line, "->", got, "want", want)
    print(f"reference vs {len(hand)} hand-computed values:", "OK" if ok else "BAD")
    # the reference must be independent of ffmpeg for the vectors, but is validated against it here
    rep = Report("reference vs ffmpeg")
    rep.finish = lambda: None
    ffmpeg_check(rep)
    n_bad = len(rep.failures)
    print(f"reference vs ffmpeg: {rep.checks - n_bad}/{rep.checks} checks passed" + ("" if not n_bad else "  " + rep.failures[0]))
    ok &= n_bad == 0
    # mutants of the reference must be caught by the ffmpeg check
    for name, mut in (("normAdjust table", lambda: T.NORM.__setitem__(3, (14, 23, 19))),
                      ("Table 8-15 entry", lambda: T.QPC.__setitem__(7, 35)),
                      ("final rounding", None),
                      ("Cb/Cr offsets swapped", "swap")):
        saved_norm, saved_qpc, saved_idct, saved_rmb = list(T.NORM), list(T.QPC), T.idct4x4, T.reconstruct_mb
        if mut == "swap":
            T.reconstruct_mb = lambda kind, qp, offs, arr, _o=saved_rmb: _o(kind, qp, offs[::-1], arr)
        elif mut:
            mut()
        else:
            def bad_idct(d, tr, _o=saved_idct):
                r = _o(d, tr)
                return [[v + (1 if (d[0][0] & 1) else 0) for v in row] for row in r]
            T.idct4x4 = bad_idct
        rep = Report("mutant")
        ffmpeg_check(rep)
        T.NORM[:], T.QPC[:], T.idct4x4, T.reconstruct_mb = saved_norm, saved_qpc, saved_idct, saved_rmb
        print(f"ffmpeg check with mutant reference ({name}): {len(rep.failures)} mismatches (want > 0)")
        ok &= len(rep.failures) > 0
    # vector protocol
    with tempfile.TemporaryDirectory() as tmp:
        vf, rf = os.path.join(tmp, "v.txt"), os.path.join(tmp, "r.txt")
        expect("write-vectors", 0, "--write-vectors", vf)
        lines = read_vectors(vf)
        ops = {}
        for i, l in enumerate(lines):
            ops.setdefault(l.split()[0], i)
        print(f"{len(lines)} vectors, ops: " + ", ".join(sorted(ops)))
        ok &= set(ops) == set(SPEC)
        res = results_text(lines).splitlines()
        open(rf, "w").write("\n".join(res) + "\n")
        expect("vectors, correct results", 0, "--vectors", vf, rf)
        for op, i in sorted(ops.items()):
            r2 = list(res)
            nums = r2[i].split()
            nums[-1] = str(int(nums[-1]) + 1)
            r2[i] = " ".join(nums)
            p = os.path.join(tmp, f"bad_{op}.txt")
            open(p, "w").write("\n".join(r2) + "\n")
            expect(f"vectors, last number of the first {op} off by one", 1, "--vectors", vf, p)
        open(rf + "1", "w").write("\n".join(res[:-1]) + "\n")
        expect("vectors, last result line missing", 1, "--vectors", vf, rf + "1")
        open(rf + "2", "w").write("1 2 three\n")
        expect("vectors, malformed results", 2, "--vectors", vf, rf + "2")
        i = ops["I16RESID"]
        r2 = list(res)
        r2[i] = " ".join(r2[i].split()[:255])
        open(rf + "3", "w").write("\n".join(r2) + "\n")
        expect("vectors, one number missing from a line", 1, "--vectors", vf, rf + "3")
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--write-vectors", metavar="VEC_FILE")
    ap.add_argument("--vectors", nargs=2, metavar=("VEC_FILE", "VEC_RESULTS"))
    ap.add_argument("--ffmpeg-check", action="store_true")
    ap.add_argument("--seed", type=int, default=52, help="seed of the random vectors / synthetic stream (default 52)")
    a = ap.parse_args()
    if a.self_test:
        ffref.require_tools()
        self_test()
    if a.write_vectors:
        open(a.write_vectors, "w").write(vectors_text(gen_vectors(a.seed)))
        print(f"wrote {a.write_vectors}")
        return
    if not a.vectors and not a.ffmpeg_check:
        ap.error("need --write-vectors, --vectors, --ffmpeg-check or --self-test")
    rep = Report("Chapter 5.2 inverse quantisation and inverse transforms")
    if a.ffmpeg_check:
        ffref.require_tools()
        ffmpeg_check(rep, a.seed)
    if a.vectors:
        if not os.path.exists(a.vectors[0]):
            die(f"error: no such vector file {a.vectors[0]}")
        check_vectors(a.vectors[0], a.vectors[1], rep)
    rep.finish()


if __name__ == "__main__":
    main()
