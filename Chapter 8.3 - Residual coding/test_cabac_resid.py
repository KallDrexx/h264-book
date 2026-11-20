#!/usr/bin/env python3
"""Harness for Chapter 8.3 (CABAC residual coding: coded_block_flag, significance map, levels, signs).

Usage (grammars: Readme, section 5):
  python3 test_cabac_resid.py INPUT READER_OUTPUT        check your macroblock dump of a CABAC stream (.ts or Annex B .264)
  python3 test_cabac_resid.py --yuv INPUT READER.yuv     optional end-to-end check: your decoded frames vs ffmpeg's (intra-only streams)
  python3 test_cabac_resid.py --print-expected hints INPUT OUT   write the HINT file your reader needs as input (section 5.1)
  python3 test_cabac_resid.py --print-expected dump  INPUT OUT   write the expected dump, to diff against yours
  python3 test_cabac_resid.py --make-variants DIR        write synthetic and x264 test streams with their hints/expected dumps
  python3 test_cabac_resid.py --self-test                verify this harness (known-good passes; every deliberately wrong rule rejected)

Compared per macroblock: mbt, cpred, modes, cbp, qpd, qp, coef (all residual coefficients), eos (and slice type / first_mb /
macroblock count); the first wrong macroblock of every slice is named with the differing fields.  Exit 0 = pass, 1 = your dump
differs, 2 = unusable input / output or a harness problem.

Truth.  JM is not used.  spoiler_reference/resref.py is an independent reference parser that decodes EVERYTHING in a CABAC
slice (including the inter syntax of Chapter 8.4); a parse only counts when end_of_slice_flag = 1 comes at exactly the last
macroblock of the slice and the rbsp_stop_one_bit follows, which is only possible if every coded_block_flag, significance map,
level and sign was read right.  It is also compared with ffmpeg (`-debug mb_type` class / partition shape and `-debug qp`
of every macroblock); synthetic streams made by an independent CABAC encoder (spoiler_reference/resgen.py: random residual of
every ctxBlockCat, huge levels, all coded_block_flag neighbour cases, expected coefficients from the construction) are decoded
by ffmpeg and by the reference; and for intra-only streams without deblocking the coefficients are pushed through a
reconstruction (spoiler_reference/refrecon.py, the arithmetic of Chapters 5.2 - 5.4) whose output must equal ffmpeg's YUV
byte for byte.  The HINT file only carries the engine states that replace the inter syntax of Chapter 8.4 (`cbp` hints).
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
from common import cavlc_ref, ffref, inter_ref, mbdump, testfiles, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import resref  # noqa: E402
import resgen  # noqa: E402
import refrecon  # noqa: E402

FIELDS = ["mbt", "cpred", "modes", "cbp", "qpd", "qp", "coef", "eos"]
FIELD_HELP = {
    "mbt": "mb_type or mb_skip_flag: check the ctxIdxInc of bin 0 (neighbours A/B), the bin order and the contexts of the later bins",
    "cpred": "intra_chroma_pred_mode: ctxIdxInc of bin 0 (neighbour A/B conditions), bins 1-2 use ctxIdx 64+3",
    "modes": "prev_intra4x4_pred_mode_flag / rem_intra4x4_pred_mode: contexts 68 and 69, rem is 3 bins LEAST significant bit first",
    "cbp": "coded_block_pattern: luma prefix ctxIdxInc (condTermFlagA + 2 * condTermFlagB per 8x8 block, neighbours inside "
           "the macroblock use the bins just decoded) or the chroma suffix (bin 1 adds 4)",
    "qpd": "mb_qp_delta: unary code, bin 0 context depends on the PREVIOUS macroblock, mapping of the code number to a signed value",
    "qp": "QP_Y: wrap modulo 52, carried across skipped / I_PCM / cbp=0 macroblocks, reset to SliceQPY at each slice start",
    "coef": "residual: coded_block_flag ctxIdxInc (neighbour rules, I_PCM / unavailable / not-coded cases), significance map contexts "
            "(ctxBlockCat offsets, scan-position increments, chroma DC), coeff_abs_level_minus1 contexts and UEG0 suffix, sign, reverse order",
    "eos": "end_of_slice_flag: read after EVERY macroblock (skipped ones too); after I_PCM the engine restarts after the samples",
}


def die(msg, code=2):
    print(f"error: {msg}")
    sys.exit(code)


def read_text(path, what):
    try:
        return open(path, "r", errors="replace").read()
    except OSError as e:
        die(f"cannot read {what} {path!r}: {e}")


# ================================================================ dump parsing
MB_VALUE = {
    "mbt": re.compile(r"(skip|\d{1,2})$"),
    "cpred": re.compile(r"([0-3]|-)$"),
    "modes": re.compile(r"([p0-7]{16}|-)$"),
    "cbp": re.compile(r"(\d{1,2}|-)$"),
    "qpd": re.compile(r"(-?\d{1,2}|-)$"),
    "qp": re.compile(r"\d{1,2}$"),
    "coef": re.compile(r"(-|[a-z0-9]+:(-|\d+=-?\d+(,\d+=-?\d+)*)(;[a-z0-9]+:(-|\d+=-?\d+(,\d+=-?\d+)*))*)$"),
    "eos": re.compile(r"[01]$"),
}


def parse_dump(text, who):
    """-> dict slice index -> dict(type, first_mb, mbs=[(addr, {field: value}, lineno)], end_mbs).  Malformed -> exit 2."""
    slices, cur = {}, None
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
            cur = dict(type=d["type"], first_mb=int(d["first_mb"]), mbs=[], end=None, line=no)
            slices[int(w[1])] = cur
            cur_idx = int(w[1])
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


def coef_diff(e, g):
    """Name the first differing residual block of two coef= values (they can be very long)."""
    eb = [] if e == "-" else e.split(";")
    gb = [] if g == "-" else g.split(";")
    for j in range(max(len(eb), len(gb))):
        x = eb[j] if j < len(eb) else None
        y = gb[j] if j < len(gb) else None
        if x != y:
            where = f"block #{j} in decoding order"
            if x is None:
                return f"{where}: you print an extra block {y!r} (expected only {len(eb)} blocks)"
            if y is None:
                return f"{where}: expected {x!r}, your list ends after {len(gb)} blocks"
            return f"{where}: expected {x!r} got {y!r}" + (f" (expected coef={e!r})" if len(e) <= 120 else "")
    return f"expected {e!r} got {g!r}"


def check_dump(exp, got, rep):
    """exp, got: parse_dump results.  One check per expected macroblock; the first wrong macroblock of each slice is described."""
    for i in sorted(got):
        if i not in exp:
            rep.check(False, f"slice {i}: not a CABAC slice of this stream (or not a slice at all): remove it from your output")
    for i, e in exp.items():
        g = got.get(i)
        if g is None:
            rep.check(False, f"slice {i} ({e['type']}, first_mb {e['first_mb']}): no SLICE block in your output")
            continue
        if g["type"] != e["type"] or g["first_mb"] != e["first_mb"]:
            rep.check(False, f"slice {i}: expected type={e['type']} first_mb={e['first_mb']}, got type={g['type']} first_mb={g['first_mb']}")
            continue
        first_bad = None
        nbad = 0
        for k, (addr, f, ln) in enumerate(e["mbs"]):
            if k >= len(g["mbs"]):
                ok = False
                msg = (f"slice {i} ({e['type']}): your output stops after {len(g['mbs'])} macroblocks, the slice has {len(e['mbs'])} "
                       f"(next is macroblock {addr}); end_of_slice_flag came too early or the parse failed")
            else:
                ga, gf, gln = g["mbs"][k]
                diffs = [x for x in FIELDS if gf[x] != f[x]]
                ok = ga == addr and not diffs
                msg = ""
                if not ok:
                    if ga != addr:
                        msg = f"slice {i}, macroblock #{k} in the slice: expected address {addr}, got {ga} (your line {gln})"
                    else:
                        d0 = diffs[0]
                        what = coef_diff(f["coef"], gf["coef"]) if d0 == "coef" else f"expected {f[d0]!r} got {gf[d0]!r}"
                        msg = (f"slice {i} ({e['type']}), macroblock {addr} (#{k}), field {d0}: {what}"
                               + (f" (also differs: {', '.join(diffs[1:])})" if len(diffs) > 1 else "")
                               + f" [your line {gln}]  hint: {FIELD_HELP[d0]}")
            if not ok:
                nbad += 1
                if first_bad is None:
                    first_bad = msg
            if k >= len(g["mbs"]):
                break
        rep.check(first_bad is None, (first_bad or "") + (f" ... and {nbad - 1} more macroblock(s) of this slice differ" if nbad > 1 else ""))
        if first_bad is None:
            if len(g["mbs"]) != len(e["mbs"]):
                rep.check(False, f"slice {i}: you print {len(g['mbs'])} macroblocks, expected {len(e['mbs'])}")
            elif g["end"] != e["end"]:
                rep.check(False, f"slice {i}: END mbs={g['end']} but the slice has {e['end']} macroblocks")
            else:
                rep.check(True, "")
    return rep


# ================================================================ truth
def poc_order(infos):
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
    rank = {p: r for r, p in enumerate(order)}
    return [rank[p] for p in sorted(rank)]


def shape_char(slice_letter, mbt):
    if slice_letter == "P":
        return {0: " ", 1: "-", 2: "|", 3: "+"}.get(mbt)
    if mbt in (0, None) or mbt >= 23:
        return None
    return " " if mbt <= 3 else "+" if mbt == 22 else ("-" if mbt % 2 == 0 else "|")


def crosscheck_ffmpeg(path, infos, parsed, td):
    """Class (intra kind / inter / skip), partition shape and QP_Y of every macroblock vs ffmpeg's debug maps.
    -> (problems, note)."""
    if any(i.hdr.get("field_pic_flag", 0) for i in infos):
        return [], "field pictures: ffmpeg cross-check skipped"
    rank = poc_order(infos)
    if rank is None:
        return [], "POC type 1: ffmpeg cross-check skipped"
    w0 = infos[0].pic_width_in_mbs
    h0 = infos[0].pic_size_in_mbs // w0
    if not all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos):
        return [], "pictures of different sizes: ffmpeg cross-check skipped"
    npics = infos[-1].picture + 1
    cells = inter_ref.ffmpeg_cells(path, td, dims=(w0, h0), count=npics)
    qps = cavlc_ref.ffmpeg_qp_grid(path, td, dims=(w0, h0), count=npics)
    if cells is None or len(cells) < npics or qps is None or len(qps) < npics:
        return [], "ffmpeg's debug log was unusable: cross-check skipped"
    cells, qps = cells[-npics:], qps[-npics:]
    problems, n = [], 0
    for info, recs, _ in parsed:
        r = rank[info.picture]
        letter = resref.slice_letter(info)
        for rec in recs:
            y, x = divmod(rec.addr, w0)
            cell, q = cells[r]["rows"][y][x], qps[r]["rows"][y][x]
            n += 1
            if rec.skip:
                ok = cell[0] in "Sd"
            elif rec.pcm:
                ok = cell[0] == "P"
            elif rec.i_nxn:
                ok = cell[0] == "i"
            elif rec.i16:
                ok = cell[0] == "I"
            else:
                sh = shape_char(letter, rec.mbt)
                ok = cell[0] not in "iIPS" and (sh is None or cell[1] == sh or cell[0] == "d")
            if not ok:
                problems.append(f"slice {info.index} macroblock {rec.addr}: reference says mbt={rec.mbt} skip={rec.skip}, "
                                f"ffmpeg's map cell is {cell!r}")
            elif not rec.pcm and q != rec.qp:
                problems.append(f"slice {info.index} macroblock {rec.addr}: reference QP_Y {rec.qp}, ffmpeg {q}")
    return problems, f"ffmpeg cross-check: class, partition shape and QP_Y of {n} macroblocks agree"


def stream_infos(path, td):
    try:
        infos = cavlc_ref.load_stream(path, td)
    except SystemExit as e:
        die(str(e.code) if e.code else "cannot read the stream")
    if not infos:
        die("no slice NAL units found in the input")
    return resref.prepare(infos)


def truth(path, td, check=True):
    """-> (infos, parsed (truth mode), note).  Exit 2 if the stream cannot be handled."""
    ffref.require_tools()
    infos = stream_infos(path, td)
    reasons = sorted({r for r in map(resref.usable, infos) if r and r != "CAVLC slice"})
    if reasons:
        die("the reference cannot handle this stream: " + "; ".join(reasons) + ". Use a Main-profile CABAC progressive stream "
            "(e.g. the files written by --make-variants).")
    if not any(resref.usable(i) is None for i in infos):
        die("the stream has no CABAC slice; use a CABAC stream, e.g. the files written by --make-variants")
    try:
        parsed = resref.parse_all(infos)
    except resref.Unusable as e:
        die(f"HARNESS PROBLEM: the reference parser does not end cleanly on this stream: {e}")
    note = ""
    if check:
        manifest = os.path.splitext(path)[0] + ".manifest.json"
        if os.path.exists(manifest):
            m = json.load(open(manifest))
            if resref.render_dump(parsed) != m["expected"]:
                die("HARNESS PROBLEM: the reference disagrees with the manifest written by the stream generator")
        problems, note = crosscheck_ffmpeg(path, infos, parsed, td)
        if problems:
            die("HARNESS PROBLEM: the reference disagrees with ffmpeg:\n  " + "\n  ".join(problems[:5]))
    return infos, parsed, note


# ================================================================ test inputs
def synth_files(seed, out_dir=None):
    data, man = resgen.build(seed)
    return data, man


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    names = []

    def emit(name, path):
        td = tempfile.mkdtemp(prefix="cabac83_")
        try:
            infos, parsed, note = truth(path, td, check=False)
            open(os.path.join(out_dir, name + ".expected.txt"), "w").write(resref.render_dump(parsed))
            open(os.path.join(out_dir, name + ".hints.txt"), "w").write(resref.render_hints(parsed))
        finally:
            shutil.rmtree(td, ignore_errors=True)
        names.append(os.path.basename(path))
    for seed in (1, 2, 3, 4):
        data, man = resgen.build(seed)
        base = f"syn_res_{seed}"
        p = os.path.join(out_dir, base + ".264")
        open(p, "wb").write(data)
        json.dump(man, open(os.path.join(out_dir, base + ".manifest.json"), "w"))
        emit(base, p)
        names.append(base + ".manifest.json")
    for r in ("T10_intra", "T10_p", "T10_b", "T10_slices_aq", "T10_qp46", "T10_qp8", "T10_full"):
        dst = os.path.join(out_dir, "x264_" + r + ".ts")
        shutil.copyfile(testfiles.get(r), dst)
        emit("x264_" + r, dst)
    print("wrote to", out_dir + ":", ", ".join(names), "(+ <name>.hints.txt and <name>.expected.txt for each stream)")


# ================================================================ self-test
def pcm_pixel_check(path, man, td):
    """ffmpeg decodes the synthetic stream cleanly and reproduces every I_PCM macroblock."""
    yuv = os.path.join(td, "syn.yuv")
    r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-i", path, "-fps_mode", "passthrough", "-f", "rawvideo",
                   "-pix_fmt", "yuv420p", yuv], timeout=120)
    err = r.stderr.decode(errors="replace").strip()
    if err or r.returncode:
        return [f"ffmpeg reports errors: {err[:300]}"], 0
    infos = cavlc_ref.load_stream(path, td)
    rank = poc_order(infos)
    w, h = man["width_mbs"], man["height_mbs"]
    fs = w * 16 * h * 16 * 3 // 2
    data = open(yuv, "rb").read()
    if len(data) != fs * len(man["pictures"]):
        return [f"ffmpeg produced {len(data) // fs} frames, expected {len(man['pictures'])}"], 0
    problems, n = [], 0
    for pi, pic in enumerate(man["pictures"]):
        frame = data[rank[pi] * fs:(rank[pi] + 1) * fs]
        for s in pic:
            for e in s["mbs"]:
                n += 1
                if mbdump.mb_pixels(frame, w, h, e["addr"]) != bytes.fromhex(e["samples"]):
                    problems.append(f"picture {pi} macroblock {e['addr']}: ffmpeg's pixels differ from the I_PCM samples")
    return problems, n


def make_x264(td, name, opts, rc, frames):
    out = os.path.join(td, name + ".ts")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", testfiles.SRC, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
           "-c:v", "libx264", *rc, *opts.split(), "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode:
        die(f"ffmpeg failed for {name}: {r.stderr.decode()[-300:]}")
    return out


def reader_text(infos, hints_text, mutant=None):
    """What a reader of this chapter outputs (the reference in reader mode; with a wrong rule when `mutant` is set)."""
    hints = resref.parse_hints(hints_text)
    resref.MUTANT = mutant
    try:
        return resref.render_dump(resref.parse_all(infos, True, hints))
    finally:
        resref.MUTANT = None


def run_check(exp, text):
    """-> (exit code, Report) like the real run: 2 for malformed output."""
    rep = Report("x")
    try:
        got = parse_dump(text, "reader output")
    except SystemExit as e:
        return e.code, rep
    check_dump(exp, got, rep)
    return (1 if rep.failures else 0), rep


def ffmpeg_yuv(path, out):
    """ffmpeg's reference decode: cropped yuv420p, every frame, output order (the Chapter 5.1 reference)."""
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    ffref.run(cmd, timeout=600)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        die(f"ffmpeg could not decode {path}")


