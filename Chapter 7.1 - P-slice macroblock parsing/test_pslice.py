#!/usr/bin/env python3
"""Harness for Chapter 7.1 (P-slice macroblock parsing, CAVLC).

    python3 test_pslice.py <input.ts|input.264> <mb_dump.txt>          # check your reader's output
    python3 test_pslice.py --print-expected <input> <out.txt>          # write the expected dump, to diff against yours
    python3 test_pslice.py --make-variants <out_dir>                   # extra test inputs (see Readme)
    python3 test_pslice.py --self-test

<mb_dump.txt> is your program's output (grammar in the Readme, section 5): the Phase 4 dump (SLICE / MB / PCM / END / HIST) with
four more keys at the end of every MB line (skip_run sub refidx mvd), one MB line for EVERY macroblock including skipped ones
(mb_type=skip name=P_Skip), the name-based HIST line, and `end_bit` on every END line.

Truth comes from common/inter_ref.py, a reference parser written for this book (there is no JM here; it builds on
common/cavlc_ref.py and cavlc_resid.py).  The reference is validated before your output is compared; a failure there is a
harness problem (exit code 2), not yours:
  1. every I and P slice must end exactly on its rbsp_stop_one_bit (a wrong mb_type / sub_mb_type / ref_idx / mvd / CBP
     table desynchronises the slice);
  2. ffmpeg's `-debug mb_type` map must show the same class and partition shape for every macroblock (S skip, > inter,
     - 16x8, | 8x16, + 8x8, i / I / P intra);
  3. ffmpeg's `-debug qp` map must show the same QP_Y for every macroblock (skipped ones included);
  4. the number of motion vectors ffmpeg exports per picture (`-flags2 +export_mvs`) must equal the number of macroblock
     partitions (skip / 16x16 = 1, 16x8 / 8x16 = 2, 8x8 = 4);
  5. synthetic streams (pgen.py, --make-variants) come with a manifest written by the bit writer: the reference's mb_type,
     bit position, skip_run, sub_mb_type, ref_idx, mvd, CBP, mb_qp_delta, transform_size_8x8_flag must equal it, ffmpeg must
     decode the stream without messages, and in the "zero" stream (all motion vectors are 0) every inter partition must be
     an exact copy of the reference picture its ref_idx names while I_PCM macroblocks keep their raw samples.
Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output, unusable input or reference failure.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import inter_ref as ir, cavlc_ref as ref, ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
from common.mbdump import match_pictures, manifest_path, decode_synth, mb_pixels  # noqa: E402
import pgen  # noqa: E402  (spoiler_reference/pgen.py: the synthetic-stream generator)

KEYS_SLICE = ["type", "first_mb", "pic_size_in_mbs", "header_end_bit"]
KEYS_PCM = ["align_bits", "sum_y", "sum_cb", "sum_cr"]
KEYS_END = ["mbs", "status", "end_bit"]

# x264 variants for --make-variants and the self-test: name -> (extra input args, codec options, rate control)
SRC = "testsrc2=size=352x288:rate=25"
VARIANTS = {
    "x264_p_slices4": ([], "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=3:slices=4", ["-qp", "26"]),
    "x264_p_cip": ([], "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2:constrained-intra=1", ["-qp", "30"]),
    "x264_p_ref16": ([], "-profile:v baseline -bf 0 -x264-params keyint=100:partitions=all:ref=16", ["-qp", "26"]),
    "x264_p_high8x8": ([], "-profile:v high -bf 0 -x264-params cabac=0:8x8dct=1:keyint=50:partitions=all:ref=3:weightp=0",
                       ["-qp", "26"]),
    "x264_p_qpdelta": ([], "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2:aq-mode=2:aq-strength=1.6",
                       ["-crf", "26"]),
    "x264_p_noskip": ([], "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2:fast-pskip=0:no-deblock=1",
                      ["-qp", "20"]),
    "x264_p_allskip": (["GRAY"], "-profile:v baseline -bf 0 -x264-params keyint=50:ref=2", ["-qp", "26"]),
    "x264_pb_main": ([], "-profile:v main -x264-params cabac=0:bframes=2:ref=3:keyint=50", ["-qp", "26"]),
}
SYNTH = ["synth_p_zero", "synth_p_mvd", "synth_p_high"]


def make_variant(name, out_ts):
    vf, opts, rc = VARIANTS[name]
    src = ["-f", "lavfi", "-i", "color=c=gray:size=352x288:rate=25"] if vf == ["GRAY"] else ["-f", "lavfi", "-i", SRC]
    cmd = ["ffmpeg", "-v", "error", "-y", *src, "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", *rc, *opts.split(),
           "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg failed for {name}: {r.stderr.decode()[-500:]}")


def make_synth(mode, out_264):
    data, man = pgen.make_stream(mode)
    open(out_264, "wb").write(data)
    json.dump(man, open(manifest_path(out_264), "w"))


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for t in ("T5", "T6", "T7"):
        shutil.copy(testfiles.get(t), os.path.join(out_dir, t + ".ts"))
        print("wrote", os.path.join(out_dir, t + ".ts"))
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for m in SYNTH:
        p = os.path.join(out_dir, m + ".264")
        make_synth(m.replace("synth_p_", ""), p)
        print("wrote", p, "and", manifest_path(p))


# ================================================================ reference + its validation
def expected_structs(results):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    try:
        tmp.write(ir.render_dump(results))
        tmp.close()
        return ir.parse_dump(tmp.name)
    finally:
        os.unlink(tmp.name)


def check_slices_complete(results):
    problems, n = [], 0
    for r in results:
        if r.status == "skipped":
            continue
        n += 1
        if r.status != "complete":
            problems.append(f"slice {r.info.index}: reference status {r.status} ({r.error or r.note}; "
                            f"{len(r.mbs)} macroblocks parsed)")
    return problems, f"{n} I/P slices parsed by the reference run exactly to their rbsp_stop_one_bit"


def region_bytes(frame, wm, hm, addr, x, y, w, h):
    """Luma w*h plus both chroma (w/2)*(h/2) samples of the rectangle (x, y) inside macroblock addr."""
    W_, H_ = wm * 16, hm * 16
    my, mx = divmod(addr, wm)
    out = bytearray()
    for yy in range(h):
        o = (my * 16 + y + yy) * W_ + mx * 16 + x
        out += frame[o:o + w]
    for plane in range(2):
        base = W_ * H_ + plane * (W_ // 2) * (H_ // 2)
        for yy in range(h // 2):
            o = base + (my * 8 + y // 2 + yy) * (W_ // 2) + mx * 8 + x // 2
            out += frame[o:o + w // 2]
    return bytes(out)


def check_vs_manifest(results, manifest):
    problems = []
    sl_all = [s for pic in manifest["pictures"] for s in pic["slices"]]
    if len(sl_all) != len(results):
        return [f"manifest has {len(sl_all)} slices, the stream {len(results)}"], ""
    n = 0
    for m, r in zip(sl_all, results):
        k = r.info.index
        if r.info.header_end_bit != m["header_end_bit"]:
            problems.append(f"slice {k}: header_end_bit {r.info.header_end_bit} (ffmpeg trace) vs {m['header_end_bit']} (generator)")
        if r.info.num_ref_idx_l0_active != m["active"] and r.info.slice_type == 0:
            problems.append(f"slice {k}: num_ref_idx_l0_active {r.info.num_ref_idx_l0_active} vs generator {m['active']}")
        if len(r.mbs) != len(m["mbs"]) or r.status != "complete":
            problems.append(f"slice {k}: reference parsed {len(r.mbs)} macroblocks / {r.status} ({r.error}), generator wrote "
                            f"{len(m['mbs'])} / complete")
            continue
        for e, rec in zip(m["mbs"], r.mbs):
            n += 1
            keys = ["name", "mb_type", "bit", "qp"] + ([] if e["kind"] == "skip" else
                                                        ["skip_run", "sub", "refidx", "mvd", "cbp", "qp_delta", "t8x8"])
            for key in keys:
                if str(e[key]) != str(rec.f[key]):
                    problems.append(f"slice {k} addr {e['addr']} {key}: reference {rec.f[key]}, generator {e[key]}")
    return problems, f"{n} macroblocks: the reference's mb_type, bit position, skip_run, sub_mb_type, ref_idx, mvd, CBP, " \
                     f"mb_qp_delta, t8x8 and QP equal the generator's"


def synth_pixel_checks(path, manifest, td):
    """ffmpeg decodes the synthetic stream without messages; I_PCM samples are exact; in a zero-mv stream every inter partition
    equals the reference picture named by its ref_idx.  -> (problems, text)"""
    problems, frames = decode_synth(path, manifest, td)
    if problems:
        return problems, ""
    wm, hm = manifest["width_mbs"], manifest["height_mbs"]
    npcm = ncopy = nsens = 0
    for pi, pic in enumerate(manifest["pictures"]):
        for s in pic["slices"]:
            for e in s["mbs"]:
                if "samples" in e:
                    npcm += 1
                    if mb_pixels(frames[pi], wm, hm, e["addr"]) != bytes.fromhex(e["samples"]):
                        problems.append(f"picture {pi} addr {e['addr']}: decoded pixels differ from the I_PCM samples")
                elif manifest["zero_mv"] and "regions" in e:
                    for (x, y, w, h, rf) in e["regions"]:
                        if pi - 1 - rf < 0:
                            problems.append(f"picture {pi} addr {e['addr']}: ref_idx {rf} points before the first picture")
                            continue
                        got = region_bytes(frames[pi], wm, hm, e["addr"], x, y, w, h)
                        exp = region_bytes(frames[pi - 1 - rf], wm, hm, e["addr"], x, y, w, h)
                        ncopy += 1
                        if got != exp:
                            problems.append(f"picture {pi} addr {e['addr']} partition ({x},{y},{w}x{h}) ref_idx {rf}: decoded pixels "
                                            f"are not a copy of picture {pi - 1 - rf}")
                        n_ref = min(pi, pgen.MAX_REFS)
                        others = [region_bytes(frames[pi - 1 - o], wm, hm, e["addr"], x, y, w, h) for o in range(n_ref) if o != rf]
                        nsens += bool(others) and all(o != exp for o in others)
    txt = (f"ffmpeg decodes the synthetic stream without messages; {npcm} I_PCM macroblocks are bit-exact")
    if manifest["zero_mv"]:
        txt += f"; {ncopy} inter partitions are exact copies of their ref_idx picture ({nsens} of them distinguish the ref_idx from every other reference)"
    return problems, txt


def reference_trust(path, infos, results, td, quiet=False):
    say = (lambda *a: None) if quiet else (lambda *a: print(" ", *a))
    problems = []
    for fn, args in ((check_slices_complete, (results,)),
                     (ir.check_vs_ffmpeg_cells, (path, infos, results, td, match_pictures)),
                     (ir.check_vs_ffmpeg_qp, (path, infos, results, td, match_pictures)),
                     (ir.check_vs_ffmpeg_mv_counts, (path, infos, results, td, match_pictures))):
        p, msg = fn(*args)
        problems += p
        say(msg if not p else f"{fn.__name__} FAILED")
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        manifest = json.load(open(mp))
        p, msg = check_vs_manifest(results, manifest)
        problems += p
        say(msg if not p else "synthetic-stream manifest check FAILED")
        p, msg = synth_pixel_checks(path, manifest, td)
        problems += p
        say(msg if not p else "ffmpeg checks of the synthetic stream FAILED")
    return problems


# ================================================================ comparison (reader vs reference)
def first_coef_diff(exp, got):
    e, g = exp.split(";"), (got or "").split(";")
    for i in range(max(len(e), len(g))):
        a = e[i] if i < len(e) else "<none>"
        b = g[i] if i < len(g) else "<none>"
        if a != b:
            return f"block (#{i} in decoding order): expected {a}, got {b}"
    return ""


def compare(rep, exp_slices, exp_hist, act_slices, act_hist):
    rep.check(len(act_slices) == len(exp_slices),
              f"number of SLICE blocks: expected {len(exp_slices)} (one per slice NAL unit, types 1 and 5, in stream order, "
              f"even for slices you skip), got {len(act_slices)}")
    for e, a in zip(exp_slices, act_slices):
        k = e["index"]
        rep.check(a["index"] == k, f"slice block #{k}: expected index {k}, got {a['index']}")
        for key in KEYS_SLICE:
            rep.check(a["slice"].get(key) == e["slice"][key],
                      f"SLICE {k} {key}: expected {e['slice'][key]}, got {a['slice'].get(key)}")
        rep.check(len(a["mbs"]) == len(e["mbs"]),
                  f"SLICE {k}: expected {len(e['mbs'])} MB lines (every macroblock, skipped ones too), got {len(a['mbs'])}; "
                  f"expected END status {e['end']['status']}, you reported {a['end'].get('status')}")
        for j, ((ea, ekv), (aa, akv)) in enumerate(zip(e["mbs"], a["mbs"])):
            rep.check(ea == aa, f"SLICE {k} MB #{j}: expected macroblock address {ea}, got {aa}")
            for key in ir.KEYS_MB:
                if akv.get(key) != ekv[key]:
                    if key == "coef":
                        rep.check(False, f"SLICE {k} MB addr {ea} coef: first differing {first_coef_diff(ekv['coef'], akv.get('coef'))}")
                        continue
                    ctx = f" (expected: name={ekv['name']} sub={ekv['sub']} refidx={ekv['refidx']} mvd={ekv['mvd']} cbp={ekv['cbp']})"
                    rep.check(False, f"SLICE {k} MB addr {ea} {key}: expected {ekv[key]}, got {akv.get(key)}{ctx}")
                else:
                    rep.check(True, "")
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
            own[kv.get("name", "?")] = own.get(kv.get("name", "?"), 0) + 1
    rep.check(act_hist == own, f"HIST does not match your own MB lines: {ir.hist_diff(own, act_hist)}")
    rep.check(act_hist == exp_hist, f"HIST differs from the expected one (name: expected vs yours): "
                                    f"{ir.hist_diff(exp_hist, act_hist)}")


def load_reference(path, td):
    infos = ir.load(path, td)
    if not infos:
        raise SystemExit("input problem: no slice NAL units found")
    return infos, ir.parse_all(infos)


def run_check(path, dump, quiet=False):
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        probs = reference_trust(path, infos, results, td, quiet=quiet)
        if probs:
            print("HARNESS PROBLEM (the reference parser disagrees with an independent source; this is not your bug):")
            for p in probs[:10]:
                print("  ", p)
            sys.exit(2)
        exp_slices, exp_hist = expected_structs(results)
    act_slices, act_hist = ir.parse_dump(dump)
    rep = Report(f"P-slice macroblock parsing of {os.path.basename(path)}")
    compare(rep, exp_slices, exp_hist, act_slices, act_hist)
    nmb = sum(len(s["mbs"]) for s in exp_slices)
    print(f"  {len(exp_slices)} slices, {nmb} macroblocks compared; expected histogram: {ir.hist_line(exp_hist)[5:120]}...")
    rep.finish()


# ================================================================ self-test
MUTANTS = [
    ("te_range1_ue", "ref_idx with num_ref_idx_active = 2 read as ue(v) instead of one inverted bit"),
    ("te_no_invert", "te(v) with range 1: the single bit is not inverted"),
    ("ref_when_one", "ref_idx_l0 read even when num_ref_idx_active = 1"),
    ("ref0_reads_ref", "P_8x8ref0 reads ref_idx like P_8x8"),
    ("mvd_yx", "mvd_l0 read y before x"),
    ("mvd_interleaved", "ref_idx and mvd interleaved per partition (all ref_idx must come first)"),
    ("no_trailing_skip", "no more_rbsp_data() test after a mb_skip_run (a slice ending in skipped macroblocks)"),
    ("skip_run_plus1", "mb_skip_run off by one"),
    ("cbp_intra_col", "intra column of Table 9-4 used for inter macroblocks"),
    ("sub_4x4_two", "P_L0_4x4 treated as having two sub-partitions"),
    ("p8x16_swap", "mb_type 1 and 2 swapped (16x8 / 8x16)"),
    ("intra_offset4", "intra mb_type in P slices taken as mb_type - 4"),
    ("skip_unavail", "skipped macroblocks treated as unavailable for nC (instead of total_coeff 0)"),
    ("qp_after_skip", "QP_Y reset to SliceQP_Y after a skipped macroblock"),
    ("qpd_always", "mb_qp_delta read for inter macroblocks with CBP = 0"),
    ("t8x8_ignores_sub", "transform_size_8x8_flag read although a sub-macroblock is smaller than 8x8"),
    ("t8x8_inter_never", "transform_size_8x8_flag never read for inter macroblocks"),
]


def self_test():
    ok_all = True

    def verdict(label, ok, detail=""):
        nonlocal ok_all
        ok_all &= ok
        print(f"  {'ok  ' if ok else 'BAD '} {label}{(': ' + detail) if detail else ''}")
    with tempfile.TemporaryDirectory() as td:
        # ---- inputs
        inputs = {}
        for t in ("T5", "T6", "T7"):
            inputs[t] = testfiles.get(t)
        for n in VARIANTS:
            p = os.path.join(td, n + ".ts")
            make_variant(n, p)
            inputs[n] = p
        for m in SYNTH:
            p = os.path.join(td, m + ".264")
            make_synth(m.replace("synth_p_", ""), p)
            inputs[m] = p
        loaded = {}
        print("[1] the reference parser against ffmpeg and the generators")
        for name, p in inputs.items():
            sub = os.path.join(td, "w_" + name)
            os.makedirs(sub)
            infos, results = load_reference(p, sub)
            loaded[name] = (p, infos, results, sub)
            probs = reference_trust(p, infos, results, sub, quiet=True)
            nmb = sum(len(r.mbs) for r in results)
            st = {}
            for r in results:
                st[r.status] = st.get(r.status, 0) + 1
            verdict(f"{name}: {len(infos)} slices {st}, {nmb} macroblocks", not probs, "; ".join(probs[:3]))
        # trailing-bit / coverage facts of the T5 and T6 inputs named by the Summary
        for name in ("T5", "T6"):
            res = loaded[name][2]
            verdict(f"{name}: every P slice ends exactly on rbsp_trailing_bits",
                    all(r.status == "complete" for r in res) and any(r.info.slice_type == 0 for r in res))
        allh = {}
        for name in loaded:
            for k, v in ir.histogram(loaded[name][2]).items():
                allh[k] = allh.get(k, 0) + v
        need = ["P_Skip", "P_L0_16x16", "P_L0_L0_16x8", "P_L0_L0_8x16", "P_8x8", "P_8x8ref0", "I_NxN", "I_PCM", "I_16x16_2_0_0"]
        verdict("test inputs cover every P mb_type, skip, and intra types in P slices", all(allh.get(k) for k in need),
                "missing: " + ",".join(k for k in need if not allh.get(k)))
        subs, nrefs, t8, runs = set(), set(), 0, set()
        for name in loaded:
            for r in loaded[name][2]:
                if r.info.slice_type != 0:
                    continue
                nrefs.add(r.info.num_ref_idx_l0_active)
                for rec in r.mbs:
                    if rec.f["sub"] != "-":
                        subs.update(rec.f["sub"].split(","))
                    t8 += rec.f["t8x8"] == 1
                    if rec.f["skip_run"] not in ("-",):
                        runs.add(min(rec.f["skip_run"], 5))
        verdict(f"coverage: sub_mb_types {sorted(subs)}, num_ref_idx_active values {sorted(nrefs)[:8]}.., inter t8x8=1 macroblocks {t8}, "
                f"skip_run values {sorted(runs)}",
                subs >= {"0", "1", "2", "3"} and {1, 2, 3, 4} <= nrefs and t8 > 0 and {0, 1, 2, 5} <= runs)

        # ---- the harness on a correct and on corrupted reader output
        print("[2] the harness on correct and on corrupted reader output")
        name = "synth_p_mvd"
        p, infos, results, sub = loaded[name]
        text = ir.render_dump(results)
        good = os.path.join(td, "good.txt")
        open(good, "w").write(text)
        es, eh = ir.parse_dump(good)

        def judge(txt, label, want_pass):
            f = os.path.join(td, "x.txt")
            open(f, "w").write(txt)
            a_s, a_h = ir.parse_dump(f)
            rep = Report(label)
            compare(rep, es, eh, a_s, a_h)
            verdict(f"{label} -> {'PASS' if not rep.failures else 'FAIL'}", (not rep.failures) == want_pass,
                    "" if (not rep.failures) == want_pass else (rep.failures[0] if rep.failures else "unexpected pass"))
        judge(text, "correct output", True)
        lines = text.splitlines()
        i = next(k for k, l in enumerate(lines) if l.startswith("MB") and "mvd=0:0" not in l and "mvd=-" not in l)
        bad = lines[:]
        bad[i] = re.sub(r"mvd=(-?\d+):(-?\d+)", lambda m: f"mvd={int(m.group(1)) + 1}:{m.group(2)}", bad[i], count=1)
        judge("\n".join(bad) + "\n", "one mvd value changed", False)
        j = max(k for k, l in enumerate(lines) if l.startswith("MB"))
        judge("\n".join(lines[:j] + lines[j + 1:]) + "\n", "last macroblock line missing", False)
        k = next(k for k, l in enumerate(lines) if "name=P_Skip" in l)
        judge("\n".join(lines[:k] + lines[k + 1:]) + "\n", "a skipped macroblock line omitted", False)
        judge(text.replace("P_Skip:", "P_Skip:1", 1), "wrong HIST count", False)

        # ---- mutants of the reference parser
        print("[3] deliberately wrong parsing rules: each must change the dump (and so fail the comparison)")
        order = ["synth_p_zero", "synth_p_mvd", "synth_p_high", "x264_p_high8x8", "x264_p_allskip", "T6", "x264_p_slices4"]
        good_text = {n: ir.render_dump(loaded[n][2]) for n in order}
        for mut, desc in MUTANTS:
            ir.MUTANT = mut
            caught = None
            try:
                for n in order:
                    pth, infos, _, _ = loaded[n]
                    mres = ir.parse_all(infos)
                    mtext = ir.render_dump(mres)
                    if mtext != good_text[n]:
                        bad_slices = sum(1 for r in mres if r.status == "error")
                        f = os.path.join(td, "m.txt")
                        open(f, "w").write(mtext)
                        a_s, a_h = ir.parse_dump(f)
                        gf = os.path.join(td, "g.txt")
                        open(gf, "w").write(good_text[n])
                        e_s, e_h = ir.parse_dump(gf)
                        rep = Report(mut)
                        compare(rep, e_s, e_h, a_s, a_h)
                        if rep.failures:
                            caught = (n, rep.failures[0], bad_slices)
                            break
            finally:
                ir.MUTANT = None
            verdict(f"mutant {mut} ({desc})", caught is not None,
                    f"caught on {caught[0]}: {caught[1][:110]}" if caught else "NOT CAUGHT by any test input")
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
            open(out, "w").write(ir.render_dump(results))
        print("wrote", out)
        return
    run_check(inp, out)


if __name__ == "__main__":
    main()
