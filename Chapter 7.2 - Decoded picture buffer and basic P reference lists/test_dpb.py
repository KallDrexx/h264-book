#!/usr/bin/env python3
"""Harness for Chapter 7.2 (decoded picture buffer, frame_num / PicNum, sliding window, default P reference list).

    python3 test_dpb.py <input.ts|input.264> <dpb_log.txt>        # check your reader's output
    python3 test_dpb.py --print-expected <input> <out.txt>        # write the expected log, to diff against yours
    python3 test_dpb.py --make-variants <out_dir>                 # extra test inputs (see Readme)
    python3 test_dpb.py --self-test

<dpb_log.txt> is your program's output (grammar in the Readme, section 5), two kinds of lines:

    SLICE <pic> <slice> frame_num=<n> active=<n> list0=<pic>:<PicNum>,<pic>:<PicNum>,...      (one per P slice; "-" = no reference picture)
    DPB <pic> refs=<pic>:<frame_num>,...                                                       (one per picture, after its marking process)

<pic> is the 0-based DECODING-order index of a coded picture (Chapter 3.4's picture grouping).

Truth.  The harness runs its own implementation of 8.2.4.1 / 8.2.5.3 / 8.2.4.2.1 (spoiler_reference/dpb_ref.py) on the slice-header
values that ffmpeg's trace_headers prints.  That implementation is validated before your output is compared; a failure there is a
harness problem (exit 2), not yours:
  1. ffmpeg `-debug mmco` prints "short term list:" after the marking process of every reference picture.  The frame_num sequence
     of the reference's DPB state must equal it for every picture with nal_ref_idc != 0 (ffmpeg confirms the DPB: sliding window,
     IDR, non-reference pictures, wrap-around).
  2. Synthetic streams (spoiler_reference/dpbgen.py): the decoded pixels reveal the list.  Every P_L0_16x16 macroblock has mvd 0 and
     no residual, so it is an exact copy of the picture its ref_idx names; ffmpeg's decoded frames must show that the picture is
     list0[ref_idx] of the reference.  ffmpeg also reports every "no reference picture" list entry ("Missing reference picture").
     ffmpeg does NOT print the list of real files, so for those the list order rests on the reference + (1).
Only frame pictures, short-term references, sliding-window marking and no list modification are supported (Chapter 7.6 adds the rest);
a file that uses MMCO, long-term references, frame_num gaps, ref_pic_list_modification or field pictures is refused with exit 2.
(T7 and T8 are refused: x264's weighted P prediction duplicates references with ref_pic_list_modification.  Use T5, T6, --make-variants.)
B slices are not logged (only P); B pictures still exist as pictures and still go through marking.
Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output, unsupported or unusable input, or reference validation failure.
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
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import dpb_ref  # noqa: E402  (spoiler_reference/dpb_ref.py: the reference model)
import dpbgen  # noqa: E402  (spoiler_reference/dpbgen.py: the synthetic-stream generator)

SRC = "testsrc2=size=352x288:rate=25"
# x264 variants: name -> (codec options, rate control)
VARIANTS = {
    "x264_ref16": ("-profile:v baseline -bf 0 -x264-params keyint=100:partitions=all:ref=16", ["-qp", "26"]),
    "x264_idr10": ("-profile:v baseline -bf 0 -x264-params keyint=10:ref=4", ["-qp", "26"]),
    "x264_ref1_slices": ("-profile:v baseline -bf 0 -x264-params keyint=100:ref=1:slices=3", ["-qp", "26"]),
    "x264_nonref_b": ("-profile:v main -x264-params cabac=0:bframes=2:b-pyramid=none:weightp=0:ref=3:keyint=100", ["-qp", "26"]),
}
SYNTH = list(dpbgen.SCENARIOS)
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
    opts, rc = VARIANTS[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC, "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
           *rc, *opts.split(), "-f", "mpegts", out_ts]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die(f"ffmpeg failed for {name}: {r.stderr.decode()[-500:]}")


def manifest_path(p):
    return (p[:-4] if p.lower().endswith(".264") else p) + ".manifest.json"


def make_synth(name, out_264):
    data, man = dpbgen.make_stream(name)
    open(out_264, "wb").write(data)
    json.dump(man, open(manifest_path(out_264), "w"))


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for t in ("T5", "T6"):
        shutil.copy(testfiles.get(t), os.path.join(out_dir, t + ".ts"))
        print("wrote", os.path.join(out_dir, t + ".ts"))
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for n in SYNTH:
        p = os.path.join(out_dir, f"synth_dpb_{n}.264")
        make_synth(n, p)
        print("wrote", p, "and", manifest_path(p))


# ================================================================ slice headers from ffmpeg trace_headers (as in Chapter 3.4)
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
            s.names = {n for n, _ in b.fields}
            s.mmco = any(n == "memory_management_control_operation" for n, _ in b.fields)
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
    """-> list of dpb_ref.Pic (decoding order).  Refuses (exit 2) what Chapter 7.2 does not cover."""
    groups, prev = [], None
    for s in slices:
        if s.d.get("redundant_pic_cnt", 0) > 0:
            continue
        if prev is None or boundary(prev, s):
            groups.append([s])
        else:
            groups[-1].append(s)
        prev = s
    pics, prev_ref_fn = [], 0
    for i, g in enumerate(groups):
        f = g[0]
        sps = f.sps
        log2 = sps["log2_max_frame_num_minus4"] + 4
        fn = f.d["frame_num"]
        def refuse(why):
            die(f"unsupported input: picture {i}: {why} (that is Chapter 7.6 / Phase 10)")
        for s in g:
            if s.d.get("field_pic_flag", 0):
                refuse("field picture (field_pic_flag = 1)")
            if s.mmco or s.d.get("adaptive_ref_pic_marking_mode_flag", 0):
                refuse("adaptive reference picture marking (MMCO)")
            if s.d.get("long_term_reference_flag", 0):
                refuse("long_term_reference_flag = 1")
            if s.d["slice_type"] % 5 == 0 and s.d.get("ref_pic_list_modification_flag_l0", 0):
                refuse("ref_pic_list_modification in a P slice")
            if (s.d["slice_type"] % 5) in (3, 4):
                refuse("SP/SI slice")
        idr = f.nal_type == 5
        if not idr and fn != prev_ref_fn and fn != (prev_ref_fn + 1) % (1 << log2):
            refuse(f"frame_num {fn} after reference frame_num {prev_ref_fn}: a gap in frame_num")
        p = dpb_ref.Pic()
        p.idx, p.idr, p.ref_idc, p.frame_num = i, idr, f.ref_idc, fn
        p.log2_max_frame_num, p.max_num_ref_frames = log2, sps["max_num_ref_frames"]
        p.slices = []
        for s in g:
            t = s.d["slice_type"] % 5
            typ = {0: 0, 1: 1, 2: 2}[t]
            act = 0
            if typ == 0:
                act = (s.d["num_ref_idx_l0_active_minus1"] if s.d.get("num_ref_idx_active_override_flag", 0)
                       else s.pps["num_ref_idx_l0_default_active_minus1"]) + 1
            p.slices.append(dict(type=typ, active=act))
        pics.append(p)
        if f.ref_idc:
            prev_ref_fn = fn
    return pics


# ================================================================ ffmpeg: -debug mmco
def ffmpeg_posts(path, td):
    """frame_num lists (newest first) of the short-term reference set after the marking process of every reference
    picture, as ffmpeg's `-debug mmco` prints them.  -> (list of lists, number of long-term entries seen, number of
    "Missing reference picture" messages of the real decode).
    ffmpeg's log is written by two threads and now and then loses or merges a line; build_truth() reruns until it is consistent."""
    log = os.path.join(td, "mmco.log")
    cmd = ["ffmpeg", "-hide_banner", "-threads", "1", "-loglevel", "repeat+debug", "-debug", "mmco"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-f", "null", "-"]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f, timeout=300, check=False)
    pre = re.compile(r"^\[([^\]]*)\]\s?")
    text = open(log, errors="replace").read().splitlines()
    # ffmpeg decodes a few frames with a first decoder instance while probing the stream; the real decode is the instance
    # (pointer in "[h264 @ 0x...]") that printed the LAST "Reinit context" line.
    inst = None
    for raw in text:
        if "Reinit context" in raw:
            inst = raw[:raw.index("]") + 1]
    posts, cur, mode, marking, nlong, nmiss = [], [], None, False, 0, 0
    ent = re.compile(r"^\d+ fn:(\d+) poc:-?\d+")
    for raw in text:
        if inst and not raw.startswith(inst):
            continue
        line = pre.sub("", raw)
        if MISSING in line:
            nmiss += 1
        if line.startswith("no mmco here") or line.startswith("mmco:"):
            marking = True
        elif line == "short term list:":
            cur, mode = [], "short"
        elif line == "long term list:":
            if mode == "short" and marking:
                posts.append(cur)
                marking = False
            mode = "long"
        else:
            m = ent.match(line)
            if m and mode == "short":
                cur.append(int(m.group(1)))
            elif m and mode == "long":
                nlong += 1
    return posts, nlong, nmiss


def check_dpb_vs_ffmpeg(dpb_rows, pics, posts):
    ref_pics = [p for p in pics if p.ref_idc]
    if len(posts) != len(ref_pics):
        return [f"ffmpeg's -debug mmco log has {len(posts)} post-marking lists but the stream has {len(ref_pics)} reference "
                f"pictures"]
    rows = dict(dpb_rows)
    problems = []
    for p, post in zip(ref_pics, posts):
        mine = [fn for _, fn in rows[p.idx]]
        if mine != post:
            problems.append(f"picture {p.idx}: reference DPB frame_nums {mine} but ffmpeg's short term list is {post}")
    return problems


# ================================================================ ffmpeg: pixels of synthetic streams
def decode_synth(path, manifest, td):
    """-> (problems, frames).  Every ffmpeg message except 'Missing reference picture' is a problem."""
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
    """-> (problems, stats).  Every inter macroblock must equal the picture list0[ref_idx] names."""
    wm, hm = manifest["width_mbs"], manifest["height_mbs"]
    rows = {(r["pic"], r["slice"]): r for r in slice_rows}
    problems, ncopy, ndisc, npcm, nnone = [], 0, 0, 0, 0
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
                if ent is None:
                    problems.append(f"picture {pi} slice {si} MB {e['addr']}: ref_idx {e['ref']} points at 'no reference picture' "
                                    f"in the reference list")
                    continue
                got = mb_pixels(frames[pi], wm, hm, e["addr"])
                exp = mb_pixels(frames[ent[0]], wm, hm, e["addr"])
                ncopy += 1
                if got != exp:
                    problems.append(f"picture {pi} slice {si} MB {e['addr']} ref_idx {e['ref']}: decoded pixels are not a copy of "
                                    f"picture {ent[0]} (the reference's list0[{e['ref']}])")
                others = [mb_pixels(frames[x[0]], wm, hm, e["addr"]) for x in row["entries"] if x is not None and x[0] != ent[0]]
                ndisc += bool(others) and all(o != exp for o in others)
    for r in slice_rows:
        nnone += sum(1 for x in r["entries"] if x is None)
    if nmiss != nnone:
        problems.append(f"ffmpeg printed 'Missing reference picture' {nmiss} times; the reference has {nnone} 'no reference "
                        f"picture' list entries")
    return problems, dict(ncopy=ncopy, ndisc=ndisc, npcm=npcm, nnone=nnone)


# ================================================================ truth for one input
class Truth:
    pass


def build_truth(path, td, quiet=True):
    t = Truth()
    t.path = path
    t.pics = build_pictures(load_slices(path, td))
    t.slice_rows, t.dpb_rows = dpb_ref.simulate(t.pics)
    t.manifest = t.frames = None
    t.nmiss = 0
    mp = manifest_path(path)
    if path.lower().endswith(".264") and os.path.exists(mp):
        t.manifest = json.load(open(mp))
        probs, t.frames = decode_synth(path, t.manifest, td)
        if probs:
            die("HARNESS PROBLEM: " + probs[0])
        if len(t.manifest["pictures"]) != len(t.pics):
            die("HARNESS PROBLEM: the manifest and the stream disagree about the number of pictures")
    for attempt in range(5):
        t.posts, t.nlong, t.nmiss = ffmpeg_posts(path, td)
        p1, p2, _ = validate(t)
        if not p1 and not p2:
            break
    return t


def validate(t, slice_rows=None, dpb_rows=None):
    """Problems found when the reference rows are checked against ffmpeg; also returns a description."""
    slice_rows = slice_rows if slice_rows is not None else t.slice_rows
    dpb_rows = dpb_rows if dpb_rows is not None else t.dpb_rows
    p1 = check_dpb_vs_ffmpeg(dpb_rows, t.pics, t.posts)
    p2, stats = [], None
    if t.manifest is not None:
        p2, stats = check_pixels(slice_rows, t.manifest, t.frames, t.nmiss)
    return p1, p2, stats


def describe(t, stats):
    nref = sum(1 for p in t.pics if p.ref_idc)
    s = (f"ffmpeg -debug mmco: the DPB frame_nums after marking agree for all {nref} reference pictures "
         f"({len(t.pics) - nref} non-reference picture(s) never appear in it)")
    if stats:
        s += (f"; synthetic pixels: {stats['ncopy']} inter macroblocks are exact copies of list0[ref_idx] "
              f"({stats['ndisc']} of them tell it apart from every other list entry), {stats['npcm']} I_PCM exact, "
              f"{stats['nnone']} 'no reference picture' entries confirmed by ffmpeg's messages")
    return s


# ================================================================ rendering / parsing the log
def fmt_entry(e):
    return "-" if e is None else f"{e[0]}:{e[1]}"


def render(slice_rows, dpb_rows):
    lines = []
    by_pic = {}
    for r in slice_rows:
        by_pic.setdefault(r["pic"], []).append(r)
    for pic, refs in dpb_rows:
        for r in by_pic.get(pic, []):
            lines.append(f"SLICE {pic} {r['slice']} frame_num={r['frame_num']} active={r['active']} "
                         f"list0={','.join(fmt_entry(e) for e in r['entries']) or '-'}")
        lines.append(f"DPB {pic} refs={','.join(f'{a}:{b}' for a, b in refs) if refs else '-'}")
    return "\n".join(lines) + "\n"


def parse_entry(tok, ln):
    if tok == "-":
        return None
    m = re.fullmatch(r"(\d+):(-?\d+)", tok)
    if not m:
        die(f"line {ln}: bad list entry {tok!r} (expected <pic>:<number> or -)")
    return (int(m.group(1)), int(m.group(2)))


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
                if len(tok) != 6:
                    die(f"line {ln}: SLICE needs 6 fields (SLICE pic slice frame_num= active= list0=), got {len(tok)}: {line}")
                kv = {}
                for t_ in tok[3:]:
                    k, _, v = t_.partition("=")
                    kv[k] = v
                if set(kv) != {"frame_num", "active", "list0"}:
                    die(f"line {ln}: SLICE keys must be frame_num, active, list0: {line}")
                key = (int(tok[1]), int(tok[2]))
                if key in slices:
                    die(f"line {ln}: duplicate SLICE {key[0]} {key[1]}")
                slices[key] = dict(frame_num=int(kv["frame_num"]), active=int(kv["active"]),
                                   entries=[parse_entry(x, ln) for x in kv["list0"].split(",")])
            elif tok[0] == "DPB":
                if len(tok) != 3 or not tok[2].startswith("refs="):
                    die(f"line {ln}: DPB needs 3 fields (DPB pic refs=...): {line}")
                v = tok[2][5:]
                refs = [] if v == "-" else [parse_entry(x, ln) for x in v.split(",")]
                if any(r is None for r in refs):
                    die(f"line {ln}: '-' is not allowed in a DPB line")
                if int(tok[1]) in dpb:
                    die(f"line {ln}: duplicate DPB {tok[1]}")
                dpb[int(tok[1])] = refs
            else:
                die(f"line {ln}: unknown line type {tok[0]!r}: {line}")
        except ValueError:
            die(f"line {ln}: malformed numbers: {line}")
    if not slices and not dpb:
        die("reader output is empty")
    return slices, dpb


def hint_list(exp, got):
    ep = [e[0] for e in exp if e]
    gp = [e[0] for e in got if e]
    if ep and gp == ep[::-1]:
        return " (hint: same pictures in the opposite order: ascending instead of descending PicNum?)"
    if ep == gp and [e for e in exp if e] != [e for e in got if e]:
        return " (hint: right pictures but wrong PicNum: FrameNumWrap / MaxFrameNum?)"
    if ep[:len(gp)] == gp[:len(ep)] and len(ep) != len(gp):
        return " (hint: list length differs: truncation to num_ref_idx_active, or missing entries not marked '-'?)"
    if set(gp) - set(ep) or set(ep) - set(gp):
        return " (hint: different pictures: wrong DPB content, or the marking was applied at the wrong time)"
    if sorted(ep) == sorted(gp):
        return (" (hint: same pictures, different order: sort by descending PicNum, where PicNum = FrameNumWrap; "
                "without the wrap a reference decoded before frame_num restarted sorts too early)")
    return ""


def hint_dpb(e, g):
    if sorted(e) == sorted(g):
        return " (same members, different order: the log must list the newest decoded picture first)"
    if len(g) > len(e):
        return " (too many references: sliding window not applied / IDR or non-reference picture stored?)"
    if len(g) < len(e):
        return " (too few references: sliding window too early, or an IDR/reference picture not stored?)"
    gone = [x for x in e if x not in g]
    kept = [x for x in g if x not in e]
    if gone and kept:
        return (f" (a different picture was removed: you dropped {','.join(f'{a}:{b}' for a, b in gone)} but should have dropped "
                f"{','.join(f'{a}:{b}' for a, b in kept)}; the sliding window removes the smallest FrameNumWrap (the oldest "
                f"reference), computed relative to the current frame_num, not the smallest raw frame_num)")
    return ""


def compare(rep, exp_slices, exp_dpb, got_slices, got_dpb):
    """Checks in decoding order (each picture's SLICE lines, then its DPB line), so the first failure shown is
    the first thing that went wrong."""
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
                diff = next((i for i in range(max(len(e['entries']), len(g['entries'])))
                             if (e['entries'][i] if i < len(e['entries']) else 'none') !=
                             (g['entries'][i] if i < len(g['entries']) else 'none')), 0)
                rep.check(False, f"{lab} list0: expected {','.join(map(fmt_entry, e['entries']))} got "
                                 f"{','.join(map(fmt_entry, g['entries']))} (first difference at ref_idx {diff})"
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
        rep.check(e == g, f"DPB {pic}: expected {','.join(f'{a}:{b}' for a, b in e) or '-'} got "
                          f"{','.join(f'{a}:{b}' for a, b in g) or '-'}{hint_dpb(e, g)}")


def expected_dicts(t):
    return ({(r["pic"], r["slice"]): dict(frame_num=r["frame_num"], active=r["active"], entries=r["entries"])
             for r in t.slice_rows}, {pic: refs for pic, refs in t.dpb_rows})


def trust(t, quiet=False):
    p1, p2, stats = validate(t)
    if p1 or p2:
        print("HARNESS PROBLEM (the reference model disagrees with ffmpeg; this is not your bug):")
        for p in (p1 + p2)[:10]:
            print("  ", p)
        sys.exit(2)
    if not quiet:
        print(" ", describe(t, stats))


def run_check(path, log, quiet=False):
    with tempfile.TemporaryDirectory() as td:
        t = build_truth(path, td)
        trust(t, quiet)
    got_s, got_d = parse_log(log)
    exp_s, exp_d = expected_dicts(t)
    rep = Report(f"decoded picture buffer and P reference lists of {os.path.basename(path)}")
    compare(rep, exp_s, exp_d, got_s, got_d)
    print(f"  {len(exp_d)} pictures, {len(exp_s)} P slices compared")
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
    es, ed = expected_dicts(t)
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
        for n in ("T5", "T6"):
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
            verdict(name, not p1 and not p2, (p1 + p2 + [""])[0] or describe(t, stats)[:150])
        print("known-good reader output must PASS:")
        for name, t in truths.items():
            fails = compare_quiet(t, render(t.slice_rows, t.dpb_rows))
            verdict(name, not fails, fails[0] if fails else f"{len(t.pics)} pictures, {len(t.slice_rows)} P slices")
        print("text corruptions of the output must FAIL (or be refused with exit 2):")
        t = truths["T6"]
        good = render(t.slice_rows, t.dpb_rows).splitlines()

        def corrupt(label, fn):
            lines = fn(list(good))
            fails = compare_quiet(t, "\n".join(lines) + "\n")
            verdict(label, bool(fails), fails[0][:110] if fails else "NOT DETECTED")
        corrupt("drop one SLICE line", lambda L: [x for i, x in enumerate(L) if i != next(j for j, y in enumerate(L) if y.startswith("SLICE 20 "))])
        corrupt("drop one DPB line", lambda L: [x for x in L if not x.startswith("DPB 7 ")])
        corrupt("swap two list entries", lambda L: [re.sub(r"list0=(\S+?),(\S+?)(,|$)", r"list0=\2,\1\3", x, count=1) if x.startswith("SLICE 30 ") else x for x in L])
        corrupt("change a PicNum", lambda L: [re.sub(r":(-?\d+)", lambda m: ":" + str(int(m.group(1)) + 1), x, count=1) if x.startswith("SLICE 25 ") else x for x in L])
        corrupt("renumber pictures from 1", lambda L: [re.sub(r"^(SLICE|DPB) (\d+)", lambda m: f"{m.group(1)} {int(m.group(2)) + 1}", x) for x in L])
        r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["T5"], "/nonexistent/log.txt"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        verdict("missing reader output file -> exit 2", r.returncode == 2, f"exit {r.returncode}")
        bad = os.path.join(td, "bad.txt")
        open(bad, "w").write("SLICE 1 0 garbage\n")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["T5"], bad], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300)
        verdict("malformed reader output -> exit 2", r.returncode == 2, f"exit {r.returncode}")
        good_file = os.path.join(td, "good.txt")
        open(good_file, "w").write(render(truths["T6"].slice_rows, truths["T6"].dpb_rows))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), inputs["T6"], good_file], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300)
        verdict("command line on T6 with the good log -> exit 0", r.returncode == 0, f"exit {r.returncode}")
        print("wrong rules (mutants of the reference): each must make the reader comparison FAIL on some input;")
        print("  'ffmpeg' lists which of ffmpeg's own checks (DPB log / pixels) would also have exposed the wrong rule:")
        for mname, desc in dpb_ref.MUTANTS.items():
            caught, ff_dpb, ff_pix = [], [], []
            for name, t in truths.items():
                s_rows, d_rows = dpb_ref.simulate(t.pics, mname)
                if compare_quiet(t, render(s_rows, d_rows)):
                    caught.append(name)
                p1, p2, _ = validate(t, s_rows, d_rows)
                if p1:
                    ff_dpb.append(name)
                if p2:
                    ff_pix.append(name)
            ffs = "+".join(x for x, v in (("DPB-log", ff_dpb), ("pixels+msgs", ff_pix)) if v) or "nothing"
            verdict(f"{mname:15s} {desc[:62]}", bool(caught), f"caught on {len(caught)}/{len(truths)} inputs; ffmpeg: {ffs}")
    print("SELF-TEST", "PASS" if ok_all else "FAIL")
    sys.exit(0 if ok_all else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="H.264 MPEG-TS (or Annex B .264) file")
    ap.add_argument("output", nargs="?", help="your reader's log file (with --print-expected: where to write the expected log)")
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
    run_check(a.input, a.output)


if __name__ == "__main__":
    main()
