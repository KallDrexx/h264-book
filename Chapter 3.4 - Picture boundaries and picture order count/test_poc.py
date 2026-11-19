#!/usr/bin/env python3
"""Harness for Chapter 3.4 (picture boundaries and picture order count).

    python3 test_poc.py <input.ts|input.264> <pictures.txt>
    python3 test_poc.py --make-variants <out_dir>      # write extra test inputs (see Readme)
    python3 test_poc.py --self-test

<input> is any ffmpeg-produced H.264 MPEG-TS file or a raw Annex B .264 file (progressive or MBAFF frames;
streams with field pictures, field_pic_flag = 1, are refused with exit 2: that is Phase 10).
<pictures.txt> is your program's output (grammar in the Readme, section 5):

    PIC <index> <idr> <nal_ref_idc> <type> <num_slices> <frame_num> <poc> <top> <bottom> <mmco5> <first_nal>
    ...one line per coded picture, DECODE order...
    DISPLAY <display_index> <pic_index> <poc> <type>
    ...one line per picture, DISPLAY order (sorted by POC inside each IDR / MMCO5 period)...

Truth (all derived from the input itself):
  * ffmpeg `-bsf:v trace_headers`: the slice headers.  The harness runs its OWN implementation of picture
    boundary detection (7.4.1.2.4 and 7.4.1.2.3) and of PicOrderCnt (8.2.1) on those fields.
  * the raw NAL unit sequence (own start code scanner): NAL indexes and the non-VCL NAL units.
  * ffprobe -show_frames / -show_packets: picture count, pict_type in display order, and PTS ordering
    (not used for synthetic inputs, which have no picture data; those come with a `<input>.expected` file
    written by the generator, independent of this harness's implementation).

Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output or unusable input.
"""
import argparse
import os
import random
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import pocgen  # noqa: E402

FIELD_RE = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
PREFIX_RE = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header")
NON_VCL_HINT = {6, 7, 8, 9, 14, 15, 16, 17, 18}     # 7.4.1.2.3: these start a new access unit after a VCL NAL
SLICE_NAMES = ["P", "B", "I", "SP", "SI"]
PIC_COLS = ["index", "idr", "nal_ref_idc", "type", "num_slices", "frame_num", "poc", "top", "bottom", "mmco5",
            "first_nal"]


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ================================================================ trace parsing (as in Chapter 3.3)
class Block:
    def __init__(self, title, extradata):
        self.title, self.extradata = title, extradata
        self.fields = []


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


# ================================================================ the raw stream
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


# ================================================================ truth: slices
class Slice:
    pass


def load_slices(path, td):
    """-> list of Slice (types 1 and 5, stream order), each with fields d, nal_type, ref_idc, sps, pps,
    mmco5, nal_index, hint (a NAL unit of 7.4.1.2.3 came between the previous slice and this one)."""
    tr = os.path.join(td, "trace.txt")
    rc = ffref.trace_headers(path, tr)
    errs = [PREFIX_RE.sub("", l.strip()) for l in open(tr, errors="replace")
            if "Failed to read unit" in l or "not available" in l or "but must be in" in l]
    if errs:
        die(f"ffmpeg trace_headers could not parse all of {path} (exit {rc}), so there is no complete truth:\n  "
            + "\n  ".join(errs[:5]))
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
            s.d, s.fields = d, b.fields
            s.nal_type, s.ref_idc = d["nal_unit_type"], d["nal_ref_idc"]
            s.pps = pps_t[pid]
            s.sps = sps_t[s.pps["seq_parameter_set_id"]]
            s.mmco5 = any(n == "memory_management_control_operation" and v == 5 for n, v in b.fields)
            out.append(s)
    if len(out) != len(sl_nals):
        die(f"internal error: {len(sl_nals)} slice NAL units in the stream but {len(out)} slice headers in the trace")
    prev = -1
    for s, i in zip(out, sl_nals):
        s.nal_index = i
        s.hint = any(t in NON_VCL_HINT for t in types[prev + 1:i])
        prev = i
    if not out:
        die(f"no slice found in {path}")
    return out


