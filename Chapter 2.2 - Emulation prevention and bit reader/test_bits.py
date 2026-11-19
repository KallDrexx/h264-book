#!/usr/bin/env python3
"""Harness for Chapter 2.2 (Emulation prevention and bit reader).

Two independent parts; run either or both.

(a) Per-NAL RBSP check
    python3 test_bits.py <input> <nal_report>
  <input> is the MPEG-TS file (the harness extracts the elementary stream with
  ffmpeg) or a raw Annex B .264 file. <nal_report> is your program's output:
    NAL_COUNT <n>
    RBSP <index> <nal_unit_type> <rbsp_len> <ep_removed> <stop_bit_pos> <fnv1a32_hex>   (n lines)
    SPS_BYTES <byte0> <byte1> <byte2>        (first three RBSP bytes of the first SPS, decimal)
  See the chapter Readme for exact definitions. Truth = this harness's own
  NAL->RBSP conversion of the elementary stream; the SPS bytes are also
  cross-checked against ffmpeg's trace_headers (profile_idc, level_idc) and
  ffprobe -show_streams (profile, level).

(b) Bit-reader unit tests
    python3 test_bits.py --write-vectors <vec_file>
    python3 test_bits.py --vectors <vec_file> <vec_results>
  --write-vectors writes the deterministic test-vector file. Your program reads
  it and writes <vec_results>; the harness recomputes the expected results from
  <vec_file> with its reference bit reader and compares line by line.

Both at once:
    python3 test_bits.py <input> <nal_report> --vectors <vec_file> <vec_results>

    python3 test_bits.py --self-test

Exit 0 = pass, 1 = fail, 2 = missing/malformed reader output or unusable input.
"""
import argparse
import os
import random
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402

PROFILE_NAMES = {"Baseline": 66, "Constrained Baseline": 66, "Main": 77, "Extended": 88, "High": 100,
                 "High 10": 110, "High 10 Intra": 110, "High 4:2:2": 122, "High 4:4:4 Predictive": 244}


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ======================================================================= reference pieces
def scan(b):
    """Annex B scan -> list of NAL unit byte strings (trailing zeros stripped, empty skipped)."""
    starts, p = [], b.find(b"\x00\x00\x01")
    while p >= 0:
        starts.append(p)
        p = b.find(b"\x00\x00\x01", p + 3)
    out = []
    for i, p in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(b)
        while end > p + 3 and b[end - 1] == 0:
            end -= 1
        if end > p + 3:
            out.append(b[p + 3:end])
    return out


def unescape(payload):
    """payload = NAL bytes after the 1-byte header. Returns (rbsp bytes, bytes removed)."""
    out, zeros, removed = bytearray(), 0, 0
    for x in payload:
        if zeros >= 2 and x == 3:
            removed += 1
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out), removed


def fnv1a32(data):
    h = 2166136261
    for x in data:
        h = ((h ^ x) * 16777619) & 0xFFFFFFFF
    return h


def stop_bit_pos(rbsp):
    """Bit offset (0 = MSB of byte 0) of the last 1 bit, or -1 if there is none."""
    i = len(rbsp) - 1
    while i >= 0 and rbsp[i] == 0:
        i -= 1
    if i < 0:
        return -1
    x = rbsp[i]
    low = (x & -x).bit_length() - 1  # index of lowest set bit, 0 = LSB
    return i * 8 + 7 - low


def rbsp_rows(es):
    rows = []
    for i, nal in enumerate(scan(es)):
        rbsp, rem = unescape(nal[1:])
        rows.append(dict(index=i, type=nal[0] & 31, rbsp_len=len(rbsp), ep_removed=rem,
                         stop=stop_bit_pos(rbsp), hash=f"{fnv1a32(rbsp):08x}", rbsp=rbsp, nal=nal))
    return rows


class Err(Exception):
    pass


