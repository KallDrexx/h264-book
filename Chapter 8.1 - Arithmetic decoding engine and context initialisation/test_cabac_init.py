#!/usr/bin/env python3
"""Harness for Chapter 8.1 (CABAC arithmetic decoding engine and context initialisation).

Three independent checks, each with its own input file and reader output (grammars in the Readme, section 5):

  1. contexts   python3 test_cabac_init.py --ctx ctx_cases.txt your_ctx_dump.txt
       every case "slice_type cabac_init_idc SliceQPY": your (pStateIdx, valMPS) for every ctxIdx.
  2. engine     python3 test_cabac_init.py --engine engine_vectors.txt your_engine_out.txt
       arithmetic-code vectors with known bins: DecodeDecision / DecodeBypass / DecodeTerminate / I_PCM restart.
  3. stream     python3 test_cabac_init.py input.ts|input.264 your_stream_out.txt      (also: --stream input out)
       for every CABAC slice of a real stream: the engine state right after initialisation at the slice start and the
       first macroblock's mb_skip_flag / mb_type bins.

The three can be given together.  Test inputs: `--make-variants DIR` writes ctx_cases.txt, engine_vectors.txt, synthetic
streams (syn_*.264 + .json manifest) and x264 CABAC streams with different QP / slice layouts (x264_*.ts).
`--print-expected ctx|engine|stream INPUT OUT` writes the expected text of a channel.  `--check-tables` re-verifies
spoiler_reference/tables_data.py against the installed libavcodec and libx264.  `--self-test` verifies this harness.

Truth.  JM is not used.  spoiler_reference/ holds an independent reference engine (cabac_ref.py), an independent CABAC
ENCODER that makes the vectors and the synthetic streams (cabac_enc.py), and the (m, n) / rangeTabLPS / transIdxLPS tables
(tables_data.py) that were cut out of libavcodec AND libx264 and compared entry by entry (extract_tables.py).  Before a
stream's expected lines are used they are cross-checked with ffmpeg's own parser (the class of the first macroblock of every
slice from `-debug mb_type`); synthetic streams are decoded by ffmpeg and the I_PCM samples are compared with the encoder's.
A failure of such a cross-check is a HARNESS PROBLEM (exit 2).
Exit 0 = pass, 1 = your output differs, 2 = unusable input / output or harness problem.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPOIL = os.path.join(HERE, "spoiler_reference")
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, SPOIL)
from common import cavlc_ref, ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import cabac_ref as R  # noqa: E402
import streamref  # noqa: E402
import vectors  # noqa: E402


def die(msg, code=2):
    print(f"error: {msg}")
    sys.exit(code)


def read_text(path, what):
    try:
        return open(path, "r", errors="replace").read()
    except OSError as e:
        die(f"cannot read {what} {path!r}: {e}")


# ================================================================ 1. contexts
def out_lines(text):
    """(line number, words) of a reader output file; '#' starts a comment, blank lines are skipped."""
    for n, ln in enumerate(text.splitlines(), 1):
        w = ln.split("#")[0].split()
        if w:
            yield n, w


def parse_ctx_output(text):
    """-> {case index: {ctxIdx: (pStateIdx, valMPS)}}"""
    cases, cur = {}, None
    for n, w in out_lines(text):
        ln = " ".join(w)
        if not w:
            continue
        if w[0] == "CASE":
            try:
                cur = cases.setdefault(int(w[1]), {})
            except (IndexError, ValueError):
                die(f"ctx output line {n}: bad CASE line {ln!r}")
        elif cur is None:
            die(f"ctx output line {n}: {ln!r} comes before the first 'CASE <k> ...' line")
        else:
            try:
                if len(w) != 3:
                    raise ValueError
                cur[int(w[0])] = (int(w[1]), int(w[2]))
            except ValueError:
                die(f"ctx output line {n}: expected 'ctxIdx pStateIdx valMPS' (three decimal numbers), got {ln!r}")
    if not cases:
        die("ctx output contains no 'CASE <k> ...' line")
    return cases


def check_ctx(cases_text, out_text, rep):
    exp_cases = {}
    k = 0
    descr = {}
    for ln in cases_text.splitlines():
        ln = ln.split("#")[0].strip()
        if ln:
            descr[k] = ln
            k += 1
    for line in R.run_ctx_cases(cases_text):
        w = line.split()
        if w[0] == "CASE":
            cur = exp_cases.setdefault(int(w[1]), {})
        else:
            cur[int(w[0])] = (int(w[1]), int(w[2]))
    got = parse_ctx_output(out_text)
    for k, exp in exp_cases.items():
        g = got.get(k)
        rep.check(g is not None, f"ctx case {k} ({descr[k]}): no 'CASE {k}' block in your output")
        if g is None:
            continue
        bad = [c for c in sorted(exp) if g.get(c) != exp[c]]
        rep.check(not bad, "")
        if bad:
            c = bad[0]
            first = (f"case {k} [{descr[k]}]: {len(bad)} of {len(exp)} contexts differ; first ctxIdx {c}: expected "
                     f"pStateIdx={exp[c][0]} valMPS={exp[c][1]}, got "
                     + ("nothing" if c not in g else f"pStateIdx={g[c][0]} valMPS={g[c][1]}"))
            rep.failures[-1] = first + ("" if len(bad) < 2 else f" (next: ctxIdx {bad[1]})")
    return rep


# ================================================================ 2. engine vectors
def sanity_vectors(text):
    """Round trip: the reference decoder must reproduce the bins the independent encoder coded (harness self-check)."""
    vectors.BINS.clear()
    fresh = vectors.engine_vectors_text()
    if fresh != text:
        return None                      # a hand-edited file: no bins known
    bad = []
    for v in R.parse_vectors(text):
        out = R.run_vector(v)
        got = [int(l.split()[2]) if l[0] == "D" else int(l.split()[1]) for l in out if l[0] in "DBT"]
        if got != vectors.BINS[v["name"]]:
            bad.append(v["name"])
    return bad


ENGINE_LINE = re.compile(r"(D -?\d+ -?\d+|B -?\d+|T -?\d+ -?\d+|P -?\d+|STATE -?\d+ -?\d+ -?\d+|END)$")


def parse_engine_output(text):
    outs, cur = {}, None
    for n, w in out_lines(text):
        ln = " ".join(w)
        if w[0] == "VECTOR":
            if len(w) != 2:
                die(f"engine output line {n}: expected 'VECTOR <name>', got {ln!r}")
            cur = outs.setdefault(w[1], [])
        elif cur is None:
            die(f"engine output line {n}: {ln!r} comes before the first 'VECTOR <name>' line")
        elif not ENGINE_LINE.match(ln):
            die(f"engine output line {n}: {ln!r} is not one of 'D <ctxIdx> <bin>', 'B <bin>', 'T <bin> <bits>', "
                "'P <byte offset>', 'STATE <ctxIdx> <pStateIdx> <valMPS>', 'END' (Readme 5.2)")
        else:
            cur.append(ln)
    if not outs:
        die("engine output contains no 'VECTOR <name>' line")
    return outs


def check_engine(vec_text, out_text, rep):
    vecs = R.parse_vectors(vec_text)
    got = parse_engine_output(out_text)
    for v in vecs:
        exp = [" ".join(l.split()) for l in R.run_vector(v)[1:] if l != "END"]
        g = [x for x in got.get(v["name"], []) if x != "END"]
        if v["name"] not in got:
            rep.check(False, f"vector {v['name']}: missing in your output")
            continue
        ok = exp == g
        rep.check(ok, "")
        if ok:
            continue
        i = next((i for i, (a, b) in enumerate(zip(exp, g)) if a != b), min(len(exp), len(g)))
        opno = sum(1 for x in exp[:i] if x[0] in "DBTP")
        what = f"line {i + 1}: expected {exp[i]!r}" if i < len(exp) else "line %d: expected nothing" % (i + 1)
        what += f", got {g[i]!r}" if i < len(g) else ", got nothing (output too short)"
        hint = ""
        if i < len(exp) and exp[i].startswith("STATE"):
            hint = " (all bins were right: look at the context state update)"
        elif i < len(exp) and exp[i].split()[0] == "T":
            hint = " (T line = 'T <bin> <bits read so far>')"
        rep.failures[-1] = f"vector {v['name']}, first difference at op #{opno}, {what}{hint}"
    return rep


# ================================================================ 3. streams
def stream_infos(path, td):
    """-> list of SliceInfo (all slice NAL units).  Refuses MBAFF streams."""
    try:
        infos = cavlc_ref.load_stream(path, td)
    except SystemExit as e:
        die(str(e.code) if e.code else "cannot read the stream")
    if not infos:
        die("no slice NAL units found in the input")
    for s in infos:
        if s.sps.get("mb_adaptive_frame_field_flag", 0) and not s.sps.get("frame_mbs_only_flag", 1):
            die("input uses MBAFF (first macroblock of a pair needs mb_field_decoding_flag handling, out of scope for 8.1); "
                "use a progressive or PAFF stream")
    return infos


def expected_stream_lines(infos):
    lines = []
    for s in infos:
        if not s.entropy:
            continue
        if s.slice_type == 4:
            die("CABAC SI slice: not possible in a conforming stream")
        idc = s.hdr.get("cabac_init_idc", 0)
        lines.append(streamref.slice_line(s.index, s.slice_type, idc, s.slice_qp, s.nal, s.header_end_bit,
                                          pcm_size(s)))
    return lines


def pcm_size(s):
    """Bytes of pcm_sample_luma + pcm_sample_chroma of one I_PCM macroblock (7.3.5)."""
    cf = s.sps.get("chroma_format_idc", 1)
    bl = 8 + s.sps.get("bit_depth_luma_minus8", 0)
    bc = 8 + s.sps.get("bit_depth_chroma_minus8", 0)
    mbwc, mbhc = {0: (0, 0), 1: (8, 8), 2: (8, 16), 3: (16, 16)}[cf]
    return (256 * bl + 2 * mbwc * mbhc * bc) // 8


def _poc_order(infos):
    """decode-order picture index -> output rank, for POC types 0 and 2 (None if unknown)."""
    pics = {}
    for s in infos:
        pics.setdefault(s.picture, s)
    prev_msb = prev_lsb = 0
    key, period = {}, -1
    for p in sorted(pics):
        s = pics[p]
        sps, h = s.sps, s.hdr
        typ = sps.get("pic_order_cnt_type", 0)
        if s.nal_type == 5:
            period += 1
            prev_msb = prev_lsb = 0
        if typ == 0:
            maxl = 1 << (sps["log2_max_pic_order_cnt_lsb_minus4"] + 4)
            lsb = h["pic_order_cnt_lsb"]
            if lsb < prev_lsb and prev_lsb - lsb >= maxl // 2:
                msb = prev_msb + maxl
            elif lsb > prev_lsb and lsb - prev_lsb > maxl // 2:
                msb = prev_msb - maxl
            else:
                msb = prev_msb
            poc = msb + lsb
            if s.nal[0] >> 5:
                prev_msb, prev_lsb = msb, lsb
        elif typ == 2:
            poc = p
        else:
            return None
        key[p] = (max(period, 0), poc)
    order = sorted(key, key=lambda p: key[p])
    return {p: r for r, p in enumerate(order)}


def crosscheck_ffmpeg(path, infos, lines, td):
    """Compare the class of the first macroblock of every slice with ffmpeg's macroblock map.  -> (problems, note)."""
    s0 = infos[0]
    if any(s.hdr.get("field_pic_flag", 0) for s in infos):
        return [], "field pictures: ffmpeg cross-check skipped"
    rank = _poc_order(infos)
    if rank is None:
        return [], "POC type 1: ffmpeg cross-check skipped"
    W = s0.pic_width_in_mbs
    H = s0.pic_size_in_mbs // W
    npics = infos[-1].picture + 1
    grid = cavlc_ref.ffmpeg_mb_grid(path, td, dims=(W, H), count=npics)
    if grid is None or len(grid) != npics:
        return [], "ffmpeg macroblock map unavailable: cross-check skipped"
    by_idx = {s.index: s for s in infos}
    problems = []
    for ln in lines:
        w = ln.split()
        s = by_idx[int(w[1])]
        f = dict(x.split("=") for x in w[3:])
        r = rank[s.picture]
        x, y = s.first_mb % W, s.first_mb // W
        cell = grid[r]["rows"][y][x]
        t = w[2]
        if t == "I" or f.get("b0") == "1" and "mbt" in f:
            mbt = int(f["mbt"])
            ok = cell == ("i" if mbt == 0 else "P" if mbt == 25 else "I")
        elif f.get("skip") == "1":
            ok = cell in "Sd"
        elif t == "P":
            ok = cell not in "iIPS"
        else:
            ok = cell == "d" if f.get("b0") == "0" else cell != "S"
        if not ok:
            problems.append(f"slice {s.index} (first macroblock {s.first_mb}): reference says {' '.join(w[3:])} but ffmpeg's "
                            f"macroblock map shows {cell!r}")
    return problems, ""


