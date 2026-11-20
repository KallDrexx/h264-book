#!/usr/bin/env python3
"""Chapter 6.1 harness: which edges are filtered, and their boundary strength bS (8.7, 8.7.2.1) for intra pictures.

usage: test_bs.py <in.ts|in.264> <your_bs_dump.txt> [--pictures N]
       test_bs.py --make-variants DIR            write the synthetic and x264 test streams into DIR
       test_bs.py --expected-out FILE <stream>   write the reference dump of a stream (to diff against yours)
       test_bs.py --validate-reference <stream>  prove the reference bS grid by consequence (needs ffmpeg; see Readme section 6)
       test_bs.py --self-test                    verify this harness itself

Your dump (one per stream, all pictures in decoding order) has one block per picture:
    PIC <n>
    MB <addr> <mbx> <mby> FV f0 f1 f2 f3 FH f0 f1 f2 f3 V <16 bS values> H <16 bS values>
V = the 4 vertical edges (x = 0, 4, 8, 12) x 4 segments (rows 0-3, 4-7, 8-11, 12-15) in that order; H = the 4 horizontal
edges (y = 0, 4, 8, 12) x 4 segments (columns 0-3, ...).  F* = filterEdgeFlag of each edge (1 = the edge is filtered).  A
non-filtered edge has flag 0 and bS 0.  Every macroblock of every picture appears, in raster order (the Readme has the grammar).

The truth is an independent reference (spoiler_reference/bsref.py) fed by the Phase 4 reference parser (macroblock types,
transform_size_8x8_flag, QP, slice membership) and the slice-header values from ffmpeg's trace_headers.  ffmpeg cannot dump bS
(see the Readme), so the reference is validated by consequence instead: its grid, fed to a reference deblocking filter and run on
ffmpeg's unfiltered picture, must reproduce ffmpeg's filtered picture byte for byte (--validate-reference, part of --self-test).
Exit code: 0 pass, 1 fail, 2 missing or malformed dump / unsupported stream.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref as R, ffref, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import bsref  # noqa: E402
import deblock_ref  # noqa: E402

class DumpError(Exception):
    pass


def die(msg):
    print("error:", msg)
    sys.exit(2)


# ================================================================== stream analysis (independent parsing)
class Mb:
    pass


class Pic:
    pass


def analyse(path, workdir, limit=None):
    """Parse the stream: -> list of Pic (.w .h .mbs[addr] (.slice .t8x8 .pcm .qp .kind) .slices[k] (.idc .alpha2 .beta2 .first
    .qp) .cw .ch .x0 .y0 .chroma_offsets).  Only intra CAVLC frame streams are supported (SystemExit(2) otherwise)."""
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
            sli.idc = h.get("disable_deblocking_filter_idc", 0)            # absent -> inferred 0 (7.4.3)
            sli.alpha2 = h.get("slice_alpha_c0_offset_div2", 0)
            sli.beta2 = h.get("slice_beta_offset_div2", 0)
            sli.control_present = s.pps.get("deblocking_filter_control_present_flag", 0)
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
        # QP_Y of I_PCM / macroblocks without mb_qp_delta: the running QP (the parser prints it for every non-PCM macroblock)
        run_qp = None
        for sli in p.slices:
            run_qp = sli.qp
            for m in p.mbs:
                if m.slice != sli.k:
                    continue
                if m.qp is None:
                    m.qp = run_qp
                run_qp = m.qp
        out.append(p)
    return out


# ================================================================== the dump format
def fmt_mb(addr, w, g):
    return (f"MB {addr} {addr % w} {addr // w} FV {' '.join(map(str, g['fv']))} FH {' '.join(map(str, g['fh']))} "
            f"V {' '.join(map(str, g['v']))} H {' '.join(map(str, g['h']))}")


def render(pics, grids):
    lines = []
    for n, (p, grid) in enumerate(zip(pics, grids)):
        lines.append(f"PIC {n}")
        lines.extend(fmt_mb(a, p.w, g) for a, g in enumerate(grid))
    return "\n".join(lines) + "\n"


MB_RE = re.compile(r"^MB (\d+) (\d+) (\d+) FV ((?:\d+ ){3}\d+) FH ((?:\d+ ){3}\d+) V ((?:\d+ ){15}\d+) H ((?:\d+ ){15}\d+)$")


def parse_dump(text):
    """-> list of pictures, each a list of dict(addr, x, y, fv, fh, v, h) in file order.  Raises DumpError(line number, text)."""
    pics = []
    for ln, raw in enumerate(text.splitlines(), 1):
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        if line.startswith("PIC "):
            if line != f"PIC {len(pics)}":
                raise DumpError(f"line {ln}: expected 'PIC {len(pics)}', got {raw!r} (pictures are numbered 0, 1, 2 ... in decoding order)")
            pics.append([])
            continue
        m = MB_RE.match(line)
        if not m:
            raise DumpError(f"line {ln}: not a valid line: {raw[:100]!r}. Expected 'PIC <n>' or "
                            "'MB <addr> <mbx> <mby> FV f f f f FH f f f f V <16 numbers> H <16 numbers>'")
        if not pics:
            raise DumpError(f"line {ln}: MB line before the first 'PIC 0' line")
        nums = [list(map(int, m.group(i).split())) for i in range(4, 8)]
        pics[-1].append(dict(addr=int(m.group(1)), x=int(m.group(2)), y=int(m.group(3)), fv=nums[0], fh=nums[1], v=nums[2], h=nums[3],
                             line=ln))
        for name, vals in zip(("FV", "FH", "V", "H"), nums):
            lim = 1 if name.startswith("F") else 4
            if max(vals) > lim:
                raise DumpError(f"line {ln}: value {max(vals)} in {name} is out of range (flags are 0/1, bS is 0..4)")
    return pics


# ================================================================== comparison
def context(p, addr):
    m = p.mbs[addr]
    sl = p.slices[m.slice]
    mx, my = addr % p.w, addr // p.w
    lines = [f"MB {addr} (x={mx}, y={my}): {m.kind}, transform_size_8x8_flag {int(m.t8x8)}, QP_Y {m.qp}",
             f"slice {m.slice} of the picture (first_mb_in_slice {sl.first}): disable_deblocking_filter_idc {sl.idc}"
             + (f", slice_alpha_c0_offset_div2 {sl.alpha2}, slice_beta_offset_div2 {sl.beta2}" if sl.idc != 1 else "")]
    nb = []
    if mx > 0:
        nb.append(f"left MB {addr - 1} is in slice {p.mbs[addr - 1].slice}" + (" (a different slice)" if p.mbs[addr - 1].slice != m.slice else ""))
    else:
        nb.append("no left macroblock (picture edge)")
    if my > 0:
        nb.append(f"top MB {addr - p.w} is in slice {p.mbs[addr - p.w].slice}" + (" (a different slice)" if p.mbs[addr - p.w].slice != m.slice else ""))
    else:
        nb.append("no top macroblock (picture edge)")
    lines.append("; ".join(nb))
    return lines


def hint(p, addr, which, edge, seg, want, got, fl_want, fl_got):
    m = p.mbs[addr]
    sl = p.slices[m.slice]
    mx, my = addr % p.w, addr // p.w
    mb_edge = edge == 0
    pic_edge = mb_edge and (mx == 0 if which == "V" else my == 0)
    other = mb_edge and not pic_edge and p.mbs[addr - 1 if which == "V" else addr - p.w].slice != m.slice
    if fl_got and not fl_want:
        if pic_edge:
            return "an edge on the picture boundary is never filtered (there is no macroblock on the other side)"
        if sl.idc == 1:
            return "disable_deblocking_filter_idc 1 switches the filter off for every edge of this macroblock"
        if sl.idc == 2 and other:
            return "idc 2: do not filter across a slice boundary; the neighbour here is in another slice"
        if m.t8x8 and edge in (1, 3):
            return "with transform_size_8x8_flag = 1 the internal edges at 4 and 12 are not transform edges: skip them"
        return "this edge should not be filtered"
    if fl_want and not fl_got:
        if other and sl.idc == 0:
            return "idc 0 filters across slice boundaries too (only idc 2 stops at them)"
        if sl.idc == 2 and not other and mb_edge:
            return "idc 2 only stops at slice boundaries; here the neighbour is in the same slice"
        if not mb_edge:
            return "internal edges are filtered whenever the macroblock is (idc != 1), except 4 and 12 with the 8x8 transform"
        return "this edge should be filtered"
    if want == 4 and got == 3:
        return "bS 4 applies on macroblock edges of intra macroblocks (frame macroblocks); bS 3 is for internal edges"
    if want == 3 and got == 4:
        return "bS 3 applies to internal edges of intra macroblocks; only MACROBLOCK edges get bS 4"
    return None


def compare_grids(pics, got, limit=None):
    """-> Report.  `got` = parse_dump() result."""
    rep = Report("bS grid / edge flags")
    n = len(pics)
    if len(got) < n:
        rep.check(False, f"the dump has {len(got)} picture(s), the stream has {n}")
    elif limit is None and len(got) > n:
        rep.check(False, f"the dump has {len(got)} pictures, the stream has {n}")
    first = True
    for pi, p in enumerate(pics[:len(got)]):
        grid = bsref.picture_bs(p)
        mbs = got[pi]
        if len(mbs) != len(grid):
            rep.check(False, f"picture {pi}: {len(mbs)} MB lines, expected {len(grid)} (one per macroblock, raster order)")
            continue
        for a, (g, e) in enumerate(zip(grid, mbs)):
            if (e["addr"], e["x"], e["y"]) != (a, a % p.w, a // p.w):
                rep.check(False, f"picture {pi}: MB line {a} (file line {e['line']}) says '{e['addr']} {e['x']} {e['y']}', expected "
                                 f"'{a} {a % p.w} {a // p.w}' (raster order, addr = y * {p.w} + x)")
                continue
            diffs = []          # (which, edge, flag expected, flag got, bS expected (4), bS got (4))
            for which in ("V", "H"):
                fl_key, bs_key = ("fv", "v") if which == "V" else ("fh", "h")
                for edge in range(4):
                    ws, vs = g[bs_key][4 * edge:4 * edge + 4], e[bs_key][4 * edge:4 * edge + 4]
                    if g[fl_key][edge] != e[fl_key][edge] or ws != vs:
                        diffs.append((which, edge, g[fl_key][edge], e[fl_key][edge], ws, vs))
            ok = not diffs
            msg = ""
            if not ok:
                items = []
                for which, edge, fw, fg, ws, vs in diffs[:4]:
                    t = f"{which} edge {edge}:"
                    if fw != fg:
                        t += f" filterEdgeFlag expected {fw}, got {fg};"
                    if ws != vs:
                        t += f" bS of segments 0-3 expected {ws}, got {vs}"
                    items.append(t.rstrip(";"))
                msg = f"picture {pi}, MB {a} (file line {e['line']}): " + " | ".join(items) + (f" | ... {len(diffs) - 4} more edges" if len(diffs) > 4 else "")
                if first:
                    which, edge, fw, fg, ws, vs = diffs[0]
                    seg = next((i for i in range(4) if ws[i] != vs[i]), 0)
                    h = hint(p, a, which, edge, seg, ws[seg], vs[seg], fw, fg)
                    msg += "\n      context: " + "\n               ".join(context(p, a))
                    if h:
                        msg += "\n      hint: " + h
                    first = False
            rep.check(ok, msg)
    return rep


def check_dump(path, text, limit=None, workdir=None):
    wd = workdir or tempfile.mkdtemp(prefix="bs61_")
    try:
        pics = analyse(path, wd, limit)
        got = parse_dump(text)
        return compare_grids(pics, got, limit)
    finally:
        if workdir is None:
            shutil.rmtree(wd, ignore_errors=True)


# ================================================================== validation by consequence
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
    return open(out, "rb").read()


class Consequence:
    """ffmpeg's filtered and unfiltered pictures of a stream, plus the reference parse; run() applies a bS grid + the reference
    filter and compares with ffmpeg's filtered output."""

    def __init__(self, path, workdir, limit=None):
        self.path = path
        self.pics = analyse(path, workdir, limit)
        p0 = self.pics[0]
        self.skip = None
        if (p0.x0, p0.y0) != (0, 0) or p0.cw != 16 * p0.w or p0.ch != 16 * p0.h:
            self.skip = "stream is cropped (the filter needs whole macroblocks)"
            return
        self.w, self.h = 16 * p0.w, 16 * p0.h
        fb = yuvref.frame_bytes(self.w, self.h)
        self.filt = decode_yuv(path, os.path.join(workdir, "f.yuv"), False)
        self.unf = decode_yuv(path, os.path.join(workdir, "u.yuv"), True)
        if len(self.filt) % fb or len(self.filt) // fb < len(self.pics):
            self.skip = "ffmpeg output does not have one frame per picture"

    def planes(self, data, i):
        fb = yuvref.frame_bytes(self.w, self.h)
        base = i * fb
        ys = self.w * self.h
        cs = (self.w // 2) * (self.h // 2)
        mk = lambda off, w, h: [list(data[base + off + r * w: base + off + (r + 1) * w]) for r in range(h)]    # noqa: E731
        return [mk(0, self.w, self.h), mk(ys, self.w // 2, self.h // 2), mk(ys + cs, self.w // 2, self.h // 2)]

    def run(self, only_first=True):
        """-> None if every picture matches, else a description of the first difference."""
        for i, p in enumerate(self.pics):
            planes = self.planes(self.unf, i)
            grid = bsref.picture_bs(p)
            qp = [m.qp for m in p.mbs]
            pcm = [m.pcm for m in p.mbs]
            oa = [2 * p.slices[m.slice].alpha2 for m in p.mbs]
            ob = [2 * p.slices[m.slice].beta2 for m in p.mbs]
            deblock_ref.filter_picture(planes, p.w, grid, qp, pcm, oa, ob, p.chroma_offsets)
            want = self.planes(self.filt, i)
            for pl, name in enumerate("YUV"):
                for y, (a, b) in enumerate(zip(planes[pl], want[pl])):
                    if a != b:
                        x = next(j for j in range(len(a)) if a[j] != b[j])
                        mbs = 16 if pl == 0 else 8
                        return (f"picture {i}, plane {name}, sample x={x} y={y} (macroblock {(y // mbs) * p.w + x // mbs}): "
                                f"reference filter gave {a[x]}, ffmpeg {b[x]}")
        return None


def validate_reference(path, limit=None, quiet=False):
    wd = tempfile.mkdtemp(prefix="bs61v_")
    try:
        c = Consequence(path, wd, limit)
        if c.skip:
            print(f"  skipped {os.path.basename(path)}: {c.skip}")
            return None
        diff = c.run()
        if not quiet:
            print(f"  {os.path.basename(path)}: {'reference bS grid + reference filter reproduce ffmpeg byte for byte' if diff is None else 'DIFFERENT: ' + diff}"
                  f" ({len(c.pics)} pictures)")
        return diff is None
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# ================================================================== test streams
X264_8X8 = "-profile:v high -x264-params cabac=0:keyint=1:8x8dct=1:partitions=i8x8,i4x4"
X264_STREAMS = {
    # name: (codec options, frames).  x264 has no way to write disable_deblocking_filter_idc = 2, so idc 2 comes from bsgen.
    "x_T3_8x8": (X264_8X8, 6),
    "x_T4_8x8": (X264_8X8 + ":slices=4", 6),
    "x_T3_8x8_dp3": (X264_8X8 + ":deblock=3,3", 4),
    "x_T3_8x8_dm3": (X264_8X8 + ":deblock=-3,-3", 4),
}


def make_x264(d, name):
    opts, frames = X264_STREAMS[name]
    out = os.path.join(d, name + ".ts")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=352x288:rate=25", "-frames:v", str(frames), "-pix_fmt",
           "yuv420p", "-c:v", "libx264", "-qp", "26", *opts.split(), "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed for " + name + ": " + r.stderr.decode()[-300:])
    return out


def make_variants(d, quiet=False):
    import bsgen
    from common import testfiles
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
    for name in X264_STREAMS:
        paths[name] = make_x264(d, name)
        if not quiet:
            print(paths[name])
    if not quiet:
        print("recipe streams for the real-encoder runs: python3 -m common.testfiles T3 T3_dm3 T3_dp3 T4 T4_nodeblock  (files in ../.cache)")
        print("run your reader on each, then: python3 test_bs.py <that.ts> <your dump>   (add --pictures 3 while debugging: faster)")
    return paths


def stats(pics):
    """Coverage summary of the streams the reference grid is computed for."""
    c = Counter()
    for p in pics:
        grid = bsref.picture_bs(p)
        for a, g in enumerate(grid):
            m = p.mbs[a]
            c["macroblocks"] += 1
            c["idc%d macroblocks" % p.slices[m.slice].idc] += 1
            c["8x8-transform macroblocks"] += m.t8x8
            c["I_PCM macroblocks"] += m.pcm
            c["bS 4 segments"] += g["v"].count(4) + g["h"].count(4)
            c["bS 3 segments"] += g["v"].count(3) + g["h"].count(3)
            mx, my = a % p.w, a // p.w
            if p.slices[m.slice].idc == 2:
                if mx and p.mbs[a - 1].slice != m.slice:
                    c["idc 2 left edges not filtered (slice boundary)"] += 1
                if my and p.mbs[a - p.w].slice != m.slice:
                    c["idc 2 top edges not filtered (slice boundary)"] += 1
            if p.slices[m.slice].idc == 0:
                if mx and p.mbs[a - 1].slice != m.slice:
                    c["idc 0 left edges filtered across a slice boundary"] += 1
                if my and p.mbs[a - p.w].slice != m.slice:
                    c["idc 0 top edges filtered across a slice boundary"] += 1
    return c


# ================================================================== self test
# (mutant, what is wrong, visible in pixels?)
MUTANTS = [
    ("mbedge_3", "bS 3 instead of 4 on macroblock edges", True),
    ("internal_4", "bS 4 instead of 3 on internal edges", True),
    ("horizontal_mbedge_3", "bS 3 on horizontal macroblock edges and horizontal internal edges only (horizontal edges all 3)", True),
    ("vertical_mbedge_3", "all vertical edges 3", True),
    ("idc2_as_0", "disable_deblocking_filter_idc 2 treated like 0 (filters across slice boundaries)", True),
    ("idc2_as_1", "disable_deblocking_filter_idc 2 treated like 1 (filters nothing)", True),
    ("idc0_as_2", "disable_deblocking_filter_idc 0 treated like 2 (does not filter across slice boundaries)", True),
    ("idc1_ignored", "disable_deblocking_filter_idc 1 ignored (filters like 0)", True),
    ("idc1_still_mb_edges", "idc 1 still filters macroblock edges (only internal edges are switched off)", True),
    ("idc2_top_crosses", "idc 2 stops at slice boundaries on the left but not above", True),
    ("idc2_left_crosses", "idc 2 stops at slice boundaries above but not on the left", True),
    ("t8x8_ignored", "transform_size_8x8_flag ignored: internal edges 4 and 12 filtered", True),
    ("t8x8_skips_edge2", "transform_size_8x8_flag = 1 also skips the edge at 8", True),
    ("internal_skipped", "internal edges never filtered", True),
    ("picture_edge_filtered", "edges on the picture boundary are flagged as filtered", False),
    ("pcm_internal_0", "internal edges of I_PCM macroblocks not filtered (bS 0 instead of 3)", True),
    ("pcm_edges13_0", "edges 1 and 3 of I_PCM macroblocks not filtered (only the luma samples would change)", False),
]
# Mutants that MUST be invisible in the pixels, with the reason (the harness checks both directions).
INVISIBLE_WHY = {
    "picture_edge_filtered": "there are no samples on the other side of a picture edge, a filter would have nothing to read; "
                             "only the dump can show the mistake",
    "pcm_edges13_0": "I_PCM macroblocks have qP 0, so indexA <= 12 and alpha = 0: the luma filter does nothing whatever bS is "
                     "(the chroma edge at 4 chroma samples has a chroma QP and an offset, so it can still move: that is pcm_internal_0)",
}


def mutate_text(text, kind):
    """Textual corruptions of a correct dump -> (wrong text)."""
    lines = text.splitlines()
    if kind == "drop_mb":
        i = next(i for i, l in enumerate(lines) if l.startswith("MB 5 "))
        del lines[i]
    elif kind == "swap_vh":
        out = []
        for l in lines:
            m = MB_RE.match(l)
            if m:
                l = (f"MB {m.group(1)} {m.group(2)} {m.group(3)} FV {m.group(5)} FH {m.group(4)} V {m.group(7)} H {m.group(6)}")
            out.append(l)
        lines = out
    elif kind == "segment_order":
        out = []
        for l in lines:
            m = MB_RE.match(l)
            if m and m.group(1) == "8":
                v = m.group(6).split()
                v[0:4] = v[0:4][::-1]
                v[4:8] = v[4:8][::-1]
                h = m.group(7).split()
                h = h[4:8] + h[0:4] + h[8:]
                l = f"MB {m.group(1)} {m.group(2)} {m.group(3)} FV {m.group(4)} FH {m.group(5)} V {' '.join(v)} H {' '.join(h)}"
            out.append(l)
        lines = out
    elif kind == "no_pic_lines":
        lines = [l for l in lines if not l.startswith("PIC")]
    elif kind == "column_major":
        pic = [i for i, l in enumerate(lines) if l.startswith("PIC")][0]
        end = next((i for i, l in enumerate(lines) if i > pic and l.startswith("PIC")), len(lines))
        body = lines[pic + 1:end]
        w = int(body[-1].split()[2]) + 1
        h = int(body[-1].split()[3]) + 1
        body = [body[y * w + x] for x in range(w) for y in range(h)]
        lines = lines[:pic + 1] + body + lines[end:]
    elif kind == "one_picture_short":
        i = [i for i, l in enumerate(lines) if l.startswith("PIC")][-1]
        lines = lines[:i]
    elif kind == "short_values":
        lines = [re.sub(r" H (\d+ ){15}\d+$", lambda m: m.group(0).rsplit(" ", 1)[0], l) if l.startswith("MB 3 ") else l for l in lines]
    else:
        raise ValueError(kind)
    return "\n".join(lines) + "\n"


TEXT_MUTANTS = ["drop_mb", "swap_vh", "segment_order", "no_pic_lines", "column_major", "one_picture_short", "short_values"]


def self_test():
    import bsgen
    global QUIET
    ffref.require_tools()
    td = tempfile.mkdtemp(prefix="bs61_self_")
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
        print(f"  {len(paths)} generated + {len(recipes)} recipe streams")
        pics_of = {}
        for name, ts in list(paths.items()) + [(n, p) for n, p in recipes.items()]:
            pics_of[name] = analyse(ts, tempfile.mkdtemp(dir=td), 3 if name in recipes else None)
        allstats = Counter()
        for pics in pics_of.values():
            allstats.update(stats(pics))
        print("reference grid coverage over all test streams: " + "; ".join(f"{k} {v}" for k, v in sorted(allstats.items())))
        need = ["idc0 macroblocks", "idc1 macroblocks", "idc2 macroblocks", "8x8-transform macroblocks", "I_PCM macroblocks",
                "idc 2 left edges not filtered (slice boundary)", "idc 2 top edges not filtered (slice boundary)",
                "idc 0 left edges filtered across a slice boundary", "idc 0 top edges filtered across a slice boundary"]
        verdict("every case the mutants below target occurs in the test streams", all(allstats[k] > 0 for k in need),
                ", ".join(k for k in need if not allstats[k]))

        print("1. the correct dump passes (on every stream):")
        texts = {}
        for name, pics in pics_of.items():
            text = render(pics, [bsref.picture_bs(p) for p in pics])
            texts[name] = text
            src = paths.get(name) or recipes[name]
            rep = check_dump(src, text, 3 if name in recipes else None, tempfile.mkdtemp(dir=td))
            if rep.failures:
                verdict(name, False, rep.failures[0][:200])
        verdict("all streams", True)

        print("2. wrong rules (bsref.MUTANT) must produce dumps that fail:")
        for mutant, what, visible in MUTANTS:
            caught_on = None
            for name, pics in pics_of.items():
                bsref.MUTANT = mutant
                try:
                    wrong = render(pics, [bsref.picture_bs(p) for p in pics])
                finally:
                    bsref.MUTANT = None
                if wrong != texts[name]:
                    src = paths.get(name) or recipes[name]
                    rep = check_dump(src, wrong, 3 if name in recipes else None, tempfile.mkdtemp(dir=td))
                    if rep.failures:
                        caught_on = name
                        break
            verdict(f"{mutant} ({what})", caught_on is not None, f"caught on {caught_on}" if caught_on else "NOT detected")

        print("3. corrupted dump files must fail:")
        name = "bs_mix"
        for kind in TEXT_MUTANTS:
            wrong = mutate_text(texts[name], kind)
            try:
                rep = check_dump(paths[name], wrong, None, tempfile.mkdtemp(dir=td))
                caught, how = bool(rep.failures), "reported as a mismatch"
            except DumpError as e:
                caught, how = True, "rejected as malformed: " + str(e)[:70]
            verdict(kind, caught, how)

        print("4. validation by consequence: the reference grid + reference filter vs ffmpeg's deblocked output:")
        cons = {}
        order = [n for n in pics_of]
        for name in order:
            src = paths.get(name) or recipes[name]
            c = Consequence(src, tempfile.mkdtemp(dir=td), 3 if name in recipes else None)
            if c.skip:
                print(f"  skipped {name}: {c.skip}")
                continue
            cons[name] = c
            diff = c.run()
            verdict(name, diff is None, "byte-exact" if diff is None else diff)

        print("5. wrong bS rules fed to the same filter must NOT reproduce ffmpeg (the streams are sensitive to them):")
        for mutant, what, visible in MUTANTS:
            seen_on = None
            if mutant not in INVISIBLE_WHY:
                for name, c in cons.items():
                    bsref.MUTANT = mutant
                    try:
                        diff = c.run()
                    finally:
                        bsref.MUTANT = None
                    if diff is not None:
                        seen_on = name
                        break
                verdict(f"{mutant}", seen_on is not None, f"changes pixels on {seen_on}" if seen_on else "INVISIBLE in every test stream")
            elif mutant == "picture_edge_filtered":
                # a filter cannot even be run on it (no samples beyond the picture edge): nothing to check in the pixels
                verdict(f"{mutant}: not checkable in the pixels (only the dump check above catches it)", True, INVISIBLE_WHY[mutant])
            else:
                # must really be invisible (a mutant that does change pixels would belong in the visible list)
                diffs = []
                for name, c in cons.items():
                    bsref.MUTANT = mutant
                    try:
                        diffs.append(c.run())
                    finally:
                        bsref.MUTANT = None
                inv = all(d is None for d in diffs)
                verdict(f"{mutant}: invisible in the pixels, as expected", inv, INVISIBLE_WHY[mutant])
    finally:
        bsref.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stream", nargs="?")
    ap.add_argument("dump", nargs="?")
    ap.add_argument("--pictures", type=int, metavar="N", help="compare only the first N pictures (your dump may have more)")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--expected-out", metavar="FILE", help="write the reference dump of <stream> to FILE and exit")
    ap.add_argument("--validate-reference", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    ffref.require_tools()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if a.expected_out:
        if not a.stream:
            ap.error("need <stream>")
        wd = tempfile.mkdtemp(prefix="bs61_")
        try:
            pics = analyse(a.stream, wd, a.pictures)
            open(a.expected_out, "w").write(render(pics, [bsref.picture_bs(p) for p in pics]))
            print(f"wrote {a.expected_out}: {len(pics)} pictures")
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        return
    if a.validate_reference:
        if not a.stream:
            ap.error("need <stream>")
        r = validate_reference(a.stream, a.pictures)
        sys.exit(0 if r in (True, None) else 1)
    if not (a.stream and a.dump):
        ap.error("need <in.ts|in.264> <your_bs_dump.txt>")
    if not os.path.isfile(a.stream):
        die(f"input {a.stream!r} not found")
    if not os.path.isfile(a.dump):
        die(f"your dump {a.dump!r} not found")
    try:
        text = open(a.dump, errors="replace").read()
        rep = check_dump(a.stream, text, a.pictures)
    except DumpError as e:
        die(f"malformed dump: {e}")
    rep.finish()


if __name__ == "__main__":
    main()
