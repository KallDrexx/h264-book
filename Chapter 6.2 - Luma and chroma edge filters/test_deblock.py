#!/usr/bin/env python3
"""Chapter 6.2 harness: the deblocking filter itself (8.7.2.2 - 8.7.2.4).  Compares the decoded YUV of your reader with ffmpeg's.

usage: test_deblock.py <in.ts|in.264> <out.yuv> [--unfiltered out_unfiltered.yuv]
       test_deblock.py --make-variants DIR       write the synthetic and x264 test streams into DIR
       test_deblock.py --self-test               verify this harness itself (about a minute)

<out.yuv> is your reader's output: every picture AFTER deblocking, 8-bit planar 4:2:0, cropped like ffmpeg's output (the format
of Phase 5).  It is compared with ffmpeg's decode (common/yuvcmp.py reports the first differing frame, plane, macroblock, pixel).

--unfiltered FILE (optional): your reader ALSO writes the same pictures BEFORE deblocking (what Phase 5 produced) to FILE.  It is
compared with ffmpeg's `-skip_loop_filter all` decode, which is exactly the picture before deblocking (intra pictures predict from
unfiltered samples), so the harness can tell a reconstruction bug from a deblocking bug.

On a mismatch the harness runs the reference filter (spoiler_reference/deblock_ref.py) on ffmpeg's unfiltered picture and names,
for the first wrong macroblock, every edge that touches the first wrong sample: direction, plane, edge index, bS, qPav, indexA,
indexB, alpha, beta, tC0 and the branch the reference took, with the sample values on both sides.  It also prints hints (only
chroma wrong, only some QP range wrong, ...).  The reference filter is validated against ffmpeg by --self-test.
Exit code: 0 identical, 1 different, 2 missing or unusable input.
"""
import argparse
import os
import shutil
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref as R, ffref, yuvref  # noqa: E402
from common.yuvcmp import compare_yuv  # noqa: E402
import bsref  # noqa: E402
import deblock_ref  # noqa: E402


def die(msg):
    print("error:", msg)
    sys.exit(2)


# ================================================================== stream analysis (independent parsing, as in Chapter 6.1)
class Mb:
    pass


class Pic:
    pass


