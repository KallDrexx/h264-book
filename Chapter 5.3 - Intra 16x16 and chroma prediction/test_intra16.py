#!/usr/bin/env python3
"""Chapter 5.3 harness: Intra16x16 and chroma prediction (the first picture milestone).

usage: test_intra16.py <in.ts|in.264> <reader_output.yuv> [--only-i16]
       test_intra16.py --make-variants DIR     write the test streams (synthetic + x264) into DIR
       test_intra16.py --self-test             verify this harness itself

The truth is ffmpeg's decode (-fps_mode passthrough -f rawvideo -pix_fmt yuv420p, cropped), compared sample for sample
with your .yuv.  Your reader (Phase 1-4 pipeline + this chapter) must write one cropped frame per picture.

Default mode: every sample of every frame must match.  That is the right test for streams without I_NxN macroblocks
(the synthetic i16_* streams, x264_i16*.ts).  A stream that contains I_NxN macroblocks (T1 does: x264 still emits some)
cannot be decoded exactly before Chapter 5.4 (Intra 4x4); the harness tells you so when it fails.

--only-i16 (for streams with I_NxN macroblocks, until 5.4): luma is not compared where it cannot yet be right,
  * the luma of every I_NxN macroblock (you have no Intra 4x4 yet), and
  * the luma of every Intra16x16 macroblock whose prediction reads (through the mode it uses) luma samples of a
    macroblock in that set, transitively, in raster order.
The test is sound: a correct 5.3 reader can never fail it, since every compared sample depends only on parsed
syntax and on compared samples.  Chroma never depends on luma, so ALL chroma samples are compared, I_NxN macroblocks
included (their intra_chroma_pred_mode and chroma residual are parsed in Phase 4 and decoded by this chapter).
I_PCM macroblocks are exact.  The harness prints how much of the luma it could check.
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
from common import cavlc_ref as R, ffref, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402

QUIET = False
MODE_NAMES = {0: "vertical", 1: "horizontal", 2: "DC", 3: "plane"}
CMODE_NAMES = {0: "DC", 1: "horizontal", 2: "vertical", 3: "plane"}


def die(msg):
    print("error:", msg)
    sys.exit(2)


# ------------------------------------------------------------------ stream analysis (per macroblock)
class Pic:
    pass


def analyse(path, workdir):
    """Parse the stream with the Phase 4 reference parser.  -> (pictures, problem).  problem is None when every slice is a
    plain CAVLC I slice that parsed completely (then each Pic has per-macroblock records), else a text."""
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
        p.mb = {}
        for s in sl:
            res = R.parse_slice(s, 4)
            if res.status != "complete":
                problem = problem or f"slice {s.index} not decodable by this chapter: {res.error or res.note}"
                continue
            for rec in res.mbs:
                f = rec.f
                kind = "PCM" if rec.pcm is not None else "NXN" if f["name"] == "I_NxN" else "I16"
                p.mb[rec.addr] = dict(kind=kind, mode=f["i16_mode"], cmode=f.get("chroma_pred", "-"), slice=s.index,
                                      first=s.first_mb, cbp=f.get("cbp", "-"), qp=f.get("qp", "-"))
        if len(p.mb) != p.w * p.h:
            problem = problem or f"picture {pi}: {len(p.mb)} macroblocks parsed, expected {p.w * p.h}"
        out.append(p)
    return out, problem


def neighbours(p, addr):
    """-> (A, B, D) availability of a macroblock (same slice, inside the picture)."""
    mb = p.mb[addr]
    mx, my = addr % p.w, addr // p.w

    def ok(cond, o):
        return bool(cond) and o in p.mb and p.mb[o]["slice"] == mb["slice"]
    return ok(mx > 0, addr - 1), ok(my > 0, addr - p.w), ok(mx > 0 and my > 0, addr - p.w - 1)


def luma_taint(p):
    """Set of macroblock addresses whose luma a 5.3 reader cannot get right yet (see the module docstring)."""
    tainted = set()
    for addr in range(p.w * p.h):
        mb = p.mb[addr]
        if mb["kind"] == "NXN":
            tainted.add(addr)
        elif mb["kind"] == "I16":
            a, b, d = neighbours(p, addr)
            used = {0: [(b, addr - p.w)], 1: [(a, addr - 1)], 2: [(a, addr - 1), (b, addr - p.w)],
                    3: [(a, addr - 1), (b, addr - p.w), (d, addr - p.w - 1)]}[mb["mode"]]
            if any(av and o in tainted for av, o in used):
                tainted.add(addr)
    return tainted


def describe_mb(p, addr):
    mb = p.mb[addr]
    mx, my = addr % p.w, addr // p.w
    a, b, d = neighbours(p, addr)
    base = f"MB {addr} (x={mx}, y={my}), slice starts at MB {mb['first']}, neighbours A={int(a)} B={int(b)} D={int(d)}"
    if mb["kind"] == "PCM":
        return base + ", I_PCM"
    cm = f"chroma mode {mb['cmode']} ({CMODE_NAMES.get(mb['cmode'], '?')})"
    if mb["kind"] == "NXN":
        return base + f", I_NxN, {cm}, CBP {mb['cbp']}, QP {mb['qp']}"
    return (base + f", I_16x16 mode {mb['mode']} ({MODE_NAMES[mb['mode']]}), {cm}, CBP {mb['cbp']}, QP {mb['qp']}")


# ------------------------------------------------------------------ reference and comparison
def reference_yuv(path, out):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    ffref.run(cmd, timeout=600)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        die(f"ffmpeg could not decode {path}")


def mb_regions(p, addr):
    """(plane, x, y, w, h) slices of macroblock addr inside the cropped frame; empty ones are skipped."""
    mx, my = addr % p.w, addr // p.w
    out = []
    for plane, size, ox, oy, pw, ph in (("Y", 16, p.x0, p.y0, p.cw, p.ch), ("Cb", 8, p.x0 // 2, p.y0 // 2, (p.cw + 1) // 2, (p.ch + 1) // 2),
                                         ("Cr", 8, p.x0 // 2, p.y0 // 2, (p.cw + 1) // 2, (p.ch + 1) // 2)):
        x1, y1 = max(size * mx - ox, 0), max(size * my - oy, 0)
        x2, y2 = min(size * mx + size - ox, pw), min(size * my + size - oy, ph)
        if x2 > x1 and y2 > y1:
            out.append((plane, x1, y1, x2 - x1, y2 - y1))
    return out


def plane_offset(p, plane):
    cw, ch = (p.cw + 1) // 2, (p.ch + 1) // 2
    return {"Y": (0, p.cw), "Cb": (p.cw * p.ch, cw), "Cr": (p.cw * p.ch + cw * ch, cw)}[plane]


def region_rows(p, frame_base, plane, x, y, w, h):
    off, stride = plane_offset(p, plane)
    return [frame_base + off + (y + r) * stride + x for r in range(h)]


def compare(path, yuv, only_i16, workdir):
    """-> Report (not finished)."""
    rep = Report("5.3 intra prediction: " + os.path.basename(path))
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
    ok_size = size == nref * fs
    msg = f"file size: expected {nref * fs} bytes = {nref} frames x {fs} bytes ({w}x{h}, yuv420p), got {size}"
    if size % fs:
        msg += f" = {size // fs} whole frames + {size % fs} stray bytes"
    rep.check(ok_size, msg)
    if size % fs or size // fs == 0:
        print("note: pixels not compared, because the file is not a whole number of frames; fix the size first (Chapter 5.1)")
        return rep
    nxn = sum(1 for p in pics for m in p.mb.values() if m["kind"] == "NXN")
    ref_b, out_b = bytearray(open(ref, "rb").read()), bytearray(open(yuv, "rb").read())
    masked = Counter()
    if problem:
        print(f"note: macroblock analysis unavailable ({problem}); plain frame comparison only")
        only_i16 = False
    nframes = min(nref, size // fs)
    if only_i16 and not nxn and not QUIET:
        print("note: this stream has no I_NxN macroblocks, so --only-i16 changes nothing: all samples are compared")
    ignored = {}
    if only_i16:
        for fi in range(nframes):
            p = pics[fi]
            ignored[fi] = luma_taint(p)
            for addr in ignored[fi]:
                for plane, x, y, ww, hh in mb_regions(p, addr)[:1]:       # luma region only
                    for pos in region_rows(p, fi * fs, plane, x, y, ww, hh):
                        ref_b[pos:pos + ww] = bytes(ww)
                        out_b[pos:pos + ww] = bytes(ww)
    tmpr, tmpo = os.path.join(workdir, "ref_masked.yuv"), os.path.join(workdir, "out_masked.yuv")
    open(tmpr, "wb").write(ref_b)
    open(tmpo, "wb").write(out_b)
    r = yuvcmp.compare_yuv(tmpr, tmpo, w, h, quiet=True, origin=(p0.x0, p0.y0))
    rep.check(r["ok"], "pixels differ from ffmpeg's reference (expected = ffmpeg, actual = yours)\n    "
              + r["text"].replace("\n", "\n    "))
    # per macroblock-type summary
    if not problem:
        stats, first = OrderedDict(), {}
        for fi in range(nframes):
            p = pics[fi]
            for addr in range(p.w * p.h):
                mb = p.mb[addr]
                bad = {}
                for plane, x, y, ww, hh in mb_regions(p, addr):
                    if plane == "Y" and fi in ignored and addr in ignored[fi]:
                        continue
                    rows = region_rows(p, fi * fs, plane, x, y, ww, hh)
                    bad[plane] = any(ref_b[pos:pos + ww] != out_b[pos:pos + ww] for pos in rows)
                classes = []
                if "Y" in bad:
                    classes.append(("luma  " + ("I_16x16 mode %d %s" % (mb["mode"], MODE_NAMES[mb["mode"]]) if mb["kind"] == "I16"
                                                else "I_PCM" if mb["kind"] == "PCM" else "I_NxN"), "Y"))
                if "Cb" in bad:
                    classes.append(("chroma " + ("I_PCM" if mb["kind"] == "PCM" else
                                                 "mode %d %s" % (mb["cmode"], CMODE_NAMES[mb["cmode"]])
                                                 + (" (in I_NxN)" if mb["kind"] == "NXN" else "")), "C"))
                for key, k in classes:
                    c = stats.setdefault(key, [0, 0])
                    wrong = bad["Y"] if k == "Y" else (bad["Cb"] or bad.get("Cr", False))
                    c[0] += 1
                    c[1] += wrong
                    if wrong and (k, fi) not in first and not any(kk[0] == k for kk in first):
                        first[(k, fi)] = (addr, [pl for pl in bad if bad[pl]])
        nbad = sum(c[1] for c in stats.values())
        if nbad:
            lines = ["macroblock types with wrong samples (checked / wrong):"]
            for key, (n, wr) in sorted(stats.items()):
                lines.append(f"  {key:<40} {n:>6} / {wr}")
            for (k, fi), (addr, planes) in first.items():
                lines.append(f"first wrong {'luma' if k == 'Y' else 'chroma'} macroblock: picture {fi}, "
                             f"{describe_mb(pics[fi], addr)} (differing planes: {', '.join(planes)})")
            hint = ""
            if nxn and not only_i16:
                hint = (f"\n    this stream contains {nxn} I_NxN macroblocks: Intra 4x4 prediction arrives in Chapter 5.4, so a "
                        "luma mismatch is expected until then; use --only-i16 to check everything that is decidable now")
            rep.check(False, "\n    ".join(lines) + hint)
    if only_i16 and not problem:
        tot = sum(p.w * p.h for p in pics[:nframes])
        luma_ign = sum(len(ignored[fi]) for fi in ignored)
        rep.title += f" [--only-i16, luma checked in {100.0 * (tot - luma_ign) / tot:.0f}% of macroblocks, chroma in 100%]"
        if not QUIET:
            print(f"--only-i16: luma compared in {tot - luma_ign} of {tot} macroblocks ({100.0 * (tot - luma_ign) / tot:.1f}%), "
                  f"chroma in {tot} of {tot}; {nxn} I_NxN macroblocks in the stream")
            if tot == luma_ign:
                print("WARNING: no luma sample could be compared (every macroblock depends on an I_NxN one): this run checked "
                      "chroma only.\n         Test your luma on the I_NxN-free streams (--make-variants), and on T1 itself after Chapter 5.4.")
    elif not problem and not QUIET:
        types = Counter(m["kind"] for p in pics[:nframes] for m in p.mb.values())
        print(f"compared all samples of {nframes} frames; macroblocks: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())))
    return rep


# ------------------------------------------------------------------ test streams
GEQ = ("nullsrc=size=352x288:rate=25,geq=lum='128+60*sin(X/60+Y/80+T/2)+20*sin(Y/45-X/70)':cb='128+40*sin(X/50+T/2)':"
       "cr='128+40*cos(Y/60+X/90)',format=yuv420p")
X264_RECIPES = {
    # name: (ffmpeg source, preset, QP, x264 params).  `-preset ultrafast` turns x264's mode analysis off, and with it
    # Intra 4x4: EVERY macroblock is Intra16x16, on any content (even T1's testsrc2).  `partitions=none` alone does
    # not do that (T1 still gets 25% I_NxN).  x264_mixed uses an ordinary preset on smooth content so that only about
    # 4% of the macroblocks are I_NxN, which gives --only-i16 something to check.  All sources are deterministic and
    # x264 runs on one thread, so the files are reproducible.
    "x264_i16": ("testsrc2=size=352x288:rate=25", "ultrafast", 26, "keyint=1:no-deblock=1:threads=1"),
    "x264_i16_slices": ("testsrc2=size=352x288:rate=25", "ultrafast", 26, "keyint=1:no-deblock=1:threads=1:slices=4"),
    "x264_i16_smooth": (GEQ, "ultrafast", 26, "keyint=1:no-deblock=1:threads=1"),
    "x264_mixed": (GEQ, "medium", 30, "keyint=1:partitions=none:no-deblock=1:threads=1:slices=4"),
}


def make_x264(dirpath, name):
    src, preset, qp, params = X264_RECIPES[name]
    ts = os.path.join(dirpath, name + ".ts")
    r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", "1", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                   "-preset", preset, "-qp", str(qp), "-profile:v", "baseline", "-x264-params", params, "-f", "mpegts", ts], timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"ffmpeg/libx264 failed for {name}: " + r.stderr.decode()[-300:])
    return ts


def make_variants(d, quiet=False):
    import predgen
    os.makedirs(d, exist_ok=True)
    cover, paths = Counter(), []
    for name, spec in predgen.VARIANTS.items():
        data, info = predgen.make_stream(spec)
        h264, ts = os.path.join(d, name + ".264"), os.path.join(d, name + ".ts")
        open(h264, "wb").write(data)
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-framerate", "25", "-i", h264, "-c:v", "copy", "-f", "mpegts", ts],
                      timeout=120)
        if r.returncode != 0:
            raise SystemExit("ffmpeg could not mux " + h264 + ": " + r.stderr.decode()[-300:])
        cover.update(info["cover"])
        paths.append(ts)
        if not quiet:
            print(f"{ts}  {16 * info['w']}x{16 * info['h']} coded, {info['pictures']} pictures, {info['n_slices']} slices")
    missing = predgen.coverage_missing(cover)
    if missing:
        raise SystemExit(f"internal error: availability/mode cases never generated: {missing}")
    for name in X264_RECIPES:
        ts = make_x264(d, name)
        paths.append(ts)
        if not quiet:
            print(f"{ts}  (x264)")
    if not quiet:
        print(f"coverage: every (luma mode, chroma mode) x (A, B, D availability) case that is legal occurs at least "
              f"{min(cover.values())} times in the synthetic streams")
        print("run your reader on each .ts (or the .264), then: python3 test_intra16.py <that.ts> <your output.yuv>")
    return paths


# ------------------------------------------------------------------ self test
def self_test():
    import refdecode
    import intraref
    global QUIET
    QUIET = True
    ffref.require_tools()
    td = tempfile.mkdtemp(prefix="intra53_self_")
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

    def run(ts, yuv, only=False):
        wd = tempfile.mkdtemp(dir=td)
        return compare(ts, yuv, only, wd)

    def decode(ts, out, mutant=None):
        intraref.MUTANT = mutant
        try:
            refdecode.write_yuv(refdecode.decode_pictures(ts), out)
        finally:
            intraref.MUTANT = None

    try:
        d = os.path.join(td, "v")
        print("generating streams ...")
        paths = {os.path.splitext(os.path.basename(p))[0]: p for p in make_variants(d, quiet=True)}
        good = {}
        for name, ts in paths.items():
            g = os.path.join(td, name + ".good.yuv")
            decode(ts, g)
            good[name] = g
        print("reference reader (refdecode.py) versus ffmpeg, whole frames:")
        for name in paths:
            if name == "x264_mixed":
                continue
            expect(f"{name} correct output", run(paths[name], good[name]), True)
        # x264 stream with only Intra16x16
        for name in ("x264_i16", "x264_i16_slices", "x264_i16_smooth"):
            pics, problem = analyse(paths[name], tempfile.mkdtemp(dir=td))
            kinds = Counter(m["kind"] for p in pics for m in p.mb.values())
            good_mix = not kinds["NXN"] and kinds["I16"] > 0
            ok &= good_mix
            print(f"  {'ok  ' if good_mix else 'BAD '} {name} macroblocks: {dict(kinds)} (must be Intra16x16 only)")
        # corruptions on a synthetic stream
        ts, g = paths["i16_ragged"], good["i16_ragged"]
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

        def swap(b):
            wh = p0.cw * p0.ch
            cs = (len(b) // (len(pics)) - wh) // 2
            for f in range(len(pics)):
                o = f * fs + wh
                b[o:o + cs], b[o + cs:o + 2 * cs] = bytes(b[o + cs:o + 2 * cs]), bytes(b[o:o + cs])
        expect("Cb and Cr swapped", run(ts, variant(swap, "c3.yuv")), False)
        expect("truncated file", run(ts, variant(lambda b: b.__delitem__(slice(len(b) - 7, None)), "c4.yuv")), False)
        # mutants of the prediction rules: the test streams must expose every one of them
        print("deliberately wrong prediction rules must be exposed by the test streams:")
        for mutant in ("lplane_noclip", "lplane_b4", "ldc_top_round", "cdc_blk1_left", "cdc_blk2_top", "cdc_blk3_top",
                       "cplane_b35", "cmode_swap"):
            caught = False
            for name in ("i16_edges", "i16_ragged", "i16_pcm", "i16_big", "i16_rows"):
                m = os.path.join(td, "m.yuv")
                try:
                    decode(paths[name], m, mutant)
                except ValueError:          # the wrong rule asked for a neighbour that does not exist
                    caught = True
                    break
                if run(paths[name], m).failures:
                    caught = True
                    break
            ok &= caught
            print(f"  {'ok  ' if caught else 'BAD '} mutant {mutant}: {'rejected on ' + name if caught else 'NOT detected'}")
        # --only-i16 on a stream that contains I_NxN macroblocks
        print("--only-i16 on x264_mixed (contains I_NxN macroblocks; the reference reader fills their luma with 128):")
        ts, g = paths["x264_mixed"], good["x264_mixed"]
        pics, _ = analyse(ts, tempfile.mkdtemp(dir=td))
        nxn = sum(1 for p in pics for m in p.mb.values() if m["kind"] == "NXN")
        p0 = pics[0]
        fs = yuvref.frame_bytes(p0.cw, p0.ch)
        raw = open(g, "rb").read()
        if not nxn:
            ok = False
            print("  BAD  x264_mixed has no I_NxN macroblocks")
        expect("full comparison fails (I_NxN luma is gray)", run(ts, g, False), False)
        expect("--only-i16 passes", run(ts, g, True), True)
        # find a tainted and an untainted luma MB and an I_NxN MB, in picture 3
        fi = 3
        tainted = luma_taint(pics[fi])
        free = [a for a in range(p0.w * p0.h) if a not in tainted and pics[fi].mb[a]["kind"] == "I16"]
        nxn_mb = [a for a in tainted if pics[fi].mb[a]["kind"] == "NXN"]
        later = [a for a in tainted if pics[fi].mb[a]["kind"] == "I16"]

        def pos_of(addr, plane, dx=3, dy=3):
            _, x, y, ww, hh = [r for r in mb_regions(pics[fi], addr) if r[0] == plane][0]
            return fi * fs + region_rows(pics[fi], 0, plane, x + dx, y + dy, 1, 1)[0]
        if free:
            expect("--only-i16: wrong luma in an Intra16x16 macroblock that is checked fails",
                   run(ts, variant(flip(pos_of(free[-1], "Y")), "d1.yuv"), True), False)
        if nxn_mb:
            expect("--only-i16: wrong chroma in an I_NxN macroblock fails",
                   run(ts, variant(flip(pos_of(nxn_mb[0], "Cb")), "d2.yuv"), True), False)
            expect("--only-i16: wrong luma inside an I_NxN macroblock is ignored",
                   run(ts, variant(flip(pos_of(nxn_mb[0], "Y")), "d3.yuv"), True), True)
        if later:
            expect("--only-i16: wrong luma in an Intra16x16 macroblock that depends on an I_NxN one is ignored",
                   run(ts, variant(flip(pos_of(later[0], "Y")), "d4.yuv"), True), True)
        # T1 (the Summary's milestone stream)
        from common import testfiles
        t1 = testfiles.get("T1")
        t1y = os.path.join(td, "t1.yuv")
        decode(t1, t1y)
        print("T1 (I_NxN present, so full byte-exactness waits for 5.4):")
        expect("T1 --only-i16 passes", run(t1, t1y, True), True)
        expect("T1 full comparison fails", run(t1, t1y, False), False)
        # extra sanity of the taint rule itself: modes use only the neighbours they read
        a = Pic()
        a.w, a.h = 3, 2
        a.mb = {i: dict(kind="I16", mode=0, cmode=0, slice=0, first=0) for i in range(6)}
        a.mb[0]["kind"] = "NXN"
        t = luma_taint(a)
        good_rule = t == {0, 3}      # vertical predictions: only the macroblock below 0 reads it
        a.mb[1]["mode"] = 1
        a.mb[4]["mode"] = 1
        t2 = luma_taint(a)           # 1 reads left (0): tainted; 4 reads left (3 is tainted via B? 3 vertical reads 0)
        good_rule &= t2 == {0, 1, 3, 4}
        ok &= good_rule
        print(f"  {'ok  ' if good_rule else 'BAD '} taint propagation rule")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stream", nargs="?")
    ap.add_argument("yuv", nargs="?")
    ap.add_argument("--only-i16", action="store_true", help="ignore luma that depends on I_NxN macroblocks (until Chapter 5.4)")
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
    wd = tempfile.mkdtemp(prefix="intra53_")
    try:
        compare(a.stream, a.yuv, a.only_i16, wd).finish()
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    main()
