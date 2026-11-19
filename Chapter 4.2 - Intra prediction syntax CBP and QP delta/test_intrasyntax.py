#!/usr/bin/env python3
"""Harness for Chapter 4.2 (intra prediction syntax, coded_block_pattern, mb_qp_delta).

    python3 test_intrasyntax.py <input.ts|input.264> <mb_dump.txt>
    python3 test_intrasyntax.py --make-variants <out_dir>        # extra test inputs (see Readme)
    python3 test_intrasyntax.py --print-expected <input> <out.txt>   # write the expected dump, to diff against yours
    python3 test_intrasyntax.py --self-test

<mb_dump.txt> is your program's output.  The line grammar is Chapter 4.1's (SLICE / MB / PCM / END / HIST)
with six more keys on every MB line, after the 4.1 keys:

    MB <i> <addr> mb_type=.. name=.. i16_mode=.. chroma_cbp=.. luma_cbp=.. bit=..   (4.1)
       t8x8=<0|1|->  modes=<list|i16:<0-3>|->  chroma_pred=<0-3|->  cbp=<0-47|->  qp_delta=<n|->  qp=<0-51>

Truth comes from common/cavlc_ref.py (level 2), a reference parser written for this book (there is no JM
here).  Before your output is compared with it, the reference itself is cross-checked:
  * ffmpeg's macroblock map (`-debug mb_type`): the class of every macroblock it printed;
  * ffmpeg's per-macroblock QP map (`-debug qp`): QP_Y of every macroblock it printed (I_PCM: ffmpeg shows 0,
    the deblocking value; the reference's inherited QP_Y is checked against the generator instead);
  * for the synthetic streams (--make-variants), the manifest written by an independent bit writer
    (modes, CBP, QP chain, stop position), ffmpeg's QP map for EVERY macroblock of the stream (also those
    past the reader's stop), clean ffmpeg decoding, and the pixels of every I_PCM macroblock.
A failure of one of those is a harness problem, not yours: exit code 2.

Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output, unusable input or reference failure.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import cavlc_ref as ref, ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
from common.mbdump import (KEYS_SLICE, KEYS_PCM, KEYS_END, die, parse_reader, hist_diff,  # noqa: E402
                           check_reference_vs_ffmpeg, match_pictures, manifest_path, decode_synth,
                           pcm_pixel_problems)
import intragen  # noqa: E402

LEVEL = 2                                   # cavlc_ref parsing level of this chapter
KEYS_MB = ref.MbRecord.CORE_KEYS + ref.EXTRA_KEYS_BY_LEVEL[2]   # the 4.1 keys + t8x8 modes chroma_pred cbp qp_delta qp


def expected_structs(results):
    text = ref.render_dump(results, LEVEL)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    try:
        tmp.write(text)
        tmp.close()
        return parse_reader(tmp.name)
    finally:
        os.unlink(tmp.name)


# ================================================================ comparison (reader vs reference)
def compare(rep, exp_slices, exp_hist, act_slices, act_hist):
    rep.check(len(act_slices) == len(exp_slices),
              f"number of SLICE blocks: expected {len(exp_slices)} (one per slice NAL unit, types 1 and 5, in stream "
              f"order, even for slices you skip), got {len(act_slices)}")
    for e, a in zip(exp_slices, act_slices):
        k = e["index"]
        rep.check(a["index"] == k, f"slice block #{k}: expected index {k}, got {a['index']}")
        for key in KEYS_SLICE:
            rep.check(a["slice"].get(key) == e["slice"][key],
                      f"SLICE {k} {key}: expected {e['slice'][key]}, got {a['slice'].get(key)}")
        rep.check(len(a["mbs"]) == len(e["mbs"]),
                  f"SLICE {k}: expected {len(e['mbs'])} MB lines (macroblocks read, including the one you stop at), "
                  f"got {len(a['mbs'])}; expected END status {e['end']['status']}, you reported {a['end']['status']}")
        for j, ((ea, ekv), (aa, akv)) in enumerate(zip(e["mbs"], a["mbs"])):
            rep.check(ea == aa, f"SLICE {k} MB #{j}: expected macroblock address {ea}, got {aa}")
            for key in KEYS_MB:
                rep.check(akv.get(key) == ekv[key],
                          f"SLICE {k} MB addr {ea} {key}: expected {ekv[key]}, got {akv.get(key)} "
                          f"(expected line: mb_type={ekv['mb_type']} t8x8={ekv['t8x8']} modes={ekv['modes']} "
                          f"chroma_pred={ekv['chroma_pred']} cbp={ekv['cbp']} qp_delta={ekv['qp_delta']} qp={ekv['qp']})")
        rep.check(sorted(a["pcm"]) == sorted(e["pcm"]),
                  f"SLICE {k}: PCM lines for macroblocks {sorted(a['pcm'])}, expected {sorted(e['pcm'])}")
        for addr in sorted(set(e["pcm"]) & set(a["pcm"])):
            for key in KEYS_PCM:
                rep.check(a["pcm"][addr].get(key) == e["pcm"][addr][key],
                          f"SLICE {k} PCM addr {addr} {key}: expected {e['pcm'][addr][key]}, got {a['pcm'][addr].get(key)}")
        for key in KEYS_END:
            rep.check(a["end"].get(key) == e["end"][key],
                      f"SLICE {k} END {key}: expected {e['end'][key]}, got {a['end'].get(key)}")
    own = {}
    for a in act_slices:
        for _, kv in a["mbs"]:
            if re.fullmatch(r"\d+", kv.get("mb_type", "")):
                own[int(kv["mb_type"])] = own.get(int(kv["mb_type"]), 0) + 1
    rep.check(act_hist == own, f"HIST does not match your own MB lines: {hist_diff(own, act_hist)}")
    rep.check(act_hist == exp_hist, f"HIST differs from the expected one (mb_type: expected vs yours): "
                                    f"{hist_diff(exp_hist, act_hist)}")


# ================================================================ checking the reference itself
def check_reference_vs_ffmpeg_qp(path, infos, results, td):
    """QP_Y of every macroblock the reference printed vs ffmpeg's `-debug qp` map. -> (problems, summary)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    h0 = infos[0].pic_size_in_mbs // w0
    if not all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos):
        return [], "ffmpeg QP cross-check skipped: pictures of different sizes"
    grid = ref.ffmpeg_qp_grid(path, td, dims=(w0, h0), count=npics)
    if grid is None or len(grid) < npics:
        return [], "ffmpeg QP cross-check skipped: ffmpeg's log was unusable"
    usable, note = match_pictures(infos, grid, "ffmpeg QP cross-check")
    if usable is None:
        return [], note
    problems, n = [], 0
    for r in results:
        if r.info.picture not in usable:
            continue
        rows = grid[r.info.picture]["rows"]
        for rec in r.mbs:
            y, x = divmod(rec.addr, r.info.pic_width_in_mbs)
            if y >= len(rows):
                continue
            theirs = rows[y][x]
            mine = 0 if rec.pcm is not None else rec.f["qp"]       # ffmpeg stores 0 for I_PCM (deblocking value)
            n += 1
            if theirs != mine:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference QP_Y {mine}, ffmpeg {theirs}")
    return problems, f"ffmpeg QP cross-check: QP_Y of {n} macroblocks in {len(usable)} picture(s) agree{note}"


