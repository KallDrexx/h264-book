#!/usr/bin/env python3
"""Harness for Chapter 4.3 (neighbour availability, nC and coeff_token).

    python3 test_coeftoken.py --print-blocks <input.ts|input.264> <blocks.txt>   # the block boundary file your reader consumes
    python3 test_coeftoken.py <input.ts|input.264> <mb_dump.txt>                 # check your reader's output
    python3 test_coeftoken.py --print-expected <input> <out.txt>                 # write the expected dump, to diff against yours
    python3 test_coeftoken.py --make-variants <out_dir>                          # extra test inputs (see Readme)
    python3 test_coeftoken.py --self-test

<mb_dump.txt> is your program's output: the line grammar of Chapters 4.1 / 4.2 (SLICE / MB / PCM / END / HIST) with
five more keys at the end of every MB line:

    ... qp=<0-51>  nc=<list|->  tc=<list|->  tcl=<16 numbers>  tccb=<4 numbers>  tccr=<4 numbers>

The Chapter 4.4 syntax (levels, total_zeros, run_before) is not part of this chapter, so --print-blocks gives your
reader the bit position where every residual block starts and ends.  Your reader decodes the coeff_token at each start
position, using the nC it derives from its own neighbour bookkeeping.

Truth comes from common/cavlc_ref.py (level 3) + common/cavlc_resid.py, a reference parser written for this book
(there is no JM here).  It parses EVERYTHING of every block, so a slice only counts as parsed when the macroblocks end
exactly on the rbsp_stop_one_bit.  Before your output is compared with it, the reference itself is cross-checked:
  * every I slice of the input (the T1..T4 files, real High CAVLC files, ...) must run exactly to its rbsp_stop_one_bit:
    a wrong nC (hence a wrong coeff_token table), a wrong table entry or a wrong block order would desynchronise it;
  * ffmpeg's macroblock map (`-debug mb_type`) and QP map (`-debug qp`) agree with the reference's macroblocks;
  * Table 9-5 passes structural checks and equals the tables found in the installed libavcodec (best effort);
  * for the synthetic stream (--make-variants): the manifest written by an independent bit writer (it chooses every
    block's TotalCoeff / TrailingOnes, derives nC with its own neighbour grid and covers all 4 x 62 + 14 Table 9-5
    entries), ffmpeg decoding it without any error message (ffmpeg checks that every slice ends exactly where it
    should), ffmpeg's QP map, and the pixels of every I_PCM macroblock.
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
from common import cavlc_ref as ref, cavlc_resid, cavlc_tables as tables, ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
from common.mbdump import (KEYS_SLICE, KEYS_PCM, KEYS_END, die, parse_reader, hist_diff,  # noqa: E402
                           check_reference_vs_ffmpeg, match_pictures, manifest_path, decode_synth,
                           pcm_pixel_problems)
import tokgen  # noqa: E402

LEVEL = 3                                                        # cavlc_ref parsing level of this chapter
KEYS_NEW = ref.EXTRA_KEYS_BY_LEVEL[3]                            # nc tc tcl tccb tccr
KEYS_MB = ref.MbRecord.CORE_KEYS + ref.EXTRA_KEYS_BY_LEVEL[2] + KEYS_NEW


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
                  f"SLICE {k}: expected {len(e['mbs'])} MB lines, got {len(a['mbs'])}; expected END status "
                  f"{e['end']['status']}, you reported {a['end']['status']}")
        for j, ((ea, ekv), (aa, akv)) in enumerate(zip(e["mbs"], a["mbs"])):
            rep.check(ea == aa, f"SLICE {k} MB #{j}: expected macroblock address {ea}, got {aa}")
            for key in KEYS_MB:
                if akv.get(key) != ekv[key]:
                    extra = ""
                    if key in KEYS_NEW:
                        extra = (f" (expected: mb_type={ekv['mb_type']} cbp={ekv['cbp']} nc={ekv['nc']} tc={ekv['tc']} "
                                 f"tcl={ekv['tcl']} tccb={ekv['tccb']} tccr={ekv['tccr']})")
                    rep.check(False, f"SLICE {k} MB addr {ea} {key}: expected {ekv[key]}, got {akv.get(key)}{extra}")
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
            if re.fullmatch(r"\d+", kv.get("mb_type", "")):
                own[int(kv["mb_type"])] = own.get(int(kv["mb_type"]), 0) + 1
    rep.check(act_hist == own, f"HIST does not match your own MB lines: {hist_diff(own, act_hist)}")
    rep.check(act_hist == exp_hist, f"HIST differs from the expected one (mb_type: expected vs yours): "
                                    f"{hist_diff(exp_hist, act_hist)}")


# ================================================================ checking the reference itself
def check_tables():
    """Table 9-5 and friends: structure, and the comparison with libavcodec.  -> (problems, text)"""
    problems = tables.check_tables()
    p2, msg = tables.check_against_libavcodec()
    return problems + p2, msg


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
            mine = 0 if rec.pcm is not None else rec.f["qp"]       # ffmpeg stores 0 for I_PCM (deblocking value)
            n += 1
            if rows[y][x] != mine:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference QP_Y {mine}, ffmpeg {rows[y][x]}")
    return problems, f"ffmpeg QP cross-check: QP_Y of {n} macroblocks in {len(usable)} picture(s) agree{note}"


def check_slices_complete(results):
    """Every slice the reference does not skip must run exactly to its rbsp_stop_one_bit."""
    problems, n = [], 0
    for r in results:
        if r.status == "skipped":
            continue
        n += 1
        if r.status != "complete":
            problems.append(f"slice {r.info.index}: reference status {r.status}"
                            f" ({r.error or r.note}; {len(r.mbs)} macroblocks parsed)")
    return problems, f"{n} slices parsed by the reference run exactly to their rbsp_stop_one_bit"


def synth_entry_key(e):
    return (e["addr"], e["kind"])


def check_reference_vs_manifest(results, manifest):
    """Per block (kind, idx, nC, TotalCoeff, TrailingOnes, start bit, end bit), stored total_coeff, bit position,
    QP and PCM samples: reference vs generator."""
    problems = []
    sl_all = [s for pic in manifest["pictures"] for s in pic]
    if len(sl_all) != len(results):
        return [f"manifest has {len(sl_all)} slices, the stream {len(results)}"], ""
    nmb = nblk = 0
    for m, r in zip(sl_all, results):
        k = r.info.index
        if r.info.header_end_bit != m["header_end_bit"]:
            problems.append(f"slice {k}: header_end_bit {r.info.header_end_bit} (ffmpeg trace) vs {m['header_end_bit']} (generator)")
        if len(r.mbs) != len(m["mbs"]) or r.status != "complete":
            problems.append(f"slice {k}: reference parsed {len(r.mbs)} macroblocks / {r.status}, generator wrote "
                            f"{len(m['mbs'])} / complete")
            continue
        for rec, e in zip(r.mbs, m["mbs"]):
            nmb += 1
            f = rec.f
            st = e["stored"]
            if (rec.addr, f["mb_type"], f["bit"], f["qp"]) != (e["addr"], e["mb_type"], e["bit"], e["qp"]):
                problems.append(f"slice {k} addr {e['addr']}: reference (addr, mb_type, bit, qp) "
                                f"{(rec.addr, f['mb_type'], f['bit'], f['qp'])}, generator "
                                f"{(e['addr'], e['mb_type'], e['bit'], e['qp'])}")
            got_st = (f["tcl"], f["tccb"], f["tccr"])
            want_st = (",".join(map(str, st["luma"])), ",".join(map(str, st["cb"])), ",".join(map(str, st["cr"])))
            if got_st != want_st:
                problems.append(f"slice {k} addr {e['addr']}: stored total_coeff {got_st}, generator {want_st}")
            mine = [(b["kind"], b["idx"], b["nc"], b["tc"], b["t1"], b["start"], b["end"]) for b in rec.blocks]
            gen = [(b["kind"], b["idx"], b["nc"], b["tc"], b["t1"], b["start"], b["end"]) for b in e["blocks"]]
            nblk += len(gen)
            if mine != gen:
                d = next((i for i, (x, y) in enumerate(zip(mine, gen)) if x != y), min(len(mine), len(gen)))
                problems.append(f"slice {k} addr {e['addr']}: block {d} (kind, idx, nC, TotalCoeff, TrailingOnes, start, "
                                f"end): reference {mine[d] if d < len(mine) else None}, generator {gen[d] if d < len(gen) else None}")
            if e["kind"] == "pcm":
                if rec.pcm is None or rec.pcm["samples"] != bytes.fromhex(e["samples"]):
                    problems.append(f"slice {k} addr {e['addr']}: PCM samples differ from generator")
    return problems, f"{nmb} macroblocks / {nblk} residual blocks of the reference equal the generator's manifest " \
                     f"(nC, TotalCoeff, TrailingOnes, bit positions, stored total_coeff, QP chain)"


def coverage_problems(results):
    """The reference's view of the synthetic stream must contain every coeff_token entry in its own table."""
    seen = set()
    for r in results:
        for rec in r.mbs:
            for b in getattr(rec, "blocks", []):
                seen.add((tokgen.class_of(b["nc"]), b["tc"], b["t1"]))
    want = {(c, tc, t1) for c in range(4) for tc in range(17) for t1 in range(min(3, tc) + 1)}
    want |= {("dc", tc, t1) for tc in range(5) for t1 in range(min(3, tc) + 1)}
    miss = sorted(want - seen, key=str)
    return [f"coeff_token entry never used by the synthetic stream: table {c} TotalCoeff {tc} TrailingOnes {t1}"
            for c, tc, t1 in miss[:10]] + ([f"... {len(miss)} entries missing in all"] if len(miss) > 10 else []), len(want)