# ================================================================ truth: picture boundaries (7.4.1.2.4)
def boundary_reasons(a, b):
    """Why slice b starts a new picture compared with the previous slice a ([] = same picture)."""
    da, db = a.d, b.d
    r = []

    def diff(name, x, y):
        if x != y:
            r.append(f"{name} {x} -> {y}")

    diff("frame_num", da["frame_num"], db["frame_num"])
    diff("pic_parameter_set_id", da["pic_parameter_set_id"], db["pic_parameter_set_id"])
    diff("field_pic_flag", da.get("field_pic_flag", 0), db.get("field_pic_flag", 0))
    diff("bottom_field_flag", da.get("bottom_field_flag", 0), db.get("bottom_field_flag", 0))
    if (a.ref_idc == 0) != (b.ref_idc == 0):
        r.append(f"nal_ref_idc {a.ref_idc} -> {b.ref_idc} (one is 0)")
    poc_type = b.sps["pic_order_cnt_type"]
    if poc_type == 0:
        diff("pic_order_cnt_lsb", da.get("pic_order_cnt_lsb", 0), db.get("pic_order_cnt_lsb", 0))
        diff("delta_pic_order_cnt_bottom", da.get("delta_pic_order_cnt_bottom", 0),
             db.get("delta_pic_order_cnt_bottom", 0))
    elif poc_type == 1:
        diff("delta_pic_order_cnt[0]", da.get("delta_pic_order_cnt[0]", 0), db.get("delta_pic_order_cnt[0]", 0))
        diff("delta_pic_order_cnt[1]", da.get("delta_pic_order_cnt[1]", 0), db.get("delta_pic_order_cnt[1]", 0))
    diff("IdrPicFlag", int(a.nal_type == 5), int(b.nal_type == 5))
    if a.nal_type == 5 and b.nal_type == 5:
        diff("idr_pic_id", da["idr_pic_id"], db["idr_pic_id"])
    return r


class Pic:
    pass


def group_pictures(slices):
    pics, prev = [], None
    for s in slices:
        if s.d.get("redundant_pic_cnt", 0) > 0:
            continue                         # redundant coded picture: not part of the primary picture
        reasons = [] if prev is None else boundary_reasons(prev, s)
        if prev is None or reasons or s.hint:
            p = Pic()
            p.slices = [s]
            p.reason = ("first slice of the stream" if prev is None else
                        "; ".join(reasons) if reasons else "a non-VCL NAL unit (AUD/SPS/PPS/SEI) precedes it")
            pics.append(p)
        else:
            pics[-1].slices.append(s)
        prev = s
    return pics


