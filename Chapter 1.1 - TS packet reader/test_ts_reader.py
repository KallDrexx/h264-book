#!/usr/bin/env python3
"""Harness for Chapter 1.1 (TS packet reader).

Usage:
  python3 test_ts_reader.py <input.ts|.m2ts> <reader_output.txt>
  python3 test_ts_reader.py --make-variants <input.ts> <dir>
  python3 test_ts_reader.py --self-test

--make-variants writes <dir>/damaged.ts (leading junk, a dropped packet,
junk bytes mid-stream, a truncated last packet) and <dir>/as.m2ts (192-byte
units), so you can test resync, continuity errors and 192-byte detection.

Computes ground truth independently from the file bytes (and cross-checks it
against ffprobe), then compares your reader's output file. Output format is
documented in Readme.md section 5. Exit 0 = pass, 1 = fail, 2 = bad/missing
reader output or unusable input.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402

NPKT = 20   # PKT lines expected
NPCR = 10   # PCR lines expected (if the reader emits any)


# ---------------------------------------------------------------- ground truth
def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def detect(data):
    """Return (packet_size, sync_offset_within_unit) or None."""
    for ps, off in ((188, 0), (192, 4)):
        if len(data) >= off + 1 and all(
                data[p] == 0x47 for p in range(off, min(len(data), off + 5 * ps), ps)):
            return ps, off
    return None


def find_sync(data, pos, ps, off):
    """First unit start >= pos whose sync byte (and the next two, if present) are 0x47."""
    n = len(data)
    p = pos
    while p + ps <= n:
        s = p + off
        if all(data[s + k * ps] == 0x47 for k in range(3) if s + k * ps < n):
            return p
        p += 1
    return None


def truth(data):
    d = detect(data)
    if d is None:
        # junk at the start: try each candidate size by scanning
        for ps, off in ((188, 0), (192, 4)):
            p = find_sync(data, 0, ps, off)
            if p is not None:
                d = (ps, off)
                break
        if d is None:
            die("error: input has no recognizable TS sync pattern")
    ps, off = d
    t = {"size": len(data), "ps": ps, "pkts": [], "skipped": 0}
    pos, n = 0, len(data)
    while pos + ps <= n:
        if data[pos + off] != 0x47:
            q = find_sync(data, pos + 1, ps, off)
            end = q if q is not None else n
            t["skipped"] += end - pos
            if q is None:
                pos = n
                break
            pos = q
            continue
        h = data[pos + off:pos + off + 4]
        pid = ((h[1] & 0x1F) << 8) | h[2]
        pk = dict(index=len(t["pkts"]), offset=pos, tei=h[1] >> 7, pusi=(h[1] >> 6) & 1,
                  pid=pid, afc=(h[3] >> 4) & 3, cc=h[3] & 15, pcr=None)
        if pk["afc"] in (2, 3):
            alen = data[pos + off + 4]
            if alen >= 7 and data[pos + off + 5] & 0x10:
                b = data[pos + off + 6:pos + off + 12]
                base = int.from_bytes(b[:4], "big") << 1 | (b[4] >> 7)
                ext = ((b[4] & 1) << 8) | b[5]
                pk["pcr"] = base * 300 + ext
        t["pkts"].append(pk)
        pos += ps
    t["skipped"] += n - pos if pos < n else 0
    # per-PID counts and continuity errors
    pids = {}
    last = {}
    for pk in t["pkts"]:
        e = pids.setdefault(pk["pid"], [0, 0])
        e[0] += 1
        pid, afc, cc = pk["pid"], pk["afc"], pk["cc"]
        if pid == 0x1FFF or afc == 0:
            continue
        if pid in last:
            exp = (last[pid] + 1) & 15 if afc in (1, 3) else last[pid]
            if cc != exp:
                e[1] += 1
        last[pid] = cc
    t["pids"] = pids
    return t


def expected_lines(t):
    L = [f"FILE_SIZE {t['size']}", f"PACKET_SIZE {t['ps']}", f"PACKETS {len(t['pkts'])}",
         f"SKIPPED_BYTES {t['skipped']}"]
    for pid in sorted(t["pids"]):
        c, e = t["pids"][pid]
        L.append(f"PID {pid:04x} {c} {e}")
    for pk in t["pkts"][:NPKT]:
        L.append(f"PKT {pk['index']} {pk['offset']} {pk['tei']} {pk['pusi']} "
                 f"{pk['pid']:04x} {pk['afc']} {pk['cc']}")
    k = 0
    for pk in t["pkts"]:
        if pk["pcr"] is not None and k < NPCR:
            L.append(f"PCR {pk['index']} {pk['pid']:04x} {pk['pcr']}")
            k += 1
    return L


# ---------------------------------------------------------------- reader output
def parse_output(path):
    """Return dict with header ints, pids {pid:(c,e)}, pkt rows, pcr rows. Exit 2 on junk."""
    if not os.path.isfile(path):
        die(f"error: reader output {path!r} not found")
    out = {"hdr": {}, "pids": {}, "pkt": [], "pcr": []}
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tok = line.split()
            kind = tok[0]
            try:
                if kind in ("FILE_SIZE", "PACKET_SIZE", "PACKETS", "SKIPPED_BYTES") and len(tok) == 2:
                    if kind in out["hdr"]:
                        raise ValueError(f"duplicate {kind} line")
                    out["hdr"][kind] = int(tok[1])
                elif kind == "PID" and len(tok) == 4:
                    if not re.fullmatch(r"[0-9a-f]{4}", tok[1]):
                        raise ValueError("PID must be 4 lowercase hex digits")
                    pid = int(tok[1], 16)
                    if pid in out["pids"]:
                        raise ValueError(f"duplicate PID {tok[1]}")
                    out["pids"][pid] = (int(tok[2]), int(tok[3]))
                elif kind == "PKT" and len(tok) == 8:
                    if not re.fullmatch(r"[0-9a-f]{4}", tok[5]):
                        raise ValueError("PID must be 4 lowercase hex digits")
                    out["pkt"].append(tuple(int(x) for x in tok[1:5]) + (int(tok[5], 16),)
                                      + tuple(int(x) for x in tok[6:8]))
                elif kind == "PCR" and len(tok) == 4:
                    if not re.fullmatch(r"[0-9a-f]{4}", tok[2]):
                        raise ValueError("PID must be 4 lowercase hex digits")
                    out["pcr"].append((int(tok[1]), int(tok[2], 16), int(tok[3])))
                else:
                    raise ValueError("unknown line type or wrong number of fields")
            except ValueError as e:
                die(f"error: {path}:{ln}: {e}: {line!r}")
    for k in ("FILE_SIZE", "PACKET_SIZE", "PACKETS", "SKIPPED_BYTES"):
        if k not in out["hdr"]:
            die(f"error: {path}: missing header line {k}")
    return out


# ---------------------------------------------------------------- comparison
def video_pid_from_ffprobe(ts):
    j = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")
    st = j.get("streams") or []
    if not st:
        return None
    try:
        return int(st[0]["id"], 0)
    except (KeyError, ValueError):
        return None


def check(ts, out_path, title="Chapter 1.1 TS packet reader"):
    with open(ts, "rb") as f:
        data = f.read()
    t = truth(data)
    rep = Report(title)
    # sanity: truth vs ffprobe (not about the reader's work)
    vpid = video_pid_from_ffprobe(ts)
    if t["ps"] == 188 and t["skipped"] == 0:
        starts = {p["offset"]: p for p in t["pkts"]}
        rows = ffref.probe_packets(ts, "pos")
        bad = [r["pos"] for r in rows if r["pos"].isdigit() and
               not (int(r["pos"]) in starts and starts[int(r["pos"])]["pid"] == vpid
                    and starts[int(r["pos"])]["pusi"] == 1)]
        if bad:
            print(f"warning: harness truth disagrees with ffprobe at pos {bad[:3]} (harness bug?)")
    o = parse_output(out_path)
    h = o["hdr"]
    rep.compare("FILE_SIZE", t["size"], h["FILE_SIZE"])
    rep.compare("PACKET_SIZE", t["ps"], h["PACKET_SIZE"])
    rep.compare("PACKETS (file size / packet size for a clean file)", len(t["pkts"]), h["PACKETS"])
    rep.compare("SKIPPED_BYTES", t["skipped"], h["SKIPPED_BYTES"])
    # PID table
    for pid in sorted(set(t["pids"]) | set(o["pids"])):
        e, a = t["pids"].get(pid), o["pids"].get(pid)
        if e is None:
            rep.check(False, f"PID {pid:04x}: reader lists a PID that is not in the file")
        elif a is None:
            rep.check(False, f"PID {pid:04x}: missing from reader output (expected count={e[0]} cc_errors={e[1]})")
        else:
            rep.compare(f"PID {pid:04x} packet count", e[0], a[0])
            rep.compare(f"PID {pid:04x} continuity errors", e[1], a[1])
    rep.compare("sum of PID counts equals PACKETS", h["PACKETS"], sum(c for c, _ in o["pids"].values()))
    if vpid is not None:
        rep.check(vpid in o["pids"], f"video PID 0x{vpid:04x} (from ffprobe) not in reader's PID table")
    # PKT lines (matched by index; line order in the file is free)
    exp = [(p["index"], p["offset"], p["tei"], p["pusi"], p["pid"], p["afc"], p["cc"])
           for p in t["pkts"][:NPKT]]
    compare_by_index(rep, "PKT", ("offset", "tei", "pusi", "pid", "afc", "cc"), exp, o["pkt"],
                     f"one PKT line for each of the first {NPKT} packets")
    # PCR lines (optional)
    if o["pcr"]:
        expp = [(p["index"], p["pid"], p["pcr"]) for p in t["pkts"] if p["pcr"] is not None][:NPCR]
        compare_by_index(rep, "PCR", ("pid", "pcr_27mhz"), expp, o["pcr"],
                         f"PCR lines for the first {NPCR} packets that carry a PCR")
    rep.finish()


def compare_by_index(rep, kind, names, expected, actual, rule):
    """Rows are (index, field...). Report missing/extra indices and each differing field."""
    exp = {r[0]: r[1:] for r in expected}
    act = {}
    for r in actual:
        if r[0] in act:
            rep.check(False, f"{kind} index {r[0]}: listed more than once")
        act[r[0]] = r[1:]
    hexf = {"pid"}
    show = lambda n, v: f"{v:04x}" if n in hexf else str(v)
    for i in sorted(set(exp) | set(act)):
        e, a = exp.get(i), act.get(i)
        if a is None:
            rep.check(False, f"{kind} index {i}: missing (expected {rule})")
        elif e is None:
            rep.check(False, f"{kind} index {i}: unexpected line (expected {rule}; "
                             f"expected indices are {sorted(exp)[:3]}...)")
        else:
            for n, ev, av in zip(names, e, a):
                rep.check(ev == av, f"{kind} index {i}: {n} expected {show(n, ev)}, got {show(n, av)}")


def make_variants(data):
    """Damaged and 192-byte versions of a clean 188-byte stream."""
    damaged = (b"\x12\x47\x99" * 3 + data[:188 * 40] + b"\xaa" * 5 + data[188 * 41:188 * 80]
               + data[188 * 80:-188] + data[-188:-88])
    m2ts = b"".join(b"\0\0\0\0" + data[i:i + 188] for i in range(0, len(data) - 187, 188))
    return damaged, m2ts


# ---------------------------------------------------------------- self test
def self_test():
    with tempfile.TemporaryDirectory() as d:
        ts = os.path.join(d, "av.ts")
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", ts], timeout=120)
        if r.returncode != 0:
            raise SystemExit("self-test: ffmpeg failed: " + r.stderr.decode()[-300:])
        data = open(ts, "rb").read()
        damaged, m2ts = make_variants(data)
        variants = {"clean 188": data, "m2ts 192": m2ts, "junk+truncated": damaged}
        me = os.path.abspath(__file__)
        fails = 0
        for name, blob in variants.items():
            p = os.path.join(d, "v.bin")
            open(p, "wb").write(blob)
            t = truth(blob)
            good = os.path.join(d, "good.txt")
            open(good, "w").write("\n".join(expected_lines(t)) + "\n")
            assert len(t["pids"]) >= 3 or name != "clean 188", t["pids"].keys()
            def run(outf):
                return subprocess.run([sys.executable, me, p, outf], stdin=subprocess.DEVNULL,
                                      capture_output=True, timeout=120)
            res = run(good)
            print(f"[{name}] known-good output ->", "PASS" if res.returncode == 0 else "UNEXPECTED FAIL")
            if res.returncode != 0:
                print(res.stdout.decode()[-500:]); fails += 1
            lines = open(good).read().splitlines()
            # corruption 1: wrong packet count for a PID
            bad1 = [re.sub(r"^(PID \S+ )(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), l, count=1)
                    if l.startswith("PID") and i == next(j for j, x in enumerate(lines) if x.startswith("PID")) else l
                    for i, l in enumerate(lines)]
            if name == "junk+truncated":
                assert t["skipped"] > 0 and sum(e for _, e in t["pids"].values()) > 0, "variant not damaged"
            # corruption 2: wrong cc in first PKT line
            bad2 = []
            done = False
            for l in lines:
                if l.startswith("PKT") and not done:
                    f = l.split(); f[-1] = str((int(f[-1]) + 1) & 15); l = " ".join(f); done = True
                bad2.append(l)
            for tag, bad in (("wrong PID count", bad1), ("wrong cc field", bad2)):
                bp = os.path.join(d, "bad.txt")
                open(bp, "w").write("\n".join(bad) + "\n")
                res = run(bp)
                ok = res.returncode == 1
                print(f"[{name}] corrupted ({tag}) ->", "FAIL as required" if ok else f"UNEXPECTED rc={res.returncode}")
                fails += 0 if ok else 1
        if fails:
            print("SELF-TEST FAILED"); sys.exit(1)
        print("SELF-TEST OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="the .ts (or 192-byte .m2ts) file")
    ap.add_argument("output", nargs="?", help="your reader's output file (Readme section 5)")
    ap.add_argument("--self-test", action="store_true", help="verify the harness itself")
    ap.add_argument("--make-variants", action="store_true",
                    help="write damaged.ts and as.m2ts from <input> into directory <output>")
    a = ap.parse_args()
    if a.make_variants:
        if not a.input or not a.output or not os.path.isfile(a.input):
            die("error: --make-variants needs an existing <input.ts> and an output directory")
        data = open(a.input, "rb").read()
        if truth(data)["ps"] != 188 or len(data) < 188 * 100:
            die("error: --make-variants needs a 188-byte .ts of at least 100 packets")
        os.makedirs(a.output, exist_ok=True)
        damaged, m2ts = make_variants(data)
        for fn, blob in (("damaged.ts", damaged), ("as.m2ts", m2ts)):
            with open(os.path.join(a.output, fn), "wb") as f:
                f.write(blob)
            print("wrote", os.path.join(a.output, fn))
        return
    ffref.require_tools()
    if a.self_test:
        return self_test()
    if not a.input or not a.output:
        ap.error("need <input> and <reader_output> (or --self-test)")
    if not os.path.isfile(a.input):
        die(f"error: input {a.input!r} not found")
    check(a.input, a.output)


if __name__ == "__main__":
    main()
