#!/usr/bin/env python3
"""Harness for Chapter 1.2 (PSI tables: PAT and PMT).

Usage:
  python3 test_psi.py <input.ts> <reader_output.txt>
  python3 test_psi.py --make-variants <input.ts> <dir>
  python3 test_psi.py --self-test

--make-variants writes <dir>/crcbad.ts (a CRC-corrupted 2nd PAT and 2nd PMT
section) and <dir>/split.ts (every PMT section enlarged to about 400 bytes so
it spans 3 packets, with the last 6 bytes of each section delivered in the
NEXT section's first packet via a non-zero pointer_field).

Ground truth: the harness reassembles and CRC-checks the sections itself
(Readme sections 3.5 and 3.6), and cross-checks program number, PMT PID, stream
PIDs and the video PID against `ffprobe -show_programs`. Output format:
Readme section 5. Exit 0 = pass, 1 = fail, 2 = bad/missing reader output or
unusable input.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402

H264 = 0x1B


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------- ground truth
def crc32_mpeg2(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def layout(data):
    for ps, off in ((188, 0), (192, 4)):
        if len(data) >= ps and all(data[p] == 0x47 for p in range(off, min(len(data), off + 5 * ps), ps)):
            return ps, off
    die("error: input is not a clean TS file (no 0x47 every 188 or 192 bytes from the start)")


def iter_packets(data):
    """Yield (pusi, pid, payload_bytes_or_None) for each packet."""
    ps, off = layout(data)
    for pos in range(0, len(data) - ps + 1, ps):
        p = data[pos + off:pos + off + 188]
        if p[0] != 0x47:
            continue
        pusi = (p[1] >> 6) & 1
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        payload = None
        if afc in (1, 3):
            start = 4
            if afc == 3:
                start = 5 + p[4]
            if start <= 188:
                payload = p[start:]
        yield pusi, pid, payload


class Assembler:
    """Per-PID section reassembly exactly as described in Readme section 3.5."""

    def __init__(self):
        self.buf = None

    def feed(self, pusi, payload):
        out = []
        if pusi:
            if not payload:
                self.buf = None
                return out
            ptr = payload[0]
            if self.buf is not None:
                self.buf += payload[1:1 + ptr]
                self.extract(out)
            self.buf = bytearray(payload[1 + ptr:])
            self.extract(out)
        elif self.buf is not None:
            self.buf += payload
            self.extract(out)
        return out

    def extract(self, out):
        buf = self.buf
        while buf:
            if buf[0] == 0xFF:        # stuffing: nothing more in this packet/section run
                buf.clear()
                break
            if len(buf) < 3:
                break
            total = 3 + (((buf[1] & 0x0F) << 8) | buf[2])
            if len(buf) < total:
                break
            out.append(bytes(buf[:total]))
            del buf[:total]
        if not buf:
            self.buf = None


def parse_pat(sec):
    """-> list of (program_number, pid) excluding program 0."""
    body = sec[8:-4]
    return [((body[i] << 8) | body[i + 1], ((body[i + 2] & 0x1F) << 8) | body[i + 3])
            for i in range(0, len(body) - 3, 4) if (body[i] << 8) | body[i + 1] != 0]


def parse_pmt(sec):
    """-> (program_number, [(stream_type, pid), ...])"""
    prog = (sec[3] << 8) | sec[4]
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    i, end, streams = 12 + pil, len(sec) - 4, []
    while i + 5 <= end:
        esl = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        streams.append((sec[i], ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]))
        i += 5 + esl
    return prog, streams


def truth(data):
    t = {"sections": [], "programs": {}, "streams": {}, "video_pid": None}
    asm, pmt_pids = {}, {}
    for pusi, pid, payload in iter_packets(data):
        if payload is None or not (pid == 0 or pid in pmt_pids):
            continue
        for sec in asm.setdefault(pid, Assembler()).feed(pusi, payload):
            ok = crc32_mpeg2(sec) == 0
            t["sections"].append((pid, sec[0], len(sec) - 3, int(ok)))
            if not ok:
                continue
            if pid == 0 and sec[0] == 0x00 and not t["programs"]:
                for prog, ppid in parse_pat(sec):
                    t["programs"][prog] = ppid
                    pmt_pids.setdefault(ppid, set()).add(prog)
            elif pid in pmt_pids and sec[0] == 0x02:
                prog, streams = parse_pmt(sec)
                if prog in pmt_pids[pid] and prog not in t["streams"]:
                    t["streams"][prog] = streams
    for prog in sorted(t["streams"]):
        for st, pid in t["streams"][prog]:
            if st == H264 and t["video_pid"] is None:
                t["video_pid"] = pid
    return t


def expected_lines(t):
    L = [f"SECTION {i} {pid:04x} {tid:02x} {ln} {ok}" for i, (pid, tid, ln, ok) in enumerate(t["sections"])]
    for prog in sorted(t["programs"]):
        L.append(f"PROGRAM {prog} {t['programs'][prog]:04x}")
    for prog in sorted(t["streams"]):
        for st, pid in t["streams"][prog]:
            L.append(f"STREAM {prog} {pid:04x} {st:02x}")
    L.append("VIDEO_PID " + ("none" if t["video_pid"] is None else f"{t['video_pid']:04x}"))
    return L


# ---------------------------------------------------------------- reader output
HEX4, HEX2 = r"[0-9a-f]{4}", r"[0-9a-f]{2}"


def parse_output(path):
    if not os.path.isfile(path):
        die(f"error: reader output {path!r} not found")
    o = {"sec": {}, "prog": {}, "stream": [], "video": "unset"}
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tok = line.split()
            try:
                k = tok[0]
                if k == "SECTION" and len(tok) == 6:
                    if not (re.fullmatch(HEX4, tok[2]) and re.fullmatch(HEX2, tok[3])):
                        raise ValueError("pid must be 4 and table_id 2 lowercase hex digits")
                    if tok[5] not in ("0", "1"):
                        raise ValueError("crc_ok must be 0 or 1")
                    idx = int(tok[1])
                    if idx in o["sec"]:
                        raise ValueError(f"duplicate SECTION index {idx}")
                    o["sec"][idx] = (int(tok[2], 16), int(tok[3], 16), int(tok[4]), int(tok[5]))
                elif k == "PROGRAM" and len(tok) == 3:
                    if not re.fullmatch(HEX4, tok[2]):
                        raise ValueError("pmt_pid must be 4 lowercase hex digits")
                    if int(tok[1]) in o["prog"]:
                        raise ValueError("duplicate PROGRAM line")
                    o["prog"][int(tok[1])] = int(tok[2], 16)
                elif k == "STREAM" and len(tok) == 4:
                    if not (re.fullmatch(HEX4, tok[2]) and re.fullmatch(HEX2, tok[3])):
                        raise ValueError("pid must be 4 and stream_type 2 lowercase hex digits")
                    o["stream"].append((int(tok[1]), int(tok[2], 16), int(tok[3], 16)))
                elif k == "VIDEO_PID" and len(tok) == 2:
                    if o["video"] != "unset":
                        raise ValueError("duplicate VIDEO_PID line")
                    if tok[1] == "none":
                        o["video"] = None
                    elif re.fullmatch(HEX4, tok[1]):
                        o["video"] = int(tok[1], 16)
                    else:
                        raise ValueError("VIDEO_PID must be 4 lowercase hex digits or 'none'")
                else:
                    raise ValueError("unknown line type or wrong number of fields")
            except ValueError as e:
                die(f"error: {path}:{ln}: {e}: {line!r}")
    if o["video"] == "unset":
        die(f"error: {path}: missing VIDEO_PID line")
    return o


# ---------------------------------------------------------------- comparison
def ffprobe_programs(ts):
    j = ffref.probe_json(ts, "-show_programs")
    res = []
    for p in j.get("programs") or []:
        streams = []
        for s in p.get("streams") or []:
            try:
                streams.append((int(s["id"], 0), s.get("codec_name"), s.get("codec_type")))
            except (KeyError, ValueError):
                pass
        res.append((p.get("program_id"), p.get("pmt_pid"), streams))
    return res


def check(ts, out_path, title="Chapter 1.2 PSI tables"):
    with open(ts, "rb") as f:
        data = f.read()
    t = truth(data)
    o = parse_output(out_path)
    rep = Report(title)
    # --- sections (the CRC / reassembly proof)
    exp = expected_lines(t)
    exp_sec = {i: s for i, s in enumerate(t["sections"])}
    rep.compare("number of SECTION lines", len(exp_sec), len(o["sec"]))
    sf = lambda s: f"pid={s[0]:04x} table_id={s[1]:02x} section_length={s[2]} crc_ok={s[3]}"
    for i in sorted(set(exp_sec) | set(o["sec"])):
        e, a = exp_sec.get(i), o["sec"].get(i)
        if a is None:
            rep.check(False, f"SECTION {i}: missing (expected {sf(e)})")
        elif e is None:
            rep.check(False, f"SECTION {i}: unexpected extra section ({sf(a)})")
        else:
            for n, ev, av, fmt in zip(("pid", "table_id", "section_length", "crc_ok"), e, a,
                                      ("{:04x}", "{:02x}", "{}", "{}")):
                rep.check(ev == av, f"SECTION {i}: {n} expected {fmt.format(ev)}, got {fmt.format(av)}")
    # --- programs / streams against python truth
    pf = lambda d: "{" + ", ".join(f"{k}: {v:04x}" for k, v in sorted(d.items())) + "}"
    rep.check(t["programs"] == o["prog"], f"PROGRAM lines (program_number: pmt_pid): expected "
                                          f"{pf(t['programs'])}, got {pf(o['prog'])}")
    exp_st = sorted((p, pid, st) for p in t["streams"] for st, pid in t["streams"][p])
    rep.compare_rows("STREAM", exp_st, sorted(o["stream"]),
                     lambda r: f"program {r[0]} pid {r[1]:04x} stream_type {r[2]:02x}")
    hx = lambda v: "none" if v is None else f"{v:04x}"
    rep.check(t["video_pid"] == o["video"], f"VIDEO_PID: expected {hx(t['video_pid'])}, got {hx(o['video'])}")
    # --- independent cross-check with ffprobe (program, PMT PID, stream PIDs, video PID)
    fp = ffprobe_programs(ts)
    if fp:
        for prog, pmt, streams in fp:
            rep.check(prog in o["prog"], f"ffprobe program {prog} not in your PROGRAM lines")
            if prog in o["prog"]:
                rep.check(pmt == o["prog"][prog], f"program {prog} PMT PID: ffprobe says {hx(pmt)}, "
                                                  f"you wrote {hx(o['prog'][prog])}")
            mine = {pid for p, pid, st in o["stream"] if p == prog}
            fpids = sorted(s[0] for s in streams)
            rep.check(fpids == sorted(mine), f"program {prog} elementary PIDs: ffprobe says "
                                             f"{[hx(v) for v in fpids]}, your STREAM lines have {[hx(v) for v in sorted(mine)]}")
            for pid, codec, ctype in streams:
                if codec == "h264":
                    rep.check(any(p == prog and q == pid and st == H264 for p, q, st in o["stream"]),
                              f"ffprobe says PID {pid:04x} is H.264; no STREAM line with stream_type 1b for it")
        vids = [pid for _, _, ss in fp for pid, codec, _ in ss if codec == "h264"]
        if vids:
            rep.check(o["video"] in vids, f"VIDEO_PID {hx(o['video'])} is not an H.264 PID according to "
                                          f"ffprobe (it lists {[hx(v) for v in vids]})")
    rep.finish()


# ---------------------------------------------------------------- variants
def make_packet(pid, pusi, cc, payload):
    assert len(payload) <= 184
    if len(payload) == 184:
        hdr = bytes([0x47, (pusi << 6) | (pid >> 8), pid & 0xFF, 0x10 | cc])
        return hdr + payload
    alen = 183 - len(payload)
    af = bytes([alen]) + (b"\x00" + b"\xff" * (alen - 1) if alen else b"")
    hdr = bytes([0x47, (pusi << 6) | (pid >> 8), pid & 0xFF, 0x30 | cc])
    return hdr + af + payload


def packets_of(data):
    return [bytearray(data[i:i + 188]) for i in range(0, len(data) - 187, 188)]


def section_in_packet(p):
    """Single-packet section starting at pointer_field 0, or None."""
    if not (p[1] & 0x40) or (p[3] >> 4) & 1 == 0 or (p[3] >> 4) & 2:
        return None
    pl = p[4:]
    if pl[0] != 0:
        return None
    ln = 3 + (((pl[2] & 0x0F) << 8) | pl[3])
    return bytes(pl[1:1 + ln]) if ln <= 183 else None


def find_psi_pids(data):
    """(pat_pid=0, pmt_pid) from the first PAT, using the harness's own parser."""
    t = truth(data)
    if not t["programs"]:
        die("error: no valid PAT in input")
    return 0, sorted(t["programs"].values())[0]


