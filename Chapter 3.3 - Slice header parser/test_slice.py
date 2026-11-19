#!/usr/bin/env python3
"""Harness for Chapter 3.3 (slice header parser).

    python3 test_slice.py <input.ts|input.264> <slice_dump.txt>
    python3 test_slice.py --make-variants <out_dir>      # write extra test inputs (see Readme)
    python3 test_slice.py --self-test

<input> is any ffmpeg-produced H.264 MPEG-TS file or a raw Annex B .264 file.
<slice_dump.txt> is your program's output (grammar in the Readme, section 5): for every slice NAL
unit (nal_unit_type 1 and 5) in stream order a block

    SLICE <ordinal>
    <syntax element name> <value>          (exactly the names/order ffmpeg's trace_headers prints)
    ...
    DERIVED <name> <value>                 (derived values, see Readme)
    ERROR <free text>                      (only if the slice cannot be parsed, see Readme)

Truth: ffmpeg `-bsf:v trace_headers` (slice header blocks of NAL types 1 and 5; SPS and PPS blocks of
the same trace give the context), plus values derived from those by this harness.  The CABAC
alignment bits are additionally checked against the raw bits of the stream itself.

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
import slicegen  # noqa: E402

FIELD_RE = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
PREFIX_RE = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header")
# Lines trace_headers prints that are not slice header syntax elements of 7.3.3 (NAL header,
# CABAC alignment bits: those are reported through DERIVED lines).
IGNORED = {"forbidden_zero_bit", "nal_ref_idc", "nal_unit_type", "cabac_alignment_one_bit"}
SLICE_NAMES = ["P", "B", "I", "SP", "SI"]
DERIVED_NAMES = ["nal_unit_type", "nal_ref_idc", "slice_type_mod5", "slice_type_name", "slice_type_all_same",
                 "sps_id", "chroma_array_type", "entropy_coding_mode_flag", "frame_num_bits", "field_pic",
                 "bottom_field", "mbaff_frame", "first_mb_addr", "num_ref_idx_l0_active",
                 "num_ref_idx_l1_active", "num_rplm_l0", "num_rplm_l1", "weight_mode", "num_mmco",
                 "slice_qp", "deblocking_idc", "filter_offset_a", "filter_offset_b", "header_end_bit",
                 "cabac_alignment_bits", "cabac_alignment_ok"]


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ================================================================ truth from trace_headers
class Block:
    def __init__(self, title, extradata):
        self.title, self.extradata = title, extradata
        self.items = []          # (bitpos, name, nbits, value)

    @property
    def fields(self):
        return [(n, v) for _, n, _, v in self.items]


def parse_trace(path):
    """-> list of Block (SPS / PPS / slice header), in trace order.  Blocks before the first
    'Packet:' line are the extradata copies (flagged)."""
    blocks, cur, started = [], None, False
    with open(path, "r", errors="replace") as f:
        for raw in f:
            if (raw.startswith("[") and not raw.startswith("[trace_headers")) or raw.startswith("    Last message"):
                continue        # decoder / muxer log noise interleaved by stderr: not part of the trace
            line = PREFIX_RE.sub("", raw.rstrip("\n"))
            if line.startswith("Packet:"):
                started, cur = True, None
                continue
            m = FIELD_RE.match(line)
            if m and cur is not None:
                cur.items.append((int(m.group(1)), m.group(2), len(m.group(3)), int(m.group(4))))
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


class Truth:
    """One slice's truth: syntax fields, derived lines, context."""
    pass


