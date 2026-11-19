#!/usr/bin/env python3
"""Harness for Chapter 1.3 (PES reassembly and timestamps).

Usage:
  python3 test_pes.py <input.ts> <reader_output.txt>
  python3 test_pes.py --make-variants <input.ts> <dir>
  python3 test_pes.py --self-test

--make-variants writes <dir>/bounded.ts (video PES packets that fit get a real,
non-zero PES_packet_length) and <dir>/hightime.ts (every PTS/DTS shifted up by
0x180000000 ticks, so the top bits of the 33-bit fields are set).

Ground truth, two independent sources:
  1. this harness re-does the PES reassembly and header parse itself
     (Readme section 3), and compares every field of every PES line;
  2. `ffprobe -show_packets` (pts, dts, size of the video stream, raw 90 kHz
     ticks), compared row for row on pts, effective dts and size, modulo 2^33.
Output format: Readme section 5. Exit 0 = pass, 1 = fail, 2 = bad/missing
reader output or unusable input.
"""
import argparse
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402

MOD = 1 << 33


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------- ground truth
def layout(data):
    for ps, off in ((188, 0), (192, 4)):
        if len(data) >= ps and all(data[p] == 0x47 for p in range(off, min(len(data), off + 5 * ps), ps)):
            return ps, off
    die("error: input is not a clean TS file (no 0x47 every 188 or 192 bytes from the start)")


def packet_info(p):
    """(pusi, pid, payload_start or None) for one 188-byte packet."""
    pusi = (p[1] >> 6) & 1
    pid = ((p[1] & 0x1F) << 8) | p[2]
    afc = (p[3] >> 4) & 3
    start = None
    if afc == 1:
        start = 4
    elif afc == 3:
        start = 5 + p[4]
        if start > 188:
            start = None
    return pusi, pid, start


def video_pid(ts):
    """PID of the first H.264 stream according to ffprobe (id is printed like '0x100')."""
    info = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")
    for s in info.get("streams", []):
        if s.get("codec_name") == "h264":
            try:
                return int(str(s["id"]), 0)
            except (KeyError, ValueError):
                pass
    die("error: ffprobe finds no H.264 video stream in the input")


def parse_ts(b):
    """33-bit timestamp from 5 bytes (Readme 3.6)."""
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def parse_pes(buf):
    """Parse one reassembled PES unit; return a dict or None if it is not a usable PES."""
    if len(buf) < 9 or buf[0] != 0 or buf[1] != 0 or buf[2] != 1:
        return None
    hl = buf[8]
    if len(buf) < 9 + hl:
        return None
    flags = buf[7] >> 6
    plen = (buf[4] << 8) | buf[5]
    end = len(buf) if plen == 0 else min(len(buf), 6 + plen)
    pts = dts = None
    if flags in (2, 3) and hl >= 5:
        pts = parse_ts(buf[9:14])
    if flags == 3 and hl >= 10:
        dts = parse_ts(buf[14:19])
    return dict(sid=buf[3], plen=plen, flags=flags, pts=pts, dts=dts, size=max(0, end - 9 - hl))


def truth_pes(data, pid):
    ps, off = layout(data)
    out, cur = [], None
    for pos in range(0, len(data) - ps + 1, ps):
        p = data[pos + off:pos + off + 188]
        if p[0] != 0x47:
            continue
        pusi, ppid, start = packet_info(p)
        if ppid != pid or start is None:
            continue
        if pusi:
            if cur is not None:
                out.append(cur)
            cur = bytearray(p[start:])
        elif cur is not None:
            cur += p[start:]
    if cur is not None:
        out.append(cur)
    res = []
    for b in out:
        r = parse_pes(b)
        if r is not None:
            res.append(r)
    return res


def fmt_line(i, r):
    return "PES %d %02x %d %d %s %s %d" % (
        i, r["sid"], r["plen"], r["flags"],
        "-" if r["pts"] is None else r["pts"], "-" if r["dts"] is None else r["dts"], r["size"])


def truth_text(ts):
    data = open(ts, "rb").read()
    pid = video_pid(ts)
    rows = truth_pes(data, pid)
    lines = ["VIDEO_PID %04x" % pid, "PES_COUNT %d" % len(rows)]
    lines += [fmt_line(i, r) for i, r in enumerate(rows)]
    return "\n".join(lines) + "\n"


def ffprobe_rows(ts):
    """[(pts or None, dts or None, size)] for the video stream, raw 90 kHz ticks."""
    p = ffref.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
                   "-show_entries", "packet=pts,dts,size", "-of", "csv=p=0", ts])
    rows = []
    for line in p.stdout.decode().splitlines():
        f = line.strip().split(",")
        if len(f) < 3:
            continue
        num = lambda s: None if s in ("N/A", "") else int(s)
        rows.append((num(f[0]), num(f[1]), int(f[2])))
    return rows


