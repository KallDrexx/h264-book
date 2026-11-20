#!/usr/bin/env python3
"""Harness for Chapter 7.4 (fractional-sample interpolation, chroma interpolation and the inter residual: P pictures, CAVLC,
no deblocking).

    python3 test_inter.py <input.ts|input.264> <your_output.yuv>     # compare your decoded frames with ffmpeg's
    python3 test_inter.py --make-variants <out_dir>                  # extra test inputs (see Readme)
    python3 test_inter.py --self-test                                # verify the harness itself (about a minute)

<your_output.yuv> = every decoded frame in output order, 8-bit planar yuv420p, CROPPED as the SPS says, no padding, no headers
(the Chapter 5.1 format).  P-only streams decode in display order, so "output order" = decoding order.

Truth: ffmpeg's bit-exact decode (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`), compared with common/yuvcmp.py: first
differing frame / plane / macroblock / pixel.  When the frames differ, the harness explains WHERE in the decoding process the
first wrong macroblock probably went wrong, using an independent reference decoder (spoiler_reference/: interdec.py, built on
common/inter_ref.py, the Chapter 7.3 predictor, the Phase 5 intra path and its own interpolation).  The diagnosis is advice, not
the verdict: the verdict is the byte comparison with ffmpeg.  Before it prints advice it checks that its own reference
reproduces ffmpeg's frame (otherwise it says so).

Exit 0 = identical, 1 = different, 2 = unusable input/output (missing file, stream this chapter cannot decode, ...).
"""
import argparse
import collections
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref, ffref, inter_ref as ir, testfiles, yuvcmp, yuvref  # noqa: E402
import interdec  # noqa: E402
import intergen  # noqa: E402
import mvpred_ref  # noqa: E402

SYNTH = ["synth_" + m for m in intergen.MODES]
VARIANTS = {   # name -> (codec options, rate control)
    "x264_inter_qp10": ("-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=3:no-deblock=1:subme=9", ["-qp", "10"]),
    "x264_inter_qp38": ("-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2:no-deblock=1", ["-qp", "38"]),
    "x264_inter_slices_cip": ("-profile:v baseline -bf 0 -x264-params keyint=40:partitions=all:ref=3:no-deblock=1:slices=4:"
                              "constrained-intra=1", ["-qp", "30"]),
    "x264_inter_main": ("-profile:v main -bf 0 -x264-params cabac=0:8x8dct=0:weightp=0:keyint=50:partitions=p8x8,p4x4,i4x4:"
                        "ref=2:no-deblock=1:me=umh:merange=32", ["-qp", "26"]),
    "x264_inter_crf": ("-profile:v baseline -bf 0 -x264-params keyint=25:partitions=all:ref=4:no-deblock=1:aq-mode=2", ["-crf", "28"]),
}
SRC = "testsrc2=size=352x288:rate=25"
CLASSES_HALF = {(2, 0), (0, 2)}
CLASSES_J = {(2, 2)}
CLASSES_JMIX = {(2, 1), (2, 3), (1, 2), (3, 2)}
CLASSES_QHV = {(1, 0), (3, 0), (0, 1), (0, 3)}
CLASSES_DIAG = {(1, 1), (1, 3), (3, 1), (3, 3)}
POS_NAME = {(0, 0): "G", (0, 1): "d", (0, 2): "h", (0, 3): "n", (1, 0): "a", (1, 1): "e", (1, 2): "i", (1, 3): "p",
            (2, 0): "b", (2, 1): "f", (2, 2): "j", (2, 3): "q", (3, 0): "c", (3, 1): "g", (3, 2): "k", (3, 3): "r"}


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ================================================================ inputs
def make_variant(name, out_ts):
    opts, rc = VARIANTS[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC, "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", *rc,
           *opts.split(), "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die(f"ffmpeg failed for {name}: {r.stderr.decode(errors='replace')[-300:]}")


def make_synth(name, out_264, seed=1):
    data, man = intergen.make_stream(name[len("synth_"):], seed)
    with open(out_264, "wb") as f:
        f.write(data)
    return man


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for n in VARIANTS:
        make_variant(n, os.path.join(out_dir, n + ".ts"))
        print("wrote", os.path.join(out_dir, n + ".ts"))
    for n in SYNTH:
        make_synth(n, os.path.join(out_dir, n + ".264"))
        print("wrote", os.path.join(out_dir, n + ".264"))
    for r in ("T5", "T6_nodeblock"):
        print("recipe", r, "->", testfiles.get(r))


def reference_yuv(path, out):
    ffref.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "yuv420p", out], timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")


