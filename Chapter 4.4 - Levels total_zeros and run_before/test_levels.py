#!/usr/bin/env python3
"""Harness for Chapter 4.4 (levels, total_zeros and run_before: the complete CAVLC residual).

    python3 test_levels.py <input.ts|input.264> <mb_dump.txt>              # check your reader's output
    python3 test_levels.py --print-expected <input> <out.txt>              # write the expected dump, to diff against yours
    python3 test_levels.py --make-variants <out_dir>                       # extra test inputs (see Readme)
    python3 test_levels.py --self-test

<mb_dump.txt> is your program's output: the line grammar of Chapters 4.1 - 4.3 (SLICE / MB / PCM / END / HIST), with one
more key at the end of every MB line and one more on the END line:

    MB ...  ... tccr=<4 numbers>  coef=<blocks or ->
    END <i> mbs=<n> status=<...> end_bit=<n or ->

There is no block boundary file any more: the reader parses every slice by itself, from the first macroblock to the
rbsp_stop_one_bit.  `coef` lists the non-zero coefficients of every coded residual block in decoding order (Readme,
section 5).  `end_bit` of a complete slice is the bit position where the last macroblock ended, which must be the position
of the rbsp_stop_one_bit.

Truth comes from common/cavlc_ref.py (level 4) + common/cavlc_resid.py, a reference parser written for this book (there is
no JM here).  What makes the COEFFICIENT VALUES trustworthy (the reference is checked before your output is compared):

  1. every I slice of the input must run exactly to its rbsp_stop_one_bit (a wrong table entry or length desynchronises
     the slice; a wrong VALUE that keeps the length does not, which is why 2 and 3 exist);
  2. the synthetic stream (--make-variants, levgen.py): an independent encoder-side implementation writes KNOWN
     coefficient arrays (every TotalCoeff / total_zeros pair, every zerosLeft / run_before pair, level_prefix 0..16, all
     suffixLength values 0..6 and their thresholds).  The reference's arrays must equal them exactly, ffmpeg must decode
     the stream without errors, and ffmpeg's decoded PIXELS must equal an independent mini reconstruction
     (minirecon.py) of those arrays.  So reference == generator == ffmpeg's decoder on the values;
  3. real x264 streams in which every macroblock is its own slice (--make-variants) and the first macroblock of every
     slice of any input with deblocking off: such a macroblock has no neighbours, so its chroma and Intra16x16 pixels
     (and block 0 of an I_NxN macroblock) equal mini-reconstruction(coefficients) exactly: real data, real values;
  4. ffmpeg's macroblock map and QP map agree with the reference's macroblocks; the code tables are checked
     structurally and against libavcodec.
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
                           check_reference_vs_ffmpeg, match_pictures, manifest_path, decode_synth, mb_pixels)
import levgen  # noqa: E402
import minirecon  # noqa: E402

LEVEL = 4                                                        # cavlc_ref parsing level of this chapter
KEYS_NEW = ref.EXTRA_KEYS_BY_LEVEL[4]                            # coef
KEYS_MB = ref.MbRecord.CORE_KEYS + ref.EXTRA_KEYS_BY_LEVEL[2] + ref.EXTRA_KEYS_BY_LEVEL[3] + KEYS_NEW


def expected_structs(results):
    text = ref.render_dump(results, LEVEL)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    try:
        tmp.write(text)
        tmp.close()
        return parse_reader(tmp.name)
    finally:
        os.unlink(tmp.name)


def first_coef_diff(exp, got):
    """Describe the first block whose coefficients differ."""
    e, g = exp.split(";"), (got or "").split(";")
    for i in range(max(len(e), len(g))):
        a = e[i] if i < len(e) else "<none>"
        b = g[i] if i < len(g) else "<none>"
        if a != b:
            return f"block (#{i} in decoding order): expected {a}, got {b}"
    return ""


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
                    if key == "coef":       # the full lists can be thousands of characters: name the first wrong block
                        rep.check(False, f"SLICE {k} MB addr {ea} coef: first differing {first_coef_diff(ekv['coef'], akv.get('coef'))} "
                                         f"(mb_type={ekv['mb_type']} cbp={ekv['cbp']} qp={ekv['qp']} nc={ekv['nc']} tc={ekv['tc']}; "
                                         f"--print-expected shows the whole line)")
                        continue
                    extra = ""
                    if key in ("nc", "tc", "tcl", "tccb", "tccr"):
                        extra = f" (expected: mb_type={ekv['mb_type']} cbp={ekv['cbp']} nc={ekv['nc']} tc={ekv['tc']})"
                    rep.check(False, f"SLICE {k} MB addr {ea} {key}: expected {ekv[key]}, got {akv.get(key)}{extra}")
                else:
                    rep.check(True, "")
        rep.check(sorted(a["pcm"]) == sorted(e["pcm"]),
                  f"SLICE {k}: PCM lines for macroblocks {sorted(a['pcm'])}, expected {sorted(e['pcm'])}")
        for addr in sorted(set(e["pcm"]) & set(a["pcm"])):
            for key in KEYS_PCM:
                rep.check(a["pcm"][addr].get(key) == e["pcm"][addr][key],
                          f"SLICE {k} PCM addr {addr} {key}: expected {e['pcm'][addr][key]}, got {a['pcm'][addr].get(key)}")
        for key in KEYS_END + ["end_bit"]:
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
    """Every slice the reference does not skip must run exactly to its rbsp_stop_one_bit (end_bit = the stop bit)."""
    problems, n = [], 0
    for r in results:
        if r.status == "skipped":
            continue
        n += 1
        if r.status != "complete":
            problems.append(f"slice {r.info.index}: reference status {r.status}"
                            f" ({r.error or r.note}; {len(r.mbs)} macroblocks parsed)")
    return problems, f"{n} slices parsed by the reference run exactly to their rbsp_stop_one_bit"


def check_reference_vs_manifest(results, manifest):
    """Per macroblock and block: reference vs the arrays the generator wrote (kind, idx, nC, TotalCoeff, TrailingOnes, bit
    positions, and above all the coefficient arrays)."""
    problems = []
    sl_all = [s for pic in manifest["pictures"] for s in pic["slices"]]
    if len(sl_all) != len(results):
        return [f"manifest has {len(sl_all)} slices, the stream {len(results)}"], ""
    nmb = nblk = ncoef = 0
    for m, r in zip(sl_all, results):
        k = r.info.index
        e = m["mb"]
        if r.info.header_end_bit != m["header_end_bit"]:
            problems.append(f"slice {k}: header_end_bit {r.info.header_end_bit} (ffmpeg trace) vs {m['header_end_bit']} (generator)")
        if len(r.mbs) != 1 or r.status != "complete":
            problems.append(f"slice {k}: reference parsed {len(r.mbs)} macroblocks / {r.status} ({r.error}), generator wrote 1 / complete")
            continue
        rec = r.mbs[0]
        nmb += 1
        f = rec.f
        if (f["mb_type"], f["bit"], f["qp"], f["cbp"]) != (e["mb_type"], e["bit"], e["qp"], e["cbp"]):
            problems.append(f"slice {k}: reference (mb_type, bit, qp, cbp) {(f['mb_type'], f['bit'], f['qp'], f['cbp'])}, "
                            f"generator {(e['mb_type'], e['bit'], e['qp'], e['cbp'])}")
        mine = [(b["kind"], b["idx"], b["nc"], b["tc"], b["t1"], b["start"], b["end"]) for b in rec.blocks]
        gen = [(b["kind"], b["idx"], b["nc"], b["tc"], b["t1"], b["start"], b["end"]) for b in e["blocks"]]
        nblk += len(gen)
        if mine != gen:
            d = next((i for i, (x, y) in enumerate(zip(mine, gen)) if x != y), min(len(mine), len(gen)))
            problems.append(f"slice {k}: block {d} (kind, idx, nC, TotalCoeff, TrailingOnes, start, end): reference "
                            f"{mine[d] if d < len(mine) else None}, generator {gen[d] if d < len(gen) else None}")
            continue
        for b, g in zip(rec.blocks, e["blocks"]):
            ncoef += sum(1 for v in g["coeffs"] if v)
            if b["coeffs"] != g["coeffs"]:
                problems.append(f"slice {k} {g['kind']}{g['idx']}: reference coefficients {b['coeffs']}, generator {g['coeffs']}")
    return problems, f"{nmb} macroblocks / {nblk} residual blocks / {ncoef} non-zero coefficients: the reference's arrays " \
                     f"equal the generator's (and so do nC, TotalCoeff, TrailingOnes, bit positions, QP)"


def coverage_problems(path, td):
    """Re-run the reference with statistics on: the synthetic stream must exercise every table entry / rule it claims."""
    infos = ref.load_stream(path, td)
    cavlc_resid.STATS = {k: set() for k in ("tz", "run", "level", "adjust", "sl", "init")}
    try:
        for i in infos:
            ref.parse_slice(i, LEVEL)
        st = cavlc_resid.STATS
    finally:
        cavlc_resid.STATS = None
    want_tz = {("4x4", tc, tz) for tc in range(1, 16) for tz in range(0, 17 - tc)} | \
              {("dc", tc, tz) for tc in range(1, 4) for tz in range(0, 5 - tc)}
    want_run = {(z, r) for z in range(1, 15) for r in range(z + 1)}
    want_level = {(0, p) for p in range(17)} | {(s, p) for s in range(1, 7) for p in range(0, 16)}
    problems = []
    for name, want, got in (("(table, TotalCoeff, total_zeros)", want_tz, st["tz"]), ("(zerosLeft, run_before)", want_run, st["run"]),
                            ("(suffixLength, level_prefix)", want_level, st["level"])):
        miss = sorted(want - got, key=str)
        if miss:
            problems.append(f"the synthetic stream never uses {name} {miss[:6]}{' ...' if len(miss) > 6 else ''} ({len(miss)} missing)")
    for what, want, got in (("suffixLength values reached", set(range(1, 7)), st["sl"]),
                            ("suffixLength at which the first-level +2 adjustment fired", {0, 1}, st["adjust"]),
                            ("(TotalCoeff > 10, TrailingOnes < 3, initial suffixLength)",
                             {(True, True, 1), (True, False, 0), (False, True, 0)}, st["init"])):
        if not want <= got:
            problems.append(f"the synthetic stream never exercises {what}: missing {sorted(want - got, key=str)}")
    return problems, f"{len(want_tz)} total_zeros entries, {len(want_run)} run_before entries, {len(want_level)} " \
                     f"(suffixLength, level_prefix) combinations (prefix 0..16, suffixLength 0..6)"


def pixel_diff(exp, got):
    """First differing sample of two 384-byte macroblock images -> text."""
    for i in range(384):
        if exp[i] != got[i]:
            if i < 256:
                where = f"Y x={i % 16} y={i // 16}"
            else:
                j = (i - 256) % 64
                where = f"{'Cb' if i < 320 else 'Cr'} x={j % 8} y={j // 8}"
            n = sum(1 for k in range(384) if exp[k] != got[k])
            return f"{where} expected {exp[i]} got {got[i]} ({n} samples differ)"
    return ""


def ffmpeg_synth_checks(path, manifest, td):
    """ffmpeg decodes the synthetic stream without errors, its QP map equals the manifest's QP_Y, and its pixels equal the
    mini reconstruction of the manifest's arrays.  -> (problems, informative sample fraction)"""
    problems, frames = decode_synth(path, manifest, td)
    if problems:
        return problems, 0
    wm, hm = manifest["width_mbs"], manifest["height_mbs"]
    pics = manifest["pictures"]
    grid = ref.ffmpeg_qp_grid(path, td, dims=(wm, hm), count=len(pics))
    if grid is None or len(grid) < len(pics):
        problems.append("ffmpeg's QP map was unusable for the synthetic stream")
    else:
        for pi, pic in enumerate(pics):
            for s in pic["slices"]:
                my, mx = divmod(s["first"], wm)
                if grid[pi]["rows"][my][mx] != s["mb"]["qp"]:
                    problems.append(f"picture {pi} addr {s['first']}: ffmpeg QP {grid[pi]['rows'][my][mx]}, generator {s['mb']['qp']}")
    inf = tot = 0
    for pi, pic in enumerate(pics):
        for s in pic["slices"]:
            e = s["mb"]
            arrays = {(b["kind"], b["idx"]): b["coeffs"] for b in e["blocks"]}
            y, cb, cr, range_ok, _ = minirecon.reconstruct(e["kind"], e["qp"], arrays, pic["offsets"])
            exp = minirecon.flat_bytes(y, cb, cr)
            got = mb_pixels(frames[pi], wm, hm, s["first"])
            inf += sum(1 for v in exp if 0 < v < 255)
            tot += 384
            if not range_ok:
                problems.append(f"picture {pi} addr {s['first']}: generator produced values outside the 16-bit range")
            if exp != got:
                problems.append(f"picture {pi} addr {s['first']} ({e['kind']}, qp {e['qp']}): ffmpeg's pixels differ from the "
                                f"reconstruction of the generator's coefficients: {pixel_diff(exp, got)}")
    return problems, (inf / tot if tot else 0)


