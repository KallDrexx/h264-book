#!/usr/bin/env python3
"""Chapter 5.5 harness: QP tracking (7.4.5) and multiple slices per picture (last chapter of Phase 5).

usage: test_qp_slices.py <in.ts|in.264> <reader_output.yuv>
       test_qp_slices.py --make-variants DIR     write the synthetic test streams into DIR
       test_qp_slices.py --self-test             verify this harness itself

The truth is ffmpeg's decode (-fps_mode passthrough -f rawvideo -pix_fmt yuv420p, cropped), compared sample for sample with
your .yuv.  Your reader (Phase 1-4 pipeline + Chapters 5.1-5.5) writes one cropped frame per decoded picture, all pictures
of the stream.  The streams are intra-only, CAVLC, deblocking off: the recipes T1, T2, T4_nodeblock, T2_crf30, T4_crf30,
T2_aq, T2_cip of common/testfiles.py and everything --make-variants writes.  (T3 and T4 have the deblocking filter: Phase 6.)

On a mismatch the harness names the first wrong macroblock in decoding order (not in pixel order) together with what matters
for this chapter: its slice (index, first macroblock, SliceQP_Y, slice_qp_delta), its position in the slice, its type,
mb_qp_delta, QP_Y,PRED, QP_Y (from the independent reference parser, and ffmpeg's own QP map), its chroma QPs, which
neighbours lie in another slice, and the macroblock before it.  It then prints hints, e.g. when the first wrong macroblock is
the first of its slice (QP_Y,PRED not reset to SliceQP_Y, or neighbours of the previous slice still treated as available).
Exit code: 0 pass, 1 fail, 2 missing or unusable reader output.
"""
import argparse
import os
import shutil
import sys
import tempfile
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref as R, ffref, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402

QUIET = False
# Table 8-15 (qPI 30..51 -> QPc), typed from the standard, independent of the reference reader
QPC_TABLE = [29, 30, 31, 32, 32, 33, 34, 34, 35, 35, 36, 36, 37, 37, 37, 38, 38, 38, 39, 39, 39, 39]
RECIPE_STREAMS = ("T1", "T2", "T4_nodeblock", "T2_crf30", "T4_crf30", "T2_aq", "T2_cip")     # from common/testfiles.py


def die(msg):
    print("error:", msg)
    sys.exit(2)


def chroma_qp(qpy, offset):
    qpi = max(0, min(51, qpy + offset))
    return qpi if qpi < 30 else QPC_TABLE[qpi - 30]


# ------------------------------------------------------------------ stream analysis (per macroblock)
class Pic:
    pass


