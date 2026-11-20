"""Chapter 8.4 synthetic stream generator: valid CABAC slices with MANY macroblocks, built with the independent
arithmetic encoder (cabac_enc.py) from macroblocks chosen at random, so the expected dump lines (including sub_mb_type, ref_idx
and mvd of every partition) are known by construction (the manifest) and never come from a decoder.

Inter macroblocks use every P / B mb_type (16x16, 16x8, 8x16, 8x8 with every sub_mb_type of Tables 7-17 / 7-18, direct
sub-macroblocks included), every ref_idx value the slice allows (num_ref_idx_active 1..3 per list, set per slice in the header),
and mvd components from a mix that hits the context thresholds exactly (neighbour sums 2, 3, 32, 33), the escape threshold
(absolute values 8, 9, 10) and big values (up to +-2500, so the UEG3 suffix has many bits).  Neighbours of every kind occur
next to each other (skipped, direct, intra, I_PCM, inter with either list, other slices), so every ref_idx / mvd neighbour
case occurs.  The residual is as in Chapter 8.3.  Deblocking is disabled, so I_PCM samples can be checked in ffmpeg's output.

The context selection below is written independently of interref.py (a per-4x4-cell description of each macroblock, closed
formulas); ffmpeg decoding the result and interref.py parsing it are the two cross-checks.
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

_PAIRS = [("L0", "L0"), ("L1", "L1"), ("L0", "L1"), ("L1", "L0"), ("L0", "Bi"), ("L1", "Bi"), ("Bi", "L0"), ("Bi", "L1"),
          ("Bi", "Bi")]


def b_parts(mbt):
    """B mb_type 1..21 -> list of prediction modes per partition (Table 7-14)."""
    if mbt <= 3:
        return [("L0", "L1", "Bi")[mbt - 1]]
    return list(_PAIRS[(mbt - 4) // 2])


class SliceEnc:
    def __init__(self, stype, qp, idc, W, first_mb, rng, nref=(1, 1)):
        self.t = stype
        self.nref = nref
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
            cond = lambda n: 0 if (n is None or n["k"] == "i4") else 1
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
                b1, b2 = {0: (0, 0), 1: (1, 1), 2: (1, 0), 3: (0, 1)}[mbt]
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
    def code_intra_syntax(self, m, A, B):
        if m["k"] == "i4":
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
        self.bin(85 + self.CBF[cat] + inc, 0 if coeffs is None else 1)
        if coeffs is None:
            return
        sigb, lastb, absb = 105 + self.SIG[cat], 166 + self.SIG[cat], 227 + self.ABS[cat]
        last_nz = max(i for i, v in enumerate(coeffs) if v)
        for i in range(nmax - 1):
            ci = min(i, 2) if cat == 3 else i
            sig = 1 if coeffs[i] else 0
            self.bin(sigb + ci, sig)
            if sig:
                self.bin(lastb + ci, 1 if i == last_nz else 0)
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

    @staticmethod
    def rand_block(rng, nmax):
        """Random content of one residual block: None (coded_block_flag 0) or a list of nmax levels (at least one non-zero)."""
        if rng.random() < 0.35:
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
                c[i] = mag if rng.random() < 0.5 else -mag
        if not any(c):
            c[0] = 1
        return c

    def code_residual(self, m, A, B):
        """Random residual for every block the macroblock owes; fills m['coef'] (expected dump value) and the flag maps."""
        rng = self.rng
        intra = m["k"] in ("i4", "i16", "pcm")
        cbl, cbc = m["cbp_l"], m["cbp_c"]
        m["dcf"], m["lcbf"], m["cdcf"], m["cacf"] = 0, {}, [0, 0], [{}, {}]
        parts = []

        def emit(kind, idx, c, nmax):
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

    # --- inter prediction syntax (Chapter 8.4)
    # Table 7-17 / 7-18: sub_mb_type -> (shape of the sub-macroblock in 4x4 units: list of (x, y, w, h), prediction)
    PSUB = {0: ([(0, 0, 2, 2)], "L0"), 1: ([(0, 0, 2, 1), (0, 1, 2, 1)], "L0"), 2: ([(0, 0, 1, 2), (1, 0, 1, 2)], "L0"),
            3: ([(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (1, 1, 1, 1)], "L0")}
    BSUB = {0: (None, "Direct"), 1: ("8x8", "L0"), 2: ("8x8", "L1"), 3: ("8x8", "Bi"), 4: ("8x4", "L0"), 5: ("4x8", "L0"),
            6: ("8x4", "L1"), 7: ("4x8", "L1"), 8: ("8x4", "Bi"), 9: ("4x8", "Bi"), 10: ("4x4", "L0"), 11: ("4x4", "L1"),
            12: ("4x4", "Bi")}
    SHAPES = {"8x8": [(0, 0, 2, 2)], "8x4": [(0, 0, 2, 1), (0, 1, 2, 1)], "4x8": [(0, 0, 1, 2), (1, 0, 1, 2)],
              "4x4": [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (1, 1, 1, 1)]}
    PSUB_BINS = {0: [1], 1: [0, 0], 2: [0, 1, 1], 3: [0, 1, 0]}
    BSUB_BINS = {0: "0", 1: "100", 2: "101", 3: "11000", 4: "11001", 5: "11010", 6: "11011", 7: "111000", 8: "111001",
                 9: "111010", 10: "111011", 11: "11110", 12: "11111"}

    def code_sub_type(self, v):
        if self.t == "P":
            for i, b in enumerate(self.PSUB_BINS[v]):
                self.bin(21 + i, b)
            return
        bins = [int(c) for c in self.BSUB_BINS[v]]
        for i, b in enumerate(bins):
            ctx = 36 if i == 0 else 37 if i == 1 else (38 if bins[1] == 1 else 39) if i == 2 else 39
            self.bin(ctx, b)

    @staticmethod
    def cell(m, x, y):
        """(ref_idx > 0 per list, absolute mvd per list) of the 4x4 cell (x, y) of macroblock m; zeros when m has no such data
        (skipped, direct, intra, I_PCM, or a partition that does not use the list)."""
        c = m.get("cells", {}).get((x, y)) if m is not None else None
        return c if c is not None else dict(ref=[0, 0], mvd=[(0, 0), (0, 0)])

    def nb_cell(self, m, A, B, x, y, side):
        """Neighbour cell of (x, y): side 'A' = left, 'B' = above; None when outside the slice / picture."""
        if side == "A":
            return self.cell(m, x - 1, y) if x else (None if A is None else self.cell(A, 3, y))
        return self.cell(m, x, y - 1) if y else (None if B is None else self.cell(B, x, 3))

    def code_ref_idx(self, m, A, B, lst, rect, v):
        x, y = rect[0], rect[1]
        ca = self.nb_cell(m, A, B, x, y, "A")
        cb = self.nb_cell(m, A, B, x, y, "B")
        inc = (1 if ca is not None and ca["ref"][lst] > 0 else 0) + 2 * (1 if cb is not None and cb["ref"][lst] > 0 else 0)
        for i in range(v + 1):
            ctx = 54 + inc if i == 0 else 54 + 4 if i == 1 else 54 + 5
            self.bin(ctx, 1 if i < v else 0)

    def eg(self, v, k):
        while v >= (1 << k):
            self.e.bypass(1)
            v -= 1 << k
            k += 1
        self.e.bypass(0)
        while k:
            k -= 1
            self.e.bypass((v >> k) & 1)

    def code_mvd_comp(self, m, A, B, lst, x, y, comp, val):
        ca = self.nb_cell(m, A, B, x, y, "A")
        cb = self.nb_cell(m, A, B, x, y, "B")
        tot = sum(c["mvd"][lst][comp] for c in (ca, cb) if c is not None)
        base = 40 if comp == 0 else 47
        a = abs(val)
        for i in range(min(a, 9) + (1 if a < 9 else 0)):
            ctx = base + ((0 if tot < 3 else 1 if tot <= 32 else 2) if i == 0 else (3, 4, 5, 6, 6, 6, 6, 6)[i - 1])
            self.bin(ctx, 1 if i < a else 0)
        if a >= 9:
            self.eg(a - 9, 3)
        if a:
            self.e.bypass(1 if val < 0 else 0)

    def rand_mvd_comp(self, rng):
        r = rng.random()
        if r < 0.30:
            return 0
        if r < 0.55:
            return rng.randint(-3, 3)
        if r < 0.75:
            return rng.choice([-1, 1]) * rng.choice([1, 2, 3, 8, 9, 10, 15, 16, 17, 31, 32, 33])
        if r < 0.92:
            return rng.randint(-60, 60)
        if r < 0.98:
            return rng.randint(-300, 300)
        return rng.choice([-1, 1]) * rng.randint(301, 2500)

    def code_inter(self, m, A, B):
        """sub_mb_type x4 (8x8 types), ref_idx_l0 .., ref_idx_l1 .., mvd_l0 .., mvd_l1 .., with random values; fills the
        expected dump fields m['sub'], m['refl'], m['mvdl'] and the cell map later macroblocks look at."""
        rng, t, mbt = self.rng, self.t, m["mbt"]
        m["cells"] = {}
        m["sub"], m["refl"], m["mvdl"] = None, [[], []], [[], []]
        parts = []          # (rect in 4x4 units, mode, top-left 8x8 block of the partition (for ref_idx))
        if mbt == (3 if t == "P" else 22):
            subs = [rng.randrange(4) if t == "P" else rng.choice([0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 12]) for _ in range(4)]
            m["sub"] = subs
            for v in subs:
                self.code_sub_type(v)
            subparts = []
            for b8, v in enumerate(subs):
                ox, oy = (b8 & 1) * 2, (b8 >> 1) * 2
                if t == "P":
                    shape, mode = self.PSUB[v]
                else:
                    sh, mode = self.BSUB[v]
                    shape = self.SHAPES[sh] if sh else None
                if mode == "Direct":
                    continue
                subparts.append((b8, mode, [(ox + a, oy + b, w, h) for a, b, w, h in shape]))
            refunits = [(sp[2][0], sp[1]) for sp in subparts]            # one ref_idx per sub-macroblock
            mvdunits = [(r, sp[1]) for sp in subparts for r in sp[2]]
        else:
            if t == "P":
                rects = {0: [(0, 0, 4, 4)], 1: [(0, 0, 4, 2), (0, 2, 4, 2)], 2: [(0, 0, 2, 4), (2, 0, 2, 4)]}[mbt]
                modes = ["L0"] * len(rects)
            else:
                modes = b_parts(mbt)
                rects = [(0, 0, 4, 4)] if len(modes) == 1 else ([(0, 0, 4, 2), (0, 2, 4, 2)] if mbt % 2 == 0 else [(0, 0, 2, 4), (2, 0, 2, 4)])
            refunits = list(zip(rects, modes))
            mvdunits = list(zip(rects, modes))
        use = lambda mode, lst: mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi"))
        refval = {}                                  # (list, rect) -> ref_idx
        cells = m["cells"]

        def setcell(rect, lst, ref=None, mvd=None):
            x, y, w, h = rect
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    c = cells.setdefault((xx, yy), dict(ref=[0, 0], mvd=[(0, 0), (0, 0)]))
                    if ref is not None:
                        c["ref"][lst] = ref
                    if mvd is not None:
                        c["mvd"][lst] = mvd
        for lst in (0, 1):
            for rect, mode in refunits:
                if not use(mode, lst):
                    continue
                n = self.nref[lst]
                v = 0 if n == 1 else rng.choice([0, 0, 0] + list(range(n)))
                if n > 1:
                    self.code_ref_idx(m, A, B, lst, rect, v)
                m["refl"][lst].append(v)
                # the reference of a partition holds for every 4x4 cell of the partition (rect of the 8x8 / 16x8 / 8x16 / 16x16)
                if mbt == (3 if t == "P" else 22):
                    bx, by = rect[0] // 2 * 2, rect[1] // 2 * 2
                    setcell((bx, by, 2, 2), lst, ref=v)
                else:
                    setcell(rect, lst, ref=v)
        for lst in (0, 1):
            for rect, mode in mvdunits:
                if not use(mode, lst):
                    continue
                vx, vy = self.rand_mvd_comp(rng), self.rand_mvd_comp(rng)
                self.code_mvd_comp(m, A, B, lst, rect[0], rect[1], 0, vx)
                self.code_mvd_comp(m, A, B, lst, rect[0], rect[1], 1, vy)
                setcell(rect, lst, mvd=(abs(vx), abs(vy)))
                m["mvdl"][lst].append((vx, vy))

    # --- one macroblock
    def code_mb(self, addr, m, last):
        A, B = self.nb(addr)
        k = m["k"]
        if self.t != "I":
            base = 11 if self.t == "P" else 24
            cond = lambda n: 0 if (n is None or n["k"] == "skip") else 1
            self.bin(base + cond(A) + cond(B), 1 if k == "skip" else 0)
        pcm_like = False
        if k == "skip":
            self.recs[addr] = m
            m["qp"] = self.qp
            self.prev_nz = 0
        else:
            self.code_mb_type(m, A, B)
            if k == "pcm":
                self.e.align_zero()
                self.e.raw_bytes(m["samples"])
                self.e.restart()
                m["qp"] = self.qp
                self.prev_nz = 0
            else:
                if k in ("i4", "i16"):
                    self.code_intra_syntax(m, A, B)
                elif k == "inter":
                    self.code_inter(m, A, B)
                if k != "i16":
                    self.code_cbp(m, A, B)
                has_qpd = k == "i16" or m["cbp_l"] or m["cbp_c"]
                if has_qpd:
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


def inter_fields(m):
    """sub / refidx / refidx1 / mvd / mvd1 of the dump line, from the construction."""
    if m["k"] != "inter":
        return ["sub=-", "refidx=-", "refidx1=-", "mvd=-", "mvd1=-"]
    lst = lambda v: ",".join(map(str, v)) or "-"
    mvs = lambda v: ",".join(f"{x}:{y}" for x, y in v) or "-"
    return [f"sub={lst(m['sub'] or [])}", f"refidx={lst(m['refl'][0])}", f"refidx1={lst(m['refl'][1])}",
            f"mvd={mvs(m['mvdl'][0])}", f"mvd1={mvs(m['mvdl'][1])}"]


def line_fields(m):
    """Expected dump fields (Readme section 5), assembled from the construction."""
    k = m["k"]
    if k == "skip":
        mbt = "skip"
    else:
        mbt = str(m["mbt"])
    intra_np = k in ("i4", "i16")
    f = [f"mbt={mbt}",
         f"cpred={m['cpred']}" if intra_np else "cpred=-",
         f"modes={m['modes']}" if k == "i4" else "modes=-"] + inter_fields(m) + [
         f"cbp={m['cbp_l'] + 16 * m['cbp_c']}" if k not in ("skip", "pcm") else "cbp=-",
         f"qpd={m['qpd']}" if (k == "i16" or (k in ("i4", "inter", "direct") and (m["cbp_l"] or m["cbp_c"]))) else "qpd=-",
         f"qp={m['qp']}", f"coef={m.get('coef', '-')}", f"eos={m['eos']}"]
    return " ".join(f)


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
    kinds = {"I": [("i4", 5), ("i16", 3), ("pcm", 1)],
             "P": [("skip", 4), ("i4", 2), ("i16", 2), ("pcm", 1), ("inter", 5)],
             "B": [("skip", 4), ("direct", 3), ("i4", 2), ("i16", 2), ("pcm", 1), ("inter", 5)]}[stype]
    k = rng.choices([a for a, _ in kinds], [b for _, b in kinds])[0]
    m = dict(k=k, cpred=0, modes=None, cbp_l=0, cbp_c=0, qpd=0, mbt=None)
    off = {"I": 0, "P": 5, "B": 23}[stype]
    if k == "i4":
        m["mbt"] = off
        m["modes"] = "".join("p" if rng.random() < 0.5 else str(rng.randrange(8)) for _ in range(16))
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
        m["mbt"] = rng.randrange(4) if stype == "P" else rng.randrange(1, 23)
        m["cbp_l"], m["cbp_c"] = rand_cbp(rng)
        m["qpd"] = rand_qpd(rng)
    if not (k == "i16" or m["cbp_l"] or m["cbp_c"]):
        m["qpd"] = 0
    return m


def choose_intra(se, addr, m, off):
    """Pick intra prediction modes that are LEGAL where the macroblock sits (ffmpeg refuses modes that need unavailable
    neighbours), then express the 4x4 modes as prev_intra4x4_pred_mode_flag / rem_intra4x4_pred_mode against the predicted
    mode (8.3.1.1), so the syntax elements in the dump are what an encoder would really send."""
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
    am, out = {}, ""
    for idx in range(16):
        x4 = (idx % 2) + 2 * ((idx // 4) % 2)
        y4 = ((idx // 2) % 2) + 2 * (idx // 8)
        top = y4 > 0 or T
        left = x4 > 0 or L
        tl = (x4 > 0 and y4 > 0) or (x4 == 0 and y4 > 0 and L) or (y4 == 0 and x4 > 0 and T) or (x4 == 0 and y4 == 0 and TL)
        legal = [2] + ([0, 3, 7] if top else []) + ([1, 8] if left else []) + ([4, 5, 6] if top and left and tl else [])
        act = rng.choice(legal)

        def nbmode(n, xx, yy):
            return 2 if n["k"] != "i4" else n["amodes"][(xx, yy)]
        if x4 > 0:
            ma = am[(x4 - 1, y4)]
        else:
            ma = None if A is None else nbmode(A, 3, y4)
        if y4 > 0:
            mb = am[(x4, y4 - 1)]
        else:
            mb = None if B is None else nbmode(B, x4, 3)
        pred = 2 if (ma is None or mb is None) else min(ma, mb)
        am[(x4, y4)] = act
        out += "p" if act == pred else str(act if act < pred else act - 1)
    m["amodes"], m["modes"] = am, out


# ---------------------------------------------------------------- stream
MBW, MBH = 11, 6


def slice_header(w, stype, first_mb, qp, idc, pic, spatial, nref=(1, 1)):
    w.ue(first_mb)
    w.ue(stype)
    w.ue(0)
    w.raw(4, pic["frame_num"])
    if pic["idr"]:
        w.ue(0)
    w.raw(6, pic["poc"])
    if stype == 1:
        w.raw(1, spatial)
    if stype != 2:
        override = nref != (1, 1) if stype == 1 else nref[0] != 1
        w.raw(1, 1 if override else 0)
        if override:
            w.ue(nref[0] - 1)
            if stype == 1:
                w.ue(nref[1] - 1)
        w.raw(1, 0)
        if stype == 1:
            w.raw(1, 0)
    if pic["ref_idc"]:
        w.raw(2, 0) if pic["idr"] else w.raw(1, 0)
    if stype != 2:
        w.ue(idc)
    w.se(qp - 26)
    w.ue(1)
    while len(w.bits) % 8:
        w.raw(1, 1)


# reference pictures available to each picture of the stream below (IDR, P, P, B, B, P): bounds num_ref_idx_active per list
AVAIL = [0, 1, 2, 3, 3, 3]


def build(seed, multiref=None):
    """-> (annexb bytes, manifest dict).  multiref: more than one active reference per list (default: from the seed, seeds 1
    and 2 use one reference everywhere, so ref_idx is never coded and temporal direct is legal)."""
    rng = random.Random(seed)
    if multiref is None:
        multiref = seed > 2
    sps = bitwriter.W()
    sps.raw(8, 77), sps.raw(8, 0), sps.raw(8, 30)
    sps.ue(0), sps.ue(0), sps.ue(0), sps.ue(2), sps.ue(4), sps.raw(1, 0)
    sps.ue(MBW - 1), sps.ue(MBH - 1), sps.raw(1, 1), sps.raw(1, 1), sps.raw(1, 0), sps.raw(1, 0)
    pps = bitwriter.W()
    pps.ue(0), pps.ue(0), pps.raw(1, 1), pps.raw(1, 0), pps.ue(0), pps.ue(0), pps.ue(0), pps.raw(1, 0), pps.raw(2, 0)
    pps.se(0), pps.se(0), pps.se(0), pps.raw(1, 1), pps.raw(1, 0), pps.raw(1, 0)
    out = bytearray()
    out += bitwriter.nal(0x67, sps.rbsp()) + bitwriter.nal(0x68, pps.rbsp())
    pics = [dict(idr=True, ref_idc=3, frame_num=0, poc=0, t="I"),
            dict(idr=False, ref_idc=2, frame_num=1, poc=8, t="P"),
            dict(idr=False, ref_idc=2, frame_num=2, poc=16, t="P"),
            dict(idr=False, ref_idc=0, frame_num=3, poc=12, t="B"),
            dict(idr=False, ref_idc=0, frame_num=3, poc=10, t="B"),
            dict(idr=False, ref_idc=2, frame_num=3, poc=24, t="P")]
    n = MBW * MBH
    exp_lines, pic_info, slice_no = [], [], 0
    for pi, pic in enumerate(pics):
        stype = {"I": 2, "P": 0, "B": 1}[pic["t"]]
        cuts = sorted(rng.sample(range(1, n), rng.choice([0, 1, 2, 4])))
        starts = [0] + cuts
        ends = cuts + [n]
        slices_info = []
        for s0, s1 in zip(starts, ends):
            qp = rng.choice([rng.randrange(0, 52), rng.randrange(18, 40)])
            idc = rng.randrange(3)
            spatial = rng.randrange(2) if not multiref else 1
            nref = (1, 1)
            if multiref and stype != 2:
                nref = (rng.randint(1, AVAIL[pi]), rng.randint(1, AVAIL[pi]) if stype == 1 else 1)
            w = bitwriter.W()
            slice_header(w, stype, s0, qp, idc, pic, spatial, nref)
            hdr_bits = list(w.bits)
            se = SliceEnc(pic["t"], qp, idc, MBW, s0, rng, nref)
            lines = [f"SLICE {slice_no} type={pic['t']} first_mb={s0}"]
            mbs_info = []
            for addr in range(s0, s1):
                m = rand_mb(rng, pic["t"])
                if m["k"] in ("i4", "i16"):
                    choose_intra(se, addr, m, {"I": 0, "P": 5, "B": 23}[pic["t"]])
                se.code_mb(addr, m, addr == s1 - 1)
                lines.append(f"MB {slice_no} {addr} " + line_fields(m))
                if m["k"] == "pcm":
                    mbs_info.append(dict(kind="pcm", addr=addr, samples=m["samples"].hex()))
            lines.append(f"END {slice_no} mbs={s1 - s0}")
            bits = hdr_bits + se.e.bits
            while len(bits) % 8:
                bits.append(0)
            rbsp = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
            out += bitwriter.nal((pic["ref_idc"] << 5) | (5 if pic["idr"] else 1), rbsp)
            exp_lines += lines
            slices_info.append(dict(mbs=mbs_info))
            slice_no += 1
        pic_info.append(slices_info)
    manifest = dict(width_mbs=MBW, height_mbs=MBH, pictures=pic_info, expected="\n".join(exp_lines) + "\n",
                    note="synthetic CABAC slices, expected dump from the construction")
    return bytes(out), manifest
