#!/usr/bin/env python3
"""Harness for Chapter 7.9 (direct prediction: B_Skip, B_Direct_16x16, B_Direct_8x8, spatial and temporal; implicit weights).

    python3 test_direct.py <input.ts|input.264> <your_output.yuv>                  # pixels (the milestone: T9_spatial, T9_temporal)
    python3 test_direct.py <input> [<your_output.yuv>] [--mv-dump F] [--weights-dump F]      # optional channels
    python3 test_direct.py --print-expected mv|weights <input> <out.txt>           # the expected text of a channel
    python3 test_direct.py --make-variants <out_dir>                               # extra test inputs (see the Readme)
    python3 test_direct.py --self-test                                             # verify the harness itself (about 2 minutes on 12 cores, 10 CPU-minutes in total)

<your_output.yuv> = every decoded frame in DISPLAY order (frames sorted by picture order count inside each IDR period), 8-bit planar
yuv420p, CROPPED as the SPS says, no padding, no headers (Chapter 5.1's format).

Truth.  Pixels: ffmpeg, decoded with ONE thread (`-threads 1 -fps_mode passthrough -f rawvideo -pix_fmt yuv420p`; frame-threaded
ffmpeg is not reliable for temporal direct prediction when a picture has several slices), compared with common/yuvcmp.py.  The only
exception are synthetic streams whose JSON manifest says "ffmpeg_ok": false: there ffmpeg is known to deviate from the standard and
the expected pixels come from the reference decoder in spoiler_reference/ (the harness says so).  When your pixels differ, the
harness decodes the stream with that reference decoder up to the first wrong frame, tells you the first wrong macroblock (type,
direct mode, lists, every prediction block with its lists, ref_idx, reference picture, vector and weights, and for a direct block
the co-located block it read) and checks whether your frame equals the reference decoder with ONE deliberately wrong rule (the
list in the Readme).

Optional channels (grammars in the Readme, section 5):
  --mv-dump       final motion vectors of both lists, one line per INTER macroblock (P_Skip, P and B types, B_Skip and direct ones)
  --weights-dump  implicit bi-prediction weights per macroblock of B slices with weighted_bipred_idc 2

Truth of the dumps = the reference implementation in spoiler_reference/.  Before your dump is compared, the reference is checked
against independent sources (a failure there is a HARNESS PROBLEM, exit 2): its decoded pixels equal ffmpeg's, and the motion
vectors that ffmpeg exports (`-flags2 +export_mvs`, one vector per 8x8 for direct macroblocks) equal the reference's.
Exit 0 = pass, 1 = different, 2 = unusable input / output or harness problem.
"""
import argparse
import collections
import functools
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles, yuvcmp  # noqa: E402
import ddec  # noqa: E402
import dgen  # noqa: E402
import direct_ref  # noqa: E402

