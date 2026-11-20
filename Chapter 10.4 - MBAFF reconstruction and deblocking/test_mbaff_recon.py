#!/usr/bin/env python3
"""Chapter 10.4 harness: MBAFF reconstruction and deblocking (whole-stream pixels).

  python3 test_mbaff_recon.py STREAM READER_YUV [--nodeblock-yuv F] [--mv-dump F]    check one stream
  python3 test_mbaff_recon.py --expected STREAM OUTDIR      write the truth files (<name>.yuv, <name>.nodeblock.yuv, <name>.mv)
  python3 test_mbaff_recon.py --make DIR                    write the suite (synthetic + x264 MBAFF streams + T12) to DIR
  python3 test_mbaff_recon.py --check DIR OUTDIR [--only a,b]   check OUTDIR/<case>.yuv (+ .nodeblock.yuv, .mv) of every DIR/<case>
  python3 test_mbaff_recon.py --suite DIR --reader './mydec {stream} {yuv} {nodeblock} {mv}' [--only a,b]
  python3 test_mbaff_recon.py --stats STREAM                macroblock-pair statistics (field / frame pairs per picture type)
  python3 test_mbaff_recon.py --self-test                   check the harness and the reference (takes about five minutes)
Exit 0 pass, 1 fail, 2 missing / malformed reader output (yuv not a whole number of frames, bad vector dump line) or no usable truth.  Needs ffmpeg + libx264; Python 3 stdlib only.

Channels.  A: the whole decoded stream in output order against `ffmpeg -threads 1` (cropped yuv420p).  B (optional): the same
stream decoded with the deblocking filter OFF EVERYWHERE (also in the pictures used as references) against
`ffmpeg -skip_loop_filter all`: reconstruction without deblocking.  C (optional): final motion vectors per 4x4 block, text grammar of
the Readme, against spoiler_reference/mbdec.py (itself cross-checked with the vectors libavcodec exports).
Truth: ffmpeg decodes every suite stream; spoiler_reference/mbdec.py (an independent decoder) reproduces ffmpeg byte for byte on the
whole suite (--self-test proves it) and supplies what ffmpeg cannot: macroblock-pair kinds for the diagnosis and the vector dump.
"""
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref as CR, ffref, testfiles, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import mbaff as M  # noqa: E402
import mbdec  # noqa: E402
import mbgen  # noqa: E402

SOURCES = {
    "move": "testsrc2=size=176x144:rate=50,scroll=vertical=0.007:horizontal=0.011,format=yuv420p,tinterlace=mode=interleave_top,fps=25",
    "move2": "testsrc2=size=176x144:rate=50,scroll=vertical=0.013,format=yuv420p,tinterlace=mode=interleave_top,fps=25",
    "noise": "testsrc2=size=176x144:rate=50,scroll=vertical=0.01:horizontal=0.02,noise=alls=25:allf=t,format=yuv420p,"
             "tinterlace=mode=interleave_top,fps=25",
    "still": "testsrc2=size=176x144:rate=25,format=yuv420p,trim=end_frame=1,loop=loop=-1:size=1,tinterlace=mode=interleave_top",
    "mix": "mandelbrot=size=176x144:rate=50,format=yuv420p,tinterlace=mode=interleave_top,fps=25",
    "life": "cellauto=size=176x144:rate=50:rule=110:random_fill_ratio=0.3:scroll=1,format=yuv420p,tinterlace=mode=interleave_top,fps=25",
    "crop": "testsrc2=size=160x120:rate=50,scroll=vertical=0.02:horizontal=0.01,noise=alls=12:allf=t,format=yuv420p,"
            "tinterlace=mode=interleave_bottom,fps=25",
    "bars": "smptebars=size=176x144:rate=50,scroll=vertical=0.03,noise=alls=40:allf=t,format=yuv420p,tinterlace=mode=interleave_top,fps=25",
}
# name -> (source, ffmpeg profile args, x264 params, rate control)
X264 = {
    "x_cabac_b": ("move", "-profile:v high", "interlaced=1:tff=1:ref=3:bframes=3:b-pyramid=normal", ["-qp", "26"]),
    "x_cavlc_main": ("move2", "-profile:v main", "interlaced=1:tff=1:cabac=0:bframes=2", ["-qp", "28"]),
    "x_cavlc_high": ("move", "-profile:v high", "interlaced=1:tff=1:cabac=0:ref=2", ["-qp", "24"]),
    "x_slices": ("move2", "-profile:v high", "interlaced=1:tff=1:slices=3:bframes=0:aq-mode=2", ["-crf", "27"]),
    "x_still": ("still", "-profile:v high", "interlaced=1:tff=1:bframes=3", ["-qp", "30"]),
    "x_temporal": ("noise", "-profile:v high", "interlaced=1:tff=1:direct=temporal:bframes=3:ref=3", ["-qp", "27"]),
    "x_temporal_cavlc": ("move", "-profile:v main", "interlaced=1:tff=1:direct=temporal:cabac=0:bframes=2", ["-qp", "26"]),
    "x_weightp": ("move", "-profile:v high", "interlaced=1:tff=1:weightp=2:bframes=2:ref=2", ["-qp", "28"]),
    "x_bff": ("noise", "-profile:v high", "interlaced=1:bff=1:bframes=3:b-pyramid=strict", ["-qp", "24"]),
    "x_noise_hi": ("noise", "-profile:v high", "interlaced=1:tff=1:bframes=3:ref=4", ["-qp", "18"]),
    "x_noise_lo": ("noise", "-profile:v high", "interlaced=1:tff=1:bframes=2:ref=2:8x8dct=0", ["-qp", "38"]),
    "x_life": ("life", "-profile:v high", "interlaced=1:tff=1:bframes=3:me=umh:subme=9", ["-qp", "24"]),
    "x_slices_cavlc": ("noise", "-profile:v main", "interlaced=1:tff=1:slices=4:cabac=0:bframes=2", ["-qp", "30"]),
    "x_deblock_off": ("move", "-profile:v high", "interlaced=1:tff=1:no-deblock=1:bframes=2", ["-qp", "28"]),
    "x_deblock_hi": ("noise", "-profile:v high", "interlaced=1:tff=1:deblock=3\\:3:bframes=2", ["-qp", "32"]),
    "x_crop_temporal": ("crop", "-profile:v high", "interlaced=1:bff=1:bframes=3:b-pyramid=normal:ref=4:direct=temporal", ["-qp", "25"]),
    "x_multiref_mix": ("bars", "-profile:v high", "interlaced=1:tff=1:bframes=4:b-adapt=2:ref=5:direct=auto:weightb=1", ["-qp", "22"]),
    "x_cavlc_weightb": ("bars", "-profile:v main", "interlaced=1:tff=1:cabac=0:bframes=3:weightb=1:ref=3", ["-qp", "29"]),
    "x_slices_temporal": ("bars", "-profile:v high", "interlaced=1:tff=1:slices=5:bframes=2:direct=temporal:ref=3", ["-qp", "31"]),
}
QUICK = ["x_cabac_b", "x_temporal", "x_cavlc_main", "x_weightp", "x_slices", "x_bff"]      # short versions used by --self-test