def ffmpeg_synth_checks(path, manifest, td):
    """ffmpeg decodes the synthetic stream cleanly (it verifies that every slice ends where the syntax says);
    its QP map equals the manifest's QP_Y of every macroblock (I_PCM: 0); every I_PCM macroblock's pixels equal
    its raw samples.  -> problems"""
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
    p3, msg3 = check_slices_complete(results)
    problems += p3
    if not quiet:
        print(" ", msg3)
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        manifest = json.load(open(mp))
        p4, msg4 = check_reference_vs_manifest(results, manifest)
        p5, n_entries = coverage_problems(results)
        p6 = ffmpeg_synth_checks(path, manifest, td)
        problems += p4 + p5 + p6
        if not quiet:
            print(" ", msg4 if not p4 else "synthetic-stream manifest check FAILED")
            print(f"  the stream uses all {n_entries} coeff_token entries (4 nC ranges x 62 + 14 chroma DC)"
                  if not p5 else "  coeff_token coverage check FAILED")
            print("  ffmpeg decodes the synthetic stream without errors, its QP map equals the generator's QP_Y of "
                  "every macroblock and every I_PCM macroblock's pixels equal its raw samples" if not p6
                  else "  ffmpeg checks of the synthetic stream FAILED")
    return problems


def run_check(path, dump):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input file not found: {path}")
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        problems = reference_trust(path, infos, results, td)
        tp, tmsg = check_tables()
        problems += tp
        print(" ", "Table 9-5 structure OK; " + tmsg if not tp else "code table checks FAILED")
        if problems:
            print("HARNESS PROBLEM (not your fault): the reference parser disagrees with an independent check:")
            for p in problems[:10]:
                print("   ", p)
            sys.exit(2)
        exp_slices, exp_hist = expected_structs(results)
    act_slices, act_hist = parse_reader(dump)
    rep = Report("Neighbour availability and coeff_token")
    compare(rep, exp_slices, exp_hist, act_slices, act_hist)
    mbs = [kv for s in exp_slices for _, kv in s["mbs"]]
    blocks = sum(len([x for x in kv["tc"].split(",") if x != "-"]) for kv in mbs)
    cnt = lambda st: sum(1 for s in exp_slices if s["end"]["status"] == st)  # noqa: E731
    print(f"  reference: {len(infos)} slices, {len(mbs)} macroblocks, {blocks} coeff_tokens, "
          f"{cnt('complete')} slices parsed completely, {cnt('skipped')} skipped")
    rep.finish()


