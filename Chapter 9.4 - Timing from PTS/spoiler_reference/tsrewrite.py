#!/usr/bin/env python3
"""SPOILER: TS PES rewriter (the generator for wrapped / discontinuous / PTS-less test streams).  Python 3 stdlib only.

Reads an MPEG-TS written by ffmpeg (one video PES per access unit), and writes a new TS in which, per video PES i (decoding order):
  * PTS and DTS are shifted:   new = (old + delta(i)) mod 2^33          (delta is a function i -> ticks of 1/90000 s)
  * the PTS (and DTS) may be removed (PTS_DTS_flags = 00)               (strip = set of i, or strip_all)
  * PES i+1 may be merged into PES i (one PES carrying two access units; the second one then has no timestamp of its own)
The video PES is re-packetised (new continuity counters, stuffing in an adaptation field, a PCR on the first packet of every PES);
all other PIDs (PAT, PMT, ...) are copied unchanged.  ffmpeg decodes the result exactly like the original (timestamps aside).

  python3 tsrewrite.py IN.ts OUT.ts [--start-pts RAW] [--jump AT DELTA] [--strip-every N] [--strip-all] [--merge I ...]
"""
import argparse
import sys

MOD = 1 << 33
VIDEO_SID = 0xE0


def _ts_bytes(prefix, v):
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF, (((v >> 15) & 0x7F) << 1) | 1,
                  (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])


def _ts_val(b):
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def read_ts(path):
    """-> (video_pid, items) ; items = list of ('pkt', 188 bytes) for non-video packets and ('pes', dict) for video PESs, in file order."""
    data = open(path, "rb").read()
    if len(data) % 188 or data[0] != 0x47:
        raise ValueError(f"{path}: not a 188-byte TS")
    items, cur, vpid = [], None, None
    for off in range(0, len(data), 188):
        p = data[off:off + 188]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = bool(p[1] & 0x40)
        afc = (p[3] >> 4) & 3
        pay = 4
        if afc & 2:
            pay += 1 + p[4]
        body = p[pay:] if afc & 1 else b""
        if vpid is None and pusi and body[:4] == bytes([0, 0, 1, VIDEO_SID]):
            vpid = pid
        if pid != vpid:
            items.append(("pkt", p))
            continue
        if pusi:
            if cur:
                items.append(("pes", cur))
            cur = {"buf": bytearray(), "pcr": None}
        if cur is not None:
            cur["buf"] += body
    if cur:
        items.append(("pes", cur))
    for kind, it in items:
        if kind == "pes":
            b = bytes(it.pop("buf"))
            if b[:3] != b"\0\0\1":
                raise ValueError("bad PES start code")
            fl = b[7] >> 6
            hl = b[8]
            it["pts"] = _ts_val(b[9:14]) if fl & 2 else None
            it["dts"] = _ts_val(b[14:19]) if fl == 3 else None
            it["payload"] = b[9 + hl:]
    return vpid, items


def _packetise(pid, cc, pes, pcr):
    """Split one PES into 188-byte packets (PUSI on the first, PCR on the first, stuffing in the last)."""
    out, pos, first = [], 0, True
    while first or pos < len(pes):
        body = b""
        if first and pcr is not None:
            base = pcr % MOD
            body = bytes([0x10, (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF, (base >> 1) & 0xFF,
                          ((base & 1) << 7) | 0x7E, 0x00])
        cap = 184 - (1 + len(body) if body else 0)
        n = min(cap, len(pes) - pos)
        need = cap - n
        if need:
            if body:
                body += b"\xff" * need
            elif need == 1:
                body = b""
            else:
                body = b"\x00" + b"\xff" * (need - 2)
        has_af = bool(body) or need == 1
        af = (bytes([len(body)]) + body) if has_af else b""
        pkt = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, (0x30 if has_af else 0x10) | (cc & 15)]) + af + pes[pos:pos + n]
        assert len(pkt) == 188, len(pkt)
        out.append(pkt)
        cc += 1
        pos += n
        first = False
    return out, cc


def rewrite(src, dst, delta=lambda i: 0, strip=(), strip_all=False, merge=()):
    """delta(i): ticks added to PES i's PTS/DTS (i = index of the original video PES in file order)."""
    vpid, items = read_ts(src)
    pes_list = [it for k, it in items if k == "pes"]
    merged = {i + 1 for i in merge}                       # PES i+1 is appended to the previous output PES
    result, cc, i = [], 0, -1
    pending = None
    def flush():
        nonlocal cc
        if pending is None:
            return
        q, d, i0 = pending
        pts = None if (strip_all or i0 in strip or q["pts"] is None) else (q["pts"] + d) % MOD
        dts = None if (strip_all or i0 in strip or q["dts"] is None) else (q["dts"] + d) % MOD
        ref = q["dts"] if q["dts"] is not None else q["pts"]
        pcr = None if ref is None else (ref + d - 63000) % MOD
        if pts is None:
            hdr = bytes([0x80, 0x00, 0x00])
        elif dts is None:
            hdr = bytes([0x80, 0x80, 5]) + _ts_bytes(2, pts)
        else:
            hdr = bytes([0x80, 0xC0, 10]) + _ts_bytes(3, pts) + _ts_bytes(1, dts)
        pk, cc = _packetise(vpid, cc, bytes([0, 0, 1, VIDEO_SID, 0, 0]) + hdr + bytes(q["payload"]), pcr)
        result.extend(pk)
    for kind, it in items:
        if kind == "pkt":
            result.append(it)
            continue
        i += 1
        if i in merged and pending is not None:
            pending[0]["payload"] = bytes(pending[0]["payload"]) + bytes(it["payload"])
            continue
        flush()
        pending = (dict(it), delta(i), i)
    flush()
    open(dst, "wb").write(b"".join(result))
    return {"pes_in": len(pes_list), "pes_out": len(pes_list) - len(merged & set(range(len(pes_list)))), "video_pid": vpid}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--start-pts", type=int, help="shift so the first PES PTS-or-DTS-order minimum becomes this raw value")
    ap.add_argument("--jump", nargs=2, type=int, metavar=("AT", "DELTA"), help="from PES index AT on, add DELTA ticks")
    ap.add_argument("--strip-every", type=int, default=0)
    ap.add_argument("--strip-all", action="store_true")
    ap.add_argument("--merge", type=int, nargs="*", default=())
    a = ap.parse_args(argv)
    _v, items = read_ts(a.src)
    pes = [it for k, it in items if k == "pes"]
    base = min(p["pts"] for p in pes if p["pts"] is not None)
    off = 0 if a.start_pts is None else a.start_pts - base
    at, dj = a.jump if a.jump else (1 << 60, 0)
    strip = {i for i in range(1, len(pes)) if a.strip_every and i % a.strip_every == 0}
    print(rewrite(a.src, a.dst, lambda i: off + (dj if i >= at else 0), strip, a.strip_all, set(a.merge)))


if __name__ == "__main__":
    main()