def die(msg, code=2):
    print(msg)
    sys.exit(code)


# ================================================================ suite
def make_x264(name, path, secs=1.0):
    src, prof, params, rc = X264[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SOURCES[src], "-t", str(secs), "-pix_fmt", "yuv420p", "-c:v", "libx264",
           *rc, *prof.split(), "-x264-params", params, "-f", "h264", path + ".tmp"]
    r = ffref.run(cmd, timeout=300)
    if r.returncode != 0:
        die(f"ffmpeg/x264 failed for {name}: {r.stderr.decode()[-300:]}")
    os.replace(path + ".tmp", path)


def ffmpeg_warnings(path):
    r = ffref.run(["ffmpeg", "-v", "warning", "-i", path, "-f", "null", "-"], timeout=120)
    return r.stderr.decode(errors="replace").strip()


def make_suite(d, only=None, secs=1.0):
    os.makedirs(d, exist_ok=True)
    names = []
    for n in mbgen.SCENARIOS:
        if only is None or n in only:
            data, _, _ = mbgen.build(n)
            p = os.path.join(d, n + ".264")
            open(p, "wb").write(data)
            w = ffmpeg_warnings(p)
            if w:
                die(f"synthetic stream {n} is not clean in ffmpeg: {w[:200]}")
            names.append(n)
    for n in X264:
        if only is None or n in only:
            make_x264(n, os.path.join(d, n + ".264"), secs)
            names.append(n)
    if only is None or "T12" in only:
        shutil.copy(testfiles.get("T12"), os.path.join(d, "T12.ts"))
        names.append("T12")
    return names


def find_stream(d, name):
    for ext in (".264", ".ts"):
        p = os.path.join(d, name + ext)
        if os.path.exists(p):
            return p
    return None


