"""SPOILER: the front half of the reference player: TS -> PES (PTS/DTS) -> NAL units -> access units (decoding order),
with each access unit's POC and the VUI timing_info.  No pixels are decoded (the harness times PTS handling, not decoding).
Python 3 stdlib only."""
import re

START = re.compile(b"\x00\x00\x01")


class Bits:
    def __init__(self, b):
        self.b, self.p = b, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.b[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def unescape(b):
    return re.sub(b"\x00\x00\x03", b"\x00\x00", b)


def parse_sps(rbsp):
    r = Bits(rbsp)
    prof = r.u(8)
    r.u(16)
    sps = {"profile": prof, "sps_id": r.ue()}
    chroma = 1
    if prof in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = r.ue()
        if chroma == 3:
            r.u(1)
        r.ue(); r.ue(); r.u(1)
        if r.u(1):
            for i in range(8 if chroma != 3 else 12):
                if r.u(1):
                    last, nxt = 8, 8
                    for _ in range(16 if i < 6 else 64):
                        if nxt:
                            nxt = (last + r.se()) % 256
                        last = nxt or last
    sps["log2_frame_num"] = r.ue() + 4
    sps["poc_type"] = r.ue()
    if sps["poc_type"] == 0:
        sps["log2_poc_lsb"] = r.ue() + 4
    elif sps["poc_type"] == 1:
        raise ValueError("pic_order_cnt_type 1 is not supported by the reference player")
    r.ue(); r.u(1); r.ue(); r.ue()
    sps["frame_mbs_only"] = r.u(1)
    if not sps["frame_mbs_only"]:
        r.u(1)
    r.u(1)
    if r.u(1):
        for _ in range(4):
            r.ue()
    sps["timing"] = None
    if r.u(1):                                         # vui_parameters_present_flag
        if r.u(1) and r.u(8) == 255:                   # aspect_ratio_info
            r.u(32)
        if r.u(1):
            r.u(1)
        if r.u(1):
            r.u(4)
            if r.u(1):
                r.u(24)
        if r.u(1):
            r.ue(); r.ue()
        if r.u(1):
            nu, ts, fixed = r.u(32), r.u(32), r.u(1)
            sps["timing"] = (nu, ts, fixed)
    return sps


def read_pes(data):
    """All video PES of the first video PID: list of dict(pts, dts, payload)."""
    if len(data) % 188 or data[:1] != b"\x47":
        raise ValueError("input is not a 188-byte MPEG-TS")
    vpid, cur, out = None, None, []
    for off in range(0, len(data), 188):
        p = data[off:off + 188]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = p[1] & 0x40
        afc = (p[3] >> 4) & 3
        body = p[(5 + p[4]) if afc & 2 else 4:] if afc & 1 else b""
        if vpid is None and pusi and body[:4] == b"\x00\x00\x01\xe0":
            vpid = pid
        if pid != vpid:
            continue
        if pusi:
            cur = bytearray()
            out.append(cur)
        if cur is not None:
            cur += body
    res = []
    for b in out:
        fl = b[7] >> 6
        ts = lambda o: (((b[o] >> 1) & 7) << 30) | (b[o + 1] << 22) | ((b[o + 2] >> 1) << 15) | (b[o + 3] << 7) | (b[o + 4] >> 1)
        res.append({"pts": ts(9) if fl & 2 else None, "dts": ts(14) if fl == 3 else None, "payload": bytes(b[9 + b[8]:])})
    return res


def access_units(path):
    """-> (aus, timing) ; aus in decoding order: dict(pts, dts, idr, ref, poc_lsb, frame_num), timing = (num_units, time_scale, fixed) or None.
    The PTS of a PES belongs to the FIRST access unit that starts inside that PES; later ones in the same PES get None."""
    pes = read_pes(open(path, "rb").read())
    es = b"".join(p["payload"] for p in pes)
    bounds, pos = [], 0                                # PES index -> start offset in es
    for p in pes:
        bounds.append(pos)
        pos += len(p["payload"])
    starts = [m.start() for m in START.finditer(es)]
    sps, aus, cur, claimed = None, [], None, set()
    pes_of = lambda off: max(i for i, b in enumerate(bounds) if b <= off) if off < len(es) else len(pes) - 1
    import bisect
    for n, s in enumerate(starts):
        if s and es[s - 1] == 0:
            s -= 1                                     # 4-byte start code: the AU starts at the leading zero
        end = starts[n + 1] if n + 1 < len(starts) else len(es)
        nal = es[starts[n] + 3:end]
        if not nal:
            continue
        t = nal[0] & 31
        rbsp = unescape(nal[1:])
        if t == 7:
            sps = parse_sps(rbsp)
        new_au = False
        if t in (9, 7, 8, 6):
            new_au = cur is None or cur["vcl"]
        elif t in (1, 5):
            first_mb = Bits(rbsp).ue()
            new_au = cur is None or (cur["vcl"] and first_mb == 0)
        if new_au:
            i = bisect.bisect_right(bounds, s) - 1
            pts = dts = None
            if i not in claimed:
                claimed.add(i)
                pts, dts = pes[i]["pts"], pes[i]["dts"]
            cur = {"pts": pts, "dts": dts, "vcl": False, "idr": False, "ref": False, "poc_lsb": 0, "frame_num": 0}
            aus.append(cur)
        if t in (1, 5) and not cur["vcl"]:
            if sps is None:
                raise ValueError("slice before any SPS")
            r = Bits(rbsp)
            r.ue(); r.ue(); r.ue()
            cur["frame_num"] = r.u(sps["log2_frame_num"])
            cur["idr"] = t == 5
            cur["ref"] = (nal[0] >> 5) != 0
            if not sps["frame_mbs_only"]:
                raise ValueError("field/MBAFF streams are not supported by the reference player")
            if t == 5:
                r.ue()
            if sps["poc_type"] == 0:
                cur["poc_lsb"] = r.u(sps["log2_poc_lsb"])
            cur["max_lsb"] = 1 << sps.get("log2_poc_lsb", 0)
            cur["vcl"] = True
        elif t in (1, 5):
            cur["vcl"] = True
    aus = [a for a in aus if a["vcl"]]
    # picture order count (8.2.1.1, type 0, frame pictures) and output order: ascending POC inside each IDR period
    prev_msb = prev_lsb = 0
    for a in aus:
        mx = a["max_lsb"]
        if a["idr"]:
            prev_msb = prev_lsb = 0
        lsb = a["poc_lsb"]
        if lsb < prev_lsb and prev_lsb - lsb >= mx // 2:
            msb = prev_msb + mx
        elif lsb > prev_lsb and lsb - prev_lsb > mx // 2:
            msb = prev_msb - mx
        else:
            msb = prev_msb
        a["poc"] = msb + lsb
        if a["ref"]:
            prev_msb, prev_lsb = msb, lsb
    return aus, (sps or {}).get("timing")


def output_order(aus):
    """Indices of aus in output order: IDR periods in order, ascending POC inside each (conforming streams without MMCO 5)."""
    order, period = [], []
    for i, a in enumerate(aus):
        if a["idr"] and period:
            order += [j for _, j in sorted((aus[j]["poc"], j) for j in period)]
            period = []
        period.append(i)
    order += [j for _, j in sorted((aus[j]["poc"], j) for j in period)]
    return order
