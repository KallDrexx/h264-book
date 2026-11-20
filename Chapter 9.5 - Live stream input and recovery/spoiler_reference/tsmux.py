"""SPOILER: a small TS muxer for the Chapter 9.5 stream generator.  Reads the video PESs of an ffmpeg-written .ts and writes
a new TS with our own PAT / PMT (CRC-32/MPEG-2), optional audio-like filler PID (private data, stream_type 6), per-PID continuity counters, optional
table / PID / version changes and CC resets.  Every output packet is a dict so the damage generators can tell which access unit
(PES) it belongs to.  Python 3 standard library only."""
MOD = 1 << 33
PMT_PID, AUD_PID = 0x1000, 0x101


def crc32_mpeg(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _ts_bytes(prefix, v):
    return bytes([(prefix << 4) | (((v >> 30) & 7) << 1) | 1, (v >> 22) & 0xFF, (((v >> 15) & 0x7F) << 1) | 1,
                  (v >> 7) & 0xFF, ((v & 0x7F) << 1) | 1])


def _ts_val(b):
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def read_pes(path):
    """Video PESs of the first PES-carrying video PID of an ffmpeg .ts: list of dict(pts, dts, payload)."""
    data = open(path, "rb").read()
    if len(data) % 188 or data[:1] != b"\x47":
        raise ValueError(f"{path}: not a 188-byte TS")
    vpid, cur, bufs = None, None, []
    for off in range(0, len(data), 188):
        p = data[off:off + 188]
        pid, pusi, afc = ((p[1] & 0x1F) << 8) | p[2], p[1] & 0x40, (p[3] >> 4) & 3
        body = p[(5 + p[4]) if afc & 2 else 4:] if afc & 1 else b""
        if vpid is None and pusi and body[:4] == b"\x00\x00\x01\xe0":
            vpid = pid
        if pid != vpid:
            continue
        if pusi:
            cur = bytearray()
            bufs.append(cur)
        if cur is not None:
            cur += body
    out = []
    for b in bufs:
        fl, hl = b[7] >> 6, b[8]
        out.append({"pts": _ts_val(b[9:14]) if fl & 2 else None, "dts": _ts_val(b[14:19]) if fl == 3 else None,
                    "payload": bytes(b[9 + hl:])})
    return out


def _packetise(pid, cc, pes, pcr):
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
        out.append(bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, (0x30 if has_af else 0x10) | (cc & 15)])
                   + af + pes[pos:pos + n])
        cc += 1
        pos += n
        first = False
    return out, cc


def _section(table_id, ident, version, body):
    sec = bytes([table_id, 0xB0 | ((len(body) + 9) >> 8), (len(body) + 9) & 0xFF, ident >> 8, ident & 0xFF,
                 0xC1 | (version << 1), 0, 0]) + body
    return sec + crc32_mpeg(sec).to_bytes(4, "big")


def pat_section(version):
    return _section(0, 1, version, bytes([0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))


def pmt_section(version, vpid, extra=()):
    es = bytes([0x1B, 0xE0 | (vpid >> 8), vpid & 0xFF, 0xF0, 0]) + bytes([0x06, 0xE0 | (AUD_PID >> 8), AUD_PID & 0xFF, 0xF0, 0])
    for pid in extra:
        es += bytes([0x06, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0])
    return _section(2, 1, version, bytes([0xE0 | (vpid >> 8), vpid & 0xFF, 0xF0, 0]) + es)


def _psi_packet(pid, cc, section):
    body = bytes([0]) + section
    return bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | (cc & 15)]) + body + b"\xff" * (184 - len(body))


def mux(aus, psi_every=5, psi_from=0, vpid=lambda i: 0x100, pmt_version=lambda i: 0, extra_es=lambda i: (),
        force_psi=(), reset_cc=(), audio_every=4):
    """aus: list of dict(pts, dts, payload) (already offset).  -> list of packet dicts
    {"b": 188 bytes, "au": index or None, "pos": packet number inside the PES, "n": packets in the PES, "pid": pid}.
    Tables go before AU i when i >= psi_from and (i - psi_from) % psi_every == 0, or i in force_psi.
    reset_cc: AU indices before which every continuity counter restarts at 0 (a sender restart)."""
    out, cc = [], {}

    def nxt(pid):
        v = cc.get(pid, 0)
        cc[pid] = (v + 1) & 15
        return v
    for i, a in enumerate(aus):
        if i in reset_cc:
            cc = {}
        if (i >= psi_from and (i - psi_from) % psi_every == 0) or i in force_psi:
            ver = pmt_version(i)
            out.append({"b": _psi_packet(0, nxt(0), pat_section(ver & 31)), "au": None, "pos": 0, "n": 1, "pid": 0})
            out.append({"b": _psi_packet(PMT_PID, nxt(PMT_PID), pmt_section(ver & 31, vpid(i), extra_es(i))), "au": None,
                        "pos": 0, "n": 1, "pid": PMT_PID})
        if audio_every and i % audio_every == 1:
            pes = bytes([0, 0, 1, 0xC0, 0, 20, 0x80, 0x00, 0x00]) + bytes((i * 7 + k) & 0xFF for k in range(11))
            body = pes + b"\xff" * (184 - len(pes))
            out.append({"b": bytes([0x47, 0x40 | (AUD_PID >> 8), AUD_PID & 0xFF, 0x10 | nxt(AUD_PID)]) + body, "au": None,
                        "pos": 0, "n": 1, "pid": AUD_PID})
        pts, dts = a["pts"] % MOD, (a["dts"] % MOD if a["dts"] is not None else None)
        hdr = (bytes([0x80, 0x80, 5]) + _ts_bytes(2, pts)) if dts is None else (bytes([0x80, 0xC0, 10]) + _ts_bytes(3, pts) + _ts_bytes(1, dts))
        pid = vpid(i)
        pcr = ((dts if dts is not None else pts) - 63000) % MOD
        pk, c = _packetise(pid, cc.get(pid, 0), bytes([0, 0, 1, 0xE0, 0, 0]) + hdr + a["payload"], pcr)
        cc[pid] = c & 15
        for k, b in enumerate(pk):
            out.append({"b": b, "au": i, "pos": k, "n": len(pk), "pid": pid})
    return out