def slice_truths(blocks):
    """-> list of Truth, one per slice NAL (types 1, 5), in stream order."""
    sps_t, pps_t, out = {}, {}, []
    for b in blocks:
        d = first_values(b.fields)
        if b.title == "Sequence Parameter Set":
            sps_t[d["seq_parameter_set_id"]] = d
        elif b.title == "Picture Parameter Set":
            pps_t[d["pic_parameter_set_id"]] = d
        elif b.extradata:
            continue
        else:
            if d.get("nal_unit_type") not in (1, 5):
                continue
            pid = d.get("pic_parameter_set_id")
            if pid not in pps_t or pps_t[pid]["seq_parameter_set_id"] not in sps_t:
                die(f"input problem: slice #{len(out)} uses PPS {pid} which is not available in the stream "
                    f"before it; the harness has no truth for such a stream")
            pps = pps_t[pid]
            sps = sps_t[pps["seq_parameter_set_id"]]
            t = Truth()
            t.nal_type, t.ref_idc = d["nal_unit_type"], d["nal_ref_idc"]
            t.sps, t.pps = sps, pps
            t.fields = [(n, v) for n, v in b.fields if n not in IGNORED]
            t.items = b.items
            last = b.items[-1]
            t.end_bit = last[0] + last[2]
            t.nalign = sum(1 for it in b.items if it[1] == "cabac_alignment_one_bit")
            t.derived = derive(t, sps, pps)
            out.append(t)
    return out


def derive(t, sps, pps):
    d = first_values(t.fields)
    st = d["slice_type"]
    m = st % 5
    cfi = sps.get("chroma_format_idc", 1)
    sep = sps.get("separate_colour_plane_flag", 0)
    field = d.get("field_pic_flag", 0)
    mbaff = 1 if sps.get("mb_adaptive_frame_field_flag", 0) and not field else 0
    ov = d.get("num_ref_idx_active_override_flag", 0)
    n0 = n1 = 0
    if m in (0, 1, 3):
        n0 = d["num_ref_idx_l0_active_minus1"] + 1 if ov else pps["num_ref_idx_l0_default_active_minus1"] + 1
    if m == 1:
        n1 = d["num_ref_idx_l1_active_minus1"] + 1 if ov else pps["num_ref_idx_l1_default_active_minus1"] + 1
    rplm, cur = [0, 0], 0
    nmmco = 0
    for n, v in t.fields:
        if n == "ref_pic_list_modification_flag_l0":
            cur = 0
        elif n == "ref_pic_list_modification_flag_l1":
            cur = 1
        elif n == "modification_of_pic_nums_idc" and v != 3:
            rplm[cur] += 1
        elif n == "memory_management_control_operation" and v != 0:
            nmmco += 1
    if m == 1:
        wm = {1: 1, 2: 2}.get(pps["weighted_bipred_idc"], 0)
    elif m in (0, 3):
        wm = 1 if pps["weighted_pred_flag"] else 0
    else:
        wm = 0
    der = [("nal_unit_type", t.nal_type), ("nal_ref_idc", t.ref_idc), ("slice_type_mod5", m),
           ("slice_type_name", SLICE_NAMES[m]), ("slice_type_all_same", 1 if st >= 5 else 0),
           ("sps_id", pps["seq_parameter_set_id"]), ("chroma_array_type", 0 if sep else cfi),
           ("entropy_coding_mode_flag", pps["entropy_coding_mode_flag"]),
           ("frame_num_bits", sps["log2_max_frame_num_minus4"] + 4), ("field_pic", field),
           ("bottom_field", d.get("bottom_field_flag", 0)), ("mbaff_frame", mbaff),
           ("first_mb_addr", d["first_mb_in_slice"] * (1 + mbaff)),
           ("num_ref_idx_l0_active", n0), ("num_ref_idx_l1_active", n1),
           ("num_rplm_l0", rplm[0]), ("num_rplm_l1", rplm[1]), ("weight_mode", wm), ("num_mmco", nmmco),
           ("slice_qp", 26 + pps["pic_init_qp_minus26"] + d["slice_qp_delta"]),
           ("deblocking_idc", d.get("disable_deblocking_filter_idc", 0)),
           ("filter_offset_a", 2 * d.get("slice_alpha_c0_offset_div2", 0)),
           ("filter_offset_b", 2 * d.get("slice_beta_offset_div2", 0)),
           ("header_end_bit", t.end_bit), ("cabac_alignment_bits", t.nalign), ("cabac_alignment_ok", 1)]
    assert [n for n, _ in der] == DERIVED_NAMES
    return [f"DERIVED {n} {v}" for n, v in der]