def expected_prefix(mbs):
    """The 4.2 stop rule applied to the generator's list of macroblocks -> (parsed entries, status).
    A macroblock needing coeff_tokens (Intra16x16, or I_NxN with CBP > 0) stops the slice if it has a
    block with coefficients or follows an I_PCM macroblock of the slice."""
    out, seen_pcm = [], False
    for m in mbs:
        out.append(m)
        if m["kind"] == "pcm":
            seen_pcm = True
            continue
        if (m["kind"] == "i16" or m["cbp"] > 0) and (seen_pcm or m["nz"]):
            return out, "stopped"
    return out, "complete"


def ffmpeg_synth_checks(path, manifest, td):
    """ffmpeg decodes the synthetic stream cleanly; its QP map equals the manifest's QP_Y of EVERY macroblock
    (I_PCM: 0); every I_PCM macroblock's pixels equal its raw samples.  -> problems"""
    problems, frames = decode_synth(path, manifest, td)
    if problems:
        return problems
    wm, hm = manifest["width_mbs"], manifest["height_mbs"]
    pics = manifest["pictures"]
    grid = ref.ffmpeg_qp_grid(path, td, dims=(wm, hm), count=len(pics))
    if grid is None or len(grid) < len(pics):
        problems.append("ffmpeg's QP map was unusable for the synthetic stream")
    else:
        for pi, pic in enumerate(pics):
            for s in pic:
                for e in s["mbs"]:
                    my, mx = divmod(e["addr"], wm)
                    want = 0 if e["kind"] == "pcm" else e["qp"]
                    got = grid[pi]["rows"][my][mx]
                    if got != want:
                        problems.append(f"picture {pi} addr {e['addr']}: ffmpeg QP {got}, generator {want}")
    return problems + pcm_pixel_problems(manifest, frames)


