#!/usr/bin/env python3
"""Harness for Chapter 3.1 (SPS parser).

    python3 test_sps.py <input.ts|input.264> <sps_dump.txt>
    python3 test_sps.py --make-variants <out_dir>      # write extra test inputs (see Readme)
    python3 test_sps.py --self-test

<input> is any ffmpeg-produced H.264 MPEG-TS file or a raw Annex B .264 file.
<sps_dump.txt> is your program's output (grammar in the Readme, section 5): for every SPS NAL
unit in stream order a block

    SPS <ordinal>
    <syntax element name> <value>          (exactly the names/order ffmpeg's trace_headers prints)
    ...
    DERIVED <name> <value> [<value>]       (derived values, see Readme)

Truth: ffmpeg `-bsf:v trace_headers` (SPS blocks after the first "Packet:" line, i.e. the
in-stream SPS NAL units; the "Extradata" copy ffmpeg prints first is ignored), plus values
derived from those fields by this harness, plus ffprobe -show_streams for the stream's
active SPS (width, height, color_space).

Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output or unusable input.
"""
import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import spsgen  # noqa: E402

FIELD_RE = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
PREFIX_RE = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header")
# Lines trace_headers prints that are not SPS syntax elements of 7.3.2.1.1 (they belong to the
# NAL unit header or to rbsp_trailing_bits).  Dropped from both sides before comparing.
IGNORED = {"forbidden_zero_bit", "nal_ref_idc", "nal_unit_type",
           "rbsp_stop_one_bit", "rbsp_alignment_zero_bit"}

COLOR_SPACE_NAMES = {0: "gbr", 1: "bt709", 4: "fcc", 5: "bt470bg", 6: "smpte170m", 7: "smpte240m",
                     8: "ycgco", 9: "bt2020nc", 10: "bt2020c", 11: "smpte2085",
                     12: "chroma-derived-nc", 13: "chroma-derived-c", 14: "ictcp"}


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


def sps_blocks_and_active(blocks):
    """-> (list of SPS field lists (ignored names removed), index of the SPS active at the first
    slice or None)."""
    spss, sps_ids, pps_map, active = [], [], {}, None
    latest = {}
    for title, fields in blocks:
        d = dict(fields)
        if title == "Sequence Parameter Set":
            latest[d.get("seq_parameter_set_id")] = len(spss)
            sps_ids.append(d.get("seq_parameter_set_id"))
            spss.append([(n, v) for n, v in fields if n not in IGNORED])
        elif title == "Picture Parameter Set":
            pps_map[d.get("pic_parameter_set_id")] = d.get("seq_parameter_set_id")
        elif title == "Slice Header" and active is None:
            sid = pps_map.get(d.get("pic_parameter_set_id"))
            active = latest.get(sid)
    return spss, active


def derive(fields):
    """DERIVED lines (strings) from an SPS field list.  See Readme section 3/5."""
    d = {}
    for n, v in fields:
        d.setdefault(n, v)
    cfi = d.get("chroma_format_idc", 1)
    sep = d.get("separate_colour_plane_flag", 0)
    cat = 0 if sep else cfi
    fmo = d["frame_mbs_only_flag"]
    sub_w, sub_h = {1: (2, 2), 2: (2, 1), 3: (1, 1)}.get(cfi, (1, 1))
    cux, cuy = (1, 2 - fmo) if cat == 0 else (sub_w, sub_h * (2 - fmo))
    cw = 16 * (d["pic_width_in_mbs_minus1"] + 1)
    ch = 16 * (2 - fmo) * (d["pic_height_in_map_units_minus1"] + 1)
    cl = cr = ct = cb = 0
    if d["frame_cropping_flag"]:
        cl, cr = d["frame_crop_left_offset"], d["frame_crop_right_offset"]
        ct, cb = d["frame_crop_top_offset"], d["frame_crop_bottom_offset"]
    out = [("width", cw - cux * (cl + cr)), ("height", ch - cuy * (ct + cb)),
           ("coded_width", cw), ("coded_height", ch),
           ("chroma_array_type", cat),
           ("max_frame_num", 1 << (d["log2_max_frame_num_minus4"] + 4))]
    lines = [f"DERIVED {n} {v}" for n, v in out]
    if d.get("timing_info_present_flag"):
        num, den = d["time_scale"], 2 * d["num_units_in_tick"]
        g = math.gcd(num, den) or 1
        lines.append(f"DERIVED frame_rate {num // g} {den // g}")
    if d.get("colour_description_present_flag"):
        lines.append(f"DERIVED matrix_coefficients {d['matrix_coefficients']}")
    if d.get("bitstream_restriction_flag"):
        lines.append(f"DERIVED max_num_reorder_frames {d['max_num_reorder_frames']}")
        lines.append(f"DERIVED max_dec_frame_buffering {d['max_dec_frame_buffering']}")
    return lines