# ================================================================ truth: POC (8.2.1), frames only
def compute_poc(pics):
    st = dict(msb=0, lsb=0, fno=0, fn=0)     # state of the previous (reference / any) picture, see below
    for p in pics:
        s = p.slices[0]
        d, sps = s.d, s.sps
        idr = s.nal_type == 5
        if d.get("field_pic_flag", 0):
            die("input problem: field pictures (field_pic_flag = 1) are not supported by this chapter")
        maxfn = 1 << (sps["log2_max_frame_num_minus4"] + 4)
        fn = d["frame_num"]
        typ = sps["pic_order_cnt_type"]
        if typ == 0:
            maxlsb = 1 << (sps["log2_max_pic_order_cnt_lsb_minus4"] + 4)
            pmsb, plsb = (0, 0) if idr else (st["msb"], st["lsb"])
            lsb = d["pic_order_cnt_lsb"]
            if lsb < plsb and plsb - lsb >= maxlsb // 2:
                msb = pmsb + maxlsb
            elif lsb > plsb and lsb - plsb > maxlsb // 2:
                msb = pmsb - maxlsb
            else:
                msb = pmsb
            top = msb + lsb
            bot = top + d.get("delta_pic_order_cnt_bottom", 0)
        else:
            if idr:
                fno = 0
            else:
                fno = st["fno"] + (maxfn if st["fn"] > fn else 0)
            if typ == 1:
                cyc = [sps[f"offset_for_ref_frame[{i}]"] for i in range(sps["num_ref_frames_in_pic_order_cnt_cycle"])]
                absn = fno + fn if cyc else 0
                if s.ref_idc == 0 and absn > 0:
                    absn -= 1
                if absn > 0:
                    cnt, k = divmod(absn - 1, len(cyc))
                    exp = cnt * sum(cyc) + sum(cyc[:k + 1])
                else:
                    exp = 0
                if s.ref_idc == 0:
                    exp += sps["offset_for_non_ref_pic"]
                top = exp + d.get("delta_pic_order_cnt[0]", 0)
                bot = top + sps["offset_for_top_to_bottom_field"] + d.get("delta_pic_order_cnt[1]", 0)
            else:
                tmp = 0 if idr else 2 * (fno + fn) - (1 if s.ref_idc == 0 else 0)
                top = bot = tmp
        p.top, p.bot, p.poc = top, bot, min(top, bot)
        p.mmco5 = int(s.mmco5)
        # state for the next picture ------------------------------------------------------------
        if typ == 0:
            if s.ref_idc != 0:               # only reference pictures feed prevPicOrderCntMsb/Lsb
                if s.mmco5:                  # 8.2.1: after MMCO 5 the picture counts as top = top - min(top, bottom)
                    st["msb"], st["lsb"] = 0, top - min(top, bot)
                else:
                    st["msb"], st["lsb"] = msb, lsb
        else:
            # any picture feeds FrameNumOffset / frame_num; after MMCO 5 both count as 0
            st["fno"], st["fn"] = (0, 0) if s.mmco5 else (fno, fn)
        p.idr = int(idr)
        p.ref_idc = s.ref_idc
        p.type = SLICE_NAMES[s.d["slice_type"] % 5]
        p.nslices = len(p.slices)
        p.frame_num = fn
        p.first_nal = s.nal_index
    return pics


def truth_rows(pics):
    return [dict(index=i, idr=p.idr, nal_ref_idc=p.ref_idc, type=p.type, num_slices=p.nslices,
                 frame_num=p.frame_num, poc=p.poc, top=p.top, bottom=p.bot, mmco5=p.mmco5,
                 first_nal=p.first_nal) for i, p in enumerate(pics)]


def display_order(rows):
    """-> list of (display_index, pic_index, poc_used, type).  A new period starts at every IDR picture and at
    every MMCO 5 picture; inside a period pictures are sorted by POC (an MMCO 5 picture counts as POC 0, its
    value after the reset of 8.2.1); periods are concatenated."""
    periods = []
    for r in rows:
        if r["idr"] or r["mmco5"] or not periods:
            periods.append([])
        periods[-1].append(r)
    out = []
    for per in periods:
        keyed = sorted(per, key=lambda r: (0 if r["mmco5"] else r["poc"], r["index"]))
        for r in keyed:
            out.append((len(out), r["index"], 0 if r["mmco5"] else r["poc"], r["type"]))
    return out


def render(rows):
    out = ["# PIC <index> <idr> <nal_ref_idc> <type> <num_slices> <frame_num> <poc> <top> <bottom> <mmco5> <first_nal>"]
    for r in rows:
        out.append("PIC " + " ".join(str(r[c]) for c in PIC_COLS))
    out.append("# DISPLAY <display_index> <pic_index> <poc> <type>")
    out += ["DISPLAY %d %d %d %s" % t for t in display_order(rows)]
    return "\n".join(out) + "\n"