# ---------------------------------------------------------------- real streams: first macroblock of every slice
def check_first_mb_pixels(path, infos, results, td):
    """For every slice whose first macroblock has no neighbours (always true), deblocking is off and the scaling lists are
    flat: chroma (all macroblock types except I_PCM), Intra16x16 luma, and luma block 0 of an I_NxN macroblock without the
    8x8 transform are predicted from nothing (128 / DC), so ffmpeg's pixels must equal minirecon(coefficients)."""
    live = [r for r in results if r.status != "skipped"]
    if not live:
        return [], "first-macroblock pixel check skipped: no parsed slices"
    if not all(i.slice_type == 2 for i in infos):
        return [], "first-macroblock pixel check skipped: the stream has P/B slices (output order differs from decode order)"
    sps0 = infos[0].sps
    if any(i.sps.get("frame_cropping_flag", 0) for i in infos):
        return [], "first-macroblock pixel check skipped: the stream uses cropping"
    wm = infos[0].pic_width_in_mbs
    hm = infos[0].pic_size_in_mbs // wm
    if not all(i.pic_width_in_mbs == wm and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos):
        return [], "first-macroblock pixel check skipped: pictures of different sizes"
    npics = infos[-1].picture + 1
    yuv = os.path.join(td, "first_mb.yuv")
    ffref.reference_yuv(path, yuv)
    fs = wm * hm * 384
    data = open(yuv, "rb").read() if os.path.exists(yuv) else b""
    if len(data) != fs * npics:
        return [], f"first-macroblock pixel check skipped: ffmpeg gave {len(data) / fs:.2f} frames for {npics} pictures"
    problems, n_i16, n_nxn, n_ch = [], 0, 0, 0
    for r in live:
        i = r.info
        if (i.hdr.get("disable_deblocking_filter_idc", 0) != 1 or i.sps.get("seq_scaling_matrix_present_flag", 0)
                or i.pps.get("pic_scaling_matrix_present_flag", 0) or not r.mbs or r.mbs[0].pcm is not None):
            continue
        rec = r.mbs[0]
        off1 = i.pps.get("chroma_qp_index_offset", 0)
        off2 = i.pps.get("second_chroma_qp_index_offset", off1)
        kind = "i16" if rec.f["name"].startswith("I_16x16") else "nxn"
        arrays = {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}
        y, cb, cr, _, _ = minirecon.reconstruct(kind, rec.f["qp"], arrays, (off1, off2))
        exp = minirecon.flat_bytes(y, cb, cr)
        got = mb_pixels(data[i.picture * fs:(i.picture + 1) * fs], wm, hm, rec.addr)
        regions = [("chroma", range(256, 384))]
        if kind == "i16":
            regions.append(("Intra16x16 luma", range(256)))
        elif rec.f["t8x8"] != 1:
            regions.append(("I_NxN luma block 0", [yy * 16 + xx for yy in range(4) for xx in range(4)]))
        for name, idxs in regions:
            n_ch += name == "chroma"
            n_i16 += name == "Intra16x16 luma"
            n_nxn += name == "I_NxN luma block 0"
            bad = [k for k in idxs if exp[k] != got[k]]
            if bad:
                k = bad[0]
                problems.append(f"slice {i.index} addr {rec.addr} ({rec.f['name']}, qp {rec.f['qp']}) {name}: sample {k} "
                                f"expected {exp[k]} (from the reference's coefficients), ffmpeg {got[k]}; {len(bad)} samples differ")
    if n_ch == 0:
        return problems, "first-macroblock pixel check: no slice qualified (deblocking on, scaling lists, I_PCM first macroblock ...)"
    return problems, (f"first-macroblock pixel check: coefficients of {n_ch} slice-first macroblocks reproduce ffmpeg's pixels "
                      f"({n_i16} Intra16x16 luma, {n_nxn} I_NxN luma block 0, {n_ch} chroma)")


