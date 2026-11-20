#!/usr/bin/env python3
"""Harness for Chapter 7.8 (B-slice parsing, B reference lists, bi-prediction; CAVLC, no direct prediction).

    python3 test_bslice.py <input.ts|input.264> <your_output.yuv>                      # pixels (the primary check)
    python3 test_bslice.py <input> [<your_output.yuv>] [--parse-dump F] [--lists-dump F] [--mv-dump F]   # optional channels
    python3 test_bslice.py --print-expected parse|lists|mv <input> <out.txt>           # the expected text of a channel
    python3 test_bslice.py --make-variants <out_dir>                                   # extra test inputs (see Readme)
    python3 test_bslice.py --self-test                                                 # verify the harness itself (a few minutes)

<your_output.yuv> = every decoded frame in DISPLAY order (frames sorted by picture order count inside each IDR period), 8-bit
planar yuv420p, CROPPED as the SPS says, no padding, no headers (Chapter 5.1's format).  Truth: ffmpeg (default output order,
`-fps_mode passthrough -f rawvideo -pix_fmt yuv420p`), compared with common/yuvcmp.py.  When your pixels differ, the harness decodes
the stream with the independent reference decoder in spoiler_reference/bdec.py, tells you the first wrong macroblock, its prediction
blocks (lists, ref_idx, reference pictures, vectors) and the lists of its slice, and checks whether your frame equals the reference
decoder with ONE deliberately wrong rule (the list in the Readme).

Optional channels (grammars in the Readme, section 5):
  --parse-dump  the Chapter 7.1 macroblock dump plus the B keys refidx1 / mvd1 (compared: SLICE type/first_mb, MB name mb_type sub
                refidx mvd refidx1 mvd1 cbp qp_delta, END status / end_bit, HIST)
  --lists-dump  Chapter 7.6's reference-list log with type=B lines, poc= and list1= (SLICE and DPB lines)
  --mv-dump     the final motion vectors of both lists, one line per inter macroblock

Truth of the secondary channels = the reference implementation in spoiler_reference/, validated before your output is compared
against independent sources (a failure there is a HARNESS PROBLEM, exit 2): every slice parses to its exact end; ffmpeg's
`-debug mb_type` map shows the same class (> list 0 only, < list 1 only, X both lists) and partition shape; ffmpeg's `-debug qp`
map the same QP_Y; ffmpeg's `-debug mmco` output shows the same short-term / long-term frames with the same picture order counts
after every marking; the motion vectors ffmpeg exports (`-flags2 +export_mvs`, source -1 = list 0, +1 = list 1) equal the
reference's; synthetic streams come with a manifest written by the bit writer.
Exit 0 = pass, 1 = different, 2 = unusable input / output (missing file, malformed dump, a stream with direct prediction when a
secondary channel needs it, ...) or harness problem.
"""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref, ffref, testfiles, yuvcmp, yuvref  # noqa: E402
from common import inter_ref as ir  # noqa: E402
import bdec  # noqa: E402
import bgen  # noqa: E402
import bmvpred_ref  # noqa: E402
import brefmgmt_ref  # noqa: E402
import bslicehdr_ref  # noqa: E402

SYNTH = ["synth_" + m for m in bgen.SCENARIOS]
SRC = "testsrc2=size=352x288:rate=25"
X264 = "cabac=0:direct=none:weightb=0"
VARIANTS = {   # name -> (video filter, codec options): x264 B-frame streams WITHOUT direct prediction
    "b_bf1": ("null", f"-profile:v main -x264-params {X264}:bframes=1:ref=2:keyint=100"),
    "b_bf3": ("null", f"-profile:v main -x264-params {X264}:bframes=3:ref=4:b-pyramid=none:keyint=10"),
    "b_pyr": ("null", f"-profile:v main -x264-params {X264}:bframes=3:ref=4:b-pyramid=normal:keyint=100"),
    "b_slices": ("null", f"-profile:v main -x264-params {X264}:bframes=2:ref=3:slices=3:keyint=100"),
    "b_nodeblock": ("null", f"-profile:v main -x264-params {X264}:bframes=2:ref=3:no-deblock=1:keyint=100"),
    "b_wpfade": ("fade=in:0:25", f"-profile:v main -x264-params {X264}:bframes=2:ref=3:weightp=2:keyint=100"),
    "b_part": ("null", f"-profile:v main -x264-params {X264}:bframes=2:ref=6:partitions=all:keyint=100"),
    "b_badapt": ("null", f"-profile:v main -x264-params {X264}:bframes=4:ref=5:b-adapt=2:b-pyramid=none:keyint=100"),
}
PREFIX_RE = re.compile(r"^\[([^\]]*)\]\s?")


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
    bgen.write_stream(name[len("synth_"):], out_264, seed)


def make_variants(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "T8.ts")
    shutil.copy(testfiles.get("T8"), p)
    print("wrote", p)
    for n in VARIANTS:
        p = os.path.join(out_dir, n + ".ts")
        make_variant(n, p)
        print("wrote", p)
    for n in SYNTH:
        p = os.path.join(out_dir, n + ".264")
        make_synth(n, p)
        print("wrote", p, "and", p + ".json")


def reference_yuv(path, out):
    ffref.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",
               "-pix_fmt", "yuv420p", out], timeout=600)
    if not os.path.exists(out):
        die(f"ffmpeg could not decode {path}")


# ================================================================ the truth object: parsed stream + reference model
class Truth:
    """Everything the reference knows about a stream: slices (infos, header tables), parsed macroblocks (results), picture order
    count / output order, motion vectors (when no direct prediction), the reference-list model."""
    pass


def load_truth(path, td, want_mv=True):
    t = Truth()
    t.path, t.td = path, td
    t.infos = ir.load(path, td, 3)
    if not t.infos:
        raise SystemExit("input problem: no slice NAL units found")
    try:
        for s in t.infos:
            bdec.check_supported(s)
    except bdec.Unsupported as e:
        raise SystemExit(f"unsupported input: {e}")
    tables = bslicehdr_ref.load(os.path.join(td, "ref_trace.txt"))
    if len(tables) != len(t.infos):
        raise SystemExit(f"internal error: {len(t.infos)} slices but {len(tables)} slice headers in the trace")
    for s, tb in zip(t.infos, tables):
        s.tab = tb
    t.results = ir.parse_all(t.infos, 3)
    t.pics = collections.OrderedDict()
    for r in t.results:
        t.pics.setdefault(r.info.picture, []).append(r)
    t.order = sorted(t.pics)
    t.meta, t.out_order = bdec.picture_meta(t.pics, t.order)
    t.out_rank = [0] * len(t.order)
    for k, pi in enumerate(t.out_order):
        t.out_rank[pi] = k
    t.direct = None
    if want_mv and all(r.status == "complete" for r in t.results):
        try:
            bmvpred_ref.fill_results(t.results)
        except bmvpred_ref.Direct as e:
            t.direct = str(e)
    t.lists = None
    return t


def geometry(t):
    s0 = t.infos[0]
    sps = s0.sps
    return yuvref.cropped_size(
        s0.pic_width_in_mbs, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
        sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
        sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))


# ================================================================ the reference-list model (8.2.4, 8.2.5) and its log
def fmt_slot(e):
    if e is None:
        return "-"
    lab, kind, num = e
    return f"{lab}:{num}" if kind == "S" else f"{lab}:L{num}"


def fmt_ref(e):
    return f"{e[0]}:{e[1]}"