# ================================================================ ffprobe truth
def ffprobe_truth(path):
    fr = ffref.probe_json(path, "-select_streams", "v:0", "-show_frames", "-show_entries",
                          "frame=pict_type,pts").get("frames", [])
    pk = ffref.probe_json(path, "-select_streams", "v:0", "-show_packets", "-show_entries",
                          "packet=pts,dts").get("packets", [])
    return fr, pk


def num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ================================================================ reader output
def parse_reader(path):
    try:
        text = open(path, "r", errors="replace").read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    pics, disp = [], []
    for ln, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tok = s.split()
        try:
            if tok[0] == "PIC":
                if len(tok) != 12:
                    die(f"{path}:{ln}: PIC line needs 11 values after 'PIC' (index idr nal_ref_idc type num_slices "
                        f"frame_num poc top bottom mmco5 first_nal), got {len(tok) - 1}: {s!r}")
                v = [int(x) if k != 3 else x for k, x in enumerate(tok[1:])]
                if v[3] not in SLICE_NAMES:
                    die(f"{path}:{ln}: type must be one of {SLICE_NAMES}, got {v[3]!r}")
                pics.append(dict(zip(PIC_COLS, v), line=ln))
            elif tok[0] == "DISPLAY":
                if len(tok) != 5:
                    die(f"{path}:{ln}: DISPLAY line needs 4 values (display_index pic_index poc type), got "
                        f"{len(tok) - 1}: {s!r}")
                disp.append((int(tok[1]), int(tok[2]), int(tok[3]), tok[4]))
            else:
                die(f"{path}:{ln}: expected a line starting with PIC or DISPLAY, got {s!r}")
        except ValueError:
            die(f"{path}:{ln}: a value is not an integer: {s!r}")
    if not pics:
        die(f"{path}: no PIC lines found")
    for i, p in enumerate(pics):
        if p["index"] != i:
            die(f"{path}:{p['line']}: PIC index must count 0, 1, 2, ... in order; expected {i}, got {p['index']}")
    return pics, disp


# ================================================================ checking
def pic_ctx(p):
    s = p.slices[0]
    d, sps = s.d, s.sps
    typ = sps["pic_order_cnt_type"]
    bits = [f"pic_order_cnt_type {typ}", f"frame_num {d['frame_num']}", f"nal_unit_type {s.nal_type}",
            f"nal_ref_idc {s.ref_idc}", f"pps {d['pic_parameter_set_id']}"]
    if typ == 0:
        bits.append(f"pic_order_cnt_lsb {d['pic_order_cnt_lsb']}")
        if "delta_pic_order_cnt_bottom" in d:
            bits.append(f"delta_pic_order_cnt_bottom {d['delta_pic_order_cnt_bottom']}")
        bits.append(f"MaxPicOrderCntLsb {1 << (sps['log2_max_pic_order_cnt_lsb_minus4'] + 4)}")
    elif typ == 1:
        bits += [f"{k} {v}" for k, v in d.items() if k.startswith("delta_pic_order_cnt[")]
        bits.append(f"offset_for_non_ref_pic {sps['offset_for_non_ref_pic']}")
        bits.append(f"cycle {[sps[f'offset_for_ref_frame[{i}]'] for i in range(sps['num_ref_frames_in_pic_order_cnt_cycle'])]}")
    bits.append(f"MaxFrameNum {1 << (sps['log2_max_frame_num_minus4'] + 4)}")
    if s.mmco5:
        bits.append("MMCO 5 in this picture")
    return ", ".join(bits)