def check_reference_vs_manifest(path, results, td, manifest):
    problems = []
    sl_all = [s for pic in manifest["pictures"] for s in pic]
    if len(sl_all) != len(results):
        return [f"manifest has {len(sl_all)} slices, the stream {len(results)}"], ""
    nmb = 0
    for m, r in zip(sl_all, results):
        k = r.info.index
        if r.info.header_end_bit != m["header_end_bit"]:
            problems.append(f"slice {k}: header_end_bit {r.info.header_end_bit} (ffmpeg trace) vs {m['header_end_bit']} (generator)")
        if r.info.slice_qp != m["slice_qp"] or r.info.transform_8x8_mode != m["t8_mode"]:
            problems.append(f"slice {k}: SliceQPY / transform_8x8_mode {r.info.slice_qp}/{r.info.transform_8x8_mode} "
                            f"(ffmpeg trace) vs {m['slice_qp']}/{m['t8_mode']} (generator)")
        exp, status = expected_prefix(m["mbs"])
        if len(r.mbs) != len(exp) or r.status != status:
            problems.append(f"slice {k}: reference parsed {len(r.mbs)} macroblocks / {r.status}, generator says "
                            f"{len(exp)} / {status}")
            continue
        for rec, e in zip(r.mbs, exp):
            nmb += 1
            f = rec.f
            got = (rec.addr, f["mb_type"], f["name"], f["bit"], f["t8x8"], f["modes"], f["chroma_pred"], f["cbp"],
                   f["qp_delta"], f["qp"])
            if e["kind"] == "pcm":
                want = (e["addr"], 25, "I_PCM", e["bit"], "-", "-", "-", "-", "-", e["qp"])
            else:
                want = (e["addr"], e["mb_type"], e["name"], e["bit"], e["t8"], e["modes"], e["cpm"], e["cbp"],
                        "-" if e["qpd"] is None else e["qpd"], e["qp"])
            if got != want:
                problems.append(f"slice {k} addr {e['addr']}: reference {got}, generator {want}")
            if e["kind"] == "pcm":
                s = bytes.fromhex(e["samples"])
                if rec.pcm is None or rec.pcm["samples"] != s:
                    problems.append(f"slice {k} addr {e['addr']}: PCM samples differ from generator")
    problems += ffmpeg_synth_checks(path, manifest, td)
    return problems, f"synthetic-stream checks: {nmb} macroblocks match the generator manifest (modes, CBP, QP chain, stop " \
                     f"position); ffmpeg decodes the stream without errors, its QP map equals the generator's QP_Y of " \
                     f"every macroblock, and every I_PCM macroblock's pixels equal its raw samples"


# ================================================================ driver
def load_reference(path, td):
    infos = ref.load_stream(path, td)
    if not infos:
        die("input problem: no slice NAL units found in the input")
    return infos, [ref.parse_slice(i, LEVEL) for i in infos]


def reference_trust(path, infos, results, td, quiet=False):
    problems, msg = check_reference_vs_ffmpeg(path, infos, results, td)
    if not quiet:
        print(" ", msg)
    p2, msg2 = check_reference_vs_ffmpeg_qp(path, infos, results, td)
    problems += p2
    if not quiet:
        print(" ", msg2)
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        manifest = json.load(open(mp))
        p3, msg3 = check_reference_vs_manifest(path, results, td, manifest)
        problems += p3
        if not quiet:
            print(" ", msg3 if not p3 else "synthetic-stream checks FAILED")
    return problems