def render_reader_output(truths):
    out = []
    for i, t in enumerate(truths):
        out.append(f"SLICE {i}")
        out += [f"{n} {v}" for n, v in t.fields]
        out += t.derived
    return "\n".join(out) + "\n"


# ================================================================ the stream itself (alignment check)
def nal_units(data):
    """Annex B scan -> list of NAL unit byte strings (own, minimal implementation)."""
    pos = [m.start() for m in re.finditer(b"\x00\x00\x01", data)]
    out = []
    for i, p in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else len(data)
        nal = data[p + 3:end]
        out.append(nal.rstrip(b"\x00"))      # trailing zeros belong to the next start code / padding
    return [n for n in out if n]


def unescape(nal):
    out, zeros = bytearray(), 0
    for x in nal:
        if zeros >= 2 and x == 3:
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


def stream_slice_rbsps(path, td):
    raw = path
    if not path.lower().endswith((".264", ".h264")):
        raw = os.path.join(td, "raw.264")
        ffref.raw_h264(path, raw)
    data = open(raw, "rb").read()
    return [unescape(n) for n in nal_units(data) if n[0] & 31 in (1, 5)]


def bit_at(rbsp, pos):
    return (rbsp[pos >> 3] >> (7 - (pos & 7))) & 1


def check_alignment_in_stream(rbsp, t):
    """-> error string or None.  Independent of the trace: reads the real bits."""
    cabac = t.pps["entropy_coding_mode_flag"]
    before = t.end_bit - t.nalign
    if t.end_bit > 8 * len(rbsp):
        return f"header end bit {t.end_bit} is beyond the {8 * len(rbsp)} bits of the NAL unit"
    if not cabac:
        return None if t.nalign == 0 else "CAVLC slice with alignment bits"
    if t.end_bit % 8:
        return f"CABAC slice data starts at bit {t.end_bit}, not byte aligned"
    if t.nalign != (-before) % 8:
        return f"{t.nalign} alignment bits after bit {before}, expected {(-before) % 8}"
    for p in range(before, t.end_bit):
        if not bit_at(rbsp, p):
            return f"cabac_alignment_one_bit at bit {p} is 0 in the stream"
    return None


# ================================================================ hints for failure messages
SECTION_PREFIX = [
    ("first_mb_in_slice slice_type pic_parameter_set_id", "slice header start (first_mb_in_slice, slice_type, pic_parameter_set_id)"),
    ("colour_plane_id frame_num field_pic_flag bottom_field_flag idr_pic_id",
     "picture identification (colour_plane_id, frame_num, field flags, idr_pic_id)"),
    ("pic_order_cnt_lsb delta_pic_order_cnt_bottom delta_pic_order_cnt", "picture order count fields"),
    ("redundant_pic_cnt direct_spatial_mv_pred_flag num_ref_idx",
     "redundant_pic_cnt / direct_spatial_mv_pred_flag / reference count override"),
    ("long_term_pic_num", "ref_pic_list_modification (7.3.3.1) or dec_ref_pic_marking (7.3.3.3)"),
    ("ref_pic_list_modification_flag modification_of_pic_nums_idc abs_diff_pic_num_minus1",
     "ref_pic_list_modification (7.3.3.1)"),
    ("luma_log2_weight_denom chroma_log2_weight_denom luma_weight luma_offset chroma_weight chroma_offset",
     "pred_weight_table (7.3.3.2)"),
    ("no_output_of_prior_pics_flag long_term_reference_flag adaptive_ref_pic_marking_mode_flag "
     "memory_management_control_operation difference_of_pic_nums_minus1 long_term_frame_idx "
     "max_long_term_frame_idx_plus1", "dec_ref_pic_marking (7.3.3.3)"),
    ("cabac_init_idc slice_qp_delta sp_for_switch_flag slice_qs_delta", "cabac_init_idc / slice QP fields"),
    ("disable_deblocking_filter_idc slice_alpha_c0_offset_div2 slice_beta_offset_div2", "deblocking fields"),
]


