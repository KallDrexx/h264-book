#!/usr/bin/env python3
"""Chapter 5.4 harness: Intra 4x4 prediction (the first byte-exact full-file milestone).

usage: test_intra4.py <in.ts|in.264> <reader_output.yuv>
       test_intra4.py --make-variants DIR     write the test streams (synthetic + x264) into DIR
       test_intra4.py --self-test             verify this harness itself

The truth is ffmpeg's decode (-fps_mode passthrough -f rawvideo -pix_fmt yuv420p, cropped), compared sample for sample
with your .yuv.  Your reader (Phase 1-4 pipeline + Chapters 5.1-5.4) must write one cropped frame per picture.
The streams must be intra-only, CAVLC, deblocking off: the recipes T1, T2, T4_nodeblock, default_intra of
common/testfiles.py and everything --make-variants writes.  (T3 and T4 have the deblocking filter: Phase 6.)

On a mismatch the harness names the first wrong macroblock (via common/yuvcmp.py) and, for an I_NxN macroblock, prints a
table of its 16 luma 4x4 blocks in decoding order: block index, position, the mode the stream selected (the harness derives
it with 8.3.1.1), which neighbours were available (A left, B above, C up-right, D corner), whether the block is right.
The first wrong block in decoding order is the one to debug: later blocks of the macroblock inherit its error.
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
I4_NAMES = ["vertical", "horizontal", "DC", "diagonal-down-left", "diagonal-down-right", "vertical-right",
            "horizontal-down", "vertical-left", "horizontal-up"]
BLK_XY = [(2 * ((b >> 2) & 1) + (b & 1), 2 * (b >> 3) + ((b >> 1) & 1)) for b in range(16)]


def die(msg):
    print("error:", msg)
    sys.exit(2)


# ------------------------------------------------------------------ stream analysis (per macroblock)
class Pic:
    pass


def mb_avail(p, addr):
    """(A, B, C, D) availability of a macroblock: inside the picture and in the same slice (6.4.9)."""
    mb = p.mb[addr]
    mx, my = addr % p.w, addr // p.w

    def ok(cond, o):
        return bool(cond) and o in p.mb and p.mb[o]["slice"] == mb["slice"]
    return (ok(mx > 0, addr - 1), ok(my > 0, addr - p.w), ok(my > 0 and mx < p.w - 1, addr - p.w + 1),
            ok(mx > 0 and my > 0, addr - p.w - 1))


def block_avail(blk, mbav):
    """(a, b, c, d) for one 4x4 block, from first principles: a sample belongs to a neighbouring macroblock, or to this
    macroblock; samples of this macroblock are usable when their block has a smaller index (is already decoded)."""
    ma, mb_, mc, md = mbav
    bx, by = BLK_XY[blk]
    idx = {xy: i for i, xy in enumerate(BLK_XY)}

    def where(sx, sy):             # block coordinates (units of 4 samples, may be -1 or 4) -> availability
        if sy >= 4 or (sx >= 4 and sy >= 0):
            return False
        if sx < 0 and sy < 0:
            return md
        if sy < 0:
            return mc if sx >= 4 else mb_
        if sx < 0:
            return ma
        return idx[(sx, sy)] < blk
    return where(bx - 1, by), where(bx, by - 1), where(bx + 1, by - 1), where(bx - 1, by - 1)


def analyse(path, workdir):
    """Parse the stream with the Phase 4 reference parser.  -> (pictures, problem).  problem is None when every slice is a
    plain CAVLC I slice that parsed completely, else a text."""
    import intra4ref
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
                                      first=s.first_mb, cbp=f.get("cbp", "-"), qp=f.get("qp", "-"),
                                      syntax=[int(v) for v in f["modes"].split(",")] if kind == "NXN" and f.get("t8x8") == 0 else None)
        if len(p.mb) != p.w * p.h:
            problem = problem or f"picture {pi}: {len(p.mb)} macroblocks parsed, expected {p.w * p.h}"
        else:
            for addr in range(p.w * p.h):                    # derive the 16 modes of every I_NxN macroblock (8.3.1.1)
                mb = p.mb[addr]
                if mb["syntax"] is not None:
                    av = mb_avail(p, addr)
                    mb["modes"] = intra4ref.derive_modes(
                        mb["syntax"], addr, av[0], av[1],
                        lambda which, addr=addr: p.mb[addr - 1 if which == "A" else addr - p.w].get("modes"))
                elif mb["kind"] == "NXN":
                    problem = problem or "transform_size_8x8_flag = 1 (Intra 8x8) is not part of Phase 5"
        out.append(p)
    return out, problem


def describe_mb(p, addr):
    mb = p.mb[addr]
    mx, my = addr % p.w, addr // p.w
    a, b, c, d = mb_avail(p, addr)
    base = (f"MB {addr} (x={mx}, y={my}), slice starts at MB {mb['first']}, "
            f"neighbour macroblocks A={int(a)} B={int(b)} C={int(c)} D={int(d)}")
    if mb["kind"] == "PCM":
        return base + ", I_PCM"
    cm = f"chroma mode {mb['cmode']} ({CMODE_NAMES.get(mb['cmode'], '?')})"
    if mb["kind"] == "NXN":
        return base + f", I_NxN, {cm}, CBP {mb['cbp']}, QP {mb['qp']}"
    return base + f", I_16x16 mode {mb['mode']} ({MODE_NAMES[mb['mode']]}), {cm}, CBP {mb['cbp']}, QP {mb['qp']}"


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
    """Sample rectangle (px, py, w, h) of the cropped frame covered by the size x size block at plane coordinates
    (x, y) of the uncropped macroblock-aligned plane; None if fully cropped away."""
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


def pred_modes(p, addr):
    """predIntra4x4PredMode of the 16 blocks (8.3.1.1), from the final modes; for the failure table only."""
    mb = p.mb[addr]
    av = mb_avail(p, addr)
    idx = {xy: i for i, xy in enumerate(BLK_XY)}
    out = []
    for blk in range(16):
        bx, by = BLK_XY[blk]
        vals = []
        for inside, avail, other, xy in ((bx > 0, av[0], addr - 1, ((bx - 1) % 4, by)), (by > 0, av[1], addr - p.w, (bx, (by - 1) % 4))):
            if inside:
                vals.append(mb["modes"][idx[xy]])
            elif not avail:
                vals.append(None)                     # dcPredModePredictedFlag
            else:
                m = p.mb[other].get("modes")
                vals.append(2 if m is None else m[idx[xy]])
        out.append(2 if None in vals else min(vals))
    return out


def block_table(p, addr, base, ref, out):
    """Per-4x4-block detail of an I_NxN macroblock.  -> (lines, first_wrong_blk or None)."""
    mb = p.mb[addr]
    mbav = mb_avail(p, addr)
    mx, my = addr % p.w, addr // p.w
    lines = ["  blk  x,y(px)  mode (correct)           pred  syntax    A B C D   result"]
    first = None
    preds = pred_modes(p, addr)
    for blk in range(16):
        bx, by = BLK_XY[blk]
        a, b, c, d = block_avail(blk, mbav)
        m = mb["modes"][blk]
        s = mb["syntax"][blk]
        syn = "flag=1" if s < 0 else f"rem={s}"
        bad = differs(p, base, ref, out, "Y", 16 * mx + 4 * bx, 16 * my + 4 * by, 4)
        if bad and first is None:
            first = blk
        # a block that needs the up-right samples (modes 3 and 7) and has none gets the substitute
        note = ""
        if m in (3, 7) and b:
            note = "  (up-right " + ("real" if c else "substituted by p[3,-1]") + ")"
        lines.append(f"  {blk:>3}  {16 * mx + 4 * bx:>3},{16 * my + 4 * by:<3}   {m} {I4_NAMES[m]:<22} {preds[blk]:>4}  {syn:<8}  "
                     f"{int(a)} {int(b)} {int(c)} {int(d)}   {'WRONG' if bad else 'ok'}{note}")
    return lines, first


def compare(path, yuv, workdir):
    """-> Report (not finished)."""
    rep = Report("5.4 intra 4x4: " + os.path.basename(path))
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
    # statistics by macroblock / block type, and the first wrong luma macroblock and chroma macroblock
    stats, first, firstblk = OrderedDict(), {}, {}
    for fi in range(nframes):
        p = pics[fi]
        for addr in range(p.w * p.h):
            mb = p.mb[addr]
            mx, my = addr % p.w, addr // p.w
            base = fi * fs
            if mb["kind"] == "NXN":
                firstwrong = None
                for blk in range(16):
                    bx, by = BLK_XY[blk]
                    bad = differs(p, base, ref_b, out_b, "Y", 16 * mx + 4 * bx, 16 * my + 4 * by, 4)
                    c = stats.setdefault(f"luma  I_NxN block, mode {mb['modes'][blk]} {I4_NAMES[mb['modes'][blk]]}", [0, 0])
                    c[0] += 1
                    if bad and firstwrong is None:
                        firstwrong = blk
                        c[1] += 1                      # counts only the first wrong block of a macroblock
                lumabad = firstwrong is not None
            else:
                lumabad = differs(p, base, ref_b, out_b, "Y", 16 * mx, 16 * my, 16)
                key = ("luma  I_PCM" if mb["kind"] == "PCM" else f"luma  I_16x16 mode {mb['mode']} {MODE_NAMES[mb['mode']]}")
                c = stats.setdefault(key, [0, 0])
                c[0] += 1
                c[1] += lumabad
            chbad = (differs(p, base, ref_b, out_b, "Cb", 8 * mx, 8 * my, 8)
                     or differs(p, base, ref_b, out_b, "Cr", 8 * mx, 8 * my, 8))
            key = "chroma " + ("I_PCM" if mb["kind"] == "PCM" else f"mode {mb['cmode']} {CMODE_NAMES[mb['cmode']]}")
            c = stats.setdefault(key, [0, 0])
            c[0] += 1
            c[1] += chbad
            if lumabad and "Y" not in first:
                first["Y"] = (fi, addr)
            if chbad and "C" not in first:
                first["C"] = (fi, addr)
    lines = ["with wrong samples (checked / wrong; for I_NxN luma, blocks are counted, and only the first wrong block of a "
             "macroblock, later blocks inherit its error):"]
    for key, (n, wr) in sorted(stats.items()):
        lines.append(f"  {key:<52} {n:>7} / {wr}")
    for k, name in (("Y", "luma"), ("C", "chroma")):
        if k in first:
            fi, addr = first[k]
            lines.append(f"first wrong {name} macroblock in decoding order: picture {fi}, {describe_mb(pics[fi], addr)}"
                         + ("  (the 'first difference' line above is first in pixel scan order, which can be another macroblock)"
                            if k == "Y" else ""))
            if k == "Y" and pics[fi].mb[addr]["kind"] == "NXN":
                tab, fb = block_table(pics[fi], addr, fi * fs, ref_b, out_b)
                lines += tab
                if fb is not None:
                    m = pics[fi].mb[addr]["modes"][fb]
                    lines.append(f"  -> debug block {fb} first (mode {m} {I4_NAMES[m]}); the blocks before it are right.")
                    lines.append("     pred = predIntra4x4PredMode; syntax flag=1 means mode = pred, rem=r means mode = r if r < pred "
                                 "else r + 1.  A B C D = left, above, up-right, corner samples available (1) for that block.")
                else:
                    lines.append("  -> all luma blocks of this macroblock are right in the visible area (the cropped part is not compared)")
    rep.check(False, "\n    ".join(lines))
    return rep


def print_summary(pics):
    types = Counter(m["kind"] for p in pics for m in p.mb.values())
    modes = Counter(md for p in pics for m in p.mb.values() if m["kind"] == "NXN" for md in m["modes"])
    ur = Counter()
    for p in pics:
        for addr, m in p.mb.items():
            if m["kind"] == "NXN":
                av = mb_avail(p, addr)
                for blk, md in enumerate(m["modes"]):
                    a, b, c, d = block_avail(blk, av)
                    if md in (3, 7) and b:
                        ur["real up-right" if c else "substituted up-right"] += 1
    print(f"compared all samples of {len(pics)} frames; macroblocks: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())))
    if modes:
        print("Intra4x4 blocks by mode: " + ", ".join(f"{m} {I4_NAMES[m]} {modes[m]}" for m in sorted(modes))
              + "; " + ", ".join(f"{k} {v}" for k, v in sorted(ur.items())) + " (modes 3 and 7)")


# ------------------------------------------------------------------ test streams
GEQ = ("nullsrc=size=352x288:rate=25,geq=lum='128+60*sin(X/60+Y/80+T/2)+20*sin(Y/45-X/70)':cb='128+40*sin(X/50+T/2)':"
       "cr='128+40*cos(Y/60+X/90)',format=yuv420p")
X264_RECIPES = {
    # name: (ffmpeg source, preset, QP, x264 params).  All intra-only (keyint=1), CAVLC (baseline), no deblocking, one
    # thread, so the files are reproducible.  They are real-encoder content for Intra 4x4: natural mode choices.
    "x264_smooth": (GEQ, "medium", 30, "keyint=1:partitions=i4x4:no-deblock=1:threads=1"),
    "x264_smooth_slices": (GEQ, "medium", 26, "keyint=1:partitions=i4x4:no-deblock=1:threads=1:slices=3"),
    "x264_qp14": ("testsrc2=size=352x288:rate=25", "slow", 14, "keyint=1:partitions=i4x4:no-deblock=1:threads=1"),
    "x264_qp44": ("testsrc2=size=352x288:rate=25", "slow", 44, "keyint=1:partitions=i4x4:no-deblock=1:threads=1:slices=2"),
}
RECIPE_STREAMS = ("T1", "T2", "T4_nodeblock", "default_intra")     # from common/testfiles.py


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
        raise SystemExit(f"internal error: cases never generated: {missing}")
    for name in X264_RECIPES:
        ts = make_x264(d, name)
        paths.append(ts)
        if not quiet:
            print(f"{ts}  (x264)")
    if not quiet:
        print(f"coverage: all {len(predgen.required_cover())} required cases occur in the synthetic streams (at least "
              f"{min(cover[k] for k in predgen.required_cover())} times each): every Intra4x4 mode under every block-level "
              "availability pattern, every mode on every block index, up-right real/substituted on every block index that "
              "can have it, mode prediction against unavailable / I_NxN / I16x16 / I_PCM neighbours on both sides")
        print("also decode the recipes with: python3 -m common.testfiles T1 T2 T4_nodeblock default_intra  (files in ../.cache)")
        print("run your reader on each .ts (or the .264), then: python3 test_intra4.py <that.ts> <your output.yuv>")
    return paths


# ------------------------------------------------------------------ self test
MUTANTS = [
    # (name, what is wrong)
    ("dc_round", "DC rounding +3 instead of +4"),
    ("dc_top_round", "DC with only the above row: +1 instead of +2"),
    ("ddl_corner", "diagonal-down-left, bottom-right sample: 2*p[7] instead of 3*p[7]"),
    ("vr_odd_2tap", "vertical-right, odd zVR: 2-tap instead of the 3-tap filter"),
    ("hd_else_2tap", "horizontal-down, zHD < -1: 2-tap instead of the 3-tap filter"),
    ("vl_odd_2tap", "vertical-left, odd rows: 2-tap instead of the 3-tap filter"),
    ("hu_z5", "horizontal-up, zHU = 5: p[-1,3] instead of (p[-1,2] + 3*p[-1,3] + 2) >> 2"),
    ("ur_subst_128", "unavailable up-right samples replaced by 128 instead of p[3,-1]"),
    ("ur_blk5_never", "block 5 never has up-right samples (it takes them from the macroblock above-right)"),
    ("ur_blk5_as_B", "block 5 takes the up-right samples whenever the macroblock above exists (ignores the picture edge)"),
    ("ur_blk3_11", "blocks 3 and 11 read up-right samples that are not decoded yet"),
    ("ur_blk7", "block 7 reads up-right samples from the not yet decoded macroblock to the right"),
    ("ur_blk13_15", "blocks 13 and 15 read up-right samples from the macroblock to the right"),
    ("ur_blk2_never", "block 2 does not use its (decoded) up-right neighbour block 1"),
    ("ur_blk1_never", "block 1 never has up-right samples"),
    ("mp_max", "predIntra4x4PredMode = max instead of min"),
    ("mp_unavail_is_dc_only", "unavailable neighbour counts as DC but the other neighbour's mode is still used (min(2, other))"),
    ("mp_other_is_vertical", "an Intra16x16 / I_PCM neighbour counts as mode 0 instead of 2"),
    ("mp_rem_le", "rem_intra4x4_pred_mode: rem <= pred compared instead of rem < pred"),
    ("pred_all_first", "all 16 blocks predicted before any residual is added and stored (no z-order dependency)"),
    ("avail_noslice", "neighbour availability ignores slice boundaries"),
]


def self_test():
    import refdecode
    import intra4ref
    global QUIET
    QUIET = True
    ffref.require_tools()
    td = tempfile.mkdtemp(prefix="intra54_self_")
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
        intra4ref.MUTANT = mutant
        try:
            refdecode.write_yuv(refdecode.decode_pictures(ts), out)
        finally:
            intra4ref.MUTANT = None

    try:
        d = os.path.join(td, "v")
        print("generating streams ...")
        paths = {os.path.splitext(os.path.basename(p))[0]: p for p in make_variants(d, quiet=True)}
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
        for name in ("T1", "T2"):
            pics, _ = analyse(paths[name], tempfile.mkdtemp(dir=td))
            kinds = Counter(m["kind"] for p in pics for m in p.mb.values())
            modes = Counter(md for p in pics for m in p.mb.values() if m["kind"] == "NXN" for md in m["modes"])
            good_mix = kinds["NXN"] > 0 and len(modes) == 9
            ok &= good_mix
            print(f"  {'ok  ' if good_mix else 'BAD '} {name}: macroblocks {dict(kinds)}; all nine Intra4x4 modes occur: "
                  f"{sorted(modes.items())}")
        # corruptions
        ts, g = paths["i4_ragged"], good["i4_ragged"]
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
            cs = (len(b) // len(pics) - wh) // 2
            for f in range(len(pics)):
                o = f * fs + wh
                b[o:o + cs], b[o + cs:o + 2 * cs] = bytes(b[o + cs:o + 2 * cs]), bytes(b[o:o + cs])
        expect("Cb and Cr swapped", run(ts, variant(swap, "c3.yuv")), False)
        expect("truncated file", run(ts, variant(lambda b: b.__delitem__(slice(len(b) - 7, None)), "c4.yuv")), False)
        # the failure report of an I_NxN macroblock carries the block table
        nx = [a for a in range(p0.w * p0.h) if pics[1].mb[a]["kind"] == "NXN"][0]
        _, rx, ry, _, _ = ("Y",) + region(pics[1], "Y", 16 * (nx % p0.w) + 5, 16 * (nx // p0.w) + 9, 1)
        rep = run(ts, variant(flip(fs + ry * p0.cw + rx), "c5.yuv"))
        txt = "\n".join(rep.failures)
        has = "first wrong luma macroblock" in txt and "blk  x,y(px)" in txt and "WRONG" in txt
        ok &= has
        print(f"  {'ok  ' if has else 'BAD '} failure report names the macroblock and prints the per-block table")
        # mutants of the prediction / mode prediction / availability rules
        print("deliberately wrong rules must be exposed by the test streams:")
        names = ("i4_edges", "i4_ragged", "i4_only", "i4_mixed", "i4_pcm", "i4_big", "i4_rows")
        for mutant, why in MUTANTS:
            caught, where = False, ""
            for name in names:
                m = os.path.join(td, "m.yuv")
                try:
                    decode(paths[name], m, mutant)
                except (ValueError, IndexError):      # the wrong rule asked for a neighbour that does not exist
                    caught, where = True, name + " (reader asked for a missing neighbour)"
                    break
                if run(paths[name], m).failures:
                    caught, where = True, name
                    break
            ok &= caught
            print(f"  {'ok  ' if caught else 'BAD '} mutant {mutant}: {'rejected on ' + where if caught else 'NOT detected'}   [{why}]")
        # and on the real stream T2, a few of them (the real-encoder streams must also expose the rules)
        for mutant in ("ur_blk5_never", "mp_max", "ur_blk3_11", "dc_round"):
            m = os.path.join(td, "m.yuv")
            try:
                decode(paths["T2"], m, mutant)
                caught = bool(run(paths["T2"], m).failures)
            except (ValueError, IndexError):
                caught = True
            ok &= caught
            print(f"  {'ok  ' if caught else 'BAD '} mutant {mutant} on T2: {'rejected' if caught else 'NOT detected'}")
        # coverage self-check of the block-availability rule against the independent table in intra4ref
        bad_rule = []
        for mbav in ((1, 1, 1, 1), (1, 1, 0, 1), (0, 1, 1, 0), (1, 0, 0, 0), (0, 0, 0, 0), (1, 1, 1, 0)):
            for blk in range(16):
                if block_avail(blk, mbav) != intra4ref.block_flags(blk, mbav):
                    bad_rule.append((mbav, blk))
        ok &= not bad_rule
        print(f"  {'ok  ' if not bad_rule else 'BAD '} harness block availability agrees with intra4ref's table")
        ur = [blk for blk in range(16) if block_avail(blk, (1, 1, 1, 1))[2]]
        good_ur = ur == [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 14]
        ok &= good_ur
        print(f"  {'ok  ' if good_ur else 'BAD '} blocks with up-right samples in an interior macroblock: {ur}")
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
    wd = tempfile.mkdtemp(prefix="intra54_")
    try:
        compare(a.stream, a.yuv, wd).finish()
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    main()
