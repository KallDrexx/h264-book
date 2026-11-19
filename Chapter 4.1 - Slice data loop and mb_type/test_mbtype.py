#!/usr/bin/env python3
"""Harness for Chapter 4.1 (slice_data loop and mb_type).

    python3 test_mbtype.py <input.ts|input.264> <mb_dump.txt>
    python3 test_mbtype.py --make-variants <out_dir>        # write extra test inputs (see Readme)
    python3 test_mbtype.py --print-expected <input> <out.txt>   # write the expected dump, to diff against yours
    python3 test_mbtype.py --self-test

<mb_dump.txt> is your program's output (grammar in the Readme, section 5):

    SLICE <i> type=<I|P|B|SP|SI> first_mb=<n> pic_size_in_mbs=<n> header_end_bit=<n>
    MB <i> <addr> mb_type=<raw> name=<NAME> i16_mode=<0-3|-> chroma_cbp=<0-2|-> luma_cbp=<0|15|-> bit=<n>
    PCM <i> <addr> align_bits=<0-7> sum_y=<n> sum_cb=<n> sum_cr=<n>
    END <i> mbs=<n> status=<complete|stopped|skipped|error>
    HIST <raw>:<count> ...                                   (one line, at the very end)

Truth comes from common/cavlc_ref.py, a reference parser written for this book (there is no JM
here).  Before your output is compared with it, the harness checks the reference itself:
  * against ffmpeg's own macroblock map (`-debug mb_type`): the class of every macroblock the
    reference printed (I4x4 / I16x16 / PCM) must agree with ffmpeg's decoder;
  * for the synthetic streams from --make-variants, against the manifest written by the generator's
    bit writer, and against ffmpeg's decoded pixels (I_PCM samples must come out unchanged and the
    stream must decode without a single ffmpeg error message).
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
import mbgen  # noqa: E402

KEYS_SLICE = ["type", "first_mb", "pic_size_in_mbs", "header_end_bit"]
KEYS_MB = ref.MbRecord.CORE_KEYS            # mb_type name i16_mode chroma_cbp luma_cbp bit
KEYS_PCM = ["align_bits", "sum_y", "sum_cb", "sum_cr"]
KEYS_END = ["mbs", "status"]
STATUSES = ("complete", "stopped", "skipped", "error")
LEVEL = 1                                   # cavlc_ref parsing level of this chapter (see common/cavlc_ref.py)


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ================================================================ reader output parsing
def parse_kv(tokens, lineno, line):
    d = {}
    for t in tokens:
        if "=" not in t:
            die(f"malformed reader output, line {lineno}: expected key=value, found {t!r}: {line!r}")
        k, v = t.split("=", 1)
        d[k] = v
    return d


def parse_reader(path):
    """-> (slices, hist).  slices: ordered list of dict(index, slice=kv, mbs=[(addr, kv)], pcm={addr: kv}, end=kv)."""
    try:
        text = open(path, "r").read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    slices, cur, hist, seen_hist = [], None, None, False
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        kind = tok[0]
        if seen_hist:
            die(f"malformed reader output, line {no}: nothing may follow the HIST line")

        def need_int(s, what):
            if not re.fullmatch(r"\d+", s):
                die(f"malformed reader output, line {no}: {what} must be a non-negative integer, found {s!r}: {line!r}")
            return int(s)
        if kind == "SLICE":
            if len(tok) < 2:
                die(f"malformed reader output, line {no}: SLICE needs an index: {line!r}")
            cur = dict(index=need_int(tok[1], "slice index"), slice=parse_kv(tok[2:], no, line),
                       mbs=[], pcm={}, end=None, line=no)
            slices.append(cur)
        elif kind in ("MB", "PCM", "END"):
            if cur is None or cur["end"] is not None:
                die(f"malformed reader output, line {no}: {kind} line outside a SLICE ... END block: {line!r}")
            if need_int(tok[1], "slice index") != cur["index"]:
                die(f"malformed reader output, line {no}: slice index {tok[1]} inside the block of slice {cur['index']}")
            if kind == "END":
                cur["end"] = parse_kv(tok[2:], no, line)
            else:
                if len(tok) < 3:
                    die(f"malformed reader output, line {no}: {kind} needs a macroblock address: {line!r}")
                addr = need_int(tok[2], "macroblock address")
                kv = parse_kv(tok[3:], no, line)
                if kind == "MB":
                    cur["mbs"].append((addr, kv))
                else:
                    cur["pcm"][addr] = kv
        elif kind == "HIST":
            hist = {}
            for t in tok[1:]:
                m = re.fullmatch(r"(\d+):(\d+)", t)
                if not m:
                    die(f"malformed reader output, line {no}: HIST entries look like 3:17, found {t!r}")
                hist[int(m.group(1))] = int(m.group(2))
            seen_hist = True
        else:
            die(f"malformed reader output, line {no}: unknown line type {kind!r} (allowed: SLICE MB PCM END HIST, "
                f"or # comments)")
    if cur is not None and cur["end"] is None:
        die(f"malformed reader output: the block of slice {cur['index']} has no END line")
    if not seen_hist:
        die("malformed reader output: no HIST line at the end")
    return slices, hist


def expected_structs(results):
    """Expected dump of the reference, in the same structure parse_reader returns."""
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
                          f"(expected line: mb_type={ekv['mb_type']} {ekv['name']})")
        rep.check(sorted(a["pcm"]) == sorted(e["pcm"]),
                  f"SLICE {k}: PCM lines for macroblocks {sorted(a['pcm'])}, expected {sorted(e['pcm'])} "
                  f"(one PCM line per I_PCM macroblock, none otherwise)")
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
    rep.check(act_hist == own, f"HIST does not match your own MB lines (mb_type: your MB-line count vs HIST): "
                               f"{hist_diff(own, act_hist)}")
    rep.check(act_hist == exp_hist, f"HIST differs from the expected one (mb_type: expected vs yours): "
                                    f"{hist_diff(exp_hist, act_hist)}")


def hist_diff(a, b):
    """Only the mb_type values whose counts differ, e.g. '7: 3 vs 2, 30: 0 vs 1'."""
    keys = sorted(k for k in set(a) | set(b) if a.get(k, 0) != b.get(k, 0))
    return ", ".join(f"{k}: {a.get(k, 0)} vs {b.get(k, 0)}" for k in keys[:10]) + (" ..." if len(keys) > 10 else "")


# ================================================================ checking the reference itself
def pic_type_letter(info):
    return {0: "P", 1: "B", 2: "I"}.get(info.slice_type, "?")


def check_reference_vs_ffmpeg(path, infos, results, td):
    """-> (message list of problems, summary text)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    same_size = all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos)
    grid = ref.ffmpeg_mb_grid(path, td, dims=(w0, infos[0].pic_size_in_mbs // w0) if same_size else None,
                              count=npics)
    if grid is None:
        return [], "ffmpeg cross-check skipped: ffmpeg's log was garbled on every try"
    if len(grid) < npics:
        return [], f"ffmpeg cross-check skipped: ffmpeg showed {len(grid)} pictures, stream has {npics}"
    grid = grid[-npics:]
    first_type = {}
    for i in infos:
        first_type.setdefault(i.picture, pic_type_letter(i))
    types_dec = "".join(first_type[p] for p in range(npics))
    types_out = "".join(g["type"] for g in grid)
    usable = None                                   # picture indices we may compare
    note = ""
    if types_dec == types_out:
        usable = set(range(npics))
    elif types_dec[:1] == "I" and types_out[:1] == "I":
        usable = {0}
        note = " (pictures are reordered: only the first picture could be matched safely)"
    else:
        return [], "ffmpeg cross-check skipped: could not match ffmpeg's pictures to slices"
    problems, n = [], 0
    for r in results:
        p = r.info.picture
        if p not in usable:
            continue
        rows = grid[p]["rows"]
        w = r.info.pic_width_in_mbs
        for rec in r.mbs:
            y, x = divmod(rec.addr, w)
            if y >= len(rows) or x >= len(rows[y]):
                problems.append(f"slice {r.info.index} addr {rec.addr}: ffmpeg's macroblock map has no such macroblock")
                continue
            ch = rows[y][x]
            cls = ref.CLASS_OF_CHAR.get(ch, f"inter or other class {ch!r}")
            mine = rec.f["name"]
            mine_cls = "I_16x16" if mine.startswith("I_16x16") else mine
            n += 1
            if cls != mine_cls:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference says {mine}, ffmpeg says {cls}")
    return problems, f"ffmpeg cross-check: {n} macroblock classes in {len(usable)} picture(s) agree{note}"


def expected_prefix(mbs):
    """The 4.1 stop rule applied to the generator's list of macroblocks -> (parsed entries, status)."""
    out, seen_pcm = [], False
    for m in mbs:
        out.append(m)
        if m["kind"] == "pcm":
            seen_pcm = True
            continue
        if seen_pcm:
            return out, "stopped"
        if m["kind"] == "i16" and m["cc"] == 0 and m["lf"] == 0:
            continue
        return out, "stopped"
    return out, "complete"


def manifest_path(path):
    base = path[:-4] if path.lower().endswith(".264") else path
    return base + ".manifest.json"


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
        exp, status = expected_prefix(m["mbs"])
        if len(r.mbs) != len(exp) or r.status != status:
            problems.append(f"slice {k}: reference parsed {len(r.mbs)} macroblocks / {r.status}, generator says "
                            f"{len(exp)} / {status}")
            continue
        for rec, e in zip(r.mbs, exp):
            nmb += 1
            got = (rec.addr, rec.f["mb_type"], rec.f["name"], rec.f["bit"])
            want = (e["addr"], e["mb_type"], e["name"], e["bit"])
            if got != want:
                problems.append(f"slice {k} addr {e['addr']}: reference {got}, generator {want}")
            if e["kind"] == "i16" and (rec.f["i16_mode"], rec.f["chroma_cbp"], rec.f["luma_cbp"]) != \
                    (e["pred"], e["cc"], 15 * e["lf"]):
                problems.append(f"slice {k} addr {e['addr']}: Intra16x16 decomposition differs from generator")
            if e["kind"] == "pcm":
                s = bytes.fromhex(e["samples"])
                want_p = (sum(s[:256]), sum(s[256:320]), sum(s[320:]))
                p = rec.pcm
                if p is None or (p["sum_y"], p["sum_cb"], p["sum_cr"]) != want_p or p["samples"] != s:
                    problems.append(f"slice {k} addr {e['addr']}: PCM samples differ from generator")
    # ffmpeg must decode the stream cleanly, and PCM samples must come out unchanged
    yuv = os.path.join(td, "synth.yuv")
    p = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-i", path, "-fps_mode", "passthrough",
                   "-f", "rawvideo", "-pix_fmt", "yuv420p", yuv], timeout=120)
    err = p.stderr.decode(errors="replace").strip()
    if err or p.returncode != 0:
        problems.append("ffmpeg reports errors decoding the synthetic stream: " + err[:300])
    else:
        w, h = manifest["width_mbs"] * 16, manifest["height_mbs"] * 16
        fs = w * h * 3 // 2
        data = open(yuv, "rb").read()
        if len(data) != fs * len(manifest["pictures"]):
            problems.append(f"ffmpeg produced {len(data) // fs} frames, expected {len(manifest['pictures'])}")
        else:
            npcm = 0
            for pi, pic in enumerate(manifest["pictures"]):
                fr = data[pi * fs:(pi + 1) * fs]
                for s in pic:
                    for e in s["mbs"]:
                        if e["kind"] != "pcm":
                            continue
                        npcm += 1
                        smp = bytes.fromhex(e["samples"])
                        my, mx = divmod(e["addr"], manifest["width_mbs"])
                        got = bytearray()
                        for yy in range(16):
                            o = (my * 16 + yy) * w + mx * 16
                            got += fr[o:o + 16]
                        for plane in range(2):
                            base = w * h + plane * (w // 2) * (h // 2)
                            for yy in range(8):
                                o = base + (my * 8 + yy) * (w // 2) + mx * 8
                                got += fr[o:o + 8]
                        if bytes(got) != smp:
                            problems.append(f"picture {pi} addr {e['addr']}: ffmpeg's decoded pixels differ from the I_PCM samples")
    return problems, f"synthetic-stream checks: {nmb} macroblocks match the generator manifest; ffmpeg decodes the stream " \
                     f"without errors and every I_PCM macroblock's pixels equal its raw samples"


# ================================================================ driver
def load_reference(path, td):
    infos = ref.load_stream(path, td)
    if not infos:
        die("input problem: no slice NAL units found in the input")
    results = [ref.parse_slice(i, LEVEL) for i in infos]
    return infos, results


def reference_trust(path, infos, results, td, quiet=False):
    """Run both reference checks; returns list of problems (empty = reference trustworthy)."""
    problems, msg = check_reference_vs_ffmpeg(path, infos, results, td)
    if not quiet:
        print(" ", msg)
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        manifest = json.load(open(mp))
        p2, msg2 = check_reference_vs_manifest(path, results, td, manifest)
        problems += p2
        if not quiet:
            print(" ", msg2 if not p2 else "synthetic-stream checks FAILED")
    return problems


def run_check(path, dump, title="Slice data loop and mb_type"):
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
    nmb = sum(len(s["mbs"]) for s in exp_slices)
    print(f"  reference: {len(infos)} slices, {nmb} macroblocks read, "
          f"{sum(1 for s in exp_slices if s['end']['status'] == 'complete')} slices parsed completely, "
          f"{sum(1 for s in exp_slices if s['end']['status'] == 'stopped')} stopped, "
          f"{sum(1 for s in exp_slices if s['end']['status'] == 'skipped')} skipped")
    rep.finish()


# ================================================================ test inputs / variants
def x264_clip(out, vf, opts, qp=26, size="352x288", frames=5):
    src = f"color=c=gray:size={size}:rate=25,format=yuv420p,{vf}" if vf else f"testsrc2=size={size}:rate=25"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-profile:v", "baseline", "-qp", str(qp), "-x264-params", opts, "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die("ffmpeg failed: " + r.stderr.decode()[-500:])


def make_variants(outdir):
    ffref.require_tools()
    os.makedirs(outdir, exist_ok=True)
    data, man = mbgen.make_stream()
    with open(os.path.join(outdir, "mb_synth.264"), "wb") as f:
        f.write(data)
    with open(os.path.join(outdir, "mb_synth.manifest.json"), "w") as f:
        json.dump(man, f)
    nsl = sum(len(p) for p in man["pictures"])
    print(f"mb_synth.264: synthetic Annex B stream (96x80, Baseline, CAVLC): {len(man['pictures'])} IDR pictures, "
          f"{nsl} I slices.  26 'probe' pictures (picture t: zero-residual Intra16x16 macroblocks, then one macroblock of "
          f"mb_type t at address {mbgen.PROBE_ADDR}, then random macroblocks), then zero-residual-only, 'skeletons then "
          f"I_PCM', random and all-I_PCM pictures.  Written to mb_synth.manifest.json as well (harness use).")
    x264_clip(os.path.join(outdir, "mb_flat.ts"), "geq=lum=128:cb=128:cr=128", "keyint=1:partitions=none:no-deblock=1")
    print("mb_flat.ts: x264, flat mid-gray (all samples 128): every macroblock is Intra16x16 with zero residual, so a "
          "4.1 reader parses EVERY slice to its end")
    x264_clip(os.path.join(outdir, "mb_flat_slices.ts"), "geq=lum=128:cb=128:cr=128",
              "keyint=1:partitions=none:no-deblock=1:slices=4")
    print("mb_flat_slices.ts: the same with 4 slices per picture (first_mb_in_slice > 0, several slices to end)")
    print("Also useful (from common.testfiles): T1, T2, T4 (each slice stops at its first macroblock), default "
          "(P/B slices: skipped), T10_full (CABAC: skipped)")


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
        inputs = [testfiles.get(n) for n in ("T1", "T2", "T4", "default", "T10_full")]
        inputs += [os.path.join(td, "v", n) for n in ("mb_synth.264", "mb_flat.ts", "mb_flat_slices.ts")]
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
        # corruptions of a known-good dump (must FAIL)
        synth = os.path.join(td, "v", "mb_synth.264")
        lines = open(good["mb_synth.264"]).read().splitlines()

        def variant(label, edit):
            nonlocal ok_all
            ls = edit(list(lines))
            p = os.path.join(td, "bad.txt")
            open(p, "w").write("\n".join(ls) + "\n")
            print(f"-- {label} (must FAIL)")
            rc = run_in_subprocess(synth, p)
            print("   exit", rc)
            ok_all &= rc == 1

        def bump_type(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and "mb_type=6 " in l)
            ls[i] = ls[i].replace("mb_type=6 ", "mb_type=7 ")
            return ls

        def drop_pcm(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("PCM "))
            del ls[i]
            return ls

        def wrong_split(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("MB ") and "name=I_16x16_3_2_1" in l)
            ls[i] = ls[i].replace("chroma_cbp=2", "chroma_cbp=1")
            return ls

        def wrong_align(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("PCM ") and "align_bits=3" in l)
            ls[i] = ls[i].replace("align_bits=3", "align_bits=4")
            return ls

        def wrong_status(ls):
            i = next(k for k, l in enumerate(ls) if l.startswith("END ") and "status=complete" in l)
            ls[i] = ls[i].replace("complete", "stopped")
            return ls

        def hist_off(ls):
            ls[-1] = ls[-1] + " 30:1"
            return ls

        variant("mb_type 6 reported as 7", bump_type)
        variant("PCM line missing", drop_pcm)
        variant("chroma_cbp of I_16x16_3_2_1 wrong", wrong_split)
        variant("pcm_alignment bit count wrong", wrong_align)
        variant("complete slice reported as stopped", wrong_status)
        variant("HIST with an extra entry", hist_off)
        # the reference's own cross-checks must be able to fail
        print("-- reference cross-checks must detect a corrupted reference (in-process)")
        infos, results = load_reference(synth, td)
        prob = reference_trust(synth, infos, results, td, quiet=True)
        good_ref = not prob
        print("   unmodified reference passes both cross-checks:", good_ref)
        r0 = next(r for r in results if len(r.mbs) > 3)
        r0.mbs[2].f["name"] = "I_NxN"
        r0.mbs[2].f["mb_type"] = 0
        prob = reference_trust(synth, infos, results, td, quiet=True)
        print("   MB renamed to I_NxN is detected:", bool(prob), "-", (prob or [""])[0][:100])
        ok_all &= good_ref and bool(prob)
        infos, results = load_reference(synth, td)
        r1 = next(r for r in results if any(m.pcm for m in r.mbs))
        m1 = next(m for m in r1.mbs if m.pcm)
        m1.pcm["samples"] = bytes([m1.pcm["samples"][0] ^ 1]) + m1.pcm["samples"][1:]
        prob = reference_trust(synth, infos, results, td, quiet=True)
        print("   one flipped PCM sample is detected:", bool(prob))
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