def section_of(name):
    base = name.split("[")[0]
    for prefixes, label in SECTION_PREFIX:
        if any(base.startswith(p) for p in prefixes.split()):
            return label
    return "unknown section"


def context_of(t):
    s, p = t.sps, t.pps
    return (f"nal_unit_type {t.nal_type}, nal_ref_idc {t.ref_idc}; PPS {p['pic_parameter_set_id']}: "
            f"entropy_coding_mode_flag {p['entropy_coding_mode_flag']}, weighted_pred_flag "
            f"{p['weighted_pred_flag']}, weighted_bipred_idc {p['weighted_bipred_idc']}, "
            f"bottom_field_pic_order_in_frame_present_flag {p['bottom_field_pic_order_in_frame_present_flag']}, "
            f"redundant_pic_cnt_present_flag {p['redundant_pic_cnt_present_flag']}, "
            f"deblocking_filter_control_present_flag {p['deblocking_filter_control_present_flag']}; "
            f"SPS {p['seq_parameter_set_id']}: frame_mbs_only_flag {s['frame_mbs_only_flag']}, "
            f"pic_order_cnt_type {s['pic_order_cnt_type']}, log2_max_frame_num_minus4 "
            f"{s['log2_max_frame_num_minus4']}, chroma_format_idc {s.get('chroma_format_idc', 1)}, "
            f"separate_colour_plane_flag {s.get('separate_colour_plane_flag', 0)}")


# ================================================================ reader output
def parse_reader(path):
    """-> list of dict(fields=[(name,int)], derived=[str], error=str|None) one per SLICE block."""
    try:
        text = open(path, "r", errors="replace").read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    blocks, cur = [], None
    for ln, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tok = s.split()
        if tok[0] == "SLICE" and len(tok) == 2:
            cur = dict(fields=[], derived=[], error=None)
            blocks.append(cur)
            continue
        if cur is None:
            die(f"{path}:{ln}: expected a 'SLICE <n>' line before any field, got {s!r}")
        if tok[0] == "ERROR":
            cur["error"] = " ".join(tok[1:]) or "(no text)"
            continue
        if tok[0] == "DERIVED":
            if len(tok) != 3:
                die(f"{path}:{ln}: malformed DERIVED line {s!r} (expected 'DERIVED <name> <value>')")
            cur["derived"].append(f"DERIVED {tok[1]} {tok[2]}")
            continue
        if len(tok) != 2:
            die(f"{path}:{ln}: expected '<name> <integer value>', got {s!r}")
        try:
            v = int(tok[1])
        except ValueError:
            die(f"{path}:{ln}: value is not an integer: {s!r}")
        if tok[0] not in IGNORED:
            cur["fields"].append((tok[0], v))
    if not blocks:
        die(f"{path}: no 'SLICE <n>' blocks found")
    return blocks