def write_expected(path, out):
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        open(out, "w").write(ref.render_dump(results, LEVEL))


def write_blocks(path, out):
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        bad = [r for r in results if r.status in ("error", "stopped")]
        if bad:
            die(f"HARNESS PROBLEM: reference could not parse slice {bad[0].info.index}: {bad[0].error or bad[0].note}")
        open(out, "w").write(ref.render_blocks(results))


# ================================================================ test inputs / variants
def x264_clip(out, src, extra, frames=2):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
           "-c:v", "libx264", *extra, "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die("ffmpeg failed: " + r.stderr.decode()[-500:])


def make_variants(outdir):
    ffref.require_tools()
    os.makedirs(outdir, exist_ok=True)
    data, man = tokgen.make_stream()
    with open(os.path.join(outdir, "tokens_synth.264"), "wb") as f:
        f.write(data)
    with open(os.path.join(outdir, "tokens_synth.manifest.json"), "w") as f:
        json.dump(man, f)
    nsl = sum(len(p) for p in man["pictures"])
    nb = sum(len(e["blocks"]) for p in man["pictures"] for s in p for e in s["mbs"])
    print(f"tokens_synth.264: synthetic Annex B stream (96x80, CAVLC): {len(man['pictures'])} IDR pictures, {nsl} I slices, "
          f"{nb} residual blocks. Coverage pictures (one macroblock per slice) use every Table 9-5 entry in all four nC "
          f"ranges and for chroma DC; mixed pictures have I_PCM next to coded blocks, Intra16x16 with / without AC, "
          f"I_NxN with every CBP, the 8x8 transform flag and several slices per picture.")
    testsrc = "testsrc2=size=176x144:rate=25"
    x264_clip(os.path.join(outdir, "real_high_cavlc.ts"), testsrc,
              ["-profile:v", "high", "-crf", "20", "-x264-params", "keyint=1:cabac=0:8x8dct=1:slices=3"])
    print("real_high_cavlc.ts: x264 High, CAVLC, 8x8dct: real I_NxN macroblocks with transform_size_8x8_flag = 1 and "
          "coefficients (each 8x8 block is four 4x4 blocks with their own coeff_token), 3 slices per picture")
    x264_clip(os.path.join(outdir, "real_lowqp.ts"), testsrc,
              ["-profile:v", "baseline", "-qp", "6", "-x264-params", "keyint=1:no-deblock=1"])
    print("real_lowqp.ts: x264 Baseline at QP 6: many coefficients per block, so the nC >= 8 table (6-bit codes) and "
          "large TotalCoeff values occur on real data")
    x264_clip(os.path.join(outdir, "real_noise.ts"), "color=c=gray:size=176x144:rate=25,format=yuv420p,"
              "geq=lum='random(1)*255':cb='128+random(1)*40':cr=128",
              ["-profile:v", "main", "-qp", "22", "-x264-params", "keyint=1:cabac=0:slices=2"])
    print("real_noise.ts: x264 Main CAVLC on noise at QP 22: dense blocks everywhere (TotalCoeff up to 16 in luma and chroma AC)")
    print("Also useful (from common.testfiles): T1, T2, T3, T4 (4 slices per picture), default (P and B slices are "
          "skipped; its first I slice is parsed), T10_full (CABAC: every slice skipped)")