def geometry(infos):
    s = infos[0].sps
    return yuvref.cropped_size(infos[0].pic_width_in_mbs, s["pic_height_in_map_units_minus1"] + 1, s.get("frame_mbs_only_flag", 1),
                               s.get("frame_crop_left_offset", 0), s.get("frame_crop_right_offset", 0),
                               s.get("frame_crop_top_offset", 0), s.get("frame_crop_bottom_offset", 0))


def recon_check(path, infos, parsed, td):
    """Reconstruct an intra-only stream from the reference's coefficients and compare with ffmpeg's YUV.
    -> (ok or None when not applicable, text)."""
    try:
        pics = refrecon.decode_all(parsed)
    except refrecon.NotIntra as e:
        return None, str(e)
    out, ref = os.path.join(td, "recon.yuv"), os.path.join(td, "ref.yuv")
    refrecon.write_yuv(pics, out)
    ffmpeg_yuv(path, ref)
    w, h, x0, y0 = geometry(infos)
    r = yuvcmp.compare_yuv(ref, out, w, h, quiet=True, origin=(x0, y0))
    return r["ok"], (f"{len(pics)} frames identical to ffmpeg" if r["ok"] else r["text"][:300])


def yuv_mode(path, yuv):
    """--yuv: the reader's decoded frames against ffmpeg's."""
    td = tempfile.mkdtemp(prefix="cabac83_")
    try:
        infos = stream_infos(path, td)
        if not os.path.exists(yuv) or os.path.getsize(yuv) == 0:
            die(f"reader output {yuv!r} not found or empty")
        w, h, x0, y0 = geometry(infos)
        ref = os.path.join(td, "ref.yuv")
        ffmpeg_yuv(path, ref)
        if any(i.slice_type not in (2, 4) for i in infos):
            print("note: the stream has P or B slices: a reader of Chapter 8.3 can only reconstruct intra pictures "
                  "(inter syntax and motion compensation come with Chapters 8.4 and 7.x); expect a mismatch on inter frames.")
        r = yuvcmp.compare_yuv(ref, yuv, w, h, quiet=True, origin=(x0, y0))
        rep = Report("Chapter 8.3 end-to-end: your YUV vs ffmpeg")
        rep.check(r["ok"], "pixels differ from ffmpeg's reference (expected = ffmpeg, actual = yours)\n    "
                  + r["text"].replace("\n", "\n    "))
        rep.finish()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def self_test():
    ok_all = True
    td = tempfile.mkdtemp(prefix="cabac83_")
    try:
        ffref.require_tools()

        def step(name, ok, detail=""):
            nonlocal ok_all
            ok_all &= ok
            print(f"  [{'ok' if ok else 'FAILED'}] {name}" + (f": {detail}" if detail else ""))
        streams = []     # (name, infos, truth text, hints text, used for mutants)

        def add(name, path, cross=True, mutants=True, recon=False):
            infos, parsed, note = truth(path, td, check=False)
            if cross:
                problems, note = crosscheck_ffmpeg(path, infos, parsed, td)
                step(f"{name}: reference vs ffmpeg", not problems, problems[0] if problems else note)
            mbs = sum(len(r) for _, r, _ in parsed)
            ncoef = sum(1 for _, rs, _ in parsed for r in rs for b in r.blocks if b["coeffs"] is not None)
            step(f"{name}: reference parse of {len(parsed)} slices / {mbs} macroblocks / {ncoef} coded residual blocks ends exactly "
                 "at end_of_slice_flag = 1", True)
            if recon:
                ok, txt = recon_check(path, infos, parsed, td)
                step(f"{name}: coefficients -> reconstruction (Chapters 5.2-5.4) == ffmpeg pixels", bool(ok), txt)
            streams.append((name, infos, resref.render_dump(parsed), resref.render_hints(parsed), mutants))
            return parsed
        # 1. synthetic streams: construction == reference; ffmpeg decodes them; I_PCM pixels
        for seed in (1, 2, 3, 4, 5, 6):
            data, man = resgen.build(seed)
            p = os.path.join(td, f"syn{seed}.264")
            open(p, "wb").write(data)
            parsed = add(f"synthetic stream {seed}", p, cross=seed <= 3)
            step(f"synthetic stream {seed}: reference dump (incl. every coefficient) == dump from the construction",
                 resref.render_dump(parsed) == man["expected"])
            problems, n = pcm_pixel_check(p, man, td)
            step(f"synthetic stream {seed}: ffmpeg decodes without error and reproduces {n} I_PCM macroblocks", not problems,
                 problems[0] if problems else "")
        # 2. x264 streams (short versions, built here); intra-only ones also go through the reconstruction
        add("x264 intra (I4x4 + I16x16, 4 slices, aq, no deblock)",
            make_x264(td, "xi", "-profile:v main -x264-params keyint=1:no-deblock=1:slices=4:aq-mode=2", ["-crf", "24"], 5), recon=True)
        add("x264 intra QP 6 (large levels)", make_x264(td, "xl", "-profile:v main -x264-params keyint=1:no-deblock=1", ["-qp", "6"], 2),
            recon=True)
        add("x264 Main (I/P/B, B pyramid, 4 slices, aq)",
            make_x264(td, "xs", "-profile:v main -x264-params bframes=3:b-pyramid=normal:slices=4:aq-mode=2:keyint=20", ["-crf", "26"], 12))
        add("x264 T10_intra (50 frames)", testfiles.get("T10_intra"), mutants=False, recon=True)
        add("x264 T10_full", testfiles.get("T10_full"), mutants=False)
        # 3. a known-good reader passes; corrupted dumps fail
        for name, infos, truth_text, hints_text, _ in streams:
            exp = parse_dump(truth_text, "expected")
            got = reader_text(infos, hints_text)
            rc, rep = run_check(exp, got)
            step(f"{name}: a correct reader (reference in reader mode, fed with the cbp HINTs only) passes",
                 rc == 0 and got == truth_text, rep.failures[0] if rep.failures else "")
        name, infos, truth_text, hints_text, _ = streams[0]
        exp = parse_dump(truth_text, "expected")
        lines = truth_text.splitlines()
        ib = next(i for i, l in enumerate(lines) if l.startswith("MB 1 ") and "mbt=skip" not in l and "qpd=-" not in l
                  and re.search(r"coef=\w+\d+:\d+=", l))

        def corrupted(fn):
            return "\n".join(fn(list(lines))) + "\n"
        tests = {
            "a wrong qp": lambda L: L[:ib] + [re.sub(r"qp=\d+", "qp=99", L[ib])] + L[ib + 1:],
            "a wrong cbp": lambda L: L[:ib] + [re.sub(r"cbp=\d+", "cbp=1" if "cbp=0 " in L[ib] else "cbp=0", L[ib])] + L[ib + 1:],
            "one coefficient value changed": lambda L: L[:ib] + [re.sub(r"(coef=\w+\d+:\d+=)(-?\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1),
                                                                         L[ib], count=1)] + L[ib + 1:],
            "one coefficient moved to the next scan position": lambda L: L[:ib] + [re.sub(r"(coef=\w+\d+:)(\d+)=", lambda m: m.group(1) + str(int(m.group(2)) + 1) + "=",
                                                                                           L[ib], count=1)] + L[ib + 1:],
            "a coded block reported as empty": lambda L: L[:ib] + [re.sub(r"(coef=\w+\d+:)[^;\s]+", r"\1-", L[ib], count=1)] + L[ib + 1:],
            "a missing macroblock": lambda L: L[:ib] + L[ib + 1:],
            "a missing END line": lambda L: [l for l in L if not l.startswith("END 0 ")],
            "a wrong eos on the last macroblock": lambda L: [l.replace("eos=1", "eos=0", 1) if l.startswith("MB 0 ") and "eos=1" in l else l for l in L],
            "an extra slice": lambda L: L + ["SLICE 999 type=I first_mb=0", "END 999 mbs=0"],
            "a missing slice": lambda L: [l for l in L if not l.startswith(("SLICE 1 ", "MB 1 ", "END 1 "))],
            "swapped macroblock lines": lambda L: L[:ib] + [L[ib + 1], L[ib]] + L[ib + 2:],
        }
        for what, fn in tests.items():
            rc, _ = run_check(exp, corrupted(fn))
            step(f"output with {what} is rejected", rc == 1, f"exit code {rc}")
        for what, text in {"a line with an unknown keyword": "FOO 1\n", "an MB line without fields": "SLICE 0 type=I first_mb=0\nMB 0 0\n",
                           "an unknown extra field": "SLICE 0 type=I first_mb=0\nMB 0 0 mbt=0 cpred=0 modes=pppppppppppppppp cbp=0 qpd=- qp=1 coef=- eos=1 x=1\nEND 0 mbs=1\n",
                           "a missing coef field": "SLICE 0 type=I first_mb=0\nMB 0 0 mbt=0 cpred=0 modes=pppppppppppppppp cbp=0 qpd=- qp=1 eos=1\nEND 0 mbs=1\n",
                           "a malformed coef value": "SLICE 0 type=I first_mb=0\nMB 0 0 mbt=0 cpred=0 modes=pppppppppppppppp cbp=0 qpd=- qp=1 coef=luma0:3 eos=1\nEND 0 mbs=1\n",
                           "a bad value": "SLICE 0 type=I first_mb=0\nMB 0 0 mbt=x cpred=0 modes=- cbp=0 qpd=- qp=1 coef=- eos=0\nEND 0 mbs=1\n"}.items():
            rc, _ = run_check(exp, text)
            step(f"output with {what} gives exit code 2", rc == 2, f"exit code {rc}")
        # 4. mutants: deliberately wrong rules in the reader
        names = list(resref.MUTANTS)
        print(f"  mutation tests ({len(names)} wrong rules; each must be rejected on at least one stream):")
        expected = [(n, parse_dump(t, "expected")) for n, _, t, _, _ in streams]
        n_x264 = {}
        for mname in names:
            syn, x264 = [], []
            for (sname, infos, _, hints_text, use), (_, exp) in zip(streams, expected):
                if not use:
                    continue
                short = sname.split(" (")[0]
                try:
                    out = reader_text(infos, hints_text, mname)
                    rc, _ = run_check(exp, out)
                except Exception as e:      # a wrong rule may crash a reader: also a detection
                    rc, short = 1, f"{short} (crash {type(e).__name__})"
                if rc:
                    (syn if short.startswith("synthetic") else x264).append(short.replace("synthetic stream ", "syn ")
                                                                              .replace("x264 ", ""))
            n_x264[mname] = bool(x264)
            n_syn = sum(1 for n, *_, u in streams if n.startswith("synthetic") and u)
            detail = (f"caught on {len(syn)}/{n_syn} synthetic streams; x264: {', '.join(x264) or 'none'}"
                      if syn or x264 else "NOT CAUGHT")
            step(f"mutant {mname}: {resref.MUTANTS[mname]}", bool(syn or x264), detail)
        print(f"  ({sum(n_x264.values())} of {len(names)} wrong rules are also caught on at least one x264 stream; "
              "the rest need situations x264 rarely produces, e.g. I_PCM neighbours or huge levels, which the synthetic streams provide)")
    finally:
        resref.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


