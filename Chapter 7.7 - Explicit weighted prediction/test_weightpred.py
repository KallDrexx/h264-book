#!/usr/bin/env python3
"""Harness for Chapter 7.7 (explicit weighted prediction in P slices: pred_weight_table, 8.4.2.3).

    python3 test_weightpred.py <input.ts|input.264> <your_output.yuv> [--weights-dump your_weights.txt]
    python3 test_weightpred.py <input.ts|input.264> --weights-dump your_weights.txt     # only the weight-table channel
    python3 test_weightpred.py --expected-weights-out FILE <input>                      # write the expected weights dump of a stream
    python3 test_weightpred.py --make-variants <out_dir>                                # extra test inputs (see Readme)
    python3 test_weightpred.py --self-test                                              # verify the harness itself (a few minutes)

<your_output.yuv> = every decoded frame in output order, 8-bit planar yuv420p, CROPPED as the SPS says, no padding, no headers
(the Chapter 5.1 format).  P-only streams decode in display order.

Optional weights dump (one line per P slice whose PPS has weighted_pred_flag = 1; no line for any other slice; decoding order):

    SLICE <pic> <slice> denomY=<n> denomC=<n> w0=<idx>:<wY>:<oY>:<wCb>:<oCb>:<wCr>:<oCr>,<idx>:...

<pic> = 0-based decoding-order index of the coded picture, <slice> = 0-based index of the slice inside the picture, denomY / denomC =
luma_log2_weight_denom / chroma_log2_weight_denom, then one entry per ref_idx 0 .. num_ref_idx_l0_active-1 (in order, comma separated,
no spaces): weight and offset of luma, Cb, Cr AS USED by the decoding process: when a flag is 0 the entry shows the DEFAULT
(weight 2^denom of that plane, offset 0), not the values of some other entry.  All numbers decimal, offsets as in the bitstream
(before any bit-depth scaling).  Truth: ffmpeg's trace_headers element values.

Truth for the pixels: the bit-exact decode of ffmpeg (`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`) compared with
common/yuvcmp.py.  When your pixels differ, the harness decodes the stream with an independent reference decoder
(spoiler_reference/weightdec.py), tells you the first wrong macroblock, its partitions, ref_idx and the weights applied there, and
checks whether your frame equals the reference decoder with one deliberately wrong rule (the list in the Readme).
Exit 0 = everything compared is identical, 1 = different, 2 = unusable input/output (missing file, malformed dump, a stream this
chapter cannot decode, ...).
"""
import argparse
import collections
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref, ffref, testfiles, yuvcmp, yuvref  # noqa: E402
import slicehdr_ref  # noqa: E402
import weightdec  # noqa: E402
import weightgen  # noqa: E402

SYNTH = ["synth_" + m for m in weightgen.SCENARIOS]
SRC = "testsrc2=size=352x288:rate=25"
VARIANTS = {   # name -> (video filter, codec options)
    "wp_simple_fadein": ("fade=in:0:25", "-profile:v main -x264-params cabac=0:bframes=0:weightp=1:keyint=100"),
    "wp_smart_fadeout": ("fade=out:8:17", "-profile:v main -x264-params cabac=0:bframes=0:weightp=2:ref=4:keyint=100"),
    "wp_smart_ref3": ("fade=in:0:25", "-profile:v main -x264-params cabac=0:bframes=0:weightp=2:ref=3:keyint=100:partitions=all"),
    "wp_slices": ("fade=in:0:12,fade=out:13:12", "-profile:v main -x264-params cabac=0:bframes=0:weightp=2:ref=5:keyint=100:slices=3"),
    "wp_ref6": ("fade=in:0:25", "-profile:v main -x264-params cabac=0:bframes=0:weightp=2:ref=6:keyint=100"),
}


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
    with open(out_264, "wb") as f:
        f.write(weightgen.make_stream(name[len("synth_"):], seed))


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "T7.ts")
    shutil.copy(testfiles.get("T7"), p)
    print("wrote", p)
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for n in SYNTH:
        p = os.path.join(out_dir, n + ".264")
        make_synth(n, p)
        print("wrote", p)


