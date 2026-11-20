#!/usr/bin/env python3
"""Harness for Chapter 7.5 (the deblocking filter in P pictures: bS 2 / 1 / 0 for inter edges; first complete Baseline decoder).

    python3 test_interdeblock.py <input.ts|input.264> <your_output.yuv> [--bs-dump your_bs.txt]
    python3 test_interdeblock.py <input.ts|input.264> --bs-dump your_bs.txt            # only the bS channel
    python3 test_interdeblock.py --expected-bs-out FILE <input>                        # write the reference bS dump of a stream
    python3 test_interdeblock.py --make-variants <out_dir>                             # extra test inputs (see Readme)
    python3 test_interdeblock.py --self-test                                           # verify the harness itself (a few minutes)

<your_output.yuv> = every decoded frame in output order, 8-bit planar yuv420p, CROPPED as the SPS says, no padding, no headers
(the Chapter 5.1 format).  P-only streams decode in display order.
The optional bS dump has the Chapter 6.1 grammar, one block per picture in decoding order (the Readme gives it in full):
    PIC <n>
    MB <addr> <mbx> <mby> FV f0 f1 f2 f3 FH f0 f1 f2 f3 V <16 bS values> H <16 bS values>

Truth: the bit-exact decode of ffmpeg (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`) compared with common/yuvcmp.py.
ffmpeg cannot dump bS, so the bS channel is judged against an independent reference (spoiler_reference/: interdec.py with
bsref_inter.py and deblock_ref.py).  That reference is itself validated by consequence: it must reproduce ffmpeg's output
byte for byte on every test stream (--self-test does this), which is only possible with correct bS values.  When your pixels
differ, the harness classifies your wrong samples against the reference's unfiltered and filtered pictures and the reference's bS.

Exit 0 = everything compared is identical, 1 = different, 2 = unusable input/output (missing file, malformed dump, a stream this
chapter cannot decode, ...).
"""
import argparse
import collections
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref, ffref, testfiles, yuvcmp, yuvref  # noqa: E402
import bsref_inter  # noqa: E402
import interdeblockgen  # noqa: E402
import interdec  # noqa: E402

SYNTH = ["synth_" + m for m in interdeblockgen.MODES]
SRC = "testsrc2=size=352x288:rate=25"
SRC_NOISY = "testsrc2=size=352x288:rate=25,noise=alls=3:allf=t"
VARIANTS = {   # name -> (source, codec options, rate control)
    "d_ref1": (SRC, "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=1", ["-qp", "30"]),
    "d_ref16": (SRC, "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=16", ["-qp", "30"]),
    "d_slices": (SRC, "-profile:v baseline -bf 0 -x264-params keyint=40:partitions=all:ref=3:slices=4", ["-qp", "32"]),
    "d_offp3": (SRC, "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=3:deblock=3,3", ["-qp", "34"]),
    "d_offm3": (SRC, "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=3:deblock=-3,-3", ["-qp", "28"]),
    "d_noisy_qp36": (SRC_NOISY, "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=2", ["-qp", "36"]),
    "d_main_cip": (SRC, "-profile:v main -bf 0 -x264-params cabac=0:8x8dct=0:weightp=0:keyint=50:partitions=p8x8,p4x4,i4x4:ref=2:"
                   "constrained-intra=1", ["-crf", "26"]),
}


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ================================================================ inputs
def make_variant(name, out_ts, seconds=1):
    src, opts, rc = VARIANTS[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264",
           *rc, *opts.split(), "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die(f"ffmpeg failed for {name}: {r.stderr.decode(errors='replace')[-300:]}")


def make_synth(name, out_264, seed=1):
    data, man = interdeblockgen.make_stream(name[len("synth_"):], seed)
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
    for r in ("T5", "T6"):
        print("recipe", r, "->", testfiles.get(r))


def reference_yuv(path, out):
    ffref.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "yuv420p", out], timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")


# ================================================================ the bS dump (Chapter 6.1 grammar)
class DumpError(Exception):
    pass


MB_RE = re.compile(r"^MB (\d+) (\d+) (\d+) FV ((?:\d+ ){3}\d+) FH ((?:\d+ ){3}\d+) V ((?:\d+ ){15}\d+) H ((?:\d+ ){15}\d+)$")


def render_dump(pics):
    lines = []
    for n, p in enumerate(pics):
        w = p["w_mbs"]
        lines.append(f"PIC {n}")
        for a, g in enumerate(p["bs"]):
            lines.append(f"MB {a} {a % w} {a // w} FV {' '.join(map(str, g['fv']))} FH {' '.join(map(str, g['fh']))} "
                         f"V {' '.join(map(str, g['v']))} H {' '.join(map(str, g['h']))}")
    return "\n".join(lines) + "\n"


