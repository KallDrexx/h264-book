#!/usr/bin/env python3
"""Harness for Chapter 2.1 (Start-code scanner and NAL header).

Usage:
  python3 test_nal.py <input> <nals.txt>
  python3 test_nal.py --make-variants <input.ts> <dir>
  python3 test_nal.py --self-test

<input> is either the MPEG-TS file (the harness extracts the elementary stream
with `ffmpeg -i in.ts -map 0:v:0 -c:v copy -f h264`) or a raw Annex B .264 file
(for example the variants written by --make-variants). It is recognised by its
content: 0x47 sync bytes every 188 (or 192) bytes means TS.

<nals.txt> is your program's output (see the chapter Readme):
  line 1:   NAL_COUNT <n>
  then n:   NAL <index> <offset> <sc_len> <size> <forbidden_zero_bit> <nal_ref_idc> <nal_unit_type>

Checks:
  1. Every line is compared with a reference scanner built into this harness
     (all values decimal; first ~10 mismatches shown with a hex window).
  2. The sequence of (forbidden_zero_bit, nal_ref_idc, nal_unit_type) is
     compared with the NAL list printed by ffmpeg's trace_headers bitstream
     filter (only NAL types ffmpeg traces: 1 5 6 7 8 9 10 11 12 13 19).
  3. The per-type counts are printed on success.

--make-variants writes <dir>/all3.264 (every start code 3 bytes) and
<dir>/padded.264 (random 3/4-byte start codes, leading zeros, trailing zeros
after NALs, extra filler (12), end-of-sequence (10) and unspecified (30) NALs).

Exit 0 = pass, 1 = fail, 2 = missing or malformed reader output / unusable input.
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

TRACED_TYPES = {1, 5, 6, 7, 8, 9, 10, 11, 12, 13, 19}
FIELDS = ("index", "offset", "sc_len", "size", "fzb", "ref_idc", "type")


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ------------------------------------------------------------ reference scanner
def scan(b):
    """Reference Annex B scan. Returns list of dicts (see FIELDS)."""
    starts = []
    p = b.find(b"\x00\x00\x01")
    while p >= 0:
        starts.append(p)
        p = b.find(b"\x00\x00\x01", p + 3)
    rows = []
    for i, p in enumerate(starts):
        data = p + 3
        end = starts[i + 1] if i + 1 < len(starts) else len(b)
        while end > data and b[end - 1] == 0:
            end -= 1
        if end == data:
            continue  # empty NAL unit: ignored
        sc = 4 if p > 0 and b[p - 1] == 0 else 3
        h = b[data]
        rows.append(dict(index=len(rows), offset=p - (sc - 3), sc_len=sc, size=end - data,
                         fzb=h >> 7, ref_idc=(h >> 5) & 3, type=h & 31))
    return rows


def is_ts(data):
    for ps, off in ((188, 0), (192, 4)):
        if len(data) >= ps * 3 and all(data[p] == 0x47 for p in range(off, min(len(data), off + 5 * ps), ps)):
            return True
    return False


def get_es(inp, tmp):
    data = open(inp, "rb").read()
    if not is_ts(data):
        return inp, data, False
    es = os.path.join(tmp, "ref.264")
    ffref.raw_h264(inp, es)
    if not os.path.exists(es) or os.path.getsize(es) == 0:
        die("error: ffmpeg could not extract an H.264 elementary stream from the input")
    return es, open(es, "rb").read(), True


# ------------------------------------------------------------ trace_headers cross-check
def trace_nals(path, tmp):
    """(fzb, ref_idc, type) per NAL after the first 'Packet:' line (skips the Extradata copy)."""
    txt = os.path.join(tmp, "trace.txt")
    try:
        ffref.trace_headers(path, txt)
    except Exception:
        return None
    out, cur, seen = [], {}, False
    for line in open(txt, "r", errors="replace"):
        if "] Packet:" in line:
            seen = True
            continue
        if not seen:
            continue
        m = re.search(r"\]\s+(\d+)\s+(forbidden_zero_bit|nal_ref_idc|nal_unit_type)\s+\S*\s*=\s*(\d+)\s*$", line)
        if not m:
            continue
        pos, name, val = int(m.group(1)), m.group(2), int(m.group(3))
        if name == "forbidden_zero_bit" and pos == 0:
            cur = {"fzb": val}
        elif name == "nal_ref_idc" and pos == 1:
            cur["ref_idc"] = val
        elif name == "nal_unit_type" and pos == 3:
            cur["type"] = val
            out.append((cur.get("fzb", 0), cur.get("ref_idc", 0), val))
    return out


# ------------------------------------------------------------ reader output
def parse_output(path):
    if not os.path.exists(path):
        die(f"error: reader output {path} does not exist")
    lines = open(path, "r", errors="replace").read().splitlines()
    lines = [l for l in lines if l.strip()]
    if not lines:
        die("error: reader output is empty")
    m = re.fullmatch(r"NAL_COUNT (\d+)", lines[0].strip())
    if not m:
        die(f"error: line 1 must be 'NAL_COUNT <n>', got {lines[0]!r}")
    count = int(m.group(1))
    rows = []
    for ln, l in enumerate(lines[1:], 2):
        parts = l.split()
        if len(parts) != 8 or parts[0] != "NAL" or not all(re.fullmatch(r"\d+", x) for x in parts[1:]):
            die(f"error: line {ln} is not 'NAL <index> <offset> <sc_len> <size> <fzb> <ref_idc> <type>' "
                f"with decimal numbers: {l!r}")
        rows.append(dict(zip(FIELDS, map(int, parts[1:]))))
    return count, rows


def hexs(b):
    return " ".join(f"{x:02x}" for x in b)


def fmt_row(r):
    return "NAL " + " ".join(str(r[k]) for k in FIELDS)


def check(inp, outp, quiet_title=None):
    with tempfile.TemporaryDirectory() as tmp:
        esp, es, was_ts = get_es(inp, tmp)
        truth = scan(es)
        if not truth:
            die("error: no start codes found in the elementary stream")
        count, rows = parse_output(outp)
        rep = Report(quiet_title or f"Chapter 2.1 start-code scanner ({len(truth)} NAL units)")
        rep.compare("NAL_COUNT header vs number of NAL lines", count, len(rows))
        rep.compare("number of NAL units", len(truth), len(rows))
        if len(truth) != len(rows):
            t_off, r_off = [t["offset"] for t in truth], [r["offset"] for r in rows]
            k = next((j for j, (x, y) in enumerate(zip(t_off, r_off)) if x != y), min(len(t_off), len(r_off)))
            if k < len(t_off) and t_off[k] not in r_off:
                o = t_off[k]
                rep.check(False, f"first divergence at NAL {k}: you have no NAL starting at offset {o} "
                                 f"(missed start code?); bytes there: {hexs(es[o:o + 12])}")
            elif k < len(r_off):
                rep.check(False, f"first divergence at NAL {k}: your NAL at offset {r_off[k]} is not a NAL unit "
                                 "(false start code, or an empty NAL that should be skipped?)")
        for i, (t, r) in enumerate(zip(truth, rows)):
            if r["index"] != i:
                rep.check(False, f"line {i + 2}: index should be {i}, got {r['index']}")
            diffs = [f"{k} expected {t[k]} got {r[k]}" for k in FIELDS[1:] if t[k] != r[k]]
            if diffs:
                msg = f"NAL {i}: " + "; ".join(diffs)
                if "offset" in "".join(diffs) or "sc_len" in "".join(diffs):
                    o = t["offset"]
                    msg += f"\n         bytes at true start code (offset {o}): {hexs(es[max(0, o - 4):o + 12])}"
                if r["size"] != t["size"] and r["offset"] == t["offset"]:
                    e = t["offset"] + t["sc_len"] + t["size"]
                    msg += (f"\n         expected NAL to end at {e}; bytes around the end: {hexs(es[e - 4:e + 8])}"
                            " (trailing zero bytes are not part of the NAL)")
                rep.check(False, msg)
            else:
                rep.check(True, "")
        # trace_headers cross-check
        tr = trace_nals(esp, tmp)
        if tr is None or not tr:
            print("note: trace_headers produced no NAL list; cross-check skipped")
        else:
            mine = [(r["fzb"], r["ref_idc"], r["type"]) for r in rows if r["type"] in TRACED_TYPES]
            if mine != tr:
                k = next((j for j, (a, b) in enumerate(zip(mine, tr)) if a != b), min(len(mine), len(tr)))
                rep.check(False, f"trace_headers cross-check: your traced-type list has {len(mine)} entries, "
                                 f"ffmpeg lists {len(tr)}; first difference at position {k}: "
                                 f"yours {mine[k] if k < len(mine) else None}, "
                                 f"ffmpeg {tr[k] if k < len(tr) else None} (fzb, nal_ref_idc, nal_unit_type)")
            else:
                rep.check(True, "")
        if not rep.failures:
            hist = {}
            for r in rows:
                hist[r["type"]] = hist.get(r["type"], 0) + 1
            print("per-type counts: " + ", ".join(f"type {t}: {n}" for t, n in sorted(hist.items())))
            print(f"start codes: {sum(r['sc_len'] == 3 for r in rows)} three-byte, "
                  f"{sum(r['sc_len'] == 4 for r in rows)} four-byte")
        rep.finish()


# ------------------------------------------------------------ writing truth / variants
def truth_text(rows):
    return "\n".join([f"NAL_COUNT {len(rows)}"] + [fmt_row(r) for r in rows]) + "\n"


def nal_payloads(es):
    return [es[r["offset"] + r["sc_len"]: r["offset"] + r["sc_len"] + r["size"]] for r in scan(es)]


def make_variants(ts, d):
    os.makedirs(d, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        _, es, _ = get_es(ts, tmp)
    nals = nal_payloads(es)
    open(os.path.join(d, "all3.264"), "wb").write(b"".join(b"\x00\x00\x01" + n for n in nals))
    rnd = random.Random(2100)
    out = bytearray(b"\x00" * 5)  # leading_zero_8bits
    for n in nals:
        if rnd.random() < 0.1:
            out += (b"\x00" * rnd.choice((0, 1, 3))) + b"\x00\x00\x01" + b"\x0c" + b"\xff" * 6 + b"\x80"
        if rnd.random() < 0.05:
            out += b"\x00\x00\x01\x1e\x11\x22\x33"
        out += rnd.choice((b"\x00\x00\x01", b"\x00\x00\x00\x01")) + n
        if n[0] & 31 in (1, 5) and rnd.random() < 0.15:
            out += b"\x00\x00\x01\x0a"
        out += b"\x00" * rnd.choice((0, 0, 1, 2, 7))  # trailing_zero_8bits
    out += b"\x00" * 3
    open(os.path.join(d, "padded.264"), "wb").write(out)
    print(f"wrote {d}/all3.264 and {d}/padded.264")


# ------------------------------------------------------------ self test
def run_sub(inp, outp):
    p = subprocess.run([sys.executable, os.path.abspath(__file__), inp, outp], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=600)
    return p.returncode, p.stdout.decode() + p.stderr.decode()


def self_test():
    from common import testfiles
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        vdir = os.path.join(tmp, "var")
        ts = testfiles.get("default")
        make_variants(ts, vdir)
        inputs = [("default.ts", ts), ("T5.ts", testfiles.get("T5")),
                  ("all3.264", os.path.join(vdir, "all3.264")), ("padded.264", os.path.join(vdir, "padded.264"))]
        for name, inp in inputs:
            _, es, _ = get_es(inp, tmp)
            rows = scan(es)
            o = os.path.join(tmp, name + ".txt")
            open(o, "w").write(truth_text(rows))
            rc, out = run_sub(inp, o)
            print(f"{name}: correct output -> exit {rc} (want 0)")
            ok &= rc == 0
            if name == "all3.264":
                ok &= all(r["sc_len"] == 3 for r in rows)
            if name == "padded.264":
                ok &= {3, 4} <= {r["sc_len"] for r in rows} and {10, 12, 30} <= {r["type"] for r in rows}
            if name != "default.ts":
                continue
            lines = open(o).read().splitlines()

            def bad(tag, newlines, want):
                nonlocal ok
                q = os.path.join(tmp, tag + ".txt")
                open(q, "w").write("\n".join(newlines) + "\n")
                rc, out = run_sub(inp, q)
                print(f"{tag} -> exit {rc} (want {want})")
                if rc == 1:
                    print("\n".join("    " + l for l in out.splitlines()[:3]))
                ok &= rc == want

            l = list(lines)
            p = l[5].split()
            p[7] = str((int(p[7]) + 1) % 32)
            l[5] = " ".join(p)
            bad("corrupt nal_unit_type", l, 1)
            l = list(lines)
            p = l[3].split()
            p[3] = "3" if p[3] == "4" else "4"
            l[3] = " ".join(p)
            bad("corrupt sc_len", l, 1)
            l = list(lines)
            p = l[4].split()
            p[4] = str(int(p[4]) + 1)
            l[4] = " ".join(p)
            bad("corrupt size (trailing zero included)", l, 1)
            l = lines[:1] + lines[1:4] + lines[5:]
            l = [l[0]] + [f"NAL {j} " + " ".join(x.split()[2:]) for j, x in enumerate(l[1:])]
            bad("missing NAL 3", l, 1)
            bad("malformed line", lines[:3] + ["garbage"], 2)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--make-variants", action="store_true", help="args: <input.ts> <dir>")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    ffref.require_tools()
    if a.self_test:
        self_test()
    if a.make_variants:
        if len(a.args) != 2:
            ap.error("--make-variants needs <input.ts> <dir>")
        make_variants(*a.args)
        return
    if len(a.args) != 2:
        ap.error("need <input> <nals.txt>")
    if not os.path.exists(a.args[0]):
        die(f"error: no such file {a.args[0]}")
    check(*a.args)


if __name__ == "__main__":
    main()