def reference_yuv(path, out):
    ffref.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "yuv420p", out], timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")


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


# ================================================================ the weights dump
class DumpError(Exception):
    pass


def expected_slices(path, td):
    """-> {(pic, slice): (denom_y, denom_c, [(wy, oy, wcb, ocb, wcr, ocr)])} from ffmpeg's trace of the stream."""
    infos = cavlc_ref.load_stream(path, td)
    tables = slicehdr_ref.load(os.path.join(td, "ref_trace.txt"))
    if len(tables) != len(infos):
        raise SystemExit(f"internal error: {len(infos)} slices but {len(tables)} slice headers in the trace")
    out, first = {}, {}
    for s, t in zip(infos, tables):
        first.setdefault(s.picture, s.index)
        if t["wt"] is not None:
            wt = t["wt"]
            out[(s.picture, s.index - first[s.picture])] = (
                wt["denom_y"], wt["denom_c"],
                [(e["wy"], e["oy"], e["wcb"], e["ocb"], e["wcr"], e["ocr"]) for e in wt["entries"]])
    return out


def render_weights(slices):
    lines = []
    for (pic, sl), (dy, dc, ents) in sorted(slices.items()):
        lines.append(f"SLICE {pic} {sl} denomY={dy} denomC={dc} w0=" +
                     ",".join(f"{i}:" + ":".join(map(str, e)) for i, e in enumerate(ents)))
    return "\n".join(lines) + "\n"


def parse_weights(text):
    out = {}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        if len(tok) != 6 or tok[0] != "SLICE" or not tok[3].startswith("denomY=") or not tok[4].startswith("denomC=") \
                or not tok[5].startswith("w0="):
            raise DumpError(f"line {no}: expected 'SLICE <pic> <slice> denomY=<n> denomC=<n> w0=<entries>', got {line[:70]!r}")
        try:
            pic, sl = int(tok[1]), int(tok[2])
            dy, dc = int(tok[3][7:]), int(tok[4][7:])
            ents = []
            for k, item in enumerate(tok[5][3:].split(",")):
                f = [int(x) for x in item.split(":")]
                if len(f) != 7 or f[0] != k:
                    raise ValueError(f"entry {k} must be '{k}:wY:oY:wCb:oCb:wCr:oCr'")
                ents.append(tuple(f[1:]))
        except ValueError as e:
            raise DumpError(f"line {no}: {e}")
        if (pic, sl) in out:
            raise DumpError(f"line {no}: slice {pic} {sl} appears twice")
        out[(pic, sl)] = (dy, dc, ents)
    return out


FIELDS = ("wY", "oY", "wCb", "oCb", "wCr", "oCr")


def entry_hint(e, g, dy, dc, idx, ents_got, ents_exp):
    f = FIELDS
    bad = [k for k in range(6) if e[k] != g[k]]
    hints = []
    if any(g[k] == 1 and e[k] == (1 << (dy if k == 0 else dc)) for k in (0, 2, 4)) and e[0] != 1:
        hints.append("you wrote weight 1 where the default 2^denom applies (flag 0 means weight = 2^denom of THAT plane, offset 0)")
    if e[2:] != g[2:] and g[2:4] == g[0:2] and e[2:4] != e[0:2]:
        hints.append("chroma shows the luma weight/offset: Cb/Cr have their own flag, weight, offset and denominator")
    if g[2:4] == e[4:6] and g[4:6] == e[2:4] and e[2:4] != e[4:6]:
        hints.append("Cb and Cr are swapped (order in the syntax: for each ref, Cb weight+offset, then Cr weight+offset)")
    for j in range(len(ents_exp)):
        if j != idx and g == ents_exp[j]:
            hints.append(f"this entry equals the correct entry {j}: the weights of another ref_idx (or flags of another index)")
            break
    if e[1] != g[1] and abs(e[1]) == abs(g[1]):
        hints.append("offset sign: luma_offset_l0 is se(v)")
    if dy != dc and any(k >= 2 for k in bad):
        hints.append("chroma_log2_weight_denom is read after luma_log2_weight_denom and is independent of it")
    return hints