def render_reader_output(spss):
    """The known-good reader output for a list of SPS field lists."""
    out = []
    for i, fields in enumerate(spss):
        out.append(f"SPS {i}")
        out += [f"{n} {v}" for n, v in fields]
        out += derive(fields)
    return "\n".join(out) + "\n"


# ================================================================ section labels (for hints)
SECTION_PREFIX = [
    ("profile_idc constraint_set reserved_zero_2bits level_idc seq_parameter_set_id",
     "SPS start: profile_idc, constraint flags, level_idc, seq_parameter_set_id"),
    ("chroma_format_idc separate_colour_plane_flag bit_depth_luma_minus8 bit_depth_chroma_minus8 "
     "qpprime_y_zero_transform_bypass_flag", "High-profile branch: chroma format and bit depth"),
    ("seq_scaling_matrix_present_flag seq_scaling_list_present_flag delta_scale",
     "scaling lists, scaling_list() 7.3.2.1.1.1"),
    ("log2_max_frame_num_minus4", "frame_num size"),
    ("pic_order_cnt_type log2_max_pic_order_cnt_lsb_minus4", "pic_order_cnt_type 0 branch"),
    ("delta_pic_order_always_zero_flag offset_for_non_ref_pic offset_for_top_to_bottom_field "
     "num_ref_frames_in_pic_order_cnt_cycle offset_for_ref_frame", "pic_order_cnt_type 1 branch"),
    ("max_num_ref_frames gaps_in_frame_num_allowed_flag", "reference frame count"),
    ("pic_width_in_mbs_minus1 pic_height_in_map_units_minus1 frame_mbs_only_flag "
     "mb_adaptive_frame_field_flag direct_8x8_inference_flag", "picture size / frame-field coding"),
    ("frame_cropping_flag frame_crop", "frame cropping"),
    ("vui_parameters_present_flag", "start of VUI (Annex E)"),
    ("aspect_ratio_info_present_flag aspect_ratio_idc sar_", "VUI: aspect ratio"),
    ("overscan_", "VUI: overscan"),
    ("video_signal_type_present_flag video_format video_full_range_flag colour_ transfer_characteristics "
     "matrix_coefficients", "VUI: video signal type"),
    ("chroma_loc_info_present_flag chroma_sample_loc", "VUI: chroma sample location"),
    ("timing_info_present_flag num_units_in_tick time_scale fixed_frame_rate_flag", "VUI: timing info"),
    ("nal_hrd_parameters_present_flag vcl_hrd_parameters_present_flag low_delay_hrd_flag "
     "pic_struct_present_flag", "VUI: HRD present flags / low_delay / pic_struct"),
    ("bitstream_restriction_flag motion_vectors_over_pic_boundaries_flag max_bytes_per_pic_denom "
     "max_bits_per_mb_denom log2_max_mv_length max_num_reorder_frames max_dec_frame_buffering",
     "VUI: bitstream_restriction"),
]
HRD_NAMES = ("cpb_cnt_minus1", "bit_rate_scale", "cpb_size_scale", "bit_rate_value_minus1",
             "cpb_size_value_minus1", "cbr_flag", "initial_cpb_removal_delay_length_minus1",
             "cpb_removal_delay_length_minus1", "dpb_output_delay_length_minus1", "time_offset_length")


def section_of(name, owner):
    base = name.split("[")[0]
    if base in HRD_NAMES:
        return f"hrd_parameters() of the {owner or '?'} HRD (E.1.2)"
    for prefixes, label in SECTION_PREFIX:
        if any(base.startswith(p) for p in prefixes.split()):
            return label
    return "unknown section"


def hrd_owner_at(expected, idx):
    owner = None
    for n, v in expected[:idx]:
        if n == "nal_hrd_parameters_present_flag" and v:
            owner = "NAL"
        elif n == "vcl_hrd_parameters_present_flag" and v:
            owner = "VCL"
    return owner


# ================================================================ reader output
def parse_reader(path):
    """-> list of (fields[(name,int)], derived[str]) one per SPS block."""
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
        if tok[0] == "SPS" and len(tok) == 2:
            cur = ([], [])
            blocks.append(cur)
            continue
        if cur is None:
            die(f"{path}:{ln}: expected a 'SPS <n>' line before any field, got {s!r}")
        if tok[0] == "DERIVED":
            if len(tok) < 3:
                die(f"{path}:{ln}: malformed DERIVED line {s!r}")
            try:
                cur[1].append("DERIVED " + " ".join([tok[1]] + [str(int(t)) for t in tok[2:]]))
            except ValueError:
                die(f"{path}:{ln}: DERIVED values must be integers: {s!r}")
            continue
        if len(tok) != 2:
            die(f"{path}:{ln}: expected '<name> <integer value>', got {s!r}")
        try:
            v = int(tok[1])
        except ValueError:
            die(f"{path}:{ln}: value is not an integer: {s!r}")
        if tok[0] not in IGNORED:
            cur[0].append((tok[0], v))
    if not blocks:
        die(f"{path}: no 'SPS <n>' blocks found")
    return blocks


