"""Test-stream generator for Chapter 8.6 (harness helper, not part of the reader's project).

Slice data never depends on the scaling lists (the syntax is the same), so the harness takes an ordinary x264 High stream and
REWRITES its SPS and PPS with a chosen combination of scaling-matrix syntax: the pixels then change, the slice NAL units do not,
and ffmpeg gives the truth for every combination.  The expected matrices of a combination are computed here from the
construction (a fresh, table-driven implementation of Table 7-2 that shares nothing with spoiler_reference/scaling_ref.py
except the numbers of the four default tables).

A header spec is None (no matrix: flag 0) or a list with one entry per scaling list (8 for the SPS; 6 + 2 * transform_8x8_mode_flag
for the PPS): None = list not present (fall-back), 'D' = present with useDefaultScalingMatrixFlag, a list of 16 / 64 integers
(scan order) = explicit values, ('T', list) = explicit values coded with an early nextScale = 0 (run of the last value).
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import bitwriter, ffref  # noqa: E402
import scaling_ref as sr  # noqa: E402  (bit positions inside SPS / PPS, and the default tables)

SMALL_SRC = "mandelbrot=size=128x96:rate=25,noise=alls=12:allf=t"
BASES = {   # name -> (qp, x264 params)
    "b_cabac": (24, "keyint=8:bframes=2:ref=2"),
    "b_cavlc": (24, "keyint=8:bframes=2:ref=2:cabac=0"),
    "b_qp40": (40, "keyint=8:bframes=2"),
    "b_qp12": (12, "keyint=8:bframes=1:cabac=0"),
    "b_no8x8": (26, "keyint=8:bframes=2:cabac=0:8x8dct=0"),
    "b_intra": (22, "keyint=1"),
}


def make_base(out, name, frames=8):
    """x264 High-profile elementary stream (flat matrices) of the given recipe."""
    qp, params = BASES[name]
    ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SMALL_SRC, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
               "-c:v", "libx264", "-profile:v", "high", "-qp", str(qp), "-x264-params", params, "-f", "h264", out], timeout=300)
    if not os.path.exists(out) or not os.path.getsize(out):
        raise SystemExit("ffmpeg/libx264 could not make a base stream")
    return out


# ------------------------------------------------------------------ writing
def wrap(d):
    return (d + 128) % 256 - 128


def write_list(w, spec, size):
    if spec == "D":
        w.se(-8)                                   # nextScale = (8 - 8) % 256 = 0 at j = 0
        return
    trunc = isinstance(spec, tuple)
    vals = list(spec[1] if trunc else spec)
    assert len(vals) == size and all(1 <= v <= 255 for v in vals)
    last = 8
    for j in range(size):
        if trunc and j > 0 and all(v == last for v in vals[j:]):
            w.se(wrap(-last))                      # nextScale = 0: the rest repeats lastScale
            return
        w.se(wrap(vals[j] - last))
        last = vals[j]


def write_section(w, spec, count):
    w.raw(1, 0 if spec is None else 1)
    if spec is not None:
        for i in range(count):
            e = spec[i] if i < len(spec) else None
            w.raw(1, 0 if e is None else 1)
            if e is not None:
                write_list(w, e, 16 if i < 6 else 64)


def bits_of(rbsp, a, b):
    return [(rbsp[k >> 3] >> (7 - (k & 7))) & 1 for k in range(a, b)]


def rewrite(data, sps_spec, pps_spec):
    """Annex B bytes with every SPS / PPS NAL unit rewritten (all other NAL units are copied byte for byte)."""
    out, sps_by_id = bytearray(), {}
    for nal in sr.nal_units(data):
        t = nal[0] & 31
        if t == 7:
            rb = sr.unescape(nal[1:])
            s = sr.parse_sps(rb)
            sps_by_id[s["id"]] = s
            w = bitwriter.W()
            if s["pos_flag"] is None:                      # Baseline / Main SPS: becomes a High SPS (profile_idc 100, 4:2:0, 8 bit)
                w.raw(8, 100)
                w.bits += bits_of(rb, 8, s["pos_id_end"])
                w.ue(1), w.ue(0), w.ue(0), w.raw(1, 0)
                write_section(w, sps_spec, 8)
                w.bits += bits_of(rb, s["pos_id_end"], s["nbits"])
            else:
                w.bits += bits_of(rb, 0, s["pos_flag"])
                write_section(w, sps_spec, 8)
                w.bits += bits_of(rb, s["pos_end"], s["nbits"])
            out += bitwriter.nal(nal[0], w.rbsp())
        elif t == 8:
            rb = sr.unescape(nal[1:])
            p = sr.parse_pps(rb, sps_by_id)
            w = bitwriter.W()
            if p["pos_flag"] is None:                      # PPS without the High extension: add it (transform_8x8_mode_flag 0)
                w.bits += bits_of(rb, 0, p["pos_t8"])
                w.raw(1, 0)
                write_section(w, pps_spec, 6)
                w.se(p["cqp"])
            else:
                w.bits += bits_of(rb, 0, p["pos_flag"])
                write_section(w, pps_spec, 6 + 2 * p["t8"])
                w.bits += bits_of(rb, p["pos_end"], p["nbits"])
            out += bitwriter.nal(nal[0], w.rbsp())
        else:
            out += b"\x00\x00\x00\x01" + nal
    return bytes(out)


def stream_t8(data):
    for nal in sr.nal_units(data):
        if nal[0] & 31 == 8:
            return sr.parse_pps(sr.unescape(nal[1:]), {})["t8"]
    raise SystemExit("no PPS in the stream")


# ------------------------------------------------------------------ expected effective matrices (from the construction)
DEF = [sr.DEF4_INTRA] * 3 + [sr.DEF4_INTER] * 3 + [sr.DEF8_INTRA, sr.DEF8_INTER]
FALL_A = {0: "def", 1: 0, 2: 1, 3: "def", 4: 3, 5: 4, 6: "def", 7: "def"}      # Table 7-2, fall-back rule set A
FALL_B = {0: "sps", 1: 0, 2: 1, 3: "sps", 4: 3, 5: 4, 6: "sps", 7: "sps"}      # fall-back rule set B


def _val(e):
    return e[1] if isinstance(e, tuple) else e


def expected_lists(sps_spec, pps_spec, t8):
    flat = [[16] * 16] * 6 + [[16] * 64] * 2
    if sps_spec is None:
        sps = flat
    else:
        sps = [None] * 8
        for i in range(8):
            e = sps_spec[i] if i < len(sps_spec) else None
            if e == "D":
                sps[i] = DEF[i]
            elif e is not None:
                sps[i] = _val(e)
            else:
                f = FALL_A[i]
                sps[i] = DEF[i] if f == "def" else sps[f]
    if pps_spec is None:
        return sps
    rule = FALL_B if sps_spec is not None else FALL_A
    pps = [None] * 8
    for i in range(8):
        e = pps_spec[i] if i < len(pps_spec) and i < 6 + 2 * t8 else None
        if e == "D":
            pps[i] = DEF[i]
        elif e is not None:
            pps[i] = _val(e)
        else:
            f = rule[i]
            pps[i] = DEF[i] if f == "def" else sps[i] if f == "sps" else pps[f]
    return pps


def raster(lst):
    n = 4 if len(lst) == 16 else 8
    m = [0] * (n * n)
    for k, (i, j) in enumerate(sr.ZZ4 if n == 4 else sr.ZZ8):
        m[i * n + j] = lst[k]
    return m


def render(lists):
    return [f"M4 {k} " + " ".join(map(str, raster(lists[k]))) for k in range(6)] + \
           [f"M8 {k} " + " ".join(map(str, raster(lists[6 + k]))) for k in range(2)]


def expected_dump(sps_spec, pps_spec, t8):
    sps_l = expected_lists(sps_spec, None, t8)
    return "\n".join(["SPS 0 id=0"] + render(sps_l) + ["PPS 0 id=0 sps=0"] + render(expected_lists(sps_spec, pps_spec, t8))) + "\n"


# ------------------------------------------------------------------ the combinations
def ramp4(rng, lo, hi):
    """16 non-decreasing-ish weights along the scan (like real quantisation matrices) with some noise."""
    return [max(1, min(255, lo + (hi - lo) * k // 15 + rng.randint(-2, 2))) for k in range(16)]


def ramp8(rng, lo, hi):
    return [max(1, min(255, lo + (hi - lo) * k // 63 + rng.randint(-2, 2))) for k in range(64)]


def variants(seed=8):
    """name -> (sps_spec, pps_spec, note).  Weights stay in 6..60 so that no 16-bit overflow occurs on the base streams."""
    rng = random.Random(seed)
    L4 = [ramp4(rng, 8 + 2 * k, 30 + 4 * k) for k in range(6)]
    L8 = [ramp8(rng, 9, 46), ramp8(rng, 12, 40), ramp8(rng, 6, 52)]
    full = [L4[0], L4[1], L4[2], L4[3], L4[4], L4[5], L8[0], L8[1]]
    # delta_scale edge cases: +128 (wraps to -128), -128, +127, jump 16 -> 144 -> 16 inside the weakly used high-frequency tail
    edge4 = [16, 17, 18, 18, 19, 20, 21, 22, 22, 23, 144, 144, 16, 16, 60, 8]
    edge8 = [16] * 20 + [144] * 4 + [16] * 20 + [255] * 2 + [1] * 2 + [30] * 16
    return {
        "sps_full": (full, None, "SPS: all 8 lists explicit; no PPS matrix (SPS matrices apply)"),
        "sps_fallA": ([L4[0], None, None, L4[3], None, None, L8[0], None], None,
                      "SPS lists 0, 3, 6 only: 1, 2 follow 0; 4, 5 follow 3; 7 = Default_8x8_Inter (rule A)"),
        "sps_empty": ([None] * 8, None, "seq_scaling_matrix_present_flag = 1 but no list present: every list = its Default table"),
        "sps_alldefault": (["D"] * 8, None, "SPS: all lists useDefaultScalingMatrixFlag"),
        "sps_mixed": (["D", L4[1], None, None, L4[4], None, "D", None], None,
                      "SPS: default / explicit / fall-back mixture (3 -> Default_4x4_Inter, 5 -> list 4, 7 -> Default_8x8_Inter)"),
        "pps_only": (None, [L4[0], None, None, None, L4[4], None, None, L8[1]],
                     "PPS only, SPS flat: absent lists use rule A (list 1 follows 0, list 3 = Default_4x4_Inter, list 6 = default)"),
        "pps_empty_A": (None, [None] * 8, "pic_scaling_matrix_present_flag = 1, no list present, SPS flat: all Default tables"),
        "pps_ruleB": (full, [None, L4[5], None, None, None, None, None, None],
                      "SPS full, PPS with list 1 only: 0, 3, 6, 7 come from the SPS (rule B), 2 follows 1, 4 follows 3, 5 follows 4"),
        "pps_empty_B": (full, [None] * 8, "SPS full, PPS matrix present with no list: rule B gives the SPS matrices"),
        "pps_override": ([L4[0], None, None, L4[3], None, None, L8[0], None],
                         [L4[2], None, L4[1], None, None, None, L8[2], None],
                         "SPS lists 0, 3, 6; PPS lists 0, 2, 6: the PPS decides, absent ones use rule B"),
        "pps_default": (full, ["D"] * 8, "SPS full, PPS all useDefault: the Default tables win over the SPS"),
        "wrap": (["D", [16] * 16, edge4, ("T", [14, 15] + [18] * 14), None, None, edge8, None], None,
                 "delta_scale at +-128 (nextScale wrap-around), values 255 / 1, early nextScale = 0 in a 4x4 list"),
        "trunc": (None, [("T", [10, 12, 14, 16, 18] + [20] * 11), ("T", [9] * 16), "D", ("T", [30] * 16), None, None,
                         ("T", [10, 11, 12, 13, 14, 16, 18] + [20] * 57), "D"],
                  "PPS lists coded with an early nextScale = 0 (runs of the last value), mixed with useDefault"),
    }


def build(base_data, name, seed=8):
    """-> (stream bytes, expected dump text) for the combination `name` applied to a base stream."""
    sps_spec, pps_spec, _ = variants(seed)[name]
    t8 = stream_t8(base_data)
    return rewrite(base_data, sps_spec, pps_spec), expected_dump(sps_spec, pps_spec, t8)