def check_pictures(rep, exp_rows, pics, got):
    rep.check(len(exp_rows) == len(got),
              f"number of PIC lines: expected {len(exp_rows)} coded pictures, got {len(got)} "
              f"(fewer: you merged pictures or lost slices; more: you split a picture; see the boundary message)")
    # 1. boundaries first: first_nal and num_slices decide whether later columns can be paired
    n = min(len(exp_rows), len(got))
    bad = next((i for i in range(n) if (exp_rows[i]["first_nal"], exp_rows[i]["num_slices"])
                != (got[i]["first_nal"], got[i]["num_slices"])), None)
    for i in range(n if bad is None else bad):
        e, g = exp_rows[i], got[i]
        diffs = [c for c in PIC_COLS[1:] if e[c] != g[c]]
        ok = not diffs
        msg = ""
        if not ok:
            msg = (f"PIC {i} (first slice NAL #{e['first_nal']}): " +
                   "; ".join(f"{c}: expected {e[c]}, got {g[c]}" for c in diffs) +
                   f"\n         header fields of its first slice: {pic_ctx(pics[i]) if pics else '(synthetic)'}")
        rep.check(ok, msg)
    if bad is not None:
        e, g = exp_rows[bad], got[bad]
        head = (f"PIC {bad}: picture boundary differs: expected a picture starting at NAL #{e['first_nal']} with "
                f"{e['num_slices']} slice(s), got one starting at NAL #{g['first_nal']} with {g['num_slices']}")
        hint = ""
        if pics and e["first_nal"] == g["first_nal"] and g["num_slices"] > e["num_slices"] and bad + 1 < len(pics):
            hint = (f". You merged it with the next picture, which starts at NAL #{exp_rows[bad + 1]['first_nal']} "
                    f"because: {pics[bad + 1].reason}")
        elif pics and e["first_nal"] == g["first_nal"] and g["num_slices"] < e["num_slices"]:
            hint = (". You split this picture: its later slices continue it (none of the 7.4.1.2.4 values "
                    "changed and no AUD/SPS/PPS/SEI came between them)")
        elif pics:
            hint = f". The expected picture starts there because: {pics[bad].reason}"
        rep.check(False, head + hint + ". Later pictures cannot be paired and are not compared")


def check_display(rep, exp_disp, got_disp):
    if not got_disp:
        rep.check(False, "no DISPLAY lines: print one per picture, sorted by POC inside each IDR / MMCO 5 period")
        return
    rep.check(len(exp_disp) == len(got_disp),
              f"number of DISPLAY lines: expected {len(exp_disp)}, got {len(got_disp)}")
    for i, (e, g) in enumerate(zip(exp_disp, got_disp)):
        if e != g:
            names = ["display_index", "pic_index", "poc", "type"]
            diffs = [f"{n}: expected {x}, got {y}" for n, x, y in zip(names, e, g) if x != y]
            rep.check(False, f"DISPLAY line {i}: " + "; ".join(diffs) +
                      f" (expected: DISPLAY {' '.join(map(str, e))})")
        else:
            rep.check(True, "")


