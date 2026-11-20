#!/usr/bin/env python3
"""SPOILER: reference for Chapter 8.6, scaling matrices (spec 7.3.2.1.1.1, 7.4.2.1.1, Table 7-2, 8.5.6 - 8.5.9).

Parses the SPS / PPS scaling-list syntax straight from an Annex B stream, resolves the fall-back rules (A for the SPS, A or B for
the PPS), and hands out for every slice the 6 + 2 EFFECTIVE weight matrices (raster order, m[row][col]).  Also renders the
Chapter 8.6 parse dump (section 5.1 of the Readme).  MUTANT switches ONE deliberately wrong rule on for the harness self-test.

usage: scaling_ref.py <in.264>      prints the dump
"""
import sys

MUTANT = None
MUTANTS = {
    "no_wrap": "nextScale = lastScale + delta_scale without the modulo 256 (a delta near +-128 gives negative or huge weights)",
    "default_ignored": "useDefaultScalingMatrixFlag is ignored: a first value giving nextScale 0 just repeats lastScale (= 8) all along",
    "trunc_to_8": "after nextScale = 0 in the middle of a list the remaining entries are 8 instead of the last value",
    "fallA_list1_default": "fall-back A: list 1 (Intra Cb) uses Default_4x4_Intra instead of list 0",
    "fallA_inter_to_intra": "fall-back A: list 3 (Inter Y) uses list 2 (the previous list) instead of Default_4x4_Inter",
    "fallA_inter8_prev": "fall-back A: list 7 (Inter Y 8x8) uses list 6 instead of Default_8x8_Inter",
    "ppsB_as_A": "a PPS list that is absent falls back to the Default_* tables even when the SPS has a matrix (rule A instead of B)",
    "ppsB_list3_prev": "fall-back B: list 3 takes list 2 (the previous list) instead of the SPS's list 3",
    "ppsB_always": "a PPS list that is absent uses rule B even when the SPS has NO matrix (flat 16 instead of the defaults)",
    "pps_absent_flat": "pic_scaling_matrix_present_flag = 0 gives Flat_16 instead of the SPS's matrices",
    "sps_absent_flat": "seq_scaling_matrix_present_flag = 1 but a list absent: Flat_16 instead of fall-back rule A",
    "list4_raster": "the 16 values of a 4x4 list are placed in raster order instead of the zig-zag scan",
    "list8_raster": "the 64 values of an 8x8 list are placed in raster order instead of the zig-zag scan",
    "pps_six_lists": "a PPS with transform_8x8_mode_flag = 1 reads 6 scaling lists, not 6 + 2",
    "swap_default8": "Default_8x8_Intra and Default_8x8_Inter are swapped",
}
# decoder-level wrong rules (applied in transref.py through hdec.MUTANT) are listed in hdec.DECODER_MUTANTS

# Table 7-3 / 7-4, in zig-zag scan order
DEF4_INTRA = [6, 13, 13, 20, 20, 20, 28, 28, 28, 28, 32, 32, 32, 37, 37, 42]
DEF4_INTER = [10, 14, 14, 20, 20, 20, 24, 24, 24, 24, 27, 27, 27, 30, 30, 34]
DEF8_INTRA = [6, 10, 10, 13, 11, 13, 16, 16, 16, 16, 18, 18, 18, 18, 18, 23, 23, 23, 23, 23, 23, 25, 25, 25, 25, 25, 25, 25,
              27, 27, 27, 27, 27, 27, 27, 27, 29, 29, 29, 29, 29, 29, 29, 31, 31, 31, 31, 31, 31, 33, 33, 33, 33, 33, 36, 36,
              36, 36, 38, 38, 38, 40, 40, 42]
DEF8_INTER = [9, 13, 13, 15, 13, 15, 17, 17, 17, 17, 19, 19, 19, 19, 19, 21, 21, 21, 21, 21, 21, 22, 22, 22, 22, 22, 22, 22,
              24, 24, 24, 24, 24, 24, 24, 24, 25, 25, 25, 25, 25, 25, 25, 27, 27, 27, 27, 27, 27, 28, 28, 28, 28, 28, 30, 30,
              30, 30, 32, 32, 32, 33, 33, 35]
assert len(DEF8_INTRA) == len(DEF8_INTER) == 64 and len(DEF4_INTRA) == len(DEF4_INTER) == 16
FLAT4, FLAT8 = [16] * 16, [16] * 64
HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


def default_table(i):
    d8i, d8p = (DEF8_INTER, DEF8_INTRA) if MUTANT == "swap_default8" else (DEF8_INTRA, DEF8_INTER)
    if i < 3:
        return DEF4_INTRA
    if i < 6:
        return DEF4_INTER
    return d8i if i % 2 == 0 else d8p