def compare_weights(exp, got):
    """-> (ok, lines)"""
    L = []
    nbad = 0
    for key in sorted(exp):
        if key not in got:
            nbad += 1
            if nbad <= 10:
                L.append(f"  slice {key[0]} {key[1]}: missing line (expected a weights line for this P slice)")
    for key in sorted(got):
        if key not in exp:
            nbad += 1
            if nbad <= 10:
                L.append(f"  slice {key[0]} {key[1]}: unexpected line (not a P slice with weighted_pred_flag = 1, or no such slice)")
    for key in sorted(exp):
        if key not in got:
            continue
        ey, ec, ee = exp[key]
        gy, gc, ge = got[key]
        msgs = []
        if ey != gy:
            msgs.append(f"denomY: expected {ey}, got {gy}")
        if ec != gc:
            msgs.append(f"denomC: expected {ec}, got {gc}")
        if len(ee) != len(ge):
            msgs.append(f"{len(ee)} entries expected (num_ref_idx_l0_active), got {len(ge)}")
        hints = []
        for i in range(min(len(ee), len(ge))):
            if ee[i] != ge[i]:
                diff = ", ".join(f"{FIELDS[k]} expected {ee[i][k]} got {ge[i][k]}" for k in range(6) if ee[i][k] != ge[i][k])
                msgs.append(f"entry {i}: {diff}")
                hints += entry_hint(ee[i], ge[i], ey, ec, i, ge, ee)
        if msgs:
            nbad += 1
            if nbad <= 10:
                L.append(f"  slice {key[0]} {key[1]}: " + "; ".join(msgs[:4]) + (" ..." if len(msgs) > 4 else ""))
                for h in dict.fromkeys(hints):
                    L.append(f"      hint: {h}")
    if nbad > 10:
        L.append(f"  ... and {nbad - 10} more slice(s) with problems")
    return nbad == 0, L


# ================================================================ diagnosis of a pixel mismatch
def read_frame(path, idx, fsize):
    with open(path, "rb") as f:
        f.seek(idx * fsize)
        return f.read(fsize)


def fmt_w(u):
    return "-" if u is None else f"w={u[0]} o={u[1]} logWD={u[2]}"