def fix_crc(sec):
    sec = bytearray(sec[:-4])
    return bytes(sec) + crc32_mpeg2(sec).to_bytes(4, "big")


def make_crcbad(data):
    pkts = packets_of(data)
    _, pmt = find_psi_pids(data)
    seen = {0: 0, pmt: 0}
    for p in pkts:
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid in seen and section_in_packet(p) is not None:
            seen[pid] += 1
            if seen[pid] == 2:
                sec_len = 3 + (((p[6] & 0x0F) << 8) | p[7])
                # PAT: flip a bit in the CRC itself; PMT: flip a bit in the body
                idx = 5 + sec_len - 1 if pid == 0 else 5 + 12
                p[idx] ^= 0x01
    if min(seen.values()) < 2:
        die("error: need at least two PAT and two PMT sections in the input for crcbad.ts")
    return b"".join(bytes(p) for p in pkts)


def make_split(data):
    pkts = packets_of(data)
    _, pmt = find_psi_pids(data)
    out, cc, prev_tail, last_idx = [], 0, b"", -1
    for i, p in enumerate(pkts):
        if (((p[1] & 0x1F) << 8) | p[2]) == pmt and section_in_packet(p) is not None:
            last_idx = i
    for i, p in enumerate(pkts):
        pid = ((p[1] & 0x1F) << 8) | p[2]
        sec = section_in_packet(p) if pid == pmt else None
        if sec is None:
            if pid == pmt and (p[3] >> 4) & 1:      # non-section PMT-PID packet: renumber CC
                p[3] = (p[3] & 0xF0) | cc
                cc = (cc + 1) & 15
            out.append(bytes(p))
            continue
        # enlarge: two private descriptors (tag 0xC0) of 190 bytes each in program_info
        pil = ((sec[10] & 0x0F) << 8) | sec[11]
        pad = b"".join(bytes([0xC0, 190]) + bytes((j * 7 + k) & 0xFF for j in range(190)) for k in (1, 2))
        body = sec[12:12 + pil] + pad + sec[12 + pil:-4]
        newpil = pil + len(pad)
        hdr = bytearray(sec[:12])
        hdr[10] = (hdr[10] & 0xF0) | (newpil >> 8)
        hdr[11] = newpil & 0xFF
        newlen = len(hdr) + len(body) + 4 - 3
        hdr[1] = (hdr[1] & 0xF0) | (newlen >> 8)
        hdr[2] = newlen & 0xFF
        sec2 = fix_crc(bytes(hdr) + body + b"\0\0\0\0")
        head, tail = sec2[:-6], sec2[-6:]
        stream = bytes([len(prev_tail)]) + prev_tail + head
        chunks = [stream[j:j + 184] for j in range(0, len(stream), 184)]
        for k, c in enumerate(chunks):
            out.append(make_packet(pmt, 1 if k == 0 else 0, cc, c))
            cc = (cc + 1) & 15
        prev_tail = tail
        if i == last_idx:                           # flush the final tail
            out.append(make_packet(pmt, 0, cc, prev_tail))
            cc = (cc + 1) & 15
    return b"".join(out)