def parse_dump(text):
    """-> list of pictures, each a list of dict(addr, x, y, fv, fh, v, h) in file order.  Raises DumpError."""
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
        pics[-1].append(dict(addr=int(m.group(1)), x=int(m.group(2)), y=int(m.group(3)), fv=nums[0], fh=nums[1], v=nums[2], h=nums[3]))
        for name, vals in zip(("FV", "FH", "V", "H"), nums):
            if max(vals) > (1 if name.startswith("F") else 4):
                raise DumpError(f"line {ln}: value {max(vals)} in {name} is out of range (flags are 0/1, bS is 0..4)")
    return pics


def mb_context(pic, addr, which, edge, seg):
    """Lines describing the two 4x4 blocks that meet at one bS segment, for the failure report."""
    bp, w = pic["bspic"], pic["w_mbs"]
    Q = bp.mbs[addr]
    mbedge = edge == 0
    vertical = which == "V"
    qi = (seg * 4 + edge) if vertical else (edge * 4 + seg)
    if mbedge:
        pa = addr - 1 if vertical else addr - pic["w_mbs"]
        pi = (seg * 4 + 3) if vertical else (12 + seg)
    else:
        pa = addr
        pi = (seg * 4 + edge - 1) if vertical else ((edge - 1) * 4 + seg)

    def desc(a, i, label):
        M = bp.mbs[a]
        if M is None:
            return f"  {label}: (no macroblock)"
        if M.pcm:
            return f"  {label}: macroblock {a} (slice {M.slice}), I_PCM"
        if M.intra:
            return f"  {label}: macroblock {a} (slice {M.slice}), intra"
        r, x, y = M.grid[i]
        return (f"  {label}: macroblock {a} (slice {M.slice}), {'P_Skip' if M.skip else 'inter'}, 4x4 block (x={i & 3}, y={i >> 2}): "
                f"reference picture #{r} (ref_idx {M.refidx[i]}), vector ({x},{y}), "
                f"{'has' if M.nz[i] else 'no'} non-zero coefficients")
    return [desc(pa, pi, "p side"), desc(addr, qi, "q side")]


def bs_hint(pic, addr, which, edge, seg, want, got, flag_want, flag_got):
    bp = pic["bspic"]
    Q = bp.mbs[addr]
    mbedge = edge == 0
    vertical = which == "V"
    qi = (seg * 4 + edge) if vertical else (edge * 4 + seg)
    if mbedge:
        pa = addr - 1 if vertical else addr - pic["w_mbs"]
        pi = (seg * 4 + 3) if vertical else (12 + seg)
    else:
        pa, pi = addr, ((seg * 4 + edge - 1) if vertical else ((edge - 1) * 4 + seg))
    P = bp.mbs[pa] if 0 <= pa < len(bp.mbs) else None
    if flag_want != flag_got:
        return "edge flag: see Chapter 6.1 (picture boundary, idc 1, idc 2 across a slice boundary); the flags are the same as in intra pictures"
    if P is None:
        return None
    if want == 4 and got == 3:
        return "intra macroblock edge: bS 4 on macroblock edges (frame macroblocks), 3 inside"
    if want == 3 and got == 4:
        return "bS 4 is for MACROBLOCK edges only; inside an intra macroblock it is 3"
    if (P.intra or Q.intra) and want >= 3 and got < 3:
        return "an edge with an intra macroblock on either side (also I_PCM, also in a P slice) has bS 3 or 4, before any coefficient or motion test"
    if want == 2 and got in (0, 1):
        return "non-zero transform coefficients in the 4x4 block on EITHER side give bS 2 (test per 4x4 block, p and q, before the motion test)"
    if want in (0, 1) and got == 2:
        return ("bS 2 needs non-zero coefficients in the 4x4 block containing p0 or q0 (total_coeff != 0 of THAT luma block; the CBP bit, "
                "chroma coefficients and P_Skip do not count)")
    if want == 1 and got == 0:
        return "motion rule: different reference PICTURES, or a vector component differing by 4 or more quarter samples, gives bS 1"
    if want == 0 and got == 1:
        return ("motion rule too strict: same reference picture and both |dx| < 4 and |dy| < 4 quarter samples give bS 0 "
                "(compare pictures, not ref_idx values; the limit is 4, not 3)")
    return None