def stream_truth(path, td, check=True):
    infos = stream_infos(path, td)
    lines = expected_stream_lines(infos)
    if not lines:
        die("the stream has no CABAC slice (entropy_coding_mode_flag = 0 everywhere); use a CABAC stream, "
            "e.g. the files written by --make-variants")
    note = ""
    if check:
        manifest = os.path.splitext(path)[0] + ".json"
        if os.path.exists(manifest):
            m = json.load(open(manifest))
            exp = m["expected"]
            got = [streamref.strip_off(l) for l in lines]
            if got != exp:
                die("HARNESS PROBLEM: the reference disagrees with the manifest written by the stream generator "
                    f"(first difference: {next((a, b) for a, b in zip(got, exp) if a != b)})")
        else:
            problems, note = crosscheck_ffmpeg(path, infos, lines, td)
            if problems:
                die("HARNESS PROBLEM: the reference disagrees with ffmpeg:\n  " + "\n  ".join(problems[:5]))
    return infos, lines, note


STREAM_FIELD = re.compile(r"(idc=(-|\d+)|(qp|off|mbt|skip|b0|eos)=-?\d+)$")


def check_stream(lines, out_text, rep):
    got = {}
    for n, w in out_lines(out_text):
        ln = " ".join(w)
        if w[0] != "SLICE":
            die(f"stream output line {n}: every line must start with 'SLICE', got {ln!r}")
        if (len(w) < 6 or not w[1].isdigit() or w[2] not in ("I", "P", "B")
                or any(not STREAM_FIELD.match(x) for x in w[3:])):
            die(f"stream output line {n}: expected 'SLICE <index> <I|P|B> idc=.. qp=.. off=.. <key>=<number> ...' "
                f"(Readme 5.3), got {ln!r}")
        got[int(w[1])] = w
    if not got:
        die("stream output contains no 'SLICE <index> ...' line")
    for e in lines:
        ew = e.split()
        i = int(ew[1])
        gw = got.get(i)
        ok = gw is not None and gw == ew
        rep.check(ok, "")
        if ok:
            continue
        if gw is None:
            msg = f"slice {i}: no line in your output (expected {e!r})"
        else:
            ed = dict([("slice_type", ew[2])] + [x.split("=", 1) for x in ew[3:] if "=" in x])
            gd = dict([("slice_type", gw[2] if len(gw) > 2 else None)] + [x.split("=", 1) for x in gw[3:] if "=" in x])
            diffs = [f"{k}: expected {ed.get(k)!r} got {gd.get(k)!r}" for k in list(ed) + [k for k in gd if k not in ed]
                     if ed.get(k) != gd.get(k)]
            hint = ""
            if "off" in [d.split(":")[0] for d in diffs]:
                hint = " (off = codIOffset right after initialisation: check where slice_data starts and the 9-bit read)"
            elif any(d.startswith(("skip", "mbt", "b0", "eos")) for d in diffs) and not any(d.startswith(("qp", "idc")) for d in diffs):
                hint = " (engine started correctly, so look at the contexts / bins of the first macroblock)"
            msg = f"slice {i}: " + "; ".join(diffs) + hint
        rep.failures[-1] = msg
    return rep