def diagnose(path, ref_yuv, act_yuv, res, w, h, origin, workdir, budget=100.0):
    frame = res["first_diff"]["frame"]
    L = []
    t0 = time.time()
    try:
        pics = weightdec.decode(path, upto=frame + 1, record=True, workdir=workdir)
    except weightdec.Unsupported as e:
        return [f"diagnosis not available: {e}"]
    except Exception as e:  # noqa: BLE001
        return [f"diagnosis not available: the reference decoder failed on this stream ({type(e).__name__}: {e})"]
    t_one = time.time() - t0
    if len(pics) <= frame:
        return ["diagnosis not available: the reference decoder produced fewer pictures than ffmpeg"]
    pic = pics[frame]
    geo, fsize = yuvcmp.frame_geometry(w, h)
    exp = read_frame(ref_yuv, frame, fsize)
    act = read_frame(act_yuv, frame, fsize)
    mine = weightdec.frame_bytes(pic)
    if mine != exp:
        L.append(f"WARNING: the harness's own reference decoder does not reproduce ffmpeg's frame {frame} exactly; the advice may be unreliable.")
    x0, y0 = origin
    wmb = pic["w_mbs"]
    bad_mb = collections.defaultdict(lambda: [0, 0, 0])        # addr -> wrong samples per plane
    diffs = []                                                  # (plane, expected, actual)
    for pl, (name, pw, ph, off) in enumerate(geo):
        for y in range(ph):
            ra, rb = exp[off + y * pw: off + (y + 1) * pw], act[off + y * pw: off + (y + 1) * pw]
            if ra == rb:
                continue
            for x in range(pw):
                if ra[x] != rb[x]:
                    if pl:
                        a = ((y0 // 2 + y) // 8) * wmb + (x0 // 2 + x) // 8
                    else:
                        a = ((y0 + y) // 16) * wmb + (x0 + x) // 16
                    bad_mb[a][pl] += 1
                    diffs.append((pl, ra[x], rb[x], a))
    first = min(bad_mb)
    cnt = bad_mb[first]
    L.append(f"first wrong macroblock in raster order of frame {frame}: address {first} (x={first % wmb}, y={first // wmb}); wrong samples "
             f"Y {cnt[0]}, Cb {cnt[1]}, Cr {cnt[2]}")
    mb = pic["mbs"].get(first)
    tab = weightdec.slicehdr_ref.load(os.path.join(workdir, "ref_trace.txt"))
    if mb is not None:
        L.append(f"  type {mb['name']}, slice {mb['slice']}")
        sl = tab[mb["slice"]]
        wt = sl["wt"]
        if wt:
            L.append(f"  that slice's table: denomY {wt['denom_y']}, denomC {wt['denom_c']}, active {sl['active']}, list modification "
                     f"commands {len(sl['mods'])}")
            for i, e in enumerate(wt["entries"]):
                lf, cf = wt["flags"][i]
                L.append(f"    ref_idx {i}: luma flag {lf} w={e['wy']} o={e['oy']} | chroma flag {cf} Cb w={e['wcb']} o={e['ocb']} "
                         f"Cr w={e['wcr']} o={e['ocr']}")
        else:
            L.append("  that slice has no pred_weight_table (default prediction)")
        for p in mb["parts"]:
            L.append(f"  partition x={p['x']} y={p['y']} {p['w']}x{p['h']}: ref_idx {p['ref']} = picture #{p['pid']}, mv {p['mv']}, "
                     f"Y [{fmt_w(p['weights'][0] if p['weights'] else None)}] Cb [{fmt_w(p['weights'][1] if p['weights'] else None)}] "
                     f"Cr [{fmt_w(p['weights'][2] if p['weights'] else None)}]")
        if not mb["parts"] and mb["name"] == "P_Skip":
            L.append("  (P_Skip: ref_idx 0 with the predicted vector; the weights of ref_idx 0 apply as well)")
    mine_diff = [(pl, e, a) for pl, e, a, ad in diffs if ad == first]
    if mine_diff:
        mx = max(abs(e - a) for _, e, a in mine_diff)
        L.append(f"  in this macroblock the largest |expected - yours| is {mx}; samples with expected 0 or 255: "
                 f"{sum(1 for _, e, a in mine_diff if e in (0, 255))} of {len(mine_diff)}")
    # ---- which known wrong rule reproduces YOUR frame?
    names = list(weightdec.WEIGHT_MUTANTS)
    matches, tried = [], 0
    t_start = time.time()
    for m in names:
        if time.time() - t_start + t_one > budget:
            break
        tried += 1
        weightdec.MUTANT = m
        try:
            mp = weightdec.decode(path, upto=frame + 1, workdir=workdir)
        except Exception:  # noqa: BLE001
            continue
        finally:
            weightdec.MUTANT = None
        if len(mp) > frame and weightdec.frame_bytes(mp[frame]) == act:
            matches.append(m)
    if matches:
        L.append("your frame is EXACTLY what the reference decoder produces with the wrong rule(s):")
        for m in matches:
            L.append(f"  - {m}: {weightdec.WEIGHT_MUTANTS[m]}")
    else:
        L.append(f"your frame equals none of the {tried} known wrong rules" + ("" if tried == len(names) else " (time budget reached; not all were tried)"))
    H = []
    if cnt[0] == 0 and cnt[1] + cnt[2]:
        H.append("only chroma is wrong: chroma has its own logWD (chroma_log2_weight_denom), its own flag, weight and offset per ref_idx, and Cb and Cr differ.")
    if cnt[0] and not cnt[1] and not cnt[2]:
        H.append("only luma is wrong: luma_weight_l0 / luma_offset_l0 / luma_log2_weight_denom of the entry named by ref_idx.")
    if mine_diff and max(abs(e - a) for _, e, a in mine_diff) <= 1:
        H.append("all differences are +-1: the rounding term 2^(logWD-1) (and the logWD = 0 branch has none), or the order shift-then-offset.")
    if mine_diff and all(e in (0, 255) for _, e, a in mine_diff):
        H.append("the differences are at expected 0 / 255: Clip1 after adding the offset.")
    if mb is not None and mb["name"] == "P_Skip":
        H.append("the first wrong macroblock is a P_Skip: it is weighted like any other macroblock (ref_idx 0).")
    if mb is not None and mb["parts"] and any(p["ref"] for p in mb["parts"]):
        H.append("the macroblock uses ref_idx > 0: the weights come from entry ref_idx of the table, and duplicated list entries have their own weights.")
    if mb is not None and tab[mb["slice"]]["wt"]:
        wtab = tab[mb["slice"]]["wt"]
        used = {p["ref"] for p in mb["parts"]} or {0}
        if any(wtab["flags"][i] == (0, 0) for i in used if i < len(wtab["flags"])):
            H.append("a used entry has both flags 0: its weights are the defaults (2^denom, offset 0), which leave the prediction unchanged; check your defaults.")
    if mb is not None and not H:
        H.append("no single pattern; compare your --weights-dump with the expected one first (--expected-weights-out), then check the order: interpolate, weight, add residual, clip.")
    L.append("hints:")
    L += ["  - " + x for x in H]
    return L


# ================================================================ main check
def run_check(path, yuv, dump_text=None, quiet=False, workdir=None, expected=None):
    td = workdir or tempfile.mkdtemp(prefix="wpred_")
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
            exp = expected_slices(path, td)
            got = parse_weights(dump_text)
        except DumpError as e:
            return 2, "\n".join(out + [f"malformed weights dump: {e}"])
        except SystemExit as e:
            return 2, "\n".join(out + [f"input problem: {e}"])
        ok, lines = compare_weights(exp, got)
        for line in lines:
            say(line)
        say(f"WEIGHTS DUMP: {len(exp)} P slice(s) with a pred_weight_table, " + ("identical to the truth" if ok else "differs from the truth"))
        if not ok:
            worst = 1
    say(("PASS" if worst == 0 else "FAIL") + ": Chapter 7.7")
    return worst, "\n".join(out)


# ================================================================ self-test
def corrupt_text(text, kind):
    lines = text.splitlines()
    if kind == "one_weight":
        i = next(k for k, l in enumerate(lines) if l.startswith("SLICE") and "w0=0:" in l)
        head, ents = lines[i].split(" w0=")
        e = ents.split(",")
        f = e[0].split(":")
        f[1] = str(int(f[1]) + 1)
        e[0] = ":".join(f)
        lines[i] = head + " w0=" + ",".join(e)
    elif kind == "chroma_offset":
        i = len(lines) // 2
        head, ents = lines[i].split(" w0=")
        e = ents.split(",")
        f = e[-1].split(":")
        f[4] = str(int(f[4]) ^ 1)
        e[-1] = ":".join(f)
        lines[i] = head + " w0=" + ",".join(e)
    elif kind == "denom":
        i = len(lines) // 3
        lines[i] = lines[i].replace("denomC=", "denomC=1", 1)
    elif kind == "drop_entry":
        i = next(k for k, l in enumerate(lines) if l.count(":") > 14)
        head, ents = lines[i].split(" w0=")
        lines[i] = head + " w0=" + ",".join(ents.split(",")[:-1])
    elif kind == "drop_line":
        del lines[len(lines) // 2]
    elif kind == "extra_line":
        lines.append("SLICE 999 0 denomY=0 denomC=0 w0=0:1:0:1:0:1:0")
    elif kind == "garbage":
        lines[1] = "SLICE x y"
    return "\n".join(lines) + "\n"


def self_test():
    td = tempfile.mkdtemp(prefix="wpred_selftest_")
    problems = []
    t_start = time.time()

    def bad(msg):
        problems.append(msg)
        print("  PROBLEM:", msg)
    try:
        # ---------------------------------------------------------------- 1. the reference decoder reproduces ffmpeg
        print("1. the reference decoder must reproduce ffmpeg byte for byte on every test stream")
        streams = []
        for n in SYNTH:
            p = os.path.join(td, n + ".264")
            make_synth(n, p)
            streams.append(p)
        for n in VARIANTS:
            p = os.path.join(td, n + ".ts")
            make_variant(n, p)
            streams.append(p)
        streams.append(testfiles.get("T7"))
        stats = collections.Counter()
        usable, expected, ref_out = [], {}, {}
        for s in streams:
            name = os.path.basename(s)
            wd = os.path.join(td, "w_" + name)
            os.makedirs(wd)
            ry = os.path.join(wd, "expected.yuv")
            reference_yuv(s, ry)
            expected[s] = ry
            y = os.path.join(wd, "ref.yuv")
            weightdec.STATS = stats
            try:
                pics = weightdec.decode(s, workdir=wd)
            except weightdec.Unsupported as e:
                bad(f"{name}: the reference decoder cannot decode it ({e})")
                continue
            finally:
                weightdec.STATS = None
            weightdec.write_yuv(pics, y)
            code, text = run_check(s, y, quiet=True, workdir=wd, expected=ry)
            print(f"  {name:24s} {len(pics):3d} pictures: {'identical to ffmpeg' if code == 0 else 'DIFFERENT'}")
            if code != 0:
                bad(f"the reference decoder does not reproduce ffmpeg on {name}:\n{text[-500:]}")
                continue
            usable.append(s)
            ref_out[s] = y
            # the weights dump of ffmpeg's trace must be parseable and stable
            exp = expected_slices(s, wd)
            code, _ = run_check(s, None, dump_text=render_weights(exp), quiet=True, workdir=wd)
            if code != 0:
                bad(f"{name}: the expected weights dump does not pass its own check")
        print("   coverage: " + ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
        need = [f"logWD {d} {p}" for d in range(8) for p in ("luma", "chroma")]
        need += ["clip at 0", "clip at 255", "negative weight", "negative offset", "positive offset", "duplicate list entry used",
                 "P_Skip with weights", "ref_idx > 0"] + [f"flags luma={a} chroma={b}" for a in (0, 1) for b in (0, 1)]
        for k in need:
            if not stats[k]:
                bad(f"no test stream exercises '{k}'")
        # ---------------------------------------------------------------- 2. corrupted pixel outputs
        print("2. corrupted outputs must be rejected")
        s = [x for x in usable if x.endswith("synth_manyrefs.264")][0]
        wd = os.path.join(td, "w_" + os.path.basename(s))
        data = bytearray(open(ref_out[s], "rb").read())
        fs = 96 * 80 * 3 // 2
        for label, mod in (("one luma sample changed", lambda d: d.__setitem__(5 * fs + 700, d[5 * fs + 700] ^ 1)),
                           ("last byte missing", lambda d: d.pop()),
                           ("one chroma sample changed", lambda d: d.__setitem__(3 * fs + 96 * 80 + 5, d[3 * fs + 96 * 80 + 5] ^ 4))):
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
        # ---------------------------------------------------------------- 3. weights dump channel
        print("3. the weights dump channel")
        for s in (x for x in usable if x.endswith("synth_flags.264") or x.endswith("T7.ts")):
            wd = os.path.join(td, "w_" + os.path.basename(s))
            text = render_weights(expected_slices(s, wd))
            code, _ = run_check(s, None, dump_text=text, quiet=True, workdir=wd)
            print(f"   {os.path.basename(s)}: truth dump: exit {code}")
            if code != 0:
                bad("the truth dump must pass")
            for kind, want in (("one_weight", 1), ("chroma_offset", 1), ("denom", 1), ("drop_entry", 1), ("drop_line", 1),
                               ("extra_line", 1), ("garbage", 2)):
                code, txt = run_check(s, None, dump_text=corrupt_text(text, kind), quiet=True, workdir=wd)
                print(f"     corrupted ({kind}): exit {code}")
                if code != want:
                    bad(f"corrupted dump ({kind}) on {os.path.basename(s)} gave exit {code}, expected {want}")
        t7 = [x for x in usable if x.endswith("T7.ts")][0]
        t = render_weights(expected_slices(t7, os.path.join(td, "w_T7.ts")))
        print("   T7, first slice with a table:", t.splitlines()[0][:150])
        # ---------------------------------------------------------------- 4. mutants
        names = list(weightdec.WEIGHT_MUTANTS)
        print(f"4. {len(names)} deliberately wrong rules: each must change the output of some stream, and the diagnosis must name it")
        order = [x for x in usable if x.endswith(".264")] + [x for x in usable if not x.endswith(".264") and not x.endswith("T7.ts")] \
            + [x for x in usable if x.endswith("T7.ts")]
        for m in names:
            caught = None
            for s in order:
                wd = os.path.join(td, "w_" + os.path.basename(s))
                y = os.path.join(wd, "mut.yuv")
                weightdec.MUTANT = m
                try:
                    weightdec.write_yuv(weightdec.decode(s, workdir=wd), y)
                except weightdec.Unsupported:
                    continue
                finally:
                    weightdec.MUTANT = None
                code, txt = run_check(s, y, quiet=True, workdir=wd, expected=expected[s])
                if code == 1:
                    caught = (os.path.basename(s), txt)
                    break
            if not caught:
                print(f"   {m:26s} NOT visible in any stream")
                bad(f"wrong rule {m!r} was not detected by any test stream")
                continue
            named = f"- {m}:" in caught[1]
            print(f"   {m:26s} changes the pixels of {caught[0]}; diagnosis {'names it' if named else 'DOES NOT name it'}")
            if not named:
                bad(f"wrong rule {m!r}: the diagnosis on {caught[0]} does not name it")
    finally:
        weightdec.MUTANT = None
        weightdec.STATS = None
        shutil.rmtree(td, ignore_errors=True)
    print(f"(self-test took {time.time() - t_start:.0f} s)")
    if problems:
        print(f"SELF-TEST FAIL: {len(problems)} problem(s)")
        return 1
    print("SELF-TEST PASS")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="input .ts / .264 (CAVLC I/P stream, weighted_pred_flag 0 or 1)")
    ap.add_argument("output", nargs="?", help="your decoded frames, raw yuv420p, cropped (optional when only --weights-dump is checked)")
    ap.add_argument("--weights-dump", metavar="FILE", help="your weight-table dump (grammar above) to compare with ffmpeg's trace")
    ap.add_argument("--expected-weights-out", metavar="FILE", help="write the expected weights dump of <input> to FILE and exit")
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
        die("error: need <input> and <your_output.yuv> and/or --weights-dump FILE")
    if not os.path.isfile(a.input):
        die(f"error: input file {a.input!r} not found")
    td = tempfile.mkdtemp(prefix="wpred_")
    try:
        if a.expected_weights_out:
            try:
                text = render_weights(expected_slices(a.input, td))
            except SystemExit as e:
                die(f"error: {e}")
            with open(a.expected_weights_out, "w") as f:
                f.write(text)
            print(f"wrote the expected weights dump ({text.count(chr(10))} slices) to {a.expected_weights_out}")
            return
        if not a.output and not a.weights_dump:
            die("error: need <your_output.yuv> and/or --weights-dump FILE")
        dump_text = None
        if a.weights_dump:
            if not os.path.isfile(a.weights_dump):
                die(f"error: your weights dump {a.weights_dump!r} not found")
            dump_text = open(a.weights_dump, errors="replace").read()
        code, _ = run_check(a.input, a.output, dump_text=dump_text, workdir=td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