# ---------------------------------------------------------------- reader output
def load(path):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        die(f"error: cannot read reader output {path}: {e}")
    vid = count = None
    pes = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()

        def bad(why):
            die(f"error: {path} line {n}: {why}: {raw!r}")
        if f[0] == "VIDEO_PID":
            if len(f) != 2 or not re.fullmatch(r"[0-9a-f]{4}", f[1]):
                bad("expected 'VIDEO_PID <4 lowercase hex digits>'")
            if vid is not None:
                bad("second VIDEO_PID line (exactly one is allowed)")
            vid = f[1]
        elif f[0] == "PES_COUNT":
            if len(f) != 2 or not f[1].isdigit():
                bad("expected 'PES_COUNT <decimal>'")
            if count is not None:
                bad("second PES_COUNT line (exactly one is allowed)")
            count = int(f[1])
        elif f[0] == "PES":
            if len(f) != 8:
                bad("expected 8 fields: PES <index> <stream_id> <pes_length> <flags> <pts> <dts> <size>")
            if not (f[1].isdigit() and re.fullmatch(r"[0-9a-f]{2}", f[2]) and f[3].isdigit()
                    and f[4] in "0123" and len(f[4]) == 1 and f[7].isdigit()
                    and all(x == "-" or x.isdigit() for x in (f[5], f[6]))):
                bad("bad field syntax (index/length/size decimal, stream_id 2 lowercase hex, "
                    "flags 0-3, pts/dts decimal or '-')")
            i = int(f[1])
            if i in pes:
                bad("duplicate PES index")
            cv = lambda x: None if x == "-" else int(x)
            pes[i] = dict(sid=int(f[2], 16), plen=int(f[3]), flags=int(f[4]),
                          pts=cv(f[5]), dts=cv(f[6]), size=int(f[7]))
        else:
            bad("unknown line type")
    if vid is None:
        die(f"error: {path}: missing VIDEO_PID line")
    if count is None:
        die(f"error: {path}: missing PES_COUNT line")
    if sorted(pes) != list(range(len(pes))):
        missing = sorted(set(range(len(pes))) - set(pes))
        die(f"error: {path}: PES indexes must be 0..n-1 without gaps (first missing index: {missing[0]})")
    return vid, count, [pes[i] for i in range(len(pes))]


# ---------------------------------------------------------------- checks
def show(key, v):
    """Format a field value the way it appears in the reader output."""
    if v is None:
        return "-"
    return "%02x" % v if key == "sid" else str(v)


def check(ts, out_path, title=None):
    data = open(ts, "rb").read()
    pid = video_pid(ts)
    vid, count, rows = load(out_path)
    rep = Report(title or f"PES reassembly and timestamps ({os.path.basename(ts)})")
    rep.compare("VIDEO_PID", "%04x" % pid, vid)
    rep.compare("PES_COUNT line vs number of PES lines", len(rows), count)
    truth = truth_pes(data, pid)
    rep.check(len(truth) == len(rows), f"number of PES lines: expected {len(truth)}, got {len(rows)}")
    for i, (e, a) in enumerate(zip(truth, rows)):
        for k, name in (("sid", "stream_id"), ("plen", "pes_packet_length"), ("flags", "pts_dts_flags"),
                        ("pts", "pts"), ("dts", "dts"), ("size", "payload_size")):
            rep.check(e[k] == a[k], f"PES {i} {name}: expected {show(k, e[k])}, got {show(k, a[k])}")
    ff = ffprobe_rows(ts)
    rep.check(len(ff) == len(rows), f"ffprobe packet count {len(ff)} vs your PES lines {len(rows)}")
    for i, ((fp, fd, fs), a) in enumerate(zip(ff, rows)):
        eff = a["dts"] if a["dts"] is not None else a["pts"]
        m = lambda v: None if v is None else v % MOD
        rep.check(m(fp) == m(a["pts"]), f"PES {i} pts vs ffprobe: ffprobe {fp}, got {show('pts', a['pts'])}")
        rep.check(m(fd) == m(eff), f"PES {i} dts vs ffprobe (your dts, or your pts when dts is '-'): "
                                   f"ffprobe {fd}, got {show('dts', eff)}")
        rep.check(fs == a["size"], f"PES {i} payload size vs ffprobe: ffprobe {fs}, got {a['size']}")
    rep.finish()


# ---------------------------------------------------------------- variants
def enc_ts(prefix, v):
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF,
                  (((v >> 15) & 0x7F) << 1) | 1, (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])