def run_check(path, dump, title="Intra prediction syntax, CBP and QP delta"):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input file not found: {path}")
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        problems = reference_trust(path, infos, results, td)
        if problems:
            print("HARNESS PROBLEM (not your fault): the reference parser disagrees with an independent check:")
            for p in problems[:10]:
                print("   ", p)
            sys.exit(2)
        exp_slices, exp_hist = expected_structs(results)
    act_slices, act_hist = parse_reader(dump)
    rep = Report(title)
    compare(rep, exp_slices, exp_hist, act_slices, act_hist)
    mbs = [kv for s in exp_slices for _, kv in s["mbs"]]
    cnt = lambda st: sum(1 for s in exp_slices if s["end"]["status"] == st)  # noqa: E731
    print(f"  reference: {len(infos)} slices, {len(mbs)} macroblocks read ({sum(1 for m in mbs if m['t8x8'] == '1')} with "
          f"transform_size_8x8_flag, {sum(1 for m in mbs if m['qp_delta'] not in ('-', '0'))} with a non-zero mb_qp_delta), "
          f"{cnt('complete')} slices parsed completely, {cnt('stopped')} stopped, {cnt('skipped')} skipped")
    rep.finish()


# ================================================================ test inputs / variants
def x264_clip(out, vf, extra, frames=4, size="352x288"):
    src = f"color=c=gray:size={size}:rate=25,format=yuv420p,{vf}"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
           "-c:v", "libx264", *extra, "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die("ffmpeg failed: " + r.stderr.decode()[-500:])


RAMP = "geq=lum='128+(1+X/20)*sin((X+Y)/3)':cb=128:cr=128"


def make_variants(outdir):
    ffref.require_tools()
    os.makedirs(outdir, exist_ok=True)
    data, man = intragen.make_stream()
    with open(os.path.join(outdir, "intra_synth.264"), "wb") as f:
        f.write(data)
    with open(os.path.join(outdir, "intra_synth.manifest.json"), "w") as f:
        json.dump(man, f)
    nsl = sum(len(p) for p in man["pictures"])
    print(f"intra_synth.264: synthetic Annex B stream (96x80, CAVLC): {len(man['pictures'])} IDR pictures, {nsl} I slices; "
          f"Baseline pictures and High pictures with transform_8x8_mode_flag 0 and 1 (I_NxN with 4 prediction modes), real "
          f"prev/rem pred modes, all 48 coded_block_pattern codes, mb_qp_delta over -26..25 with QP wrap, pictures that parse "
          f"completely and pictures with I_PCM / coefficient blocks where the reader stops.")
    x264_clip(os.path.join(outdir, "mb_qp.ts"), RAMP,
              ["-profile:v", "baseline", "-crf", "48", "-x264-params", "keyint=1:aq-mode=2:aq-strength=2:no-deblock=1:slices=4"])
    print("mb_qp.ts: x264 Baseline, a smooth ramp at crf 48 with adaptive quantisation, 4 slices per picture: Intra16x16 "
          "macroblocks with zero residual whose QP jumps between 34 and 50 inside a slice (mb_qp_delta and the QP_Y chain on "
          "real encoder output, checked per macroblock against ffmpeg); most slices parse to the end")
    x264_clip(os.path.join(outdir, "mb_high_cavlc.ts"), RAMP,
              ["-profile:v", "high", "-crf", "46", "-x264-params", "keyint=1:aq-mode=2:aq-strength=2:cabac=0:8x8dct=1:slices=3"])
    print("mb_high_cavlc.ts: x264 High with CAVLC and 8x8dct: PPS transform_8x8_mode_flag = 1, real I_NxN macroblocks with "
          "transform_size_8x8_flag = 1 (4 prediction modes), CBP 0 (no mb_qp_delta) next to Intra16x16 ones")
    x264_clip(os.path.join(outdir, "mb_flat.ts"), "geq=lum=128:cb=128:cr=128",
              ["-profile:v", "baseline", "-qp", "26", "-x264-params", "keyint=1:partitions=none:no-deblock=1"])
    print("mb_flat.ts: flat mid-gray, all macroblocks Intra16x16 with zero residual, QP constant (every delta 0)")
    print("Also useful (from common.testfiles): T1, T2, T3, T4 (each slice stops after the first macroblock, whose modes, "
          "CBP and QP delta you do read), T11_high is CABAC (skipped)")