class BitReader:
    def __init__(self, data):
        self.d, self.pos = data, 0
        self.n = len(data) * 8
        self.stop = stop_bit_pos(data)

    def u(self, n):
        if self.pos + n > self.n:
            raise Err
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z >= 32:
                raise Err
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)

    def aligned(self):
        return self.pos % 8 == 0

    def more_data(self):
        return self.pos < self.stop

    def trailing(self):
        if self.pos >= self.n or self.u(1) != 1:
            return False
        while not self.aligned():
            if self.u(1):
                return False
        return all(x == 0 for x in self.d[self.pos >> 3:])


def run_op(r, op):
    p = op.split()
    try:
        if p[0] == "u":
            return str(r.u(int(p[1])))
        if p[0] == "ue":
            return str(r.ue())
        if p[0] == "se":
            return str(r.se())
        if p[0] == "aligned":
            return "1" if r.aligned() else "0"
        if p[0] == "more_data":
            return "1" if r.more_data() else "0"
        if p[0] == "skip":
            r.u(int(p[1])) if int(p[1]) <= 32 else [r.u(1) for _ in range(int(p[1]))]
            return "ok"
        if p[0] == "trailing":
            return "1" if r.trailing() else "0"
    except Err:
        return "ERR"
    raise ValueError(op)