def zigzag(n):
    out, i, j, up = [], 0, 0, True
    for _ in range(n * n):
        out.append((i, j))
        if up:
            if j == n - 1:
                i += 1
                up = False
            elif i == 0:
                j += 1
                up = False
            else:
                i, j = i - 1, j + 1
        else:
            if i == n - 1:
                j += 1
                up = True
            elif j == 0:
                i += 1
                up = True
            else:
                i, j = i + 1, j - 1
    return out


ZZ4, ZZ8 = zigzag(4), zigzag(8)


def to_raster(lst):
    """Scan-order list (16 or 64 values) -> matrix rows (weightScale4x4 / 8x8)."""
    n = 4 if len(lst) == 16 else 8
    zz = ZZ4 if n == 4 else ZZ8
    m = [[0] * n for _ in range(n)]
    for k, v in enumerate(lst):
        if (n == 4 and MUTANT == "list4_raster") or (n == 8 and MUTANT == "list8_raster"):
            m[k // n][k % n] = v
        else:
            i, j = zz[k]
            m[i][j] = v
    return m


# ------------------------------------------------------------------ bits
def nal_units(data):
    """Annex B -> list of NAL units (header byte included, still escaped)."""
    out, i, n = [], 0, len(data)
    starts = []
    while i + 2 < n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3)
            i += 3
        else:
            i += 1
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while e > s and data[e - 1] == 0:
            e -= 1
        out.append(bytes(data[s:e]))
    return out


def unescape(b):
    out, zeros = bytearray(), 0
    for x in b:
        if zeros >= 2 and x == 3:
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


class R:
    def __init__(self, rbsp):
        self.d, self.p = rbsp, 0
        self.n = len(rbsp) * 8

    def u(self, k):
        v = 0
        for _ in range(k):
            if self.p >= self.n:
                raise ValueError("ran out of bits")
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 32:
                raise ValueError("bad ue(v)")
        return (1 << z) - 1 + self.u(z)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)

    def payload_bits(self):
        """Number of bits before rbsp_trailing_bits (the last 1 bit)."""
        for i in range(len(self.d) - 1, -1, -1):
            if self.d[i]:
                b = self.d[i]
                tz = (b & -b).bit_length() - 1
                return i * 8 + 7 - tz
        return 0

    def more_rbsp_data(self):
        return self.p < self.payload_bits()


def read_scaling_list(r, size):
    """7.3.2.1.1.1: -> 'D' (useDefaultScalingMatrixFlag) or the list of `size` values in scan order."""
    last, nxt = 8, 8
    out, use_default = [], False
    for j in range(size):
        if nxt != 0:
            delta = r.se()
            if not -128 <= delta <= 127:
                raise ValueError(f"delta_scale {delta} out of range")
            nxt = last + delta if MUTANT == "no_wrap" else (last + delta + 256) % 256
            use_default = (j == 0 and nxt == 0)
        if nxt == 0:
            if j == 0 and MUTANT == "default_ignored":
                v = last
            elif MUTANT == "trunc_to_8" and j > 0:
                v = 8
            else:
                v = last
        else:
            v = nxt
        out.append(v)
        last = v
    if use_default and MUTANT != "default_ignored":
        return "D"
    return out


def read_lists(r, count):
    return [read_scaling_list(r, 16 if i < 6 else 64) if r.u(1) else None for i in range(count)]


# ------------------------------------------------------------------ SPS / PPS
def parse_sps(rbsp):
    r = R(rbsp)
    s = dict(profile=r.u(8))
    r.u(8)
    s["level"] = r.u(8)
    s["id"] = r.ue()
    s["pos_id_end"] = r.p
    s.update(cfi=1, present=0, entries=[], pos_flag=None, pos_end=None)
    if s["profile"] in HIGH_PROFILES:
        s["cfi"] = r.ue()
        if s["cfi"] == 3:
            r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        s["pos_flag"] = r.p
        s["present"] = r.u(1)
        if s["present"]:
            s["entries"] = read_lists(r, 8 if s["cfi"] != 3 else 12)
        s["pos_end"] = r.p
    s["nbits"] = r.payload_bits()
    return s


def parse_pps(rbsp, sps_by_id):
    r = R(rbsp)
    p = dict(id=r.ue(), sps_id=r.ue())
    r.u(1)
    r.u(1)
    if r.ue() != 0:
        raise ValueError("slice groups (FMO) are not supported")
    r.ue()
    r.ue()
    r.u(1)
    r.u(2)
    r.se()
    r.se()
    p["cqp"] = r.se()
    r.u(1)
    r.u(1)
    r.u(1)
    p.update(t8=0, present=0, entries=[], pos_t8=r.p, pos_flag=None, pos_end=None)
    if r.more_rbsp_data():
        p["t8"] = r.u(1)
        p["pos_flag"] = r.p
        p["present"] = r.u(1)
        if p["present"]:
            sps = sps_by_id.get(p["sps_id"])
            cfi = sps["cfi"] if sps else 1
            n = 6 + ((2 if cfi != 3 else 6) * p["t8"] if MUTANT != "pps_six_lists" else 0)
            p["entries"] = read_lists(r, n)
        p["pos_end"] = r.p
        if r.more_rbsp_data():
            p["second_chroma_qp_index_offset"] = r.se()
    p["nbits"] = r.payload_bits()
    return p