# ================================================================ checking
def compare_fields(rep, k, t, actual):
    expected = t.fields
    head = f"SLICE #{k} (slice_type {first_values(expected)['slice_type']})"
    n = min(len(expected), len(actual))
    for i in range(n):
        if expected[i] != actual[i]:
            en, ev = expected[i]
            an, av = actual[i]
            ctx = "; ".join(f"{a} {b}" for a, b in expected[max(0, i - 4):i]) or "(start of header)"
            what = (f"field name: expected '{en}', got '{an} {av}'" if en != an
                    else f"value of '{en}': expected {ev}, got {av}")
            rep.check(False, f"{head} field #{i}: {what}\n         expected syntax section: {section_of(en)}\n"
                             f"         previous fields (all matched): {ctx}\n         context: {context_of(t)}")
            return False
    if len(expected) != len(actual):
        if len(actual) < len(expected):
            en, ev = expected[len(actual)]
            what = f"your header block ends early; next expected field is '{en}' (= {ev}), section: {section_of(en)}"
        else:
            an = actual[len(expected)][0]
            what = f"extra field(s) after the last expected one: '{an}' ({section_of(an)})"
        rep.check(False, f"{head}: {what} (expected {len(expected)} fields, got {len(actual)})\n"
                         f"         context: {context_of(t)}")
        return False
    rep.check(True, "")
    return True


def compare_derived(rep, k, expected, actual):
    if expected == actual:
        rep.check(True, "")
        return
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else "(nothing)"
        a = actual[i] if i < len(actual) else "(nothing)"
        if e != a:
            rep.check(False, f"SLICE #{k} derived line #{i}: expected '{e}', got '{a}'")
            return


def run_check(path, dump, title="Slice header parser"):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input not found: {path}")
    reader = parse_reader(dump)
    with tempfile.TemporaryDirectory() as td:
        tr = os.path.join(td, "trace.txt")
        rc = ffref.trace_headers(path, tr)
        blocks = parse_trace(tr)
        errs = [PREFIX_RE.sub("", l.strip()) for l in open(tr, errors="replace")
                if "Failed to read unit" in l or "not available" in l or "but must be in" in l]
        if errs:
            die(f"ffmpeg trace_headers could not parse all of {path} (exit {rc}), so there is no complete truth "
                f"to compare against:\n  " + "\n  ".join(errs[:5]))
        truths = slice_truths(blocks)
        if not truths:
            die(f"ffmpeg trace_headers found no slice in {path}")
        rbsps = stream_slice_rbsps(path, td)
    rep = Report(title)
    rep.check(len(truths) == len(reader),
              f"number of SLICE blocks: expected {len(truths)} (one per slice NAL unit of type 1 or 5, in stream "
              f"order), got {len(reader)}; blocks are paired in order, so after a missing or extra block "
              f"the following blocks fail too: fix this first")
    if len(rbsps) != len(truths):
        die(f"internal error: {len(rbsps)} slice NAL units in the stream but {len(truths)} in the trace")
    for k, (t, act) in enumerate(zip(truths, reader)):
        err = check_alignment_in_stream(rbsps[k], t)
        if err:
            die(f"input problem: slice #{k}: {err} (the stream itself is not a valid CABAC slice header "
                f"according to ffmpeg's parse, so the harness has no truth)")
        ok = compare_fields(rep, k, t, act["fields"])
        rep.check(act["error"] is None, f"SLICE #{k}: unexpected ERROR line ({act['error']!r}); the SPS/PPS of "
                                         f"this slice are in the stream")
        if ok:
            compare_derived(rep, k, t.derived, act["derived"])
    return rep


# ================================================================ test inputs / variants
def x264_clip(out, opts, size="352x288", frames=30):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
           "-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264", *opts, "-f", "mpegts", out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die("ffmpeg failed: " + r.stderr.decode()[-500:])


