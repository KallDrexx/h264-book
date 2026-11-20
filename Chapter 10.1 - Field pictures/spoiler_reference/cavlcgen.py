"""CAVLC macroblock writer for the Chapter 10.1 field-picture streams (I slices only): the same random macroblocks as cabacgen.py
(Intra4x4, Intra16x16, I_PCM; no 8x8 transform), coded with Exp-Golomb syntax and CAVLC residual blocks (9.2).
It is an ENCODER written from the standard's description (coeff_token by nC, trailing ones, level prefix/suffix with the
suffixLength adaptation, total_zeros, run_before) and uses only the code tables of common/cavlc_tables.py.  ffmpeg decoding its
output is the cross-check."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import cabacgen  # noqa: E402
from common import bitwriter, cavlc_tables as CT  # noqa: E402
from common.cavlc_ref import CBP_INTRA  # noqa: E402


def level_bits(level, first_after_t1, suffix_len):
    """-> (bit string, new suffix_len) for one non-trailing-one level."""
    code = 2 * level - 2 if level > 0 else -2 * level - 1
    if first_after_t1:
        code -= 2
    sl = suffix_len
    if sl == 0:
        if code < 14:
            prefix, suf, n = code, 0, 0
        elif code < 30:
            prefix, suf, n = 14, code - 14, 4
        else:
            prefix, suf, n = 15, code - 30, 12
    else:
        if code < (15 << sl):
            prefix, suf, n = code >> sl, code & ((1 << sl) - 1), sl
        else:
            prefix, suf, n = 15, code - (15 << sl), 12
    assert suf < (1 << n) or n == 0
    bits = "0" * prefix + "1" + (format(suf, f"0{n}b") if n else "")
    if sl == 0:
        sl = 1
    if abs(level) > (3 << (sl - 1)) and sl < 6:
        sl += 1
    return bits, sl


def block_bits(coeffs, nc, maxnum):
    """CAVLC residual_block_cavlc for `coeffs` (scan order, maxnum entries) with the given nC -> (bit string, TotalCoeff)."""
    nz = [i for i, v in enumerate(coeffs) if v]
    tc = len(nz)
    rev = [coeffs[i] for i in reversed(nz)]                 # highest frequency first
    t1 = 0
    for v in rev[:3]:
        if abs(v) == 1:
            t1 += 1
        else:
            break
    tab = CT.coeff_token_table(nc)
    out = tab[(tc, t1)]
    if tc == 0:
        return out, 0
    for v in rev[:t1]:
        out += "1" if v < 0 else "0"
    sl = 1 if (tc > 10 and t1 < 3) else 0
    for k, v in enumerate(rev[t1:]):
        b, sl = level_bits(v, k == 0 and t1 < 3, sl)
        out += b
    if tc < maxnum:
        zeros = nz[-1] + 1 - tc
        out += (CT.TOTAL_ZEROS_DC if maxnum == 4 else CT.TOTAL_ZEROS)[tc][zeros]
        left = zeros
        pos = list(reversed(nz))
        for k in range(tc - 1):
            if left <= 0:
                break
            run = pos[k] - pos[k + 1] - 1
            out += CT.RUN_BEFORE[min(left, 7)][run]
            left -= run
    return out, tc


class CavlcEnc(cabacgen.SliceEnc):
    """Same interface as cabacgen.SliceEnc (code_mb(addr, m, last)); the slice data bits end up in self.w."""

    def __init__(self, qp, W, first_mb, rng, cap, w, field=False, mutant=None):
        super().__init__("I", qp, 0, W, first_mb, rng, 0, cap, field, mutant)
        self.w = w                      # the bit writer that already holds the slice header (pcm alignment counts its bits)
        self.tc = {}                    # addr -> dict('y': {(x4,y4): n}, 'cb': {(x,y): n}, 'cr': ...)

    def bits(self, s):
        self.w.bits += [int(c) for c in s]

    def nc_of(self, addr, plane, x, y):
        """nC for the block at (x, y) (units of blocks) of the given plane ('y' 4x4 grid, 'cb' / 'cr' 2x2 grid)."""
        n = 4 if plane == "y" else 2
        vals = []
        for dx, dy in ((-1, 0), (0, -1)):
            xx, yy = x + dx, y + dy
            if xx >= 0 and yy >= 0:
                vals.append(self.cur[plane].get((xx, yy), 0))
                continue
            a = None
            if xx < 0 and addr % self.W and addr - 1 >= self.fm:
                a, xx = addr - 1, n - 1
            if yy < 0 and addr - self.W >= self.fm:
                a, yy = addr - self.W, n - 1
            if a is None:
                vals.append(None)
            else:
                r = self.recs[a]
                vals.append(16 if r["k"] == "pcm" else self.tc[a][plane].get((xx, yy), 0))
        a, b = vals
        if a is not None and b is not None:
            return (a + b + 1) >> 1
        return a if a is not None else b if b is not None else 0

    def code_mb(self, addr, m, last):
        w = self.w
        k = m["k"]
        m.setdefault("t8", 0)
        self.cur = {"y": {}, "cb": {}, "cr": {}}
        self.tc[addr] = self.cur
        w.ue(m["mbt"])
        if k == "pcm":
            while len(w.bits) % 8:          # pcm_alignment_zero_bit
                w.bits.append(0)
            self.bits("".join(format(x, "08b") for x in m["samples"]))
            self.cur["y"] = {(x, y): 16 for x in range(4) for y in range(4)}
            m["qp"] = self.qp
            self.recs[addr] = m
            return
        if k in ("i4", "i8"):
            for ch in m["modes"]:
                if ch == "p":
                    w.raw(1, 1)
                else:
                    w.raw(1, 0)
                    w.raw(3, int(ch))
        w.ue(m["cpred"])
        if k != "i16":
            w.ue(CBP_INTRA.index(m["cbp_l"] + 16 * m["cbp_c"]))
        has_qpd = k == "i16" or m["cbp_l"] or m["cbp_c"]
        if has_qpd:
            if self.cap is not None:
                m["qpd"] = min(30, max(0, self.qp + m["qpd"])) - self.qp
            w.se(m["qpd"])
            self.qp = (self.qp + m["qpd"] + 52) % 52
        m["qp"] = self.qp
        self.recs[addr] = m
        m["blocks"] = []
        if not has_qpd:
            return
        rng = self.rng

        def put(kind, idx, c, nc, nmax, plane, pos):
            if c is None:
                c = [0] * nmax
            b, tc = block_bits(c, nc, nmax)
            self.bits(b)
            if plane:
                self.cur[plane][pos] = tc
            m["blocks"].append((kind, idx, list(c) if any(c) else None))
        if k == "i16":
            put("i16dc", 0, self.rand_cavlc(16, False), self.nc_of(addr, "y", 0, 0), 16, None, None)
        for b8 in range(4):
            if not (m["cbp_l"] >> b8) & 1:
                continue
            for b4 in range(4):
                idx = b8 * 4 + b4
                x4, y4 = cabacgen.CELL_ORDER[idx]
                nmax = 15 if k == "i16" else 16
                put("i16ac" if k == "i16" else "luma", idx, self.rand_cavlc(nmax, False), self.nc_of(addr, "y", x4, y4), nmax,
                    "y", (x4, y4))
        if m["cbp_c"]:
            for ch in range(2):
                put(("cbdc", "crdc")[ch], 0, self.rand_cavlc(4, False), -1, 4, None, None)
        if m["cbp_c"] == 2:
            for ch in range(2):
                for b in range(4):
                    x, y = b & 1, b >> 1
                    put(("cbac", "crac")[ch], b, self.rand_cavlc(15, False), self.nc_of(addr, ("cb", "cr")[ch], x, y), 15,
                        ("cb", "cr")[ch], (x, y))

    def rand_cavlc(self, nmax, always):
        c = self.rand_block(self.rng, nmax, always)
        return c