def _fall(i, lists, sps_lists, rule_b):
    """Table 7-2: the list used when list i is not present (lists = the lists resolved so far, sps_lists = the SPS's)."""
    if i >= 8:
        return lists[i - 2]
    if rule_b:
        if i in (0, 3, 6, 7):
            return lists[2] if (MUTANT == "ppsB_list3_prev" and i == 3) else sps_lists[i]
        return lists[i - 1]
    if i == 1 and MUTANT == "fallA_list1_default":
        return DEF4_INTRA
    if i == 3 and MUTANT == "fallA_inter_to_intra":
        return lists[2]
    if i == 7 and MUTANT == "fallA_inter8_prev":
        return lists[6]
    if i in (1, 2, 4, 5):
        return lists[i - 1]
    return default_table(i)


def resolve_sps(sps):
    n = 8 if sps["cfi"] != 3 else 12
    if not sps["present"]:
        return [FLAT4] * 6 + [FLAT8] * (n - 6)
    lists = []
    for i in range(n):
        e = sps["entries"][i] if i < len(sps["entries"]) else None
        if e == "D":
            lists.append(default_table(i))
        elif e is None:
            lists.append((FLAT4 if i < 6 else FLAT8) if MUTANT == "sps_absent_flat" else _fall(i, lists, None, False))
        else:
            lists.append(e)
    return lists


def resolve_pps(pps, sps):
    sl = resolve_sps(sps)
    n = len(sl)
    if not pps["present"]:
        return [FLAT4] * 6 + [FLAT8] * (n - 6) if MUTANT == "pps_absent_flat" else sl
    rule_b = bool(sps["present"]) and MUTANT != "ppsB_as_A" or MUTANT == "ppsB_always"
    lists = []
    for i in range(n):
        e = pps["entries"][i] if i < len(pps["entries"]) else None
        if e == "D":
            lists.append(default_table(i))
        elif e is None:
            lists.append(_fall(i, lists, sl, rule_b))
        else:
            lists.append(e)
    return lists


def effective(lists):
    """Scan-order lists -> dict(m4 = six 4x4 matrices, m8 = two 8x8 matrices), raster rows."""
    return dict(m4=[to_raster(lists[i]) for i in range(6)], m8=[to_raster(lists[6]), to_raster(lists[7])])


# ------------------------------------------------------------------ walking a stream
def _walk(data):
    """Yield ('SPS'/'PPS'/'SLICE', parsed dict, effective matrices) in stream order (repeats are NOT removed here)."""
    sps_by_id, pps_by_id = {}, {}
    for nal in nal_units(data):
        t = nal[0] & 31
        if t == 7:
            s = parse_sps(unescape(nal[1:]))
            sps_by_id[s["id"]] = s
            yield "SPS", s, effective(resolve_sps(s)), nal
        elif t == 8:
            p = parse_pps(unescape(nal[1:]), sps_by_id)
            pps_by_id[p["id"]] = p
            sps = sps_by_id.get(p["sps_id"])
            if sps is None:
                raise ValueError(f"PPS {p['id']} refers to the missing SPS {p['sps_id']}")
            yield "PPS", p, effective(resolve_pps(p, sps)), nal
        elif t in (1, 5):
            r = R(unescape(nal[1:]))
            r.ue()
            r.ue()
            pid = r.ue()
            p = pps_by_id.get(pid)
            if p is None or p["sps_id"] not in sps_by_id:
                raise ValueError("a slice refers to a PPS / SPS that is not in the stream")
            yield "SLICE", p, effective(resolve_pps(p, sps_by_id[p["sps_id"]])), nal


def slice_matrices(data):
    return [e for k, _, e, _ in _walk(data) if k == "SLICE"]


def render_dump(data):
    lines, last, count = [], {}, {"SPS": 0, "PPS": 0}
    for kind, d, eff, nal in _walk(data):
        if kind == "SLICE":
            continue
        key = (kind, d["id"])
        if last.get(key) == nal:
            continue                                   # a repeated copy of the same parameter set
        last[key] = nal
        if kind == "SPS":
            lines.append(f"SPS {count[kind]} id={d['id']}")
        else:
            lines.append(f"PPS {count[kind]} id={d['id']} sps={d['sps_id']}")
        count[kind] += 1
        for k in range(6):
            lines.append(f"M4 {k} " + " ".join(str(v) for row in eff["m4"][k] for v in row))
        for k in range(2):
            lines.append(f"M8 {k} " + " ".join(str(v) for row in eff["m8"][k] for v in row))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_dump(open(sys.argv[1], "rb").read()))
