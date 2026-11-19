#!/usr/bin/env python3
"""Harness for Chapter 3.2 (PPS parser).

    python3 test_pps.py <input.ts|input.264> <pps_dump.txt>
    python3 test_pps.py --make-variants <out_dir>      # write extra test inputs (see Readme)
    python3 test_pps.py --self-test

<input> is any ffmpeg-produced H.264 MPEG-TS file or a raw Annex B .264 file.
<pps_dump.txt> is your program's output (grammar in the Readme, section 5): for every PPS NAL
unit in stream order a block

    PPS <ordinal>
    <syntax element name> <value>          (exactly the names/order ffmpeg's trace_headers prints)
    ...
    DERIVED <name> <value>                 (derived values, see Readme)
    ERROR <free text>                      (only for PPSs with slice groups, see Readme)

Truth: ffmpeg `-bsf:v trace_headers` (PPS blocks after the first "Packet:" line, i.e. the in-stream
PPS NAL units; the "Extradata" copy ffmpeg prints first is ignored), plus values derived from those
fields (and from the SPS fields in the same trace) by this harness.

Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output or unusable input.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import ppsgen  # noqa: E402

FIELD_RE = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
PREFIX_RE = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header")
# Lines trace_headers prints that are not PPS syntax elements of 7.3.2.2 (NAL header, trailing bits).
IGNORED = {"forbidden_zero_bit", "nal_ref_idc", "nal_unit_type",
           "rbsp_stop_one_bit", "rbsp_alignment_zero_bit"}
HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ================================================================ truth from trace_headers
def parse_trace(path):
    """Return list of (title, [(name, value:int), ...]) for every parameter-set/slice block that
    appears after the first 'Packet:' line."""
    blocks, cur, started = [], None, False
    with open(path, "r", errors="replace") as f:
        for raw in f:
            line = PREFIX_RE.sub("", raw.rstrip("\n"))
            if line.startswith("Packet:"):
                started, cur = True, None
                continue
            if not started:
                continue
            m = FIELD_RE.match(line)
            if m and cur is not None:
                cur.append((m.group(2), int(m.group(4))))
            elif line in TITLES:
                cur = []
                blocks.append((line, cur))
            else:
                cur = None
    return blocks


def pps_blocks(blocks):
    """-> list of (fields, sps_chroma_format_idc) for every PPS NAL unit in stream order.  The SPS
    that applies is the most recent SPS NAL with the PPS's seq_parameter_set_id before it (the
    chroma_format_idc of a Main/Baseline SPS is inferred as 1).  Fields are the PPS syntax elements
    only (NAL header / trailing bits removed)."""
    sps_cfi, out = {}, []
    for title, fields in blocks:
        d = dict(fields)
        if title == "Sequence Parameter Set":
            sps_cfi[d["seq_parameter_set_id"]] = d.get("chroma_format_idc", 1)
        elif title == "Picture Parameter Set":
            out.append(([(n, v) for n, v in fields if n not in IGNORED],
                        sps_cfi.get(d["seq_parameter_set_id"], 1)))
    return out


def expected_list_count(cfi, t8x8):
    """Number of pic_scaling_list_present_flag entries (7.3.2.2)."""
    return 6 + (2 if cfi != 3 else 6) * t8x8


def truth_block(fields, cfi):
    """-> (syntax fields to compare, derived lines, rejected:bool).  A PPS with
    num_slice_groups_minus1 > 0 is rejected by the reader: only the fields up to and including
    num_slice_groups_minus1 are expected, then an ERROR line."""
    for i, (n, v) in enumerate(fields):
        if n == "num_slice_groups_minus1" and v > 0:
            return fields[:i + 1], [], True
    d = {}
    for n, v in fields:
        d.setdefault(n, v)
    present = "transform_8x8_mode_flag" in d
    t8 = d.get("transform_8x8_mode_flag", 0)
    nlists = 0
    if d.get("pic_scaling_matrix_present_flag"):
        nlists = expected_list_count(cfi, t8)
        got = sum(1 for n, _ in fields if n.startswith("pic_scaling_list_present_flag["))
        if got != nlists:   # would mean the harness formula disagrees with ffmpeg
            die(f"internal error: expected {nlists} scaling lists, ffmpeg trace has {got}")
    der = [("pic_init_qp", 26 + d["pic_init_qp_minus26"]),
           ("pic_init_qs", 26 + d["pic_init_qs_minus26"]),
           ("num_ref_idx_l0_default_active", d["num_ref_idx_l0_default_active_minus1"] + 1),
           ("num_ref_idx_l1_default_active", d["num_ref_idx_l1_default_active_minus1"] + 1),
           ("trailing_fields_present", 1 if present else 0),
           ("transform_8x8_mode_flag", t8),
           ("sps_chroma_format_idc", cfi),
           ("num_scaling_lists", nlists),
           ("second_chroma_qp_index_offset",
            d.get("second_chroma_qp_index_offset", d["chroma_qp_index_offset"]))]
    return fields, [f"DERIVED {n} {v}" for n, v in der], False


def render_reader_output(pps):
    out = []
    for i, (fields, cfi) in enumerate(pps):
        f, der, rej = truth_block(fields, cfi)
        out.append(f"PPS {i}")
        out += [f"{n} {v}" for n, v in f]
        out += der
        if rej:
            out.append("ERROR slice groups (FMO) are not supported")
    return "\n".join(out) + "\n"


# ================================================================ section labels (for hints)
SECTION_PREFIX = [
    ("pic_parameter_set_id seq_parameter_set_id", "PPS start: pic_parameter_set_id, seq_parameter_set_id"),
    ("entropy_coding_mode_flag bottom_field_pic_order_in_frame_present_flag num_slice_groups_minus1",
     "entropy coder / field POC flag / slice group count"),
    ("slice_group_map_type run_length_minus1 top_left bottom_right slice_group_change "
     "pic_size_in_map_units_minus1 slice_group_id", "slice group (FMO) fields"),
    ("num_ref_idx_l0_default_active_minus1 num_ref_idx_l1_default_active_minus1",
     "default reference index counts"),
    ("weighted_pred_flag weighted_bipred_idc", "weighted prediction flags"),
    ("pic_init_qp_minus26 pic_init_qs_minus26 chroma_qp_index_offset", "initial QPs and chroma QP offset"),
    ("deblocking_filter_control_present_flag constrained_intra_pred_flag redundant_pic_cnt_present_flag",
     "control flags before the optional trailing fields"),
    ("transform_8x8_mode_flag pic_scaling_matrix_present_flag pic_scaling_list_present_flag delta_scale "
     "second_chroma_qp_index_offset",
     "optional trailing fields (only if more_rbsp_data()): transform_8x8_mode_flag, scaling matrix, "
     "second_chroma_qp_index_offset"),
]


def section_of(name):
    base = name.split("[")[0]
    for prefixes, label in SECTION_PREFIX:
        if any(base.startswith(p) for p in prefixes.split()):
            return label
    return "unknown section"


# ================================================================ reader output
def parse_reader(path):
    """-> list of dict(fields=[(name,int)], derived=[str], error=str|None) one per PPS block."""
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
        if tok[0] == "PPS" and len(tok) == 2:
            cur = dict(fields=[], derived=[], error=None)
            blocks.append(cur)
            continue
        if cur is None:
            die(f"{path}:{ln}: expected a 'PPS <n>' line before any field, got {s!r}")
        if tok[0] == "ERROR":
            cur["error"] = " ".join(tok[1:]) or "(no text)"
            continue
        if tok[0] == "DERIVED":
            if len(tok) != 3:
                die(f"{path}:{ln}: malformed DERIVED line {s!r} (expected 'DERIVED <name> <integer>')")
            try:
                cur["derived"].append(f"DERIVED {tok[1]} {int(tok[2])}")
            except ValueError:
                die(f"{path}:{ln}: DERIVED value must be an integer: {s!r}")
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
        die(f"{path}: no 'PPS <n>' blocks found")
    return blocks


# ================================================================ checking
def compare_fields(rep, k, expected, actual, rejected):
    n = min(len(expected), len(actual))
    for i in range(n):
        if expected[i] != actual[i]:
            en, ev = expected[i]
            an, av = actual[i]
            ctx = "; ".join(f"{a} {b}" for a, b in expected[max(0, i - 4):i]) or "(start of PPS)"
            if en != an:
                what = f"field name: expected '{en}', got '{an} {av}'"
            else:
                what = f"value of '{en}': expected {ev}, got {av}"
            rep.check(False, f"PPS #{k} field #{i}: {what}\n         expected syntax section: "
                             f"{section_of(en)}\n         previous fields (all matched): {ctx}")
            return
    if len(expected) != len(actual):
        if len(actual) < len(expected):
            en, ev = expected[len(actual)]
            what = f"your PPS block ends early; next expected field is '{en}' (= {ev}), section: {section_of(en)}"
        else:
            an = actual[len(expected)][0]
            if rejected:
                what = (f"this PPS has num_slice_groups_minus1 > 0: print fields only up to that one, then an "
                        f"ERROR line; you printed '{an}' after it")
            else:
                what = f"extra field(s) after the last expected one: '{an}' ({section_of(an)})"
        rep.check(False, f"PPS #{k}: {what} (expected {len(expected)} fields, got {len(actual)})")
    else:
        rep.check(True, "")


def compare_derived(rep, k, expected, actual):
    if expected == actual:
        rep.check(True, "")
        return
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else "(nothing)"
        a = actual[i] if i < len(actual) else "(nothing)"
        if e != a:
            rep.check(False, f"PPS #{k} derived line #{i}: expected '{e}', got '{a}' "
                             f"(all expected: {'; '.join(expected)})")
            return


def run_check(path, dump, title="PPS parser"):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input not found: {path}")
    reader = parse_reader(dump)
    with tempfile.TemporaryDirectory() as td:
        tr = os.path.join(td, "trace.txt")
        rc = ffref.trace_headers(path, tr)
        blocks = parse_trace(tr)
        errs = [PREFIX_RE.sub("", l.strip()) for l in open(tr, errors="replace")
                if "Failed to read unit" in l or "not available" in l]
    if rc != 0 and errs:
        die(f"ffmpeg trace_headers could not parse {path} (exit {rc}), so there is no truth to compare "
            f"against:\n  " + "\n  ".join(errs[:5]))
    pps = pps_blocks(blocks)
    if not pps:
        die(f"ffmpeg trace_headers found no PPS in {path}; is it an H.264 stream with parameter sets?")
    rep = Report(title)
    rep.check(len(pps) == len(reader),
              f"number of PPS blocks: expected {len(pps)} (one per PPS NAL unit in the stream, repeats "
              f"included), got {len(reader)}")
    for k, ((fields, cfi), act) in enumerate(zip(pps, reader)):
        exp, der, rej = truth_block(fields, cfi)
        compare_fields(rep, k, exp, act["fields"], rej)
        if rej:
            rep.check(act["error"] is not None,
                      f"PPS #{k}: num_slice_groups_minus1 is {exp[-1][1]} (slice groups / FMO): expected an "
                      f"'ERROR <text>' line in this block, got none")
            rep.check(not act["derived"], f"PPS #{k}: a rejected PPS has no DERIVED lines, got {len(act['derived'])}")
        else:
            rep.check(act["error"] is None, f"PPS #{k}: unexpected ERROR line ({act['error']!r}) for a PPS "
                                            f"that has no slice groups")
            compare_derived(rep, k, der, act["derived"])
    return rep


# ================================================================ test inputs / variants
def x264_clip(out, size="352x288", frames=3, vf=None, opts=(), fmt="mpegts"):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25",
           "-frames:v", str(frames), "-pix_fmt", "yuv420p"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", *opts, "-f", fmt, out]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die("ffmpeg failed: " + r.stderr.decode()[-500:])


def make_variants(outdir):
    ffref.require_tools()
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, "_base.264")
    x264_clip(base, frames=3, opts=["-profile:v", "high", "-x264-params", "keyint=1"], fmt="h264")
    data = open(base, "rb").read()
    os.remove(base)
    sc = b"\x00\x00\x00\x01"
    print("pps_variants.264: synthetic SPS (ids 1-4) and PPS NAL units, followed by a real 3-frame High stream "
          "(SPS 0, PPS 0)")
    out = b""
    for desc, nal in ppsgen.make_variants():
        out += sc + nal
        print("  " + desc)
    with open(os.path.join(outdir, "pps_variants.264"), "wb") as f:
        f.write(out + data)
    print("pps_fmo.264: Baseline SPS and four PPSs, three of them with slice groups (map types 0, 2, 6), "
          "followed by a real stream (the reader must reject the FMO PPSs and carry on)")
    out = b""
    for desc, nal in ppsgen.make_fmo():
        out += sc + nal
        print("  " + desc)
    with open(os.path.join(outdir, "pps_fmo.264"), "wb") as f:
        f.write(out + data)
    ramp16 = ",".join(str(16 + i) for i in range(16))
    ramp64 = ",".join(str(10 + i // 2) for i in range(64))
    x264_clip(os.path.join(outdir, "pps_cqm.ts"),
              opts=["-profile:v", "high", "-x264-params", f"cqm4iy={ramp16}:cqm8iy={ramp64}"])
    print("pps_cqm.ts: x264 with custom quantiser matrices: explicit scaling lists in the PPS")
    print("Also useful (from common.testfiles): T1 (Baseline, no trailing fields), T10_full (CABAC), T11_high "
          "(High, transform_8x8), T11_cqm, T9_spatial, T12.")
    print("Note: x264's cqm=jvt (T11_cqm) sends the 8 list flags all 0, because the JVT matrices are the spec "
          "defaults; only custom matrices produce explicit lists.")


# ================================================================ self test
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
        inputs = [testfiles.get(n) for n in ("T1", "T10_full", "T11_high", "T11_cqm", "T9_spatial", "T12")]
        inputs += [os.path.join(vdir, n) for n in ("pps_variants.264", "pps_fmo.264", "pps_cqm.ts")]
        seen = set()
        for inp in inputs:
            tr = os.path.join(td, "trace.txt")
            ffref.trace_headers(inp, tr)
            pps = pps_blocks(parse_trace(tr))
            for f, _ in pps:
                seen.update(n for n, _ in f)
            good = os.path.join(td, "good.txt")
            open(good, "w").write(render_reader_output(pps))
            print(f"-- {os.path.basename(inp)}: {len(pps)} PPS block(s), known-good output must PASS")
            if run_in_subprocess(inp, good) != 0:
                ok_all = False
                print("   UNEXPECTED FAIL")
        for needed in ("delta_scale[0]", "second_chroma_qp_index_offset", "transform_8x8_mode_flag",
                       "pic_scaling_list_present_flag[11]", "slice_group_map_type", "slice_group_id[0]",
                       "run_length_minus1[0]", "top_left[0]", "weighted_bipred_idc"):
            if needed not in seen:
                ok_all = False
                print(f"self-test coverage problem: no PPS with {needed}")
        # corruptions on the variants file
        inp = os.path.join(vdir, "pps_variants.264")
        ffref.trace_headers(inp, os.path.join(td, "trace.txt"))
        pps = pps_blocks(parse_trace(os.path.join(td, "trace.txt")))
        lines = render_reader_output(pps).splitlines()

        def find(prefix, nth=0):
            return [i for i, l in enumerate(lines) if l.startswith(prefix)][nth]

        corrupt = {}
        c = list(lines)
        c[find("pic_init_qp_minus26 ", 3)] = "pic_init_qp_minus26 77"
        corrupt["wrong pic_init_qp_minus26"] = c
        c = list(lines)
        del c[find("second_chroma_qp_index_offset ", 0)]
        corrupt["missing second_chroma_qp_index_offset"] = c
        # PPS #4 (index) has transform_8x8_mode_flag 0 and 6 lists; a reader that reads 8 adds two flags
        c = list(lines)
        # locate the block with transform_8x8_mode_flag 0 and a matrix: variant PPS 4 (4th matrix block)
        idxs = [j for j, l in enumerate(lines) if l.startswith("pic_scaling_list_present_flag[5] ")]
        for j in idxs:
            blk_end = next(k for k in range(j, len(lines)) if lines[k].startswith("second_chroma_qp"))
            if not any(lines[k].startswith("pic_scaling_list_present_flag[6]") for k in range(j, blk_end)):
                c[blk_end:blk_end] = ["pic_scaling_list_present_flag[6] 0", "pic_scaling_list_present_flag[7] 0"]
                break
        corrupt["8 scaling lists although transform_8x8_mode_flag is 0"] = c
        c = list(lines)
        c[find("DERIVED second_chroma_qp_index_offset ", 1)] = "DERIVED second_chroma_qp_index_offset 99"
        corrupt["wrong DERIVED second_chroma_qp_index_offset"] = c
        c = list(lines)
        j = [i for i, l in enumerate(c) if l.startswith("PPS ")]
        del c[j[1]:j[2]]
        corrupt["missing PPS block"] = c
        for name, c in corrupt.items():
            bad = os.path.join(td, "bad.txt")
            open(bad, "w").write("\n".join(c) + "\n")
            print(f"-- corrupted ({name}): must FAIL")
            if run_in_subprocess(inp, bad) != 1:
                ok_all = False
                print("   UNEXPECTED: harness did not fail")
        # FMO file: drop the ERROR line
        inp = os.path.join(vdir, "pps_fmo.264")
        ffref.trace_headers(inp, os.path.join(td, "trace.txt"))
        pps = pps_blocks(parse_trace(os.path.join(td, "trace.txt")))
        c = [l for l in render_reader_output(pps).splitlines() if not l.startswith("ERROR")]
        bad = os.path.join(td, "bad.txt")
        open(bad, "w").write("\n".join(c) + "\n")
        print("-- corrupted (FMO PPS without ERROR line): must FAIL")
        if run_in_subprocess(inp, bad) != 1:
            ok_all = False
            print("   UNEXPECTED: harness did not fail")
    print("SELF-TEST " + ("PASS" if ok_all else "FAIL"))
    sys.exit(0 if ok_all else 1)


def run_in_subprocess(inp, dump):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), inp, dump], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=300)
    for l in (r.stdout.decode() + r.stderr.decode()).splitlines()[-6:]:
        print("   |", l[:200])
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="H.264 .ts or raw .264 file")
    ap.add_argument("dump", nargs="?", help="your PPS dump")
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