# ================================================================ checking
def compare_fields(rep, k, expected, actual):
    n = min(len(expected), len(actual))
    for i in range(n):
        if expected[i] != actual[i]:
            en, ev = expected[i]
            an, av = actual[i]
            ctx = "; ".join(f"{a} {b}" for a, b in expected[max(0, i - 4):i]) or "(start of SPS)"
            sec = section_of(en, hrd_owner_at(expected, i))
            if en != an:
                what = f"field name: expected '{en}', got '{an} {av}'"
            else:
                what = f"value of '{en}': expected {ev}, got {av}"
            rep.check(False, f"SPS #{k} field #{i}: {what}\n         expected syntax section: {sec}\n"
                             f"         previous fields (all matched): {ctx}")
            return
    if len(expected) != len(actual):
        if len(actual) < len(expected):
            en, ev = expected[len(actual)]
            what = f"your SPS block ends early; next expected field is '{en}' (= {ev}), section: " \
                   f"{section_of(en, hrd_owner_at(expected, len(actual)))}"
        else:
            what = f"extra field(s) after the last expected one: '{actual[len(expected)][0]}'"
        rep.check(False, f"SPS #{k}: {what} (expected {len(expected)} fields, got {len(actual)})")
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
            rep.check(False, f"SPS #{k} derived line #{i}: expected '{e}', got '{a}' "
                             f"(all expected: {'; '.join(expected)})")
            return


def ffprobe_stream(path):
    j = ffref.probe_json(path, "-select_streams", "v:0", "-show_streams")
    s = (j.get("streams") or [None])[0]
    return s


def check_ffprobe(rep, path, active, reader_blocks):
    s = ffprobe_stream(path)
    if not s:
        print("note: ffprobe found no video stream; skipping ffprobe cross-check", file=sys.stderr)
        return
    if active is None or active >= len(reader_blocks):
        print("note: could not determine the active SPS; skipping ffprobe cross-check", file=sys.stderr)
        return
    der = {}
    for l in reader_blocks[active][1]:
        t = l.split()
        der[t[1]] = t[2:]
    for key in ("width", "height"):  # ffprobe reports the cropped size; its coded_* fields are not reliable
        if key in s and key in der:
            rep.check(der[key] == [str(s[key])],
                      f"ffprobe {key} for the active SPS (reader block #{active}): ffprobe says {s[key]}, "
                      f"your DERIVED {key} is {' '.join(der[key]) or '(missing)'}")
        elif key in s:
            rep.check(False, f"missing 'DERIVED {key}' in reader block #{active}")
    mc = der.get("matrix_coefficients")
    if mc and s.get("color_space") and int(mc[0]) in COLOR_SPACE_NAMES:
        rep.check(COLOR_SPACE_NAMES[int(mc[0])] == s["color_space"],
                  f"ffprobe color_space {s['color_space']!r} vs your DERIVED matrix_coefficients {mc[0]} "
                  f"({COLOR_SPACE_NAMES[int(mc[0])]})")


