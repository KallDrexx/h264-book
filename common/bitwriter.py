"""Minimal H.264 bit writer for the synthetic test streams of Phase 4 (independent of cavlc_ref.py).

W collects bits (u(n), ue(v), se(v)) and turns them into an RBSP with rbsp_trailing_bits; nal() adds
emulation prevention and an Annex B start code.  Used by Chapter 4.1's mbgen.py and 4.2's intragen.py.
"""


class W:
    def __init__(self):
        self.bits = []

    def raw(self, n, v):
        assert 0 <= v < (1 << n) or n == 0, (n, v)
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, v):
        assert v >= 0
        v += 1
        n = v.bit_length()
        self.raw(n - 1, 0)
        self.raw(n, v)

    def se(self, v):
        self.ue(2 * v - 1 if v > 0 else -2 * v)

    def pos(self):
        """Bit position counted from the NAL header's first bit (header byte = 8 bits)."""
        return 8 + len(self.bits)

    def rbsp(self):
        b = self.bits + [1]
        while len(b) % 8:
            b.append(0)
        return bytes(int("".join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))


def escape(rbsp):
    """Insert emulation_prevention_three_bytes (00 00 0x with x <= 3 -> 00 00 03 0x)."""
    out, zeros = bytearray(), 0
    for x in rbsp:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


def nal(header, rbsp):
    return b"\x00\x00\x00\x01" + bytes([header]) + escape(rbsp)