def analyse(path, workdir):
    """Parse the stream with the Phase 4 reference parser.  -> (pictures, problem).  problem is None when every slice is a
    plain CAVLC I slice that parsed completely, else a text."""
    try:
        slices = R.load_stream(path, workdir)
    except SystemExit as e:
        return [], str(e)
    pics, problem = OrderedDict(), None
    for s in slices:
        pics.setdefault(s.picture, []).append(s)
    out = []
    for pi, sl in pics.items():
        s0 = sl[0]
        p = Pic()
        p.index, p.w = pi, s0.pic_width_in_mbs
        p.h = s0.pic_size_in_mbs // p.w
        sps = s0.sps
        p.cw, p.ch, p.x0, p.y0 = yuvref.cropped_size(
            p.w, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
            sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
            sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
        p.mb, p.slices = {}, []
        p.cip = int(s0.pps.get("constrained_intra_pred_flag", 0))
        o1 = s0.pps["chroma_qp_index_offset"]
        p.offs = (o1, s0.pps.get("second_chroma_qp_index_offset", o1))
        for k, s in enumerate(sl):
            res = R.parse_slice(s, 4)
            if res.status != "complete":
                problem = problem or f"slice {s.index} not decodable by this chapter: {res.error or res.note}"
                continue
            init = 26 + s.pps["pic_init_qp_minus26"]
            p.slices.append(dict(k=k, first=s.first_mb, qp=s.slice_qp, init=init, delta=s.slice_qp - init, n=len(res.mbs)))
            prev_qp, prev = s.slice_qp, None
            for pos, rec in enumerate(res.mbs):
                f = rec.f
                kind = "PCM" if rec.pcm is not None else "NXN" if f["name"] == "I_NxN" else "I16"
                p.mb[rec.addr] = dict(kind=kind, slice=k, first=s.first_mb, pos=pos, cbp=f.get("cbp", "-"), qp=f.get("qp", "-"),
                                      qp_delta=f.get("qp_delta", "-"), pred_qp=prev_qp, prev_kind=prev,
                                      prev_nodelta=prev is not None and prev["qp_delta"] == "-",
                                      t8=f.get("t8x8") == 1)
                prev_qp, prev = p.mb[rec.addr]["qp"], p.mb[rec.addr]
        if len(p.mb) != p.w * p.h:
            problem = problem or f"picture {pi}: {len(p.mb)} macroblocks parsed, expected {p.w * p.h}"
        elif any(m["t8"] for m in p.mb.values()):
            problem = problem or "transform_size_8x8_flag = 1 (Intra 8x8) is not part of Phase 5"
        out.append(p)
    return out, problem


def other_slice_neighbours(p, addr):
    """Which of A, B, C, D exist in the picture but belong to another slice."""
    mb = p.mb[addr]
    mx, my = addr % p.w, addr // p.w
    names = []
    for name, o, inside in (("A", addr - 1, mx > 0), ("B", addr - p.w, my > 0), ("C", addr - p.w + 1, my > 0 and mx < p.w - 1),
                            ("D", addr - p.w - 1, mx > 0 and my > 0)):
        if inside and p.mb[o]["slice"] != mb["slice"]:
            names.append(name)
    return names


def wraps(mb):
    d = mb["qp_delta"]
    return d != "-" and not 0 <= mb["pred_qp"] + d <= 51


def describe_mb(p, addr, ffqp=None):
    mb = p.mb[addr]
    sl = p.slices[mb["slice"]]
    mx, my = addr % p.w, addr // p.w
    lines = [f"MB {addr} (x={mx}, y={my}), {mb['kind']}, CBP {mb['cbp']}",
             f"slice {mb['slice']} of the picture: first_mb_in_slice {sl['first']}, SliceQP_Y {sl['qp']} "
             f"(pic_init_qp {sl['init']}, slice_qp_delta {sl['delta']:+d}); this is macroblock {mb['pos']} of the slice"]
    d = mb["qp_delta"]
    qline = f"QP_Y,PRED {mb['pred_qp']}, " + ("no mb_qp_delta (inferred 0)" if d == "-" else f"mb_qp_delta {d:+d}") \
        + f"  ->  QP_Y {mb['qp']}" + ("   [the sum wrapped: " + f"{mb['pred_qp']} + {d} = {mb['pred_qp'] + d}]" if wraps(mb) else "")
    if ffqp is not None:
        qline += f";  ffmpeg's QP map: {ffqp}" + ("  (I_PCM shows 0: that is the value the deblocking filter uses, 8.7.2.2)" if mb["kind"] == "PCM" else "")
    lines.append(qline)
    if mb["kind"] != "PCM":
        lines.append(f"chroma QP: Cb {chroma_qp(mb['qp'], p.offs[0])} (offset {p.offs[0]:+d}), "
                     f"Cr {chroma_qp(mb['qp'], p.offs[1])} (offset {p.offs[1]:+d})")
    oth = other_slice_neighbours(p, addr)
    lines.append("neighbours in another slice (available in the picture, but unavailable for prediction): "
                 + (", ".join(oth) if oth else "none") + f";  constrained_intra_pred_flag {p.cip}")
    pk = mb["prev_kind"]
    if pk is not None:
        lines.append(f"previous macroblock in the slice: {pk['kind']}, "
                     + ("no mb_qp_delta" if pk["qp_delta"] == "-" else f"mb_qp_delta {pk['qp_delta']:+d}") + f", QP_Y {pk['qp']}")
    return lines


# ------------------------------------------------------------------ reference and comparison
def reference_yuv(path, out):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    ffref.run(cmd, timeout=600)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        die(f"ffmpeg could not decode {path}")


def region(p, plane, x, y, size):
    ox, oy = (p.x0, p.y0) if plane == "Y" else (p.x0 // 2, p.y0 // 2)
    pw, ph = (p.cw, p.ch) if plane == "Y" else ((p.cw + 1) // 2, (p.ch + 1) // 2)
    x1, y1, x2, y2 = max(x - ox, 0), max(y - oy, 0), min(x + size - ox, pw), min(y + size - oy, ph)
    return (x1, y1, x2 - x1, y2 - y1) if x2 > x1 and y2 > y1 else None


def offset(p, plane):
    cw, ch = (p.cw + 1) // 2, (p.ch + 1) // 2
    return {"Y": (0, p.cw), "Cb": (p.cw * p.ch, cw), "Cr": (p.cw * p.ch + cw * ch, cw)}[plane]


def differs(p, base, ref, out, plane, x, y, size):
    r = region(p, plane, x, y, size)
    if r is None:
        return False
    off, stride = offset(p, plane)
    rx, ry, rw, rh = r
    for row in range(rh):
        pos = base + off + (ry + row) * stride + rx
        if ref[pos:pos + rw] != out[pos:pos + rw]:
            return True
    return False


def ffmpeg_qps(path, pics, workdir):
    """ffmpeg's -debug qp map for every picture -> list of dict addr -> QP, or None when unavailable."""
    try:
        grid = R.ffmpeg_qp_grid(path, workdir, dims=(pics[0].w, pics[0].h), count=len(pics))
    except Exception:       # noqa: BLE001 - diagnostics only
        return None
    if not grid or len(grid) != len(pics):
        return None
    return [{a: g["rows"][a // p.w][a % p.w] for a in range(p.w * p.h)} for g, p in zip(grid, pics)]


def hints(pics, wrong, first):
    """wrong = list of (picture index, addr, luma_bad, chroma_bad) in decoding order; first = the first of them."""
    fi, addr, lbad, cbad = first
    p = pics[fi]
    mb = p.mb[addr]
    out = []
    firsts = {}
    for pi, a, _, _ in wrong:                       # first wrong macroblock of every (picture, slice)
        firsts.setdefault((pi, pics[pi].mb[a]["slice"]), a)
    at_start = [k for k, a in firsts.items() if pics[k[0]].mb[a]["pos"] == 0]
    if mb["pos"] == 0 and fi == 0 and mb["slice"] == 0:
        out.append("The first wrong macroblock is the very first macroblock of the stream: nothing can have been carried over "
                   "from an earlier slice yet.  Check SliceQP_Y = 26 + pic_init_qp_minus26 + slice_qp_delta and this "
                   "macroblock's own QP update and prediction (with no neighbours available).")
    elif mb["pos"] == 0:
        out.append("The first wrong macroblock is the FIRST macroblock of its slice: the wrong values start exactly at a slice "
                   "boundary.  Check that (1) QP_Y,PRED is reset to SliceQP_Y = 26 + pic_init_qp_minus26 + slice_qp_delta at the "
                   "start of every slice (not carried over from the previous slice, not the picture's pic_init_qp), and (2) the "
                   "neighbour availability is reset: macroblocks of an earlier slice are NOT available for intra prediction, "
                   "mode prediction (dcPredModePredictedFlag) or nC.")
    if len(firsts) > 1 and len(at_start) == len(firsts):
        out.append(f"In all {len(firsts)} damaged slices the first wrong macroblock is the slice's first one: a slice-start "
                   "problem (QP reset or availability), not a QP arithmetic problem.")
    elif len(firsts) > 1 and mb["pos"] != 0 and not at_start:
        out.append("No damaged slice starts wrong: the slice-start handling is probably fine; look at the macroblock itself.")
    pk = mb["prev_kind"]
    if pk is not None and pk["kind"] == "PCM":
        out.append("The previous macroblock is I_PCM.  It has no mb_qp_delta (inferred 0), so QP_Y,PRED for this macroblock is the "
                   "QP_Y,PRED the PCM macroblock itself had: the PCM macroblock does NOT change the running QP (not to 0 - "
                   "that 0 is only what the deblocking filter uses for it, 8.7.2.2 - and not back to SliceQP_Y).")
    elif pk is not None and mb["qp_delta"] != "-" and pk["qp_delta"] == "-" and mb["pos"] > 0:
        out.append("The previous macroblock has no mb_qp_delta (I_NxN with CBP 0): its QP_Y is the running QP_Y,PRED, unchanged.  "
                   "Make sure a macroblock without mb_qp_delta does not reset or modify the running QP.")
    if wraps(mb):
        out.append(f"This macroblock's mb_qp_delta wraps: {mb['pred_qp']} + ({mb['qp_delta']:+d}) = {mb['pred_qp'] + mb['qp_delta']}, "
                   f"so QP_Y = {mb['qp']} (7.4.5: QP_Y = (QP_Y,PRED + mb_qp_delta + 52) mod 52 for 8-bit video).  If your QP_Y here "
                   f"is not {mb['qp']}: clamping to 0..51 is wrong, a plain sum outside the range is wrong, and the remainder must "
                   "be non-negative.")
    wfrac = sum(1 for pi, a, _, _ in wrong if wraps(pics[pi].mb[a])) / len(wrong)
    allmb = [m for pi in {x[0] for x in wrong} for m in pics[pi].mb.values()]
    if not wraps(mb) and wfrac > 0 and wfrac > 2 * sum(1 for m in allmb if wraps(m)) / len(allmb):
        out.append(f"{100 * wfrac:.0f}% of the wrong macroblocks have a wrapping mb_qp_delta, far more than in the pictures overall: "
                   "check the modulo formula of 7.4.5.")
    if not lbad and cbad:
        out.append("Luma is right but chroma is wrong in the first wrong macroblock: the luma QP chain is probably fine; check the "
                   "chroma QP: QPc from qPI = Clip3(0, 51, QP_Y + offset), the table for qPI >= 30 (Table 8-15), and "
                   "second_chroma_qp_index_offset for Cr (chroma_qp_index_offset for Cb).")
    oth = other_slice_neighbours(p, addr)
    if oth and mb["pos"] != 0:
        out.append(f"The first wrong macroblock has neighbours in another slice ({', '.join(oth)}) although it is not the first of its "
                   "slice (a slice that started in the middle of a row): such neighbours must count as unavailable, for sample "
                   "prediction (A left, B above, C above-right, D above-left) and for Intra4x4 mode prediction.")
    if p.cip:
        out.append("This picture has constrained_intra_pred_flag = 1.  In an I picture every macroblock is intra, so the flag must change "
                   "NOTHING: if your reader treats neighbours as unavailable because of it, only Inter neighbours may (Phase 7).")
    return out


def compare(path, yuv, workdir):
    """-> Report (not finished)."""
    rep = Report("5.5 QP tracking and slices: " + os.path.basename(path))
    if not os.path.isfile(yuv):
        die(f"reader output {yuv!r} not found (did your program write it?)")
    if os.path.getsize(yuv) == 0:
        die(f"reader output {yuv!r} is empty")
    pics, problem = analyse(path, workdir)
    if not pics:
        die(f"cannot analyse {path}: {problem}")
    ref = os.path.join(workdir, "ref.yuv")
    reference_yuv(path, ref)
    p0 = pics[0]
    w, h = p0.cw, p0.ch
    fs = yuvref.frame_bytes(w, h)
    nref, size = os.path.getsize(ref) // fs, os.path.getsize(yuv)
    rep.check(len(pics) == nref, f"harness sanity: {len(pics)} pictures parsed, ffmpeg decodes {nref} frames")
    msg = f"file size: expected {nref * fs} bytes = {nref} frames x {fs} bytes ({w}x{h}, yuv420p), got {size}"
    if size % fs:
        msg += f" = {size // fs} whole frames + {size % fs} stray bytes"
    msg += "; every picture of the stream must be decoded and written (all slices of every picture)"
    rep.check(size == nref * fs, msg)
    if size % fs or size // fs == 0:
        print("note: pixels not compared, because the file is not a whole number of frames; fix the size first (Chapter 5.1)")
        return rep
    ref_b, out_b = open(ref, "rb").read(), open(yuv, "rb").read()
    if problem:
        print(f"note: macroblock analysis unavailable ({problem}); plain frame comparison only")
    nframes = min(nref, size // fs)
    r = yuvcmp.compare_yuv(ref, yuv, w, h, quiet=True, origin=(p0.x0, p0.y0))
    rep.check(r["ok"], "pixels differ from ffmpeg's reference (expected = ffmpeg, actual = yours)\n    "
              + r["text"].replace("\n", "\n    "))
    if r["ok"] or problem:
        if r["ok"] and not problem and not QUIET:
            print_summary(pics[:nframes])
        return rep
    wrong = []                          # (picture, addr, luma_bad, chroma_bad) in decoding order
    for fi in range(nframes):
        p = pics[fi]
        for addr in range(p.w * p.h):
            mx, my = addr % p.w, addr // p.w
            base = fi * fs
            lb = differs(p, base, ref_b, out_b, "Y", 16 * mx, 16 * my, 16)
            cb = differs(p, base, ref_b, out_b, "Cb", 8 * mx, 8 * my, 8) or differs(p, base, ref_b, out_b, "Cr", 8 * mx, 8 * my, 8)
            if lb or cb:
                wrong.append((fi, addr, lb, cb))
    if not wrong:
        rep.check(False, "only the cropped-away part differs")
        return rep
    fi, addr, lb, cb = wrong[0]
    ff = ffmpeg_qps(path, pics[:nframes], workdir)
    lines = [f"{len(wrong)} macroblock(s) differ in {len({a[0] for a in wrong})} of {nframes} pictures "
             f"(checked: {nframes * p0.w * p0.h}).",
             f"first wrong macroblock in DECODING order: picture {fi}, " + ("luma and chroma" if lb and cb else "luma" if lb else "chroma")
             + " wrong"]
    lines += ["  " + t for t in describe_mb(pics[fi], addr, ff[fi][addr] if ff else None)]
    p = pics[fi]
    bad_in_slice = Counter(p.mb[a]["slice"] for pi, a, _, _ in wrong if pi == fi)
    per = ", ".join(f"slice {k} (first MB {p.slices[k]['first']}): {n} of {p.slices[k]['n']}" for k, n in sorted(bad_in_slice.items())[:8])
    lines.append(f"wrong macroblocks per slice in picture {fi}: {per}" + (" ..." if len(bad_in_slice) > 8 else ""))
    lines += ["hint: " + t for t in hints(pics, wrong, wrong[0])]
    rep.check(False, "\n    ".join(lines))
    return rep


def print_summary(pics):
    types = Counter(m["kind"] for p in pics for m in p.mb.values())
    nsl = sum(len(p.slices) for p in pics)
    deltas = [m["qp_delta"] for p in pics for m in p.mb.values() if m["qp_delta"] != "-"]
    qps = [m["qp"] for p in pics for m in p.mb.values() if m["kind"] != "PCM"]
    nw = sum(1 for p in pics for m in p.mb.values() if wraps(m))
    npcm_after = sum(1 for p in pics for m in p.mb.values() if m["prev_kind"] is not None and m["prev_kind"]["kind"] == "PCM")
    print(f"compared all samples of {len(pics)} frames in {nsl} slices ({nsl / len(pics):.1f} per picture); macroblocks: "
          + ", ".join(f"{k} {v}" for k, v in sorted(types.items())))
    if deltas:
        print(f"QP_Y {min(qps)}..{max(qps)}; {sum(1 for d in deltas if d)} of {len(deltas)} mb_qp_delta values non-zero "
              f"(range {min(deltas):+d}..{max(deltas):+d}), {nw} wrap around 0/51; {len(pics[0].mb) * len(pics) - len(deltas)} macroblocks "
              f"without mb_qp_delta; {npcm_after} macroblocks directly after an I_PCM; "
              f"constrained_intra_pred_flag {sorted({p.cip for p in pics})}")


# ------------------------------------------------------------------ test streams
def make_variants(d, quiet=False):
    import qsgen
    os.makedirs(d, exist_ok=True)
    cover, paths = Counter(), {}
    for name, spec in qsgen.VARIANTS.items():
        data, info = qsgen.make_stream(spec)
        h264, ts = os.path.join(d, name + ".264"), os.path.join(d, name + ".ts")
        open(h264, "wb").write(data)
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-framerate", "25", "-i", h264, "-c:v", "copy", "-f", "mpegts", ts],
                      timeout=120)
        if r.returncode != 0:
            raise SystemExit("ffmpeg could not mux " + h264 + ": " + r.stderr.decode()[-300:])
        cover.update(info["cover"])
        paths[name] = ts
        if not quiet:
            print(f"{ts}  {16 * info['w']}x{16 * info['h']} coded, {info['pictures']} pictures, {info['n_slices']} slices")
    missing = qsgen.coverage_missing(cover)
    if missing:
        raise SystemExit(f"internal error: cases never generated: {missing}")
    # constrained_intra_pred_flag = 1 must change nothing in an intra-only stream: qs_cip and qs_nocip differ only in the PPS flag
    outs = []
    for name in ("qs_cip", "qs_nocip"):
        o = os.path.join(d, name + ".ref.yuv")
        reference_yuv(paths[name], o)
        outs.append(open(o, "rb").read())
        os.remove(o)
    if outs[0] != outs[1]:
        raise SystemExit("internal error: ffmpeg decodes qs_cip and qs_nocip differently")
    if not quiet:
        print(f"coverage: all {len(qsgen.required_cover())} required cases occur in the synthetic streams (each at least "
              f"{min(cover[k] for k in qsgen.required_cover())} times): mb_qp_delta -26 and +25, QP_Y wrapping up and down, QP 0 and 51, "
              "macroblocks after I_PCM with and without mb_qp_delta, macroblocks without mb_qp_delta before ones with it, "
              "slices of one macroblock, starting at a row start and mid-row, neighbours A/B/C/D in another slice, I_NxN "
              "neighbours in another slice, chroma qPI clipped low/high and in the table range, constrained_intra_pred_flag = 1")
        print("verified: ffmpeg decodes qs_cip (constrained_intra_pred_flag = 1) and qs_nocip (0) to identical frames")
        print("also decode the recipes with: python3 -m common.testfiles " + " ".join(RECIPE_STREAMS) + "  (files in ../.cache)")
        print("run your reader on each .ts (or the .264), then: python3 test_qp_slices.py <that.ts> <your output.yuv>")
    return list(paths.values())


# ------------------------------------------------------------------ self test
MUTANTS = [
    # (name, what is wrong, streams to try)
    ("qp_no_reset", "QP_Y,PRED at the start of a slice is the last QP_Y of the previous slice instead of SliceQP_Y"),
    ("qp_slice_pic_init", "SliceQP_Y = pic_init_qp (slice_qp_delta ignored)"),
    ("qp_clamp", "mb_qp_delta result clamped to 0..51 instead of wrapping"),
    ("qp_nomod", "mb_qp_delta result not wrapped at all (QP_Y can leave 0..51)"),
    ("qp_pcm_zero", "an I_PCM macroblock sets the running QP to 0 (it is 0 only for the deblocking filter)"),
    ("qp_pcm_slice", "an I_PCM macroblock resets the running QP to SliceQP_Y"),
    ("qp_nodelta_reset", "a macroblock without mb_qp_delta resets the running QP to SliceQP_Y instead of keeping it"),
    ("cqp_noclip", "chroma qPI = QP_Y + offset without Clip3(0, 51, .)"),
    ("cqp_no_table", "chroma QP = qPI without the Table 8-15 mapping for qPI >= 30"),
    ("cqp_cr_uses_cb", "Cr uses chroma_qp_index_offset instead of second_chroma_qp_index_offset"),
    ("avail_noslice", "neighbour availability ignores slice boundaries"),
    ("avail_noslice_above", "macroblocks above (B, C, D) ignore slice boundaries, the left one does not"),
    ("mp_avail_noslice", "Intra4x4 mode prediction ignores slice boundaries (sample availability does not)"),
    ("cip_all_unavailable", "constrained_intra_pred_flag = 1 makes every neighbour unavailable (it only excludes Inter ones)"),
    ("cip_pcm_unavailable", "constrained_intra_pred_flag = 1 treats I_PCM neighbours as Inter (they are Intra)"),
]


def self_test():
    import refdecode
    global QUIET
    QUIET = True
    ffref.require_tools()
    td = tempfile.mkdtemp(prefix="qp55_self_")
    ok = True

    def expect(label, rep, want_pass):
        nonlocal ok
        good = (not rep.failures) == want_pass
        ok &= good
        print(f"  {'ok  ' if good else 'BAD '} {label}: {'passes' if not rep.failures else 'fails'}"
              f"{'' if good else '  <-- unexpected'}")
        if rep.failures and not want_pass:
            print("       e.g.", rep.failures[0].replace("\n", "\n       ")[:300].split("\n")[0])
        return rep

    def run(ts, yuv):
        return compare(ts, yuv, tempfile.mkdtemp(dir=td))

    def decode(ts, out, mutant=None):
        refdecode.MUTANT = mutant
        try:
            refdecode.write_yuv(refdecode.decode_pictures(ts), out)
        finally:
            refdecode.MUTANT = None

    try:
        d = os.path.join(td, "v")
        print("generating streams ...")
        paths = {os.path.splitext(os.path.basename(p))[0]: p for p in make_variants(d, quiet=True)}
        print("  ok   qs_cip and qs_nocip (constrained_intra_pred_flag 1 / 0) decode identically in ffmpeg")
        from common import testfiles
        for name in RECIPE_STREAMS:
            paths[name] = testfiles.get(name)
        good = {}
        print("reference reader (refdecode.py) versus ffmpeg, whole frames (byte-identical required):")
        for name, ts in paths.items():
            g = os.path.join(td, name + ".good.yuv")
            decode(ts, g)
            good[name] = g
            expect(f"{name} correct output", run(ts, g), True)
        # what the real streams contain
        print("what the real encodes exercise:")
        for name, need_slices, need_qp in (("T2_crf30", 1, True), ("T2_aq", 1, True), ("T4_crf30", 4, True), ("T4_nodeblock", 4, False),
                                           ("T2_cip", 1, False)):
            pics, _ = analyse(paths[name], tempfile.mkdtemp(dir=td))
            nsl = {len(p.slices) for p in pics}
            deltas = [m["qp_delta"] for p in pics for m in p.mb.values() if m["qp_delta"] != "-"]
            nz = sum(1 for x in deltas if x)
            good_ = nsl == {need_slices} and (not need_qp or nz > 200) and (name != "T2_cip" or {p.cip for p in pics} == {1})
            ok &= good_
            print(f"  {'ok  ' if good_ else 'BAD '} {name}: {len(pics)} pictures, slices per picture {sorted(nsl)}, {nz} of {len(deltas)} "
                  f"mb_qp_delta non-zero, constrained_intra_pred_flag {sorted({p.cip for p in pics})}")
        # the reference parser's QP chain versus ffmpeg's own QP map (also settles the I_PCM rule against a conformant decoder)
        print("QP chain of the reference parser versus ffmpeg's -debug qp map (I_PCM macroblocks show 0 in ffmpeg):")
        for name in ("qs_wrap", "qs_pcm", "qs_midrow", "T4_crf30", "T2_aq"):
            pics, _ = analyse(paths[name], tempfile.mkdtemp(dir=td))
            ff = ffmpeg_qps(paths[name], pics, tempfile.mkdtemp(dir=td))
            bad = []
            if ff is None:
                bad = ["ffmpeg QP map unavailable"]
            else:
                for pi, p in enumerate(pics):
                    for a, m in p.mb.items():
                        want = 0 if m["kind"] == "PCM" else m["qp"]
                        if ff[pi][a] != want:
                            bad.append((pi, a, want, ff[pi][a]))
            ok &= not bad
            npcm = sum(1 for p in pics for m in p.mb.values() if m["kind"] == "PCM")
            print(f"  {'ok  ' if not bad else 'BAD '} {name}: {sum(len(p.mb) for p in pics)} macroblocks ({npcm} I_PCM), "
                  f"{'all agree' if not bad else 'disagree, e.g. ' + str(bad[:3])}")
        # corruptions
        ts, g = paths["qs_ragged"], good["qs_ragged"]
        pics, _ = analyse(ts, tempfile.mkdtemp(dir=td))
        p0 = pics[0]
        fs = yuvref.frame_bytes(p0.cw, p0.ch)
        raw = open(g, "rb").read()

        def variant(fn, name):
            b = bytearray(raw)
            fn(b)
            path = os.path.join(td, name)
            open(path, "wb").write(b)
            return path

        def flip(pos):
            def f(b):
                b[pos] ^= 0x01
            return f
        print("corrupted output must fail:")
        expect("one luma sample off by one (frame 1)", run(ts, variant(flip(fs + 5 * p0.cw + 9), "c1.yuv")), False)
        expect("one Cr sample off by one", run(ts, variant(flip(2 * fs + p0.cw * p0.ch + 64 * 3 + 5), "c2.yuv")), False)
        expect("truncated file (last picture missing)", run(ts, variant(lambda b: b.__delitem__(slice(len(b) - fs, None)), "c3.yuv")), False)
        expect("truncated file (7 bytes)", run(ts, variant(lambda b: b.__delitem__(slice(len(b) - 7, None)), "c4.yuv")), False)
        # mutants: the wrong rules must be exposed by the test streams
        print("deliberately wrong rules must be exposed by the test streams:")
        names = ("qs_mid", "qs_rand", "qs_wrap", "qs_pcm", "qs_single", "qs_rows", "qs_midrow", "qs_ragged", "qs_cip", "qs_big")
        reports = {}
        crash_only = []
        for mutant, why in MUTANTS:
            caught, where, crashed = False, "", []
            for name in names:          # prefer a stream where the mutant decodes and the pixel comparison rejects it
                m = os.path.join(td, "m.yuv")
                try:
                    decode(paths[name], m, mutant)
                except (ValueError, IndexError, KeyError, TypeError):      # the wrong rule asked for something that does not exist
                    crashed.append(name)
                    continue
                rep = run(paths[name], m)
                if rep.failures:
                    caught, where = True, name
                    reports[mutant] = "\n".join(rep.failures)
                    break
            if not caught and crashed:
                # the reference reader cannot even run with this rule (a QP outside 0..51, or a mode that needs a neighbour the
                # rule made unavailable).  A reader in C may not crash; it then writes wrong pixels, which the comparison catches.
                caught, where = True, ", ".join(crashed[:3]) + (f" (+{len(crashed) - 3} more)" if len(crashed) > 3 else "") + " ONLY BY CRASHING the reference reader (no stream decodes with this rule)"
                crash_only.append(mutant)
            ok &= caught
            print(f"  {'ok  ' if caught else 'BAD '} mutant {mutant}: {'rejected on ' + where if caught else 'NOT detected'}   [{why}]")
        if crash_only:
            print(f"  note: {len(crash_only)} mutant(s) ({', '.join(crash_only)}) make the reference reader raise an exception on every "
                  "stream, so their failure report is not exercised here; a reader that survives such a rule writes wrong samples, "
                  "which the byte comparison above always rejects")
        # and on real encodes
        for mutant, stream in (("qp_no_reset", "T4_crf30"), ("qp_slice_pic_init", "T4_crf30"), ("qp_nodelta_reset", "T2_crf30"),
                               ("avail_noslice", "T4_nodeblock"), ("avail_noslice_above", "T4_nodeblock"),
                               ("mp_avail_noslice", "T4_nodeblock")):
            m = os.path.join(td, "m.yuv")
            how = "rejected by the comparison"
            try:
                decode(paths[stream], m, mutant)
                caught = bool(run(paths[stream], m).failures)
            except (ValueError, IndexError, KeyError, TypeError):
                caught, how = True, "rejected only by crashing the reference reader"
            ok &= caught
            print(f"  {'ok  ' if caught else 'BAD '} mutant {mutant} on {stream}: {how if caught else 'NOT detected'}")
        # the failure report: hints must point at the right cause
        print("failure reports must carry the diagnosis:")
        for mutant, needles in (("qp_no_reset", ("FIRST macroblock of its slice", "slice boundary")),
                                ("qp_pcm_zero", ("I_PCM", "running QP")),
                                ("qp_clamp", ("wraps", "mod 52")),
                                ("cqp_cr_uses_cb", ("chroma QP",))):
            txt = reports.get(mutant, "")
            has = "first wrong macroblock in DECODING order" in txt and "SliceQP_Y" in txt and all(n in txt for n in needles)
            ok &= has
            print(f"  {'ok  ' if has else 'BAD '} {mutant}: report names the macroblock, its slice and QPs, and the hint ({'; '.join(needles)})")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stream", nargs="?")
    ap.add_argument("yuv", nargs="?")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    ffref.require_tools()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if not (a.stream and a.yuv):
        ap.error("need <in.ts|in.264> <reader_output.yuv>")
    if not os.path.isfile(a.stream):
        die(f"input {a.stream!r} not found")
    wd = tempfile.mkdtemp(prefix="qp55_")
    try:
        compare(a.stream, a.yuv, wd).finish()
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    main()