SRC = "testsrc2=size=352x288:rate=25"
X264 = "cabac=0:weightb=1:b-pyramid=normal:keyint=100"
VARIANTS = {   # name -> (video filter, codec options): x264 streams WITH direct prediction
    "d_auto": ("null", f"-profile:v main -x264-params {X264}:bframes=3:direct=auto"),
    "d_sp_bf1": ("null", "-profile:v main -x264-params cabac=0:weightb=1:b-pyramid=none:keyint=100:bframes=1:direct=spatial"),
    "d_tmp_bf2": ("null", "-profile:v main -x264-params cabac=0:weightb=1:b-pyramid=none:keyint=100:bframes=2:direct=temporal"),
    "d_sp_bf4": ("null", f"-profile:v main -x264-params {X264}:bframes=4:b-adapt=2:ref=5:direct=spatial"),
    "d_tmp_bf4": ("null", f"-profile:v main -x264-params {X264}:bframes=4:b-adapt=2:ref=5:direct=temporal"),
    "d_sp_strict": ("null", "-profile:v main -x264-params cabac=0:weightb=1:b-pyramid=strict:keyint=100:bframes=3:direct=spatial"),
    "d_tmp_strict": ("null", "-profile:v main -x264-params cabac=0:weightb=1:b-pyramid=strict:keyint=100:bframes=3:direct=temporal"),
    "d_sp_wb0": ("null", "-profile:v main -x264-params cabac=0:weightb=0:b-pyramid=normal:keyint=100:bframes=3:direct=spatial"),
    "d_tmp_wb0": ("null", "-profile:v main -x264-params cabac=0:weightb=0:b-pyramid=normal:keyint=100:bframes=3:direct=temporal"),
    "d_sp_slices": ("null", f"-profile:v main -x264-params {X264}:bframes=3:direct=spatial:slices=3"),
    "d_tmp_slices": ("null", f"-profile:v main -x264-params {X264}:bframes=3:direct=temporal:slices=3"),
    "d_sp_part": ("null", f"-profile:v main -x264-params {X264}:bframes=3:direct=spatial:partitions=all:ref=4"),
    "d_tmp_part": ("null", f"-profile:v main -x264-params {X264}:bframes=3:direct=temporal:partitions=all:ref=4"),
    "d_nodeblock": ("null", f"-profile:v main -x264-params {X264}:bframes=3:direct=auto:no-deblock=1"),
    "d_fade": ("fade=in:0:25", f"-profile:v main -x264-params {X264}:bframes=2:direct=auto:weightp=2"),
    "d_idr": ("null", "-profile:v main -x264-params cabac=0:weightb=1:b-pyramid=normal:keyint=10:bframes=3:direct=temporal"),
}
SYNTH = ["synth_" + m for m in dgen.SCENARIOS]


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ================================================================ inputs
def make_variant(name, out_ts, seconds=1):
    vf, opts = VARIANTS[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC, "-t", str(seconds), "-vf", vf, "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-qp", "26", *opts.split(), "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die(f"ffmpeg failed for {name}: {r.stderr.decode(errors='replace')[-300:]}")


def make_synth(name, out_264, seed=7):
    dgen.write_stream(name[len("synth_"):], out_264, seed)


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for n in ("T9_spatial", "T9_temporal"):
        p = os.path.join(out_dir, n + ".ts")
        shutil.copy(testfiles.get(n), p)
        print("wrote", p)
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for n in SYNTH:
        p = os.path.join(out_dir, n + ".264")
        make_synth(n, p)
        print("wrote", p, "and", p + ".json")


def ffmpeg_yuv(path, out):
    """ffmpeg's decode, one thread, display order, cropped, yuv420p."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-threads", "1"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", out]
    ffref.run(cmd, timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")


def manifest_of(path):
    try:
        with open(path + ".json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ffmpeg_is_truth(path):
    m = manifest_of(path)
    return m is None or m.get("ffmpeg_ok", True)


def expected_yuv(path, out, workdir):
    """-> source description.  Writes the expected frames to `out`."""
    if ffmpeg_is_truth(path):
        ffmpeg_yuv(path, out)
        return "ffmpeg (one thread)"
    ddec.write_yuv(ddec.decode(path, workdir=workdir), out)
    return "the reference decoder (this synthetic stream reads a co-located picture whose slices used different reference lists; " \
           "ffmpeg keeps one list table per picture and does not follow the standard there)"


# ================================================================ expected dumps
def fmt_entry(ref, mvx, mvy):
    return f"{ref}:{mvx}:{mvy}" if ref >= 0 else "-"


def mb_records(pics):
    """-> [(pid, rec, slice_info)] over all macroblock records in decoding order."""
    out = []
    for d in sorted((p for p in pics if p is not None), key=lambda p: p["pid"]):
        for r in d["results"]:
            for rec in r.mbs:
                out.append((d["pid"], rec, r.info))
    return out


def render_mv(pics):
    lines = []
    for pid, rec, _ in mb_records(pics):
        if rec.grid is None:
            continue
        l0 = ",".join(fmt_entry(g[0], g[1], g[2]) for g in rec.grid)
        l1 = ",".join(fmt_entry(g[3], g[4], g[5]) for g in rec.grid)
        lines.append(f"MV {pid} {rec.addr} l0={l0} l1={l1}")
    return "\n".join(lines) + "\n"


def render_weights(pics):
    lines = []
    for pid, rec, _ in mb_records(pics):
        wg = getattr(rec, "wgrid", None)
        if not wg or not any(wg):
            continue
        lines.append(f"WT {pid} {rec.addr} w=" + ",".join(f"{w[0]}:{w[1]}" if w else "-" for w in wg))
    return "\n".join(lines) + "\n"


# ================================================================ dump parsing and comparison
class DumpError(Exception):
    pass


def parse_grid(tok, n, no, what, entry):
    items = tok.split(",")
    if len(items) != n:
        raise DumpError(f"line {no}: {what} has {len(items)} entries, expected {n}")
    out = []
    for it in items:
        try:
            out.append(entry(it))
        except ValueError:
            raise DumpError(f"line {no}: bad {what} entry {it!r}")
    return out


def mv_entry(it):
    if it == "-":
        return None
    r, x, y = it.split(":")
    return (int(r), int(x), int(y))


def wt_entry(it):
    if it == "-":
        return None
    a, b = it.split(":")
    return (int(a), int(b))


def parse_dump(text, tag, keys, entry):
    """-> {(pic, addr): {key: [16 entries]}}"""
    out = {}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] != tag or len(parts) < 3 + len(keys):
            raise DumpError(f"line {no}: expected '{tag} <pic> <addr> " + " ".join(k + "=..." for k in keys) + f"', got {line[:60]!r}")
        try:
            key = (int(parts[1]), int(parts[2]))
        except ValueError:
            raise DumpError(f"line {no}: picture and address must be decimal integers")
        kv = {}
        for tok in parts[3:]:
            if "=" not in tok:
                raise DumpError(f"line {no}: token {tok!r} is not key=value")
            k, v = tok.split("=", 1)
            kv[k] = v
        for k in keys:
            if k not in kv:
                raise DumpError(f"line {no}: missing {k}=")
        if key in out:
            raise DumpError(f"line {no}: {tag} {key[0]} {key[1]} appears twice")
        out[key] = {k: parse_grid(kv[k], 16, no, k, entry) for k in keys}
    return out


def describe(info_map, key):
    pid, addr = key
    name, mode = info_map.get(key, ("?", ""))
    return f"picture {pid} macroblock {addr} ({name}{', ' + mode if mode else ''})"


def info_of(pics):
    m = {}
    for pid, rec, info in mb_records(pics):
        name = rec.f["name"]
        mode = ""
        if info.slice_type == 1 and (name in ("B_Skip", "B_Direct_16x16") or any(p.get("mode") == "Direct" for p in rec.parts)):
            mode = "spatial direct" if info.hdr.get("direct_spatial_mv_pred_flag", 0) else "temporal direct"
        m[(pid, rec.addr)] = (name, mode)
    return m


def compare_grids(rep, label, exp, got, info_map, fmt, keys):
    for key in sorted(set(exp) | set(got)):
        if key not in got:
            rep.check(False, f"{label}: no line for {describe(info_map, key)}")
            continue
        if key not in exp:
            rep.check(False, f"{label}: line for {describe(info_map, key)}, which has none in the truth")
            continue
        for k in keys:
            for b, (e, a) in enumerate(zip(exp[key][k], got[key][k])):
                rep.check(e == a, f"{label}: {describe(info_map, key)} {k} block {b} (x={4 * (b % 4)} y={4 * (b // 4)}): expected "
                                  f"{fmt(e)}, got {fmt(a)}")
                if e != a:
                    break


def fmt_mv(e):
    return "-" if e is None else f"{e[0]}:{e[1]}:{e[2]}"


def fmt_wt(e):
    return "-" if e is None else f"{e[0]}:{e[1]}"


def pics_to_expected(pics):
    exp_mv, exp_wt = {}, {}
    for pid, rec, _ in mb_records(pics):
        if rec.grid is not None:
            exp_mv[(pid, rec.addr)] = dict(
                l0=[None if g[0] < 0 else (g[0], g[1], g[2]) for g in rec.grid],
                l1=[None if g[3] < 0 else (g[3], g[4], g[5]) for g in rec.grid])
        wg = getattr(rec, "wgrid", None)
        if wg and any(wg):
            exp_wt[(pid, rec.addr)] = dict(w=[tuple(w) if w else None for w in wg])
    return exp_mv, exp_wt


# ================================================================ independent checks of the reference (ffmpeg's exported vectors)
def raw_264(path, td):
    if path.lower().endswith((".264", ".h264")):
        return path
    raw = os.path.join(td, "mv_raw.264")
    if not os.path.exists(raw):
        ffref.raw_h264(path, raw)
    return raw


def ffmpeg_mvs(path, td):
    """{output frame: {(dst_x, dst_y, src, w, h): (mvx, mvy)}} from ffmpeg's exported motion vectors (ffmv_helper.py)."""
    raw = raw_264(path, td)
    outp = os.path.join(td, "mv_export.txt")
    p = subprocess.run([sys.executable, os.path.join(HERE, "ffmv_helper.py"), raw, outp], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=300)
    if p.returncode != 0:
        return None
    frames = collections.defaultdict(dict)
    for line in open(outp):
        f, src, w, h, dx, dy, mx, my, sc = map(int, line.split())
        frames[f][(dx, dy, src, w, h)] = (mx, my)
    return frames


def check_mvs_vs_ffmpeg(pics, path, td):
    """-> (problems, message).  The reference's vectors at 8x8 granularity equal ffmpeg's exported ones, both directions."""
    ff = ffmpeg_mvs(path, td)
    if ff is None:
        return [], "ffmpeg motion-vector cross-check skipped: libavcodec could not be loaded by ffmv_helper.py"
    probs, n = [], 0
    for rank, d in enumerate(p for p in pics if p is not None):
        wm = d["w_mbs"]
        grids = {}
        for r in d["results"]:
            for rec in r.mbs:
                if rec.grid is not None:
                    grids[rec.addr] = rec.grid
        exported = ff.get(rank, {})
        for a, g in grids.items():
            mx0, my0 = (a % wm) * 16, (a // wm) * 16
            for lst in (0, 1):
                if not any(e[3 * lst] >= 0 for e in g):
                    continue
                for q in range(4):
                    x, y = (q & 1) * 8, (q >> 1) * 8
                    e = g[(y >> 2) * 4 + (x >> 2)]
                    mv = (e[3 * lst + 1], e[3 * lst + 2]) if e[3 * lst] >= 0 else (0, 0)
                    cx, cy = mx0 + x + 4, my0 + y + 4
                    found = None
                    for (dx, dy, src, w, h), v in exported.items():
                        if src == (-1 if lst == 0 else 1) and dx - w // 2 <= cx < dx + w // 2 and dy - h // 2 <= cy < dy + h // 2:
                            found = v
                    n += 1
                    if found != mv and len(probs) < 5:
                        probs.append(f"output frame {rank}, macroblock {a}, list {lst}, quarter {q}: reference {mv}, ffmpeg {found}")
        for k, v in exported.items():          # backward: every exported vector is the reference's vector of the block it covers
            dx, dy, src, w, h = k
            ox, oy = dx - w // 2, dy - h // 2          # top-left corner of the exported partition: its first 4x4 block holds the vector
            a = (oy // 16) * wm + ox // 16
            if a not in grids:
                if len(probs) < 5:
                    probs.append(f"output frame {rank}: ffmpeg exports a vector at ({dx},{dy}) where the reference has no inter macroblock")
                continue
            lst = 0 if src < 0 else 1
            e = grids[a][((oy % 16) >> 2) * 4 + ((ox % 16) >> 2)]
            mv = (e[3 * lst + 1], e[3 * lst + 2]) if e[3 * lst] >= 0 else (0, 0)
            n += 1
            if v != mv and len(probs) < 5:
                probs.append(f"output frame {rank}, vector exported at ({dx},{dy}) list {lst}: ffmpeg {v}, reference {mv}")
    return probs, f"ffmpeg's exported motion vectors: {n} vectors (8x8 quarters, both lists) equal the reference's"


# ================================================================ diagnosis of a wrong picture
def read_frame(path, idx, fsize):
    with open(path, "rb") as f:
        f.seek(idx * fsize)
        return f.read(fsize)


def mutant_worker(args):
    path, frame, mname, cache = args
    ddec.MUTANT = mname
    try:
        pics = ddec.decode(path, upto=frame + 1, cache=cache)
        if len(pics) > frame and pics[frame] is not None:
            return mname, ddec.frame_bytes(pics[frame])
    except Exception:  # noqa: BLE001
        pass
    finally:
        ddec.MUTANT = None
    return mname, None


def wrong_rule_names():
    return [k for k in ddec.ALL_MUTANTS if k.startswith(("d_", "w_")) or k in ("skip_mv_zero", "skip_is_coded", "ref_idx_compare")]


def match_wrong_rules(path, frame, act, cache, budget, names=None):
    names = list(names) if names else wrong_rule_names()
    t0 = time.time()
    hits = []
    if not mp.current_process().daemon and len(names) > 4:
        try:
            with mp.get_context("fork").Pool(min(os.cpu_count() or 2, 12)) as pool:
                res = pool.map_async(mutant_worker, [(path, frame, m, cache) for m in names])
                res = res.get(timeout=max(budget, 30))
            return [m for m, fb in res if fb == act], len(names)
        except Exception:  # noqa: BLE001
            return hits, 0
    tried = 0
    for m in names:
        if time.time() - t0 > budget:
            break
        tried += 1
        if mutant_worker((path, frame, m, cache))[1] == act:
            hits.append(m)
    return hits, tried


def block_text(p, wg=None):
    refs = ",".join(f"L{l}[{r}]" for l, r in p["refs"])
    mvs = " ".join(f"mv({x},{y})" for x, y in p["mvs"])
    pics = ",".join(f"#{x}" for x in p["pids"])
    return f"x={p['x']} y={p['y']} {p['w']}x{p['h']} {p['mode']}: {refs} = picture {pics}, {mvs}"


def diagnose(path, ref_yuv, act_yuv, res, w, h, origin, td, budget=60.0, candidates=None):
    frame = res["first_diff"]["frame"]
    L = []
    cache = {}
    store = {}
    try:
        pics = ddec.decode(path, upto=frame + 1, record=True, workdir=td, cache=cache, store=store)
    except Exception as e:  # noqa: BLE001
        return [f"diagnosis not available: the reference decoder failed on this stream ({type(e).__name__}: {e})"]
    if len(pics) <= frame or pics[frame] is None:
        return ["diagnosis not available: the reference decoder produced fewer pictures than ffmpeg"]
    pic = pics[frame]
    geo, fsize = yuvcmp.frame_geometry(w, h)
    exp = read_frame(ref_yuv, frame, fsize)
    act = read_frame(act_yuv, frame, fsize)
    if ddec.frame_bytes(pic) != exp:
        L.append(f"WARNING: the harness's own reference decoder does not reproduce the expected frame {frame} exactly; the advice may "
                 "be unreliable.")
    x0, y0 = origin
    wmb = pic["w_mbs"]
    bad_mb = collections.defaultdict(lambda: [0, 0, 0])
    maxdiff = collections.defaultdict(int)
    for pl, (name, pw, ph, off) in enumerate(geo):
        for y in range(ph):
            ra, rb = exp[off + y * pw: off + (y + 1) * pw], act[off + y * pw: off + (y + 1) * pw]
            if ra == rb:
                continue
            for x in range(pw):
                if ra[x] != rb[x]:
                    a = (((y0 // 2 + y) // 8) * wmb + (x0 // 2 + x) // 8) if pl else (((y0 + y) // 16) * wmb + (x0 + x) // 16)
                    bad_mb[a][pl] += 1
                    maxdiff[a] = max(maxdiff[a], abs(ra[x] - rb[x]))
    if bad_mb:   # neighbouring frames are often similar, so check for an order problem whatever the size of the difference
        try:
            nfr = os.path.getsize(ref_yuv) // fsize
            for j in range(nfr):
                if j != frame and read_frame(ref_yuv, j, fsize) == act:
                    L.append(f"your frame {frame} is exactly ffmpeg's frame {j}: the frames are written in the wrong ORDER")
                    break
        except OSError:
            pass
    first = min(bad_mb)
    cnt = bad_mb[first]
    d0 = pic["results"][0].info
    L.append(f"output frame {frame} = coded picture #{pic['pid']} (decoding order), slice types {sorted(set(pic['slice_types']))}, "
             f"POC {pic['poc']}")
    L.append(f"first wrong macroblock in raster order: address {first} (x={first % wmb}, y={first // wmb}); wrong samples "
             f"Y {cnt[0]}, Cb {cnt[1]}, Cr {cnt[2]}; largest difference {maxdiff[first]}")
    mb = pic["mbs"].get(first)
    rec = None
    info = None
    for r in pic["results"]:
        for rc in r.mbs:
            if rc.addr == first:
                rec, info = rc, r.info
    direct_mode = None
    if mb is not None:
        L.append(f"  type {mb['name']}, slice {mb['slice']}")
        for lg in pic["lists"]:
            if lg["slice"] == mb["slice"]:
                s0, s1 = lg["slots"]
                L.append("  that slice's list0: " + ",".join("-" if e is None else f"#{e[0]}" for e in s0)
                         + ("   list1: " + ",".join("-" if e is None else f"#{e[0]}" for e in s1) if lg["type"] == 1 else "")
                         + "   (#n = coded picture n in decoding order)")
        if info is not None and info.slice_type == 1:
            is_direct = mb["name"] in ("B_Skip", "B_Direct_16x16") or any(p.get("mode") == "Direct" for p in rec.parts)
            if is_direct:
                direct_mode = "spatial" if info.hdr.get("direct_spatial_mv_pred_flag", 0) else "temporal"
                L.append(f"  direct prediction, {direct_mode} mode, direct_8x8_inference_flag = "
                         f"{info.sps.get('direct_8x8_inference_flag', 0)}")
        for p in mb["parts"]:
            L.append("  prediction block " + block_text(p))
        if rec is not None and getattr(rec, "wgrid", None) and any(rec.wgrid):
            L.append("  implicit weights (w0:w1) per 4x4 block, raster order: "
                     + ",".join(f"{w[0]}:{w[1]}" if w else "-" for w in rec.wgrid))
        if direct_mode and rec is not None:
            lg = [g for g in pic["lists"] if g["slice"] == mb["slice"]][0]
            s1 = lg["slots"][1]
            if s1 and s1[0] is not None and s1[0][0] in store:
                col = store[s1[0][0]]["mot"]
                wm = pic["w_mbs"]
                for bx, by in ((0, 0), (3, 0), (0, 3), (3, 3)):
                    i = ((first // wm) * 4 + by) * (wm * 4) + (first % wm) * 4 + bx
                    e = [(l, col.ref[l][i], col.mvx[l][i], col.mvy[l][i], col.rpid[l][i]) for l in (0, 1) if col.ref[l][i] >= 0]
                    L.append(f"  co-located block (corner {bx},{by}) in picture #{s1[0][0]}: "
                             + ("intra" if not e else "; ".join(f"list {l} refIdx {r} mv({x},{y}) -> picture #{pid}"
                                                                for l, r, x, y, pid in e)))
    names = candidates if candidates else None
    matches, tried = match_wrong_rules(path, frame, act, cache, budget, names)
    if matches:
        L.append("your frame is EXACTLY what the reference decoder produces with the wrong rule(s):")
        for m_ in matches:
            L.append(f"  - {m_}: {ddec.ALL_MUTANTS[m_]}")
    else:
        L.append(f"your frame equals none of the {tried} known wrong rules")
    H = []
    if direct_mode == "spatial":
        H.append("spatial direct: refIdxLX = MinPositive over A, B, C (D for C), computed ONCE per macroblock from the 16x16 "
                 "neighbours; both negative -> refIdx 0/0 and zero vectors; else the 16x16 median per list; a vector is 0 when its "
                 "refIdx is 0 and colZeroFlag (RefPicList1[0] short-term, refIdxCol == 0, mvCol within +-1) holds.")
    if direct_mode == "temporal":
        H.append("temporal direct: refIdxL0 = lowest index of the picture the co-located block referred to (intra: 0), refIdxL1 = 0; "
                 "mvL0 = (DistScaleFactor * mvCol + 128) >> 8, mvL1 = mvL0 - mvCol; long-term pic0 -> mvL0 = mvCol, mvL1 = 0.")
    if maxdiff[first] <= 2 and rec is not None and getattr(rec, "wgrid", None) and any(rec.wgrid):
        H.append("small differences in a macroblock with implicit weights: w0 = 64 - (DistScaleFactor >> 2), w1 = DistScaleFactor >> 2, "
                 "logWD = 5, rounding term 2^logWD, clip; 32/32 when the POC distance is 0, a picture is long-term or w1 is outside "
                 "-64..128; one-list blocks are NOT weighted.")
    if not H:
        H.append("compare your --mv-dump with the expected one first (--print-expected mv), then the weights; the first wrong "
                 "macroblock is not direct: look at what it reads from its direct neighbours (their stored vectors / refIdx) or at "
                 "the deblocking filter (bS uses the derived vectors of direct blocks).")
    L.append("hints:")
    L += ["  - " + x for x in H]
    return L


# ================================================================ main check
def decode_truth(path, td, pixels=False):
    try:
        return ddec.decode(path, record=False, workdir=td, pixels=pixels)
    except ddec.Unsupported as e:
        raise SystemExit(f"unsupported input: {e}")
    except ddec.direct_ref.DirectInvalid as e:
        raise SystemExit(f"unsupported input: {e}")


def run_check(path, yuv=None, mv_text=None, wt_text=None, quiet=False, workdir=None, expected=None, diag=True, trust=True,
              diag_budget=60.0, diag_candidates=None, truth_pics=None):
    td = workdir or tempfile.mkdtemp(prefix="direct_")
    out = []

    def say(s):
        out.append(s)
        if not quiet:
            print(s)
    worst = 0
    need_dump = mv_text is not None or wt_text is not None
    try:
        pics = truth_pics if truth_pics is not None else decode_truth(path, td, pixels=bool(trust and need_dump))
    except SystemExit as e:
        return 2, f"input problem: {e}"
    ref_yuv = expected
    if yuv is not None or (trust and need_dump):
        if ref_yuv is None:
            ref_yuv = os.path.join(td, "expected.yuv")
            src = expected_yuv(path, ref_yuv, td)
            if not src.startswith("ffmpeg"):
                say(f"(note: expected pixels come from {src})")
    d0 = next(p for p in pics if p is not None)
    w, h, x0, y0 = d0["width"], d0["height"], d0["x0"], d0["y0"]
    if trust and need_dump:
        probs = []
        mine = os.path.join(td, "reference.yuv")
        ddec.write_yuv(pics, mine)
        if open(mine, "rb").read() != open(ref_yuv, "rb").read():
            probs.append("the reference decoder's pixels differ from the expected frames")
        if ffmpeg_is_truth(path):
            p2, msg = check_mvs_vs_ffmpeg(pics, path, td)
            probs += p2
        if probs:
            return 2, "\n".join(out + ["HARNESS PROBLEM (the reference disagrees with an independent source; this is not your bug):"]
                                + ["   " + p for p in probs[:8]])
    if yuv is not None:
        if not os.path.isfile(yuv):
            return 2, f"your output {yuv!r} does not exist"
        res = yuvcmp.compare_yuv(ref_yuv, yuv, w, h, quiet=True, origin=(x0, y0))
        say(res["text"])
        if res["ok"]:
            say(f"PIXELS: all {res['frames_expected']} frames byte-identical to the expected frames")
        elif any("does not exist" in p for p in res["size_problems"]):
            return 2, "\n".join(out)
        else:
            worst = 1
            if res["first_diff"] and diag:
                say("---- diagnosis (reference decoder in spoiler_reference/) ----")
                for line in diagnose(path, ref_yuv, yuv, res, w, h, (x0, y0), td, diag_budget, diag_candidates):
                    say(line)
            say("PIXELS: output differs from the expected frames")
    info_map = None
    exp_mv = exp_wt = None
    if need_dump:
        exp_mv, exp_wt = pics_to_expected(pics)
        info_map = info_of(pics)
    from common.report import Report
    if mv_text is not None:
        try:
            got = parse_dump(mv_text, "MV", ["l0", "l1"], mv_entry)
        except DumpError as e:
            return 2, "\n".join(out + [f"malformed mv dump: {e}"])
        rep = Report("mv")
        compare_grids(rep, "MV", exp_mv, got, info_map, fmt_mv, ["l0", "l1"])
        for m_ in rep.failures[:10]:
            say("  FAIL: " + m_)
        if len(rep.failures) > 10:
            say(f"  ... and {len(rep.failures) - 10} more")
        say(f"MV DUMP: {len(exp_mv)} macroblock line(s) expected: " + ("identical to the truth" if not rep.failures else "differs from the truth"))
        worst = max(worst, 1 if rep.failures else 0)
    if wt_text is not None:
        try:
            got = parse_dump(wt_text, "WT", ["w"], wt_entry)
        except DumpError as e:
            return 2, "\n".join(out + [f"malformed weights dump: {e}"])
        rep = Report("weights")
        compare_grids(rep, "WT", exp_wt, got, info_map, fmt_wt, ["w"])
        for m_ in rep.failures[:10]:
            say("  FAIL: " + m_)
        if len(rep.failures) > 10:
            say(f"  ... and {len(rep.failures) - 10} more")
        say(f"WEIGHTS DUMP: {len(exp_wt)} macroblock line(s) expected: " + ("identical to the truth" if not rep.failures else
                                                                            "differs from the truth"))
        worst = max(worst, 1 if rep.failures else 0)
    say(("PASS" if worst == 0 else "FAIL") + ": Chapter 7.9")
    return worst, "\n".join(out)


# ================================================================ self-test
REQUIRED_COVERAGE = [
    "spatial: colZeroFlag 1", "spatial: colZeroFlag 0, moving co-located block (mvCol outside -1..1)",
    "spatial: colZeroFlag 0, refIdxCol != 0", "spatial: colZeroFlag 0, co-located intra",
    "spatial: colZeroFlag 0 only because RefPicList1[0] is long-term", "spatial: both refIdx negative (directZeroPrediction)",
    "spatial: one list only (the other refIdx < 0)", "spatial: both lists used", "spatial: a derived refIdx > 0",
    "co-located block predicted from list 1 only", "co-located block is intra",
    "temporal: scaled", "temporal: intra co-located block (refIdxL0 = 0, mv 0)", "temporal: refIdxL0 > 0 after MapColToList0",
    "temporal: long-term fallback mvL0 = mvCol", "temporal: the co-located reference is in RefPicList0 more than once (lowest index wins)",
    "temporal: co-located picture whose slices used different reference lists",
    "DistScaleFactor with td < 0", "DistScaleFactor clipped to -1024..1023",
    "implicit weights: computed, 0 <= w1 <= 64", "implicit weights: computed, w1 < 0 or > 64 (extrapolation)",
    "implicit weights: fallback, DiffPicOrderCnt(pic1, pic0) == 0", "implicit weights: fallback, long-term reference",
    "implicit weights: fallback, DistScaleFactor >> 2 outside -64..128", "explicit Bi",
]
NAMING_SAMPLE = ["d_minpos_min", "d_col_mv_zero_only", "d_t_map_none", "d_t_dsf_round", "w_swapped", "w_no_range_fallback",
                 "d_infer_topleft", "d_direct_not_stored"]
# the order in which streams are tried for a wrong rule (cheap, informative ones first)
CATCH_ORDER = ["sp_inf1", "sp_inf0", "tmp_inf1", "tmp_inf0", "mixed", "lt", "lt_tmp", "poc_far", "poc_far_tmp", "static", "moving",
               "intra", "map", "dup_w", "tail", "colslices", "wp", "idc1"]

_ST = {}      # name -> (stream path, expected yuv path)   (filled in the parent before the pools start)


def safe(fn):
    """Pool workers must not die with SystemExit (the pool would wait for them forever): return the failure instead."""
    @functools.wraps(fn)
    def run(arg):
        try:
            return fn(arg)
        except BaseException as e:  # noqa: BLE001
            return ("ERROR", f"{type(e).__name__}: {e}"[:300], None)
    return run


def private_dir(td, name):
    d = os.path.join(td, "w_" + name)
    os.makedirs(d, exist_ok=True)
    return d


@safe
def build_stream(args):
    name, td = args
    p = os.path.join(td, name + ".264")
    dgen.write_stream(name, p, 7)
    y = os.path.join(td, name + ".exp.yuv")
    src = expected_yuv(p, y, private_dir(td, name))
    return name, p, y, src


@safe
def check_stream(args):
    """Reference decoder vs expected frames (and ffmpeg's exported vectors); returns coverage counters."""
    name, path, yuv, td = args
    wd = private_dir(td, "chk_" + name)
    c = collections.Counter()
    direct_ref.STATS = c
    ddec.STATS = c
    pics = ddec.decode(path, workdir=wd)
    direct_ref.STATS = ddec.STATS = None
    out = os.path.join(td, name + ".ref.yuv")
    ddec.write_yuv(pics, out)
    same = open(out, "rb").read() == open(yuv, "rb").read()
    probs = []
    if ffmpeg_is_truth(path):
        probs, _ = check_mvs_vs_ffmpeg(pics, path, wd)
    return name, same, probs, dict(c)


@safe
def catch_worker(mname):
    """-> (mutant, stream that shows a difference to the expected frames | None)."""
    for name in CATCH_ORDER:
        path, yuv = _ST[name]
        exp = open(yuv, "rb").read()
        bad = []

        def hook(d, rank, exp=exp, bad=bad):
            fs = d["width"] * d["height"] * 3 // 2
            if ddec.frame_bytes(d) != exp[rank * fs:(rank + 1) * fs]:
                bad.append("pixels")
                raise ddec.StopDecoding()
        ddec.MUTANT = mname
        try:
            ddec.decode(path, hook=hook)
        except Exception as e:  # noqa: BLE001
            bad.append(type(e).__name__)
        finally:
            ddec.MUTANT = None
        if bad:
            return mname, name, bad[0]
    return mname, None, None


def corrupt_text(text, kind):
    lines = text.splitlines()
    if kind == "mv_value":
        i = next(k for k, l in enumerate(lines) if l.startswith("MV") and re.search(r"l1=\d+:", l))
        lines[i] = re.sub(r"(l1=\d+:)(-?\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), lines[i], count=1)
    elif kind == "mv_ref":
        i = next(k for k, l in enumerate(lines) if l.startswith("MV") and re.search(r"l0=\d+:", l))
        lines[i] = re.sub(r"(l0=)(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), lines[i], count=1)
    elif kind == "mv_drop":
        del lines[len(lines) // 2]
    elif kind == "mv_garbage":
        lines[1] = "MV 1 2 3"
    elif kind == "wt_value":
        i = next(k for k, l in enumerate(lines) if l.startswith("WT") and re.search(r"w=\d+:\d+", l))
        lines[i] = re.sub(r"w=(\d+):(\d+)", lambda m: f"w={int(m.group(1)) + 1}:{int(m.group(2)) - 1}", lines[i], count=1)
    elif kind == "wt_drop":
        del lines[len(lines) // 3]
    elif kind == "wt_garbage":
        lines[0] = "WT x"
    return "\n".join(lines) + "\n"


def self_test():
    td = tempfile.mkdtemp(prefix="direct_selftest_")
    problems = []
    t0 = time.time()
    ncpu = min(os.cpu_count() or 2, 12)

    def sec(msg):
        print(f"{msg}  [t={time.time() - t0:.0f}s]", flush=True)

    def bad(msg):
        problems.append(msg)
        print("  PROBLEM:", msg, flush=True)
    ctx = mp.get_context("fork")
    try:
        # ---------------------------------------------------------------- 1. streams, truth, the reference against the truth
        sec(f"1. generating {len(dgen.SCENARIOS)} synthetic streams (every temporal-direct slice is checked for legality) and the "
            "expected frames")
        with ctx.Pool(ncpu) as pool:
            built = pool.map(build_stream, [(n, td) for n in dgen.SCENARIOS])
        for b in built:
            if b[0] == "ERROR":
                raise SystemExit(f"self-test: building a synthetic stream failed: {b[1]}")
        for name, p, y, src in built:
            _ST[name] = (p, y)
            if not src.startswith("ffmpeg"):
                print(f"   {name}: expected frames from the reference decoder (ffmpeg_ok = false)")
        sec("   the reference decoder vs the expected frames on every synthetic stream and on T9_spatial / T9_temporal "
            "(pixels; ffmpeg's exported vectors; coverage of the rules)")
        jobs = [(n, _ST[n][0], _ST[n][1], td) for n in dgen.SCENARIOS]
        t9 = {}
        for n in ("T9_spatial", "T9_temporal"):
            p = testfiles.get(n)
            y = os.path.join(td, n + ".exp.yuv")
            ffmpeg_yuv(p, y)
            t9[n] = (p, y)
            jobs.append((n, p, y, td))
        with ctx.Pool(ncpu) as pool:
            checked = pool.map(check_stream, jobs, chunksize=1)
        for c_ in checked:
            if c_[0] == "ERROR":
                bad(f"checking a stream failed: {c_[1]}")
        checked = [c_ for c_ in checked if c_[0] != "ERROR"]
        cov = collections.Counter()
        for name, same, probs, c in checked:
            if not same:
                bad(f"the reference decoder does not reproduce the expected frames of {name}")
            for pr in probs:
                bad(f"{name}: reference vs ffmpeg exported vectors: {pr}")
            if name in dgen.SCENARIOS:
                cov.update(c)
        print(f"   {len(checked)} streams: reference == expected frames" + (" (all)" if not problems else ""), flush=True)
        miss = [k for k in REQUIRED_COVERAGE if cov[k] == 0]
        if miss:
            bad("the synthetic streams never exercise: " + "; ".join(miss))
        else:
            print(f"   coverage: all {len(REQUIRED_COVERAGE)} rule cases of the synthetic streams occur "
                  f"(e.g. colZeroFlag 1: {cov['spatial: colZeroFlag 1']}, temporal scaled: {cov['temporal: scaled']}, "
                  f"implicit weights computed: {cov['implicit weights: computed, 0 <= w1 <= 64']})")
        # ---------------------------------------------------------------- 2. the harness on a known-good and corrupted outputs
        sec("2. the harness itself: a correct output passes, damaged outputs and dumps fail")
        name = "tmp_inf0"
        path, expyuv = _ST[name]
        wd = os.path.join(td, "run")
        os.makedirs(wd)
        pics = ddec.decode(path, workdir=wd, record=True)
        good = os.path.join(td, "good.yuv")
        ddec.write_yuv(pics, good)
        mvt, wtt = render_mv(pics), render_weights(pics)
        code, msg = run_check(path, good, mv_text=mvt, wt_text=wtt, quiet=True, workdir=wd, expected=expyuv, truth_pics=pics, trust=True)
        if code != 0:
            bad("a correct output / mv dump / weights dump was not accepted:\n" + msg[-600:])
        else:
            print("   correct output, mv dump and weights dump: PASS", flush=True)
        data = bytearray(open(good, "rb").read())
        fs = pics[0]["width"] * pics[0]["height"] * 3 // 2
        data[5 * fs + 700] ^= 0x10
        dmg = os.path.join(td, "dmg.yuv")
        open(dmg, "wb").write(bytes(data))
        code, msg = run_check(path, dmg, quiet=True, workdir=wd, expected=expyuv, diag_budget=20)
        if code != 1 or "frame 5" not in msg.replace("Frame", "frame"):
            bad("one damaged sample was not reported as a pixel difference in frame 5:\n" + msg[-500:])
        else:
            print("   one damaged sample: FAIL reported with the location (frame 5)", flush=True)
        short = os.path.join(td, "short.yuv")
        open(short, "wb").write(bytes(data[:3 * fs]))
        code, msg = run_check(path, short, quiet=True, workdir=wd, expected=expyuv, diag=False)
        if code not in (1, 2):
            bad("a truncated output was accepted")
        code, msg = run_check(path, os.path.join(td, "nonexistent.yuv"), quiet=True, workdir=wd, expected=expyuv, diag=False)
        if code != 2:
            bad("a missing output file did not give exit code 2")
        for kind, which in (("mv_value", "mv"), ("mv_ref", "mv"), ("mv_drop", "mv"), ("wt_value", "wt"), ("wt_drop", "wt")):
            kw = {"mv_text": corrupt_text(mvt, kind)} if which == "mv" else {"wt_text": corrupt_text(wtt, kind)}
            code, msg = run_check(path, None, quiet=True, workdir=wd, truth_pics=pics, trust=False, **kw)
            if code != 1:
                bad(f"the damaged dump '{kind}' was not reported (exit {code})")
        for kind, which in (("mv_garbage", "mv"), ("wt_garbage", "wt")):
            kw = {"mv_text": corrupt_text(mvt, kind)} if which == "mv" else {"wt_text": corrupt_text(wtt, kind)}
            code, msg = run_check(path, None, quiet=True, workdir=wd, truth_pics=pics, trust=False, **kw)
            if code != 2:
                bad(f"the malformed dump '{kind}' did not give exit code 2 (exit {code})")
        print("   damaged / malformed dumps: reported", flush=True)
        # ---------------------------------------------------------------- 3. every wrong rule changes the pictures
        names = [m for m in ddec.ALL_MUTANTS if m.startswith(("d_", "w_")) or m in ("skip_mv_zero", "skip_is_coded", "ref_idx_compare")]
        sec(f"3. {len(names)} deliberately wrong rules: each must change the decoded frames of at least one test stream")
        with ctx.Pool(ncpu) as pool:
            res = pool.map(catch_worker, names, chunksize=1)
        for r_ in res:
            if r_[0] == "ERROR":
                bad(f"wrong-rule check failed: {r_[1]}")
        res = [r_ for r_ in res if r_[0] != "ERROR"]
        uncaught = [m for m, s, _ in res if s is None]
        for m, s, why in sorted(res):
            if s is None:
                bad(f"wrong rule {m} ({ddec.ALL_MUTANTS[m]}) is NOT caught by any test stream")
        print(f"   {len(res) - len(uncaught)} of {len(res)} wrong rules caught (first stream that shows each: "
              + ", ".join(sorted({s for _, s, _ in res if s})) + ")", flush=True)
        # ---------------------------------------------------------------- 4. the diagnosis names the wrong rule
        sec("4. the diagnosis recognises a wrong rule")
        for m in NAMING_SAMPLE:
            s_name = next(s for mm, s, _ in res if mm == m)
            p2, y2 = _ST[s_name]
            out_y = os.path.join(td, "mut.yuv")
            ddec.MUTANT = m
            try:
                ddec.write_yuv(ddec.decode(p2, workdir=wd), out_y)
            finally:
                ddec.MUTANT = None
            code, msg = run_check(p2, out_y, quiet=True, workdir=wd, expected=y2, diag_budget=60)
            if code != 1 or f"- {m}:" not in msg:
                bad(f"the diagnosis did not name the wrong rule {m} on {s_name}:\n" + "\n".join(msg.splitlines()[-12:]))
            else:
                print(f"   {m}: reported as pixel difference, wrong rule named", flush=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"(self-test took {time.time() - t0:.0f} s)")
    if problems:
        print(f"SELF-TEST FAIL: {len(problems)} problem(s)")
        return 1
    print("SELF-TEST PASS")
    return 0


# ================================================================ command line
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="input .ts / .264 (CAVLC, I / P / B slices, direct prediction allowed)")
    ap.add_argument("output", nargs="?", help="your decoded frames, raw yuv420p, cropped, display order")
    ap.add_argument("--mv-dump", metavar="FILE", help="your final motion vectors, both lists (grammar in the Readme, section 5)")
    ap.add_argument("--weights-dump", metavar="FILE", help="your implicit weights per macroblock (grammar in the Readme, section 5)")
    ap.add_argument("--print-expected", nargs=3, metavar=("KIND", "INPUT", "OUT"), help="write the expected mv|weights text of INPUT")
    ap.add_argument("--make-variants", metavar="DIR", help="write extra test inputs into DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if a.print_expected:
        kind, inp, outp = a.print_expected
        if kind not in ("mv", "weights") or not os.path.isfile(inp):
            die("error: --print-expected needs KIND in mv|weights and an existing input file")
        td = tempfile.mkdtemp(prefix="direct_")
        try:
            try:
                pics = decode_truth(inp, td)
            except SystemExit as e:
                die(f"error: {e}")
            text = render_mv(pics) if kind == "mv" else render_weights(pics)
        finally:
            shutil.rmtree(td, ignore_errors=True)
        open(outp, "w").write(text)
        print(f"wrote the expected {kind} text ({text.count(chr(10))} lines) to {outp}")
        return
    if not a.input:
        ap.print_usage(sys.stderr)
        die("error: need <input> and <your_output.yuv> and/or a dump channel")
    if not os.path.isfile(a.input):
        die(f"error: input file {a.input!r} not found")
    if not (a.output or a.mv_dump or a.weights_dump):
        die("error: need <your_output.yuv> and/or --mv-dump / --weights-dump")
    texts = {}
    for k, f in (("mv_text", a.mv_dump), ("wt_text", a.weights_dump)):
        if f:
            if not os.path.isfile(f):
                die(f"error: {f!r} not found")
            texts[k] = open(f, errors="replace").read()
    td = tempfile.mkdtemp(prefix="direct_")
    try:
        code, msg = run_check(a.input, a.output, workdir=td, **texts)
        if code == 2:
            print(msg, file=sys.stderr)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
