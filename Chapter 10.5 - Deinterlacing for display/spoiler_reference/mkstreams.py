"""SPOILER: builds the test inputs of Chapter 10.5.

x264 cannot write PAFF field pictures, so the suite combines
  * x264 MBAFF streams (--tff / --bff: pic_struct 3 / 4 in every picture's pic_timing SEI) as .ts (x264 -> mkv -> ffmpeg -c copy),
  * x264 command-line streams made from a y4m file: SEI-less interlaced streams (the SEIs are cut out), pulldown streams
    (--pulldown 32 / double / triple / euro give pic_struct 3..8) and a stream whose pic_struct values are rewritten to
    cycle through 0..8 (single fields, repeated fields, frame doubling / tripling),
  * the PAFF field-picture streams of Chapter 10.1 (spoiler_reference of that chapter), which carry no SEI at all.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, ROOT)
from common import ffref  # noqa: E402

W, H = 96, 64


def _run(cmd, timeout=120):
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.decode(errors='replace')[-400:]}")


def _y4m(path, fps, secs):
    _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={fps}", "-t", str(secs),
          "-pix_fmt", "yuv420p", path])


def _x264(y4m, out, *args):
    _run(["x264", "--demuxer", "y4m", "--qp", "26", "--bframes", "2", "--keyint", "15", "--no-progress", "--threads", "1",
          *args, "-o", out, y4m])


def _ts(out, fps, secs, params):
    _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={fps}", "-t", str(secs),
          "-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", "26", "-profile:v", "high", "-x264-params", params,
          "-f", "mpegts", out])


def split_nals(data):
    """Annex B -> list of NAL unit byte strings (without start codes)."""
    out, i, n = [], 0, len(data)
    starts = []
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for a, b in zip(starts, starts[1:] + [n + 3]):
        nal = data[a:b - 3] if b <= n else data[a:]
        while nal and nal[-1] == 0:                    # trailing_zero_8bits / the first zero of a 4-byte start code
            nal = nal[:-1]
        out.append(nal)
    return out


def join_nals(nals):
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def pic_timing_sei(ps):
    """A complete pic_timing SEI NAL unit (no HRD in the stream): pic_struct, one clock_timestamp_flag (= 0) per clock, alignment."""
    nclock = {0: 1, 1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3, 7: 2, 8: 3}[ps]
    bits = format(ps, "04b") + "0" * nclock + "1"
    bits += "0" * (-len(bits) % 8)
    return bytes([0x06, 0x01, len(bits) // 8]) + int(bits, 2).to_bytes(len(bits) // 8, "big") + b"\x80"


def rewrite_pic_timing(data, fn):
    """Replace the pic_struct of the i-th pic_timing SEI by fn(i) (None removes the SEI NAL unit)."""
    out, i = [], 0
    for nal in split_nals(data):
        if nal[0] & 31 == 6 and nal[1] == 1:
            v = fn(i)
            i += 1
            if v is not None:
                out.append(pic_timing_sei(v))
            continue
        out.append(nal)
    return join_nals(out)


def _fgen(name):
    sys.path.insert(0, os.path.join(ROOT, "Chapter 10.1 - Field pictures", "spoiler_reference"))
    import fgen
    data, _ = fgen.build(fgen.scenarios()[name])
    return data


FGEN_CASES = ["cavlc_tff", "cabac_bff", "cabac_t8_mixed", "cavlc_mixed_poc1", "cabac_poc2", "cavlc_crop", "cabac_idr_again",
              "cavlc_nonref_pairs", "cavlc_reorder"]


def case_names():
    return (["tff.ts", "bff.ts", "conflict.ts", "tff_nosei.264", "bff_nosei.264", "ps_cycle.264", "pd32.264", "pd_double.264", "pd_triple.264",
             "pd_euro.264", "prog.ts", "prog_ps.264"] + [f"paff_{n}.264" for n in FGEN_CASES])


def make(directory, tmp):
    """Write every case into `directory`; returns the list of file names."""
    ffref.require_tools()
    os.makedirs(directory, exist_ok=True)
    p = lambda n: os.path.join(directory, n)  # noqa: E731
    ia, p24, pp = os.path.join(tmp, "i25.y4m"), os.path.join(tmp, "p24.y4m"), os.path.join(tmp, "p25.y4m")
    _y4m(ia, 25, 1.5)
    _y4m(p24, 24, 2)
    # libx264 through ffmpeg: --tff is requested but the SEI says top-first for frames the POCs say bottom-first (a CONFLICT case)
    _ts(p("conflict.ts"), 25, 1.5, "interlaced=1:tff=1:bframes=2:keyint=15")
    _ts(p("prog.ts"), 25, 1.5, "bframes=2:keyint=15")
    for nm, fl in (("tff", "--tff"), ("bff", "--bff")):
        raw = os.path.join(tmp, nm + ".264")
        _x264(ia, raw, fl)
        mkv = os.path.join(tmp, nm + ".mkv")
        _x264(ia, mkv, fl)
        _run(["ffmpeg", "-v", "error", "-y", "-i", mkv, "-c", "copy", "-f", "mpegts", p(nm + ".ts")])
        data = open(raw, "rb").read()
        open(p(nm + "_nosei.264"), "wb").write(rewrite_pic_timing(data, lambda i: None))
        if nm == "tff":
            cyc = [0, 1, 2, 3, 4, 5, 6, 7, 8, 4, 3, 6, 5, 2, 1]
            open(p("ps_cycle.264"), "wb").write(rewrite_pic_timing(data, lambda i: cyc[i % len(cyc)]))
    for nm, pd in (("pd32", "32"), ("pd_double", "double"), ("pd_triple", "triple"), ("pd_euro", "euro")):
        _x264(p24, p(nm + ".264"), "--pulldown", pd, "--fps", "24")
    _y4m(pp, 25, 1.5)
    _x264(pp, p("prog_ps.264"), "--pic-struct")
    for n in FGEN_CASES:
        open(p(f"paff_{n}.264"), "wb").write(_fgen(n))
    return case_names()