def compare_dump(pics, got_text):
    """-> (ok, lines).  pics = reference decoder pictures (dict with bs, bspic, w_mbs)."""
    try:
        got = parse_dump(got_text)
    except DumpError as e:
        return None, [f"malformed bS dump: {e}"]
    L, n_bad = [], 0
    if len(got) != len(pics):
        L.append(f"the dump has {len(got)} picture(s), the stream has {len(pics)}")
        if len(got) < len(pics):
            return False, L
    shown = 0
    for pn, (rp, gp) in enumerate(zip(pics, got)):
        w = rp["w_mbs"]
        nmb = len(rp["bs"])
        if len(gp) != nmb:
            L.append(f"PIC {pn}: {len(gp)} MB lines, expected {nmb} (every macroblock exactly once, raster order)")
            n_bad += 1
            continue
        for a, (g, d) in enumerate(zip(rp["bs"], gp)):
            if d["addr"] != a or d["x"] != a % w or d["y"] != a // w:
                L.append(f"PIC {pn}: line {a} is macroblock {d['addr']} ({d['x']},{d['y']}), expected {a} ({a % w},{a // w})")
                n_bad += 1
                break
            for key, which in (("fv", "V"), ("fh", "H")):
                for e in range(4):
                    if g[key][e] != d[key][e]:
                        n_bad += 1
                        if shown < 10:
                            shown += 1
                            L.append(f"PIC {pn} MB {a}: filter flag F{which}{e}: expected {g[key][e]}, got {d[key][e]}")
            for key, which in (("v", "V"), ("h", "H")):
                for k in range(16):
                    if g[key][k] != d[key][k]:
                        n_bad += 1
                        if shown < 10:
                            shown += 1
                            e, s = divmod(k, 4)
                            L.append(f"PIC {pn} MB {a} (x={a % w}, y={a // w}): {which} edge {e} segment {s}: expected bS {g[key][k]}, got {d[key][k]}")
                            if shown <= 3:
                                L += mb_context(rp, a, which, e, s)
                                h = bs_hint(rp, a, which, e, s, g[key][k], d[key][k], g["f" + key][e], d["f" + key][e])
                                if h:
                                    L.append("  hint: " + h)
    if n_bad:
        L.append(f"{n_bad} differing flag/bS value(s) in total" + (" (first 10 shown)" if n_bad > 10 else ""))
    return n_bad == 0 and len(got) == len(pics), L


# ================================================================ pixel diagnosis
def read_frame(path, idx, fsize):
    with open(path, "rb") as f:
        f.seek(idx * fsize)
        return f.read(fsize)