def reference_trust(path, infos, results, td, quiet=False):
    say = (lambda *a: None) if quiet else (lambda *a: print(" ", *a))
    problems, msg = check_reference_vs_ffmpeg(path, infos, results, td)
    say(msg)
    p2, msg2 = check_reference_vs_ffmpeg_qp(path, infos, results, td)
    problems += p2
    say(msg2)
    p3, msg3 = check_slices_complete(results)
    problems += p3
    say(msg3)
    p4, msg4 = check_first_mb_pixels(path, infos, results, td)
    problems += p4
    say(msg4 if not p4 else "first-macroblock pixel check FAILED")
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        manifest = json.load(open(mp))
        p5, msg5 = check_reference_vs_manifest(results, manifest)
        p6, msg6 = coverage_problems(path, td)
        p7, frac = ffmpeg_synth_checks(path, manifest, td)
        problems += p5 + p6 + p7
        say(msg5 if not p5 else "synthetic-stream manifest check FAILED")
        say("coverage: " + msg6 if not p6 else "coverage check FAILED")
        say(f"ffmpeg decodes the synthetic stream without errors and every macroblock's pixels equal the mini reconstruction "
            f"of the generator's coefficients ({frac:.0%} of the samples are not clipped)" if not p7
            else "ffmpeg checks of the synthetic stream FAILED")
    return problems