# ================================================================ truth
def ffmpeg_yuv(path, out, skip_loop=False, timeout=600):
    cmd = ["ffmpeg", "-v", "error", "-y", "-threads", "1"] + (["-skip_loop_filter", "all"] if skip_loop else []) + \
          ["-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    r = ffref.run(cmd, timeout=timeout)
    if r.returncode != 0 or not os.path.exists(out):
        die(f"ffmpeg could not decode {path}: {r.stderr.decode(errors='replace')[-300:]}")
    return out


class Truth:
    """Everything the harness knows about a stream: geometry, the reference parse (macroblock pair kinds per output frame)."""

    def __init__(self, path, td, cache=None):
        self.path, self.td = path, td
        try:
            self.parsed = mbdec.load_parsed(path, td, cache)
        except mbdec.Unsupported as e:
            die(f"{path}: the reference decoder cannot handle this stream: {e}")
        if not self.parsed:
            die(f"{path}: no MBAFF slice in this stream (needs mb_adaptive_frame_field_flag 1, frame pictures)")
        info = self.parsed[0][0]
        sps = info.sps
        self.wm = info.pic_width_in_mbs
        self.hm = info.pic_size_in_mbs // self.wm
        self.crop = yuvref.cropped_size(self.wm, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
                                        sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
                                        sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
        pics = {}
        for item in self.parsed:
            pics.setdefault(item[0].picture, []).append(item)
        order = sorted(pics)
        try:
            meta, out_order = mbdec.picture_meta(pics, order)
        except mbdec.Unsupported as e:
            die(f"{path}: {e}")
        self.frames = []                 # output order: dict(fld=[pair -> 0/1], types=letters)
        for pi in out_order:
            fld = {}
            types = ""
            for info_, tab, sl, recs in pics[pi]:
                types += sl.t
                for r in recs:
                    if r.addr % 2 == 0:
                        fld[r.addr // 2] = r.fld
            self.frames.append(dict(fld=fld, types=types))

    def pair_stats(self):
        out = {}
        for f in self.frames:
            t = f["types"][0]
            a = out.setdefault(t, [0, 0])
            a[0] += sum(f["fld"].values())
            a[1] += len(f["fld"]) - sum(f["fld"].values())
        return out


def stats_text(tr):
    st = tr.pair_stats()
    parts = [f"{t}: {a[0]} field / {a[1]} frame pairs" for t, a in sorted(st.items())]
    tot_f = sum(a[0] for a in st.values())
    tot = sum(a[0] + a[1] for a in st.values())
    return f"{len(tr.frames)} pictures, {tot_f} of {tot} pairs are field pairs ({100 * tot_f // max(tot, 1)}%); " + "; ".join(parts)


# ================================================================ diagnosis
def frame_samples(data, W, H, fi):
    fs = W * H + 2 * ((W + 1) // 2) * ((H + 1) // 2)
    return data[fi * fs:(fi + 1) * fs], fs


def diagnose(exp_path, act_path, tr, label):
    """-> list of text lines explaining the first wrong frame in MBAFF terms."""
    W, H, x0, y0 = tr.crop
    cw, ch = (W + 1) // 2, (H + 1) // 2
    ed, ad = open(exp_path, "rb").read(), open(act_path, "rb").read()
    fs = W * H + 2 * cw * ch
    out = []
    n = min(len(ed), len(ad)) // fs
    fi = next((i for i in range(n) if ed[i * fs:(i + 1) * fs] != ad[i * fs:(i + 1) * fs]), None)
    if fi is None:
        return out
    e, a = ed[fi * fs:(fi + 1) * fs], ad[fi * fs:(fi + 1) * fs]
    luma, chroma = [], []
    for i in range(W * H):
        if e[i] != a[i]:
            luma.append((i % W + x0, i // W + y0))
    for pl in range(2):
        off = W * H + pl * cw * ch
        for i in range(cw * ch):
            if e[off + i] != a[off + i]:
                chroma.append((2 * (i % cw) + x0, 2 * (i // cw) + y0))
    fmap = tr.frames[fi]["fld"] if fi < len(tr.frames) else {}
    wm = tr.wm

    def describe(X, Y):
        pair = (Y // 32) * wm + X // 16
        fld = fmap.get(pair)
        rr = Y % 32
        kind = "?" if fld is None else "field" if fld else "frame"
        if fld:
            where = f"{'bottom' if rr & 1 else 'top'} field MB (line {rr} of the pair = field line {rr >> 1})"
        else:
            where = f"{'bottom' if rr >= 16 else 'top'} frame MB (line {rr % 16})"
        return pair, kind, where
    pts = luma or chroma
    X, Y = pts[0]
    pair, kind, where = describe(X, Y)
    plane = "luma" if luma else "chroma"
    out.append(f"{label}: first wrong picture = output frame {fi} (types {tr.frames[fi]['types'] if fi < len(tr.frames) else '?'}), "
               f"{len(luma)} luma and {len(chroma)} chroma samples differ")
    out.append(f"  first wrong {plane} sample at luma position ({X}, {Y}): macroblock pair {pair} (column {pair % wm}, row {pair // wm}), "
               f"a {kind} pair, {where}")
    if luma:
        kinds = {"field": 0, "frame": 0, "?": 0}
        par = [0, 0]
        edge = 0
        for (X, Y) in luma:
            p, k, _ = describe(X, Y)
            kinds[k] += 1
            rr = Y % 32
            if k == "field":
                par[rr & 1] += 1
            xm = X % 16
            near_v = xm < 3 or xm > 12
            fl = fmap.get((Y // 32) * wm + X // 16)
            ym = ((rr >> 1) if fl else (rr % 16))
            near_h = ym < 3 or ym > 12
            edge += 1 if (near_v or near_h) else 0
        if kinds["frame"] == 0 and kinds["field"]:
            out.append("  hint: every wrong sample is in a FIELD pair: look at what is specific to field macroblocks (line interleave, "
                       "reference field choice, vector scaling, chroma offset, field scan, mixed edges)")
        elif kinds["field"] == 0 and kinds["frame"]:
            out.append("  hint: every wrong sample is in a FRAME pair (but the pair may touch a field pair: neighbour derivation, "
                       "mixed edges, direct modes with a field co-located pair)")
        if kinds["field"] and (par[0] == 0 or par[1] == 0):
            out.append(f"  hint: in field pairs only the {'top' if par[1] == 0 else 'bottom'} field lines (even / odd lines) are wrong: "
                       f"parity of one field MB (reference field parity, line mapping)")
        if edge == len(luma):
            out.append("  hint: all wrong luma samples lie within 3 samples of a macroblock edge: deblocking of macroblock edges "
                       "(mixed frame / field edges, bS, line mapping of the left edge, double top edge filtering)")
    elif chroma and not luma:
        out.append("  hint: luma is right, only chroma differs: chroma vector offset for field MBs, chroma QP, chroma deblocking lines")
    return out


# ================================================================ comparison
def compare_yuv_file(exp, act, tr, quiet=True):
    W, H, x0, y0 = tr.crop
    r = yuvcmp.compare_yuv(exp, act, W, H, quiet=True, origin=(x0, y0))
    # yuvcmp numbers macroblocks in raster order; MBAFF addresses are pair based, so keep only the (x, y) position
    r["text"] = re.sub(r" = address \d+", "", r["text"])
    return r


MV_CELL = re.compile(r"^(-|\d+:-?\d+,-?\d+)$")


def validate_outputs(tr, yuv, nodeblock=None, mvdump=None):
    """Exit 2 (malformed reader output) unless every file exists and follows the grammar of Readme section 5."""
    W, H, _, _ = tr.crop
    fs = W * H + 2 * ((W + 1) // 2) * ((H + 1) // 2)
    for label, f in (("reader yuv", yuv), ("reader nodeblock yuv", nodeblock)):
        if f is None:
            continue
        if not os.path.isfile(f):
            die(f"{label} {f!r} not found")
        n = os.path.getsize(f)
        if n == 0:
            die(f"{label} {f!r} is empty")
        if n % fs:
            die(f"{label} {f!r} is malformed: {n} bytes is not a whole number of {W}x{H} yuv420p frames ({fs} bytes each; "
                f"{n // fs} whole frames + {n % fs} stray bytes): wrong cropping, padding, or a truncated write?")
    if mvdump is not None:
        if not os.path.isfile(mvdump):
            die(f"reader motion vector dump {mvdump!r} not found")
        for i, ln in enumerate(open(mvdump, errors="replace"), 1):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            t = ln.split(" ")
            good = len(t) >= 5 and t[0] == "MV" and t[1].isdigit() and t[2].isdigit() and t[3] in ("fld=0", "fld=1")
            if good and len(t) == 5:
                good = t[4] == "intra"
            elif good:
                good = len(t) == 6 and t[4].startswith("l0=") and t[5].startswith("l1=") and all(
                    len(c) == 16 and all(MV_CELL.match(x) for x in c) for c in (t[4][3:].split("/"), t[5][3:].split("/")))
            if not good:
                die(f"reader motion vector dump {mvdump!r} line {i} is malformed: {ln[:120]!r}\n"
                    f"  expected 'MV <frame> <addr> fld=<0|1> intra' or 'MV <frame> <addr> fld=<0|1> l0=<16 cells> l1=<16 cells>', "
                    f"cells '-' or '<refIdx>:<mvx>,<mvy>' separated by '/'")


def check_files(path, td, tr, yuv, nodeblock=None, mvdump=None, rep=None, mv_truth=None, quiet=False):
    """Run the channels; returns (Report, list of extra text lines)."""
    name = os.path.basename(path)
    rep = rep or Report(name, max_shown=12)
    notes = []
    validate_outputs(tr, yuv, nodeblock, mvdump)
    truth_a = ffmpeg_yuv(path, os.path.join(td, "truth_a.yuv"))
    ra = compare_yuv_file(truth_a, yuv, tr)
    rep.check(ra["ok"], f"channel A (decoded stream vs ffmpeg):\n    " + ra["text"].replace("\n", "\n    "))
    if not ra["ok"]:
        notes += diagnose(truth_a, yuv, tr, "A")
        # does the output look like a decode without deblocking?
        truth_b0 = ffmpeg_yuv(path, os.path.join(td, "truth_b0.yuv"), skip_loop=True)
        if compare_yuv_file(truth_b0, yuv, tr)["ok"]:
            notes.append("A: your output equals the decode WITHOUT deblocking (ffmpeg -skip_loop_filter all): the loop filter is not applied")
    if nodeblock is not None:
        truth_b = os.path.join(td, "truth_b.yuv")
        if not os.path.exists(truth_b):
            ffmpeg_yuv(path, truth_b, skip_loop=True)
        rb = compare_yuv_file(truth_b, nodeblock, tr)
        rep.check(rb["ok"], f"channel B (no deblocking vs ffmpeg -skip_loop_filter all):\n    " + rb["text"].replace("\n", "\n    "))
        if not rb["ok"]:
            notes += diagnose(truth_b, nodeblock, tr, "B")
            notes.append("B: the reconstruction itself is wrong before any deblocking (prediction / residual / vectors)")
        elif not ra["ok"]:
            notes.append("A / B: the reconstruction without deblocking is byte-exact but the deblocked output is not: the deblocking filter is wrong")
    if mvdump is not None:
        truth = mv_truth if mv_truth is not None else mbdec.mv_dump_lines(mbdec.decode(path, workdir=td))
        mine = [ln.strip() for ln in open(mvdump) if ln.strip() and not ln.startswith("#")]
        bad = next((i for i, (a, b) in enumerate(zip(truth, mine)) if a != b), None)
        if bad is None and len(truth) != len(mine):
            bad = min(len(truth), len(mine))
        msg = ""
        if bad is not None:
            msg = f"line {bad + 1}: expected {truth[bad][:110] if bad < len(truth) else '(end)'!r}, got {mine[bad][:110] if bad < len(mine) else '(end)'!r}"
            if bad < len(truth) and bad < len(mine):
                te, tm = truth[bad].split(" "), mine[bad].split(" ")
                if te[:3] != tm[:3]:
                    msg += f"; expected the line of output frame {te[1]} macroblock {te[2]} (a line is missing, extra or out of order)"
                elif te[3] != tm[3]:
                    msg += f"; output frame {te[1]} macroblock {te[2]}: field flag expected {te[3]}, got {tm[3]}"
                elif (te[4] == "intra") != (tm[4] == "intra"):
                    msg += f"; output frame {te[1]} macroblock {te[2]}: expected {'intra' if te[4] == 'intra' else 'inter'}"
                else:
                    for li in (4, 5):
                        ce, cm = te[li][3:].split("/"), tm[li][3:].split("/")
                        c = next((j for j in range(16) if ce[j] != cm[j]), None)
                        if c is not None:
                            msg += (f"; output frame {te[1]} macroblock {te[2]} ({'field' if te[3] == 'fld=1' else 'frame'} MB), list {li - 4}, "
                                    f"4x4 block x={c % 4} y={c // 4}: expected {ce[c]}, got {cm[c]}")
                            break
            msg += f"; {len(truth)} expected lines, {len(mine)} read"
        rep.check(bad is None, "channel C (motion vectors): " + msg)
    return rep, notes


def check_stream(path, yuv, nodeblock=None, mvdump=None):
    td = tempfile.mkdtemp(prefix="mbrecon_")
    try:
        tr = Truth(path, td)
        rep, notes = check_files(path, td, tr, yuv, nodeblock, mvdump)
        print(f"{os.path.basename(path)}: {stats_text(tr)}")
        return rep, notes
    finally:
        shutil.rmtree(td, ignore_errors=True)


def write_expected(path, outdir):
    os.makedirs(outdir, exist_ok=True)
    td = tempfile.mkdtemp(prefix="mbrecon_")
    try:
        base = os.path.join(outdir, os.path.splitext(os.path.basename(path))[0])
        tr = Truth(path, td)
        ffmpeg_yuv(path, base + ".yuv")
        ffmpeg_yuv(path, base + ".nodeblock.yuv", skip_loop=True)
        open(base + ".mv", "w").write("\n".join(mbdec.mv_dump_lines(mbdec.decode(path, workdir=td))) + "\n")
        print(f"wrote {base}.yuv, {base}.nodeblock.yuv, {base}.mv  ({stats_text(tr)})")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run_suite(d, outdir, only=None, reader=None):
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith((".264", ".ts")))
    if only:
        names = [n for n in names if n in only]
    if not names:
        die("no streams to check")
    bad = malformed = 0
    for n in names:
        path = find_stream(d, n)
        yuv, nb, mv = (os.path.join(outdir, n + s) for s in (".yuv", ".nodeblock.yuv", ".mv"))
        if reader:
            cmd = reader.format(stream=path, yuv=yuv, nodeblock=nb, mv=mv)
            os.makedirs(outdir, exist_ok=True)
            r = subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=3600)
            if r.returncode != 0:
                print(f"{n}: reader exited with {r.returncode}: {r.stderr.decode(errors='replace')[-200:]}")
        if not os.path.exists(yuv):
            print(f"{n}: reader output {yuv} missing")
            print(f"MALFORMED {n}")
            malformed += 1
            continue
        try:
            rep, notes = check_stream(path, yuv, nb if os.path.exists(nb) else None, mv if os.path.exists(mv) else None)
        except SystemExit as e:                  # malformed output of this stream (message already printed): go on with the rest
            if e.code != 2:
                raise
            print(f"MALFORMED {n}")
            malformed += 1
            continue
        for m in rep.failures[:3]:
            print("  FAIL:", m)
        for t in notes:
            print("  " + t)
        print(f"{'PASS' if not rep.failures else 'FAIL'} {n}")
        bad += 1 if rep.failures else 0
    print(f"{len(names) - bad - malformed}/{len(names)} streams pass" + (f", {malformed} with malformed output" if malformed else ""))
    return 2 if malformed else 0 if bad == 0 else 1


# ================================================================ self-test
def selftest_streams(d):
    names = []
    for n in mbgen.SCENARIOS:
        data, _, _ = mbgen.build(n)
        open(os.path.join(d, n + ".264"), "wb").write(data)
        names.append(n)
    for n in QUICK:
        make_x264(n, os.path.join(d, n + ".264"), 0.5)
        names.append(n)
    return names


def selftest(stages=(1, 2, 3, 4, 5), mslice=(0, 1)):
    t0 = time.time()
    td = tempfile.mkdtemp(prefix="mbrecon_self_")
    problems = []

    def ok(cond, msg):
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            problems.append(msg)
    try:
        names = selftest_streams(td)
        truth, caches = {}, {}
        for n in names + ["T12"]:
            path = os.path.join(td, n + ".264") if n != "T12" else testfiles.get("T12")
            caches[n] = {}
            truth[n] = (path, ffmpeg_yuv(path, os.path.join(td, n + ".ffA.yuv")), ffmpeg_yuv(path, os.path.join(td, n + ".ffB.yuv"), skip_loop=True))
        # ---- 1. the reference decoder reproduces ffmpeg on every stream, with and without the loop filter
        if 1 in stages:
            print("1. reference decoder vs ffmpeg (pictures with and without deblocking)")
            totals = {"field": 0, "frame": 0}
            for n in names + ["T12"]:
                path, a, b = truth[n]
                st = {}
                frs = mbdec.decode(path, workdir=td, cache=caches[n], stats=st)
                ra = b"".join(mbdec.frame_bytes(f) for f in frs) == open(a, "rb").read()
                frs_b = mbdec.decode(path, workdir=td, cache=caches[n], deblock=False)
                rb = b"".join(mbdec.frame_bytes(f) for f in frs_b) == open(b, "rb").read()
                nf = sum(1 for f in frs for m in f.mbs.values() if m.fld and m.addr % 2 == 0)
                nr = sum(1 for f in frs for m in f.mbs.values() if not m.fld and m.addr % 2 == 0)
                totals["field"] += nf
                totals["frame"] += nr
                viol = st.get("coefficient range violation", 0)
                ok(ra and rb and not viol, f"{n}: A {'equal' if ra else 'DIFFERENT'}, B {'equal' if rb else 'DIFFERENT'}, {nf} field / {nr} frame pairs"
                   + (f", {viol} range violations" if viol else ""))
            print(f"   suite total: {totals['field']} field pairs, {totals['frame']} frame pairs")
        # ---- 2. the harness itself
        if 2 in stages:
            print("2. harness checks on corrupted reader outputs")
            n = "x_cabac_b"
            path, a, b = truth[n]
            tr = Truth(path, td, caches[n])
            print("   " + stats_text(tr))
            good = open(a, "rb").read()
            W, H, x0, y0 = tr.crop
            fs = W * H * 3 // 2

            def attempt(label, data, expect_pass, must=(), nodeblock=None, mv=None):
                p = os.path.join(td, "reader.yuv")
                open(p, "wb").write(data)
                rep, notes = check_files(path, td, tr, p, nodeblock, mv)
                text = "\n".join(rep.failures + notes)
                passed = not rep.failures
                ok(passed == expect_pass and all(m in text for m in must), f"harness: {label} -> {'PASS' if passed else 'FAIL'}"
                   + ("" if all(m in text for m in must) else f" (missing {[m for m in must if m not in text]})"))

            def attempt_malformed(label, data, mv_text=None):
                p = os.path.join(td, "reader.yuv")
                open(p, "wb").write(data)
                pm = None
                if mv_text is not None:
                    pm = os.path.join(td, "reader_bad.mv")
                    open(pm, "w").write(mv_text)
                code = None
                try:
                    check_files(path, td, tr, p, None, pm)
                except SystemExit as e:
                    code = e.code
                ok(code == 2, f"harness: {label} -> exit {code} (want 2)")
            attempt("identical output", good, True)
            attempt_malformed("truncated file (not a whole number of frames)", good[:-fs // 2])
            attempt_malformed("empty file", b"")
            attempt("one frame missing", good[:-fs], False, ("frame count",))
            attempt("frame 1 missing (later frames shifted)", good[:fs] + good[2 * fs:], False, ("output frame 1",))
            fmap = tr.frames[3]["fld"]
            fp = next(p for p, v in fmap.items() if v)
            px, py = (fp % tr.wm) * 16 + 5, (fp // tr.wm) * 32 + 9          # luma sample in the odd lines of a field pair
            bad = bytearray(good)
            bad[3 * fs + py * W + px] ^= 1
            attempt("one luma sample off by one in a field pair", bytes(bad), False, ("a field pair", "bottom field MB"))
            bad = bytearray(good)
            for yy in range(32):
                for xx in range(16):
                    o = 3 * fs + ((fp // tr.wm) * 32 + yy) * W + (fp % tr.wm) * 16 + xx
                    bad[o] = good[3 * fs + ((fp // tr.wm) * 32 + (yy ^ 1)) * W + (fp % tr.wm) * 16 + xx]
            attempt("field lines of one pair exchanged", bytes(bad), False, ("FIELD pair",))
            nb = open(truth[n][2], "rb").read()
            attempt("decoder that never deblocks (A)", nb, False, ("loop filter is not applied",))
            p_nb = os.path.join(td, "reader_nb.yuv")
            open(p_nb, "wb").write(nb)
            attempt("deblocking wrong only (A fails, B passes)", bytes(bad), False, ("deblocking filter is wrong",), nodeblock=p_nb)
            bad2 = bytearray(nb)
            bad2[2 * fs + 100] ^= 4
            open(p_nb, "wb").write(bytes(bad2))
            attempt("reconstruction wrong (B fails)", good, False, ("reconstruction itself is wrong",), nodeblock=p_nb)
            mvl = mbdec.mv_dump_lines(mbdec.decode(path, workdir=td, cache=caches[n]))
            p_mv = os.path.join(td, "reader.mv")
            open(p_mv, "w").write("\n".join(mvl) + "\n")
            attempt("motion vectors identical", good, True, mv=p_mv)
            mvl2 = list(mvl)
            k = next(i for i, ln in enumerate(mvl2) if "intra" not in ln and "l0=-" not in ln)
            cells = mvl2[k].split(" l1=")[0].split("l0=")[1].split("/")
            r0, v0 = cells[0].split(":")
            cells[0] = f"{r0}:{int(v0.split(',')[0]) + 1},{v0.split(',')[1]}"
            mvl2[k] = mvl2[k].split("l0=")[0] + "l0=" + "/".join(cells) + " l1=" + mvl2[k].split(" l1=")[1]
            open(p_mv, "w").write("\n".join(mvl2) + "\n")
            attempt("one wrong vector component", good, False, ("channel C", f"line {k + 1}"), mv=p_mv)
            open(p_mv, "w").write("\n".join(mvl[:k] + mvl[k + 1:]) + "\n")
            attempt("vector dump with one macroblock line missing", good, False, ("channel C",), mv=p_mv)
            attempt_malformed("vector dump line with 15 cells", good, "\n".join(mvl[:k] + [mvl[k].rsplit("/", 1)[0]] + mvl[k + 1:]) + "\n")
            attempt_malformed("vector dump with garbage", good, "MV 0 0 fld=1 l0=banana\n")
        # ---- 3. the vectors of the reference against libavcodec's exported vectors
        if 3 in stages:
            print("3. reference vectors vs the vectors libavcodec exports")
            for n in ("x_cabac_b", "x_temporal", "g_b_cabac"):
                tot, bad_ = mv_vs_ffmpeg(truth[n][0], td, caches[n])
                ok(tot > 0 and bad_ == 0, f"{n}: {tot} exported vectors compared, {bad_} differ")
        # ---- 4. mutants
        if 4 in stages:
            print("4. mutants of the reference decoder (every one must be caught by the pixel comparison)")
            allm = sorted(mbdec.ALL_MUTANTS)
            for mname in allm[mslice[0]::mslice[1]]:
                hit = None
                for n in names:
                    path, a, b = truth[n]
                    mbdec.set_mutant(mname)
                    try:
                        frs = mbdec.decode(path, workdir=td, cache=caches[n])
                    except mbdec.Unsupported:
                        mbdec.set_mutant(None)
                        hit = (n, "decoder gave up")
                        break
                    except Exception as e:             # a wrong rule may violate the stream's own consistency
                        mbdec.set_mutant(None)
                        hit = (n, f"exception {type(e).__name__}")
                        break
                    mbdec.set_mutant(None)
                    if b"".join(mbdec.frame_bytes(f) for f in frs) != open(a, "rb").read():
                        hit = (n, "pixels")
                        break
                kind = ""
                if hit and hit[1] == "pixels" and mname in mbdec.mbdeblock.MUTANTS:
                    mbdec.set_mutant(mname)
                    frs_b = mbdec.decode(truth[hit[0]][0], workdir=td, cache=caches[hit[0]], deblock=False)
                    mbdec.set_mutant(None)
                    same_b = b"".join(mbdec.frame_bytes(f) for f in frs_b) == open(truth[hit[0]][2], "rb").read()
                    kind = " (pictures without deblocking still exact: caught by channel A only)" if same_b else " (also visible without deblocking)"
                ok(hit is not None, f"mutant {mname}: " + (f"caught on {hit[0]} by {hit[1]}{kind}" if hit else "NOT CAUGHT")
                   + f"  [{mbdec.ALL_MUTANTS[mname][:70]}]")
        # ---- 5. the full x264 suite
        if 5 in stages:
            print("5. reference decoder vs ffmpeg on the complete x264 suite (1 s streams)")
            for n in X264:
                path = os.path.join(td, n + "_full.264")
                make_x264(n, path, 1.0)
                a = ffmpeg_yuv(path, os.path.join(td, n + ".fullA.yuv"))
                b = ffmpeg_yuv(path, os.path.join(td, n + ".fullB.yuv"), skip_loop=True)
                st, cache = {}, {}
                frs = mbdec.decode(path, workdir=td, cache=cache, stats=st)
                ra = b"".join(mbdec.frame_bytes(f) for f in frs) == open(a, "rb").read()
                rb = b"".join(mbdec.frame_bytes(f) for f in mbdec.decode(path, workdir=td, cache=cache, deblock=False)) == open(b, "rb").read()
                nf = sum(1 for f in frs for m in f.mbs.values() if m.fld and m.addr % 2 == 0)
                nt = sum(1 for f in frs for m in f.mbs.values() if m.addr % 2 == 0)
                ok(ra and rb, f"{n}: A {'equal' if ra else 'DIFFERENT'}, B {'equal' if rb else 'DIFFERENT'}, {len(frs)} frames, {100 * nf // max(nt, 1)}% field pairs")
                os.remove(path)
    finally:
        mbdec.set_mutant(None)
        shutil.rmtree(td, ignore_errors=True)
    print(f"{'PASS' if not problems else 'FAIL'}: self-test ({time.time() - t0:.0f} s, {len(problems)} problem(s))")
    return 0 if not problems else 1


def mv_vs_ffmpeg(path, td, cache):
    """Compare the reference's final vectors with libavcodec's exported motion vectors (ffmv_helper.py).  Known export quirks: vertical
    components of field macroblocks with 16x8 / 8x16 partitions come out doubled, and a list that a partition does not use is exported
    with a zero vector; both are accounted for.  -> (compared, differing)."""
    raw = path
    outp = os.path.join(td, "ffmv.txt")
    r = subprocess.run([sys.executable, os.path.join(HERE, "ffmv_helper.py"), raw, outp], stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    if r.returncode != 0:
        return 0, 0
    frs = mbdec.decode(path, workdir=td, cache=cache)
    tot = bad = 0
    for line in open(outp):
        n, src, w, h, dx, dy, mx, my, sc = map(int, line.split())
        fr = frs[n]
        mbx, mby = dx // 16, dy // 16
        m = fr.mbs[2 * ((mby // 2) * fr.wm + mbx) + (mby & 1)]
        cell = ((dy - h // 2 - 16 * mby) // 4) * 4 + (dx - w // 2 - 16 * mbx) // 4
        l = 0 if src == -1 else 1
        if m.ref[l][cell] < 0:
            continue
        mv = m.mv[l][cell]
        if m.fld and (w, h) in ((16, 8), (8, 16)):
            mv = (mv[0], mv[1] * 2)
        tot += 1
        bad += mv != (mx, my)
    return tot, bad


# ================================================================ main
def main(argv):
    a = list(argv)
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if a[0] == "--self-test":
        stages, msl = (1, 2, 3, 4, 5), (0, 1)
        if "--stages" in a:                       # development aid: --stages 2,3  --mutant-slice K/N (every N-th mutant from K)
            stages = tuple(int(x) for x in a[a.index("--stages") + 1].split(","))
        if "--mutant-slice" in a:
            k, nn = a[a.index("--mutant-slice") + 1].split("/")
            msl = (int(k), int(nn))
        return selftest(stages, msl)
    if a[0] == "--make" and len(a) == 2:
        print("streams:", ", ".join(make_suite(a[1])))
        return 0
    if a[0] == "--expected" and len(a) == 3:
        write_expected(a[1], a[2])
        return 0
    if a[0] == "--stats" and len(a) == 2:
        td = tempfile.mkdtemp(prefix="mbrecon_")
        try:
            print(stats_text(Truth(a[1], td)))
        finally:
            shutil.rmtree(td, ignore_errors=True)
        return 0
    only = None
    if "--only" in a:
        i = a.index("--only")
        only = a[i + 1].split(",")
        del a[i:i + 2]
    if a[0] == "--check" and len(a) == 3:
        return run_suite(a[1], a[2], only)
    if a[0] == "--suite" and len(a) >= 4 and a[2] == "--reader":
        out = tempfile.mkdtemp(prefix="mbrecon_out_")
        try:
            make_suite(a[1], only)
            return run_suite(a[1], out, only, reader=a[3])
        finally:
            shutil.rmtree(out, ignore_errors=True)
    nb = mv = None
    for flag in ("--nodeblock-yuv", "--mv-dump"):
        if flag in a:
            i = a.index(flag)
            v = a[i + 1]
            del a[i:i + 2]
            if flag == "--nodeblock-yuv":
                nb = v
            else:
                mv = v
    if len(a) != 2:
        print(__doc__)
        return 2
    if not os.path.exists(a[0]):
        die(f"stream {a[0]} not found")
    if not os.path.exists(a[1]):
        die(f"reader output {a[1]} not found")
    rep, notes = check_stream(a[0], a[1], nb, mv)
    for t in notes:
        print(t)
    rep.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