def make_variants(outdir):
    ffref.require_tools()
    os.makedirs(outdir, exist_ok=True)
    data, exp = slicegen.make_stream()
    with open(os.path.join(outdir, "slice_variants.264"), "wb") as f:
        f.write(data)
    print(f"slice_variants.264: {len(slicegen.SPSS)} synthetic SPSs, {len(slicegen.PPSS)} synthetic PPSs, then "
          f"{len(exp)} synthetic slice headers (random but reproducible; random filler instead of slice data)")
    for s in slicegen.SPSS:
        print("  SPS", {k: v for k, v in s.items()})
    for p in slicegen.PPSS:
        print("  PPS", {k: v for k, v in p.items()})
    print("  (halfway through, PPS 2 and PPS 1 are re-sent with different content)")
    x264_clip(os.path.join(outdir, "slice_multi.ts"),
              ["-profile:v", "high", "-x264-params", "slices=3:bframes=3:ref=5:weightp=2:b-pyramid=normal:cabac=1"])
    print("slice_multi.ts: x264, 3 slices per picture (first_mb_in_slice > 0), 5 refs, weighted P, CABAC, B pyramid")
    x264_clip(os.path.join(outdir, "slice_cavlc_multi.ts"),
              ["-profile:v", "main", "-x264-params",
               "slices=4:bframes=2:ref=3:weightp=2:cabac=0:direct=temporal:weightb=1:b-pyramid=strict"])
    print("slice_cavlc_multi.ts: the same ideas with CAVLC, 4 slices per picture, temporal direct, implicit B weights")
    print("Also useful (from common.testfiles): T1, T4 (4 slices), T5, T6, T7 (weighted P, reordering), T8, "
          "T9_spatial, T9_temporal, T10_full (CABAC), T12 (MBAFF).")
    print("Coverage note: x264 never writes field pictures, long-term references, MMCO 2-6, explicit weights for B, "
          "POC types 1/2, redundant slices, SP/SI or separate colour planes. slice_variants.264 covers them, "
          "verified against trace_headers only (no decoder is involved).")


