"""[Chapter 10.1: copy of Chapter 8.5's t8gen.py, trimmed to what the field-picture generator uses, with a `field` switch in the
residual coder (field-coded significant_coeff_flag / last_significant_coeff_flag contexts, 9.3.3.1.3) and block recording.]

Chapter 8.5 synthetic stream generator: valid HIGH-profile CABAC streams (PPS transform_8x8_mode_flag = 1) with many macroblocks,
built with the independent arithmetic encoder (cabac_enc.py), so the expected dump (transform_size_8x8_flag, prediction modes AND the
derived Intra4x4 / Intra8x8 modes, 8x8 coefficients) is known by construction and never comes from a decoder.

What it covers that x264 streams cover only by luck: all nine Intra 8x8 modes on every availability pattern (macroblock at the
picture / slice border, every 8x8 block's left / above / above-left / above-right case), Intra 8x8 neighbours of Intra 4x4 macroblocks
and the other way round (mode prediction reads Intra4x4PredMode[blk8 * 4 + 1] / [blk8 * 4 + 2] and Intra8x8PredMode[blk4 >> 2]),
transform_size_8x8_flag next to every kind of neighbour (skipped, I_PCM, Intra16x16, inter, other slice), the inter flag rules
(cbp luma zero / non-zero; B_Direct_16x16 with direct_8x8_inference_flag 0 and 1), ctxBlockCat 5 blocks that are dense, sparse,
end on the last position or carry huge levels, coded_block_flag contexts of 4x4 blocks next to 8x8-coded neighbours, and random
slice-level QP, chroma QP offsets and deblocking parameters.
`small=True` keeps the levels so small that no dequantised coefficient leaves 16 bits (ffmpeg then decodes exactly what the
specification says, so the pixels can be compared); `small=False` uses huge levels (dump check only).
The context selection is written independently of parse8.py; ffmpeg decoding the result and parse8.py parsing it are the cross-checks.
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import cabac_enc  # noqa: E402
import cabac_ref as R  # noqa: E402
from common import bitwriter  # noqa: E402

# Table 9-43 (frame coded), significant_coeff_flag / last_significant_coeff_flag ctxIdxInc of ctxBlockCat 5 by scan position
SIG8F = [0, 1, 1, 2, 2, 3, 3, 4, 5, 6, 7, 7, 7, 8, 4, 5, 6, 9, 10, 10, 8, 11, 12, 11, 9, 9, 10, 10, 8, 11, 12, 11,
         9, 9, 10, 10, 8, 11, 12, 11, 9, 9, 10, 10, 8, 13, 13, 9, 9, 10, 10, 8, 13, 13, 9, 9, 10, 10, 14, 14, 14, 14, 14]
SIG8 = [0, 1, 2, 3, 4, 5, 5, 4, 4, 3, 3, 4, 4, 4, 5, 5, 4, 4, 4, 4, 3, 3, 6, 7, 7, 7, 8, 9, 10, 9, 8, 7,
        7, 6, 11, 12, 13, 11, 6, 7, 8, 9, 14, 10, 9, 8, 6, 11, 12, 13, 11, 6, 9, 14, 10, 9, 11, 12, 13, 11, 14, 10, 12]
LAST8 = [0] + [1] * 15 + [2] * 16 + [3] * 8 + [4] * 8 + [5] * 4 + [6] * 4 + [7] * 4 + [8] * 3
assert len(SIG8) == len(LAST8) == 63

_PAIRS = [("L0", "L0"), ("L1", "L1"), ("L0", "L1"), ("L1", "L0"), ("L0", "Bi"), ("L1", "Bi"), ("Bi", "L0"), ("Bi", "L1"),
          ("Bi", "Bi")]


def b_parts(mbt):
    """B mb_type 1..21 -> list of prediction modes per partition (Table 7-14)."""
    if mbt <= 3:
        return [("L0", "L1", "Bi")[mbt - 1]]
    return list(_PAIRS[(mbt - 4) // 2])


class SliceEnc:
    def __init__(self, stype, qp, idc, W, first_mb, rng, inference=1, cap=None, field=False, mutant=None):
        self.field = field
        self.t8mode = True
        self.mutant = mutant
        self.inference = inference
        self.cap = cap                  # largest level magnitude (small=True streams) or None
        self.t = stype
        self.W = W
        self.fm = first_mb
        self.e = cabac_enc.Encoder()
        self.st = R.init_contexts(stype, idc, qp)
        self.recs = {}
        self.qp = qp
        self.prev_nz = 0
        self.rng = rng
        self.lines = []

    # --- helpers
    def nb(self, addr):
        a = self.recs.get(addr - 1) if addr % self.W and addr - 1 >= self.fm else None
        b = self.recs.get(addr - self.W) if addr - self.W >= self.fm else None
        return a, b

    def bin(self, ctx, v):
        self.e.decision(self.st[ctx], v)

    # --- mb_type pieces
    def i_bins(self, ival, base, islice):
        """Bins b1.. of an I mb_type (b0 = 1 already written)."""
        e = self.e
        if ival == 25:
            e.terminate(1)
            return
        e.terminate(0)
        v = ival - 1
        mode, chroma, luma = v % 4, (v // 4) % 3, v // 12
        if islice:
            self.bin(base + 3, luma)
            self.bin(base + 4, 1 if chroma else 0)
            if chroma:
                self.bin(base + 5, chroma - 1)
            self.bin(base + 6, mode >> 1)
            self.bin(base + 7, mode & 1)
        else:
            self.bin(base + 1, luma)
            self.bin(base + 2, 1 if chroma else 0)
            if chroma:
                self.bin(base + 2, chroma - 1)
            self.bin(base + 3, mode >> 1)
            self.bin(base + 3, mode & 1)

    def code_mb_type(self, m, A, B):
        """m['mbt'] is the spec value of the slice's own table (I: 0..25, P: 0..2 inter / 5.. intra, B: 0..21 / 23.. intra)."""
        t, mbt = self.t, m["mbt"]
        if t == "I":
            cond = lambda n: 0 if (n is None or n["k"] in ("i4", "i8")) else 1
            self.bin(3 + cond(A) + cond(B), 0 if mbt == 0 else 1)
            if mbt:
                self.i_bins(mbt, 3, True)
        elif t == "P":
            if mbt >= 5:
                self.bin(14, 1)
                self.bin(17, 0 if mbt == 5 else 1)
                if mbt > 5:
                    self.i_bins(mbt - 5, 17, False)
            else:
                self.bin(14, 0)
                b1, b2 = {0: (0, 0), 1: (1, 1), 2: (1, 0)}[mbt]
                self.bin(15, b1)
                self.bin(16 if b1 == 0 else 17, b2)
        else:
            cond = lambda n: 0 if (n is None or n["k"] in ("skip", "direct")) else 1
            self.bin(27 + cond(A) + cond(B), 0 if mbt == 0 else 1)
            if mbt == 0:
                return
            if mbt in (1, 2):
                self.bin(27 + 3, 0)
                self.bin(27 + 5, mbt - 1)
                return
            self.bin(27 + 3, 1)
            if mbt >= 23:
                for c, b in ((4, 1), (5, 1), (5, 0), (5, 1)):
                    self.bin(27 + c, b)
                iv = mbt - 23
                self.bin(32, 0 if iv == 0 else 1)
                if iv:
                    self.i_bins(iv, 32, False)
                return
            if mbt <= 10:
                bits = [(mbt - 3) >> 3 & 1, (mbt - 3) >> 2 & 1, (mbt - 3) >> 1 & 1, (mbt - 3) & 1]
                tail = []
            elif mbt == 11:
                bits, tail = [1, 1, 1, 0], []
            elif mbt == 22:
                bits, tail = [1, 1, 1, 1], []
            else:
                b5 = mbt + 4
                bits, tail = [(b5 >> 4) & 1, (b5 >> 3) & 1, (b5 >> 2) & 1, (b5 >> 1) & 1], [b5 & 1]
            self.bin(27 + 4, bits[0])
            for b in bits[1:] + tail:
                self.bin(27 + 5, b)

    # --- other elements
    def t8_flag(self, m, A, B):
        cond = lambda n: 0 if (n is None or not n.get("t8")) else 1       # noqa: E731
        self.bin(399 + cond(A) + cond(B), 1 if m.get("t8") else 0)

    def code_intra_syntax(self, m, A, B):
        if m["k"] in ("i4", "i8"):
            if self.t8mode:                                   # transform_size_8x8_flag exists only when the PPS enables the 8x8 transform
                self.t8_flag(m, A, B)
            for ch in m["modes"]:
                if ch == "p":
                    self.bin(68, 1)
                else:
                    self.bin(68, 0)
                    v = int(ch)
                    for i in range(3):
                        self.bin(69, (v >> i) & 1)
        cond = lambda n: 0 if (n is None or n["k"] in ("skip", "pcm", "direct", "inter") or n["cpred"] == 0) else 1
        c = m["cpred"]
        self.bin(64 + cond(A) + cond(B), 0 if c == 0 else 1)
        if c:
            self.bin(67, 0 if c == 1 else 1)
            if c > 1:
                self.bin(67, 0 if c == 2 else 1)

    def code_cbp(self, m, A, B):
        luma, chroma = m["cbp_l"], m["cbp_c"]

        def lcond(n, bit):           # condTermFlag of an 8x8 block of another macroblock
            if n is None or n["k"] == "pcm":
                return 0
            return 0 if (n["cbp_l"] >> bit) & 1 else 1
        for b8 in range(4):
            x8, y8 = b8 & 1, b8 >> 1
            ca = (0 if (luma >> (b8 - 1)) & 1 else 1) if x8 else lcond(A, b8 + 1)
            cb = (0 if (luma >> (b8 - 2)) & 1 else 1) if y8 else lcond(B, b8 + 2)
            self.bin(73 + ca + 2 * cb, (luma >> b8) & 1)

        def ccond(n, two):
            if n is None:
                return 0
            if n["k"] == "pcm":
                return 1
            return 1 if (n["cbp_c"] == 2 if two else n["cbp_c"] > 0) else 0
        self.bin(77 + ccond(A, False) + 2 * ccond(B, False), 1 if chroma else 0)
        if chroma:
            self.bin(77 + 4 + ccond(A, True) + 2 * ccond(B, True), chroma - 1)

    def code_qpd(self, qpd):
        k = 2 * qpd - 1 if qpd > 0 else -2 * qpd
        for i in range(k):
            self.bin(60 + (self.prev_nz if i == 0 else (2 if i == 1 else 3)), 1)
        self.bin(60 + (self.prev_nz if k == 0 else (2 if k == 1 else 3)), 0)
        self.qp = (self.qp + qpd + 52) % 52

    # --- residual
    CBF = [0, 4, 8, 12, 16]
    SIG = [0, 15, 29, 44, 47]
    ABS = [0, 10, 20, 30, 39]

    def level_cap(self, mag):
        return mag if self.cap is None else (mag if mag <= self.cap else 1 + mag % self.cap)

    def nbf(self, n, intra, kind, c=0, x=0, y=0):
        """coded_block_flag of the neighbouring block in macroblock n (None = outside the slice), as condTermFlagN."""
        if n is None:
            return 1 if intra else 0
        k = n["k"]
        if k == "pcm":
            return 1
        if k == "skip":
            return 0
        if kind == "dc":
            return n["dcf"] if k == "i16" else 0
        if kind == "luma":
            return n["lcbf"].get((x, y), 0) if (n["cbp_l"] >> ((y // 2) * 2 + x // 2)) & 1 else 0
        if kind == "cdc":
            return n["cdcf"][c] if n["cbp_c"] else 0
        return n["cacf"][c].get((x, y), 0) if n["cbp_c"] == 2 else 0

    def eg0(self, v):
        k = 0
        while v >= (1 << k):
            self.e.bypass(1)
            v -= 1 << k
            k += 1
        self.e.bypass(0)
        while k:
            k -= 1
            self.e.bypass((v >> k) & 1)

    def code_block(self, cat, inc, coeffs, nmax):
        fld = self.field and self.mutant != "frame_ctx_in_field"       # ctxIdxOffsets of Table 9-34, field-coded columns
        if cat == 5:                      # 8x8 luma: no coded_block_flag (4:2:0), own context ranges
            sigb, lastb, absb = (436, 451, 426) if fld else (402, 417, 426)
        else:
            self.bin(85 + self.CBF[cat] + inc, 0 if coeffs is None else 1)
            if coeffs is None:
                return
            sigb = (277 if fld else 105) + self.SIG[cat]
            lastb = (338 if fld else 166) + self.SIG[cat]
            absb = 227 + self.ABS[cat]
        last_nz = max(i for i, v in enumerate(coeffs) if v)
        for i in range(nmax - 1):
            ci = min(i, 2) if cat == 3 else i
            if cat == 5:
                si, li = (SIG8F if (self.field and self.mutant != 'frame_sig8_in_field') else SIG8)[i], LAST8[i]
            else:
                si = li = ci
            sig = 1 if coeffs[i] else 0
            self.bin(sigb + si, sig)
            if sig:
                self.bin(lastb + li, 1 if i == last_nz else 0)
                if i == last_nz:
                    break
        eq1 = gt1 = 0
        for v in [v for v in reversed(coeffs) if v]:
            a = abs(v) - 1
            self.bin(absb + (0 if gt1 else min(4, 1 + eq1)), 1 if a > 0 else 0)
            if a == 0:
                eq1 += 1
            else:
                cn = 5 + min(4 - (1 if cat == 3 else 0), gt1)
                for _ in range(min(a, 14) - 1):
                    self.bin(absb + cn, 1)
                if a < 14:
                    self.bin(absb + cn, 0)
                else:
                    self.eg0(a - 14)
                gt1 += 1
            self.e.bypass(1 if v < 0 else 0)

    def rand_block(self, rng, nmax, always=False):
        """Random content of one residual block: None (coded_block_flag 0) or a list of nmax levels (at least one non-zero)."""
        if rng.random() < 0.35 and not always:
            return None
        style = rng.choice(["sparse", "sparse", "dense", "last", "single"])
        c = [0] * nmax
        if style == "sparse":
            for _ in range(rng.randint(1, 4)):
                c[rng.randrange(nmax)] = 1
        elif style == "dense":
            for i in range(nmax):
                if rng.random() < 0.6:
                    c[i] = 1
        elif style == "last":
            c[nmax - 1] = 1
            for _ in range(rng.randint(0, 3)):
                c[rng.randrange(nmax)] = 1
        else:
            c[rng.choice([0, nmax - 1, rng.randrange(nmax)])] = 1
        for i in range(nmax):
            if c[i]:
                r = rng.random()
                mag = (1 if r < 0.55 else rng.randint(2, 4) if r < 0.80 else rng.randint(5, 30) if r < 0.93
                       else rng.choice([13, 14, 15, 16, 17]) if r < 0.97 else rng.randint(31, 3000))
                mag = self.level_cap(mag)
                c[i] = mag if rng.random() < 0.5 else -mag
        if not any(c):
            c[0] = 1
        return c

    def code_residual(self, m, A, B):
        """Random residual for every block the macroblock owes; fills m['coef'] (expected dump value) and the flag maps."""
        rng = self.rng
        intra = m["k"] in ("i4", "i8", "i16", "pcm")
        cbl, cbc = m["cbp_l"], m["cbp_c"]
        m["dcf"], m["lcbf"], m["cdcf"], m["cacf"] = 0, {}, [0, 0], [{}, {}]
        parts = []

        m["blocks"] = []

        def emit(kind, idx, c, nmax):
            m["blocks"].append((kind, idx, None if c is None else list(c)))
            if c is None:
                parts.append(f"{kind}{idx}:-")
            else:
                off = 1 if nmax == 15 else 0
                parts.append(f"{kind}{idx}:" + ",".join(f"{i + off}={v}" for i, v in enumerate(c) if v))
        if m["k"] == "i16":
            c = self.rand_block(rng, 16)
            self.code_block(0, self.nbf(A, intra, "dc") + 2 * self.nbf(B, intra, "dc"), c, 16)
            m["dcf"] = 0 if c is None else 1
            emit("i16dc", 0, c, 16)
        for b8 in range(4):
            if not (cbl >> b8) & 1:
                continue
            if m.get("t8"):
                c = self.rand_block(rng, 64, always=True)
                self.code_block(5, 0, c, 64)
                for k in range(4):
                    m["lcbf"][((b8 & 1) * 2 + (k & 1), (b8 >> 1) * 2 + (k >> 1))] = 1
                emit("luma8", b8, c, 64)
                continue
            for b4 in range(4):
                idx = b8 * 4 + b4
                x4 = (idx % 2) + 2 * ((idx // 4) % 2)
                y4 = ((idx // 2) % 2) + 2 * (idx // 8)
                fa = m["lcbf"].get((x4 - 1, y4), 0) if x4 else self.nbf(A, intra, "luma", x=3, y=y4)
                fb = m["lcbf"].get((x4, y4 - 1), 0) if y4 else self.nbf(B, intra, "luma", x=x4, y=3)
                nmax = 15 if m["k"] == "i16" else 16
                c = self.rand_block(rng, nmax)
                self.code_block(1 if m["k"] == "i16" else 2, fa + 2 * fb, c, nmax)
                m["lcbf"][(x4, y4)] = 0 if c is None else 1
                emit("i16ac" if m["k"] == "i16" else "luma", idx, c, nmax)
        if cbc:
            for ch in range(2):
                c = self.rand_block(rng, 4)
                self.code_block(3, self.nbf(A, intra, "cdc", ch) + 2 * self.nbf(B, intra, "cdc", ch), c, 4)
                m["cdcf"][ch] = 0 if c is None else 1
                emit(("cbdc", "crdc")[ch], 0, c, 4)
        if cbc == 2:
            for ch in range(2):
                for b in range(4):
                    x, y = b & 1, b >> 1
                    fa = m["cacf"][ch].get((x - 1, y), 0) if x else self.nbf(A, intra, "cac", ch, 1, y)
                    fb = m["cacf"][ch].get((x, y - 1), 0) if y else self.nbf(B, intra, "cac", ch, x, 1)
                    c = self.rand_block(rng, 15)
                    self.code_block(4, fa + 2 * fb, c, 15)
                    m["cacf"][ch][(x, y)] = 0 if c is None else 1
                    emit(("cbac", "crac")[ch], b, c, 15)
        m["coef"] = ";".join(parts) or "-"

    def code_zero_mvds(self, m):
        n = 1 if self.t == "P" else None
        if self.t == "P":
            modes = {0: ["L0"], 1: ["L0", "L0"], 2: ["L0", "L0"]}[m["mbt"]]
        else:
            modes = b_parts(m["mbt"])
        for lst, names in ((0, ("L0", "Bi")), (1, ("L1", "Bi"))):
            for mode in modes:
                if mode in names:
                    self.bin(40, 0)
                    self.bin(47, 0)

    # --- one macroblock
    def code_mb(self, addr, m, last):
        A, B = self.nb(addr)
        k = m["k"]
        m.setdefault("t8", 0)
        if self.t != "I":
            base = 11 if self.t == "P" else 24
            cond = lambda n: 0 if (n is None or n["k"] == "skip") else 1
            self.bin(base + cond(A) + cond(B), 1 if k == "skip" else 0)
        pcm_like = False
        if k == "skip":
            self.recs[addr] = m
            m["qp"] = self.qp
            m["t8"] = 0
            self.prev_nz = 0
        else:
            self.code_mb_type(m, A, B)
            if k == "pcm":
                self.e.align_zero()
                self.e.raw_bytes(m["samples"])
                self.e.restart()
                m["qp"] = self.qp
                m["t8"] = 0
                self.prev_nz = 0
            else:
                if k in ("i4", "i8", "i16"):
                    self.code_intra_syntax(m, A, B)
                elif k == "inter":
                    self.code_zero_mvds(m)
                if k != "i16":
                    self.code_cbp(m, A, B)
                if k in ("inter", "direct") and m["cbp_l"] and (k == "inter" or self.inference):
                    self.t8_flag(m, A, B)
                elif k in ("inter", "direct"):
                    m["t8"] = 0
                has_qpd = k == "i16" or m["cbp_l"] or m["cbp_c"]
                if has_qpd:
                    if self.cap is not None:                                # keep QP_Y low: no 16-bit overflow in the transforms
                        m["qpd"] = min(30, max(0, self.qp + m["qpd"])) - self.qp
                    self.code_qpd(m["qpd"])
                    self.prev_nz = 1 if m["qpd"] else 0
                else:
                    self.prev_nz = 0
                m["qp"] = self.qp
                if has_qpd:
                    self.code_residual(m, A, B)
            self.recs[addr] = m
        self.e.terminate(1 if last else 0)
        m["eos"] = 1 if last else 0


def line_fields(m):
    """Expected dump fields (Readme section 5), assembled from the construction."""
    k = m["k"]
    nxn = k in ("i4", "i8")
    f = [f"mbt={'skip' if k == 'skip' else m['mbt']}",
         f"t8={m['t8'] if (nxn or k in ('inter', 'direct')) else '-'}",
         f"modes={m['modes']}" if nxn else "modes=-",
         "imode=" + (",".join(str(m["amodes"][(x4, y4)]) for x4, y4 in (CELL_ORDER[::4] if m["t8"] else CELL_ORDER)) if nxn else "-"),
         f"cbp={m['cbp_l'] + 16 * m['cbp_c']}" if k not in ("skip", "pcm") else "cbp=-",
         f"qp={m['qp']}", f"coef={m.get('coef', '-')}"]
    return " ".join(f)


CELL_ORDER = [((i % 2) + 2 * ((i // 4) % 2), ((i // 2) % 2) + 2 * (i // 8)) for i in range(16)]     # luma4x4BlkIdx -> (x4, y4)


# ---------------------------------------------------------------- random macroblocks
def rand_cbp(rng):
    luma = rng.randrange(16) if rng.random() < 0.7 else 0
    chroma = rng.choice([0, 0, 1, 2, 2]) if rng.random() < 0.7 else 0
    return luma, chroma


def rand_qpd(rng):
    r = rng.random()
    if r < 0.4:
        return 0
    if r < 0.75:
        return rng.randint(-3, 3)
    if r < 0.9:
        return rng.randint(-26, 25)
    return rng.choice([-26, 25, -25, 24])


def rand_mb(rng, stype):
    kinds = {"I": [("i4", 3), ("i8", 5), ("i16", 3), ("pcm", 1)],
             "P": [("skip", 4), ("i4", 1), ("i8", 2), ("i16", 2), ("pcm", 1), ("inter", 6)],
             "B": [("skip", 4), ("direct", 3), ("i4", 1), ("i8", 2), ("i16", 2), ("pcm", 1), ("inter", 6)]}[stype]
    k = rng.choices([a for a, _ in kinds], [b for _, b in kinds])[0]
    m = dict(k=k, cpred=0, modes=None, cbp_l=0, cbp_c=0, qpd=0, mbt=None, t8=0)
    off = {"I": 0, "P": 5, "B": 23}[stype]
    if k in ("i4", "i8"):
        m["mbt"] = off
        m["t8"] = 1 if k == "i8" else 0
        m["cpred"] = rng.randrange(4)
        m["cbp_l"], m["cbp_c"] = rand_cbp(rng)
        m["qpd"] = rand_qpd(rng)
    elif k == "i16":
        mode, chroma, luma = rng.randrange(4), rng.randrange(3), rng.randrange(2)
        m["mbt"] = off + 1 + mode + 4 * chroma + 12 * luma
        m["cpred"] = rng.randrange(4)
        m["cbp_l"], m["cbp_c"] = 15 * luma, chroma
        m["qpd"] = rand_qpd(rng)
    elif k == "pcm":
        m["mbt"] = off + 25
        m["samples"] = bytes(rng.randrange(256) for _ in range(384))
    elif k == "direct":
        m["mbt"] = 0
        m["cbp_l"], m["cbp_c"] = rand_cbp(rng)
        m["qpd"] = rand_qpd(rng)
    elif k == "inter":
        m["mbt"] = rng.randrange(3) if stype == "P" else rng.randrange(1, 22)
        m["cbp_l"], m["cbp_c"] = rand_cbp(rng)
        m["qpd"] = rand_qpd(rng)
    if not (k == "i16" or m["cbp_l"] or m["cbp_c"]):
        m["qpd"] = 0
    if k in ("inter", "direct") and m["cbp_l"]:
        m["t8"] = rng.randrange(2)         # the generator coded it only if the flag is present; code_mb resets it otherwise
    return m


def choose_intra(se, addr, m, off):
    """Pick intra prediction modes that are LEGAL where the macroblock sits (ffmpeg refuses modes that need unavailable
    neighbours), then express them as prev_intra{4x4,8x8}_pred_mode_flag / rem_intra{4x4,8x8}_pred_mode against the predicted
    mode (8.3.1.1 / 8.3.2.1), so the syntax elements in the dump are what an encoder would really send.  m['amodes']: the
    actual mode of every 4x4 cell (x4, y4) (an 8x8 block's mode fills its four cells)."""
    W = se.W
    A, B = se.nb(addr)
    D = se.recs.get(addr - W - 1) if addr % W and addr - W - 1 >= se.fm else None
    L, T, TL = A is not None, B is not None, D is not None
    rng = se.rng
    cl = [0] + ([1] if L else []) + ([2] if T else []) + ([3] if L and T and TL else [])
    m["cpred"] = rng.choice(cl)
    if m["k"] == "i16":
        legal = [2] + ([0] if T else []) + ([1] if L else []) + ([3] if L and T and TL else [])
        mode, chroma, luma = rng.choice(legal), rng.randrange(3), rng.randrange(2)
        m["mbt"] = off + 1 + mode + 4 * chroma + 12 * luma
        m["cbp_l"], m["cbp_c"] = 15 * luma, chroma
        return

    def nbmode(n, xx, yy):                 # mode of cell (xx, yy) of an available neighbouring macroblock n; 2 if it is not I_NxN
        return n["amodes"][(xx, yy)] if n["k"] in ("i4", "i8") else 2
    am, out = {}, ""
    if m["k"] == "i8":
        for blk in range(4):
            bx, by = blk & 1, blk >> 1
            top, left = by > 0 or T, bx > 0 or L
            tl = (bx > 0 and by > 0) or (bx == 0 and by > 0 and L) or (by == 0 and bx > 0 and T) or (bx == 0 and by == 0 and TL)
            legal = [2] + ([0, 3, 7] if top else []) + ([1, 8] if left else []) + ([4, 5, 6] if top and left and tl else [])
            act = rng.choice(legal)
            ma = am[(2 * bx - 1, 2 * by)] if bx else (None if A is None else nbmode(A, 3, 2 * by))
            mb = am[(2 * bx, 2 * by - 1)] if by else (None if B is None else nbmode(B, 2 * bx, 3))
            pred = 2 if (ma is None or mb is None) else min(ma, mb)
            for dx in (0, 1):
                for dy in (0, 1):
                    am[(2 * bx + dx, 2 * by + dy)] = act
            out += "p" if act == pred else str(act if act < pred else act - 1)
        m["amodes"], m["modes"] = am, out
        return
    for idx in range(16):
        x4, y4 = CELL_ORDER[idx]
        top = y4 > 0 or T
        left = x4 > 0 or L
        tl = (x4 > 0 and y4 > 0) or (x4 == 0 and y4 > 0 and L) or (y4 == 0 and x4 > 0 and T) or (x4 == 0 and y4 == 0 and TL)
        legal = [2] + ([0, 3, 7] if top else []) + ([1, 8] if left else []) + ([4, 5, 6] if top and left and tl else [])
        act = rng.choice(legal)
        ma = am[(x4 - 1, y4)] if x4 else (None if A is None else nbmode(A, 3, y4))
        mb = am[(x4, y4 - 1)] if y4 else (None if B is None else nbmode(B, x4, 3))
        pred = 2 if (ma is None or mb is None) else min(ma, mb)
        am[(x4, y4)] = act
        out += "p" if act == pred else str(act if act < pred else act - 1)
    m["amodes"], m["modes"] = am, out