def write_expected(path, out):
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        open(out, "w").write(ref.render_dump(results, LEVEL))


# ================================================================ self test
def run_in_subprocess(inp, dump):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), inp, dump], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=600)
    for l in (r.stdout.decode() + r.stderr.decode()).splitlines()[-4:]:
        print("   |", l[:200])
    return r.returncode


def self_test():
    ffref.require_tools()
    ok_all = True
    with tempfile.TemporaryDirectory() as td:
        so, sys.stdout = sys.stdout, open(os.devnull, "w")
        try:
            make_variants(os.path.join(td, "v"))
        finally:
            sys.stdout.close()
            sys.stdout = so
        v = lambda n: os.path.join(td, "v", n)  # noqa: E731
        inputs = [testfiles.get(n) for n in ("T1", "T2", "T4", "default", "T10_full")]
        inputs += [v(n) for n in ("intra_synth.264", "mb_qp.ts", "mb_high_cavlc.ts", "mb_flat.ts")]
        good = {}
        for inp in inputs:
            name = os.path.basename(inp)
            dump = os.path.join(td, name + ".txt")
            write_expected(inp, dump)
            good[name] = dump
            print(f"-- known-good output on {name} (must PASS)")
            rc = run_in_subprocess(inp, dump)
            print("   exit", rc)
            ok_all &= rc == 0
        synth = v("intra_synth.264")
        lines = open(good["intra_synth.264"]).read().splitlines()

        def variant(label, edit, inp=synth, base=lines):
            nonlocal ok_all
            ls = edit(list(base))
            p = os.path.join(td, "bad.txt")
            open(p, "w").write("\n".join(ls) + "\n")
            print(f"-- {label} (must FAIL)")
            rc = run_in_subprocess(inp, p)
            print("   exit", rc)
            ok_all &= rc == 1

        def sub(pred, old, new):
            def edit(ls):
                i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and pred(l))
                ls[i] = ls[i].replace(old, new, 1)
                return ls
            return edit

        def mode_edit(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and "t8x8=0" in l)
            kv = dict(t.split("=", 1) for t in ls[i].split()[3:])
            m = kv["modes"].split(",")
            m[5] = "3" if m[5] != "3" else "4"
            ls[i] = ls[i].replace("modes=" + kv["modes"], "modes=" + ",".join(m))
            return ls

        def drop_qp(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("MB "))
            ls[i] = re.sub(r" qp=\d+", "", ls[i])
            return ls

        def swap_t8_modes(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and "t8x8=1" in l)
            ls[i] = re.sub(r"modes=\S+", "modes=-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1", ls[i])
            return ls

        def wrong_status(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("END ") and "status=complete" in l)
            ls[i] = ls[i].replace("complete", "stopped")
            return ls
        def no_wrap(ls):
            """A reader that forgets the modulo: QP_Y = QP_Y,PRED + mb_qp_delta at the first macroblock that wraps."""
            prev = None
            for i, l in enumerate(ls):
                if l.startswith("SLICE "):
                    prev = None
                if not l.startswith("MB "):
                    continue
                kv = dict(t.split("=", 1) for t in l.split()[3:])
                q, d = int(kv["qp"]), 0 if kv["qp_delta"] == "-" else int(kv["qp_delta"])
                pred = q - d if prev is None else prev
                if pred + d != q:
                    ls[i] = l.replace(f" qp={q}", f" qp={pred + d}")
                    return ls
                prev = q
            raise AssertionError("no QP wrap in the synthetic stream")

        def neg_delta(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and re.search(r" qp_delta=[1-9]", l))
            ls[i] = ls[i].replace(" qp_delta=", " qp_delta=-", 1)
            return ls
        variant("one rem_intra4x4_pred_mode wrong", mode_edit)
        variant("Table 9-4 entries 12 and 13 swapped (CBP 39 printed as 43)",
                sub(lambda l: "name=I_NxN" in l and " cbp=39 " in l, " cbp=39 ", " cbp=43 "))
        variant("mb_qp_delta with the wrong sign (se(v) mapping reversed)", neg_delta)
        variant("QP_Y not wrapped (no modulo 52)", no_wrap)
        variant("intra_chroma_pred_mode wrong", sub(lambda l: "chroma_pred=0" in l, "chroma_pred=0", "chroma_pred=1"))
        variant("transform_size_8x8_flag wrong", sub(lambda l: "t8x8=1" in l, "t8x8=1", "t8x8=0"))
        variant("t8x8 macroblock with 16 modes instead of 4", swap_t8_modes)
        variant("qp key missing", drop_qp)
        variant("complete slice reported as stopped", wrong_status)
        # a reader that skips the transform_size_8x8_flag on the real High CAVLC stream
        hl = open(good["mb_high_cavlc.ts"]).read().splitlines()
        variant("High CAVLC: t8x8 reported 0 on a real transform_size_8x8_flag=1 macroblock",
                sub(lambda l: "t8x8=1" in l, "t8x8=1", "t8x8=0"), v("mb_high_cavlc.ts"), hl)
        # the reference's own cross-checks must be able to fail
        print("-- reference cross-checks must detect a corrupted reference (in-process)")
        infos, results = load_reference(synth, td)
        good_ref = not reference_trust(synth, infos, results, td, quiet=True)
        print("   unmodified reference passes all cross-checks:", good_ref)
        ok_all &= good_ref
        r0 = next(r for r in results if len(r.mbs) > 3)
        r0.mbs[2].f["qp"] = (r0.mbs[2].f["qp"] + 1) % 52
        prob = reference_trust(synth, infos, results, td, quiet=True)
        print("   a wrong QP_Y is detected:", bool(prob), "-", (prob or [""])[0][:100])
        ok_all &= bool(prob)
        infos, results = load_reference(synth, td)
        m = next(m for r in results for m in r.mbs if m.f["name"] == "I_NxN" and m.f["t8x8"] == 0)
        mm = m.f["modes"].split(",")
        mm[3] = "-1" if mm[3] != "-1" else "0"
        m.f["modes"] = ",".join(mm)
        prob = reference_trust(synth, infos, results, td, quiet=True)
        print("   a wrong intra mode is detected:", bool(prob), "-", (prob or [""])[0][:100])
        ok_all &= bool(prob)
        infos, results = load_reference(synth, td)
        next(m for r in results for m in r.mbs if m.f["name"] == "I_NxN").f["cbp"] = 99
        prob = reference_trust(synth, infos, results, td, quiet=True)
        print("   a wrong CBP is detected:", bool(prob))
        ok_all &= bool(prob)
        # the generator's own checks: a deliberately broken generator must be caught by ffmpeg
        print("-- a generator with two Table 9-4 entries swapped must be caught by the ffmpeg checks")
        saved = dict(intragen.CBP_TO_CODENUM)
        intragen.CBP_TO_CODENUM[39], intragen.CBP_TO_CODENUM[43] = saved[43], saved[39]
        bad_dir = os.path.join(td, "badgen")
        os.makedirs(bad_dir)
        bad_data, bad_man = intragen.make_stream()
        intragen.CBP_TO_CODENUM.clear()
        intragen.CBP_TO_CODENUM.update(saved)
        bad_path = os.path.join(bad_dir, "bad.264")
        open(bad_path, "wb").write(bad_data)
        prob = ffmpeg_synth_checks(bad_path, bad_man, bad_dir)
        print("   detected:", bool(prob), "-", (prob or [""])[0][:100])
        ok_all &= bool(prob)
    print("SELF-TEST", "PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="input.(ts|264) reader_output.txt")
    ap.add_argument("--make-variants", metavar="OUT_DIR", help="write extra test inputs and exit")
    ap.add_argument("--print-expected", action="store_true",
                    help="files = input out.txt: write the expected dump (what a correct reader prints)")
    ap.add_argument("--self-test", action="store_true", help="verify the harness itself")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if len(a.files) != 2:
        ap.error("need <input> <reader_output> (or --self-test / --make-variants / --print-expected)")
    if a.print_expected:
        ffref.require_tools()
        write_expected(*a.files)
        return
    run_check(*a.files)


if __name__ == "__main__":
    main()