def run_check(path, dump, title="Picture boundaries and picture order count"):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input not found: {path}")
    got, got_disp = parse_reader(dump)
    with tempfile.TemporaryDirectory() as td:
        slices = load_slices(path, td)
        pics = compute_poc(group_pictures(slices))
        rows = truth_rows(pics)
        disp = display_order(rows)
        sidecar = path + ".expected"
        synthetic = os.path.exists(sidecar)
        if synthetic:
            side = parse_reader(sidecar)
            srows = [{c: p[c] for c in PIC_COLS} for p in side[0]]
            if srows != rows or side[1] != disp:
                die("internal error: the generator's expectation (.expected file) and the harness's own POC "
                    "computation disagree; this is a harness bug, not yours")
        fr, pk = ([], []) if synthetic else ffprobe_truth(path)
    rep = Report(title)
    check_pictures(rep, rows, pics, got)
    check_display(rep, disp, got_disp)
    if not synthetic:
        # ---- ffprobe: count, pict_type in display order, PTS order
        types = [f.get("pict_type") for f in fr]
        mine = [t for _, _, _, t in disp]
        if len(fr) != len(rows) or [t for t in types] != mine:
            die(f"input problem: ffprobe -show_frames ({len(fr)} frames, types {''.join(map(str, types))[:60]}) "
                f"and the harness's own truth ({len(rows)} pictures, types {''.join(mine)[:60]}) disagree on this "
                f"file (e.g. the stream starts with undecodable pictures); no usable truth")
        gtypes = [t for _, _, _, t in got_disp]
        rep.check(len(fr) == len(got), f"picture count: ffprobe -show_frames reports {len(fr)} frames, you "
                                       f"print {len(got)} PIC lines")
        rep.check(gtypes == types,
                  "pict_type sequence in display order differs from ffprobe -show_frames: first difference at "
                  f"display position {next((i for i, (a, b) in enumerate(zip(gtypes, types)) if a != b), min(len(gtypes), len(types)))}"
                  f" (yours {''.join(gtypes)[:50]!r}..., ffprobe {''.join(map(str, types))[:50]!r}...)")
        pts = [num(p.get("pts")) for p in pk]
        if not path.lower().endswith((".264", ".h264")) and pk and all(x is not None for x in pts):
            if len(pk) != len(got):
                rep.check(False, f"packet count: ffprobe -show_packets reports {len(pk)} packets (one per access "
                                 f"unit) but you print {len(got)} pictures")
            else:
                order = [pts[pi] for _, pi, _, _ in got_disp if 0 <= pi < len(pts)]
                bad = next((i for i in range(1, len(order)) if order[i] <= order[i - 1]), None)
                rep.check(bad is None and len(order) == len(pts),
                          "PTS check failed: the PTS of the packets taken in YOUR display order must increase "
                          f"strictly; it does not at display position {bad} "
                          f"(PTS {order[bad - 1] if bad else None} then {order[bad] if bad else None}, pictures "
                          f"{got_disp[bad - 1][1] if bad else None} then {got_disp[bad][1] if bad else None}: "
                          "packet i is picture i)")
                fpts = [num(f.get("pts")) for f in fr]
                if all(x is not None for x in fpts):
                    rep.check(order == fpts, "PTS of the pictures in your display order differ from ffprobe "
                                             "-show_frames pts order")
    return rep


# ================================================================ test inputs / variants
def x264_clip(out, opts, frames, profile):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=352x288:rate=25",
           "-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-profile:v", profile,
           "-qp", "26", "-x264-params", opts, "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die("ffmpeg failed: " + r.stderr.decode()[-500:])


def make_variants(outdir):
    ffref.require_tools()
    os.makedirs(outdir, exist_ok=True)
    data, gen = pocgen.make_stream()
    p = os.path.join(outdir, "poc_variants.264")
    open(p, "wb").write(data)
    rows = [dict(index=i, idr=g["idr"], nal_ref_idc=g["ref_idc"], type=g["type"], num_slices=g["nslices"],
                 frame_num=g["frame_num"], poc=g["poc"], top=g["top"], bottom=g["bottom"], mmco5=g["mmco5"],
                 first_nal=g["first_nal"]) for i, g in enumerate(gen)]
    open(p + ".expected", "w").write(render(rows))
    print(f"poc_variants.264: {len(rows)} synthetic pictures (real slice headers, random filler instead of "
          f"macroblocks) in 6 sections, each with its own SPS/PPS:")
    for line in pocgen.DESCRIPTION:
        print("  " + line)
    print("  poc_variants.264.expected: the generator's own expected output (the harness uses it as extra truth)")
    x264_clip(os.path.join(outdir, "poc_idr_periods.ts"),
              "keyint=10:min-keyint=10:scenecut=0:bframes=2:b-pyramid=normal:cabac=0", 45, "main")
    print("poc_idr_periods.ts: x264 Main, an IDR every 10 frames, B pyramid (POC type 0), 45 pictures")
    x264_clip(os.path.join(outdir, "poc_type2_slices.ts"),
              "keyint=12:min-keyint=12:scenecut=0:bframes=0:slices=3:ref=2", 40, "baseline")
    print("poc_type2_slices.ts: x264 Baseline, no B frames (x264 then writes POC type 2), 3 slices per picture, IDR every 12")
    x264_clip(os.path.join(outdir, "poc_opengop.ts"),
              "open-gop=1:keyint=12:min-keyint=12:scenecut=0:bframes=3:b-pyramid=normal:cabac=0", 48, "main")
    print("poc_opengop.ts: x264 Main, open GOP: non-IDR I pictures with B pictures that follow in decode order "
          "but precede in display order, across the I picture")
    print("Also useful (from common.testfiles): T1, T5, T4, T8, T9_spatial, T9_temporal, T12.")