# ================================================================ self test
def run_in_subprocess(inp, dump):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), inp, dump], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=900)
    for l in (r.stdout.decode() + r.stderr.decode()).splitlines()[-3:]:
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
        inputs = [testfiles.get(n) for n in ("T1", "T4", "default", "T10_full")]
        inputs += [v(n) for n in ("tokens_synth.264", "real_high_cavlc.ts", "real_lowqp.ts", "real_noise.ts")]
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
        # the block boundary file: well formed, and a reader that decodes at every start reaches every end
        blocks = os.path.join(td, "blocks.txt")
        write_blocks(v("real_high_cavlc.ts"), blocks)
        nb = sum(1 for l in open(blocks) if l.startswith("BLK "))
        print(f"-- --print-blocks on real_high_cavlc.ts wrote {nb} BLK lines (must be > 0)")
        ok_all &= nb > 0

        synth = v("tokens_synth.264")
        lines = open(good["tokens_synth.264"]).read().splitlines()

        def variant(label, edit, inp=synth, base=lines):
            nonlocal ok_all
            ls = edit(list(base))
            p = os.path.join(td, "bad.txt")
            open(p, "w").write("\n".join(ls) + "\n")
            print(f"-- {label} (must FAIL)")
            rc = run_in_subprocess(inp, p)
            print("   exit", rc)
            ok_all &= rc == 1

        def kv_edit(pred, key, fn):
            def edit(ls):
                i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and pred(dict(t.split("=", 1) for t in l.split()[3:])))
                kv = dict(t.split("=", 1) for t in ls[i].split()[3:])
                ls[i] = ls[i].replace(f" {key}={kv[key]}", f" {key}={fn(kv[key])}", 1)
                return ls
            return edit

        def bump(s):
            xs = s.split(",")
            xs[1] = str(int(xs[1]) + 1)
            return ",".join(xs)

        def swap_tc_t1(s):
            xs = s.split(",")
            for i, x in enumerate(xs):
                a, b = x.split("/")
                if a != b and int(b) > 0:
                    xs[i] = f"{b}/{a}"
                    break
            return ",".join(xs)

        def drop_key(key):
            def edit(ls):
                i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and f" {key}=" in l)
                ls[i] = re.sub(rf" {key}=\S+", "", ls[i])
                return ls
            return edit
        coded = lambda kv: kv["tc"] != "-" and len(kv["tc"].split(",")) > 3 and kv["tc"].split(",")[1] != "0/0"  # noqa: E731
        variant("one nC value wrong", kv_edit(coded, "nc", bump))
        variant("one coeff_token TotalCoeff wrong", kv_edit(coded, "tc", lambda s: "9/0" + s[s.index(","):] if "," in s else "9/0"))
        variant("TotalCoeff and TrailingOnes swapped in a token", kv_edit(lambda kv: coded(kv) and any(
            x.split("/")[0] != x.split("/")[1] and x.split("/")[1] != "0" for x in kv["tc"].split(",")), "tc", swap_tc_t1))
        variant("stored luma total_coeff wrong (tcl)", kv_edit(lambda kv: kv["tcl"] != "-" and kv["tcl"] != ",".join(["0"] * 16),
                                                               "tcl", lambda s: "7" + s[s.index(","):]))
        variant("stored Cb total_coeff wrong (tccb)", kv_edit(lambda kv: kv["tccb"] != "0,0,0,0", "tccb", lambda s: "15,15,15,15"))
        variant("tc key missing", drop_key("tc"))
        variant("nc key missing", drop_key("nc"))
        variant("I_PCM neighbour stored as 0 instead of 16 (tcl)", kv_edit(lambda kv: kv["name"] == "I_PCM", "tcl",
                                                                          lambda s: ",".join(["0"] * 16)))

        def wrong_status(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("END ") and "status=complete" in l)
            ls[i] = ls[i].replace("complete", "stopped")
            return ls
        variant("complete slice reported as stopped", wrong_status)
        hl = open(good["real_high_cavlc.ts"]).read().splitlines()
        variant("real High CAVLC: one stored 4x4 total_coeff of an 8x8-transform macroblock wrong",
                kv_edit(lambda kv: kv["t8x8"] == "1" and kv["tcl"] != ",".join(["0"] * 16), "tcl", lambda s: "5" + s[s.index(","):]),
                v("real_high_cavlc.ts"), hl)
        # classic bugs of the neighbour / nC rules: run the reference with the bug and require the harness to fail
        mutants = {"nc_floor": "nC = (nA + nB) >> 1 (forgot the +1)",
                   "pcm0": "I_PCM neighbours count 0 instead of 16",
                   "dc_counted": "Intra16x16 DC total_coeff stored in block 0",
                   "unavail_as_zero": "unavailable neighbours treated as available with 0",
                   "ignore_b": "above neighbour ignored",
                   "chroma_dc_nc": "chroma DC read with nC = 0 instead of -1"}
        for name, what in mutants.items():
            cavlc_resid.MUTANT = name
            try:
                infos, results = load_reference(synth, td)
                text = ref.render_dump(results, LEVEL)
            finally:
                cavlc_resid.MUTANT = None
            p = os.path.join(td, "mut.txt")
            open(p, "w").write(text)
            print(f"-- a reader with this bug: {what} (must FAIL)")
            rc = run_in_subprocess(synth, p)
            print("   exit", rc)
            ok_all &= rc == 1
        # the reference's own cross-checks must be able to fail
        print("-- reference cross-checks must detect a corrupted reference (in-process)")
        infos, results = load_reference(synth, td)
        good_ref = not reference_trust(synth, infos, results, td, quiet=True)
        print("   unmodified reference passes all cross-checks:", good_ref)
        ok_all &= good_ref
        for name in ("nc_floor", "pcm0", "dc_counted", "unavail_as_zero"):
            cavlc_resid.MUTANT = name
            try:
                infos, results = load_reference(synth, td)
            finally:
                cavlc_resid.MUTANT = None
            prob = reference_trust(synth, infos, results, td, quiet=True)
            print(f"   a reference with bug {name} is detected:", bool(prob), "-", (prob or [""])[0][:90])
            ok_all &= bool(prob)
        # a typo in the reference's Table 9-5 (swap two entries of the 4-7 column): caught by the manifest / trailing bits
        t = tables.COEFF_TOKEN[2]
        saved = dict(t)
        t[(5, 1)], t[(5, 2)] = saved[(5, 2)], saved[(5, 1)]
        cavlc_resid._DEC.clear()
        try:
            infos, results = load_reference(synth, td)
            prob = reference_trust(synth, infos, results, td, quiet=True)
        finally:
            t.clear()
            t.update(saved)
            cavlc_resid._DEC.clear()
        print("   a typo in the reference's Table 9-5 is detected:", bool(prob), "-", (prob or [""])[0][:90])
        ok_all &= bool(prob)
        tp = tables.check_tables()
        t[(7, 0)] = t[(7, 1)]
        bad_tp = tables.check_tables()
        t.clear()
        t.update(saved)
        print("   the table structure check detects a duplicated code:", bool(bad_tp) and not tp)
        ok_all &= bool(bad_tp) and not tp
        # a generator with a wrong Table 9-5 entry must be caught by the ffmpeg checks
        for label, tab, k1, k2 in (("nC 0-1", 0, (3, 0), (3, 1)), ("nC 8+", 3, (9, 2), (9, 3)), ("nC 4-7", 2, (4, 2), (5, 3))):
            print(f"-- a generator with two {label} coeff_token entries swapped must be caught by ffmpeg")
            saved_g = dict(tokgen.CT[tab])
            tokgen.CT[tab][k1], tokgen.CT[tab][k2] = saved_g[k2], saved_g[k1]
            bad_dir = os.path.join(td, "badgen")
            os.makedirs(bad_dir, exist_ok=True)
            try:
                bad_data, bad_man = tokgen.make_stream()
            finally:
                tokgen.CT[tab].clear()
                tokgen.CT[tab].update(saved_g)
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
    ap.add_argument("--print-blocks", action="store_true",
                    help="files = input blocks.txt: write the block boundary file for your reader")
    ap.add_argument("--self-test", action="store_true", help="verify the harness itself")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if len(a.files) != 2:
        ap.error("need <input> <reader_output> (or --self-test / --make-variants / --print-expected / --print-blocks)")
    if a.print_expected:
        ffref.require_tools()
        write_expected(*a.files)
        return
    if a.print_blocks:
        ffref.require_tools()
        write_blocks(*a.files)
        return
    run_check(*a.files)


if __name__ == "__main__":
    main()