# ---------------------------------------------------------------- self test
def self_test():
    with tempfile.TemporaryDirectory() as d:
        ts = os.path.join(d, "av.ts")
        r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.5",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", ts], timeout=120)
        if r.returncode != 0:
            raise SystemExit("self-test: ffmpeg failed: " + r.stderr.decode()[-300:])
        data = open(ts, "rb").read()
        variants = {"clean": data, "crcbad": make_crcbad(data), "split": make_split(data)}
        fails = 0
        me = os.path.abspath(__file__)
        for name, blob in variants.items():
            t = truth(blob)
            nbad = sum(1 for s in t["sections"] if not s[3])
            if name == "clean":
                assert nbad == 0 and len(t["sections"]) >= 4 and t["video_pid"] is not None, t
                assert len(t["streams"][1]) == 2, "expected video+audio"
            elif name == "crcbad":
                assert nbad == 2, f"expected 2 bad sections, got {nbad}"
            else:
                assert nbad == 0 and len(t["sections"]) == len(truth(data)["sections"]), \
                    "split variant must keep every section valid"
                assert any(s[2] > 400 for s in t["sections"])
                assert t["video_pid"] == truth(data)["video_pid"]
            p = os.path.join(d, name + ".ts")
            open(p, "wb").write(blob)
            lines = expected_lines(t)

            def run(ls):
                o = os.path.join(d, "o.txt")
                open(o, "w").write("\n".join(ls) + "\n")
                return subprocess.run([sys.executable, me, p, o], stdin=subprocess.DEVNULL,
                                      capture_output=True, timeout=120)
            res = run(lines)
            ok = res.returncode == 0
            print(f"[{name}] known-good output ->", "PASS" if ok else "UNEXPECTED FAIL")
            if not ok:
                print(res.stdout.decode()[-600:]); fails += 1
            bad1 = [re.sub(r" (\d)$", lambda m: " " + str(1 - int(m.group(1))), l) if l.startswith("SECTION 2 ") else l
                    for l in lines]
            wrong = "0101" if lines[-1] != "VIDEO_PID 0101" else "0102"
            bad2 = [(f"VIDEO_PID {wrong}" if l.startswith("VIDEO_PID") else l) for l in lines]
            for tag, bad in (("flipped crc_ok", bad1), ("wrong VIDEO_PID", bad2)):
                res = run(bad)
                ok = res.returncode == 1
                print(f"[{name}] corrupted ({tag}) ->", "FAIL as required" if ok else f"UNEXPECTED rc={res.returncode}")
                fails += 0 if ok else 1
        if fails:
            print("SELF-TEST FAILED"); sys.exit(1)
        print("SELF-TEST OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="the .ts file")
    ap.add_argument("output", nargs="?", help="your reader's output file (Readme section 5), or a directory for --make-variants")
    ap.add_argument("--self-test", action="store_true", help="verify the harness itself")
    ap.add_argument("--make-variants", action="store_true", help="write crcbad.ts and split.ts into directory <output>")
    a = ap.parse_args()
    ffref.require_tools()
    if a.make_variants:
        if not a.input or not a.output or not os.path.isfile(a.input):
            die("error: --make-variants needs an existing <input.ts> and an output directory")
        data = open(a.input, "rb").read()
        layout(data)
        if layout(data) != (188, 0):
            die("error: --make-variants needs a 188-byte .ts")
        os.makedirs(a.output, exist_ok=True)
        for fn, blob in (("crcbad.ts", make_crcbad(data)), ("split.ts", make_split(data))):
            with open(os.path.join(a.output, fn), "wb") as f:
                f.write(blob)
            print("wrote", os.path.join(a.output, fn))
        return
    if a.self_test:
        return self_test()
    if not a.input or not a.output:
        ap.error("need <input> and <reader_output> (or --self-test)")
    if not os.path.isfile(a.input):
        die(f"error: input {a.input!r} not found")
    check(a.input, a.output)


if __name__ == "__main__":
    main()