def sensitivity(manifest):
    """Share of non-zero coefficients of the synthetic stream whose change by +1 changes the macroblock's pixels (how much
    the pixel comparison can tell about each value)."""
    n = hit = 0
    for pic in manifest["pictures"]:
        for s in pic["slices"]:
            e = s["mb"]
            arr = {(b["kind"], b["idx"]): list(b["coeffs"]) for b in e["blocks"]}
            base = minirecon.flat_bytes(*minirecon.reconstruct(e["kind"], e["qp"], arr, pic["offsets"])[:3])
            for a in arr.values():
                for k, v in enumerate(a):
                    if v:
                        a[k] = v + 1
                        n += 1
                        hit += minirecon.flat_bytes(*minirecon.reconstruct(e["kind"], e["qp"], arr, pic["offsets"])[:3]) != base
                        a[k] = v
    return n, hit


# ================================================================ driver
def load_reference(path, td):
    infos = ref.load_stream(path, td)
    if not infos:
        die("input problem: no slice NAL units found in the input")
    return infos, [ref.parse_slice(i, LEVEL) for i in infos]


def run_check(path, dump):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input file not found: {path}")
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        problems = reference_trust(path, infos, results, td)
        tp, tmsg = check_tables()
        problems += tp
        print(" ", "Tables 9-5, 9-7 .. 9-10 structure OK; " + tmsg if not tp else "code table checks FAILED")
        if problems:
            print("HARNESS PROBLEM (not your fault): the reference parser disagrees with an independent check:")
            for p in problems[:10]:
                print("   ", p)
            sys.exit(2)
        exp_slices, exp_hist = expected_structs(results)
    act_slices, act_hist = parse_reader(dump)
    rep = Report("Levels, total_zeros and run_before")
    compare(rep, exp_slices, exp_hist, act_slices, act_hist)
    mbs = [kv for s in exp_slices for _, kv in s["mbs"]]
    ncoef = sum(len(re.findall(r"=-?\d+", kv["coef"])) for kv in mbs if kv["coef"] != "-")
    cnt = lambda st: sum(1 for s in exp_slices if s["end"]["status"] == st)  # noqa: E731
    print(f"  reference: {len(infos)} slices, {len(mbs)} macroblocks, {ncoef} non-zero coefficients, "
          f"{cnt('complete')} slices parsed to their stop bit, {cnt('skipped')} skipped")
    rep.finish()