def analyse(path, workdir, limit=None):
    """Parse the stream with the Phase 4 reference parser -> list of Pic (.w .h in macroblocks, .mbs[addr] (.slice .t8x8 .pcm .qp
    .kind), .slices[k] (.idc .alpha2 .beta2 .first .qp), .cw .ch .x0 .y0, .chroma_offsets).  Intra CAVLC frame streams only."""
    try:
        slices = R.load_stream(path, workdir)
    except SystemExit as e:
        die(str(e))
    by_pic = {}
    for s in slices:
        by_pic.setdefault(s.picture, []).append(s)
    out = []
    for pi in sorted(by_pic):
        if limit is not None and len(out) >= limit:
            break
        sl = by_pic[pi]
        s0 = sl[0]
        p = Pic()
        p.index, p.w = pi, s0.pic_width_in_mbs
        p.h = s0.pic_size_in_mbs // p.w
        sps = s0.sps
        p.cw, p.ch, p.x0, p.y0 = yuvref.cropped_size(
            p.w, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
            sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
            sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
        o1 = s0.pps["chroma_qp_index_offset"]
        p.chroma_offsets = (o1, s0.pps.get("second_chroma_qp_index_offset", o1))
        p.slices, p.mbs = [], [None] * (p.w * p.h)
        for k, s in enumerate(sl):
            if s.skip_reason:
                die(f"slice {s.index}: {s.skip_reason}; this chapter's harness handles intra CAVLC frame streams only")
            res = R.parse_slice(s, 3)
            if res.status != "complete":
                die(f"slice {s.index} could not be parsed by the reference parser: {res.error or res.note}")
            h = s.hdr
            sli = Mb()
            sli.k, sli.first, sli.qp = k, s.first_mb, s.slice_qp
            sli.idc = h.get("disable_deblocking_filter_idc", 0)
            sli.alpha2 = h.get("slice_alpha_c0_offset_div2", 0)
            sli.beta2 = h.get("slice_beta_offset_div2", 0)
            p.slices.append(sli)
            for pos, rec in enumerate(res.mbs):
                m = Mb()
                m.addr, m.slice, m.pos = rec.addr, k, pos
                m.pcm = rec.pcm is not None
                m.kind = "I_PCM" if m.pcm else ("I_NxN" if rec.f["name"] == "I_NxN" else "I_16x16")
                m.t8x8 = rec.f.get("t8x8") == 1
                q = rec.f.get("qp", "-")
                m.qp = q if isinstance(q, int) else None
                p.mbs[rec.addr] = m
        if any(m is None for m in p.mbs):
            die(f"picture {pi}: not every macroblock was parsed")
        for sli in p.slices:                    # QP_Y of macroblocks without mb_qp_delta (I_PCM, no residual): the running QP
            run_qp = sli.qp
            for m in p.mbs:
                if m.slice != sli.k:
                    continue
                if m.qp is None:
                    m.qp = run_qp
                run_qp = m.qp
        out.append(p)
    return out


# ================================================================== ffmpeg truth and the reference filter
def decode_yuv(path, out, unfiltered=False):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if unfiltered:
        cmd += ["-skip_loop_filter", "all"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    ffref.run(cmd, timeout=600)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        die(f"ffmpeg could not decode {path}")
    return out


def split_planes(data, i, w, h):
    """Frame i of a yuv420p byte string -> [Y, Cb, Cr] as lists of rows (lists of ints)."""
    fb = yuvref.frame_bytes(w, h)
    base, ys, cw, ch = i * fb, w * h, (w + 1) // 2, (h + 1) // 2
    mk = lambda off, pw, ph: [list(data[base + off + r * pw: base + off + (r + 1) * pw]) for r in range(ph)]    # noqa: E731
    return [mk(0, w, h), mk(ys, cw, ch), mk(ys + cw * ch, cw, ch)]


def run_reference(pic, planes, **kw):
    """Run the reference filter in place on `planes` (the unfiltered picture) for parsed picture `pic`."""
    grid = bsref.picture_bs(pic)
    deblock_ref.filter_picture(
        planes, pic.w, grid, [m.qp for m in pic.mbs], [m.pcm for m in pic.mbs],
        [pic.slices[m.slice].alpha2 for m in pic.mbs], [pic.slices[m.slice].beta2 for m in pic.mbs], pic.chroma_offsets, **kw)


class Truth:
    """ffmpeg's filtered and unfiltered pictures of a stream plus the reference parse; .run() applies the reference filter to the
    unfiltered pictures and compares with the filtered ones (None = byte identical, else a description of the first difference)."""

    def __init__(self, path, workdir, limit=None):
        self.path = path
        self.pics = analyse(path, workdir, limit)
        p0 = self.pics[0]
        self.skip = None
        if (p0.x0, p0.y0) != (0, 0) or p0.cw != 16 * p0.w or p0.ch != 16 * p0.h:
            self.skip = "stream is cropped (the reference filter works on whole macroblocks)"
            return
        self.w, self.h = 16 * p0.w, 16 * p0.h
        self.filt = open(decode_yuv(path, os.path.join(workdir, "f.yuv")), "rb").read()
        self.unf = open(decode_yuv(path, os.path.join(workdir, "u.yuv"), True), "rb").read()
        fb = yuvref.frame_bytes(self.w, self.h)
        if len(self.filt) % fb or len(self.filt) // fb < len(self.pics):
            self.skip = "ffmpeg output does not have one frame per picture"

    def run(self):
        for i, p in enumerate(self.pics):
            planes = split_planes(self.unf, i, self.w, self.h)
            run_reference(p, planes)
            want = split_planes(self.filt, i, self.w, self.h)
            for pl, name in enumerate(("Y", "Cb", "Cr")):
                for y, (a, b) in enumerate(zip(planes[pl], want[pl])):
                    if a != b:
                        x = next(j for j in range(len(a)) if a[j] != b[j])
                        return f"picture {i}, plane {name}, x={x} y={y}: reference filter gave {a[x]}, ffmpeg {b[x]}"
        return None


# ================================================================== checking a reader's output
def stream_geometry(path):
    info = yuvref.stream_info(path)
    if not info["width"] or not info["frames"]:
        die(f"ffprobe found no video frames in {path}")
    return info


def first_bad_frame(expected, actual, w, h):
    fb = yuvref.frame_bytes(w, h)
    for i in range(min(len(expected), len(actual)) // fb):
        if expected[i * fb:(i + 1) * fb] != actual[i * fb:(i + 1) * fb]:
            return i
    return None


def mb_of_sample(plane, x, y):
    return (x // 16, y // 16) if plane == 0 else (x // 8, y // 8)


def edge_distance_index(v):
    """For a coordinate across an edge (4 sample blocks): 0 = next to an edge (p0 / q0), 1 = p1 / q1, 2 = p2 / q2 (or 3)."""
    return min(v % 4, 3 - v % 4)


def diagnose(path, expected_yuv, actual_yuv, info, workdir, out):
    """Explain the first wrong picture with the reference filter.  Prints to `out` (a list of lines)."""
    w, h = info["width"], info["height"]
    exp, act = open(expected_yuv, "rb").read(), open(actual_yuv, "rb").read()
    f = first_bad_frame(exp, act, w, h)
    if f is None:
        return
    out.append(f"--- diagnosis of picture {f} (the first picture that differs) ---")
    try:
        pics = analyse(path, workdir, f + 1)
    except SystemExit:
        out.append("(the reference parser cannot handle this stream: no edge-level diagnosis)")
        return
    if len(pics) <= f:
        out.append("(the stream has fewer pictures than the output; no edge-level diagnosis)")
        return
    pic = pics[f]
    if (pic.x0, pic.y0) != (0, 0) or pic.cw != 16 * pic.w or pic.ch != 16 * pic.h:
        out.append("(cropped stream: no edge-level diagnosis)")
        return
    unf_path = decode_yuv(path, os.path.join(workdir, "diag_u.yuv"), True)
    unf = open(unf_path, "rb").read()
    mine, want = split_planes(act, f, w, h), split_planes(exp, f, w, h)
    ref = split_planes(unf, f, w, h)
    run_reference(pic, ref)
    if ref != want:
        out.append("(internal: the reference filter does not reproduce ffmpeg for this picture; trust ffmpeg's numbers only)")
    unf_planes = split_planes(unf, f, w, h)
    if mine == unf_planes:
        out.append("your picture equals the UNFILTERED picture: the filter changed nothing (not run, or every edge skipped).")
    # which samples are wrong, per plane and macroblock
    wrong, per_plane, wrong_mbs, dist = [], Counter(), {}, Counter()
    for pl in range(3):
        for y, (a, b) in enumerate(zip(mine[pl], want[pl])):
            if a == b:
                continue
            for x in range(len(a)):
                if a[x] != b[x]:
                    mx, my = mb_of_sample(pl, x, y)
                    addr = my * pic.w + mx
                    wrong.append((pl, x, y))
                    per_plane[pl] += 1
                    wrong_mbs.setdefault(addr, Counter())[pl] += 1
                    if pl == 0:
                        dist[min(edge_distance_index(x), edge_distance_index(y))] += 1
    names = ("Y", "Cb", "Cr")
    out.append("wrong samples: " + ", ".join(f"{names[k]} {per_plane[k]}" for k in range(3) if per_plane[k])
               + f" in {len(wrong_mbs)} macroblock(s) of {pic.w * pic.h}")
    # the first wrong macroblock in raster order, and its first wrong sample (Y first)
    addr0 = min(wrong_mbs)
    mb = pic.mbs[addr0]
    sl = pic.slices[mb.slice]
    mx0, my0 = addr0 % pic.w, addr0 // pic.w
    out.append(f"first wrong macroblock: {addr0} (x={mx0}, y={my0}): {mb.kind}, QP_Y {mb.qp}, slice {mb.slice} with "
               f"disable_deblocking_filter_idc {sl.idc}, slice_alpha_c0_offset_div2 {sl.alpha2}, slice_beta_offset_div2 {sl.beta2}")
    target = next((pl, x, y) for pl, x, y in wrong if mb_of_sample(pl, x, y) == (mx0, my0))
    tp, tx, ty = target
    out.append(f"first wrong sample there: plane {names[tp]} x={tx} y={ty} (macroblock-relative "
               f"{tx - (16 if tp == 0 else 8) * mx0},{ty - (16 if tp == 0 else 8) * my0}): expected {want[tp][ty][tx]}, "
               f"you wrote {mine[tp][ty][tx]}; unfiltered value {unf_planes[tp][ty][tx]}")
    events = []

    def trace(plane, vertical, addr, edge, line, positions, inf):
        if plane == names[tp] and (tx, ty) in positions:
            events.append((plane, vertical, addr, edge, line, positions, inf))
    ref = split_planes(unf, f, w, h)
    run_reference(pic, ref, trace=trace)
    out.append(f"edges of the reference filter that look at that sample, in filtering order ({len(events)}):")
    for plane, vertical, addr, edge, line, positions, inf in events:
        side = positions.index((tx, ty))
        taps = len(positions) // 2
        role = f"p{taps - 1 - side}" if side < taps else f"q{side - taps}"
        if plane == "Y":
            ename, taps_txt = f"edge {edge}", "p3..p0 | q0..q3"
        else:
            ename = "edge 0" if edge == 0 else "edge 4 (chroma units: the internal edge, bS of luma edge 2)"
            taps_txt = "p1 p0 | q0 q1" if taps == 2 else "p3..p0 | q0..q3"
        branch = inf['branch']
        if branch == "off (thresholds)":
            branch = ("not filtered: filterSamplesFlag 0 (|p0-q0| >= alpha, |p1-p0| >= beta or |q1-q0| >= beta)"
                      if inf['bs'] else "not filtered: bS 0")
        out.append(f"  - {plane} {'vertical' if vertical else 'horizontal'} {ename} of macroblock {addr}, line {line} "
                   f"(the sample is {role}): bS {inf['bs']}, qPav {inf['qav']}, indexA {inf['indexA']}, indexB {inf['indexB']}, "
                   f"alpha {inf['alpha']}, beta {inf['beta']}")
        out.append(f"      reads {taps_txt} = " + " ".join(map(str, inf['p'][::-1])) + " | " + " ".join(map(str, inf['q']))
                   + f"   ->  {' '.join(map(str, inf['new_p'][::-1]))} | {' '.join(map(str, inf['new_q']))}"
                   + f"   [{branch}]")
    # hints
    hints = []
    if per_plane[0] == 0 and (per_plane[1] or per_plane[2]):
        hints.append("luma is correct, only chroma is wrong: check the chroma QP (QPc from each macroblock's QP_Y with its own "
                     "offset, per side, then averaged), tC = tC0 + 1, only p0/q0 modified, chroma edges only at 0 and 4 taking "
                     "bS from luma edges 0 and 2 (two chroma lines per luma segment)")
    if per_plane[0] and not per_plane[1] and not per_plane[2]:
        hints.append("chroma is correct, only luma is wrong: look at the luma-only parts (ap/aq in tC, p1/q1 modification, the "
                     "bS 4 strong filter with p2/p3)")
    if per_plane[1] == 0 and per_plane[2] and per_plane[0] == 0:
        hints.append("only Cr is wrong: Cr uses second_chroma_qp_index_offset (equal to chroma_qp_index_offset only in Baseline/Main PPSs "
                     "without the extension)")
    if per_plane[0] and dist and set(dist) <= {1, 2} and 0 not in dist:
        hints.append("every wrong luma sample is 2 or more samples from an edge (p1/q1/p2/q2): the p0/q0 outputs are right; "
                     "check the p1/q1 update of bS<4 (ap/aq < beta, clip to tC0) or the bS 4 strong filter")
    if per_plane[0] and dist and set(dist) == {0}:
        hints.append("only samples next to an edge (p0/q0) are wrong: check delta, tC and Clip1 (bS<4) or the weak bS 4 formula")
    qps = sorted(pic.mbs[a].qp for a in wrong_mbs)
    allq = sorted(m.qp for m in pic.mbs)
    if len(wrong_mbs) >= 3 and (qps[-1] - qps[0]) * 2 < (allq[-1] - allq[0]):
        hints.append(f"the wrong macroblocks have QP_Y {qps[0]}..{qps[-1]} while the picture spans {allq[0]}..{allq[-1]}: "
                     "a table entry (alpha, beta, tC0) or the indexA/indexB clipping in that range is a likely culprit")
    if len(wrong_mbs) >= 3 and all(pic.slices[pic.mbs[a].slice].alpha2 or pic.slices[pic.mbs[a].slice].beta2 for a in wrong_mbs) \
            and any(not (s.alpha2 or s.beta2) for s in pic.slices):
        hints.append("only macroblocks of slices with non-zero slice_alpha_c0_offset_div2 / slice_beta_offset_div2 are wrong: "
                     "FilterOffsetA = slice_alpha_c0_offset_div2 << 1, taken from the slice containing q0 (the current macroblock)")
    pcm_nb = [a for a in wrong_mbs if any(0 <= n < pic.w * pic.h and pic.mbs[n].pcm for n in (a, a - 1, a - pic.w))]
    if pcm_nb and len(pcm_nb) == len(wrong_mbs):
        hints.append("every wrong macroblock is, or touches, an I_PCM macroblock: qPp / qPq is 0 for I_PCM in the filter "
                     "(the running QP of 5.5 is untouched)")
    for hnt in hints:
        out.append("hint: " + hnt)
    if not hints:
        out.append("hint: compare the printed edge decisions with your own for that sample, step by step (bS, indexA, alpha, beta, tC0).")


def check_output(path, out_yuv, unfiltered=None, frames=None, workdir=None, quiet=False):
    """-> (ok, text).  Compares the reader's output with ffmpeg's decode; diagnoses a mismatch."""
    own = workdir is None
    wd = workdir or tempfile.mkdtemp(prefix="dbk62_")
    lines = []
    try:
        info = stream_geometry(path)
        w, h = info["width"], info["height"]
        ref_yuv = decode_yuv(path, os.path.join(wd, "ref.yuv"))
        ok = True
        if unfiltered:
            unf_ref = decode_yuv(path, os.path.join(wd, "ref_unf.yuv"), True)
            r = compare_yuv(unf_ref, unfiltered, w, h, quiet=True, origin=(0, 0), **({"max_frames": 3}))
            if r["ok"]:
                lines.append("unfiltered pictures: identical to ffmpeg -skip_loop_filter all (reconstruction is right)")
            elif r["size_problems"]:
                ok = False
                lines.append("your --unfiltered file has the wrong size or picture count (it must hold the same pictures as the "
                             "main output, same layout): no reconstruction verdict possible.")
                lines.append(r["text"])
            else:
                ok = False
                lines.append("unfiltered pictures DIFFER from ffmpeg -skip_loop_filter all: this is a RECONSTRUCTION bug (Phase 5), "
                             "not a deblocking bug. Fix it first (Chapter 5.x harnesses with the no-deblock recipes).")
                lines.append(r["text"])
        r = compare_yuv(ref_yuv, out_yuv, w, h, quiet=True, origin=(0, 0))
        if r["ok"]:
            lines.append(f"deblocked pictures: identical to ffmpeg ({info['frames']} frames)")
        else:
            ok = False
            lines.append("deblocked pictures DIFFER from ffmpeg:")
            lines.append(r["text"])
            if not r["size_problems"]:
                if unfiltered and any("RECONSTRUCTION" in l for l in lines):
                    lines.append("(edge-level diagnosis skipped: your unfiltered picture is already wrong)")
                else:
                    if not unfiltered:
                        lines.append("tip: write your pre-deblocking pictures too and pass --unfiltered FILE to tell reconstruction from "
                                     "deblocking bugs.")
                    try:
                        diagnose(path, ref_yuv, out_yuv, info, wd, lines)
                    except SystemExit:
                        lines.append("(no edge-level diagnosis: stream not supported by the reference parser)")
        text = "\n".join(lines)
        return ok, text
    finally:
        if own:
            shutil.rmtree(wd, ignore_errors=True)


# ================================================================== test streams
# x264 streams (name: (source, options, frames)).  Baseline / CAVLC / Intra 4x4 like the book's recipes, at several QPs and with
# deblock offsets; "x_*_8x8" use High-profile Intra 8x8 (a reader without Phase 8 cannot decode those; the harness does not need to).
BASE = "-profile:v baseline -x264-params keyint=1:partitions=i4x4"
X264 = {
    "x_q18":      ("testsrc2", BASE, 18, 3),
    "x_q30_dp6":  ("testsrc2", BASE + ":deblock=6,6", 30, 3),
    "x_q40_dm6":  ("testsrc2", BASE + ":deblock=-6,-6", 40, 3),
    "x_q48":      ("testsrc2", BASE, 48, 3),
    "x_q51_dp6":  ("testsrc2", BASE + ":deblock=6,6", 51, 3),
    "x_q34_s4":   ("mandelbrot", BASE + ":slices=4", 34, 3),
    "x_mand_q36": ("mandelbrot", BASE + ":deblock=3,-2", 36, 3),
    "x_bars_q44": ("smptehdbars", BASE, 44, 3),
    "x_bars_q22": ("smptehdbars", BASE, 22, 3),
    "x_T3_8x8":   ("testsrc2", "-profile:v high -x264-params cabac=0:keyint=1:8x8dct=1:partitions=i8x8,i4x4", 26, 3),
    "x_T4_8x8":   ("testsrc2", "-profile:v high -x264-params cabac=0:keyint=1:8x8dct=1:partitions=i8x8,i4x4:slices=4", 26, 3),
}
SIZES = {"testsrc2": "352x288", "mandelbrot": "352x288", "smptehdbars": "352x288"}


def make_x264(d, name):
    src, opts, qp, frames = X264[name]
    out = os.path.join(d, name + ".ts")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}=size={SIZES[src]}:rate=25", "-frames:v", str(frames),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", str(qp), *opts.split(), "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed for " + name + ": " + r.stderr.decode()[-300:])
    return out


def make_variants(d, quiet=False):
    import bsgen
    os.makedirs(d, exist_ok=True)
    cover, paths = Counter(), {}
    for name, spec in bsgen.VARIANTS.items():
        data, info = bsgen.make_stream(spec)
        h264, ts = os.path.join(d, name + ".264"), os.path.join(d, name + ".ts")
        open(h264, "wb").write(data)
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-framerate", "25", "-i", h264, "-c:v", "copy", "-f", "mpegts", ts],
                      timeout=120)
        if r.returncode != 0:
            raise SystemExit("ffmpeg could not mux " + h264 + ": " + r.stderr.decode()[-300:])
        cover.update(info["cover"])
        paths[name] = ts
        if not quiet:
            print(f"{ts}  {16 * info['w']}x{16 * info['h']}, {info['pictures']} pictures, {info['n_slices']} slices")
    missing = bsgen.coverage_missing(cover)
    if missing:
        raise SystemExit(f"internal error: cases never generated: {missing}")
    for name in X264:
        paths[name] = make_x264(d, name)
        if not quiet:
            print(paths[name])
    if not quiet:
        print("recipe streams for the real-encoder runs: python3 -m common.testfiles T3 T3_dm3 T3_dp3 T4   (files in ../.cache)")
        print("then: python3 test_deblock.py <stream> <your.yuv> [--unfiltered <your_unfiltered.yuv>]")
    return paths


# ================================================================== self test
# (mutant, what is wrong).  Every one must change pixels on some test stream and be caught by the harness.
MUTANTS = [
    ("tc_no_ap_aq", "luma tC = tC0 (the +1 for ap < beta and +1 for aq < beta is missing)"),
    ("tc_always_plus2", "luma tC = tC0 + 2 whatever ap and aq are"),
    ("chroma_tc_no_plus1", "chroma tC = tC0 (the +1 is missing)"),
    ("chroma_tc_like_luma", "chroma tC computed with the luma rule (ap / aq)"),
    ("no_clip_delta", "delta is not clipped to +-tC"),
    ("no_clip1", "p0' / q0' not clipped to 0..255 (Clip1 missing)"),
    ("delta_round_3", "delta rounds with +3 instead of +4"),
    ("no_p1_q1", "the luma p1 / q1 update of the bS < 4 filter is missing"),
    ("q1_missing", "only p1 is updated, q1 forgotten"),
    ("p1_clip_tc", "p1' / q1' clipped to tC instead of tC0"),
    ("tc0_bs_index", "tC0 column off by one (bS 3 reads the bS 2 column)"),
    ("chroma_strong_luma", "chroma bS 4 uses the luma strong filter"),
    ("strong_no_small_check", "bS 4 luma: the |p0 - q0| < (alpha >> 2) + 2 test is missing"),
    ("strong_thr_alpha", "bS 4 luma: |p0 - q0| < alpha instead of (alpha >> 2) + 2"),
    ("strong_p2_missing", "bS 4 luma: p2' / q2' not written"),
    ("hv_swapped", "horizontal edges filtered before the vertical ones"),
    ("edges_reversed", "edges of a direction filtered right to left / bottom to top"),
    ("unfiltered_samples", "edges read the unfiltered samples instead of the already filtered ones"),
    ("qpav_floor", "qPav = (qPp + qPq) >> 1 (no rounding up)"),
    ("offset_not_doubled", "FilterOffsetA / B = slice_*_offset_div2 (not doubled)"),
    ("offset_a_only", "FilterOffsetB ignored (indexB = qPav)"),
    ("offsets_from_p", "slice offsets taken from the slice containing p0 instead of q0"),
    ("swap_alpha_beta_index", "alpha from indexB and beta from indexA"),
    ("no_beta_check", "the |p1 - p0| < beta and |q1 - q0| < beta tests are missing"),
    ("pcm_qp_real", "I_PCM uses its running QP instead of 0"),
    ("chroma_uses_luma_qp", "chroma edges filtered with the luma QPs"),
    ("chroma_qp_of_average", "chroma QP derived from the average of the luma QPs instead of per macroblock"),
    ("cr_uses_cb_offset", "Cr uses chroma_qp_index_offset instead of second_chroma_qp_index_offset"),
]
# Mutants that may be invisible on some streams but must still be visible on the set as a whole: checked automatically.


def self_test():
    ffref.require_tools()
    td = tempfile.mkdtemp(prefix="dbk62_self_")
    ok = True

    def verdict(label, good, extra=""):
        nonlocal ok
        ok &= good
        print(f"  {'ok  ' if good else 'BAD '} {label}{(': ' + extra) if extra else ''}")

    try:
        print("test streams:")
        paths = make_variants(td, quiet=True)
        from common import testfiles
        recipes = {n: testfiles.get(n) for n in ("T3", "T3_dm3", "T3_dp3", "T4")}
        allp = dict(paths)
        allp.update(recipes)
        print(f"  {len(paths)} generated + {len(recipes)} recipe streams")
        truth = {}
        for name, src in allp.items():
            t = Truth(src, tempfile.mkdtemp(dir=td), 2 if name in recipes else (3 if name.startswith("x_") else None))
            if t.skip:
                print(f"  skipped {name}: {t.skip}")
                continue
            truth[name] = t
        print("1. the reference filter on ffmpeg's UNFILTERED picture reproduces ffmpeg's filtered picture (byte for byte):")
        bad = {name: d for name, t in truth.items() if (d := t.run())}
        for name, d in bad.items():
            verdict(name, False, d)
        verdict(f"all {len(truth)} streams", not bad)
        # coverage of the filter branches the mutants need
        cov = Counter()

        def tr(plane, vertical, addr, edge, line, positions, inf):
            if inf["indexA"] < 16:
                cov["lines with indexA < 16 (alpha 0: nothing happens)"] += 1
            if inf["indexA"] == 51:
                cov["lines with indexA clipped to 51"] += 1
            if inf["indexA"] == 0:
                cov["lines with indexA clipped to 0"] += 1
            if inf["filtered"]:
                key = ("luma" if plane == "Y" else "chroma", "bS4" if inf["bs"] == 4 else "bS3")
                cov[key] += 1
                if inf["branch"].startswith("bS=4 luma"):
                    cov["strong p+q" if "strong p side True, strong q side True" in inf["branch"] else "bS4 luma non-strong side"] += 1
                if plane == "Y" and inf["bs"] < 4:
                    cov["luma p1 updated"] += inf["new_p"][1] != inf["p"][1]
                    cov["luma q1 updated"] += inf["new_q"][1] != inf["q"][1]
        for name, t in truth.items():
            for i, p in enumerate(t.pics[:2]):
                planes = split_planes(t.unf, i, t.w, t.h)
                run_reference(p, planes, trace=tr)
        print("  filter activity: " + "; ".join(f"{k} {v}" for k, v in sorted(cov.items(), key=str)))
        verdict("every filter branch is exercised", all(cov[k] for k in (("luma", "bS4"), ("luma", "bS3"), ("chroma", "bS4"), ("chroma", "bS3"),
                                                                       "strong p+q", "luma p1 updated", "luma q1 updated",
                                                                       "lines with indexA < 16 (alpha 0: nothing happens)",
                                                                       "lines with indexA clipped to 51", "lines with indexA clipped to 0")))

        print("2. the harness on a reader's output:")
        name = "x_q30_dp6"
        src = allp[name]
        ref = decode_yuv(src, os.path.join(td, "ref_a.yuv"))
        unf = decode_yuv(src, os.path.join(td, "unf_a.yuv"), True)
        data = bytearray(open(ref, "rb").read())
        info = stream_geometry(src)
        W, H = info["width"], info["height"]
        fb = yuvref.frame_bytes(W, H)

        def write(nm, b):
            p = os.path.join(td, nm)
            open(p, "wb").write(bytes(b))
            return p
        good = write("good.yuv", data)
        r_ok, txt = check_output(src, good, workdir=tempfile.mkdtemp(dir=td))
        verdict("ffmpeg's own output passes", r_ok, txt[:200] if not r_ok else "")
        r_ok, txt = check_output(src, good, unfiltered=unf, workdir=tempfile.mkdtemp(dir=td))
        verdict("passes with --unfiltered given the right unfiltered file", r_ok, txt[:200] if not r_ok else "")
        bad = bytearray(data)
        bad[fb + 5 * W + 20] ^= 1
        r_ok, txt = check_output(src, write("bad1.yuv", bad), workdir=tempfile.mkdtemp(dir=td))
        verdict("one wrong luma sample in frame 1 fails", not r_ok and "picture 1" in txt and "x=20" in txt, "")
        r_ok, txt = check_output(src, write("bad2.yuv", data[:-fb]), workdir=tempfile.mkdtemp(dir=td))
        verdict("a missing frame fails", not r_ok, "")
        r_ok, txt = check_output(src, write("bad3.yuv", data[:fb * 3 - 7]), workdir=tempfile.mkdtemp(dir=td))
        verdict("a truncated file fails", not r_ok, "")
        # a reader that forgot the filter: its output is the unfiltered picture
        r_ok, txt = check_output(src, unf, workdir=tempfile.mkdtemp(dir=td))
        verdict("output without deblocking fails, diagnosis equals-unfiltered note and edge trace",
                not r_ok and "UNFILTERED" in txt and "bS " in txt and "indexA" in txt and "hint:" in txt, "")
        print("    --- sample diagnosis output (unfiltered picture given as the reader's output) ---")
        tl = txt.splitlines()
        k = next((i for i, l in enumerate(tl) if l.startswith("--- diagnosis")), 0)
        print("\n".join("    " + l for l in tl[k:k + 16]))
        r_ok, txt = check_output(src, unf, unfiltered=unf, workdir=tempfile.mkdtemp(dir=td))
        verdict("--unfiltered right, output wrong: reported as a deblocking bug", not r_ok and "reconstruction is right" in txt, "")
        badu = bytearray(open(unf, "rb").read())
        badu[7 * W + 9] ^= 3
        r_ok, txt = check_output(src, good, unfiltered=write("badu.yuv", badu), workdir=tempfile.mkdtemp(dir=td))
        verdict("--unfiltered wrong: reported as a reconstruction bug", not r_ok and "RECONSTRUCTION" in txt, "")
        # only chroma wrong -> chroma hint
        badc = bytearray(data)
        deblock_ref.MUTANT = "chroma_tc_no_plus1"
        t = truth[name]
        try:
            pl = split_planes(t.unf, 0, t.w, t.h)
            run_reference(t.pics[0], pl)
        finally:
            deblock_ref.MUTANT = None
        for k in range(len(pl[1])):
            badc[W * H + k * (W // 2):W * H + (k + 1) * (W // 2)] = bytes(pl[1][k])
        r_ok, txt = check_output(src, write("badc.yuv", badc), workdir=tempfile.mkdtemp(dir=td))
        verdict("only chroma wrong gets the chroma hint", not r_ok and "only chroma is wrong" in txt, "")
        # exit codes through the command line
        import subprocess
        me = os.path.abspath(__file__)
        for args, want in (([src, good], 0), ([src, write("bad4.yuv", bad)], 1), ([src, os.path.join(td, "nonexistent.yuv")], 2)):
            rc = subprocess.run([sys.executable, me, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=600).returncode
            verdict(f"exit code {want} for {os.path.basename(args[1])}", rc == want, f"got {rc}")

        print("3. wrong filter rules (deblock_ref.MUTANT): the pixels must differ from ffmpeg on some stream, and the harness must fail:")
        order = sorted(truth, key=lambda n: truth[n].w * truth[n].h)
        for mutant, what in MUTANTS:
            seen = None
            for nm in order:
                t = truth[nm]
                deblock_ref.MUTANT = mutant
                try:
                    d = t.run()
                finally:
                    deblock_ref.MUTANT = None
                if d:
                    seen = nm
                    break
            caught = False
            if seen:
                # write the mutant's output of the first wrong picture position as a reader output and run the harness on it
                t = truth[seen]
                buf = bytearray()
                deblock_ref.MUTANT = mutant
                try:
                    for i, p in enumerate(t.pics):
                        pl = split_planes(t.unf, i, t.w, t.h)
                        run_reference(p, pl)
                        for plane in pl:
                            for row in plane:
                                buf += bytes(v & 255 for v in row)
                finally:
                    deblock_ref.MUTANT = None
                # frames beyond the parsed pictures are ffmpeg's (so only the mutated ones can differ)
                buf += t.filt[len(buf):]
                r_ok, txt = check_output(allp[seen], write("mut.yuv", buf), workdir=tempfile.mkdtemp(dir=td))
                caught = not r_ok and "first wrong macroblock" in txt
            verdict(f"{mutant} ({what})", bool(seen) and caught, f"caught on {seen}" if seen and caught else "NOT detected")
    finally:
        deblock_ref.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stream", nargs="?")
    ap.add_argument("yuv", nargs="?")
    ap.add_argument("--unfiltered", metavar="FILE", help="your pictures before deblocking (compared with ffmpeg -skip_loop_filter all)")
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
        ap.error("need <in.ts|in.264> <out.yuv>")
    if not os.path.isfile(a.stream):
        die(f"input {a.stream!r} not found")
    for f, what in ((a.yuv, "your output"), (a.unfiltered, "your --unfiltered file")):
        if f and not os.path.isfile(f):
            die(f"{what} {f!r} not found")
    ok, text = check_output(a.stream, a.yuv, a.unfiltered)
    print(text)
    print("PASS: deblocked output identical to ffmpeg" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