# ================================================================ diagnosis
def read_frame(path, idx, fsize):
    with open(path, "rb") as f:
        f.seek(idx * fsize)
        return f.read(fsize)


def diagnose(path, ref_yuv, act_yuv, res, w, h, origin, workdir):
    """-> list of text lines explaining the first bad frame (or why no explanation is available)."""
    fd = res["first_diff"]
    frame = fd["frame"]
    L = []
    try:
        pics = interdec.decode(path, upto=frame + 1, record=True, workdir=workdir)
    except interdec.Unsupported as e:
        return [f"diagnosis not available: {e}"]
    except Exception as e:  # noqa: BLE001
        return [f"diagnosis not available: the reference decoder failed on this stream ({type(e).__name__}: {e})"]
    if len(pics) <= frame:
        return ["diagnosis not available: the reference decoder produced fewer pictures than ffmpeg"]
    pic = pics[frame]
    geo, fsize = yuvcmp.frame_geometry(w, h)
    exp = read_frame(ref_yuv, frame, fsize)
    act = read_frame(act_yuv, frame, fsize)
    mine = interdec.frame_bytes(pic)
    if mine != exp:
        L.append("WARNING: the harness's own reference decoder does not reproduce ffmpeg's frame %d exactly; "
                 "the advice below may be unreliable." % frame)
    x0, y0 = origin
    wmb = pic["w_mbs"]
    # wrong samples per plane -> macroblock address and prediction partition
    bad = collections.defaultdict(lambda: [0, 0, 0])             # addr -> [luma, cb, cr] wrong sample counts
    part_bad = collections.defaultdict(lambda: [0, 0])           # (addr, part index) -> [luma, chroma]
    part_of = {}

    def find(addr, lx, ly):
        mb = pic["mbs"].get(addr)
        if mb is None:
            return None
        for k, p in enumerate(mb["parts"]):
            if p["x"] <= lx < p["x"] + p["w"] and p["y"] <= ly < p["y"] + p["h"]:
                return k
        return None
    for pl, (name, pw, ph, off) in enumerate(geo):
        for y in range(ph):
            ra, rb = exp[off + y * pw: off + (y + 1) * pw], act[off + y * pw: off + (y + 1) * pw]
            if ra == rb:
                continue
            for x in range(pw):
                if ra[x] == rb[x]:
                    continue
                if pl == 0:
                    X, Y = x0 + x, y0 + y
                    addr, lx, ly = (Y // 16) * wmb + X // 16, X % 16, Y % 16
                else:
                    X, Y = x0 // 2 + x, y0 // 2 + y
                    addr, lx, ly = (Y // 8) * wmb + X // 8, 2 * (X % 8), 2 * (Y % 8)
                bad[addr][pl] += 1
                k = find(addr, lx, ly)
                if k is not None:
                    part_bad[(addr, k)][0 if pl == 0 else 1] += 1
    first = min(bad)
    mb = pic["mbs"].get(first)
    L.append(f"first wrong macroblock in raster order of frame {frame}: address {first} (x={first % wmb}, y={first // wmb}); "
             f"wrong samples there: Y {bad[first][0]}, Cb {bad[first][1]}, Cr {bad[first][2]}")
    if mb is not None:
        L.append(f"  type {mb['name']}, cbp {mb['cbp']} (luma bits {mb['cbp'] & 15:04b}, chroma {mb['cbp'] >> 4}), QP_Y {mb['qp']}")
        for k, p in enumerate(mb["parts"]):
            xf, yf = p["xf"], p["yf"]
            wb = part_bad.get((first, k), [0, 0])
            L.append(f"  partition {k}: {p['w']}x{p['h']} at ({p['x']},{p['y']}) ref_idx {p['ref']} mv ({p['mv'][0]},{p['mv'][1]}) "
                     f"quarter-sample position ({xf},{yf}) = '{POS_NAME[(xf, yf)]}', chroma frac ({p['mv'][0] & 7},{p['mv'][1] & 7})/8"
                     f"{', reference block outside the picture' if p['outside'] else ''}"
                     f"{', filter window reaches outside' if p['window_outside'] and not p['outside'] else ''}"
                     f" -> wrong samples Y {wb[0]}, chroma {wb[1]}")
        if not mb["parts"]:
            L.append("  (an intra macroblock: no inter prediction in it)")
    # ---- statistics over the whole first bad frame
    parts = []
    for a, m in pic["mbs"].items():
        for k, p in enumerate(m["parts"]):
            wb = part_bad.get((a, k), [0, 0])
            parts.append(dict(p, cbp=m["cbp"], lbad=wb[0], cbad=wb[1], addr=a))
    intra_bad = sum(1 for a in bad if not pic["mbs"].get(a, {}).get("parts"))
    inter_bad = {(p["addr"]) for p in parts if p["lbad"] or p["cbad"]}
    tab = collections.defaultdict(lambda: [0, 0, 0])             # luma class -> [partitions, wrong partitions, wrong samples]
    for p in parts:
        t = tab[(p["xf"], p["yf"])]
        t[0] += 1
        t[1] += 1 if p["lbad"] else 0
        t[2] += p["lbad"]
    L.append(f"frame {frame}: {len(parts)} inter partitions, wrong samples in {len(inter_bad)} inter and {intra_bad} intra macroblocks")
    L.append("  luma position class: partitions / partitions with wrong luma samples")
    row = []
    for yf in range(4):
        row.append("   " + "  ".join(f"{POS_NAME[(xf, yf)]}({xf},{yf}) {tab[(xf, yf)][0]:>3}/{tab[(xf, yf)][1]:<3}" for xf in range(4)))
    L += row
    # ---- hints
    H = []
    lbad_no = [p for p in parts if p["lbad"] and not p["cbp"] & 15]
    lgood_no = [p for p in parts if not p["lbad"] and not p["cbp"] & 15]
    lbad_res = [p for p in parts if p["lbad"] and p["cbp"] & 15]
    cbad = [p for p in parts if p["cbad"]]
    cl = lambda ps: {(p["xf"], p["yf"]) for p in ps}  # noqa: E731
    ok_classes = cl(lgood_no)
    if intra_bad and not inter_bad:
        H.append("only intra macroblocks are wrong: not an inter problem; check the intra path inside P slices "
                 "(neighbouring inter macroblocks count as available unless constrained_intra_pred_flag = 1; I_PCM; intra QP).")
    if lbad_no:
        bad_cls = cl(lbad_no)
        if bad_cls == {(0, 0)}:
            if all(p["outside"] for p in lbad_no) and any(not p["outside"] and (p["xf"], p["yf"]) == (0, 0) for p in lgood_no):
                H.append("only integer-position blocks that reach outside the picture are wrong: reference coordinate clamping "
                         "(Clip3(0, PicWidthInSamples-1, x), likewise y).")
            else:
                H.append("integer-position blocks are wrong: wrong reference picture (ref_idx / reference list order), wrong "
                         "integer part of the vector (mv >> 2), or the reference picture is not the final stored picture.")
        else:
            edge_only = all(p["window_outside"] for p in lbad_no) and any(
                (p["xf"], p["yf"]) in bad_cls and not p["window_outside"] for p in lgood_no)
            if edge_only:
                H.append("only blocks whose filter window reaches outside the picture are wrong while interior blocks of the same "
                         "positions are right: coordinate clamping (including off-by-one at the right/bottom edge).")
            if bad_cls & CLASSES_HALF:
                H.append("half-sample positions b / h are wrong: the 6-tap filter (1,-5,20,20,-5,1), its rounding (+16 >> 5) or Clip1.")
            elif bad_cls & CLASSES_J and not (bad_cls - CLASSES_J - CLASSES_JMIX):
                H.append("only positions using the centre sample j (j, f, i, k, q) are wrong: j must come from the UNROUNDED "
                         "intermediate values (j1 = filter over b1 or h1, j = Clip1((j1 + 512) >> 10)).")
            elif bad_cls & (CLASSES_JMIX | CLASSES_J) and not edge_only and not (bad_cls & (CLASSES_QHV | CLASSES_DIAG)):
                H.append("positions that average with j are wrong: check j (unrounded intermediates, +512 >> 10) and the averaging.")
            if bad_cls & CLASSES_DIAG and not (bad_cls & (CLASSES_HALF | CLASSES_J | CLASSES_JMIX | CLASSES_QHV)):
                H.append("diagonal quarter positions e, g, p, r are wrong: each averages TWO half samples (b or s with h or m); "
                         "check which pair belongs to which position.")
            elif bad_cls & (CLASSES_QHV | CLASSES_DIAG) and not (bad_cls & (CLASSES_HALF | CLASSES_J)) and not edge_only:
                H.append("quarter positions are wrong but the half positions b, h, j are right: the averaging step "
                         "((a + b + 1) >> 1, rounding UP) or the choice of the two samples.")
            if not H:
                H.append("fractional positions %s are wrong; compare your table of which samples are averaged with Table 8-12."
                         % ", ".join(sorted(POS_NAME[c] for c in bad_cls)))
    elif lbad_res and not cbad:
        H.append("luma is wrong only in macroblocks with luma residual while residual-free partitions are right: the inter "
                 "residual (all 16 coefficients including DC are dequantised like AC; QP_Y chain; transform; Clip1(pred + residual)).")
    elif lbad_res:
        H.append("luma is wrong only in macroblocks with luma residual (and chroma wrong too): check the residual path.")
    if cbad:
        no = [p for p in cbad if not p["cbp"] >> 4]
        if no and not lbad_no:
            H.append("chroma is wrong in macroblocks without chroma residual: chroma interpolation "
                     "(eighth-sample bilinear: weights (8-xF)(8-yF), xF(8-yF), (8-xF)yF, xF yF; +32 >> 6; vector units 1/8 chroma "
                     "sample = mv & 7 and mv >> 3; chroma block = half the luma partition size; clamping against the CHROMA size).")
        elif not lbad_no and not lbad_res:
            good_no = [p for p in parts if not p["cbad"] and not p["cbp"] >> 4]
            if good_no:
                H.append("chroma is wrong only where chroma residual exists: chroma DC/AC residual or the chroma QP "
                         "(QP_C from QP_Y + chroma_qp_index_offset, Table 8-15).")
        elif lbad_no:
            H.append("chroma is wrong as well: if luma is fixed first, chroma may follow.")
    if not H and inter_bad:
        H.append("no pattern recognised; compare this macroblock's prediction and residual step by step with the numbers above.")
    L.append("hints:")
    L += ["  - " + x for x in H] or ["  (none)"]
    return L


# ================================================================ main check
def run_check(path, yuv, quiet=False, workdir=None):
    """-> (exit code, text)."""
    td = workdir or tempfile.mkdtemp(prefix="inter_")
    out = []

    def say(s):
        out.append(s)
        if not quiet:
            print(s)
    try:
        infos = cavlc_ref.load_stream(path, td)
    except SystemExit as e:
        return 2, f"input problem: {e}"
    if not infos:
        return 2, "input problem: no slices in the stream"
    s0 = infos[0]
    sps = s0.sps
    w, h, x0, y0 = yuvref.cropped_size(
        s0.pic_width_in_mbs, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
        sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
        sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
    ref_yuv = os.path.join(td, "expected.yuv")
    reference_yuv(path, ref_yuv)
    if not os.path.isfile(yuv):
        return 2, f"your output {yuv!r} does not exist"
    res = yuvcmp.compare_yuv(ref_yuv, yuv, w, h, quiet=True, origin=(x0, y0))
    say(res["text"])
    if res["ok"]:
        say(f"PASS: Chapter 7.4: all {res['frames_expected']} frames byte-identical to ffmpeg")
        return 0, "\n".join(out)
    if any("does not exist" in p for p in res["size_problems"]):
        return 2, "\n".join(out)
    if res["first_diff"]:
        say("---- diagnosis (reference decoder in spoiler_reference/) ----")
        for line in diagnose(path, ref_yuv, yuv, res, w, h, (x0, y0), td):
            say(line)
    say("FAIL: Chapter 7.4: output differs from ffmpeg's decode")
    return 1, "\n".join(out)


# ================================================================ self-test
def self_test():
    td = tempfile.mkdtemp(prefix="inter_selftest_")
    problems = []

    def bad(msg):
        problems.append(msg)
        print("  PROBLEM:", msg)
    try:
        streams = []
        cov = collections.Counter()
        print("generating synthetic streams ...")
        for n in SYNTH:
            p = os.path.join(td, n + ".264")
            man = make_synth(n, p)
            cov.update(man["coverage"])
            streams.append(p)
        miss = intergen.missing_coverage(cov)
        print(f"  luma position coverage (16 positions x window inside / reaching outside): {32 - len(miss)}/32")
        if miss:
            bad(f"synthetic streams do not cover {miss}")
        print("recipes T5, T6_nodeblock and x264 variants ...")
        streams.append(testfiles.get("T5"))
        streams.append(testfiles.get("T6_nodeblock"))
        for n in ("x264_inter_qp10", "x264_inter_slices_cip", "x264_inter_main"):
            p = os.path.join(td, n + ".ts")
            make_variant(n, p)
            streams.append(p)
        expected = {}
        good = {}
        for s in streams:
            name = os.path.basename(s)
            wd = os.path.join(td, "w_" + name)
            os.makedirs(wd)
            ry = os.path.join(wd, "expected.yuv")
            reference_yuv(s, ry)
            expected[s] = ry
            interdec.MUTANT = None
            y = os.path.join(wd, "ref.yuv")
            interdec.write_yuv(interdec.decode(s, workdir=wd), y)
            good[s] = y
            code, text = run_check(s, y, quiet=True, workdir=wd)
            print(f"  reference decoder vs ffmpeg on {name}: {'identical' if code == 0 else 'DIFFERENT'}")
            if code != 0:
                bad(f"the reference decoder does not reproduce ffmpeg on {name}:\n{text[-600:]}")
        # ---- corrupted outputs must FAIL
        s = streams[0]
        data = bytearray(open(good[s], "rb").read())
        for label, mod in (("one luma sample changed", lambda d: d.__setitem__(3 * 96 * 64 * 3 // 2 + 700, d[3 * 96 * 64 * 3 // 2 + 700] ^ 1)),
                           ("last byte missing", lambda d: d.pop()),
                           ("one chroma sample changed", lambda d: d.__setitem__(2 * 96 * 64 * 3 // 2 + 96 * 64 + 5, d[2 * 96 * 64 * 3 // 2 + 96 * 64 + 5] ^ 4))):
            d = bytearray(data)
            mod(d)
            fp = os.path.join(td, "corrupt.yuv")
            open(fp, "wb").write(bytes(d))
            code, text = run_check(s, fp, quiet=True, workdir=td)
            print(f"  corrupted output ({label}): exit {code}")
            if code != 1:
                bad(f"corrupted output ({label}) not rejected (exit {code})")
        code, _ = run_check(s, os.path.join(td, "nonexistent.yuv"), quiet=True, workdir=td)
        print(f"  missing output file: exit {code}")
        if code != 2:
            bad("missing output must give exit 2")
        # ---- wrong rules (mutants) must FAIL on some stream, with the right advice
        expect_hint = {
            "taps": "6-tap", "taps_sign": "6-tap", "b_round": "6-tap",
            "j_from_rounded": "centre sample j", "j_from_clipped_b1": "centre sample j", "j_round": "centre sample j", "j_shift": "centre sample j",
            "avg_round_down": "averaging step", "diag_pair": "diagonal", "diag_pair_r": "diagonal",
            "clamp_off_by_one": "clamping", "no_clamp": "clamping",
            "chroma_mv_units": "chroma", "chroma_swap_frac": "chroma", "chroma_swap_bc": "chroma", "chroma_round": "chroma",
            "inter_dc_prescaled": "residual", "no_final_clip": "residual", "qp_slice_only": "residual",
            "chroma_qp_luma": "chroma residual",
        }
        mutants = list(interdec.INTERP_MUTANTS) + list(interdec.DECODER_MUTANTS)
        print(f"{len(mutants)} deliberately wrong rules, each must be rejected by at least one stream:")
        synth_streams = streams[:len(SYNTH)]
        for m in mutants:
            caught, hint_ok, hints = [], None, ""
            for s in synth_streams:
                wd = os.path.join(td, "w_" + os.path.basename(s))
                y = os.path.join(wd, "mut.yuv")
                interdec.MUTANT = m
                try:
                    interdec.write_yuv(interdec.decode(s, workdir=wd), y)
                except interdec.Unsupported:
                    continue
                finally:
                    interdec.MUTANT = None
                code, text = run_check(s, y, quiet=True, workdir=wd)
                if code == 1:
                    caught.append(os.path.basename(s)[6:-4])
                    if m in expect_hint:
                        hint_ok = expect_hint[m] in text.split("hints:")[-1]
                        if not hint_ok:
                            hints = text.split("hints:")[-1].split("FAIL:")[0].strip()[:300]
                    break                                  # one catching stream is enough (keeps the self-test short)
            tag = f"caught by {', '.join(caught)}" if caught else "NOT CAUGHT"
            if m in expect_hint:
                tag += "; advice " + ("ok" if hint_ok else "MISSING '%s'" % expect_hint[m])
            print(f"  {m:20s} {tag}")
            if not caught:
                bad(f"wrong rule {m!r} was not detected by any synthetic stream")
            elif m in expect_hint and not hint_ok:
                bad(f"wrong rule {m!r}: diagnosis does not mention {expect_hint[m]!r}; hints were: {hints}")
        # ---- a T5-sized check of the whole pipeline on the real recipe through the CLI path
        code, _ = run_check(streams[len(SYNTH)], good[streams[len(SYNTH)]], quiet=True, workdir=td)
        if code != 0:
            bad("T5 with the reference decoder's output did not pass")
    finally:
        interdec.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    if problems:
        print(f"SELF-TEST FAIL: {len(problems)} problem(s)")
        return 1
    print("SELF-TEST PASS")
    return 0


def stream_summary(path, td):
    """One line about what the stream exercises (partition shapes, fractional positions)."""
    try:
        infos = ir.load(path, td)
        results = ir.parse_all(infos)
        mvpred_ref.fill_results(results)
    except Exception:  # noqa: BLE001
        return None
    names, pos, outside = collections.Counter(), collections.Counter(), 0
    for r in results:
        for rec in r.mbs:
            names["I_16x16" if rec.f["name"].startswith("I_16x16") else rec.f["name"]] += 1
            if rec.grid:
                for p in (rec.parts or [dict(x=0, y=0)]):
                    _, mx, my = rec.grid[(p["y"] >> 2) * 4 + (p["x"] >> 2)]
                    pos[(mx & 3, my & 3)] += 1
    return (f"stream: {sum(names.values())} macroblocks ("
            + ", ".join(f"{k} {v}" for k, v in sorted(names.items())) + f"); {len(pos)} of 16 luma positions used")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="input .ts / .264 (P-picture CAVLC stream without deblocking)")
    ap.add_argument("output", nargs="?", help="your decoded frames, raw yuv420p, cropped")
    ap.add_argument("--make-variants", metavar="DIR", help="write extra test inputs into DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if not a.input or not a.output:
        ap.print_usage(sys.stderr)
        die("error: need <input> and <your_output.yuv>")
    if not os.path.isfile(a.input):
        die(f"error: input file {a.input!r} not found")
    td = tempfile.mkdtemp(prefix="inter_")
    try:
        code, _ = run_check(a.input, a.output, workdir=td)
        if code == 0:
            line = stream_summary(a.input, td)
            if line:
                print(line)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