# ================================================================ self test
def run_in_subprocess(inp, dump):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), inp, dump], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=300)
    for l in (r.stdout.decode() + r.stderr.decode()).splitlines()[-6:]:
        print("   |", l[:200])
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
        names = ("T1", "T4", "T5", "T6", "T7", "T8", "T9_spatial", "T9_temporal", "T10_full", "T12")
        inputs = [testfiles.get(n) for n in names]
        inputs += [os.path.join(vdir, n) for n in ("slice_variants.264", "slice_multi.ts", "slice_cavlc_multi.ts")]
        seen, cache = set(), {}
        for inp in inputs:
            tr = os.path.join(td, "trace.txt")
            ffref.trace_headers(inp, tr)
            tt = slice_truths(parse_trace(tr))
            cache[inp] = tt
            for t in tt:
                seen.update(n for n, _ in t.fields)
            good = os.path.join(td, "good.txt")
            open(good, "w").write(render_reader_output(tt))
            print(f"-- {os.path.basename(inp)}: {len(tt)} slice block(s), known-good output must PASS")
            if run_in_subprocess(inp, good) != 0:
                ok_all = False
                print("   UNEXPECTED FAIL")
        # the writer must agree with ffmpeg (so the variants really contain what they claim to)
        vinp = os.path.join(vdir, "slice_variants.264")
        _, exp = slicegen.make_stream()
        tt = cache[vinp]
        bad = [i for i, (e, t) in enumerate(zip(exp, tt))
               if e[0] != [(n, v) for n, v in t.fields if n not in IGNORED] or e[1] != t.end_bit]
        print(f"-- slicegen writer vs ffmpeg: {len(exp)} headers written, {len(tt)} parsed, "
              f"{len(bad)} differ" + (f" (first: #{bad[0]})" if bad else ""))
        if bad or len(exp) != len(tt):
            ok_all = False
        for needed in ("field_pic_flag", "bottom_field_flag", "colour_plane_id", "idr_pic_id", "pic_order_cnt_lsb",
                       "delta_pic_order_cnt_bottom", "delta_pic_order_cnt[0]", "delta_pic_order_cnt[1]",
                       "redundant_pic_cnt", "direct_spatial_mv_pred_flag", "num_ref_idx_l1_active_minus1",
                       "ref_pic_list_modification_flag_l1", "long_term_pic_num", "abs_diff_pic_num_minus1",
                       "luma_weight_l1[0]", "chroma_offset_l1[0][1]", "chroma_log2_weight_denom",
                       "no_output_of_prior_pics_flag", "long_term_reference_flag",
                       "difference_of_pic_nums_minus1", "long_term_frame_idx", "max_long_term_frame_idx_plus1",
                       "cabac_init_idc", "sp_for_switch_flag", "slice_qs_delta", "slice_alpha_c0_offset_div2"):
            if needed not in seen:
                ok_all = False
                print(f"self-test coverage problem: no slice with {needed}")
        mmco = set()
        for t in cache[vinp]:
            mmco.update(v for n, v in t.fields if n == "memory_management_control_operation")
        if mmco != {0, 1, 2, 3, 4, 5, 6}:
            ok_all = False
            print(f"self-test coverage problem: MMCO commands seen {sorted(mmco)}")
        # the independent alignment check must notice a 0 bit
        with tempfile.TemporaryDirectory() as td2:
            inp = testfiles.get("T10_full")
            rb = stream_slice_rbsps(inp, td2)
            t0 = cache[inp][0]
            r0 = check_alignment_in_stream(rb[0], t0)
            flipped = bytearray(rb[0])
            p = t0.end_bit - 1
            flipped[p >> 3] &= ~(1 << (7 - (p & 7))) & 0xFF
            r1 = check_alignment_in_stream(bytes(flipped), t0)
            print(f"-- stream alignment check: intact -> {r0}, bit {p} cleared -> {r1}")
            if r0 is not None or r1 is None:
                ok_all = False
                print("   UNEXPECTED")
        # corruptions
        inp = os.path.join(vdir, "slice_variants.264")
        lines = render_reader_output(cache[vinp]).splitlines()

        def find(prefix, nth=0):
            return [i for i, l in enumerate(lines) if l.startswith(prefix)][nth]

        corrupt = {}
        c = list(lines)
        c[find("frame_num ", 5)] = "frame_num 0" if lines[find("frame_num ", 5)] != "frame_num 0" else "frame_num 1"
        corrupt["wrong frame_num"] = c
        c = list(lines)
        del c[find("luma_log2_weight_denom ")]
        corrupt["missing luma_log2_weight_denom"] = c
        c = list(lines)
        del c[find("bottom_field_flag ", 2)]
        corrupt["missing bottom_field_flag"] = c
        c = list(lines)
        del c[find("memory_management_control_operation 0", 0)]
        corrupt["missing MMCO terminator"] = c
        c = list(lines)
        c[find("DERIVED num_ref_idx_l1_active ", 7)] = "DERIVED num_ref_idx_l1_active 9"
        corrupt["wrong DERIVED num_ref_idx_l1_active"] = c
        c = list(lines)
        c[find("DERIVED header_end_bit ", 3)] = "DERIVED header_end_bit 1"
        corrupt["wrong DERIVED header_end_bit"] = c
        c = list(lines)
        c[find("DERIVED cabac_alignment_ok ", 0)] = "DERIVED cabac_alignment_ok 0"
        corrupt["DERIVED cabac_alignment_ok 0"] = c
        c = list(lines)
        j = [i for i, l in enumerate(c) if l.startswith("SLICE ")]
        del c[j[1]:j[2]]
        corrupt["missing SLICE block"] = c
        for name, c in corrupt.items():
            badf = os.path.join(td, "bad.txt")
            open(badf, "w").write("\n".join(c) + "\n")
            print(f"-- corrupted ({name}): must FAIL")
            if run_in_subprocess(inp, badf) != 1:
                ok_all = False
                print("   UNEXPECTED: harness did not fail")
    print("SELF-TEST " + ("PASS" if ok_all else "FAIL"))
    sys.exit(0 if ok_all else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="H.264 .ts or raw .264 file")
    ap.add_argument("dump", nargs="?", help="your slice header dump")
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