# ================================================================ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="CABAC H.264 stream (.ts or Annex B .264)")
    ap.add_argument("output", nargs="?", help="your macroblock dump")
    ap.add_argument("--yuv", action="store_true", help="INPUT READER.yuv: compare your decoded frames with ffmpeg's (intra-only streams)")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--print-expected", nargs=3, metavar=("WHAT", "INPUT", "OUT"), help="WHAT = hints | dump")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.make_variants:
        ffref.require_tools()
        make_variants(a.make_variants)
        return
    if a.yuv:
        if not (a.input and a.output):
            die("--yuv needs INPUT and READER.yuv")
        ffref.require_tools()
        yuv_mode(a.input, a.output)
        return
    td = tempfile.mkdtemp(prefix="cabac83_")
    try:
        if a.print_expected:
            what, inp, out = a.print_expected
            if what not in ("hints", "dump"):
                die("--print-expected takes hints or dump")
            _, parsed, note = truth(inp, td)
            open(out, "w").write(resref.render_hints(parsed) if what == "hints" else resref.render_dump(parsed))
            print("wrote", out, ("(" + note + ")") if note else "")
            return
        if not (a.input and a.output):
            ap.print_help()
            die("nothing to check: give <input> <your_dump>, or one of the options above")
        _, parsed, note = truth(a.input, td)
        if note:
            print("note:", note)
        exp = parse_dump(resref.render_dump(parsed), "expected dump")
        got = parse_dump(read_text(a.output, "reader output"), "reader output")
        rep = Report("Chapter 8.3 CABAC residual coding")
        check_dump(exp, got, rep)
        rep.failures = [f for f in rep.failures if f]
        rep.finish()
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
