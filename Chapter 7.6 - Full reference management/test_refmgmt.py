#!/usr/bin/env python3
"""Harness for Chapter 7.6 (full reference picture management: ref_pic_list_modification, long-term references, MMCO 1-6,
IDR long_term_reference_flag, frame_num gaps).  It extends Chapter 7.2's log.

    python3 test_refmgmt.py <input.ts|input.264> <refs_log.txt>            # check your reader's output
    python3 test_refmgmt.py <input> <refs_log.txt> --yuv decoded.yuv        # ... and your decoded frames (inputs ffmpeg can prove)
    python3 test_refmgmt.py --print-expected <input> <out.txt>             # write the expected log, to diff against yours
    python3 test_refmgmt.py --make-variants <out_dir>                      # extra test inputs (see Readme)
    python3 test_refmgmt.py --self-test

<refs_log.txt> (grammar in the Readme, section 5), two kinds of lines:

    SLICE <pic> <slice> type=P frame_num=<n> active=<n> list0=<slot>,...      one per P slice; slot = <pic>:<PicNum> | <pic>:L<LongTermPicNum> | -
    DPB <pic> short=<ref>,... long=<ref>,... maxlt=<n|none>                    one per picture, after its marking process

<pic> is the 0-based DECODING-order index of a coded picture (Chapter 3.4's grouping); a "non-existing" frame created by the frame_num gap
process is named g<k> (k = 0, 1, ... in order of creation over the whole stream).

Truth.  The harness runs its own implementation of 8.2.4 / 8.2.5 (spoiler_reference/refmgmt_ref.py) on the slice-header values that
ffmpeg's trace_headers prints (including the modification commands and MMCO operations).  That implementation is validated before
your output is compared; a failure there is a harness problem (exit 2), not yours:
  1. ffmpeg `-debug mmco` prints "short term list:" and "long term list:" after every marking process (every reference picture and every
     non-existing frame): the frame_nums of the short-term frames (newest first) and (LongTermFrameIdx, frame_num) of the long-term
     frames must equal the reference's state.  All inputs.
  2. Synthetic streams (spoiler_reference/refmgmtgen.py): every inter macroblock has mvd 0 and no residual, so the decoded pixels are an exact
     copy of the picture RefPicList0[ref_idx] names (AFTER the modification commands); "Missing reference picture" messages count the
     "no reference picture" entries.
Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output, unsupported or unusable input (field pictures, SP/SI, a frame_num gap
without gaps_in_frame_num_value_allowed_flag, a stream that violates the standard), or reference validation failure.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles, yuvcmp  # noqa: E402
from common.report import Report  # noqa: E402
import refmgmt_ref as R  # noqa: E402  (spoiler_reference/refmgmt_ref.py: the reference model)
import refmgmtgen as G  # noqa: E402  (spoiler_reference/refmgmtgen.py: the synthetic-stream generator)

SRC = "testsrc2=size=352x288:rate=25"
FADE = ["-vf", "fade=in:0:25"]
# x264 variants: name -> (extra input args, codec options, rate control)
VARIANTS = {
    "x264_weightp_ref6": (FADE, "-profile:v main -x264-params cabac=0:bframes=0:weightp=2:ref=6:keyint=100", ["-qp", "26"]),
    "x264_pyramid": ([], "-profile:v main -x264-params cabac=0:bframes=3:b-pyramid=normal:weightb=0:weightp=0:ref=4:keyint=100",
                     ["-qp", "26"]),
    "x264_pyramid_ref8": ([], "-profile:v main -x264-params cabac=0:bframes=5:b-pyramid=normal:weightp=0:ref=8:keyint=100",
                          ["-qp", "26"]),
}
SYNTH = list(G.SCENARIOS)
PREFIX_RE = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
FIELD_RE = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header")
NON_VCL_HINT = {6, 7, 8, 9, 14, 15, 16, 17, 18}
MISSING = "Missing reference picture"


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ================================================================ making inputs
def make_variant(name, out_ts):
    vf, opts, rc = VARIANTS[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC, "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
           *rc, *vf, *opts.split(), "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die(f"ffmpeg failed for {name}: {r.stderr.decode()[-500:]}")


def manifest_path(p):
    return (p[:-4] if p.lower().endswith(".264") else p) + ".manifest.json"


def make_synth(name, out_264):
    data, man = G.make_stream(name)
    open(out_264, "wb").write(data)
    json.dump(man, open(manifest_path(out_264), "w"))


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for t in ("T6_ref16", "T7", "T9_spatial"):
        shutil.copy(testfiles.get(t), os.path.join(out_dir, t + ".ts"))
        print("wrote", os.path.join(out_dir, t + ".ts"))
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for n in SYNTH:
        p = os.path.join(out_dir, f"synth_ref_{n}.264")
        make_synth(n, p)
        print("wrote", p, "and", manifest_path(p))


# ================================================================ slice headers from ffmpeg trace_headers (as in Chapter 7.2)
class Block:
    def __init__(self, title, extradata):
        self.title, self.extradata, self.fields = title, extradata, []


def parse_trace(path):
    blocks, cur, started = [], None, False
    with open(path, "r", errors="replace") as f:
        for raw in f:
            if (raw.startswith("[") and not raw.startswith("[trace_headers")) or raw.startswith("    Last message"):
                continue
            line = PREFIX_RE.sub("", raw.rstrip("\n"))
            if line.startswith("Packet:"):
                started, cur = True, None
                continue
            m = FIELD_RE.match(line)
            if m and cur is not None:
                cur.fields.append((m.group(2), int(m.group(4))))
            elif line in TITLES:
                cur = Block(line, not started)
                blocks.append(cur)
            else:
                cur = None
    return blocks


def first_values(fields):
    d = {}
    for n, v in fields:
        d.setdefault(n, v)
    return d


def nal_units(data):
    pos = [m.start() for m in re.finditer(b"\x00\x00\x01", data)]
    out = []
    for i, p in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else len(data)
        out.append(data[p + 3:end].rstrip(b"\x00"))
    return [n for n in out if n]


def raw_nal_types(path, td):
    raw = path
    if not path.lower().endswith((".264", ".h264")):
        raw = os.path.join(td, "raw.264")
        ffref.raw_h264(path, raw)
    return [n[0] & 31 for n in nal_units(open(raw, "rb").read())]


def parse_commands(fields):
    """(list0 modification commands [(idc, value)], MMCO list [(op, args)] or None) from the ordered fields of a slice header.
    The commands appear as repeated syntax elements; they are read in order, as the syntax tables (7.3.3.1, 7.3.3.3) prescribe."""
    names = fields
    mods, mm = [], None
    idx = next((i for i, (n, _) in enumerate(names) if n == "ref_pic_list_modification_flag_l0"), None)
    if idx is not None and names[idx][1] == 1:
        i = idx + 1
        while i < len(names) and names[i][0] == "modification_of_pic_nums_idc":
            idc = names[i][1]
            i += 1
            if idc == 3:
                break
            mods.append((idc, names[i][1]))
            i += 1
    idx = next((i for i, (n, _) in enumerate(names) if n == "adaptive_ref_pic_marking_mode_flag"), None)
    if idx is not None and names[idx][1] == 1:
        mm, i = [], idx + 1
        order = {1: ("difference_of_pic_nums_minus1",), 2: ("long_term_pic_num",),
                 3: ("difference_of_pic_nums_minus1", "long_term_frame_idx"), 4: ("max_long_term_frame_idx_plus1",),
                 5: (), 6: ("long_term_frame_idx",)}
        while i < len(names) and names[i][0] == "memory_management_control_operation":
            op = names[i][1]
            i += 1
            if op == 0:
                break
            a = {}
            for k in order[op]:
                if i >= len(names) or names[i][0] != k:
                    die(f"internal error: expected {k} after MMCO {op} in the slice header trace")
                a[k] = names[i][1]
                i += 1
            mm.append((op, a))
    return mods, mm


class Slice:
    pass


def load_slices(path, td):
    tr = os.path.join(td, "trace.txt")
    rc = ffref.trace_headers(path, tr)
    errs = [PREFIX_RE.sub("", l.strip()) for l in open(tr, errors="replace")
            if "Failed to read unit" in l or "not available" in l or "but must be in" in l]
    if errs:
        die(f"ffmpeg trace_headers could not parse all of {path} (exit {rc}):\n  " + "\n  ".join(errs[:5]))
    blocks = parse_trace(tr)
    types = raw_nal_types(path, td)
    sl_nals = [i for i, t in enumerate(types) if t in (1, 5)]
    sps_t, pps_t, out = {}, {}, []
    for b in blocks:
        d = first_values(b.fields)
        if b.title == "Sequence Parameter Set":
            sps_t[d["seq_parameter_set_id"]] = d
        elif b.title == "Picture Parameter Set":
            pps_t[d["pic_parameter_set_id"]] = d
        elif not b.extradata and d.get("nal_unit_type") in (1, 5):
            pid = d["pic_parameter_set_id"]
            if pid not in pps_t or pps_t[pid]["seq_parameter_set_id"] not in sps_t:
                die(f"input problem: slice #{len(out)} uses PPS {pid} which is not available before it")
            s = Slice()
            s.d = d
            s.nal_type, s.ref_idc = d["nal_unit_type"], d["nal_ref_idc"]
            s.pps = pps_t[pid]
            s.sps = sps_t[s.pps["seq_parameter_set_id"]]
            s.mods, s.mmco = parse_commands(b.fields)
            out.append(s)
    if len(out) != len(sl_nals):
        die(f"internal error: {len(sl_nals)} slice NAL units in the stream but {len(out)} slice headers in the trace")
    prev = -1
    for s, i in zip(out, sl_nals):
        s.hint = any(t in NON_VCL_HINT for t in types[prev + 1:i])
        prev = i
    if not out:
        die(f"no slice found in {path}")
    return out


def boundary(a, b):
    """True if slice b starts a new picture (7.4.1.2.4)."""
    da, db = a.d, b.d
    if b.hint:
        return True
    for k in ("frame_num", "pic_parameter_set_id", "field_pic_flag", "bottom_field_flag"):
        if da.get(k, 0) != db.get(k, 0):
            return True
    if (a.ref_idc == 0) != (b.ref_idc == 0):
        return True
    t = b.sps["pic_order_cnt_type"]
    keys = {0: ("pic_order_cnt_lsb", "delta_pic_order_cnt_bottom"), 1: ("delta_pic_order_cnt[0]", "delta_pic_order_cnt[1]")}.get(t, ())
    if any(da.get(k, 0) != db.get(k, 0) for k in keys):
        return True
    if (a.nal_type == 5) != (b.nal_type == 5):
        return True
    return a.nal_type == 5 and da["idr_pic_id"] != db["idr_pic_id"]


def build_pictures(slices):
    """-> (list of R.Pic in decoding order, (max_num_ref_frames, log2_max_frame_num, gaps_allowed)).  Refuses (exit 2) what is out of scope."""
    groups, prev = [], None
    for s in slices:
        if s.d.get("redundant_pic_cnt", 0) > 0:
            continue
        if prev is None or boundary(prev, s):
            groups.append([s])
        else:
            groups[-1].append(s)
        prev = s
    pics, params = [], None
    for i, g in enumerate(groups):
        f = g[0]
        sps = f.sps
        pr = (sps["max_num_ref_frames"], sps["log2_max_frame_num_minus4"] + 4, bool(sps.get("gaps_in_frame_num_allowed_flag", 0)))
        if params is None:
            params = pr
        elif pr != params and f.nal_type != 5:
            die(f"unsupported input: picture {i}: a different SPS (max_num_ref_frames / frame_num size / gaps flag) without an IDR")
        elif pr != params:
            die(f"unsupported input: picture {i}: an IDR picture switches to an SPS with other max_num_ref_frames / log2_max_frame_num "
                f"/ gaps flag; this harness keeps one DPB configuration per file")
        for s in g:
            if s.d.get("field_pic_flag", 0):
                die(f"unsupported input: picture {i}: field picture (field_pic_flag = 1) (Phase 10)")
            if (s.d["slice_type"] % 5) in (3, 4):
                die(f"unsupported input: picture {i}: SP/SI slice")
        p = R.Pic()
        p.idx, p.idr, p.ref_idc, p.frame_num = i, f.nal_type == 5, f.ref_idc, f.d["frame_num"]
        p.long_term_reference_flag = f.d.get("long_term_reference_flag", 0) if p.idr else 0
        p.mmcos = f.mmco                    # the FIRST slice's dec_ref_pic_marking (all slices carry the same one)
        p.slices = []
        for s in g:
            t = s.d["slice_type"] % 5
            act = 0
            if t == 0:
                act = (s.d["num_ref_idx_l0_active_minus1"] if s.d.get("num_ref_idx_active_override_flag", 0)
                       else s.pps["num_ref_idx_l0_default_active_minus1"]) + 1
            p.slices.append(dict(type=t, active=act, mods=s.mods if t == 0 else []))
        pics.append(p)
    return pics, params


# ================================================================ ffmpeg: -debug mmco
def ffmpeg_posts(path, td):
    """ffmpeg's view after every marking process: list of ([short frame_nums newest first], [(LongTermFrameIdx, frame_num)]).
    Non-existing frames of the gap process get their own entry.  -> (posts, number of "Missing reference picture" messages).
    ffprobe -count_frames decodes without the ffmpeg CLI's filter/muxer threads, whose debug output (filter format lists printed
    in pieces) can otherwise splice into the decoder's lines and garble them; build_truth() still reruns on an inconsistency."""
    log = os.path.join(td, "mmco.log")
    cmd = ["ffprobe", "-hide_banner", "-loglevel", "repeat+debug", "-debug", "mmco", "-threads", "1", "-count_frames",
           "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += [path]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f, timeout=300, check=False)
    pre = re.compile(r"^\[([^\]]*)\]\s?")
    text = open(log, errors="replace").read().splitlines()
    inst, start = None, 0            # stream probing decodes a few frames first; the real decode starts at the last reinit
    for k, raw in enumerate(text):
        if "Reinit context" in raw:
            inst, start = raw[:raw.index("]") + 1], k
    blocks, cur, mode, marking, nmiss = [], None, None, False, 0
    ent = re.compile(r"^(\d+) fn:(\d+) poc:-?\d+")
    for raw in text[start:]:
        if inst and not raw.startswith(inst):
            continue
        line = pre.sub("", raw)
        if MISSING in line:
            nmiss += 1
        if line.startswith("no mmco here") or line.startswith("mmco:"):
            marking = True
        elif line == "short term list:":
            cur = dict(marking=marking, short=[], long=[])
            marking = False
            blocks.append(cur)
            mode = "short"
        elif line == "long term list:":
            mode = "long"
        else:
            m = ent.match(line)
            if m and cur is not None:
                if mode == "short":
                    cur["short"].append(int(m.group(2)))
                elif mode == "long":
                    cur["long"].append((int(m.group(1)), int(m.group(2))))
    return [(b["short"], b["long"]) for b in blocks if b["marking"]], nmiss


def check_posts(model_posts, ff_posts):
    if len(model_posts) != len(ff_posts):
        return [f"ffmpeg's -debug mmco log has {len(ff_posts)} marking results but the reference expects {len(model_posts)} "
                f"(reference pictures + non-existing frames)"]
    out = []
    for k, (m, f) in enumerate(zip(model_posts, ff_posts)):
        if m[0] != f[0] or m[1] != f[1]:
            out.append(f"marking #{k}: reference short {m[0]} long {m[1]} but ffmpeg short {f[0]} long {f[1]}")
    return out


# ================================================================ ffmpeg: pixels of synthetic streams
def decode_synth(path, manifest, td):
    yuv = os.path.join(td, "synth.yuv")
    p = ffref.run(["ffmpeg", "-v", "error", "-loglevel", "repeat+error", "-threads", "1", "-y", "-f", "h264", "-i", path,
                   "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", yuv], timeout=120)
    lines = [l for l in p.stderr.decode(errors="replace").splitlines() if l.strip()]
    other = [l for l in lines if MISSING not in l]
    if other or p.returncode != 0:
        return ["ffmpeg reports problems decoding the synthetic stream: " + " | ".join(other)[:300]], []
    w, h = manifest["width_mbs"] * 16, manifest["height_mbs"] * 16
    fs = w * h * 3 // 2
    data = open(yuv, "rb").read()
    n = len(manifest["pictures"])
    if len(data) != fs * n:
        return [f"ffmpeg produced {len(data) // fs} frames, expected {n}"], []
    return [], [data[i * fs:(i + 1) * fs] for i in range(n)]


def mb_pixels(frame, wm, hm, addr):
    w, h = wm * 16, hm * 16
    my, mx = divmod(addr, wm)
    out = bytearray()
    for yy in range(16):
        o = (my * 16 + yy) * w + mx * 16
        out += frame[o:o + 16]
    for plane in range(2):
        base = w * h + plane * (w // 2) * (h // 2)
        for yy in range(8):
            o = base + (my * 8 + yy) * (w // 2) + mx * 8
            out += frame[o:o + 8]
    return bytes(out)


def check_pixels(slice_rows, manifest, frames, nmiss):
    """-> (problems, stats).  Every inter macroblock must equal the picture RefPicList0[ref_idx] names."""
    wm, hm = manifest["width_mbs"], manifest["height_mbs"]
    rows = {(r["pic"], r["slice"]): r for r in slice_rows}
    problems, ncopy, ndisc, npcm, nnone, ndup = [], 0, 0, 0, 0, 0
    for pi, pic in enumerate(manifest["pictures"]):
        for si, s in enumerate(pic["slices"]):
            row = rows.get((pi, si))
            for e in s["mbs"]:
                if e["kind"] == "pcm":
                    npcm += 1
                    if mb_pixels(frames[pi], wm, hm, e["addr"]) != bytes.fromhex(e["samples"]):
                        problems.append(f"picture {pi}: I_PCM macroblock {e['addr']} not decoded to its samples")
                    continue
                if row is None:
                    problems.append(f"picture {pi} slice {si}: no list in the reference for a P slice")
                    break
                ent = row["entries"][e["ref"]] if e["ref"] < len(row["entries"]) else None
                if ent is None or isinstance(ent[0], str):
                    problems.append(f"picture {pi} slice {si} MB {e['addr']}: ref_idx {e['ref']} points at "
                                    f"{'a non-existing frame' if ent else 'no reference picture'} in the reference list")
                    continue
                got = mb_pixels(frames[pi], wm, hm, e["addr"])
                exp = mb_pixels(frames[ent[0]], wm, hm, e["addr"])
                ncopy += 1
                if got != exp:
                    problems.append(f"picture {pi} slice {si} MB {e['addr']} ref_idx {e['ref']}: decoded pixels are not a copy of "
                                    f"picture {ent[0]} (the reference's list0[{e['ref']}])")
                others = [mb_pixels(frames[x[0]], wm, hm, e["addr"]) for x in row["entries"]
                          if x is not None and not isinstance(x[0], str) and x[0] != ent[0]]
                ndisc += bool(others) and all(o != exp for o in others)
    for r in slice_rows:
        nnone += sum(1 for x in r["entries"] if x is None)
        labs = [x[0] for x in r["entries"] if x is not None]
        ndup += len(labs) != len(set(labs))
    if nmiss != nnone:
        problems.append(f"ffmpeg printed 'Missing reference picture' {nmiss} times; the reference has {nnone} 'no reference "
                        f"picture' list entries")
    return problems, dict(ncopy=ncopy, ndisc=ndisc, npcm=npcm, nnone=nnone, ndup=ndup)


# ================================================================ truth for one input
class Truth:
    pass


def simulate(t, mutant=None):
    try:
        return R.simulate(t.pics, *t.params, mutant=mutant)
    except R.StreamError as e:
        if mutant:
            raise
        die(f"unsupported input or non-conforming stream: {e}")


def build_truth(path, td):
    t = Truth()
    t.path = path
    t.pics, t.params = build_pictures(load_slices(path, td))
    t.slice_rows, t.dpb_rows, t.gap_rows, t.posts = simulate(t)
    t.manifest = t.frames = None
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        t.manifest = json.load(open(mp))
        probs, t.frames = decode_synth(path, t.manifest, td)
        if probs:
            die("HARNESS PROBLEM: " + probs[0])
        if len(t.manifest["pictures"]) != len(t.pics):
            die("HARNESS PROBLEM: the manifest and the stream disagree about the number of pictures")
    for attempt in range(5):
        t.ff_posts, t.nmiss = ffmpeg_posts(path, td)
        p1, p2, _ = validate(t)
        if not p1 and not p2:
            break
    return t


def validate(t, rows=None):
    """Problems found when a (possibly mutated) reference result is checked against ffmpeg.  rows = (slice_rows, posts)."""
    slice_rows, posts = rows if rows is not None else (t.slice_rows, t.posts)
    p1 = check_posts(posts, t.ff_posts)
    p2, stats = [], None
    if t.manifest is not None:
        p2, stats = check_pixels(slice_rows, t.manifest, t.frames, t.nmiss)
    return p1, p2, stats


def describe(t, stats):
    nref = sum(1 for p in t.pics if p.ref_idc)
    ngap = sum(len(g) for _, g in t.gap_rows)
    nmm = sum(1 for p in t.pics if p.mmcos)
    nmod = sum(1 for p in t.pics for s in p.slices if s["mods"])
    s = (f"ffmpeg -debug mmco: short- and long-term sets agree after all {nref} reference pictures"
         f"{f' and {ngap} non-existing frame(s)' if ngap else ''} ({nmm} with MMCO, {nmod} slices with list modification)")
    if stats:
        s += (f"; synthetic pixels: {stats['ncopy']} inter macroblocks are exact copies of list0[ref_idx] "
              f"({stats['ndisc']} tell it apart from every other list entry; {stats['ndup']} lists hold a picture twice), "
              f"{stats['npcm']} I_PCM exact, {stats['nnone']} 'no reference picture' entries confirmed by ffmpeg's messages")
    return s


# ================================================================ rendering / parsing the log
def fmt_slot(e):
    return "-" if e is None else f"{e[0]}:{'L' if e[1] == 'L' else ''}{e[2]}"


def fmt_ref(e):
    return f"{e[0]}:{e[1]}"


def fmt_mx(m):
    return "none" if m is None else str(m)


def render(slice_rows, dpb_rows):
    lines, by_pic = [], {}
    for r in slice_rows:
        by_pic.setdefault(r["pic"], []).append(r)
    for pic, sh, lg, mx in dpb_rows:
        for r in by_pic.get(pic, []):
            lines.append(f"SLICE {pic} {r['slice']} type=P frame_num={r['frame_num']} active={r['active']} "
                         f"list0={','.join(fmt_slot(e) for e in r['entries']) or '-'}")
        lines.append(f"DPB {pic} short={','.join(map(fmt_ref, sh)) or '-'} long={','.join(map(fmt_ref, lg)) or '-'} maxlt={fmt_mx(mx)}")
    return "\n".join(lines) + "\n"


def parse_label(x, ln):
    return x if x.startswith("g") else int(x)


def parse_slot(tok, ln):
    if tok == "-":
        return None
    m = re.fullmatch(r"(g?\d+):(L?)(-?\d+)", tok)
    if not m:
        die(f"line {ln}: bad list slot {tok!r} (expected <pic>:<PicNum>, <pic>:L<LongTermPicNum> or -)")
    return (parse_label(m.group(1), ln), "L" if m.group(2) else "S", int(m.group(3)))


def parse_ref(tok, ln):
    m = re.fullmatch(r"(g?\d+):(\d+)", tok)
    if not m:
        die(f"line {ln}: bad DPB entry {tok!r} (expected <pic>:<number>)")
    return (parse_label(m.group(1), ln), int(m.group(2)))


def kvs(tok, ln, keys, line):
    kv = {}
    for t_ in tok:
        k, eq, v = t_.partition("=")
        if not eq:
            die(f"line {ln}: expected key=value, got {t_!r}: {line}")
        kv[k] = v
    if set(kv) != set(keys):
        die(f"line {ln}: keys must be exactly {', '.join(keys)} (got {', '.join(kv)}): {line}")
    return kv


def parse_log(path):
    try:
        text = open(path, "r", errors="replace").read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    slices, dpb = {}, {}
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        try:
            if tok[0] == "SLICE":
                if len(tok) != 7:
                    die(f"line {ln}: SLICE needs 7 fields (SLICE pic slice type= frame_num= active= list0=), got {len(tok)}: {line}\n"
                        f"  (Chapter 7.2's lines lack type=P; add it)")
                kv = kvs(tok[3:], ln, ("type", "frame_num", "active", "list0"), line)
                if kv["type"] != "P":
                    die(f"line {ln}: only type=P slices are logged in this chapter: {line}")
                key = (int(tok[1]), int(tok[2]))
                if key in slices:
                    die(f"line {ln}: duplicate SLICE {key[0]} {key[1]}")
                slices[key] = dict(frame_num=int(kv["frame_num"]), active=int(kv["active"]),
                                   entries=[parse_slot(x, ln) for x in kv["list0"].split(",")])
            elif tok[0] == "DPB":
                if len(tok) != 5:
                    die(f"line {ln}: DPB needs 5 fields (DPB pic short= long= maxlt=), got {len(tok)}: {line}\n"
                        f"  (Chapter 7.2's refs= is now short=, and long= and maxlt= are new)")
                kv = kvs(tok[2:], ln, ("short", "long", "maxlt"), line)
                sh = [] if kv["short"] == "-" else [parse_ref(x, ln) for x in kv["short"].split(",")]
                lg = [] if kv["long"] == "-" else [parse_ref(x, ln) for x in kv["long"].split(",")]
                mx = None if kv["maxlt"] == "none" else int(kv["maxlt"])
                if int(tok[1]) in dpb:
                    die(f"line {ln}: duplicate DPB {tok[1]}")
                dpb[int(tok[1])] = (sh, lg, mx)
            else:
                die(f"line {ln}: unknown line type {tok[0]!r}: {line}")
        except ValueError:
            die(f"line {ln}: malformed numbers: {line}")
    if not slices and not dpb:
        die("reader output is empty")
    return slices, dpb


def hint_list(exp, got):
    ep = [e for e in exp if e]
    gp = [e for e in got if e]
    if len(exp) != len(got):
        return " (hint: list length differs: the list has exactly num_ref_idx_active entries after the modification; a longer temporary list must be cut back)"
    if [e[:2] for e in ep] == [e[:2] for e in gp]:
        return " (hint: right pictures but wrong PicNum / LongTermPicNum: FrameNumWrap, or the long-term number of the entry)"
    if any(e[1] == "L" for e in ep + gp) and [e[0] for e in ep if e[1] == "S"] == [e[0] for e in gp if e[1] == "S"]:
        return " (hint: the short-term part is right; look at the long-term entries: after the short-term ones, ascending LongTermPicNum)"
    if sorted(map(str, [e[0] for e in ep])) == sorted(map(str, [e[0] for e in gp])):
        return " (hint: same pictures, different order: initial order, or the modification insertion / duplicate removal)"
    if len({e[0] for e in gp}) < len({e[0] for e in ep}):
        return " (hint: a picture is missing from your list: did the insertion shift entries and drop the wrong one, or remove too many duplicates?)"
    return " (hint: different pictures: wrong DPB content before this slice, or a wrong picNumLXPred / picNumLX computation)"


def hint_dpb(e, g):
    es, el, em = e
    gs, gl, gm = g
    if gm != em:
        return " (hint: MaxLongTermFrameIdx: set by MMCO 4 (value - 1, or 'none' for 0), reset to none by MMCO 5 and by an IDR, 0 for an IDR with long_term_reference_flag)"
    if el != gl:
        return " (hint: long-term set differs: MMCO 2/3/4/6 and IDR long_term_reference_flag; MMCO 3 and 6 first free the frame that holds the index)"
    if sorted(map(str, es)) == sorted(map(str, gs)):
        return " (same members, different order: short= lists the most recently decoded first)"
    if len(gs) > len(es):
        return " (too many short-term frames: sliding window not run / counts only short-term? / MMCO not applied)"
    if len(gs) < len(es):
        return " (too few short-term frames: sliding window too early or counting wrongly; MMCO 1/3 removed the wrong frame?)"
    return " (a different short-term frame was removed: sliding window = smallest FrameNumWrap; MMCO 1 picNumX = CurrPicNum - (difference_of_pic_nums_minus1 + 1))"


def compare(rep, exp_slices, exp_dpb, got_slices, got_dpb):
    pics = sorted({k[0] for k in exp_slices} | {k[0] for k in got_slices} | set(exp_dpb) | set(got_dpb))
    for pic in pics:
        keys = sorted({k for k in exp_slices if k[0] == pic} | {k for k in got_slices if k[0] == pic})
        for key in keys:
            lab = f"SLICE {key[0]} {key[1]}"
            if key not in got_slices:
                rep.check(False, f"missing line: {lab} (a P slice)")
                continue
            if key not in exp_slices:
                rep.check(False, f"unexpected line: {lab} (not a P slice, or not a slice of this stream)")
                continue
            e, g = exp_slices[key], got_slices[key]
            rep.compare(f"{lab} frame_num", e["frame_num"], g["frame_num"])
            rep.compare(f"{lab} active", e["active"], g["active"])
            if e["entries"] != g["entries"]:
                n = max(len(e["entries"]), len(g["entries"]))
                diff = next((i for i in range(n) if (e["entries"][i] if i < len(e["entries"]) else "none") !=
                             (g["entries"][i] if i < len(g["entries"]) else "none")), 0)
                rep.check(False, f"{lab} list0: expected {','.join(map(fmt_slot, e['entries']))} got "
                                 f"{','.join(map(fmt_slot, g['entries']))} (first difference at ref_idx {diff})"
                                 f"{hint_list(e['entries'], g['entries'])}")
            else:
                rep.check(True, "")
        if pic not in got_dpb:
            if pic in exp_dpb:
                rep.check(False, f"missing line: DPB {pic}")
            continue
        if pic not in exp_dpb:
            rep.check(False, f"unexpected line: DPB {pic} (the stream has {len(exp_dpb)} pictures, index 0..{len(exp_dpb) - 1})")
            continue
        e, g = exp_dpb[pic], got_dpb[pic]
        rep.check(e == g, f"DPB {pic}: expected short={','.join(map(fmt_ref, e[0])) or '-'} long={','.join(map(fmt_ref, e[1])) or '-'} "
                          f"maxlt={fmt_mx(e[2])} got short={','.join(map(fmt_ref, g[0])) or '-'} long={','.join(map(fmt_ref, g[1])) or '-'} "
                          f"maxlt={fmt_mx(g[2])}{hint_dpb(e, g)}")


def expected_dicts(slice_rows, dpb_rows):
    return ({(r["pic"], r["slice"]): dict(frame_num=r["frame_num"], active=r["active"], entries=r["entries"])
             for r in slice_rows}, {pic: (sh, lg, mx) for pic, sh, lg, mx in dpb_rows})


def trust(t, quiet=False):
    p1, p2, stats = validate(t)
    if p1 or p2:
        print("HARNESS PROBLEM (the reference model disagrees with ffmpeg; this is not your bug):")
        for p in (p1 + p2)[:10]:
            print("  ", p)
        sys.exit(2)
    if not quiet:
        print(" ", describe(t, stats))


def check_yuv(t, path, yuv):
    """Compare the reader's decoded frames with ffmpeg's (inputs whose pixels need only features of Phases 5-7.6)."""
    if not os.path.exists(yuv):
        die(f"decoded frames file not found: {yuv}")
    with tempfile.TemporaryDirectory() as td:
        ref = os.path.join(td, "ref.yuv")
        if path.lower().endswith(".264"):
            ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-i", path, "-fps_mode", "passthrough", "-f", "rawvideo",
                       "-pix_fmt", "yuv420p", ref], timeout=300)
        else:
            ffref.reference_yuv(path, ref)
        from common import yuvref
        if t.manifest:
            w, h = t.manifest["width_mbs"] * 16, t.manifest["height_mbs"] * 16
        else:
            info = yuvref.stream_info(path)
            w, h = info["width"], info["height"]
        res = yuvcmp.compare_yuv(ref, yuv, w, h, quiet=False)
    return res


def run_check(path, log, yuv=None):
    with tempfile.TemporaryDirectory() as td:
        t = build_truth(path, td)
        trust(t)
    got_s, got_d = parse_log(log)
    exp_s, exp_d = expected_dicts(t.slice_rows, t.dpb_rows)
    rep = Report(f"reference management of {os.path.basename(path)}")
    compare(rep, exp_s, exp_d, got_s, got_d)
    print(f"  {len(exp_d)} pictures, {len(exp_s)} P slices compared")
    if yuv:
        res = check_yuv(t, path, yuv)
        rep.check(res["ok"], "decoded frames differ from ffmpeg's (see the report above)")
    rep.finish()


# ================================================================ self-test
def compare_quiet(t, text):
    path = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    try:
        path.write(text)
        path.close()
        gs, gd = parse_log(path.name)
    finally:
        os.unlink(path.name)
    rep = Report("x")
    es, ed = expected_dicts(t.slice_rows, t.dpb_rows)
    compare(rep, es, ed, gs, gd)
    return rep.failures


def self_test():
    ok_all = True

    def verdict(label, ok, detail=""):
        nonlocal ok_all
        ok_all &= ok
        print(f"  {'ok  ' if ok else 'BAD '} {label}{(': ' + detail) if detail else ''}")
    with tempfile.TemporaryDirectory() as td:
        inputs = {}
        for n in ("T6_ref16", "T7", "T9_spatial"):
            inputs[n] = testfiles.get(n)
        for n in VARIANTS:
            inputs[n] = os.path.join(td, n + ".ts")
            make_variant(n, inputs[n])
        for n in SYNTH:
            inputs["synth_" + n] = os.path.join(td, f"synth_{n}.264")
            make_synth(n, inputs["synth_" + n])
        truths = {}
        print("reference validated against ffmpeg:")
        for name, p in inputs.items():
            sub = os.path.join(td, "w_" + name)
            os.makedirs(sub)
            t = truths[name] = build_truth(p, sub)
            p1, p2, stats = validate(t)
            verdict(name, not p1 and not p2, (p1 + p2 + [""])[0] or describe(t, stats)[:170])
        print("known-good reader output must PASS:")
        for name, t in truths.items():
            fails = compare_quiet(t, render(t.slice_rows, t.dpb_rows))
            verdict(name, not fails, fails[0] if fails else f"{len(t.pics)} pictures, {len(t.slice_rows)} P slices")
        print("text corruptions of the output must FAIL (or be refused with exit 2):")
        t = truths["synth_all"]
        good = render(t.slice_rows, t.dpb_rows).splitlines()

        def corrupt(label, fn):
            lines = fn(list(good))
            fails = compare_quiet(t, "\n".join(lines) + "\n")
            verdict(label, bool(fails), fails[0][:110] if fails else "NOT DETECTED")
        corrupt("drop one SLICE line", lambda L: [x for i, x in enumerate(L) if i != next(j for j, y in enumerate(L) if y.startswith("SLICE 20 "))])
        corrupt("drop one DPB line", lambda L: [x for x in L if not x.startswith("DPB 7 ")])
        def swap_first_distinct(L):
            out, done = [], False
            for x in L:
                m = re.search(r"list0=(\S+?),(\S+?)(,|$)", x)
                if not done and x.startswith("SLICE ") and m and m.group(1) != m.group(2) and "-" not in (m.group(1), m.group(2)):
                    x, done = x[:m.start()] + f"list0={m.group(2)},{m.group(1)}{m.group(3)}" + x[m.end():], True
                out.append(x)
            return out
        corrupt("swap two list entries", swap_first_distinct)
        corrupt("turn a long-term slot into a short-term one", lambda L: [x.replace(":L", ":", 1) if ":L" in x else x for x in L])
        corrupt("change maxlt", lambda L: [re.sub(r"maxlt=\S+", "maxlt=7", x) if x.startswith("DPB 25 ") else x for x in L])
        corrupt("renumber pictures from 1", lambda L: [re.sub(r"^(SLICE|DPB) (\d+)", lambda m: f"{m.group(1)} {int(m.group(2)) + 1}", x) for x in L])
        r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["T6_ref16"], "/nonexistent/log.txt"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        verdict("missing reader output file -> exit 2", r.returncode == 2, f"exit {r.returncode}")
        bad = os.path.join(td, "bad.txt")
        open(bad, "w").write("SLICE 1 0 garbage\n")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["T6_ref16"], bad], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300)
        verdict("malformed reader output -> exit 2", r.returncode == 2, f"exit {r.returncode}")
        good_file = os.path.join(td, "good.txt")
        open(good_file, "w").write(render(truths["synth_lt"].slice_rows, truths["synth_lt"].dpb_rows))
        for extra, label, want in (([], "command line on synth_lt with the good log -> exit 0", 0),
                                   (["--yuv", os.path.join(td, "nope.yuv")], "missing --yuv file -> exit 2", 2)):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["synth_lt"], good_file, *extra],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
            verdict(label, r.returncode == want, f"exit {r.returncode}")
        # --yuv with ffmpeg's own decode (exit 0) and a corrupted copy (exit 1)
        ysrc = os.path.join(td, "good.yuv")
        ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-i", inputs["synth_lt"], "-fps_mode", "passthrough", "-f", "rawvideo",
                   "-pix_fmt", "yuv420p", ysrc], timeout=120)
        data = bytearray(open(ysrc, "rb").read())
        bad_y = os.path.join(td, "bad.yuv")
        data[len(data) // 2] ^= 0x55
        open(bad_y, "wb").write(data)
        for yf, want, label in ((ysrc, 0, "--yuv with the exact frames -> exit 0"), (bad_y, 1, "--yuv with one wrong sample -> exit 1")):
            r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["synth_lt"], good_file, "--yuv", yf],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
            verdict(label, r.returncode == want, f"exit {r.returncode}")
        print("wrong rules (mutants of the reference): each must make the reader comparison FAIL on some input;")
        print("  'ffmpeg' lists which of ffmpeg's own checks (marking log / pixels+msgs) would also have exposed the wrong rule:")
        for mname, desc in R.MUTANTS.items():
            caught, ff_dpb, ff_pix, rejected = [], [], [], []
            for name, t in truths.items():
                try:
                    s_rows, d_rows, _, posts = R.simulate(t.pics, *t.params, mutant=mname)
                except R.StreamError:
                    caught.append(name)              # the wrong rule makes a valid stream look non-conforming
                    rejected.append(name)
                    continue
                es, ed = expected_dicts(s_rows, d_rows)
                rep = Report("x")
                gs, gd = expected_dicts(t.slice_rows, t.dpb_rows)
                compare(rep, gs, gd, es, ed)
                if rep.failures:
                    caught.append(name)
                p1, p2, _ = validate(t, (s_rows, posts))
                if p1:
                    ff_dpb.append(name)
                if p2:
                    ff_pix.append(name)
            ffs = "+".join(x for x, v in (("marking-log", ff_dpb), ("pixels+msgs", ff_pix)) if v) or "nothing"
            verdict(f"{mname:15s} {desc[:60]}", bool(caught),
                    f"caught on {len(caught)}/{len(truths)} inputs{' (' + str(len(rejected)) + ' as non-conforming)' if rejected else ''}; ffmpeg: {ffs}")
    print("SELF-TEST", "PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="H.264 MPEG-TS (or Annex B .264) file")
    ap.add_argument("output", nargs="?", help="your reader's log file (with --print-expected: where to write the expected log)")
    ap.add_argument("--yuv", metavar="FILE", help="your decoded frames (raw yuv420p, cropped) to compare with ffmpeg's decode")
    ap.add_argument("--print-expected", action="store_true")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    ffref.require_tools()
    if a.self_test:
        self_test()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if not a.input or not a.output:
        ap.error("need <input> and <output>")
    if not os.path.exists(a.input):
        die(f"input not found: {a.input}")
    if a.print_expected:
        with tempfile.TemporaryDirectory() as td:
            t = build_truth(a.input, td)
        open(a.output, "w").write(render(t.slice_rows, t.dpb_rows))
        print("wrote", a.output)
        return
    run_check(a.input, a.output, a.yuv)


if __name__ == "__main__":
    main()