def lists_model(t, mutant=None):
    """-> dict(slices=[row], dpb=[row], posts=[state after every reference picture's marking]).  row of a slice: dict(pic, slice,
    type, frame_num, poc, a0, a1, l0, l1); dpb row: (pic, short, long, maxlt)."""
    s0 = t.infos[0]
    sps = s0.sps
    mu = mutant if mutant in brefmgmt_ref.MUTANTS else None
    d = brefmgmt_ref.Dpb(sps["max_num_ref_frames"], sps["log2_max_frame_num_minus4"] + 4,
                         bool(sps.get("gaps_in_frame_num_allowed_flag", 0)), mu)
    old = bdec.MUTANT
    bdec.MUTANT = mutant if mutant == "poc_no_msb" else None
    try:
        meta, _ = bdec.picture_meta(t.pics, t.order)
    finally:
        bdec.MUTANT = old
    out = dict(slices=[], dpb=[], posts=[], gaps=False)
    for pi in t.order:
        rs = t.pics[pi]
        i0 = rs[0].info
        tb0 = i0.tab
        try:
            labels = d.check_frame_num(i0.hdr["frame_num"], i0.nal_type == 5)
        except brefmgmt_ref.StreamError as e:
            raise SystemExit(f"unsupported input: {e}")
        if labels:
            out["gaps"] = True
        poc = meta[pi]["raw"]
        for si, r in enumerate(rs):
            tb = r.info.tab
            if tb["slice_type"] in (0, 1):
                try:
                    l0, l1 = (d.build_list(tb["frame_num"], tb["active0"], tb["mods0"]), []) if tb["slice_type"] == 0 else \
                        d.build_lists_b(tb["frame_num"], poc, tb["active0"], tb["active1"], tb["mods0"], tb["mods1"])
                except brefmgmt_ref.StreamError as e:
                    raise SystemExit(f"unsupported input: {e}")
                out["slices"].append(dict(pic=pi, slice=si, type="P" if tb["slice_type"] == 0 else "B", frame_num=tb["frame_num"],
                                          poc=poc, a0=tb["active0"], a1=tb["active1"], l0=l0, l1=l1))
        try:
            d.finish_picture(pi, tb0["frame_num"], i0.nal_type == 5, tb0["nal_ref_idc"], tb0["lt_flag"], tb0["mmco"], poc)
        except brefmgmt_ref.StreamError as e:
            raise SystemExit(f"unsupported input: {e}")
        sh, lg, mx = d.state()
        out["dpb"].append((pi, sh, lg, mx))
        if tb0["nal_ref_idc"]:
            out["posts"].append(d.post_state_poc())
    return out


def render_lists(m):
    lines = []
    rows = {(r["pic"], r["slice"]): r for r in m["slices"]}
    by_pic = collections.defaultdict(list)
    for (p, s), r in sorted(rows.items()):
        by_pic[p].append(r)
    for p, sh, lg, mx in m["dpb"]:
        for r in by_pic.get(p, []):
            line = (f"SLICE {r['pic']} {r['slice']} type={r['type']} frame_num={r['frame_num']} poc={r['poc']} active={r['a0']} "
                    f"list0={','.join(map(fmt_slot, r['l0']))}")
            if r["type"] == "B":
                line += f" active1={r['a1']} list1={','.join(map(fmt_slot, r['l1']))}"
            lines.append(line)
        lines.append(f"DPB {p} short={','.join(map(fmt_ref, sh)) or '-'} long={','.join(map(fmt_ref, lg)) or '-'} "
                     f"maxlt={'none' if mx is None else mx}")
    return "\n".join(lines) + "\n"


# ================================================================ the motion-vector dump
def render_mv(t):
    lines = []
    for pi in t.order:
        for r in t.pics[pi]:
            for rec in r.mbs:
                if rec.grid is None:
                    continue
                l0 = ",".join(f"{g[0]}:{g[1]}:{g[2]}" if g[0] >= 0 else "-" for g in rec.grid)
                l1 = ",".join(f"{g[3]}:{g[4]}:{g[5]}" if g[3] >= 0 else "-" for g in rec.grid)
                lines.append(f"MV {pi} {rec.addr} l0={l0} l1={l1}")
    return "\n".join(lines) + "\n"


# ================================================================ expected parse dump
def expected_parse_structs(t):
    text = ir.render_dump(t.results, 3)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text)
    try:
        return ir.parse_dump(f.name)
    finally:
        os.unlink(f.name)


# ================================================================ reading the optional channels (reader output)
class DumpError(Exception):
    pass


def kv_tokens(tokens, no, line):
    d = {}
    for tk in tokens:
        if "=" not in tk:
            raise DumpError(f"line {no}: expected key=value, found {tk!r}: {line[:80]!r}")
        k, v = tk.split("=", 1)
        d[k] = v
    return d


def parse_slot(tok, no):
    if tok == "-":
        return None
    m = re.fullmatch(r"(g?\d+):(L?)(-?\d+)", tok)
    if not m:
        raise DumpError(f"line {no}: list entry {tok!r} is not <pic>:<PicNum>, <pic>:L<LongTermPicNum> or -")
    return (m.group(1), "L" if m.group(2) else "S", int(m.group(3)))


def parse_ref(tok, no):
    m = re.fullmatch(r"(g?\d+):(-?\d+)", tok)
    if not m:
        raise DumpError(f"line {no}: entry {tok!r} is not <pic>:<number>")
    return (m.group(1), int(m.group(2)))


def parse_lists(text):
    """-> (slices {(pic, slice): dict}, dpb {pic: (short, long, maxlt)})"""
    slices, dpb = {}, {}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        try:
            if tok[0] == "SLICE":
                if len(tok) < 4:
                    raise DumpError(f"line {no}: SLICE needs <pic> <slice> and key=value fields: {line[:80]!r}")
                pic, sl = int(tok[1]), int(tok[2])
                kv = kv_tokens(tok[3:], no, line)
                for need in ("type", "frame_num", "active", "list0"):
                    if need not in kv:
                        raise DumpError(f"line {no}: SLICE line without {need}=")
                row = dict(type=kv["type"], frame_num=int(kv["frame_num"]), poc=int(kv["poc"]) if "poc" in kv else None,
                           a0=int(kv["active"]), l0=[parse_slot(x, no) for x in kv["list0"].split(",")], a1=None, l1=None)
                if "list1" in kv or "active1" in kv:
                    row["a1"] = int(kv.get("active1", -1))
                    row["l1"] = [parse_slot(x, no) for x in kv.get("list1", "-").split(",")]
                if (pic, sl) in slices:
                    raise DumpError(f"line {no}: slice {pic} {sl} appears twice")
                slices[(pic, sl)] = row
            elif tok[0] == "DPB":
                if len(tok) < 3:
                    raise DumpError(f"line {no}: DPB needs <pic> and short= long= maxlt=")
                pic = int(tok[1])
                kv = kv_tokens(tok[2:], no, line)
                for need in ("short", "long", "maxlt"):
                    if need not in kv:
                        raise DumpError(f"line {no}: DPB line without {need}=")
                dpb[pic] = ([parse_ref(x, no) for x in kv["short"].split(",")] if kv["short"] != "-" else [],
                            [parse_ref(x, no) for x in kv["long"].split(",")] if kv["long"] != "-" else [],
                            None if kv["maxlt"] == "none" else int(kv["maxlt"]))
            else:
                raise DumpError(f"line {no}: unknown line type {tok[0]!r} (allowed: SLICE, DPB)")
        except ValueError as e:
            raise DumpError(f"line {no}: {e}")
    return slices, dpb


def parse_mv(text):
    """-> {(pic, addr): (l0 [16 entries], l1 [16 entries])}, entry = (ref, mvx, mvy) or None"""
    out = {}
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        if len(tok) != 5 or tok[0] != "MV" or not tok[3].startswith("l0=") or not tok[4].startswith("l1="):
            raise DumpError(f"line {no}: expected 'MV <pic> <addr> l0=<16 entries> l1=<16 entries>', got {line[:80]!r}")
        try:
            pic, addr = int(tok[1]), int(tok[2])
            lists = []
            for part in (tok[3][3:], tok[4][3:]):
                ents = []
                for e in part.split(","):
                    if e == "-":
                        ents.append(None)
                    else:
                        r, x, y = (int(v) for v in e.split(":"))
                        ents.append((r, x, y))
                if len(ents) != 16:
                    raise DumpError(f"line {no}: {len(ents)} entries in a list, need exactly 16 (4x4 blocks, raster order)")
                lists.append(ents)
        except ValueError:
            raise DumpError(f"line {no}: entries must be '-' or ref:mvx:mvy (integers): {line[:80]!r}")
        if (pic, addr) in out:
            raise DumpError(f"line {no}: macroblock {pic} {addr} appears twice")
        out[(pic, addr)] = (lists[0], lists[1])
    return out


# ================================================================ comparing the channels
KEYS_SLICE = ["type", "first_mb"]
KEYS_PCM = ["align_bits", "sum_y", "sum_cb", "sum_cr"]
KEYS_END = ["mbs", "status", "end_bit"]
KEYS_CMP = ["name", "mb_type", "sub", "refidx", "mvd", "refidx1", "mvd1", "cbp", "qp_delta"]


