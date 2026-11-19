#!/usr/bin/env python3
"""Harness for Chapter 1.4 (Elementary stream output).

Usage:
  python3 test_es.py [--ignore-aud] <input.ts> <out.264> <pes_list.txt>
  python3 test_es.py --make-variants <input.ts> <dir>
  python3 test_es.py --self-test

Checks, against ground truth derived from the same .ts:
  1. out.264 is compared byte for byte with the reference produced by
       ffmpeg -i in.ts -map 0:v:0 -c:v copy -f h264 ref.264
     On a difference the harness prints both sizes, the first differing offset,
     a hex window of both files around it, a re-synchronisation analysis (how
     many bytes were inserted or dropped) and hints about likely causes.
  2. pes_list.txt (the chapter 1.3 format: VIDEO_PID, PES_COUNT, PES lines) is
     compared with `ffprobe -show_packets` (pts, dts, size per video packet,
     modulo 2^33), and the payload sizes must add up to the size of out.264.

--ignore-aud: pass the byte comparison when the two files differ only by access
unit delimiter NAL units (type 9). Normally ffmpeg's -c copy to .h264 neither
adds nor removes anything, so the default is a strict comparison.

--make-variants writes <dir>/noaud.ts (no access unit delimiters in the video)
and <dir>/padhdr.ts (PES headers padded with stuffing bytes, bounded
PES_packet_length, different TS packetisation). Run your reader on them too.

The harness never launches ffplay. Exit 0 = pass, 1 = fail, 2 = missing or
malformed reader output / unusable input.
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

MOD = 1 << 33


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


# ------------------------------------------------------------ own demuxer (truth for self-test / variants)
def layout(data):
    for ps, off in ((188, 0), (192, 4)):
        if len(data) >= ps and all(data[p] == 0x47 for p in range(off, min(len(data), off + 5 * ps), ps)):
            return ps, off
    die("error: input is not a clean TS file (no 0x47 every 188 or 192 bytes from the start)")


def packet_info(p):
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
    info = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")
    for s in info.get("streams", []):
        if s.get("codec_name") == "h264":
            try:
                return int(str(s["id"]), 0)
            except (KeyError, ValueError):
                pass
    die("error: ffprobe finds no H.264 video stream in the input")


def parse_ts(b):
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def enc_ts(prefix, v):
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF,
                  (((v >> 15) & 0x7F) << 1) | 1, (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])


def parse_pes(buf):
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
    return dict(sid=buf[3], plen=plen, flags=flags, pts=pts, dts=dts,
                payload=bytes(buf[9 + hl:end]), head=bytes(buf[:9 + hl]))


def own_pes(data, pid):
    ps, off = layout(data)
    units, cur = [], None
    for pos in range(0, len(data) - ps + 1, ps):
        p = data[pos + off:pos + off + 188]
        if p[0] != 0x47:
            continue
        pusi, ppid, start = packet_info(p)
        if ppid != pid or start is None:
            continue
        if pusi:
            if cur is not None:
                units.append(cur)
            cur = bytearray(p[start:])
        elif cur is not None:
            cur += p[start:]
    if cur is not None:
        units.append(cur)
    return [r for r in map(parse_pes, units) if r is not None]


def list_text(pid, rows):
    L = ["VIDEO_PID %04x" % pid, "PES_COUNT %d" % len(rows)]
    for i, r in enumerate(rows):
        L.append("PES %d %02x %d %d %s %s %d" % (
            i, r["sid"], r["plen"], r["flags"],
            "-" if r["pts"] is None else r["pts"], "-" if r["dts"] is None else r["dts"], len(r["payload"])))
    return "\n".join(L) + "\n"


# ------------------------------------------------------------ NAL helpers
def nal_spans(b):
    """[(start, end, type)] of NAL units (start includes a leading zero byte of a 4-byte start code)."""
    starts = [m.start() for m in re.finditer(b"\x00\x00\x01", b)]
    spans = []
    for k, s in enumerate(starts):
        a = s - 1 if s > 0 and b[s - 1] == 0 and (k == 0 or starts[k - 1] + 3 <= s - 1) else s
        nxt = starts[k + 1] if k + 1 < len(starts) else len(b)
        if s + 3 < len(b):
            spans.append((a, nxt, b[s + 3] & 0x1F))
    return spans


def strip_aud(b):
    out, pos = bytearray(), 0
    for a, e, t in nal_spans(b):
        if t == 9:
            out += b[pos:a]
            pos = e
    out += b[pos:]
    return bytes(out)


def nal_at(b, off):
    """Description of the NAL unit containing byte offset off."""
    best = None
    for a, e, t in nal_spans(b):
        if a <= off:
            best = (a, t)
        else:
            break
    return "NAL type %d starting at offset %d" % (best[1], best[0]) if best else "before the first start code"


# ------------------------------------------------------------ comparison with hints
def hexs(b):
    return " ".join("%02x" % x for x in b)


def window(label, b, d, before=16, after=16):
    lo = max(0, d - before)
    seg = b[lo:d + after]
    mark = d - lo
    return "    %-9s @%-8d %s | %s" % (label, lo, hexs(seg[:mark]), hexs(seg[mark:]))


def pes_at(starts, off):
    """Index of the reference PES packet whose payload contains byte offset off (starts = payload offsets)."""
    i = 0
    while i + 1 < len(starts) and starts[i + 1] <= off:
        i += 1
    return i


def analyse(exp, act, starts=()):
    """Return (first_diff_offset, [lines of explanation]).

    starts: offset in the reference of the first payload byte of each PES packet (from ffprobe sizes)."""
    n = min(len(exp), len(act))
    d = next((i for i in range(n) if exp[i] != act[i]), n)
    L = []
    diff = len(act) - len(exp)
    L.append("size: reference %d bytes, yours %d bytes (difference %+d)" % (len(exp), len(act), diff))
    if d == n and len(exp) != len(act):
        if len(act) < len(exp):
            L.append("first difference at offset %d: your file ends there (the reference continues)." % d)
            if d == 0:
                L.append("hint: your file is empty: no PES payload was written at all (wrong PID, every packet "
                         "skipped, or the output was never written/closed).")
            elif starts and d in starts:
                i = list(starts).index(d)
                L.append("hint: your file ends exactly after PES packet %d; the last %d PES packet(s) are missing. "
                         "Typical: the last PES was never flushed at the end of the input." % (i - 1, len(starts) - i))
            else:
                L.append("hint: your file stops in the middle of a PES packet%s: its last TS packets were dropped, "
                         "or the last PES was cut short." % (" (%d)" % pes_at(starts, d) if starts else ""))
        else:
            L.append("first difference at offset %d: the reference ends there, your file continues with %d "
                     "extra bytes." % (d, diff))
            L.append("hint: extra data at the end: a PES packet written twice, or bytes from a packet of another PID.")
        return d, L
    L.append("first differing byte: offset %d (0x%x): reference %02x, yours %02x" % (d, d, exp[d], act[d]))
    L.append("hex window (16 bytes before | 16 bytes from the difference):")
    L.append(window("reference", exp, d))
    L.append(window("yours", act, d))
    L.append("reference: %s; yours: %s" % (nal_at(exp, d), nal_at(act, d)))
    at_pes_start = False
    if starts:
        i = pes_at(starts, d)
        into = d - starts[i]
        L.append("in the reference this is byte %d of the payload of PES packet %d (payload starts at offset %d)"
                 % (into, i, starts[i]))
        at_pes_start = into <= 3
        if at_pes_start:
            L.append("hint: the difference is at the very start of a PES payload: the payload start offset is "
                     "wrong (it is 9 + PES_header_data_length bytes after the 00 00 01 prefix).")
    # re-synchronisation: did bytes get inserted into or dropped from your stream?
    k_ins = k_drop = None
    w = 24
    if d + w <= len(exp):
        i = act.find(exp[d:d + w], d + 1, d + 4096)
        if i >= 0:
            k_ins = i - d
    if d + w <= len(act):
        i = exp.find(act[d:d + w], d + 1, d + 4096)
        if i >= 0:
            k_drop = i - d
    if k_ins is not None and (k_drop is None or k_ins <= k_drop):
        ins = act[d:d + k_ins]
        L.append("after the difference both files agree again once YOUR file has %d extra bytes inserted here:" % k_ins)
        L.append("    inserted: %s%s" % (hexs(ins[:32]), " ..." if k_ins > 32 else ""))
        if re.search(b"\x00\x00\x01[\xe0-\xef]", act[max(0, d - 3):d + k_ins]):
            L.append("hint: the inserted bytes are a PES header (00 00 01 e0 ...): a PES header was copied into "
                     "the output. Payload must start at byte 9 + PES_header_data_length of each PES packet "
                     "(%d bytes here), and the PES header must not be written at all." % k_ins)
        elif ins[:1] == b"\x47":
            L.append("hint: the inserted bytes look like a TS packet header (47 ...): a TS header was copied "
                     "into the output; the payload of a TS packet starts after the 4-byte header and any "
                     "adaptation field.")
        elif all(x in (0x00, 0xFF) for x in ins):
            L.append("hint: the inserted bytes are stuffing (00 / ff): the adaptation field was not skipped "
                     "(payload starts at 5 + adaptation_field_length), or PES header stuffing was copied.")
        elif 170 <= k_ins <= 188:
            L.append("hint: about one TS payload (<= 184 bytes) was written twice: a duplicated packet "
                     "(continuity-counter handling? the same packet processed twice?).")
        else:
            L.append("hint: %d extra bytes: check where the payload of the TS packet / PES packet starts." % k_ins)
    elif k_drop is not None:
        gone = exp[d:d + k_drop]
        L.append("after the difference both files agree again once %d bytes are DROPPED from the reference:" % k_drop)
        L.append("    missing:  %s%s" % (hexs(gone[:32]), " ..." if k_drop > 32 else ""))
        if 150 <= k_drop <= 184:
            L.append("hint: about one TS payload (up to 184 bytes) is missing: a dropped TS packet, e.g. one "
                     "whose payload is the last of a PES, one with adaptation field control 3 mishandled, or "
                     "one skipped because of a continuity-counter or PUSI test.")
        elif k_drop <= 40:
            L.append("hint: the first %d bytes of some payload are missing: payload start too late "
                     "(PES_header_data_length counted twice? header length wrong?) or the adaptation field "
                     "length was over-counted." % k_drop)
        else:
            L.append("hint: %d bytes are missing: a whole PES packet or several TS packets were dropped." % k_drop)
    else:
        L.append("the files do not agree again within 4096 bytes: bytes are changed, not inserted or dropped.")
        L.append("hint: wrong payload offsets that shift everything, a stream of the wrong PID (audio?), or "
                 "output written in text mode (line-ending conversion: open the file as binary).")
    sa, sr = strip_aud(act), strip_aud(exp)
    if sa == sr:
        L.append("note: the two files are identical once access unit delimiters (NAL type 9) are removed. "
                 "ffmpeg's -c copy keeps whatever the TS contains (it does not add or drop AUDs); "
                 "so you dropped or added an AUD, or an AUD split across TS packets was damaged. "
                 "Use --ignore-aud only if you intentionally strip them.")
    elif not at_pes_start and exp[d:d + 6] == b"\x00\x00\x00\x01\x09\xf0" or act[d:d + 6] == b"\x00\x00\x00\x01\x09\xf0":
        L.append("hint: the difference is at an access unit delimiter (00 00 00 01 09 f0).")
    return d, L


# ------------------------------------------------------------ reader's PES list
def load_list(path):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        die(f"error: cannot read PES list {path}: {e}")
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
                bad("second VIDEO_PID line")
            vid = f[1]
        elif f[0] == "PES_COUNT":
            if len(f) != 2 or not f[1].isdigit():
                bad("expected 'PES_COUNT <decimal>'")
            if count is not None:
                bad("second PES_COUNT line")
            count = int(f[1])
        elif f[0] == "PES":
            if len(f) != 8:
                bad("expected 8 fields: PES <index> <stream_id> <pes_length> <flags> <pts> <dts> <size>")
            if not (f[1].isdigit() and re.fullmatch(r"[0-9a-f]{2}", f[2]) and f[3].isdigit()
                    and re.fullmatch(r"[0-3]", f[4]) and f[7].isdigit()
                    and all(x == "-" or x.isdigit() for x in (f[5], f[6]))):
                bad("bad field syntax (see chapter 1.3 section 5)")
            i = int(f[1])
            if i in pes:
                bad("duplicate PES index")
            cv = lambda x: None if x == "-" else int(x)
            pes[i] = dict(pts=cv(f[5]), dts=cv(f[6]), size=int(f[7]), flags=int(f[4]))
        else:
            bad("unknown line type")
    if vid is None:
        die(f"error: {path}: missing VIDEO_PID line")
    if count is None:
        die(f"error: {path}: missing PES_COUNT line")
    if sorted(pes) != list(range(len(pes))):
        die(f"error: {path}: PES indexes must be 0..n-1 without gaps")
    return vid, count, [pes[i] for i in range(len(pes))]


def ffprobe_rows(ts):
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


# ------------------------------------------------------------ main check
def check(ts, out264, listpath, ignore_aud=False):
    if not os.path.exists(out264):
        die(f"error: cannot read {out264}")
    act = open(out264, "rb").read()
    vid, count, rows = load_list(listpath)
    pid = video_pid(ts)
    rep = Report(f"Elementary stream output ({os.path.basename(ts)})")
    ff = ffprobe_rows(ts)
    starts, pos = [], 0
    for _, _, size in ff:
        starts.append(pos)
        pos += size
    with tempfile.TemporaryDirectory() as tmp:
        ref = os.path.join(tmp, "ref.264")
        ffref.raw_h264(ts, ref)
        if not os.path.exists(ref) or os.path.getsize(ref) == 0:
            die("error: ffmpeg could not produce the reference .264 from the input")
        exp = open(ref, "rb").read()

    # 1. byte comparison
    if act == exp:
        rep.check(True, "")
        print("out.264 is byte-identical to the ffmpeg reference (%d bytes)" % len(exp))
    elif ignore_aud and strip_aud(act) == strip_aud(exp):
        rep.check(True, "")
        print("out.264 equals the reference apart from access unit delimiters (accepted: --ignore-aud)")
    else:
        d, lines = analyse(exp, act, starts if pos == len(exp) else ())
        print("out.264 differs from the ffmpeg reference:")
        for l in lines:
            print("  " + l)
        rep.check(False, f"out.264 differs from ref.264 at offset {d} (size {len(act)} vs {len(exp)})")

    # 2. PES list vs ffprobe
    rep.compare("VIDEO_PID", "%04x" % pid, vid)
    rep.compare("PES_COUNT line vs number of PES lines", len(rows), count)
    rep.check(len(ff) == len(rows), f"ffprobe packet count {len(ff)} vs your PES lines {len(rows)}")
    m = lambda v: None if v is None else v % MOD
    for i, ((fp, fd, fs), a) in enumerate(zip(ff, rows)):
        eff = a["dts"] if a["dts"] is not None else a["pts"]
        rep.check(m(fp) == m(a["pts"]), f"PES {i} pts vs ffprobe: ffprobe {fp}, got {a['pts']}")
        rep.check(m(fd) == m(eff), f"PES {i} dts vs ffprobe (your dts, or your pts when '-'): ffprobe {fd}, got {eff}")
        rep.check(fs == a["size"], f"PES {i} payload size vs ffprobe: ffprobe {fs}, got {a['size']}")
    total = sum(r["size"] for r in rows)
    rep.check(ignore_aud or total == len(act), f"sum of PES payload sizes in your list ({total}) != size of your out.264 "
                                 f"({len(act)}): the list and the file were not written from the same data")
    rep.finish()


# ------------------------------------------------------------ variants
def build_pes(head9, hdr_data, payload, bounded=True):
    body = hdr_data + payload
    plen = 3 + len(body)
    pl = plen if (bounded and plen <= 0xFFFF) else 0
    return b"\x00\x00\x01" + bytes([head9[3], pl >> 8, pl & 255, head9[6], head9[7], len(hdr_data)]) + body


def packetise(pid, pes, cc):
    pkts, pos, first = [], 0, True
    while pos < len(pes):
        rem = len(pes) - pos
        h = bytes([0x47, ((1 if first else 0) << 6) | (pid >> 8), pid & 255])
        if rem >= 184:
            pkts.append(h + bytes([0x10 | (cc & 15)]) + pes[pos:pos + 184])
            pos += 184
        else:
            alen = 183 - rem
            af = bytes([alen]) + (b"" if alen == 0 else b"\x00" + b"\xff" * (alen - 1))
            pkts.append(h + bytes([0x30 | (cc & 15)]) + af + pes[pos:])
            pos += rem
        cc += 1
        first = False
    return pkts, cc


def remux(data, pid, transform):
    """Copy the TS, replacing each video PES by transform(pes_dict) -> bytes of the new PES."""
    ps, off = layout(data)
    out, cur, cc = bytearray(), None, 0

    def flush():
        nonlocal cc
        if cur is not None:
            r = parse_pes(cur)
            raw = transform(r) if r else bytes(cur)
            pk, cc = packetise(pid, raw, cc)
            out.extend(b"".join(pk))
    for pos in range(0, len(data) - ps + 1, ps):
        p = data[pos + off:pos + off + 188]
        pusi, ppid, start = packet_info(p)
        if ppid != pid:
            out += p
            continue
        if start is None:
            continue
        if pusi:
            flush()
            cur = bytearray(p[start:])
            out += b""  # video packets are re-emitted at the next flush
        elif cur is not None:
            cur += p[start:]
    flush()
    return bytes(out)
    # note: PAT/PMT packets that appear in the middle are emitted first, which is legal


def make_variants(ts, outdir):
    data = open(ts, "rb").read()
    pid = video_pid(ts)
    os.makedirs(outdir, exist_ok=True)

    def pts_hdr(r):
        h = b""
        if r["flags"] in (2, 3):
            h += enc_ts(3 if r["flags"] == 3 else 2, r["pts"])
        if r["flags"] == 3:
            h += enc_ts(1, r["dts"])
        return h

    def noaud(r):
        return build_pes(r["head"], pts_hdr(r), strip_aud(r["payload"]))

    def padhdr(r):
        return build_pes(r["head"], pts_hdr(r) + b"\xff" * 7, r["payload"])
    # PES units contain whole access units; a lone AUD is only removed where it starts the payload
    for name, fn in (("noaud.ts", noaud), ("padhdr.ts", padhdr)):
        open(os.path.join(outdir, name), "wb").write(remux(data, pid, fn))
        print(f"wrote {os.path.join(outdir, name)}")


# ------------------------------------------------------------ self test
def run_sub(args, want, label):
    r = subprocess.run([sys.executable, os.path.abspath(__file__)] + args, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=300)
    good = r.returncode == want
    tail = (r.stdout.decode().strip().splitlines() or [r.stderr.decode().strip()])[-1]
    print(f"  [{'ok' if good else 'WRONG'}] {label}: exit {r.returncode} (wanted {want}) - {tail}")
    return good, r.stdout.decode()


def self_test():
    from common import testfiles
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        vdir = os.path.join(tmp, "var")
        make_variants(testfiles.get("default"), vdir)
        inputs = [("default", testfiles.get("default")), ("T5", testfiles.get("T5")),
                  ("noaud.ts", os.path.join(vdir, "noaud.ts")), ("padhdr.ts", os.path.join(vdir, "padhdr.ts"))]
        for name, ts in inputs:
            pid = video_pid(ts)
            rows = own_pes(open(ts, "rb").read(), pid)
            es = b"".join(r["payload"] for r in rows)
            o, l = os.path.join(tmp, name + ".264"), os.path.join(tmp, name + ".txt")
            open(o, "wb").write(es)
            open(l, "w").write(list_text(pid, rows))
            g, _ = run_sub([ts, o, l], 0, f"{name}: correct output")
            ok &= g
            if name == "noaud.ts":
                ok &= (b"\x00\x00\x01\x09" not in es)  # the variant really has no AUD
            if name == "padhdr.ts":
                ok &= any(r["head"][8] > 14 for r in rows)
            if name != "default":
                continue
            mid = len(es) // 2

            def bad(tag, data, want, listtext=None, extra=(), expect_text=None):
                nonlocal ok
                p = os.path.join(tmp, tag + ".264")
                open(p, "wb").write(data)
                q = l
                if listtext is not None:
                    q = os.path.join(tmp, tag + ".txt")
                    open(q, "w").write(listtext)
                g, out = run_sub(list(extra) + [ts, p, q], want, tag)
                ok &= g
                if expect_text:
                    t = expect_text in out
                    print(f"  [{'ok' if t else 'WRONG'}] {tag}: output mentions {expect_text!r}")
                    ok &= t
            bad("dropped_packet", es[:mid] + es[mid + 184:], 1, expect_text="DROPPED")
            bad("dup_packet", es[:mid + 184] + es[mid:], 1, expect_text="inserted")
            bad("pes_header_leaked", es[:mid] + rows[3]["head"] + es[mid:], 1, expect_text="PES header")
            bad("truncated", es[:-100], 1, expect_text="middle of a PES packet")
            bad("last_pes_lost", b"".join(r["payload"] for r in rows[:-1]), 1, expect_text="never flushed")
            bad("empty", b"", 1, expect_text="empty")
            bad("pes_headers_kept", b"".join(r["head"] + r["payload"] for r in rows), 1, expect_text="PES header")
            bad("pts_bytes_kept", b"".join(r["head"][9:] + r["payload"] for r in rows), 1,
                expect_text="very start of a PES payload")
            bad("one_byte", es[:mid] + bytes([es[mid] ^ 1]) + es[mid + 1:], 1, expect_text="first differing byte")
            noaud_es = strip_aud(es)
            bad("aud_stripped", noaud_es, 1, expect_text="access unit delimiters")
            bad("aud_stripped_ok", noaud_es, 0, extra=["--ignore-aud"])
            lines = open(l).read().splitlines()
            idx = [i for i, x in enumerate(lines) if x.startswith("PES ")]
            f = lines[idx[2]].split()
            f[6] = str(int(f[6]) + 1) if f[6] != "-" else "5"
            lines[idx[2]] = " ".join(f)
            bad("bad_dts_list", es, 1, listtext="\n".join(lines) + "\n")
        mal = os.path.join(tmp, "mal.txt")
        open(mal, "w").write("PES x y\n")
        g, _ = run_sub([inputs[0][1], os.path.join(tmp, "default.264"), mal], 2, "malformed list")
        ok &= g
        g, _ = run_sub([inputs[0][1], os.path.join(tmp, "nonexistent.264"), mal], 2, "missing out.264")
        ok &= g
    print("SELF-TEST", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--make-variants", action="store_true", help="args: <input.ts> <dir>")
    ap.add_argument("--ignore-aud", action="store_true")
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
    if len(a.args) != 3:
        ap.error("need <input.ts> <out.264> <pes_list.txt>")
    if not os.path.exists(a.args[0]):
        die(f"error: no such file {a.args[0]}")
    check(*a.args, ignore_aud=a.ignore_aud)


if __name__ == "__main__":
    main()