# ================================================================ test inputs
X264_VARIANTS = {   # name -> (ffmpeg input filter chain, codec options, rate control)
    "x264_qp10_intra": ("-profile:v main -x264-params keyint=1", ["-qp", "10"]),
    "x264_qp38_bf3": ("-profile:v main -x264-params bframes=3:b-pyramid=normal:keyint=40", ["-qp", "38"]),
    "x264_crf_aq": ("-profile:v high -x264-params aq-mode=2:keyint=30", ["-crf", "23"]),
    "x264_slices4": ("-profile:v main -x264-params slices=4:keyint=25", ["-crf", "28"]),
    "x264_qp1": ("-profile:v high -x264-params keyint=12", ["-qp", "1"]),
    "x264_qp51": ("-profile:v main -x264-params keyint=12", ["-qp", "51"]),
    "x264_slices8_bf": ("-profile:v high -x264-params slices=8:bframes=2:keyint=20", ["-qp", "22"]),
}


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "ctx_cases.txt"), "w").write(vectors.ctx_cases_text())
    open(os.path.join(out_dir, "engine_vectors.txt"), "w").write(vectors.engine_vectors_text())
    names = ["ctx_cases.txt", "engine_vectors.txt"]
    for seed in (1, 2):
        data, info = streamref.synth_stream(seed)
        base = f"syn_pcm_skip_{seed}"
        open(os.path.join(out_dir, base + ".264"), "wb").write(data)
        json.dump({"note": "one macroblock per slice; expected lines (without off=) from the construction",
                   "expected": streamref.synth_expected_lines(info)}, open(os.path.join(out_dir, base + ".json"), "w"))
        names += [base + ".264", base + ".json"]
    for name, (opts, rc) in X264_VARIANTS.items():
        out = os.path.join(out_dir, name + ".ts")
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", testfiles.SRC, "-t", "2", "-pix_fmt", "yuv420p",
               "-c:v", "libx264", *rc, *opts.split(), "-f", "mpegts", out]
        r = ffref.run(cmd, timeout=180)
        if r.returncode:
            die(f"ffmpeg failed for {name}: {r.stderr.decode()[-300:]}")
        names.append(name + ".ts")
    print("wrote to", out_dir + ":", ", ".join(names))