def make_variants(ts, outdir):
    data = open(ts, "rb").read()
    pid = video_pid(ts)
    ps, off = layout(data)
    os.makedirs(outdir, exist_ok=True)
    # locate each PES: (packet positions of the unit)
    units, cur = [], None
    for pos in range(0, len(data) - ps + 1, ps):
        p = data[pos + off:pos + off + 188]
        pusi, ppid, start = packet_info(p)
        if ppid != pid or start is None:
            continue
        if pusi:
            cur = [pos]
            units.append(cur)
        elif cur is not None:
            cur.append(pos)
    bounded = bytearray(data)
    high = bytearray(data)
    nb = 0
    for u in units:
        first = u[0]
        p = data[first + off:first + off + 188]
        _, _, start = packet_info(p)
        base = first + off + start
        if data[base:base + 3] != b"\0\0\1" or start + 9 > 188:
            continue
        total = 0
        for q in u:
            _, _, s = packet_info(data[q + off:q + off + 188])
            total += 188 - s
        if 0 < total - 6 <= 0xFFFF:
            bounded[base + 4] = (total - 6) >> 8
            bounded[base + 5] = (total - 6) & 0xFF
            nb += 1
        flags = data[base + 7] >> 6
        if flags in (2, 3):
            pts = parse_ts(data[base + 9:base + 14])
            high[base + 9:base + 14] = enc_ts(3 if flags == 3 else 2, (pts + 0x180000000) % MOD)
        if flags == 3:
            dts = parse_ts(data[base + 14:base + 19])
            high[base + 14:base + 19] = enc_ts(1, (dts + 0x180000000) % MOD)
    open(os.path.join(outdir, "bounded.ts"), "wb").write(bounded)
    open(os.path.join(outdir, "hightime.ts"), "wb").write(high)
    print(f"wrote {outdir}/bounded.ts ({nb} of {len(units)} PES packets now have a non-zero PES_packet_length)")
    print(f"wrote {outdir}/hightime.ts (all PTS/DTS + 0x180000000)")


# ---------------------------------------------------------------- self test
def self_test():
    from common import testfiles
    import subprocess
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("default", "T5"):
            ts = testfiles.get(name)
            good = os.path.join(tmp, name + ".txt")
            open(good, "w").write(truth_text(ts))
            ok &= run_sub(ts, good, 0, f"{name}: ground-truth output")
            lines = open(good).read().splitlines()
            idx = [i for i, l in enumerate(lines) if l.startswith("PES ")]
            # corruption 1: PTS off by one
            bad1 = list(lines)
            f = bad1[idx[3]].split()
            f[5] = str(int(f[5]) + 1)
            bad1[idx[3]] = " ".join(f)
            p1 = os.path.join(tmp, name + "_bad1.txt")
            open(p1, "w").write("\n".join(bad1) + "\n")
            ok &= run_sub(ts, p1, 1, f"{name}: corrupted PTS")
            # corruption 2: a PES line missing (and renumbered)
            bad2 = [l for i, l in enumerate(lines) if i != idx[5]]
            n = 0
            for i, l in enumerate(bad2):
                if l.startswith("PES "):
                    f = l.split()
                    f[1] = str(n)
                    n += 1
                    bad2[i] = " ".join(f)
            p2 = os.path.join(tmp, name + "_bad2.txt")
            open(p2, "w").write("\n".join(bad2) + "\n")
            ok &= run_sub(ts, p2, 1, f"{name}: dropped PES")
            # corruption 3: payload size off by one
            bad3 = list(lines)
            f = bad3[idx[0]].split()
            f[7] = str(int(f[7]) - 1)
            bad3[idx[0]] = " ".join(f)
            p3 = os.path.join(tmp, name + "_bad3.txt")
            open(p3, "w").write("\n".join(bad3) + "\n")
            ok &= run_sub(ts, p3, 1, f"{name}: wrong payload size")
        # variants
        vdir = os.path.join(tmp, "var")
        make_variants(testfiles.get("default"), vdir)
        for v in ("bounded.ts", "hightime.ts"):
            vp = os.path.join(vdir, v)
            out = os.path.join(tmp, v + ".txt")
            open(out, "w").write(truth_text(vp))
            ok &= run_sub(vp, out, 0, f"variant {v}")
        # the variants must really differ from the clean file
        t0 = truth_text(testfiles.get("default"))
        ok &= (truth_text(os.path.join(vdir, "bounded.ts")) != t0)
        ok &= (truth_text(os.path.join(vdir, "hightime.ts")) != t0)
        # malformed output -> exit 2
        mal = os.path.join(tmp, "mal.txt")
        open(mal, "w").write("PES x y\n")
        ok &= run_sub(testfiles.get("default"), mal, 2, "malformed output")
    print("SELF-TEST", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def run_sub(ts, out, want, label):
    import subprocess
    r = subprocess.run([sys.executable, os.path.abspath(__file__), ts, out], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=300)
    good = r.returncode == want
    tail = (r.stdout.decode().strip().splitlines() or [r.stderr.decode().strip()])[-1]
    print(f"  [{'ok' if good else 'WRONG'}] {label}: exit {r.returncode} (wanted {want}) - {tail}")
    return good


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
        ap.error("need <input.ts> <reader_output.txt>")
    if not os.path.exists(a.args[0]):
        die(f"error: no such file {a.args[0]}")
    check(*a.args)


if __name__ == "__main__":
    main()