def seg_candidates(pic, addr, lx, ly):
    """Reference bS values of the vertical / horizontal luma edge segments whose filter touches luma sample (lx, ly) of macroblock
    addr (samples p2..p0 / q0..q2 are modified).  -> list of (orientation, bS)."""
    w = pic["w_mbs"]
    out = []
    for vertical in (True, False):
        c, o = (lx, ly) if vertical else (ly, lx)       # c across the edge, o along it
        cands = []
        if c % 4 <= 2:
            cands.append((addr, c // 4))
        if c % 4 >= 1:
            if c // 4 + 1 == 4:
                na = addr + 1 if vertical else addr + w
                if (vertical and addr % w < w - 1) or (not vertical and na < len(pic["bs"])):
                    cands.append((na, 0))
            else:
                cands.append((addr, c // 4 + 1))
        for a, e in cands:
            g = pic["bs"][a]
            b = (g["v"] if vertical else g["h"])[e * 4 + o // 4]
            out.append(("V" if vertical else "H", b))
    return out


def diagnose(path, ref_yuv, act_yuv, res, w, h, origin, workdir):
    """-> list of text lines explaining the first bad frame (or why no explanation is available)."""
    frame = res["first_diff"]["frame"]
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
    unf = interdec.frame_bytes(dict(pic, planes=pic["unfiltered"]))
    if mine != exp:
        L.append("WARNING: the harness's own reference decoder does not reproduce ffmpeg's frame %d exactly; "
                 "the advice below may be unreliable." % frame)
    x0, y0 = origin
    wmb = pic["w_mbs"]
    cls = collections.Counter()               # luma: how the wrong samples relate to the reference's unfiltered / filtered picture
    near = collections.Counter()              # luma wrong samples next to a segment of reference bS k (k = 0..4); a sample counts once per k
    nobs = collections.Counter()              # how many wrong samples are next to NO filtered segment (bS > 0)
    chroma_bad = 0
    bad_mb = collections.Counter()
    wrong_per_bs_seg = collections.Counter()
    for pl, (name, pw, ph, off) in enumerate(geo):
        for y in range(ph):
            ra, rb = exp[off + y * pw: off + (y + 1) * pw], act[off + y * pw: off + (y + 1) * pw]
            if ra == rb:
                continue
            for x in range(pw):
                if ra[x] == rb[x]:
                    continue
                if pl:
                    chroma_bad += 1
                    bad_mb[((y0 // 2 + y) // 8) * wmb + (x0 // 2 + x) // 8] += 1
                    continue
                X, Y = x0 + x, y0 + y
                addr = (Y // 16) * wmb + X // 16
                bad_mb[addr] += 1
                u = unf[off + y * pw + x]
                cls["not filtered by you (your sample = unfiltered)" if rb[x] == u else
                    "filtered by you, not by ffmpeg" if ra[x] == u else "filtered differently"] += 1
                cands = seg_candidates(pic, addr, X % 16, Y % 16)
                vals = {b for _, b in cands}
                for b in vals:
                    near[b] += 1
                if not any(b > 0 for b in vals):
                    nobs["none"] += 1
    first = min(bad_mb)
    L.append(f"first wrong macroblock in raster order of frame {frame}: address {first} (x={first % wmb}, y={first // wmb})")
    mb = pic["mbs"].get(first)
    bp = pic["bspic"].mbs[first]
    if mb is not None:
        L.append(f"  type {mb['name']}, cbp {mb['cbp']}, QP_Y {mb['qp']}, slice {bp.slice} "
                 f"(disable_deblocking_filter_idc {pic['bspic'].slices[bp.slice].idc})")
        if bp.grid:
            L.append("  4x4 blocks (raster): reference picture id / vector / coefficients")
            for r in range(4):
                L.append("    " + "  ".join(f"#{bp.grid[4 * r + c][0]}({bp.grid[4 * r + c][1]},{bp.grid[4 * r + c][2]})"
                                            f"{'*' if bp.nz[4 * r + c] else ' '}" for c in range(4)) + "     (* = non-zero coefficients)")
    g = pic["bs"][first]
    L.append(f"  reference bS of this macroblock: V " + " ".join("".join(map(str, g["v"][4 * e:4 * e + 4])) for e in range(4))
             + " | H " + " ".join("".join(map(str, g["h"][4 * e:4 * e + 4])) for e in range(4)) + "   (edge 0..3, segments 0..3)")
    tot = sum(cls.values())
    L.append(f"frame {frame}: {tot} wrong luma samples in {len(bad_mb)} macroblock(s), {chroma_bad} wrong chroma samples")
    for k, v in cls.most_common():
        L.append(f"  luma: {v} {k}")
    L.append("  wrong luma samples next to a filtered edge segment of reference bS: "
             + ", ".join(f"{b}: {near[b]}" for b in range(5) if near[b]) + f"; next to no filtered segment at all: {nobs['none']}")
    H = []
    if tot == 0 and chroma_bad:
        H.append("only chroma is wrong: chroma edges use the LUMA bS (no separate chroma coefficient test), only the edges at 0 and 8 luma "
                 "samples (chroma 0 and 4), the 2-sample-wide chroma filter, and the chroma QPs (QP_C of each side, then averaged).")
    if nobs["none"]:
        H.append("some wrong samples are next to no edge that the reference filters (bS 0): either you filter an edge that should "
                 "have bS 0 (bS too high: motion threshold, ref_idx compared instead of pictures, ...), or the reconstruction before the "
                 "filter is wrong (prediction, residual, or a wrong reference picture - references must be DEBLOCKED pictures). "
                 "--bs-dump tells the two apart.")
    if cls["not filtered by you (your sample = unfiltered)"] and cls["not filtered by you (your sample = unfiltered)"] * 2 >= tot:
        H.append("most wrong samples equal the UNFILTERED value: you leave edges unfiltered that ffmpeg filters (bS too low or an edge "
                 "missing). Look at the bS lines of the edges next to them.")
    if cls["filtered by you, not by ffmpeg"] and cls["filtered by you, not by ffmpeg"] * 2 >= tot:
        H.append("most wrong samples are filtered by you but not by ffmpeg: your bS is too high (or you filter an edge that has flag 0).")
    if cls["filtered differently"] and cls["filtered differently"] * 2 >= tot:
        H.append("the wrong samples are filtered by both, with a different result: bS 1 vs 2 (tC0) or 3 vs 4, or a different QP / "
                 "offset / threshold; compare your bS grid with --bs-dump first.")
    if near[1] and not near[2] and not near[3] and not near[4] and not nobs["none"]:
        H.append("only edges with reference bS 1 are involved: the motion rule (reference PICTURES differ, or |dmv| >= 4 in a component).")
    if near[2] and not near[1] and not near[3] and not near[4] and not nobs["none"]:
        H.append("only edges with reference bS 2 are involved: the coefficient rule (total_coeff != 0 of the 4x4 block on either side).")
    if (near[3] or near[4]) and not near[1] and not near[2] and not nobs["none"]:
        H.append("only edges with intra macroblocks (bS 3/4) are involved: intra macroblocks inside P pictures (and I_PCM) follow the "
                 "same bS 4 / 3 rule; check also that the QP used for I_PCM is 0.")
    if not H:
        H.append("no single pattern; run with --bs-dump to compare the bS grid, and check that stored references are the filtered pictures.")
    L.append("hints:")
    L += ["  - " + x for x in H]
    return L


# ================================================================ main check
def stream_geometry(path, td):
    infos = cavlc_ref.load_stream(path, td)
    if not infos:
        raise SystemExit("no slices in the stream")
    s0 = infos[0]
    sps = s0.sps
    return yuvref.cropped_size(
        s0.pic_width_in_mbs, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
        sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
        sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))


def run_check(path, yuv, dump_text=None, quiet=False, workdir=None, expected=None):
    """-> (exit code, text).  yuv / dump_text may be None (that channel is then not checked)."""
    td = workdir or tempfile.mkdtemp(prefix="interdb_")
    out = []

    def say(s):
        out.append(s)
        if not quiet:
            print(s)
    worst = 0
    if yuv is not None:
        try:
            w, h, x0, y0 = stream_geometry(path, td)
        except SystemExit as e:
            return 2, f"input problem: {e}"
        ref_yuv = expected or os.path.join(td, "expected.yuv")
        if not expected:
            reference_yuv(path, ref_yuv)
        if not os.path.isfile(yuv):
            return 2, f"your output {yuv!r} does not exist"
        res = yuvcmp.compare_yuv(ref_yuv, yuv, w, h, quiet=True, origin=(x0, y0))
        say(res["text"])
        if res["ok"]:
            say(f"PIXELS: all {res['frames_expected']} frames byte-identical to ffmpeg")
        elif any("does not exist" in p for p in res["size_problems"]):
            return 2, "\n".join(out)
        else:
            worst = 1
            if res["first_diff"]:
                say("---- diagnosis (reference decoder in spoiler_reference/) ----")
                for line in diagnose(path, ref_yuv, yuv, res, w, h, (x0, y0), td):
                    say(line)
            say("PIXELS: output differs from ffmpeg's decode")
    if dump_text is not None:
        try:
            pics = interdec.decode(path, workdir=td, pixels=False)
        except interdec.Unsupported as e:
            return 2, "\n".join(out + [f"bS dump cannot be checked: {e}"])
        ok, lines = compare_dump(pics, dump_text)
        if ok is None:
            say(lines[0])
            return 2, "\n".join(out)
        for line in lines:
            say(line)
        say("BS DUMP: " + ("identical to the reference grid" if ok else "differs from the reference grid"))
        if not ok:
            worst = 1
    say(("PASS" if worst == 0 else "FAIL") + ": Chapter 7.5")
    return worst, "\n".join(out)


# ================================================================ statistics (sensitivity of the test streams)
def edge_stats(pics):
    """Counts of what decides each filtered inter segment of the reference pictures (proves the streams are sensitive)."""
    c = collections.Counter()
    for p in pics:
        bp = p["bspic"]
        for a, g in enumerate(p["bs"]):
            Q = bp.mbs[a]
            for vertical in (True, False):
                for e in range(4):
                    if not (g["fv"] if vertical else g["fh"])[e]:
                        continue
                    for s in range(4):
                        qi = (s * 4 + e) if vertical else (e * 4 + s)
                        if e == 0:
                            pa = a - 1 if vertical else a - bp.w
                            pi = (s * 4 + 3) if vertical else (12 + s)
                        else:
                            pa, pi = a, ((s * 4 + e - 1) if vertical else ((e - 1) * 4 + s))
                        P = bp.mbs[pa]
                        b = (g["v"] if vertical else g["h"])[e * 4 + s]
                        c[f"bS{b}"] += 1
                        if P.intra or Q.intra:
                            continue
                        if P.nz[pi] != Q.nz[qi]:
                            c["coef on one side only"] += 1
                        if not (P.nz[pi] or Q.nz[qi]):
                            (rp, xp, yp), (rq, xq, yq) = P.grid[pi], Q.grid[qi]
                            if rp != rq:
                                c["different reference pictures"] += 1
                            else:
                                d = max(abs(xp - xq), abs(yp - yq))
                                if d in (3, 4, 5):
                                    c[f"same picture, largest |dmv| = {d}"] += 1
                        if P.skip or Q.skip:
                            c["edge touching P_Skip"] += 1
                        if e == 0 and P.slice != Q.slice:
                            c["macroblock edge between slices"] += 1
    return c


# ================================================================ unit cases for the bS function
def unit_cases():
    """Hand-built (description, p-block, q-block, mb_edge, expected bS).  Blocks: dict(intra, pcm, skip, nz, ref pic id, ref_idx, mv)."""
    def inter(pic, mv, nz=False, idx=0, skip=False):
        return dict(intra=False, pcm=False, skip=skip, nz=nz, pic=pic, idx=idx, mv=mv)
    intra = dict(intra=True, pcm=False, skip=False, nz=False, pic=None, idx=-1, mv=(0, 0))
    pcm = dict(intra=True, pcm=True, skip=False, nz=True, pic=None, idx=-1, mv=(0, 0))
    return [
        ("same picture, vectors differ by (3,3): 0", inter(5, (0, 0)), inter(5, (3, 3)), False, 0),
        ("same picture, horizontal difference exactly 4: 1", inter(5, (0, 0)), inter(5, (4, 0)), False, 1),
        ("same picture, vertical difference exactly -4: 1", inter(5, (0, 0)), inter(5, (0, -4)), False, 1),
        ("same picture, difference 5 in y: 1", inter(5, (7, 1)), inter(5, (7, 6)), True, 1),
        ("different reference pictures, identical vectors: 1", inter(5, (2, 2)), inter(4, (2, 2)), True, 1),
        ("same picture reached by DIFFERENT ref_idx (possible from 7.6 on): 0", inter(5, (2, 2), idx=0), inter(5, (2, 2), idx=2), False, 0),
        ("different pictures with the same ref_idx (other slice's list): 1", inter(5, (2, 2), idx=1), inter(4, (2, 2), idx=1), True, 1),
        ("coefficients only on p: 2", inter(5, (0, 0), nz=True), inter(5, (0, 0)), False, 2),
        ("coefficients only on q: 2", inter(5, (0, 0)), inter(5, (0, 0), nz=True), True, 2),
        ("coefficients and different pictures: 2 (coefficients first)", inter(5, (0, 0), nz=True), inter(4, (9, 9)), False, 2),
        ("P_Skip next to P_Skip with the same vector: 0", inter(5, (-6, 3), skip=True), inter(5, (-6, 3), skip=True), True, 0),
        ("P_Skip (vector 0,0) next to an inter block with vector (4,0): 1", inter(5, (0, 0), skip=True), inter(5, (4, 0)), True, 1),
        ("intra on q, macroblock edge: 4", inter(5, (0, 0)), intra, True, 4),
        ("intra on p, macroblock edge: 4", intra, inter(5, (0, 0), nz=True), True, 4),
        ("intra on q, internal edge: 3", intra, intra, False, 3),
        ("I_PCM next to inter, macroblock edge: 4", pcm, inter(5, (0, 0)), True, 4),
    ]


def unit_bs(case):
    _, p, q, mbedge, _ = case

    def mk(b):
        M = bsref_inter.Mb(0, intra=b["intra"], pcm=b["pcm"], skip=b["skip"])
        M.nz = [b["nz"]] * 16
        M.cbp_luma = 15 if b["nz"] else 0
        if not b["intra"]:
            M.grid = [(b["pic"], b["mv"][0], b["mv"][1])] * 16
            M.refidx = [b["idx"]] * 16
        return M
    return bsref_inter.seg_bs(mk(p), 0, mk(q), 0, mbedge)


def run_units(mutant=None):
    bsref_inter.MUTANT = mutant
    try:
        return [(c[0], c[4], unit_bs(c)) for c in unit_cases()]
    finally:
        bsref_inter.MUTANT = None


# ================================================================ self-test
def corrupt_text(text, kind):
    lines = text.splitlines()
    mbs = [i for i, ln in enumerate(lines) if ln.startswith("MB ")]
    if kind == "one_bs":
        i = mbs[len(mbs) // 2]
        parts = lines[i].split()
        k = parts.index("V") + 1 + 5
        parts[k] = str((int(parts[k]) + 1) % 5)
        lines[i] = " ".join(parts)
    elif kind == "flag":
        i = mbs[7]
        parts = lines[i].split()
        k = parts.index("FH") + 2
        parts[k] = str(1 - int(parts[k]))
        lines[i] = " ".join(parts)
    elif kind == "drop_mb":
        del lines[mbs[10]]
    elif kind == "drop_pic":
        j = [i for i, ln in enumerate(lines) if ln.startswith("PIC ")][-1]
        lines = lines[:j]
    elif kind == "garbage":
        lines[mbs[3]] = "MB 3 oops"
    return "\n".join(lines) + "\n"


def self_test():
    td = tempfile.mkdtemp(prefix="interdb_selftest_")
    problems = []
    t_start = time.time()

    def bad(msg):
        problems.append(msg)
        print("  PROBLEM:", msg)
    try:
        # ---------------------------------------------------------------- 1. streams and the validation of the reference
        streams, expected, refpics = [], {}, {}
        cov = collections.Counter()
        print("1. the reference decoder must reproduce ffmpeg byte for byte (this validates its bS rules by consequence)")
        for n in SYNTH:
            p = os.path.join(td, n + ".264")
            make_synth(n, p)
            streams.append(p)
        for n in VARIANTS:
            p = os.path.join(td, n + ".ts")
            make_variant(n, p)
            streams.append(p)
        streams.append(testfiles.get("T6"))
        usable = []
        for s in streams:
            name = os.path.basename(s)
            wd = os.path.join(td, "w_" + name)
            os.makedirs(wd)
            ry = os.path.join(wd, "expected.yuv")
            reference_yuv(s, ry)
            expected[s] = ry
            y = os.path.join(wd, "ref.yuv")
            interdec.MUTANT = None
            try:
                pics = interdec.decode(s, workdir=wd, record=False)
            except interdec.Unsupported as e:
                print(f"  {name}: skipped by the reference decoder ({e})")
                continue
            interdec.write_yuv(pics, y)
            usable.append(s)
            refpics[s] = pics
            code, text = run_check(s, y, quiet=True, workdir=wd, expected=ry)
            print(f"  {name:28s} {len(pics):3d} pictures: {'identical to ffmpeg' if code == 0 else 'DIFFERENT'}")
            if code != 0:
                bad(f"the reference decoder does not reproduce ffmpeg on {name}:\n{text[-500:]}")
            cov.update(edge_stats(pics))
        synth = [s for s in usable if s.endswith(".264")]
        if len(synth) != len(SYNTH):
            bad("the reference decoder cannot decode all synthetic streams")
        if "T6.ts" not in [os.path.basename(s) for s in usable] or "d_ref1.ts" not in [os.path.basename(s) for s in usable]:
            bad("T6 / d_ref1 must be decodable by the reference")
        print("   sensitivity of the streams (filtered segments of all streams): " + ", ".join(f"{k}: {v}" for k, v in sorted(cov.items())))
        for key in ("bS0", "bS1", "bS2", "bS3", "bS4", "coef on one side only", "different reference pictures",
                    "same picture, largest |dmv| = 3", "same picture, largest |dmv| = 4", "same picture, largest |dmv| = 5",
                    "edge touching P_Skip", "macroblock edge between slices"):
            if not cov[key]:
                bad(f"no test stream exercises '{key}'")
        # ---------------------------------------------------------------- 2. corrupted pixel outputs
        print("2. corrupted outputs must be rejected")
        s = synth[0]
        wd = os.path.join(td, "w_" + os.path.basename(s))
        good = os.path.join(wd, "ref.yuv")
        data = bytearray(open(good, "rb").read())
        fs = 96 * 64 * 3 // 2
        for label, mod in (("one luma sample changed", lambda d: d.__setitem__(3 * fs + 700, d[3 * fs + 700] ^ 1)),
                           ("last byte missing", lambda d: d.pop()),
                           ("one chroma sample changed", lambda d: d.__setitem__(2 * fs + 96 * 64 + 5, d[2 * fs + 96 * 64 + 5] ^ 4))):
            d = bytearray(data)
            mod(d)
            fp = os.path.join(td, "corrupt.yuv")
            open(fp, "wb").write(bytes(d))
            code, _ = run_check(s, fp, quiet=True, workdir=wd, expected=expected[s])
            print(f"   {label}: exit {code}")
            if code != 1:
                bad(f"corrupted output ({label}) not rejected (exit {code})")
        code, _ = run_check(s, os.path.join(td, "nonexistent.yuv"), quiet=True, workdir=wd, expected=expected[s])
        print(f"   missing output file: exit {code}")
        if code != 2:
            bad("missing output must give exit 2")
        # ---------------------------------------------------------------- 3. the bS dump channel
        print("3. the bS dump channel")
        s = [x for x in synth if "mix" in x][0]
        wd = os.path.join(td, "w_" + os.path.basename(s))
        text = render_dump(refpics[s])
        code, _ = run_check(s, None, dump_text=text, quiet=True, workdir=wd)
        print(f"   reference dump: exit {code}")
        if code != 0:
            bad("the reference's own dump must pass")
        for kind, want in (("one_bs", 1), ("flag", 1), ("drop_mb", 1), ("drop_pic", 1), ("garbage", 2)):
            code, txt = run_check(s, None, dump_text=corrupt_text(text, kind), quiet=True, workdir=wd)
            print(f"   dump corrupted ({kind}): exit {code}")
            if code != want:
                bad(f"corrupted dump ({kind}) gave exit {code}, expected {want}")
        # ---------------------------------------------------------------- 4. unit cases
        print("4. unit cases of the bS function (also listed in the Readme)")
        for desc, want, got in run_units():
            if want != got:
                bad(f"unit case '{desc}': expected {want}, reference gives {got}")
        print(f"   {len(unit_cases())} cases, all as expected")
        # ---------------------------------------------------------------- 5. mutants
        expect_hint = {   # mutant -> text that the diagnosis (pixel failure) should contain
            "mv_thr_3": "too high", "mv_thr_5": "UNFILTERED", "ref_ignored": "UNFILTERED",
        }
        equivalent = {"ref_idx_compare": "equivalent on streams until 7.6 (default lists never put one picture at two ref_idx): caught by unit case only"}
        bs_mut = list(bsref_inter.MUTANTS)
        dec_mut = [m for m in interdec.DECODER_MUTANTS if m in ("ref_unfiltered", "intra_from_filtered", "chroma_bs_own",
                                                                   "skip_qp_slice", "no_deblock_in_p", "ref_list_ascending",
                                                                   "ref_always_zero")]
        print(f"5. {len(bs_mut) + len(dec_mut)} deliberately wrong rules; each must change the output of some stream "
              f"(or fail a unit case / the dump check, for the ones the pixels cannot show)")
        # sensitive streams first, T6 prefix last
        order = sorted(synth, key=lambda x: {"motion": 0, "coef": 1, "mix": 2, "slices": 3, "refs": 4, "intra": 5}[os.path.basename(x)[6:-4]])
        t6 = [x for x in usable if x.endswith("T6.ts")][0]
        n_pix = 0
        for m in bs_mut + dec_mut:
            caught, hint_ok = None, None
            if m in bs_mut:
                unit_fail = [d for d, wv, gv in run_units(m) if wv != gv]
            else:
                unit_fail = []
            for s in order:
                wd = os.path.join(td, "w_" + os.path.basename(s))
                y = os.path.join(wd, "mut.yuv")
                interdec.MUTANT = m
                try:
                    interdec.write_yuv(interdec.decode(s, workdir=wd), y)
                except interdec.Unsupported:
                    continue
                finally:
                    interdec.MUTANT = None
                code, txt = run_check(s, y, quiet=True, workdir=wd, expected=expected[s])
                if code == 1:
                    caught = os.path.basename(s)[6:-4]
                    if m in expect_hint:
                        hint_ok = expect_hint[m] in txt.split("hints:")[-1]
                    break
            tag = f"changes pixels of '{caught}'" if caught else "NOT visible in the pixels of the synthetic streams"
            if caught and m in expect_hint:
                tag += "; advice " + ("ok" if hint_ok else "MISSING '%s'" % expect_hint[m])
            if unit_fail:
                tag += f"; fails {len(unit_fail)} unit case(s)"
            print(f"   {m:24s} {tag}")
            if caught:
                n_pix += 1
            if m in equivalent:
                if caught:
                    bad(f"mutant {m} was expected to be equivalent on streams but changed pixels")
                elif not unit_fail:
                    bad(f"mutant {m} is neither visible in streams nor in the unit cases")
                continue
            if not caught:
                bad(f"wrong rule {m!r} was not detected by any synthetic stream")
            elif m in expect_hint and not hint_ok:
                bad(f"wrong rule {m!r}: diagnosis does not mention {expect_hint[m]!r}")
        print(f"   {n_pix} of {len(bs_mut) + len(dec_mut)} wrong rules visible in the pixels; ref_idx_compare: {equivalent['ref_idx_compare']}")
        # bS mutants through the dump channel on a sensitive stream
        print("6. bS-dump channel: every wrong bS rule must make the dump differ somewhere (on the synthetic streams)")
        for m in bs_mut:
            hit = None
            for s in order:
                interdec.MUTANT = m
                try:
                    pics = interdec.decode(s, workdir=td, pixels=False)
                finally:
                    interdec.MUTANT = None
                code, _ = run_check(s, None, dump_text=render_dump(pics), quiet=True, workdir=td)
                if code == 1:
                    hit = os.path.basename(s)[6:-4]
                    break
            print(f"   {m:24s} {'dump differs on ' + hit if hit else 'dump identical (not visible)'}")
            if not hit and m not in equivalent:
                bad(f"wrong bS rule {m!r} does not change the bS dump of any stream")
    finally:
        interdec.MUTANT = None
        bsref_inter.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    print(f"(self-test took {time.time() - t_start:.0f} s)")
    if problems:
        print(f"SELF-TEST FAIL: {len(problems)} problem(s)")
        return 1
    print("SELF-TEST PASS")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="input .ts / .264 (P-picture CAVLC Baseline-style stream, deblocking on or off)")
    ap.add_argument("output", nargs="?", help="your decoded frames, raw yuv420p, cropped (optional when only --bs-dump is checked)")
    ap.add_argument("--bs-dump", metavar="FILE", help="your bS dump (Chapter 6.1 grammar) to compare with the reference grid")
    ap.add_argument("--expected-bs-out", metavar="FILE", help="write the reference bS dump of <input> to FILE and exit")
    ap.add_argument("--make-variants", metavar="DIR", help="write extra test inputs into DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if not a.input:
        ap.print_usage(sys.stderr)
        die("error: need <input> and <your_output.yuv> and/or --bs-dump FILE")
    if not os.path.isfile(a.input):
        die(f"error: input file {a.input!r} not found")
    td = tempfile.mkdtemp(prefix="interdb_")
    try:
        if a.expected_bs_out:
            try:
                pics = interdec.decode(a.input, workdir=td, pixels=False)
            except interdec.Unsupported as e:
                die(f"error: {e}")
            with open(a.expected_bs_out, "w") as f:
                f.write(render_dump(pics))
            print(f"wrote the reference bS dump ({len(pics)} pictures) to {a.expected_bs_out}")
            return
        if not a.output and not a.bs_dump:
            die("error: need <your_output.yuv> and/or --bs-dump FILE")
        dump_text = None
        if a.bs_dump:
            if not os.path.isfile(a.bs_dump):
                die(f"error: your bS dump {a.bs_dump!r} not found")
            dump_text = open(a.bs_dump, errors="replace").read()
        code, _ = run_check(a.input, a.output, dump_text=dump_text, workdir=td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