# ================================================================ self-test
def self_test():
    ok_all = True
    td = tempfile.mkdtemp(prefix="cabac81_")
    try:
        ffref.require_tools()

        def step(name, ok, detail=""):
            nonlocal ok_all
            ok_all &= ok
            print(f"  [{'ok' if ok else 'FAILED'}] {name}" + (f": {detail}" if detail else ""))

        def passes(fn, *a):
            rep = Report("x")
            fn(*a, rep)
            return not rep.failures, rep

        # 0. tables vs the installed libraries
        r = subprocess.run([sys.executable, os.path.join(SPOIL, "extract_tables.py"), "--check"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=120)
        if r.returncode == 2:
            print("  [skip] tables vs libavcodec/libx264: libraries not found or layout not recognised")
        else:
            step("tables_data.py == libavcodec == libx264 (4 x 1024 (m,n), rangeTabLPS, transIdxLPS)", r.returncode == 0,
                 r.stdout.decode().strip())
        # 1. engine round trip: independent encoder -> reference decoder
        ctx_txt = vectors.ctx_cases_text()
        eng_txt = vectors.engine_vectors_text()
        bad = sanity_vectors(eng_txt)
        step(f"encoder/decoder round trip on all {eng_txt.count('VECTOR ')} engine vectors", bad == [], str(bad))
        # 2. ctx and engine: good output passes, corrupted output fails
        ctx_out = "\n".join(R.run_ctx_cases(ctx_txt)) + "\n"
        eng_out = "\n".join(l for v in R.parse_vectors(eng_txt) for l in R.run_vector(v)) + "\n"
        ok, _ = passes(check_ctx, ctx_txt, ctx_out)
        step("known-good context dump passes", ok)
        lines = ctx_out.splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith("1000 "))
        bad_ctx = "\n".join(lines[:i] + ["1000 5 1"] + lines[i + 1:]) + "\n"
        ok, _ = passes(check_ctx, ctx_txt, bad_ctx)
        step("context dump with one wrong value is rejected", not ok)
        ok, _ = passes(check_ctx, ctx_txt, "\n".join(lines[:i - 50] + lines[i - 40:]) + "\n")
        step("context dump with missing lines is rejected", not ok)
        ok, _ = passes(check_engine, eng_txt, eng_out)
        step("known-good engine output passes", ok)
        el = eng_out.splitlines()
        j = next(j for j, l in enumerate(el) if l.startswith("T 1"))
        ok, _ = passes(check_engine, eng_txt, "\n".join(el[:j] + ["T 1 3"] + el[j + 1:]) + "\n")
        step("engine output with a wrong bit count is rejected", not ok)
        j = next(j for j, l in enumerate(el) if l.startswith("STATE"))
        ok, _ = passes(check_engine, eng_txt, "\n".join(el[:j] + [el[j] + "0"] + el[j + 1:]) + "\n")
        step("engine output with a wrong final state is rejected", not ok)
        # 3. streams: synthetic (ffmpeg decodes them, I_PCM samples must match) and x264
        syn, info = streamref.synth_stream(1)
        sp = os.path.join(td, "syn.264")
        open(sp, "wb").write(syn)
        json.dump({"expected": streamref.synth_expected_lines(info)}, open(os.path.join(td, "syn.json"), "w"))
        yuv = os.path.join(td, "syn.yuv")
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-i", sp, "-f", "rawvideo", "-pix_fmt", "yuv420p", yuv], timeout=120)
        W, H = streamref.MBW * 16, streamref.MBH * 16
        fs = W * H * 3 // 2
        y = open(yuv, "rb").read()
        bad = 0
        pcm_n = 0
        for disp, pic in enumerate([0, 1, 3, 2]):          # display order -> decode order (poc 0, 8, 12, 16)
            fr = y[disp * fs:(disp + 1) * fs]
            for mb, m in enumerate(info[pic]):
                if m["pcm"] is None:
                    continue
                x0, y0 = mb % streamref.MBW, mb // streamref.MBW
                luma = bytes(fr[(y0 * 16 + rr) * W + x0 * 16 + c] for rr in range(16) for c in range(16))
                pcm_n += 1
                bad += luma != m["pcm"][:256]
        step(f"ffmpeg decodes the synthetic stream without error and reproduces all {pcm_n} I_PCM macroblocks",
             r.returncode == 0 and not r.stderr and len(y) == 4 * fs and bad == 0, r.stderr.decode()[:200])
        infos, exp, note = stream_truth(sp, td)
        step(f"reference lines of the synthetic stream equal the construction ({len(exp)} slices)", True)
        ok, _ = passes(check_stream, exp, "\n".join(exp) + "\n")
        step("known-good stream output passes", ok)
        ok, _ = passes(check_stream, exp, "\n".join(exp[:100] + [exp[100].replace("off=", "off=1")] + exp[101:]) + "\n")
        step("stream output with a wrong off= is rejected", not ok)
        ok, _ = passes(check_stream, exp, "\n".join(exp[:-3]) + "\n")
        step("stream output with missing slices is rejected", not ok)
        t10 = testfiles.get("T10_full")
        t_infos, t_exp, note = stream_truth(t10, td)
        cc, note2 = crosscheck_ffmpeg(t10, t_infos, t_exp, td)
        step(f"x264 T10_full: first macroblock of {len(t_exp)} slices agrees with ffmpeg's parser", not cc and not note2,
             (cc[0] if cc else note2))
        ok, _ = passes(check_stream, t_exp, "\n".join(t_exp) + "\n")
        step("known-good stream output of T10_full passes", ok)
        kinds = {l.split()[2] for l in t_exp}
        # 4. mutants: each deliberately wrong rule must be caught by at least one of the three checks
        print(f"  mutation tests ({len(R.MUTANTS)} wrong rules; each must be rejected by the check shown):")
        truth_syn = exp
        truth_x = t_exp
        for name, why in R.MUTANTS.items():
            R.MUTANT = name
            try:
                c_out = "\n".join(R.run_ctx_cases(ctx_txt)) + "\n"
                e_out = "\n".join(l for v in R.parse_vectors(eng_txt) for l in R.run_vector(v)) + "\n"
                s_syn = "\n".join(streamref.slice_line(s.index, s.slice_type, s.hdr.get("cabac_init_idc", 0), s.slice_qp,
                                                       s.nal, s.header_end_bit, pcm_size(s)) for s in infos if s.entropy) + "\n"
                s_x = "\n".join(streamref.slice_line(s.index, s.slice_type, s.hdr.get("cabac_init_idc", 0), s.slice_qp,
                                                     s.nal, s.header_end_bit, pcm_size(s)) for s in t_infos if s.entropy) + "\n"
            except Exception as e:           # a mutant may crash the reader: that is also a detection
                c_out = e_out = s_syn = s_x = None
                crashed = type(e).__name__
            finally:
                R.MUTANT = None
            if c_out is None:
                step(f"mutant {name}: {why}", True, "reader crashed (" + crashed + ")")
                continue
            caught = []
            if not passes(check_ctx, ctx_txt, c_out)[0]:
                caught.append("ctx")
            if not passes(check_engine, eng_txt, e_out)[0]:
                caught.append("engine")
            if not passes(check_stream, truth_syn, s_syn)[0]:
                caught.append("stream(syn)")
            if not passes(check_stream, truth_x, s_x)[0]:
                caught.append("stream(x264)")
            step(f"mutant {name}: {why}", bool(caught), "caught by " + ", ".join(caught) if caught else "NOT CAUGHT")
    finally:
        R.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


