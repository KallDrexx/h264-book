"""SPOILER: header-only H.264 helpers for the Chapter 9.5 reference player and stream generator (no pixel decoding).
NAL splitting, SPS / PPS / slice-header / recovery-point-SEI parsing, POC (types 0 and 2) and the SPS id rewriter.
Everything raises ValueError on malformed input (never IndexError): damaged data must not crash a player.
Python 3 standard library only."""
import hashlib
import re

START = re.compile(b"\x00\x00\x01")


class Bits:
    def __init__(self, b):
        self.b, self.p = b, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            i = self.p >> 3
            if i >= len(self.b):
                raise ValueError("ran past the end of the RBSP")
            v = (v << 1) | ((self.b[i] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 32:
                raise ValueError("bad Exp-Golomb code")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def unescape(b):
    return b.replace(b"\x00\x00\x03", b"\x00\x00")


def split_nals(es):
    """-> list of (start_offset_of_nal_header, nal_bytes); NAL ends at the next start code, trailing zero bytes removed."""
    starts = [m.end() for m in START.finditer(es)]
    out = []
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else len(es)
        while e > s and es[e - 1] == 0:
            e -= 1
        if e > s:
            out.append((s, es[s:e]))
    return out


def slice_hash(nals):
    """Identity of an access unit's coded slices (used by the reference to simulate 'the decoder noticed the slice data is bad')."""
    h = hashlib.sha1()
    for n in nals:
        if (n[0] & 31) in (1, 5):
            h.update(n)
    return h.hexdigest()


def parse_sps(nal):
    r = Bits(unescape(nal[1:]))
    prof = r.u(8)
    r.u(16)
    sps = {"profile": prof, "id": r.ue(), "raw": bytes(nal)}
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
    sps["log2_fn"] = r.ue() + 4
    sps["poc_type"] = r.ue()
    if sps["poc_type"] == 0:
        sps["log2_lsb"] = r.ue() + 4
    elif sps["poc_type"] == 1:
        raise ValueError("pic_order_cnt_type 1 is not supported by the reference player")
    elif sps["poc_type"] != 2:
        raise ValueError("bad pic_order_cnt_type")
    sps["nref"] = r.ue()
    r.u(1)
    wm, hm = r.ue() + 1, r.ue() + 1
    fmo = r.u(1)
    if not fmo:
        raise ValueError("interlaced streams are Phase 10")
    r.u(1)
    cl = cr = ct = cb = 0
    if r.u(1):
        cl, cr, ct, cb = r.ue(), r.ue(), r.ue(), r.ue()
    sps["frame_mbs_only"] = fmo
    sps["w"], sps["h"] = wm * 16 - 2 * (cl + cr), hm * 16 - 2 * (ct + cb)
    if sps["w"] <= 0 or sps["h"] <= 0 or wm > 512 or hm > 512:
        raise ValueError("absurd picture size")
    return sps


def parse_pps(nal):
    r = Bits(unescape(nal[1:]))
    return {"id": r.ue(), "sps_id": r.ue(), "raw": bytes(nal)}


def parse_slice(nal, pps_t, sps_t):
    """-> dict(first_mb, type, pps, sps, frame_num, idr, ref, poc_lsb); raises ValueError when the header cannot be trusted."""
    r = Bits(unescape(nal[1:]))
    d = {"first_mb": r.ue(), "type": r.ue() % 5, "pps": r.ue()}
    if d["pps"] not in pps_t or pps_t[d["pps"]]["sps_id"] not in sps_t:
        raise ValueError("slice refers to a parameter set that is not available")
    sps = sps_t[pps_t[d["pps"]]["sps_id"]]
    d["sps"] = sps
    d["idr"] = (nal[0] & 31) == 5
    d["ref"] = (nal[0] >> 5) != 0
    d["frame_num"] = r.u(sps["log2_fn"])
    if d["idr"]:
        r.ue()
    d["poc_lsb"] = r.u(sps["log2_lsb"]) if sps["poc_type"] == 0 else 0
    return d


def recovery_point(nal):
    """-> recovery_frame_cnt of a recovery point SEI (payloadType 6) in this SEI NAL, else None."""
    b = unescape(nal[1:])
    i = 0
    try:
        while i + 2 < len(b):
            t = 0
            while b[i] == 0xFF:
                t += 255; i += 1
            t += b[i]; i += 1
            s = 0
            while b[i] == 0xFF:
                s += 255; i += 1
            s += b[i]; i += 1
            if i + s > len(b):
                return None
            if t == 6:
                return Bits(b[i:i + s]).ue()
            i += s
    except (IndexError, ValueError):
        return None
    return None


class Poc:
    """Picture order count (8.2.1) for frame pictures, types 0 and 2; state is reset on IDR (and set at a recovery start)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.pmsb = self.plsb = 0
        self.fn_off = self.pfn = 0

    def start_at(self, s):
        self.pmsb, self.plsb = 0, s["poc_lsb"]
        self.fn_off, self.pfn = 0, s["frame_num"]

    def compute(self, s):
        sps = s["sps"]
        if sps["poc_type"] == 0:
            mx, lsb = 1 << sps["log2_lsb"], s["poc_lsb"]
            if lsb < self.plsb and self.plsb - lsb >= mx // 2:
                msb = self.pmsb + mx
            elif lsb > self.plsb and lsb - self.plsb > mx // 2:
                msb = self.pmsb - mx
            else:
                msb = self.pmsb
            if s["ref"]:
                self.pmsb, self.plsb = msb, lsb
            return msb + lsb
        mx = 1 << sps["log2_fn"]
        if s["idr"]:
            self.fn_off = 0
        elif self.pfn > s["frame_num"]:
            self.fn_off += mx
        self.pfn = s["frame_num"]
        return 2 * (self.fn_off + s["frame_num"]) - (0 if s["ref"] else 1) if not s["idr"] else 0


# ------------------------------------------------------------------------------------------------ SPS id rewriting
def _bits_of(b):
    return "".join(format(x, "08b") for x in b)


def _ue_bits(v):
    s = format(v + 1, "b")
    return "0" * (len(s) - 1) + s


def _from_bits(s):
    s += "0" * (-len(s) % 8)
    return bytes(int(s[i:i + 8], 2) for i in range(0, len(s), 8))


def _escape(b):
    out, z = bytearray(), 0
    for c in b:
        if z >= 2 and c <= 3:
            out.append(3)
            z = 0
        out.append(c)
        z = z + 1 if c == 0 else 0
    return bytes(out)


def rewrite_sps_id(nal, new_id):
    """SPS NAL with seq_parameter_set_id replaced (ue(v) field after profile/constraints/level)."""
    t = nal[0] & 31
    bits = _bits_of(unescape(nal[1:])).rstrip("0")
    head, rest = bits[:24], bits[24:]
    r = Bits(_from_bits(rest))
    old = r.ue()
    return bytes([nal[0]]) + _escape(_from_bits(head + _ue_bits(new_id) + rest[len(_ue_bits(old)):])) if t == 7 else nal


def rewrite_pps_sps_id(nal, new_sps_id):
    """PPS NAL with seq_parameter_set_id replaced (the second ue(v))."""
    bits = _bits_of(unescape(nal[1:])).rstrip("0")
    r = Bits(_from_bits(bits))
    a = r.ue()
    p1 = r.p
    r.ue()
    p2 = r.p
    return bytes([nal[0]]) + _escape(_from_bits(bits[:p1] + _ue_bits(new_sps_id) + bits[p2:]))