def write_expected(path, out):
    with tempfile.TemporaryDirectory() as td:
        infos, results = load_reference(path, td)
        open(out, "w").write(ref.render_dump(results, LEVEL))


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
    data, man = levgen.make_stream()
    with open(os.path.join(outdir, "levels_synth.264"), "wb") as f:
        f.write(data)
    with open(os.path.join(outdir, "levels_synth.manifest.json"), "w") as f:
        json.dump(man, f)
    nb = sum(len(s["mb"]["blocks"]) for p in man["pictures"] for s in p["slices"])
    n, hit = sensitivity(man)
    print(f"levels_synth.264: synthetic Annex B stream (64x48, CAVLC, High profile): {len(man['pictures'])} IDR pictures, "
          f"{len(man['pictures']) * levgen.N_MBS} slices of one macroblock each, {nb} residual blocks with known coefficient arrays "
          f"({n} non-zero coefficients, {hit / n:.0%} of them move the decoded pixels when changed by 1). Covers every "
          f"(TotalCoeff, total_zeros) and (zerosLeft, run_before) pair, level_prefix 0..16 and suffixLength 0..6 with its "
          f"thresholds; the manifest lists the arrays.")
    testsrc = "testsrc2=size=176x144:rate=25"
    noise = ("color=c=gray:size=176x144:rate=25,format=yuv420p,geq=lum='random(1)*255':cb='128+random(1)*40':cr=128")
    x264_clip(os.path.join(outdir, "real_1mb_slices.ts"), testsrc,
              ["-profile:v", "baseline", "-qp", "18", "-x264-params", "keyint=1:no-deblock=1:slice-max-mbs=1"], frames=3)
    print("real_1mb_slices.ts: x264 Baseline, every macroblock is its own slice, deblocking off: every macroblock is the first of "
          "its slice, so all its chroma and Intra16x16 pixels (and I_NxN block 0) are checked against your coefficients")
    x264_clip(os.path.join(outdir, "real_noise_1mb.ts"), noise,
              ["-profile:v", "main", "-qp", "20", "-x264-params", "keyint=1:cabac=0:no-deblock=1:slice-max-mbs=1"], frames=2)
    print("real_noise_1mb.ts: the same on noise at QP 20: dense blocks, large levels")
    x264_clip(os.path.join(outdir, "real_high_cavlc.ts"), testsrc,
              ["-profile:v", "high", "-crf", "20", "-x264-params", "keyint=1:cabac=0:8x8dct=1:slices=3"])
    print("real_high_cavlc.ts: x264 High, CAVLC, 8x8dct: I_NxN with transform_size_8x8_flag = 1 (each 8x8 block is four "
          "interleaved 4x4 blocks), 3 slices per picture")
    x264_clip(os.path.join(outdir, "real_lowqp.ts"), testsrc,
              ["-profile:v", "baseline", "-qp", "4", "-x264-params", "keyint=1:no-deblock=1"])
    print("real_lowqp.ts: x264 Baseline at QP 4: large levels (level_prefix escapes) on real data")
    x264_clip(os.path.join(outdir, "real_noise.ts"), noise,
              ["-profile:v", "main", "-qp", "12", "-x264-params", "keyint=1:cabac=0:slices=2"])
    print("real_noise.ts: x264 Main CAVLC on noise at QP 12: the densest blocks (TotalCoeff 16, suffixLength up to 6)")
    print("Also useful (from common.testfiles): T1, T2 (first macroblock of each slice is pixel-checked), T3, T4, default, "
          "T10_full (CABAC: every slice skipped)")


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
        inputs = [testfiles.get(n) for n in ("T1", "T2", "T4", "default", "T10_full")]
        inputs += [v(n) for n in ("levels_synth.264", "real_1mb_slices.ts", "real_noise_1mb.ts", "real_high_cavlc.ts",
                                  "real_lowqp.ts", "real_noise.ts")]
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

        synth = v("levels_synth.264")
        lines = open(good["levels_synth.264"]).read().splitlines()

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

        def drop_key(prefix, key):
            def edit(ls):
                i = next(k for k, l in enumerate(ls) if l.startswith(prefix) and f" {key}=" in l)
                ls[i] = re.sub(rf" {key}=\S+", "", ls[i])
                return ls
            return edit
        has = lambda kv: kv["coef"] != "-"  # noqa: E731

        def edit_first(fn):
            def g(s):
                bl = s.split(";")
                for i, b in enumerate(bl):
                    new = fn(b)
                    if new != b:
                        bl[i] = new
                        break
                return ";".join(bl)
            return g

        def val_plus(b):                      # one coefficient value changed by 1
            name, lst = b.split(":")
            if lst == "-":
                return b
            first = lst.split(",")
            p, v = first[0].split("=")
            first[0] = f"{p}={int(v) + (1 if int(v) > 0 else -1)}"
            return f"{name}:" + ",".join(first)

        def sign_flip(b):
            name, lst = b.split(":")
            if lst == "-":
                return b
            first = lst.split(",")
            p, v = first[-1].split("=")
            first[-1] = f"{p}={-int(v)}"
            return f"{name}:" + ",".join(first)

        def shift_pos(b):                     # a coefficient one scan position too low (the AC-offset mistake)
            name, lst = b.split(":")
            if lst == "-" or not name.endswith(("0", "1", "2", "3")):
                return b
            items = lst.split(",")
            p, v = items[0].split("=")
            if int(p) < 1:
                return b
            items[0] = f"{int(p) - 1}={v}"
            return f"{name}:" + ",".join(items)

        def dropped_block(s):
            bl = s.split(";")
            return ";".join(bl[1:]) if len(bl) > 1 else s

        def swap_blocks(s):
            bl = s.split(";")
            if len(bl) > 2:
                bl[0], bl[1] = bl[1], bl[0]
            return ";".join(bl)
        multi = lambda kv: has(kv) and len(kv["coef"].split(";")) > 2 and "=" in kv["coef"].split(";")[0]  # noqa: E731
        variant("one coefficient value off by one", kv_edit(multi, "coef", edit_first(val_plus)))
        variant("one coefficient sign flipped", kv_edit(multi, "coef", edit_first(sign_flip)))
        variant("a coefficient at scan position - 1", kv_edit(multi, "coef", edit_first(shift_pos)))
        variant("a block missing from the coef list", kv_edit(multi, "coef", dropped_block))
        variant("two blocks swapped in the coef list", kv_edit(multi, "coef", swap_blocks))
        variant("coef key missing", drop_key("MB ", "coef"))
        variant("coef empty for a macroblock that has coefficients", kv_edit(has, "coef", lambda s: "-"))
        variant("end_bit key missing", drop_key("END ", "end_bit"))

        def wrong_end(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("END ") and "status=complete" in l)
            m = re.search(r"end_bit=(\d+)", ls[i])
            ls[i] = ls[i].replace(m.group(0), f"end_bit={int(m.group(1)) + 1}")
            return ls
        variant("end_bit one too large", wrong_end)

        def wrong_status(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("END ") and "status=complete" in l)
            ls[i] = ls[i].replace("complete", "stopped")
            return ls
        variant("complete slice reported as stopped", wrong_status)
        tl = open(good["T1.ts"]).read().splitlines()
        variant("T1: a coefficient of a real macroblock changed", kv_edit(multi, "coef", edit_first(val_plus)), testfiles.get("T1"), tl)
        # classic bugs of the level / total_zeros / run_before rules: run the reference with the bug and require the harness to fail
        mutants = {"lvl_no_adjust": "first level after fewer than 3 trailing ones: +2 adjustment forgotten",
                   "lvl_sl_init": "suffixLength starts at 0 even for TotalCoeff > 10 with TrailingOnes < 3",
                   "lvl_inc_ge": "suffixLength incremented at |level| >= threshold instead of >",
                   "lvl_no_inc": "suffixLength never incremented beyond 1",
                   "lvl_p14": "level_prefix 14 with suffixLength 0 has no 4-bit suffix",
                   "lvl_esc_sl0": "escape (prefix 15) with suffixLength 0: the +15 forgotten",
                   "lvl_esc16": "level_prefix >= 16: the (1 << (prefix - 3)) - 4096 term forgotten",
                   "t1_sign": "trailing-ones sign inverted",
                   "run_min6": "run_before table index min(zerosLeft, 6) instead of 7",
                   "tz_dc_table": "chroma DC total_zeros read with the 4x4 table",
                   "place_reversed": "coefficients placed in reverse order",
                   "no_ac_offset": "15-coefficient blocks stored from scan position 0 instead of 1"}
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
        for name in mutants:
            if name == "no_ac_offset":                  # only changes how the dump is printed, not what is parsed
                continue
            cavlc_resid.MUTANT = name
            try:
                infos, results = load_reference(synth, td)
            finally:
                cavlc_resid.MUTANT = None
            prob = reference_trust(synth, infos, results, td, quiet=True)
            print(f"   a reference with bug {name} is detected:", bool(prob), "-", (prob or [""])[0][:90])
            ok_all &= bool(prob)
        # the same on a REAL stream: value-only bugs (no desynchronisation) are caught by the first-macroblock pixel check
        real1 = v("real_1mb_slices.ts")
        for name in ("lvl_no_adjust", "t1_sign", "place_reversed", "lvl_inc_ge"):
            cavlc_resid.MUTANT = name
            try:
                infos, results = load_reference(real1, td)
            finally:
                cavlc_resid.MUTANT = None
            prob = reference_trust(real1, infos, results, td, quiet=True)
            print(f"   a reference with bug {name} is detected on real_1mb_slices.ts:", bool(prob), "-", (prob or [""])[0][:90])
            ok_all &= bool(prob)
        # table typos in the reference
        for label, tab, k1, k2 in (("total_zeros", "tz", (3, 2), (3, 3)), ("run_before", "rb", (7, 2), (7, 3))):
            t = tables.TOTAL_ZEROS[k1[0]] if tab == "tz" else tables.RUN_BEFORE[k1[0]]
            saved = dict(t)
            t[k1[1]], t[k2[1]] = saved[k2[1]], saved[k1[1]]
            cavlc_resid._DEC.clear()
            try:
                infos, results = load_reference(synth, td)
                prob = reference_trust(synth, infos, results, td, quiet=True)
            finally:
                t.clear()
                t.update(saved)
                cavlc_resid._DEC.clear()
            print(f"   a typo in the reference's {label} table is detected:", bool(prob), "-", (prob or [""])[0][:90])
            ok_all &= bool(prob)
        # generators / mini reconstruction with a wrong rule must be caught by the ffmpeg checks
        manifest_of = {}
        for bug in ("no_adjust", "inc_ge"):
            print(f"-- a generator with the writer bug {bug} must be caught by ffmpeg")
            levgen.BUG = bug
            bad_dir = os.path.join(td, "badgen")
            os.makedirs(bad_dir, exist_ok=True)
            try:
                bad_data, bad_man = levgen.make_stream()
            finally:
                levgen.BUG = None
            bad_path = os.path.join(bad_dir, "bad.264")
            open(bad_path, "wb").write(bad_data)
            prob, _ = ffmpeg_synth_checks(bad_path, bad_man, bad_dir)
            print("   detected:", bool(prob), "-", (prob or [""])[0][:100])
            ok_all &= bool(prob)
        for label, tab, k1, k2 in (("total_zeros", levgen.TZ, 4, (2, 3)), ("run_before", levgen.RB, 5, (1, 2))):
            print(f"-- a generator with two {label} entries swapped must be caught by ffmpeg")
            saved = dict(tab[k1])
            tab[k1][k2[0]], tab[k1][k2[1]] = saved[k2[1]], saved[k2[0]]
            bad_dir = os.path.join(td, "badgen")
            try:
                bad_data, bad_man = levgen.make_stream()
            finally:
                tab[k1].clear()
                tab[k1].update(saved)
            bad_path = os.path.join(bad_dir, "bad2.264")
            open(bad_path, "wb").write(bad_data)
            prob, _ = ffmpeg_synth_checks(bad_path, bad_man, bad_dir)
            print("   detected:", bool(prob), "-", (prob or [""])[0][:100])
            ok_all &= bool(prob)
        print("-- a mini reconstruction with a wrong zig-zag entry must be caught by the pixel comparison")
        saved_zz = list(minirecon.ZIGZAG)
        minirecon.ZIGZAG[7], minirecon.ZIGZAG[8] = saved_zz[8], saved_zz[7]
        try:
            man = json.load(open(manifest_path(synth)))
            prob, _ = ffmpeg_synth_checks(synth, man, td)
        finally:
            minirecon.ZIGZAG[:] = saved_zz
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