# ======================================================================= vector file
def parse_vectors(path):
    """-> list of records: ('V', name, data, ops) or ('E', name, nal_bytes)."""
    recs, cur = [], None
    for ln, line in enumerate(open(path).read().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split()
        where = f"error: vector file {path} line {ln}"
        try:
            if p[0] == "VECTOR" and len(p) == 2 and cur is None:
                cur = ["V", p[1], None, []]
            elif p[0] == "HEX" and len(p) == 2 and cur and cur[2] is None and not cur[3]:
                cur[2] = b"" if p[1] == "-" else bytes.fromhex(p[1])
            elif p[0] == "END" and cur and cur[2] is not None:
                recs.append(tuple(cur))
                cur = None
            elif p[0] == "ESCAPE" and len(p) == 3 and cur is None:
                nal = bytes.fromhex(p[2])
                if not nal:
                    die(f"{where}: ESCAPE needs at least the header byte")
                recs.append(("E", p[1], nal))
            elif cur is not None and cur[2] is not None and (
                    (p[0] in ("ue", "se", "aligned", "more_data", "trailing") and len(p) == 1) or
                    (p[0] == "u" and len(p) == 2 and p[1].isdigit() and 1 <= int(p[1]) <= 32) or
                    (p[0] == "skip" and len(p) == 2 and p[1].isdigit())):
                cur[3].append(" ".join(p))
            else:
                die(f"{where}: unexpected {line!r} (see the Readme, section 5.2, for the grammar)")
        except ValueError:
            die(f"{where}: bad hex digits in {line!r}")
    if cur is not None:
        die(f"error: vector file {path}: VECTOR {cur[1]} has no END line")
    names = [r[1] for r in recs]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        die(f"error: vector file {path}: duplicate names {sorted(dup)}")
    return recs


def expected_results(recs):
    """-> list of (name, [lines]) in the exact order/format of the results file."""
    out = []
    for r in recs:
        if r[0] == "V":
            br = BitReader(r[2])
            out.append((r[1], [run_op(br, op) for op in r[3]]))
        else:
            rbsp, rem = unescape(r[2][1:])
            out.append((r[1], ["ESCAPE %s %s %d" % (r[1], rbsp.hex() or "-", rem)]))
    return out


def results_text(recs):
    lines = []
    for r, (name, res) in zip(recs, expected_results(recs)):
        if r[0] == "V":
            lines += [f"VECTOR {name}"] + res + ["END"]
        else:
            lines += res
    return "\n".join(lines) + "\n"


def parse_results(path):
    """-> dict name -> list of lines (for ESCAPE records, the single ESCAPE line)."""
    if not os.path.exists(path):
        die(f"error: results file {path} does not exist")
    res, cur = {}, None
    for l in open(path, errors="replace").read().splitlines():
        l = l.strip()
        if not l:
            continue
        if l.startswith("VECTOR "):
            cur = res.setdefault(l.split()[1], [])
        elif l == "END":
            cur = None
        elif l.startswith("ESCAPE "):
            res["ESC:" + l.split()[1]] = [l]
        elif cur is not None:
            cur.append(l)
        else:
            die(f"error: results line outside any VECTOR block: {l!r}")
    if not res:
        die("error: results file contains no VECTOR or ESCAPE records")
    return res


def check_vectors(vec_file, res_file, rep=None):
    recs = parse_vectors(vec_file)
    got = parse_results(res_file)
    own = rep is None
    rep = rep or Report(f"Chapter 2.2 bit reader ({len(recs)} vectors)")
    nops = 0
    for r, (name, exp) in zip(recs, expected_results(recs)):
        if r[0] == "E":
            a = got.get("ESC:" + name)
            rep.check(a is not None and a[0] == exp[0],
                      f"ESCAPE {name}: NAL {r[2].hex()}: expected {exp[0]!r}, got {a[0] if a else 'no line'!r}")
            nops += 1
            continue
        a = got.get(name)
        if a is None:
            rep.check(False, f"vector {name}: missing from results")
            continue
        rep.check(len(a) == len(exp), f"vector {name}: expected {len(exp)} result lines, got {len(a)}")
        for i, (op, e) in enumerate(zip(r[3], exp)):
            g = a[i] if i < len(a) else "<missing>"
            nops += 1
            rep.check(e == g, f"vector {name} op {i} (0-based) '{op}': expected {e}, got {g}   "
                              f"(data {r[2].hex() or '-'}, bit position before op: "
                              f"{_pos_before(r, i)})")
    if own:
        print(f"{nops} operations checked")
        rep.finish()


def _pos_before(r, i):
    br = BitReader(r[2])
    for op in r[3][:i]:
        run_op(br, op)
    return br.pos


# ======================================================================= vector generation
class BW:
    def __init__(self):
        self.bits = ""

    def u(self, v, n):
        self.bits += format(v, f"0{n}b") if n else ""

    def ue(self, k):
        s = format(k + 1, "b")
        self.bits += "0" * (len(s) - 1) + s

    def se(self, v):
        self.ue(2 * v - 1 if v > 0 else -2 * v)

    def trailing(self):
        self.bits += "1"
        self.bits += "0" * (-len(self.bits) % 8)

    def bytes(self):
        b = self.bits + "0" * (-len(self.bits) % 8)
        return bytes(int(b[i:i + 8], 2) for i in range(0, len(b), 8))


def gen_vectors():
    recs = []

    def V(name, data, ops):
        recs.append(("V", name, data, list(ops)))

    # Table 9-2
    w = BW()
    for k in range(32):
        w.ue(k)
    w.trailing()
    V("table_9_2_ue", w.bytes(), ["more_data"] + ["ue"] * 32 + ["more_data", "trailing"])
    V("table_9_2_first_codes", bytes([0b10100110, 0b01000010, 0b10011000, 0b11100010, 0b00000000]),
      ["ue", "ue", "ue", "ue", "ue", "ue", "ue", "ue"])
    w = BW()
    for k in range(32):
        w.ue(k)
    V("se_mapping_from_codenum", w.bytes(), ["se"] * 31)  # codeNum 0..30 -> 0,1,-1,2,-2,...
    w = BW()
    seq = [0, 1, -1, 2, -2, 100, -100, 32767, -32768, 2147483647, -2147483647]
    for v in seq:
        w.se(v)
    w.trailing()
    V("se_roundtrip", w.bytes(), ["se"] * len(seq) + ["more_data", "trailing"])
    # long ue codes
    big = [2**32 - 2, 2**31 - 1, 2**31, 65535, 65536, 2**32 - 3, 1, 0, 2**16 - 2, 2**24 + 5]
    w = BW()
    for k in big:
        w.ue(k)
    w.trailing()
    V("ue_long_codes", w.bytes(), ["ue"] * len(big) + ["more_data", "trailing"])
    w = BW()
    for k in (2**32 - 2, 2**32 - 3, 2**31, 2**31 - 1):
        w.ue(k)
    V("se_long_codes", w.bytes(), ["se"] * 4)
    # u(n) across byte boundaries
    d = bytes.fromhex("deadbeef0123456789abcdef")
    V("u_across_bytes", d, ["u 3", "aligned", "u 7", "u 13", "aligned", "u 1", "u 8", "u 32", "u 4", "aligned",
                            "u 16", "u 12", "u 1", "u 1", "u 1", "u 1", "aligned"])
    V("u_32_bits", bytes.fromhex("ffffffff0000000180000000"), ["u 32", "u 32", "u 32", "aligned"])
    V("u_1_bit_each", bytes.fromhex("a5"), ["u 1"] * 8 + ["aligned", "u 1"])
    V("u_misaligned_32", bytes.fromhex("0123456789abcdef"), ["u 4", "u 32", "u 28"])
    V("skip_and_align", bytes.fromhex("ff00ff00ff80"), ["skip 3", "aligned", "skip 5", "aligned", "u 8",
                                                        "skip 40", "more_data"])
    w = BW()
    w.u(5, 3); w.ue(7); w.se(-3); w.u(1, 1); w.ue(0); w.u(0xABCD, 16); w.ue(300); w.trailing()
    V("mixed_fields", w.bytes(), ["u 3", "ue", "se", "u 1", "ue", "u 16", "ue", "more_data", "trailing"])
    # more_rbsp_data
    V("more_data_stop_in_byte0", bytes.fromhex("80"), ["more_data", "u 1", "more_data", "aligned"])
    V("more_data_walk", bytes.fromhex("e8"), ["more_data", "u 1", "more_data", "u 1", "more_data", "u 1",
                                             "more_data", "u 1", "more_data", "trailing"])
    V("more_data_two_bytes", bytes.fromhex("ab80"), ["more_data", "u 8", "more_data", "aligned", "trailing"])
    V("more_data_zero_padding", bytes.fromhex("ab80000000"), ["u 8", "more_data", "aligned", "trailing"])
    V("more_data_cabac_zero_words", bytes.fromhex("5c" + "80" + "0000" * 4), ["u 8", "more_data", "trailing"])
    V("more_data_unaligned_stop_zero_tail", bytes.fromhex("f0000000"), ["u 3", "more_data", "u 1", "more_data",
                                                                          "trailing"])
    V("more_data_past_stop", bytes.fromhex("a0"), ["u 3", "more_data", "u 5", "more_data"])
    V("more_data_all_zero", bytes(4), ["more_data", "u 8", "more_data"])
    V("more_data_empty", b"", ["more_data", "aligned"])
    V("more_data_stop_in_last_byte_long", bytes.fromhex("0100"), ["more_data", "u 15", "more_data"])
    # trailing-bits check
    V("trailing_ok_aligned", bytes.fromhex("80"), ["trailing"])
    V("trailing_ok_unaligned", bytes.fromhex("b0"), ["u 3", "trailing"])
    V("trailing_ok_with_zero_words", bytes.fromhex("90" + "00" * 6), ["u 3", "trailing"])
    V("trailing_bad_nonzero_after", bytes.fromhex("8001"), ["trailing"])
    V("trailing_bad_nonzero_in_byte", bytes.fromhex("a8"), ["u 1", "trailing"])
    V("trailing_bad_no_stop_bit", bytes.fromhex("00"), ["trailing"])
    V("trailing_bad_not_at_stop", bytes.fromhex("c0"), ["trailing"])
    V("trailing_no_data", b"", ["trailing"])
    # errors (ERR must be the last op of a vector)
    V("err_read_past_end", bytes.fromhex("ff"), ["u 4", "u 5"])
    V("err_ue_runs_off_end", bytes.fromhex("00"), ["ue"])
    V("err_ue_suffix_cut_short", bytes.fromhex("08"), ["ue"])  # 0000 1 000: needs 4 suffix bits, only 3 left
    V("err_ue_32_leading_zeros", bytes.fromhex("0000000080ffffffff"), ["ue"])
    V("err_ue_31_leading_zeros_ok", bytes.fromhex("00000001" + "ffffffff"), ["ue"])
    V("err_se_runs_off_end", bytes.fromhex("0000"), ["se"])
    V("err_u32_short", bytes.fromhex("010203"), ["u 32"])
    # randomized, built from known values (independent truth: every value is checked at generation time)
    rnd = random.Random(264)
    for t in range(12):
        w, ops, exp = BW(), [], []
        nfields = rnd.randint(5, 30)
        for fi in range(nfields):
            kind = rnd.choice("uuuueeess")
            if kind == "u":
                n = rnd.randint(1, 32)
                v = rnd.getrandbits(n)
                w.u(v, n); ops.append(f"u {n}"); exp.append(str(v))
            elif kind == "e":
                k = rnd.choice([rnd.randint(0, 40), rnd.getrandbits(rnd.randint(1, 32)) % (2**32 - 1)])
                w.ue(k); ops.append("ue"); exp.append(str(k))
            else:
                v = rnd.choice([rnd.randint(-20, 20), rnd.randint(-2**31 + 1, 2**31 - 1)])
                w.se(v); ops.append("se"); exp.append(str(v))
            if rnd.random() < 0.2:
                ops.append("aligned"); exp.append("1" if len(w.bits) % 8 == 0 else "0")
            if rnd.random() < 0.3 and fi < nfields - 1:
                ops.append("more_data"); exp.append("1")  # a stop bit is still ahead
        ops.append("more_data"); exp.append("0")
        w.trailing()
        data = w.bytes() + b"\x00" * rnd.choice((0, 0, 2, 5))
        ops.append("trailing"); exp.append("1")
        # (the final more_data runs after all fields, with the stop bit next: 0)
        recs.append(("V", f"random_built_{t}", data, ops))
        br_ = BitReader(data)
        got = [run_op(br_, o) for o in ops]
        if got != exp:
            raise SystemExit(f"internal error: generated vector random_built_{t} disagrees with its construction")
    for t in range(12):  # arbitrary bytes, expected results come from the reference reader
        n = rnd.randint(1, 12)
        data = bytes(rnd.choice((0, 0, 0, 1, 0x80, rnd.getrandbits(8))) for _ in range(n))
        ops = [rnd.choice(["u 1", "u 5", "u 9", "u 17", "u 32", "ue", "ue", "se", "aligned", "more_data",
                           "trailing", "skip 7"]) for _ in range(rnd.randint(4, 14))]
        br, cut = BitReader(data), None
        for i, o in enumerate(ops):
            if run_op(br, o) == "ERR" or o == "trailing":
                cut = i + 1
                break
        recs.append(("V", f"random_raw_{t}", data, ops[:cut] if cut else ops))
    # NAL -> RBSP
    for i, h in enumerate([
        "6500000301", "41110000" + "03", "41" + "0000030000" + "03", "410003", "41000003" + "03", "41" + "0000" + "03" + "02",
        "41aa" + "000003000003000003", "41aa55", "09", "6580", "4100000300",
        "41" + "000003" + "01" + "000003" + "02",
    ]):
        recs.append(("E", f"esc{i}", bytes.fromhex(h)))
    return recs


def vectors_text(recs):
    out = ["# Chapter 2.2 bit reader test vectors (see the Readme for the grammar)"]
    for r in recs:
        if r[0] == "V":
            out += [f"VECTOR {r[1]}", "HEX " + (r[2].hex() or "-")] + r[3] + ["END"]
        else:
            out.append(f"ESCAPE {r[1]} {r[2].hex()}")
    return "\n".join(out) + "\n"


# ======================================================================= part (a)
def is_ts(data):
    for ps, off in ((188, 0), (192, 4)):
        if len(data) >= ps * 3 and all(data[p] == 0x47 for p in range(off, min(len(data), off + 5 * ps), ps)):
            return True
    return False


def get_es(inp, tmp):
    data = open(inp, "rb").read()
    if not is_ts(data):
        return inp, data
    es = os.path.join(tmp, "ref.264")
    ffref.raw_h264(inp, es)
    if not os.path.exists(es) or os.path.getsize(es) == 0:
        die("error: ffmpeg could not extract an H.264 elementary stream from the input")
    return es, open(es, "rb").read()


def trace_sps(path, tmp):
    """(profile_idc, level_idc) of the first SPS in ffmpeg's trace_headers log, or None."""
    txt = os.path.join(tmp, "trace.txt")
    try:
        ffref.trace_headers(path, txt)
    except Exception:
        return None
    p = l = None
    for line in open(txt, errors="replace"):
        m = re.search(r"\s(profile_idc|level_idc)\s+\S+\s*=\s*(\d+)\s*$", line)
        if m:
            if m.group(1) == "profile_idc" and p is None:
                p = int(m.group(2))
            if m.group(1) == "level_idc" and l is None:
                l = int(m.group(2))
        if p is not None and l is not None:
            return p, l
    return None


def probe_sps(path):
    try:
        j = ffref.probe_json(path, "-show_streams", "-select_streams", "v:0")
        s = j["streams"][0]
        return PROFILE_NAMES.get(s.get("profile")), s.get("level")
    except Exception:
        return None


def parse_report(path):
    if not os.path.exists(path):
        die(f"error: reader output {path} does not exist")
    lines = [l for l in open(path, errors="replace").read().splitlines() if l.strip()]
    if not lines:
        die("error: reader output is empty")
    m = re.fullmatch(r"NAL_COUNT (\d+)", lines[0].strip())
    if not m:
        die(f"error: line 1 must be 'NAL_COUNT <n>', got {lines[0]!r}")
    rows, sps = [], None
    for ln, l in enumerate(lines[1:], 2):
        p = l.split()
        if p and p[0] == "RBSP":
            if len(p) != 7 or not all(re.fullmatch(r"-?\d+", x) for x in p[1:6]) or \
                    not re.fullmatch(r"[0-9a-f]{8}", p[6]):
                die(f"error: line {ln}: expected 'RBSP <index> <type> <rbsp_len> <ep_removed> <stop_bit_pos> "
                    f"<8 lowercase hex digits>', got {l!r}")
            rows.append(dict(zip(("index", "type", "rbsp_len", "ep_removed", "stop", "hash"),
                                 [int(x) for x in p[1:6]] + [p[6]])))
        elif p and p[0] == "SPS_BYTES":
            if len(p) != 4 or not all(re.fullmatch(r"\d+", x) for x in p[1:]):
                die(f"error: line {ln}: expected 'SPS_BYTES <b0> <b1> <b2>' (decimal), got {l!r}")
            sps = tuple(int(x) for x in p[1:])
        else:
            die(f"error: line {ln}: unrecognised line {l!r}")
    if sps is None:
        die("error: missing final 'SPS_BYTES <b0> <b1> <b2>' line")
    return int(m.group(1)), rows, sps


def check_nals(inp, outp, rep):
    with tempfile.TemporaryDirectory() as tmp:
        esp, es = get_es(inp, tmp)
        truth = rbsp_rows(es)
        if not truth:
            die("error: no NAL units found in the elementary stream")
        count, rows, sps = parse_report(outp)
        rep.compare("NAL_COUNT header vs number of RBSP lines", count, len(rows))
        rep.compare("number of NAL units", len(truth), len(rows))
        for i, (t, r) in enumerate(zip(truth, rows)):
            d = [f"{k} expected {t[k]} got {r[k]}" for k in ("index", "type", "rbsp_len", "ep_removed", "stop", "hash")
                 if t[k] != r[k]]
            if d:
                hint = ""
                if "hash" in "".join(d) and t["rbsp_len"] == r["rbsp_len"]:
                    hint = (f"\n         lengths agree but the bytes differ; first RBSP bytes: {t['rbsp'][:16].hex()}"
                            f" (NAL payload bytes before unescaping: {t['nal'][1:20].hex()})")
                rep.check(False, f"NAL {i} (type {t['type']}, NAL size {len(t['nal'])}): " + "; ".join(d) + hint)
            else:
                rep.check(True, "")
        first = next((t for t in truth if t["type"] == 7), None)
        if first is None or len(first["rbsp"]) < 3:
            die("error: no usable SPS in the stream")
        b = tuple(first["rbsp"][:3])
        rep.compare("SPS_BYTES (first three RBSP bytes of first SPS: profile_idc, constraint flags, level_idc)", b, sps)
        tr = trace_sps(esp, tmp)
        if tr:
            rep.compare("SPS profile_idc/level_idc vs ffmpeg trace_headers", tr, (sps[0], sps[2]))
        else:
            print("note: trace_headers gave no SPS fields; that cross-check was skipped")
        pr = probe_sps(esp)
        if pr and pr[0] is not None and pr[1] is not None:
            rep.compare("SPS profile_idc/level_idc vs ffprobe -show_streams", pr, (sps[0], sps[2]))
        else:
            print("note: ffprobe profile/level not usable; that cross-check was skipped")
        removed = sum(t["ep_removed"] for t in truth)
        withcz = sum(1 for t in truth if t["type"] in (1, 5) and t["rbsp"].endswith(b"\x00\x00"))
        if not rep.failures:
            print(f"{len(truth)} NAL units, {removed} emulation prevention bytes removed in total; "
                  f"SPS profile_idc {sps[0]} level_idc {sps[2]}; {withcz} slice RBSPs end in zero bytes "
                  "(cabac_zero_words)")


# ======================================================================= self test
def run_sub(*args):
    p = subprocess.run([sys.executable, os.path.abspath(__file__), *args], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=600)
    return p.returncode, p.stdout.decode() + p.stderr.decode()


def report_text(es):
    rows = rbsp_rows(es)
    out = [f"NAL_COUNT {len(rows)}"]
    out += [f"RBSP {r['index']} {r['type']} {r['rbsp_len']} {r['ep_removed']} {r['stop']} {r['hash']}" for r in rows]
    s = next(r for r in rows if r["type"] == 7)["rbsp"]
    out.append(f"SPS_BYTES {s[0]} {s[1]} {s[2]}")
    return out


def self_test():
    from common import testfiles
    ok = True

    def expect(tag, want, *args):
        nonlocal ok
        rc, out = run_sub(*args)
        print(f"{tag} -> exit {rc} (want {want})")
        if rc == 1:
            print("\n".join("    " + l for l in out.splitlines()[:2]))
        ok &= rc == want
        return out

    with tempfile.TemporaryDirectory() as tmp:
        # part (a)
        for name in ("default", "T5", "T1"):
            ts = testfiles.get(name)
            _, es = get_es(ts, tmp)
            o = os.path.join(tmp, name + ".rep")
            open(o, "w").write("\n".join(report_text(es)) + "\n")
            out = expect(f"{name}.ts correct report", 0, ts, o)
        print("   " + out.strip().splitlines()[-2])
        # variant with cabac_zero_words appended to slice NALs, and extra escapes
        nals = scan(es)
        v = b"".join(b"\x00\x00\x01" + n + (b"\x00\x00\x03\x00\x00\x03" if n[0] & 31 in (1, 5) else b"") for n in nals)
        vp = os.path.join(tmp, "cabaczero.264")
        open(vp, "wb").write(v)
        o = os.path.join(tmp, "cz.rep")
        open(o, "w").write("\n".join(report_text(v)) + "\n")
        out = expect("cabac_zero_words variant .264, correct report", 0, vp, o)
        print("   " + out.strip().splitlines()[-2])
        lines = open(o).read().splitlines()
        sl = next(i for i, l in enumerate(lines) if l.startswith("RBSP") and l.split()[2] in ("1", "5"))

        def mod(tag, f, want):
            l = list(lines)
            f(l)
            q = os.path.join(tmp, tag.replace(" ", "_") + ".rep")
            open(q, "w").write("\n".join(l) + "\n")
            expect(tag, want, vp, q)

        def edit(i, field, fn):
            def g(l):
                p = l[i].split()
                p[field] = fn(p[field])
                l[i] = " ".join(p)
            return g
        mod("corrupt hash", edit(sl, 6, lambda h: format(int(h, 16) ^ 1, "08x")), 1)
        mod("corrupt stop_bit_pos (counted trailing zeros)", edit(sl, 5, lambda x: str(int(x) + 16)), 1)
        mod("corrupt ep_removed", edit(sl, 4, lambda x: str(int(x) + 1)), 1)
        mod("malformed line", lambda l: l.__setitem__(sl, "RBSP garbage"), 2)
        mod("wrong SPS byte", lambda l: l.__setitem__(-1, "SPS_BYTES 66 0 99"), 1)
        # part (b)
        recs = gen_vectors()
        vf, rf = os.path.join(tmp, "v.txt"), os.path.join(tmp, "r.txt")
        open(vf, "w").write(vectors_text(recs))
        res = results_text(recs)
        open(rf, "w").write(res)
        kinds = {l for l in res.splitlines() if l == "ERR"}
        ok &= "ERR" in kinds
        expect("vectors, correct results", 0, "--vectors", vf, rf)
        rl = res.splitlines()
        i = next(j for j, l in enumerate(rl) if l == "VECTOR table_9_2_ue") + 3
        rl2 = list(rl); rl2[i] = str(int(rl2[i]) + 1)
        open(rf + "1", "w").write("\n".join(rl2) + "\n")
        expect("vectors, one wrong ue value", 1, "--vectors", vf, rf + "1")
        rl2 = [l for l in rl if not l.startswith("ESCAPE esc2 ")]
        open(rf + "2", "w").write("\n".join(rl2) + "\n")
        expect("vectors, missing ESCAPE line", 1, "--vectors", vf, rf + "2")
        open(rf + "3", "w").write("nonsense\n")
        expect("vectors, malformed results", 2, "--vectors", vf, rf + "3")
        # combined invocation
        expect("combined run", 0, ts, os.path.join(tmp, "T1.rep"), "--vectors", vf, rf)
        # spot-check the reference against hand-derived values
        r = BitReader(bytes.fromhex("a6 42 98 e2 00"))
        hand = [r.ue() for _ in range(7)]
        exp = [0, 1, 2, 3, 4, 5, 6]
        print("Table 9-2 spot check:", hand, "OK" if hand == exp else "BAD")
        ok &= hand == exp
        ok &= unescape(bytes.fromhex("000003000003"))[0] == bytes(4)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--write-vectors", metavar="VEC_FILE")
    ap.add_argument("--vectors", nargs=2, metavar=("VEC_FILE", "VEC_RESULTS"))
    ap.add_argument("args", nargs="*", help="<input> <nal_report>")
    a = ap.parse_args()
    if a.self_test:
        ffref.require_tools()
        self_test()
    if a.write_vectors:
        open(a.write_vectors, "w").write(vectors_text(gen_vectors()))
        print(f"wrote {a.write_vectors}")
        return
    if len(a.args) not in (0, 2) or (not a.args and not a.vectors):
        ap.error("need <input> <nal_report> and/or --vectors <vec_file> <vec_results>")
    rep = Report("Chapter 2.2 emulation prevention and bit reader")
    if a.args:
        ffref.require_tools()
        if not os.path.exists(a.args[0]):
            die(f"error: no such file {a.args[0]}")
        check_nals(*a.args, rep)
    if a.vectors:
        if not os.path.exists(a.vectors[0]):
            die(f"error: no such vector file {a.vectors[0]}")
        check_vectors(a.vectors[0], a.vectors[1], rep)
    rep.finish()


if __name__ == "__main__":
    main()