def parse_hints(e, a):
    h = []
    if e["name"] != a.get("name"):
        if e["mb_type"] != a.get("mb_type"):
            h.append("the mb_type VALUE differs: check where the B slice's mb_type table starts (0 = B_Direct_16x16, 1..3 = 16x16 "
                     "L0 / L1 / Bi, 4..21 = 16x8 / 8x16 pairs, 22 = B_8x8, 23.. = intra) and that an earlier macroblock did not "
                     "consume the wrong number of bits")
        else:
            h.append("same mb_type value, different name: Table 7-14 is mis-typed (L0 / L1 / Bi order, or 16x8 versus 8x16 -- even "
                     "numbers are 16x8, odd numbers 8x16)")
    elif e["sub"] != a.get("sub"):
        h.append("sub_mb_type values differ: Table 7-18 (0 direct, 1..3 L0 / L1 / Bi 8x8, 4..9 8x4 / 4x8 in L0, L1, Bi pairs, "
                 "10..12 4x4)")
    elif e["refidx"] != a.get("refidx") or e["refidx1"] != a.get("refidx1"):
        if e["refidx"] == a.get("refidx1") and e["refidx1"] == a.get("refidx"):
            h.append("refidx and refidx1 are swapped")
        h.append("ref_idx: list 0 values come first for ALL partitions, then list 1 values; te(v) uses num_ref_idx_l0_active for list 0 "
                 "and num_ref_idx_l1_active for list 1 (one inverted bit when the active count is 2, nothing when it is 1); "
                 "a partition has an entry only for the lists it uses")
    elif e["mvd"] != a.get("mvd") or e["mvd1"] != a.get("mvd1"):
        h.append("mvd: all mvd_l0 of the macroblock (partitions and sub-partitions in order) come before all mvd_l1")
    elif e["cbp"] != a.get("cbp"):
        h.append("coded_block_pattern: inter column of Table 9-4 (as in P slices)")
    return h


def compare_parse(rep, exp_slices, exp_hist, act_slices, act_hist):
    rep.check(len(act_slices) == len(exp_slices),
              f"number of SLICE blocks: expected {len(exp_slices)} (one per slice NAL unit, stream order), got {len(act_slices)}")
    shown = 0
    for e, a in zip(exp_slices, act_slices):
        k = e["index"]
        rep.check(a["index"] == k, f"slice block #{k}: expected index {k}, got {a['index']}")
        for key in KEYS_SLICE:
            rep.check(a["slice"].get(key) == e["slice"][key], f"SLICE {k} {key}: expected {e['slice'][key]}, got {a['slice'].get(key)}")
        rep.check(len(a["mbs"]) == len(e["mbs"]),
                  f"SLICE {k}: expected {len(e['mbs'])} MB lines (every macroblock), got {len(a['mbs'])}; expected END status "
                  f"{e['end']['status']}, you reported {a['end'].get('status')}")
        for j, ((ea, ekv), (aa, akv)) in enumerate(zip(e["mbs"], a["mbs"])):
            rep.check(ea == aa, f"SLICE {k} MB #{j}: expected macroblock address {ea}, got {aa}")
            akv = dict(akv)
            for key in ("refidx1", "mvd1"):
                akv.setdefault(key, "-")
            bad = [key for key in KEYS_CMP if str(akv.get(key)) != str(ekv[key])]
            if not bad:
                rep.check(True, "")
                continue
            for key in bad:
                rep.check(False, f"SLICE {k} MB addr {ea} {key}: expected {ekv[key]}, got {akv.get(key)}"
                                 f" (expected: name={ekv['name']} sub={ekv['sub']} refidx={ekv['refidx']} mvd={ekv['mvd']} "
                                 f"refidx1={ekv['refidx1']} mvd1={ekv['mvd1']})")
            if shown < 3:
                shown += 1
                for hint in parse_hints(ekv, akv):
                    rep.failures.append(f"    hint: {hint}")
        rep.check(sorted(a["pcm"]) == sorted(e["pcm"]), f"SLICE {k}: PCM lines for macroblocks {sorted(a['pcm'])}, expected {sorted(e['pcm'])}")
        for addr in sorted(set(e["pcm"]) & set(a["pcm"])):
            for key in KEYS_PCM:
                rep.check(a["pcm"][addr].get(key) == e["pcm"][addr][key],
                          f"SLICE {k} PCM addr {addr} {key}: expected {e['pcm'][addr][key]}, got {a['pcm'][addr].get(key)}")
        for key in KEYS_END:
            rep.check(a["end"].get(key) == e["end"][key], f"SLICE {k} END {key}: expected {e['end'][key]}, got {a['end'].get(key)}")
    own = {}
    for a in act_slices:
        for _, kv in a["mbs"]:
            own[kv.get("name", "?")] = own.get(kv.get("name", "?"), 0) + 1
    rep.check(act_hist == own, f"HIST does not match your own MB lines: {ir.hist_diff(own, act_hist)}")
    rep.check(act_hist == exp_hist, f"HIST differs from the expected one (name: expected vs yours): {ir.hist_diff(exp_hist, act_hist)}")


def list_hint(exp0, exp1, got, which):
    """At most one hint, the most specific first."""
    exp = exp0 if which == 0 else exp1
    other = exp0 if which == 1 else exp1
    if which == 1 and len(got) == len(exp) and len(got) > 1 and got[0] == exp[1] and got[1] == exp[0] and got[2:] == exp[2:]:
        return ["first two entries of list 1 swapped: the swap rule ('when list 1 has more than one entry and equals list 0, swap "
                "its first two entries') is missing, applied when it should not be, or tested at the wrong time: it is tested on "
                "the full initial lists, BEFORE they are cut to the active counts"]
    if other is not None and got == other and got != exp:
        return [f"your list {which} equals the expected list {1 - which}: list 0 and list 1 are ordered differently "
                f"(list 0: past pictures first, nearest first; list 1: future pictures first, nearest first)"]
    if sorted(map(str, got)) == sorted(map(str, exp)) and got != exp:
        return ["same pictures, different order: check the sort keys (POC, not PicNum, for B slices) and the direction "
                "(past: descending POC; future: ascending POC)"]
    return []


def compare_lists(rep, exp, got):
    es, ed = exp
    gs, gd = got
    for key in sorted(es):
        if key not in gs:
            rep.check(False, f"slice {key[0]} {key[1]}: missing SLICE line (one per P and B slice)")
    for key in sorted(gs):
        if key not in es:
            rep.check(False, f"slice {key[0]} {key[1]}: unexpected SLICE line (an I slice, or no such slice)")
    for key in sorted(es):
        if key not in gs:
            continue
        e, g = es[key], gs[key]
        msgs, hints = [], []
        if e["type"] != g["type"]:
            msgs.append(f"type: expected {e['type']}, got {g['type']}")
        if e["frame_num"] != g["frame_num"]:
            msgs.append(f"frame_num: expected {e['frame_num']}, got {g['frame_num']}")
        if g["poc"] is not None and g["poc"] != e["poc"]:
            msgs.append(f"poc: expected {e['poc']}, got {g['poc']} (PicOrderCnt of the picture, 8.2.1)")
        elif g["poc"] is None and e["type"] == "B":
            msgs.append("poc= missing on a B slice line")
        if e["a0"] != g["a0"]:
            msgs.append(f"active: expected {e['a0']}, got {g['a0']}")
        if e["l0"] != g["l0"]:
            msgs.append(f"list0: expected {','.join(map(fmt_slot, e['l0']))}, got {','.join(map(fmt_slot, g['l0']))}")
            hints += list_hint(e["l0"], e["l1"] or None, g["l0"], 0)
        if e["type"] == "B":
            if g["l1"] is None:
                msgs.append("list1= missing on a B slice line")
            else:
                if e["a1"] != g["a1"]:
                    msgs.append(f"active1: expected {e['a1']}, got {g['a1']}")
                if e["l1"] != g["l1"]:
                    msgs.append(f"list1: expected {','.join(map(fmt_slot, e['l1']))}, got {','.join(map(fmt_slot, g['l1']))}")
                    hints += list_hint(e["l0"], e["l1"], g["l1"], 1)
        elif g["l1"] is not None and g["type"] == "P":
            msgs.append("a P slice has no list1")
        rep.check(not msgs, f"slice {key[0]} {key[1]} (type {e['type']}, poc {e['poc']}): " + "; ".join(msgs))
        for hh in dict.fromkeys(hints):
            if msgs:
                rep.failures.append(f"    hint: {hh}")
    for p, (sh, lg, mx) in sorted(ed.items()):
        if p not in gd:
            rep.check(False, f"picture {p}: missing DPB line (one per coded picture, non-reference pictures too)")
            continue
        gsh, glg, gmx = gd[p]
        rep.check(sh == gsh, f"DPB {p} short: expected {','.join(map(fmt_ref, sh)) or '-'}, got {','.join(map(fmt_ref, gsh)) or '-'}"
                  + (" (a non-reference B picture is not stored; reference pictures are, newest first)" if sh != gsh else ""))
        rep.check(lg == glg, f"DPB {p} long: expected {lg}, got {glg}")
        rep.check(mx == gmx, f"DPB {p} maxlt: expected {mx}, got {gmx}")
    for p in sorted(gd):
        if p not in ed:
            rep.check(False, f"DPB line for picture {p}, which does not exist")