# ================================================================ self test
def run_in_subprocess(inp, dump, expect):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), inp, dump], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=300)
    for l in (r.stdout.decode() + r.stderr.decode()).splitlines()[-4:]:
        print("   |", l[:230])
    return r.returncode


def self_test():
    ffref.require_tools()
    ok_all = True
    with tempfile.TemporaryDirectory() as td:
        vdir = os.path.join(td, "v")
        so = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            make_variants(vdir)
        finally:
            sys.stdout.close()
            sys.stdout = so
        names = ("T1", "T5", "T4", "T8", "T9_spatial", "T9_temporal", "T12")
        inputs = [testfiles.get(n) for n in names]
        inputs += [os.path.join(vdir, n) for n in ("poc_variants.264", "poc_idr_periods.ts", "poc_type2_slices.ts",
                                                    "poc_opengop.ts")]
        cache = {}
        poc_types, multi, mmco = set(), False, False
        for inp in inputs:
            with tempfile.TemporaryDirectory() as t2:
                pics = compute_poc(group_pictures(load_slices(inp, t2)))
            rows = truth_rows(pics)
            cache[inp] = rows
            poc_types |= {p.slices[0].sps["pic_order_cnt_type"] for p in pics}
            multi |= any(p.nslices > 1 for p in pics)
            mmco |= any(p.mmco5 for p in pics)
            reorder = sum(1 for a, b in zip(display_order(rows), rows) if a[1] != b["index"])
            good = os.path.join(td, "good.txt")
            open(good, "w").write(render(rows))
            print(f"-- {os.path.basename(inp)}: {len(rows)} picture(s), POC types "
                  f"{sorted({p.slices[0].sps['pic_order_cnt_type'] for p in pics})}, {reorder} pictures out of "
                  f"decode order; known-good output must PASS")
            if run_in_subprocess(inp, good, 0) != 0:
                ok_all = False
                print("   UNEXPECTED FAIL")
        print(f"-- coverage: POC types {sorted(poc_types)}, multi-slice pictures: {multi}, MMCO 5: {mmco}")
        if poc_types != {0, 1, 2} or not multi or not mmco:
            ok_all = False
            print("   self-test coverage problem")
        # ---- corruptions
        def corrupt_run(label, inp, mut):
            nonlocal ok_all
            lines = render(cache[inp]).splitlines()
            lines = mut(lines)
            badf = os.path.join(td, "bad.txt")
            open(badf, "w").write("\n".join(lines) + "\n")
            print(f"-- corrupted ({label}): must FAIL")
            if run_in_subprocess(inp, badf, 1) != 1:
                ok_all = False
                print("   UNEXPECTED: harness did not fail")

        def pic_lines(lines):
            return [i for i, l in enumerate(lines) if l.startswith("PIC ")]

        def set_col(col, delta, nth):
            def m(lines):
                i = pic_lines(lines)[nth]
                t = lines[i].split()
                k = PIC_COLS.index(col) + 1
                t[k] = str(int(t[k]) + delta)
                lines[i] = " ".join(t)
                return lines
            return m

        def renumber(lines):          # a reader that loses or merges a picture still counts 0, 1, 2, ...
            k = 0
            for i in pic_lines(lines):
                t = lines[i].split()
                t[1] = str(k)
                lines[i] = " ".join(t)
                k += 1
            return lines

        def drop_pic(nth):
            def m(lines):
                del lines[pic_lines(lines)[nth]]
                return renumber(lines)
            return m

        def merge_pics(nth):         # two pictures reported as one (a missed boundary)
            def m(lines):
                i = pic_lines(lines)
                a, b = lines[i[nth]].split(), lines[i[nth + 1]].split()
                a[5] = str(int(a[5]) + int(b[5]))
                lines[i[nth]] = " ".join(a)
                del lines[i[nth + 1]]
                return renumber(lines)
            return m

        def swap_display(lines):
            idx = [i for i, l in enumerate(lines) if l.startswith("DISPLAY ")]
            a, b = idx[4], idx[5]
            la, lb = lines[a].split(), lines[b].split()
            la[2:], lb[2:] = lb[2:], la[2:]
            lines[a], lines[b] = " ".join(la), " ".join(lb)
            return lines

        def drop_display(lines):
            return [l for l in lines if not l.startswith("DISPLAY ")]

        def display_by_decode_order(lines):
            pl = [l.split() for l in lines if l.startswith("PIC ")]
            return [l for l in lines if not l.startswith("DISPLAY ")] + [
                f"DISPLAY {i} {p[1]} {p[7]} {p[4]}" for i, p in enumerate(pl)]

        vinp = os.path.join(vdir, "poc_variants.264")
        t9 = testfiles.get("T9_temporal")
        t4 = testfiles.get("T4")
        corrupt_run("wrong poc in one picture (variants)", vinp, set_col("poc", 1, 50))
        corrupt_run("wrong top (variants)", vinp, set_col("top", 2, 120))
        corrupt_run("wrong bottom (variants)", vinp, set_col("bottom", 1, 200))
        corrupt_run("wrong frame_num", vinp, set_col("frame_num", 1, 30))
        corrupt_run("MMCO 5 flag cleared", vinp, set_col("mmco5", -1, next(
            i for i, r in enumerate(cache[vinp]) if r["mmco5"])))
        corrupt_run("wrong num_slices (T4)", t4, set_col("num_slices", 1, 3))
        corrupt_run("two pictures merged (T4: missed boundary)", t4, merge_pics(3))
        corrupt_run("two pictures merged (variants, differ only by nal_ref_idc/idr_pic_id)", vinp, merge_pics(
            next(i for i, r in enumerate(cache[vinp]) if r["idr"] and cache[vinp][i + 1]["idr"])))
        corrupt_run("a picture lost (T9_temporal)", t9, drop_pic(10))
        corrupt_run("display order swapped (T9_temporal)", t9, swap_display)
        corrupt_run("display order = decode order (T9_temporal)", t9, display_by_decode_order)
        corrupt_run("no DISPLAY section", t9, drop_display)
        def first_b_as_p(lines):
            i = next(i for i, l in enumerate(lines) if l.startswith("PIC ") and l.split()[4] == "B")
            t = lines[i].split()
            t[4] = "P"
            lines[i] = " ".join(t)
            return lines

        corrupt_run("wrong slice type letter (T8)", testfiles.get("T8"), first_b_as_p)
    print("SELF-TEST " + ("PASS" if ok_all else "FAIL"))
    sys.exit(0 if ok_all else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="H.264 .ts or raw .264 file")
    ap.add_argument("dump", nargs="?", help="your pictures file (PIC and DISPLAY lines)")
    ap.add_argument("--make-variants", metavar="DIR", help="write extra test inputs into DIR and exit")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if not a.input or not a.dump:
        ap.print_usage(sys.stderr)
        sys.exit(2)
    run_check(a.input, a.dump).finish()


if __name__ == "__main__":
    main()