# ================================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="CABAC H.264 stream (.ts or Annex B .264) for the stream check")
    ap.add_argument("output", nargs="?", help="your stream output (SLICE lines)")
    ap.add_argument("--ctx", nargs=2, metavar=("CASES", "OUT"))
    ap.add_argument("--engine", nargs=2, metavar=("VECTORS", "OUT"))
    ap.add_argument("--stream", nargs=2, metavar=("INPUT", "OUT"))
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--print-expected", nargs=3, metavar=("WHAT", "INPUT", "OUT"))
    ap.add_argument("--check-tables", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.check_tables:
        r = subprocess.run([sys.executable, os.path.join(SPOIL, "extract_tables.py"), "--check"], stdin=subprocess.DEVNULL,
                           timeout=120)
        sys.exit(r.returncode)
    if a.make_variants:
        ffref.require_tools()
        make_variants(a.make_variants)
        return
    td = tempfile.mkdtemp(prefix="cabac81_")
    try:
        if a.print_expected:
            what, inp, out = a.print_expected
            if what == "ctx":
                text = "\n".join(R.run_ctx_cases(read_text(inp, "cases file"))) + "\n"
            elif what == "engine":
                text = "\n".join(l for v in R.parse_vectors(read_text(inp, "vectors file")) for l in R.run_vector(v)) + "\n"
            elif what == "stream":
                text = "\n".join(stream_truth(inp, td)[1]) + "\n"
            else:
                die("--print-expected takes ctx, engine or stream")
            open(out, "w").write(text)
            print("wrote", out)
            return
        stream = a.stream or ((a.input, a.output) if a.input and a.output else None)
        if not (a.ctx or a.engine or stream):
            ap.print_help()
            die("nothing to check: give --ctx, --engine, or <input> <output> (see --help)")
        rep = Report("Chapter 8.1 CABAC engine and context initialisation")
        if a.ctx:
            check_ctx(read_text(a.ctx[0], "cases file"), read_text(a.ctx[1], "ctx output"), rep)
        if a.engine:
            check_engine(read_text(a.engine[0], "vectors file"), read_text(a.engine[1], "engine output"), rep)
        if stream:
            ffref.require_tools()
            _, lines, note = stream_truth(stream[0], td)
            if note:
                print("note:", note)
            check_stream(lines, read_text(stream[1], "stream output"), rep)
        rep.failures = [f for f in rep.failures if f]
        rep.finish()
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