def compare_mv(rep, t, got):
    exp = {}
    for pi in t.order:
        for r in t.pics[pi]:
            for rec in r.mbs:
                if rec.grid is not None:
                    exp[(pi, rec.addr)] = rec
    for key in sorted(exp):
        if key not in got:
            rep.check(False, f"macroblock {key[0]} {key[1]}: missing MV line (every inter macroblock, skipped ones too)")
    for key in sorted(got):
        if key not in exp:
            rep.check(False, f"macroblock {key[0]} {key[1]}: unexpected MV line (intra macroblock or no such macroblock)")
    for key in sorted(exp):
        if key not in got:
            continue
        rec = exp[key]
        el0 = [(g[0], g[1], g[2]) if g[0] >= 0 else None for g in rec.grid]
        el1 = [(g[3], g[4], g[5]) if g[3] >= 0 else None for g in rec.grid]
        gl0, gl1 = got[key]
        bad = None
        for lst, (e, g) in enumerate(((el0, gl0), (el1, gl1))):
            for b in range(16):
                if e[b] != g[b]:
                    bad = (lst, b, e[b], g[b])
                    break
            if bad:
                break
        ok = bad is None
        msg = ""
        if not ok:
            lst, b, e, g = bad
            f = lambda v: "-" if v is None else f"{v[0]}:{v[1]}:{v[2]}"  # noqa: E731
            msg = (f"picture {key[0]} macroblock {key[1]} ({rec.f['name']}) list {lst} block {b} (x={(b % 4) * 4}, y={(b // 4) * 4}): "
                   f"expected {f(e)}, got {f(g)}")
            other = (el1 if lst == 0 else el0)
            if g == other[b] and g is not None:
                msg += "  [hint: that is the expected value of the OTHER list]"
        rep.check(ok, msg)


# ================================================================ validating the reference against independent sources
def raw_264(path, td):
    if path.lower().endswith((".264", ".h264")):
        return path
    raw = os.path.join(td, "mv_raw.264")
    if not os.path.exists(raw):
        ffref.raw_h264(path, raw)
    return raw


def check_slices_complete(t):
    probs = [f"slice {r.info.index}: reference parser status {r.status} ({r.error or r.note})" for r in t.results if r.status != "complete"]
    return probs, f"all {len(t.results)} slices parse to their exact end (rbsp_stop_one_bit)"


def match_cells(t, td):
    return ir.check_vs_ffmpeg_cells(t.path, t.infos, t.results, td, None, out_rank=t.out_rank)


def match_qp(t, td):
    return ir.check_vs_ffmpeg_qp(t.path, t.infos, t.results, td, None, out_rank=t.out_rank)