def run_check(path, dump, title="SPS parser"):
    ffref.require_tools()
    if not os.path.exists(path):
        die(f"input not found: {path}")
    reader = parse_reader(dump)
    with tempfile.TemporaryDirectory() as td:
        tr = os.path.join(td, "trace.txt")
        ffref.trace_headers(path, tr)
        blocks = parse_trace(tr)
    spss, active = sps_blocks_and_active(blocks)
    if not spss:
        die(f"ffmpeg trace_headers found no SPS in {path}; is it an H.264 stream?")
    rep = Report(title)
    rep.check(len(spss) == len(reader),
              f"number of SPS blocks: expected {len(spss)} (one per SPS NAL unit in the stream, repeats "
              f"included), got {len(reader)}")
    for k, (exp, (act, der)) in enumerate(zip(spss, reader)):
        compare_fields(rep, k, exp, act)
        compare_derived(rep, k, derive(exp), der)
    check_ffprobe(rep, path, active, reader)
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
    out = b""
    print("sps_variants.264: synthetic SPS NAL units, followed by a real 3-frame stream (SPS id 0)")
    for i, (desc, p) in enumerate(spsgen.make_variants()):
        out += b"\x00\x00\x00\x01" + spsgen.sps_nal(p)
        print(f"  SPS #{i}  seq_parameter_set_id={p['id']:<2} {desc}")
    with open(os.path.join(outdir, "sps_variants.264"), "wb") as f:
        f.write(out + data)
    x264_clip(os.path.join(outdir, "sps_1080p.ts"), size="1920x1080",
              opts=["-profile:v", "high"])
    print("sps_1080p.ts: x264 1920x1080, coded 1088 rows, frame_crop_bottom_offset 4")
    x264_clip(os.path.join(outdir, "sps_vui_hrd.ts"), vf="setsar=16/11",
              opts=["-profile:v", "main", "-b:v", "400k", "-maxrate", "400k", "-bufsize", "400k",
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-color_range", "tv", "-x264-params", "nal-hrd=cbr:force-cfr=1"])
    print("sps_vui_hrd.ts: x264 with SAR, colour description, NAL HRD (nal-hrd=cbr), bitstream_restriction")
    x264_clip(os.path.join(outdir, "sps_interlaced_crop.ts"), size="1920x1080",
              opts=["-profile:v", "high", "-x264-params", "interlaced=1:tff=1"])
    print("sps_interlaced_crop.ts: x264 MBAFF 1920x1080, frame_mbs_only_flag 0, "
          "frame_crop_bottom_offset 2 (crop unit 4 rows)")
    print("Also useful (from common.testfiles): T1 (poc type 2), T9_spatial (poc type 0, B frames), "
          "T11_high, T11_cqm, T12.")
    print("Not producible by x264, hence only in sps_variants.264: poc type 1, scaling lists in the SPS, "
          "4:2:2/4:4:4/4:0:0, 'SAR 255', VCL HRD.")


# ================================================================ self test
def self_test():
    ffref.require_tools()
    ok_all = True
    with tempfile.TemporaryDirectory() as td:
        make_variants_dir = os.path.join(td, "v")
        so = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            make_variants(make_variants_dir)
        finally:
            sys.stdout.close()
            sys.stdout = so
        inputs = [testfiles.get("T1"), testfiles.get("T11_high"), testfiles.get("T12"),
                  testfiles.get("T9_spatial")]
        inputs += [os.path.join(make_variants_dir, n) for n in
                   ("sps_variants.264", "sps_1080p.ts", "sps_vui_hrd.ts", "sps_interlaced_crop.ts")]
        seen_feature = set()
        for inp in inputs:
            tr = os.path.join(td, "trace.txt")
            ffref.trace_headers(inp, tr)
            spss, _ = sps_blocks_and_active(parse_trace(tr))
            for f in spss:
                seen_feature.update(n for n, _ in f)
            good = os.path.join(td, "good.txt")
            open(good, "w").write(render_reader_output(spss))
            name = os.path.basename(inp)
            print(f"-- {name}: {len(spss)} SPS block(s), known-good output must PASS")
            if run_in_subprocess(inp, good) != 0:
                ok_all = False
                print("   UNEXPECTED FAIL")
        for needed in ("mb_adaptive_frame_field_flag", "delta_scale[0]", "offset_for_ref_frame[0]", "bit_rate_value_minus1[1]",
                       "sar_width", "matrix_coefficients", "frame_crop_bottom_offset"):
            if needed not in seen_feature:
                ok_all = False
                print(f"self-test coverage problem: no SPS with {needed}")
        # corruptions on the variants file
        inp = os.path.join(make_variants_dir, "sps_variants.264")
        ffref.trace_headers(inp, os.path.join(td, "trace.txt"))
        spss, _ = sps_blocks_and_active(parse_trace(os.path.join(td, "trace.txt")))
        text = render_reader_output(spss)
        lines = text.splitlines()
        corrupt = {}
        # 1: change a value inside the third SPS's scaling list
        c = list(lines)
        idx = [i for i, l in enumerate(c) if l.startswith("delta_scale[3] ")][0]
        c[idx] = "delta_scale[3] 99"
        corrupt["wrong delta_scale value"] = c
        # 2: drop the VUI max_dec_frame_buffering line of the full-VUI SPS
        c = list(lines)
        idx = [i for i, l in enumerate(c) if l.startswith("max_dec_frame_buffering ")][0]
        del c[idx]
        corrupt["missing max_dec_frame_buffering"] = c
        # 3: wrong cropped height
        c = list(lines)
        idx = [i for i, l in enumerate(c) if l.startswith("DERIVED height ")][4]
        c[idx] = "DERIVED height 1088"
        corrupt["wrong DERIVED height"] = c
        # 4: drop a whole SPS block
        c = list(lines)
        j = [i for i, l in enumerate(c) if l.startswith("SPS ")]
        del c[j[1]:j[2]]
        corrupt["missing SPS block"] = c
        for name, c in corrupt.items():
            bad = os.path.join(td, "bad.txt")
            open(bad, "w").write("\n".join(c) + "\n")
            print(f"-- corrupted ({name}): must FAIL")
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
    ap.add_argument("dump", nargs="?", help="your SPS dump")
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