def ffmpeg_posts(path, td):
    """ffmpeg's `-debug mmco` view after every marking: list of ([(frame_num, poc)] short-term newest first,
    [(LongTermFrameIdx, frame_num, poc)])."""
    log = os.path.join(td, "mmco.log")
    cmd = ["ffprobe", "-hide_banner", "-loglevel", "repeat+debug", "-debug", "mmco", "-threads", "1", "-count_frames",
           "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += [path]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f, timeout=300, check=False)
    text = open(log, errors="replace").read().splitlines()
    inst, start = None, 0
    for k, raw in enumerate(text):
        if "Reinit context" in raw:
            inst, start = raw[:raw.index("]") + 1], k
    blocks, cur, mode, marking = [], None, None, False
    ent = re.compile(r"^(\d+) fn:(\d+) poc:(-?\d+)")
    for raw in text[start:]:
        if inst and not raw.startswith(inst):
            continue
        line = PREFIX_RE.sub("", raw)
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
                    cur["short"].append((int(m.group(2)), int(m.group(3))))
                elif mode == "long":
                    cur["long"].append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return [(b["short"], b["long"]) for b in blocks if b["marking"]]


def check_posts(model, ff):
    """frame_num lists must be equal; picture order counts must agree up to ONE constant per list (ffmpeg adds an offset of 65536
    to the counts of a type-0 stream)."""
    if len(model) != len(ff):
        return [f"ffmpeg's -debug mmco log has {len(ff)} marking results, the reference expects {len(model)}"]
    out = []
    for k, ((ms, ml), (fs, fl)) in enumerate(zip(model, ff)):
        if [x[0] for x in ms] != [x[0] for x in fs] or [(x[0], x[1]) for x in ml] != [(x[0], x[1]) for x in fl]:
            out.append(f"marking #{k}: reference short {[x[0] for x in ms]} long {[(x[0], x[1]) for x in ml]}, ffmpeg short "
                       f"{[x[0] for x in fs]} long {[(x[0], x[1]) for x in fl]}")
            continue
        mp = [x[1] for x in ms] + [x[2] for x in ml]
        fp = [x[1] for x in fs] + [x[2] for x in fl]
        if mp and len({b - a for a, b in zip(mp, fp)}) > 1:
            out.append(f"marking #{k}: picture order counts of the frames: reference {mp}, ffmpeg {fp} (differences are not constant)")
    return out


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


def export_expected(t):
    """What ffmpeg's export must contain per DECODING picture: for every inter macroblock and every list the macroblock uses at all,
    one vector per macroblock partition (16x16: 1, 16x8 / 8x16: 2, 8x8 or smaller: 4 -- the vector of the first 4x4 block of each 8x8
    quarter); a partition that does not use that list shows (0, 0)."""
    out = {}
    for pi in t.order:
        d = {}
        for r in t.pics[pi]:
            w = r.info.pic_width_in_mbs
            for rec in r.mbs:
                if rec.grid is None:
                    continue
                n = rec.f["name"]
                mx0, my0 = (rec.addr % w) * 16, (rec.addr // w) * 16
                if n.endswith("16x16") or n == "P_Skip":
                    geo = [(0, 0, 16, 16)]
                elif n.endswith("16x8"):
                    geo = [(0, 0, 16, 8), (0, 8, 16, 8)]
                elif n.endswith("8x16"):
                    geo = [(0, 0, 8, 16), (8, 0, 8, 16)]
                else:
                    geo = [(0, 0, 8, 8), (8, 0, 8, 8), (0, 8, 8, 8), (8, 8, 8, 8)]
                for lst in (0, 1):
                    if n != "B_8x8" and not any(g[3 * lst] >= 0 for g in rec.grid):
                        continue            # ffmpeg treats every B_8x8 macroblock as using both lists
                    for x, y, pw, ph in geo:
                        g = rec.grid[(y >> 2) * 4 + (x >> 2)]
                        mv = (g[3 * lst + 1], g[3 * lst + 2]) if g[3 * lst] >= 0 else (0, 0)
                        d[(mx0 + x + pw // 2, my0 + y + ph // 2, -1 if lst == 0 else 1, pw, ph)] = mv
        out[pi] = d
    return out


def check_mvs(t, td):
    if t.direct:
        return [], f"ffmpeg motion-vector cross-check skipped: {t.direct}"
    ff = ffmpeg_mvs(t.path, td)
    if ff is None:
        return [], "ffmpeg motion-vector cross-check skipped: libavcodec could not be loaded by ffmv_helper.py"
    exp = export_expected(t)
    probs, n = [], 0
    for pi in t.order:
        got = ff.get(t.out_rank[pi], {})
        e = exp[pi]
        n += len(e)
        if e != got:
            miss = [k for k in e if k not in got]
            extra = [k for k in got if k not in e]
            diff = [k for k in e if k in got and e[k] != got[k]]
            k = (diff or miss or extra)[0]
            probs.append(f"picture {pi}: exported vectors differ: {len(diff)} different, {len(miss)} missing, {len(extra)} extra; "
                         f"e.g. {k}: reference {e.get(k)}, ffmpeg {got.get(k)}")
    return probs, f"ffmpeg exported motion vectors: {n} vectors (list 0 and list 1) in {len(t.order)} picture(s) equal the reference's"


def check_manifest(t, manifest):
    probs, n = [], 0
    if len(manifest["pictures"]) != len(t.order):
        return [f"manifest has {len(manifest['pictures'])} pictures, the stream {len(t.order)}"], ""
    sl = 0
    for pi, mp in zip(t.order, manifest["pictures"]):
        for ms in mp["slices"]:
            r = t.results[sl]
            sl += 1
            if r.status != "complete" or len(r.mbs) != len(ms["mbs"]):
                probs.append(f"slice {r.info.index}: reference parsed {len(r.mbs)} macroblocks ({r.status}), generator wrote {len(ms['mbs'])}")
                continue
            for e, rec in zip(ms["mbs"], r.mbs):
                n += 1
                for key in ("name", "mb_type", "sub", "refidx", "mvd", "refidx1", "mvd1"):
                    if key in e and str(e[key]) != str(rec.f[key]):
                        probs.append(f"slice {r.info.index} addr {rec.addr} {key}: reference {rec.f[key]}, generator {e[key]}")
    return probs, f"{n} macroblocks: the reference's mb_type, sub_mb_type, ref_idx_l0 / l1 and mvd_l0 / l1 equal the generator's"


def synth_decode_clean(path, td):
    """ffmpeg decodes a synthetic stream without any message other than 'Missing reference picture' (printed for list entries
    beyond the available pictures, which the generator never uses)."""
    p = ffref.run(["ffmpeg", "-v", "warning", "-threads", "1", "-y", "-f", "h264", "-i", path, "-f", "null", "-"], timeout=120)
    msgs = [l for l in p.stderr.decode(errors="replace").splitlines() if l.strip() and "Missing reference picture" not in l
            and "Last message repeated" not in l]
    return [f"ffmpeg message on a synthetic stream: {m[:120]}" for m in msgs[:3]]


def reference_trust(t, quiet=False, lists_model_out=None):
    """-> problems (empty = the reference agrees with every independent source)."""
    say = (lambda *a: None) if quiet else (lambda *a: print(" ", *a))
    problems = []
    td = t.td
    checks = [(check_slices_complete, (t,)), (match_cells, (t, td)), (match_qp, (t, td))]
    for fn, args in checks:
        p, msg = fn(*args)
        problems += p
        say(msg if not p else f"{fn.__name__} FAILED")
    m = lists_model_out or lists_model(t)
    try:
        ff = ffmpeg_posts(t.path, td)
        p = check_posts(m["posts"], ff) if not m["gaps"] else []
        problems += p
        say("ffmpeg -debug mmco: short-term / long-term frames and picture order counts agree after each of "
            f"{len(m['posts'])} marking(s)" if not p else "mmco cross-check FAILED")
    except Exception as e:  # noqa: BLE001
        say(f"ffmpeg -debug mmco cross-check skipped ({e})")
    p, msg = check_mvs(t, td)
    problems += p
    say(msg if not p else "motion-vector cross-check FAILED")
    mp = t.path + ".json"
    if t.path.lower().endswith(".264") and os.path.exists(mp):
        manifest = json.load(open(mp))
        p, msg = check_manifest(t, manifest)
        problems += p
        say(msg if not p else "synthetic-stream manifest check FAILED")
        p = synth_decode_clean(t.path, td)
        problems += p
        say("ffmpeg decodes the synthetic stream without messages" if not p else "ffmpeg messages on the synthetic stream")
    return problems


# ================================================================ diagnosis of a pixel mismatch
def read_frame(path, idx, fsize):
    with open(path, "rb") as f:
        f.seek(idx * fsize)
        return f.read(fsize)


def fmt_mode(p):
    refs = ",".join(f"L{l}[{r}]" for l, r in p["refs"])
    mvs = " ".join(f"mv({x},{y})" for x, y in p["mvs"])
    pics = ",".join(f"#{x}" for x in p["pids"])
    return f"x={p['x']} y={p['y']} {p['w']}x{p['h']} {p['mode']}: {refs} = picture {pics}, {mvs}"


def diagnose(t, ref_yuv, act_yuv, res, w, h, origin, workdir, budget=40.0, cache=None, candidates=None):
    frame = res["first_diff"]["frame"]
    L = []
    if t.direct:
        return [f"diagnosis not available: {t.direct}"]
    t0 = time.time()
    cache = cache if cache is not None else {"parsed": (t.infos, t.results)}
    try:
        pics = bdec.decode(t.path, upto=frame + 1, record=True, workdir=workdir, cache=cache)
    except bdec.Unsupported as e:
        return [f"diagnosis not available: {e}"]
    except Exception as e:  # noqa: BLE001
        return [f"diagnosis not available: the reference decoder failed on this stream ({type(e).__name__}: {e})"]
    t_one = time.time() - t0
    if len(pics) <= frame or pics[frame] is None:
        return ["diagnosis not available: the reference decoder produced fewer pictures than ffmpeg"]
    pic = pics[frame]
    geo, fsize = yuvcmp.frame_geometry(w, h)
    exp = read_frame(ref_yuv, frame, fsize)
    act = read_frame(act_yuv, frame, fsize)
    mine = bdec.frame_bytes(pic)
    if mine != exp:
        L.append(f"WARNING: the harness's own reference decoder does not reproduce ffmpeg's frame {frame} exactly; the advice may be unreliable.")
    x0, y0 = origin
    wmb = pic["w_mbs"]
    bad_mb = collections.defaultdict(lambda: [0, 0, 0])
    diffs = []
    for pl, (name, pw, ph, off) in enumerate(geo):
        for y in range(ph):
            ra, rb = exp[off + y * pw: off + (y + 1) * pw], act[off + y * pw: off + (y + 1) * pw]
            if ra == rb:
                continue
            for x in range(pw):
                if ra[x] != rb[x]:
                    a = (((y0 // 2 + y) // 8) * wmb + (x0 // 2 + x) // 8) if pl else (((y0 + y) // 16) * wmb + (x0 + x) // 16)
                    bad_mb[a][pl] += 1
                    diffs.append((pl, ra[x], rb[x], a))
    # is this frame some OTHER expected frame (output order)?
    if bad_mb and len(bad_mb) > 0.5 * pic["w_mbs"] * pic["h_mbs"]:
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
    m = t.meta[pic["pid"]]
    L.append(f"output frame {frame} = coded picture #{pic['pid']} (decoding order), slice types {sorted(set(pic['slice_types']))}, "
             f"POC {m['raw']}, IDR period {m['period']}")
    L.append(f"first wrong macroblock in raster order: address {first} (x={first % wmb}, y={first // wmb}); wrong samples "
             f"Y {cnt[0]}, Cb {cnt[1]}, Cr {cnt[2]}")
    mb = pic["mbs"].get(first)
    if mb is not None:
        L.append(f"  type {mb['name']}, slice {mb['slice']}")
        for lg in pic["lists"]:
            if lg["slice"] == mb["slice"]:
                s0, s1 = lg["slots"]
                L.append(f"  that slice's list0: {','.join(map(fmt_slot, s0))}" + (f"   list1: {','.join(map(fmt_slot, s1))}" if lg["type"] == 1 else ""))
        for p in mb["parts"]:
            L.append("  partition " + fmt_mode(p))
        if mb["name"] in ("P_Skip",):
            L.append("  (P_Skip: ref_idx 0 of list 0 with the predicted vector)")
    mine_diff = [(pl, e, a) for pl, e, a, ad in diffs if ad == first]
    mx = max(abs(e - a) for _, e, a in mine_diff) if mine_diff else 0
    if mine_diff:
        L.append(f"  in this macroblock the largest |expected - yours| is {mx}")
    names = list(candidates) if candidates else [k for k in bdec.ALL_MUTANTS]
    matches, tried = [], 0
    t_start = time.time()
    for mname in names:
        if time.time() - t_start + t_one > budget:
            break
        tried += 1
        bdec.MUTANT = mname
        try:
            mp = bdec.decode(t.path, upto=frame + 1, workdir=workdir, cache=cache)
        except Exception:  # noqa: BLE001
            continue
        finally:
            bdec.MUTANT = None
        if len(mp) > frame and mp[frame] is not None and bdec.frame_bytes(mp[frame]) == act:
            matches.append(mname)
    bmvpred_ref.fill_results(t.results)          # leave the shared parse results as the correct rules computed them
    if matches:
        L.append("your frame is EXACTLY what the reference decoder produces with the wrong rule(s):")
        for mname in matches:
            L.append(f"  - {mname}: {bdec.ALL_MUTANTS[mname]}")
    else:
        L.append(f"your frame equals none of the {tried} known wrong rules" + ("" if tried == len(names) else " (time budget reached)"))
    H = []
    if mb is not None and mb["parts"]:
        modes = {p["mode"] for p in mb["parts"]}
        if "Bi" in modes and mx <= 1:
            H.append("a bi-predicted partition is off by one: the default average is (p0 + p1 + 1) >> 1 per sample, from the two "
                     "interpolated (rounded, clipped) predictions.")
        if "L1" in modes or "Bi" in modes:
            H.append("the first wrong macroblock uses list 1: check RefPicList1 (the initial order, the swap rule, truncation to "
                     "num_ref_idx_l1_active, modification) and that ref_idx_l1 / mvd_l1 / the list-1 vector predictor are used.")
        if mb["name"].startswith("B_") and mb["name"].endswith("8x8"):
            H.append("a B_8x8 macroblock: sub_mb_type decides per quarter which lists are used and how it is split (Table 7-18).")
    if mine_diff and mx <= 1:
        H.append("all differences are +-1: rounding (bi-prediction average, or the explicit-weight rounding term).")
    if mb is not None and mb["name"].startswith(("I_", "P_Skip")):
        H.append("the first wrong macroblock is not a B inter macroblock: look at what it reads from the neighbours or at the "
                 "deblocking filter (bS between B partitions with two vectors is the new part).")
    if not H:
        H.append("compare your --lists-dump and --mv-dump with the expected ones first (--print-expected lists|mv), then check the "
                 "prediction step (interpolate each list, average / weight, add residual, clip) and the deblocking bS rules for "
                 "blocks with two vectors.")
    L.append("hints:")
    L += ["  - " + x for x in H]
    return L


# ================================================================ main check
def run_check(path, yuv=None, parse_text=None, lists_text=None, mv_text=None, quiet=False, workdir=None, expected_yuv=None,
              truth=None, diag_budget=40.0, diag=True, trust=True, diag_candidates=None):
    td = workdir or tempfile.mkdtemp(prefix="bslice_")
    out = []

    def say(s):
        out.append(s)
        if not quiet:
            print(s)
    worst = 0
    try:
        t = truth or load_truth(path, td)
    except SystemExit as e:
        return 2, f"input problem: {e}"
    if trust and (parse_text is not None or lists_text is not None or mv_text is not None):
        probs = reference_trust(t, quiet=True)
        if probs:
            return 2, "\n".join(out + ["HARNESS PROBLEM (the reference disagrees with an independent source; this is not your bug):"]
                                + ["   " + p for p in probs[:8]])
    if yuv is not None:
        w, h, x0, y0 = geometry(t)
        ref_yuv = expected_yuv or os.path.join(td, "expected.yuv")
        if not expected_yuv:
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
            if res["first_diff"] and diag:
                say("---- diagnosis (reference decoder in spoiler_reference/) ----")
                for line in diagnose(t, ref_yuv, yuv, res, w, h, (x0, y0), td, diag_budget, candidates=diag_candidates):
                    say(line)
            say("PIXELS: output differs from ffmpeg's decode")
    if parse_text is not None:
        tmp = os.path.join(td, "reader_parse.txt")
        open(tmp, "w").write(parse_text)
        try:
            act_slices, act_hist = ir.parse_dump(tmp)
        except SystemExit:
            return 2, "\n".join(out + ["malformed parse dump (see the message above)"])
        exp_slices, exp_hist = expected_parse_structs(t)
        from common.report import Report
        rep = Report("parse")
        compare_parse(rep, exp_slices, exp_hist, act_slices, act_hist)
        for m_ in rep.failures[:12]:
            say("  FAIL: " + m_ if not m_.startswith("    ") else m_)
        if len(rep.failures) > 12:
            say(f"  ... and {len(rep.failures) - 12} more")
        nmb = sum(len(s["mbs"]) for s in exp_slices)
        say(f"PARSE DUMP: {len(exp_slices)} slices, {nmb} macroblocks compared: " + ("identical to the truth" if not rep.failures else "differs from the truth"))
        worst = max(worst, 1 if rep.failures else 0)
    if lists_text is not None:
        try:
            got = parse_lists(lists_text)
        except DumpError as e:
            return 2, "\n".join(out + [f"malformed lists dump: {e}"])
        m = t.lists = t.lists or lists_model(t)
        ns = lambda e: None if e is None else (str(e[0]), e[1], e[2])  # noqa: E731
        nr = lambda e: (str(e[0]), e[1])  # noqa: E731
        exp_s = {(r["pic"], r["slice"]): dict(r, l0=[ns(e) for e in r["l0"]], l1=[ns(e) for e in r["l1"]]) for r in m["slices"]}
        exp_d = {p: ([nr(e) for e in sh], [nr(e) for e in lg], mx) for p, sh, lg, mx in m["dpb"]}
        from common.report import Report
        rep = Report("lists")
        compare_lists(rep, (exp_s, exp_d), got)
        for m_ in rep.failures[:12]:
            say("  FAIL: " + m_ if not m_.startswith("    ") else m_)
        if len(rep.failures) > 12:
            say(f"  ... and {len(rep.failures) - 12} more")
        say(f"LISTS DUMP: {len(exp_s)} P/B slice(s), {len(exp_d)} picture(s): " + ("identical to the truth" if not rep.failures else "differs from the truth"))
        worst = max(worst, 1 if rep.failures else 0)
    if mv_text is not None:
        if t.direct:
            return 2, "\n".join(out + [f"the stream uses direct prediction ({t.direct}); the motion-vector channel starts in Chapter 7.9"])
        try:
            got = parse_mv(mv_text)
        except DumpError as e:
            return 2, "\n".join(out + [f"malformed mv dump: {e}"])
        from common.report import Report
        rep = Report("mv")
        compare_mv(rep, t, got)
        for m_ in rep.failures[:10]:
            say("  FAIL: " + m_)
        if len(rep.failures) > 10:
            say(f"  ... and {len(rep.failures) - 10} more")
        say(f"MV DUMP: {len(got)} macroblock line(s): " + ("identical to the truth" if not rep.failures else "differs from the truth"))
        worst = max(worst, 1 if rep.failures else 0)
    say(("PASS" if worst == 0 else "FAIL") + ": Chapter 7.8")
    return worst, "\n".join(out)


# ================================================================ self-test
def corrupt_text(text, kind):
    lines = text.splitlines()
    if kind == "parse_refidx1":
        i = next(k for k, l in enumerate(lines) if l.startswith("MB") and "name=B_L1_16x16" in l or ("name=B_Bi_16x16" in l and l.startswith("MB")))
        lines[i] = re.sub(r"mvd1=(-?\d+):", lambda m: f"mvd1={int(m.group(1)) + 1}:", lines[i], count=1)
    elif kind == "parse_name":
        i = next(k for k, l in enumerate(lines) if l.startswith("MB") and "name=B_L0_16x16" in l)
        lines[i] = lines[i].replace("name=B_L0_16x16", "name=B_L1_16x16")
    elif kind == "parse_drop_mb":
        i = next(k for k, l in enumerate(lines) if l.startswith("MB") and "name=B_" in l)
        del lines[i]
    elif kind == "parse_end":
        i = next(k for k, l in enumerate(lines) if l.startswith("END"))
        lines[i] = re.sub(r"end_bit=\d+", "end_bit=1", lines[i])
    elif kind == "parse_garbage":
        lines[3] = "MB x y"
    elif kind == "lists_swap1":
        i = next(k for k, l in enumerate(lines) if l.startswith("SLICE") and "type=B" in l and "list1=" in l and l.split("list1=")[1].count(",") >= 1)
        head, l1 = lines[i].split("list1=")
        e = l1.split(",")
        e[0], e[1] = e[1], e[0]
        lines[i] = head + "list1=" + ",".join(e)
    elif kind == "lists_poc":
        i = next(k for k, l in enumerate(lines) if l.startswith("SLICE") and "type=B" in l)
        lines[i] = re.sub(r"poc=(-?\d+)", lambda m: f"poc={int(m.group(1)) + 2}", lines[i], count=1)
    elif kind == "lists_drop_dpb":
        i = next(k for k, l in enumerate(lines) if l.startswith("DPB"))
        del lines[i]
    elif kind == "lists_nolist1":
        i = next(k for k, l in enumerate(lines) if l.startswith("SLICE") and "type=B" in l)
        lines[i] = lines[i].split(" active1=")[0]
    elif kind == "lists_garbage":
        lines[0] = "SLICE x"
    elif kind == "mv_value":
        i = next(k for k, l in enumerate(lines) if l.startswith("MV") and re.search(r"l1=\d+:", l))
        lines[i] = re.sub(r"(l1=\d+:)(-?\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), lines[i], count=1)
    elif kind == "mv_swap_lists":
        i = next(k for k, l in enumerate(lines) if l.startswith("MV") and re.search(r"l1=\d+:", l) and re.search(r"l0=\d+:", l))
        a, b, c, d = lines[i].split(" ", 3)
        l0, l1 = d.split(" ")
        lines[i] = f"{a} {b} {c} l0={l1[3:]} l1={l0[3:]}"
    elif kind == "mv_drop":
        del lines[len(lines) // 2]
    elif kind == "mv_garbage":
        lines[1] = "MV 1 2 3"
    return "\n".join(lines) + "\n"


PARSER_MUTANTS = ["b_sub_table", "b_8x16_swap", "b_bi_is_l0", "b_te_range_l0", "b_list_order", "b_mvd_interleaved", "b_swap_l0_l1",
                  "b_intra_offset22"]
PARSER_DOC = {
    "b_sub_table": "sub_mb_type 4..9: the 8x4 and 4x8 rows of Table 7-18 swapped",
    "b_8x16_swap": "16x8 and 8x16 partition shapes swapped in the B mb_type table",
    "b_bi_is_l0": "a Bi partition reads ref_idx / mvd for list 0 only",
    "b_te_range_l0": "ref_idx_l1 is read with the range of num_ref_idx_l0_active",
    "b_list_order": "list 1 syntax elements (ref_idx_l1, mvd_l1) before list 0",
    "b_mvd_interleaved": "mvd_l0 and mvd_l1 interleaved per partition (all mvd_l0 must come first)",
    "b_swap_l0_l1": "mb_type 1 and 2 (B_L0_16x16 / B_L1_16x16) swapped",
    "b_intra_offset22": "intra mb_type in B slices taken as mb_type - 22 (should be 23)",
}
LISTS_MUTANTS = [k for k in brefmgmt_ref.MUTANTS if k.startswith("b_")] + ["poc_no_msb"]
MV_MUTANTS = list(bdec.MVPRED_MUTANTS)
DECODER_MUTANTS = [k for k in bdec.MUTANTS if k not in ("poc_no_msb",)] + [k for k in bdec.ALL_MUTANTS if k.startswith("b_bs_")] + \
    ["ref_idx_compare", "mv_thr_5"]
NAMING_SAMPLE = ["bi_round_down", "l1_ref_from_l0", "b_bs_pair_by_list", "b_no_swap", "b_mvp_l1_uses_l0_nb", "bi_w_offset_sum", "poc_no_msb",
                 "output_decode_order"]


def write_yuv_of(pics, path):
    bdec.write_yuv(pics, path)


def self_test():
    td = tempfile.mkdtemp(prefix="bslice_selftest_")
    problems = []
    t_start = time.time()

    def sec(msg):
        print(f"{msg}  [t={time.time() - t_start:.0f}s]")

    def bad(msg):
        problems.append(msg)
        print("  PROBLEM:", msg)
    try:
        # ---------------------------------------------------------------- 1. inputs; the reference must reproduce ffmpeg
        sec("1. the reference decoder must reproduce ffmpeg byte for byte on every test stream; the reference parser, lists and "
              "vectors must agree with every independent source")
        streams = []
        for n in SYNTH:
            p = os.path.join(td, n + ".264")
            make_synth(n, p)
            streams.append(p)
        for n in ("b_bf1", "b_bf3", "b_pyr", "b_slices", "b_wpfade"):
            p = os.path.join(td, n + ".ts")
            make_variant(n, p)
            streams.append(p)
        streams.append(testfiles.get("T8"))
        stats = collections.Counter()
        info = {}
        for s in streams:
            name = os.path.basename(s)
            wd = os.path.join(td, "w_" + name)
            os.makedirs(wd)
            ry = os.path.join(wd, "expected.yuv")
            reference_yuv(s, ry)
            try:
                t = load_truth(s, wd)
            except SystemExit as e:
                bad(f"{name}: {e}")
                continue
            probs = reference_trust(t, quiet=True)
            if probs:
                bad(f"{name}: the reference disagrees with an independent source: {probs[:2]}")
                continue
            cache = {}
            y = os.path.join(wd, "ref.yuv")
            bdec.STATS = stats
            try:
                pics = bdec.decode(s, workdir=wd, cache=cache)
            except bdec.Unsupported as e:
                bad(f"{name}: the reference decoder cannot decode it ({e})")
                continue
            finally:
                bdec.STATS = None
            write_yuv_of(pics, y)
            code, text = run_check(s, y, quiet=True, workdir=wd, expected_yuv=ry, truth=t, trust=False)
            print(f"  {name:22s} {len(pics):3d} pictures: {'identical to ffmpeg' if code == 0 else 'DIFFERENT'}")
            if code != 0:
                bad(f"the reference decoder does not reproduce ffmpeg on {name}:\n{text[-500:]}")
                continue
            m = t.lists = lists_model(t)
            texts = dict(parse=ir.render_dump(t.results, 3), lists=render_lists(m), mv=render_mv(t))
            code, text = run_check(s, parse_text=texts["parse"], lists_text=texts["lists"], mv_text=texts["mv"], quiet=True, workdir=wd,
                                   truth=t, trust=False)
            if code != 0:
                bad(f"{name}: the expected dumps do not pass their own check:\n{text[-400:]}")
                continue
            info[s] = dict(name=name, wd=wd, ry=ry, t=t, cache=cache, ref_yuv=y, texts=texts)
            # coverage of the lists / pictures
            for r in m["slices"]:
                if r["type"] == "B":
                    stats["B slices"] += 1
            stats["IDR periods>1"] += max(x["period"] for x in t.meta.values()) > 1
            stats["POC above lsb range"] += any(x["raw"] >= 32 for x in t.meta.values())
            stats["non-reference B pictures"] += sum(1 for pi in t.order if t.pics[pi][0].info.tab["slice_type"] == 1
                                                     and t.pics[pi][0].info.tab["nal_ref_idc"] == 0)
            stats["reference B pictures"] += sum(1 for pi in t.order if t.pics[pi][0].info.tab["slice_type"] == 1
                                                 and t.pics[pi][0].info.tab["nal_ref_idc"] != 0)
            stats["B slices with list 1 modification"] += sum(1 for r in t.results if r.info.tab["slice_type"] == 1 and r.info.tab["mods1"])
            stats["long-term frame in a B list"] += sum(1 for r in m["slices"] if r["type"] == "B" and any(e and e[1] == "L" for e in r["l1"]))
        print("   coverage: " + ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
        need = ["partition L0", "partition L1", "partition Bi", "Bi same picture", "Bi two pictures", "explicit Bi", "explicit L1 only",
                "Bi with ref_idx > 0", "ref_idx_l1 > 0", "IDR periods>1", "POC above lsb range", "non-reference B pictures",
                "reference B pictures", "B slices with list 1 modification", "long-term frame in a B list"]
        for k in need:
            if not stats[k]:
                bad(f"no test stream exercises '{k}'")
        usable = [s for s in streams if s in info]
        if not usable:
            return finish_self_test(problems, t_start)
        # ---------------------------------------------------------------- 2. corrupted pixel outputs
        sec("2. corrupted pixel outputs must be rejected")
        s = next(x for x in usable if x.endswith("synth_types.264"))
        I = info[s]
        data = bytearray(open(I["ref_yuv"], "rb").read())
        fs = 96 * 80 * 3 // 2
        swapped = bytearray(data)
        swapped[fs:2 * fs], swapped[2 * fs:3 * fs] = data[2 * fs:3 * fs], data[fs:2 * fs]
        for label, mod in (("one luma sample changed", lambda d: d.__setitem__(5 * fs + 700, d[5 * fs + 700] ^ 1)),
                           ("last byte missing", lambda d: d.pop()),
                           ("one chroma sample changed", lambda d: d.__setitem__(3 * fs + 96 * 80 + 5, d[3 * fs + 96 * 80 + 5] ^ 4)),
                           ("frames 1 and 2 swapped (wrong output order)", lambda d: d.__setitem__(slice(fs, 3 * fs), swapped[fs:3 * fs]))):
            d = bytearray(data)
            mod(d)
            fp = os.path.join(td, "corrupt.yuv")
            open(fp, "wb").write(bytes(d))
            code, txt = run_check(s, fp, quiet=True, workdir=I["wd"], expected_yuv=I["ry"], truth=I["t"], trust=False, diag_budget=5,
                                  diag_candidates=["output_decode_order"])
            print(f"   {label}: exit {code}")
            if code != 1:
                bad(f"corrupted output ({label}) not rejected (exit {code})")
            if "swapped" in label and "wrong ORDER" not in txt:
                bad("the diagnosis does not recognise frames written in the wrong order")
        code, _ = run_check(s, os.path.join(td, "nonexistent.yuv"), quiet=True, workdir=I["wd"], expected_yuv=I["ry"], truth=I["t"], trust=False)
        print(f"   missing output file: exit {code}")
        if code != 2:
            bad("missing output must give exit 2")
        # ---------------------------------------------------------------- 3. corrupted dumps
        sec("3. the optional channels: truth passes, corrupted dumps fail")
        s = next(x for x in usable if x.endswith("synth_lists.264"))
        I = info[s]
        for kind, ch, want in (("parse_refidx1", "parse", 1), ("parse_name", "parse", 1), ("parse_drop_mb", "parse", 1),
                               ("parse_end", "parse", 1), ("parse_garbage", "parse", 2),
                               ("lists_swap1", "lists", 1), ("lists_poc", "lists", 1), ("lists_drop_dpb", "lists", 1),
                               ("lists_nolist1", "lists", 1), ("lists_garbage", "lists", 2),
                               ("mv_value", "mv", 1), ("mv_swap_lists", "mv", 1), ("mv_drop", "mv", 1), ("mv_garbage", "mv", 2)):
            txt = corrupt_text(I["texts"][ch], kind)
            kw = {ch + "_text": txt}
            code, out = run_check(s, quiet=True, workdir=I["wd"], truth=I["t"], trust=False, **kw)
            print(f"   {kind}: exit {code}")
            if code != want:
                bad(f"corrupted dump ({kind}) gave exit {code}, expected {want}: {out[-200:]}")
        # ---------------------------------------------------------------- 4. wrong rules
        order = [x for x in usable if x.endswith(".264")] + [x for x in usable if not x.endswith(".264") and not x.endswith("T8.ts")]
        sec(f"4. {len(PARSER_MUTANTS)} wrong parsing rules: each must make the parse-dump check fail on some stream")
        for m in PARSER_MUTANTS:
            caught = None
            for s in order:
                I = info[s]
                ir.MUTANT = m
                try:
                    res = ir.parse_all(I["t"].infos, 3)
                    text = ir.render_dump(res, 3)
                except Exception as e:  # noqa: BLE001
                    text = None
                finally:
                    ir.MUTANT = None
                if text is None:
                    caught = os.path.basename(s)
                    break
                code, _ = run_check(s, parse_text=text, quiet=True, workdir=I["wd"], truth=I["t"], trust=False)
                if code == 1:
                    caught = os.path.basename(s)
                    break
            print(f"   {m:20s} {'caught on ' + caught if caught else 'NOT CAUGHT'}")
            if not caught:
                bad(f"wrong parsing rule {m!r} was not detected by any test stream")
        sec(f"5. {len(LISTS_MUTANTS)} wrong list / POC rules: caught by the lists dump check AND by the pixels")
        for m in LISTS_MUTANTS:
            c_l = c_p = None
            for s in order:
                I = info[s]
                if not c_l:
                    code, _ = run_check(s, lists_text=render_lists(lists_model(I["t"], mutant=m)), quiet=True, workdir=I["wd"],
                                        truth=I["t"], trust=False)
                    if code == 1:
                        c_l = os.path.basename(s)
                if not c_p:
                    c_p = pixel_catch(s, I, m)
                if c_l and c_p:
                    break
            print(f"   {m:22s} lists dump: {c_l or 'NOT CAUGHT'}; pixels: {c_p or 'NOT CAUGHT'}")
            if not c_l:
                bad(f"wrong rule {m!r} not detected by the lists dump check")
            if not c_p:
                bad(f"wrong rule {m!r} not visible in the pixels of any test stream")
        sec(f"6. {len(MV_MUTANTS)} wrong vector-prediction rules: caught by the mv dump check AND by the pixels")
        for m in MV_MUTANTS:
            c_v = c_p = None
            for s in order:
                I = info[s]
                if not c_v:
                    bmvpred_ref.MUTANT = m
                    try:
                        bmvpred_ref.fill_results(I["t"].results)
                        text = render_mv(I["t"])
                    finally:
                        bmvpred_ref.MUTANT = None
                        bmvpred_ref.fill_results(I["t"].results)
                    code, _ = run_check(s, mv_text=text, quiet=True, workdir=I["wd"], truth=I["t"], trust=False)
                    if code == 1:
                        c_v = os.path.basename(s)
                if not c_p:
                    c_p = pixel_catch(s, I, m)
                if c_v and c_p:
                    break
            print(f"   {m:26s} mv dump: {c_v or 'NOT CAUGHT'}; pixels: {c_p or 'NOT CAUGHT'}")
            if not c_v:
                bad(f"wrong rule {m!r} not detected by the mv dump check")
            if not c_p:
                bad(f"wrong rule {m!r} not visible in the pixels of any test stream")
        sec(f"7. {len(DECODER_MUTANTS)} wrong prediction / deblocking / DPB / output rules: caught by the pixels")
        for m in DECODER_MUTANTS:
            c = None
            for s in order:
                c = pixel_catch(s, info[s], m)
                if c:
                    break
            if not c:
                for s in usable:
                    if s in order:
                        continue
                    c = pixel_catch(s, info[s], m)
                    if c:
                        break
            print(f"   {m:26s} {'changes the pixels of ' + c if c else 'NOT VISIBLE'}")
            if not c:
                bad(f"wrong rule {m!r} was not detected by any test stream")
        sec("8. the diagnosis names the wrong rule (sample)")
        for m in NAMING_SAMPLE:
            named = None
            for s in order:
                I = info[s]
                y = os.path.join(I["wd"], "mut.yuv")
                bdec.MUTANT = m
                try:
                    write_yuv_of(bdec.decode(s, workdir=I["wd"], cache=I["cache"]), y)
                except Exception:  # noqa: BLE001
                    continue
                finally:
                    bdec.MUTANT = None
                code, txt = run_check(s, y, quiet=True, workdir=I["wd"], expected_yuv=I["ry"], truth=I["t"], trust=False, diag_budget=30,
                                      diag_candidates=NAMING_SAMPLE)
                if code == 1:
                    named = (os.path.basename(s), f"- {m}:" in txt)
                    break
            print(f"   {m:26s} {('on ' + named[0] + ': ' + ('named' if named[1] else 'NOT NAMED')) if named else 'not visible'}")
            if not named or not named[1]:
                bad(f"the diagnosis does not name the wrong rule {m!r}")
    finally:
        bdec.MUTANT = None
        bdec.STATS = None
        ir.MUTANT = None
        bmvpred_ref.MUTANT = None
        shutil.rmtree(td, ignore_errors=True)
    return finish_self_test(problems, t_start)


def pixel_catch(s, I, m):
    """Name of the stream if the reference decoder with wrong rule m differs from ffmpeg on it (checked through run_check)."""
    y = os.path.join(I["wd"], "mut.yuv")
    bdec.MUTANT = m
    try:
        write_yuv_of(bdec.decode(s, workdir=I["wd"], cache=I["cache"]), y)
    except bdec.Unsupported:
        return os.path.basename(s) + " (rejected as unsupported)"
    except Exception:  # noqa: BLE001
        return os.path.basename(s) + " (decoder failure)"
    finally:
        bdec.MUTANT = None
    code, _ = run_check(s, y, quiet=True, workdir=I["wd"], expected_yuv=I["ry"], truth=I["t"], trust=False, diag=False)
    return os.path.basename(s) if code == 1 else None


def finish_self_test(problems, t_start):
    print(f"(self-test took {time.time() - t_start:.0f} s)")
    if problems:
        print(f"SELF-TEST FAIL: {len(problems)} problem(s)")
        return 1
    print("SELF-TEST PASS")
    return 0


# ================================================================ command line
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="input .ts / .264 (CAVLC stream with I, P and B slices; no direct prediction for the "
                    "dump channels)")
    ap.add_argument("output", nargs="?", help="your decoded frames, raw yuv420p, cropped, display order")
    ap.add_argument("--parse-dump", metavar="FILE", help="your macroblock dump (Chapter 7.1 grammar + refidx1 / mvd1)")
    ap.add_argument("--lists-dump", metavar="FILE", help="your reference-list log (Chapter 7.6 grammar + type=B, poc=, list1=)")
    ap.add_argument("--mv-dump", metavar="FILE", help="your final motion vectors, both lists")
    ap.add_argument("--print-expected", nargs=3, metavar=("KIND", "INPUT", "OUT"), help="write the expected parse|lists|mv text of INPUT")
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
        if kind not in ("parse", "lists", "mv") or not os.path.isfile(inp):
            die("error: --print-expected needs KIND in parse|lists|mv and an existing input file")
        td = tempfile.mkdtemp(prefix="bslice_")
        try:
            t = load_truth(inp, td)
            if kind == "parse":
                text = ir.render_dump(t.results, 3)
            elif kind == "lists":
                text = render_lists(lists_model(t))
            else:
                if t.direct:
                    die(f"the stream uses direct prediction ({t.direct}); the motion-vector channel starts in Chapter 7.9")
                text = render_mv(t)
        except SystemExit as e:
            die(f"error: {e}")
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
    if not (a.output or a.parse_dump or a.lists_dump or a.mv_dump):
        die("error: need <your_output.yuv> and/or --parse-dump / --lists-dump / --mv-dump")
    texts = {}
    for k, f in (("parse_text", a.parse_dump), ("lists_text", a.lists_dump), ("mv_text", a.mv_dump)):
        if f:
            if not os.path.isfile(f):
                die(f"error: {f!r} not found")
            texts[k] = open(f, errors="replace").read()
    td = tempfile.mkdtemp(prefix="bslice_")
    try:
        code, msg = run_check(a.input, a.output, workdir=td, **texts)
        if code == 2:
            print(msg, file=sys.stderr)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
