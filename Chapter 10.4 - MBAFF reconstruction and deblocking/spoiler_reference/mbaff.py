"""Chapter 10.3 reference: the MBAFF macroblock-layer parser (CABAC and CAVLC; I, P and B slices; 8-bit 4:2:0; transform 8x8)
and the neighbour derivation of 6.4.12.2 (Table 6-4), written for the harness.

One parser, two modes.
  * decode mode (the harness's truth): reads an existing slice; a slice must end exactly (end_of_slice_flag = 1 with the stop
    bit where the arithmetic coder ended, or more_rbsp_data() false exactly at the rbsp_stop_one_bit).
  * generate mode (mbgen.py): the very same code is driven by a *random source* instead of a stream: every bin / bit the parser
    would read is drawn at random, written by an independent CABAC encoder (cabac_enc.py) or a bit writer, and the parser's own
    state follows it.  Rejection sampling (`pick`) keeps the stream legal (valid intra modes, ref_idx range, QP range, ...).  The
    result is a conforming stream together with the parse of it.  What validates the neighbour rules is NOT this self-consistency
    but ffmpeg (which decodes the generated streams with its own context derivation) and x264's real streams.
MUTANT: the harness's --self-test switches ONE deliberately wrong rule on; every mutant must be caught.
Dump grammar: Readme section 5.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import cabac_ref as R  # noqa: E402
import cabac_enc  # noqa: E402
from common import cavlc_ref as CR  # noqa: E402
from common import cavlc_resid as CRes  # noqa: E402

MUTANT = None
MUTANTS = {
    # --- Table 6-4 families (neighbour derivation)
    "t64_A_frame_cur_field_left_parity": "cur frame MB, left pair field: always the top field MB (the row parity rule is missing)",
    "t64_A_frame_bottom_row": "cur BOTTOM frame MB, left field pair: yM = yN >> 1 instead of (yN + 16) >> 1",
    "t64_A_field_cur_left_frame_noshift": "cur field MB, left pair frame: the left MB is chosen by yN < 8 but yM = yN (not 2 * yN + parity)",
    "t64_A_bottom_field_cur_top_mb": "cur BOTTOM field MB, left pair field: the top MB of the left pair instead of the bottom MB",
    "t64_B_top_frame_above_field": "cur top frame MB, above pair field: the top field MB instead of the bottom one",
    "t64_B_bottom_frame_above_pair": "cur bottom frame MB: B is taken from the pair above (B + 1) instead of the top MB of its own pair",
    "t64_B_top_field_above_frame": "cur top field MB, above pair frame: the top frame MB (B) instead of the bottom one (B + 1)",
    "t64_B_bottom_field": "cur bottom field MB: B is the top MB of the pair above instead of its bottom MB",
    "t64_naive": "neighbours are the same-position MB of the left pair / the MB above in the same row (no MBAFF rules)",
    # --- field decoding flag
    "infer_B_first": "inferred mb_field_decoding_flag: the pair above is tried before the left pair",
    "infer_always_frame": "inferred mb_field_decoding_flag is always 0",
    "infer_left_wraps_row": "inferred mb_field_decoding_flag: at the start of a pair row the 'left' pair is the last pair of the row above",
    "fld_ctx_only_A": "mb_field_decoding_flag ctxIdxInc counts the left pair only",
    "fld_ctx_unavail1": "mb_field_decoding_flag ctxIdxInc counts an unavailable pair as field",
    "fld_ctx_base71": "mb_field_decoding_flag uses ctxIdx 71 + increment",
    "skip_ctx_cur_frame": "mb_skip_flag of a pair whose field flag is not decoded yet: neighbours are derived as for a frame MB",
    "bottom_skip_peek_order": "top MB skipped: mb_field_decoding_flag is read before the bottom MB's mb_skip_flag",
    "bottom_flag_always": "the bottom MB reads mb_field_decoding_flag even when the top MB was not skipped",
    "top_flag_not_propagated": "top MB skipped, flag read with the bottom MB: the top MB keeps the inferred flag",
    "eos_after_top": "end_of_slice_flag is read after the top MB of a pair too",
    "cavlc_bottom_flag_missing": "CAVLC: the bottom MB does not read mb_field_decoding_flag after a skipped top MB",
    "cavlc_top_flag_after_skip_missing": "CAVLC: a top MB following a skip run does not read mb_field_decoding_flag",
    # --- contexts / nC from neighbours
    "nc_naive": "nC: neighbouring blocks from the naive (non-MBAFF) neighbour MBs",
    "cbf_naive": "coded_block_flag: neighbouring blocks from the naive neighbour MBs",
    "cbp_luma_naive_b8": "coded_block_pattern luma ctxIdxInc: neighbouring 8x8 block = same row / column of the neighbour MB (non-MBAFF)",
    "cpred_naive": "intra_chroma_pred_mode ctxIdxInc: naive neighbour MBs",
    "mbtype_naive": "mb_type ctxIdxInc: naive neighbour MBs",
    "t8_naive": "transform_size_8x8_flag ctxIdxInc: naive neighbour MBs",
    "pm_naive": "intra prediction mode prediction: naive neighbours",
    "pm8_n_always_1": "Intra8x8 mode prediction from a 4x4 left neighbour: always block 8x8idx * 4 + 1 (the n = 3 case of a frame MB next to a field pair missing)",
    "mvd_scale_none": "mvd ctxIdxInc: vertical neighbour mvd not scaled between frame and field MBs",
    "mvd_scale_swapped": "mvd ctxIdxInc: scaling directions swapped (frame cur / field nb: /2, field cur / frame nb: *2)",
    "mvd_scale_x_too": "mvd ctxIdxInc: the horizontal component is scaled as well",
    "ref_ctx_no_adjust": "ref_idx ctxIdxInc: refIdx > 1 rule for field neighbours of frame MBs missing",
    "ref_ctx_adjust_wrong": "ref_idx ctxIdxInc: refIdx > 1 also used for frame neighbours of field MBs",
    "ref_range_not_doubled": "field MB: ref_idx te(v) range is num_ref_idx_active - 1 (not 2 * num_ref_idx_active - 1)",
    "ref_present_ignores_field": "field MB with num_ref_idx_active = 1: ref_idx is not read",
    "qp_pred_left": "QP_Y,PRED is the QP of the left neighbour MB instead of the previous MB in decoding order",
    "qpd_ctx_left": "mb_qp_delta ctxIdxInc uses the left neighbour instead of the previous MB in decoding order",
    "sig_ctx_frame_in_field": "field MBs use the frame ctxIdxOffsets for significant_coeff_flag / last_significant_coeff_flag",
    "sig8_table_frame_in_field": "field MBs: cat 5 significance uses the frame Table 9-43 column",
    "skip_ctx_naive": "mb_skip_flag ctxIdxInc: naive neighbour MBs",
    "mbaff_slice_start": "first_mb_in_slice is not doubled (first MB = first_mb_in_slice)",
}


def mut(name):
    return MUTANT == name


class Unusable(Exception):
    """The reference cannot parse this stream (unsupported feature or a parse that does not end cleanly)."""


def clip3(lo, hi, x):
    return lo if x < lo else hi if x > hi else x


# ---------------------------------------------------------------- tables (7-13, 7-14, 7-17, 7-18 shapes in 4x4 units)
R16, R168, R816 = [(0, 0, 4, 4)], [(0, 0, 4, 2), (0, 2, 4, 2)], [(0, 0, 2, 4), (2, 0, 2, 4)]
P_TYPES = {0: (R16, ("L0",)), 1: (R168, ("L0", "L0")), 2: (R816, ("L0", "L0"))}
_B_PAIRS = [("L0", "L0"), ("L1", "L1"), ("L0", "L1"), ("L1", "L0"), ("L0", "Bi"), ("L1", "Bi"), ("Bi", "L0"),
            ("Bi", "L1"), ("Bi", "Bi")]
B_TYPES = {1: (R16, ("L0",)), 2: (R16, ("L1",)), 3: (R16, ("Bi",))}
for _k, _p in enumerate(_B_PAIRS):
    B_TYPES[4 + 2 * _k] = (R168, _p)
    B_TYPES[5 + 2 * _k] = (R816, _p)
_S88, _S84, _S48 = [(0, 0, 2, 2)], [(0, 0, 2, 1), (0, 1, 2, 1)], [(0, 0, 1, 2), (1, 0, 1, 2)]
_S44 = [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 1, 1), (1, 1, 1, 1)]
P_SUB = {0: (_S88, "L0"), 1: (_S84, "L0"), 2: (_S48, "L0"), 3: (_S44, "L0")}
B_SUB = {0: (None, "Direct"), 1: (_S88, "L0"), 2: (_S88, "L1"), 3: (_S88, "Bi"), 4: (_S84, "L0"), 5: (_S48, "L0"),
         6: (_S84, "L1"), 7: (_S48, "L1"), 8: (_S84, "Bi"), 9: (_S48, "Bi"), 10: (_S44, "L0"), 11: (_S44, "L1"),
         12: (_S44, "Bi")}
SIG8 = [0, 1, 2, 3, 4, 5, 5, 4, 4, 3, 3, 4, 4, 4, 5, 5, 4, 4, 4, 4, 3, 3, 6, 7, 7, 7, 8, 9, 10, 9, 8, 7,
        7, 6, 11, 12, 13, 11, 6, 7, 8, 9, 14, 10, 9, 8, 6, 11, 12, 13, 11, 6, 9, 14, 10, 9, 11, 12, 13, 11, 14, 10, 12]
SIG8F = [0, 1, 1, 2, 2, 3, 3, 4, 5, 6, 7, 7, 7, 8, 4, 5, 6, 9, 10, 10, 8, 11, 12, 11, 9, 9, 10, 10, 8, 11, 12, 11,
         9, 9, 10, 10, 8, 11, 12, 11, 9, 9, 10, 10, 8, 13, 13, 9, 9, 10, 10, 8, 13, 13, 9, 9, 10, 10, 14, 14, 14, 14, 14]
LAST8 = [0] + [1] * 15 + [2] * 16 + [3] * 8 + [4] * 8 + [5] * 4 + [6] * 4 + [7] * 4 + [8] * 3
CBF_OFF = [0, 4, 8, 12, 16]
SIG_OFF = [0, 15, 29, 44, 47]
ABS_OFF = [0, 10, 20, 30, 39]
INTRA_ALLOWED_4 = {0: "T", 1: "L", 2: "", 3: "T", 4: "TLD", 5: "TLD", 6: "TLD", 7: "T", 8: "L"}   # samples a 4x4 / 8x8 mode needs


def blk_xy(idx):
    """luma4x4BlkIdx -> (x4, y4)."""
    return (idx % 2) + 2 * ((idx // 4) % 2), ((idx // 2) % 2) + 2 * (idx // 8)


# ================================================================ sources (decode / generate)
class CabacDec:
    gen = False

    def __init__(self, nal, bitpos):
        self.nal = nal
        self.e = R.Engine(nal, bitpos)

    def decision(self, st, p=None):
        return self.e.decision(st)

    def bypass(self, p=None):
        return self.e.bypass()

    def term_pcm(self, p=0.0):
        return self.e.terminate()

    def term_end(self, last):
        return self.e.terminate()

    def pcm(self):
        p0 = (self.e.pos + 7) & ~7
        if p0 // 8 + 384 > len(self.nal):
            raise Unusable("I_PCM samples run past the end of the NAL unit")
        data = bytes(self.nal[p0 // 8:p0 // 8 + 384])
        self.e.restart_after_pcm(384)
        return data

    @property
    def pos(self):
        return self.e.pos


class CabacGen:
    gen = True

    def __init__(self, rng):
        self.enc = cabac_enc.Encoder()
        self.rng = rng
        self.journal = []

    def decision(self, st, p=None):
        b = 1 if self.rng.random() < (0.5 if p is None else p) else 0
        self.journal.append((st, st[0], st[1]))
        self.enc.decision(st, b)
        return b

    def bypass(self, p=None):
        b = 1 if self.rng.random() < (0.5 if p is None else p) else 0
        self.enc.bypass(b)
        return b

    def term_pcm(self, p=0.0):
        b = 1 if self.rng.random() < p else 0
        self.enc.terminate(b)
        return b

    def term_end(self, last):
        self.enc.terminate(1 if last else 0)
        return 1 if last else 0

    def pcm(self):
        self.enc.align_zero()
        data = bytes(self.rng.randrange(40, 216) for _ in range(384))
        self.enc.raw_bytes(data)
        self.enc.restart()
        return data

    def mark(self):
        e = self.enc
        return (e.low, e.range, e.first, e.out, len(e.bits), len(self.journal))

    def reset(self, m):
        e = self.enc
        e.low, e.range, e.first, e.out = m[:4]
        del e.bits[m[4]:]
        while len(self.journal) > m[5]:
            st, p, v = self.journal.pop()
            st[0], st[1] = p, v

    def result(self):
        self.enc.align_zero()
        return self.enc.bits


class BitsDec:
    gen = False

    def __init__(self, nal, pos):
        self.br = CR.BitReader(nal, pos)
        self.nal = nal

    def u(self, n, p=None):
        return self.br.u(n)

    def ue(self, w=None):
        return self.br.ue()

    def se(self, w=None):
        return self.br.se()

    def more_data(self, addr):
        return self.br.more_rbsp_data()

    def pcm(self):
        self.br.pos = (self.br.pos + 7) & ~7
        return bytes(self.br.u(8) for _ in range(384))

    @property
    def pos(self):
        return self.br.pos


class BitsGen:
    gen = True

    def __init__(self, rng, end_addr):
        self.rng, self.bits, self.end_addr = rng, [], end_addr
        self.base = 0                  # bits of the slice header written before the first data bit (for byte alignment)

    def u(self, n, p=None):
        v = 0
        for _ in range(n):
            v = (v << 1) | (1 if self.rng.random() < (0.5 if p is None else p) else 0)
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]
        return v

    def _put_ue(self, v):
        v += 1
        n = v.bit_length()
        self.bits += [0] * (n - 1) + [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def _sample(self, w):
        if w is None:
            k = 0
            while self.rng.random() < 0.5 and k < 40:
                k += 1
            return (1 << k) - 1 + self.rng.randrange(1 << k)          # the value of a random ue(v) code
        return self.rng.choices(range(len(w)), weights=w)[0]

    def ue(self, w=None):
        v = self._sample(w)
        self._put_ue(v)
        return v

    def se(self, w=None):
        m = self._sample(w) if w is not None else min(self._sample(None), 40)
        v = m if (m == 0 or self.rng.random() < 0.5) else -m
        self._put_ue(2 * v - 1 if v > 0 else -2 * v)
        return v

    def more_data(self, addr):
        return addr < self.end_addr

    def pcm(self):
        while (self.base + len(self.bits)) % 8:
            self.bits.append(0)
        data = bytes(self.rng.randrange(40, 216) for _ in range(384))
        for x in data:
            self.bits += [(x >> (7 - i)) & 1 for i in range(8)]
        return data

    def mark(self):
        return len(self.bits)

    def reset(self, m):
        del self.bits[m:]

    @property
    def pos(self):
        return len(self.bits)


# ================================================================ per-macroblock record
class Mb:
    """What later macroblocks need to know about this one, plus the dump fields."""

    def __init__(self, addr):
        self.addr = addr
        self.fld = 0                # mb_field_decoding_flag of the pair (inferred until decoded)
        self.skip = False
        self.pcm = False
        self.intra = False
        self.i_nxn = False
        self.i16 = False
        self.direct = False
        self.mbt = None
        self.cbp_l = 0
        self.cbp_c = 0
        self.cpred = 0
        self.qpd = 0
        self.qpd_present = False
        self.t8 = 0
        self.qp = 0
        self.eos = None
        self.modes = None           # dump: prev/rem syntax
        self.pm = []                # dump: derived Intra4x4/8x8 pred modes, decoding order
        self.ipm = [2] * 16         # derived pred mode per 4x4 cell (8x8 modes replicated)
        self.lcbf = [0] * 16
        self.dccbf = 0
        self.cdc = [0, 0]
        self.cac = [[0] * 4, [0] * 4]
        self.nzc = [0] * 16         # CAVLC total_coeff per 4x4 cell (raster)
        self.nzcc = [[0] * 4, [0] * 4]
        self.mvd = [[(0, 0)] * 16, [(0, 0)] * 16]    # absolute mvd per 4x4 cell, per list
        self.ref = [[0] * 4, [0] * 4]
        self.used = [[False] * 4, [False] * 4]
        self.dir8 = [False] * 4
        self.noSub = True
        self.sub = None
        self.refl = [[], []]
        self.mvdl = [[], []]
        self.blocks = []
        self.pcm_samples = None


class Info:
    """The slice facts the parser needs (decode mode gets a cavlc_ref.SliceInfo, generate mode builds one of these)."""

    def __init__(self, **kw):
        self.hdr, self.sps, self.pps, self.nal = {}, {}, {}, b""
        self.header_end_bit, self.index = 0, 0
        self.num_ref_idx_l0_active = self.num_ref_idx_l1_active = 1
        self.transform_8x8_mode = 0
        self.__dict__.update(kw)


# ================================================================ the slice parser
class Slice:
    def __init__(self, info, gen_rng=None, end_addr=None, params=None):
        self.info = info
        self.W = info.pic_width_in_mbs
        self.fm = info.first_mb
        self.t = {0: "P", 1: "B", 2: "I", 3: "P", 4: "I"}[info.slice_type]
        self.cabac = bool(info.entropy)
        self.cip = bool(info.pps.get("constrained_intra_pred_flag", 0))
        self.gen = gen_rng is not None
        self.rng = gen_rng
        self.P = params or {}
        self.end_addr = end_addr
        self.recs = {}
        self.qp = info.slice_qp
        self.prev_nz = False
        if self.cabac:
            self.st = R.init_contexts(self.t, info.hdr.get("cabac_init_idc", 0), info.slice_qp)
            self.eng = CabacGen(gen_rng) if self.gen else CabacDec(info.nal, ((info.header_end_bit + 7) // 8) * 8)
        else:
            self.eng = BitsGen(gen_rng, end_addr) if self.gen else BitsDec(info.nal, info.header_end_bit)

    # ------------------------------------------------------------ generation helpers
    def pick(self, fn, ok=None, tries=400):
        """Decode mode: fn().  Generate mode: draw until fn() yields a value that `ok` accepts (ok returns False/True or an
        acceptance probability); the source is rewound after every rejected draw."""
        if not self.gen:
            return fn()
        for _ in range(tries):
            m = self.eng.mark()
            try:
                v = fn()
                w = 1 if ok is None else ok(v)
                if w is True or (w and (w >= 1 or self.rng.random() < w)):
                    return v
            except (Unusable, CR.ParseError):
                pass
            self.eng.reset(m)
        raise Unusable("generator could not find a legal value")

    def dec(self, ctx, p=None):
        return self.eng.decision(self.st[ctx], p)

    def pr(self, key, default):
        return self.P.get(key, default)

    # ------------------------------------------------------------ 6.4.12.2 neighbour derivation (Table 6-4)
    def pair_ok(self, q, cur_pair):
        """Is the macroblock pair with index q (top MB 2q) available for the pair `cur_pair`: inside the picture (checked by the
        caller), decoded already, and in the same slice (addresses of this slice are all >= first_mb)."""
        return q >= 0 and 2 * q >= self.fm and 2 * q in self.recs

    def pair_nbs(self, p):
        """-> (A, B, C, D) pair indices or None (6.4.10)."""
        W = self.W
        a = p - 1 if p % W else None
        b = p - W
        c = p - W + 1 if (p + 1) % W else None
        d = p - W - 1 if p % W else None
        f = lambda q: q if (q is not None and self.pair_ok(q, p)) else None
        return f(a), f(b), f(c), f(d)

    def nb(self, addr, xN, yN, maxW=16, maxH=16):
        """6.4.12.2: luma (or, with maxW = maxH = 8, chroma) location (xN, yN) relative to the upper-left sample of macroblock
        `addr` -> (mbAddrN, xW, yW) or None when not available."""
        if 0 <= xN < maxW and 0 <= yN < maxH:
            return addr, xN, yN
        if yN >= maxH or (xN >= maxW and yN >= 0):
            return None
        if mut("t64_naive"):
            return self.nb_naive(addr, xN, yN, maxW, maxH)
        cur = self.recs[addr]
        top = addr % 2 == 0
        cf = not cur.fld
        p = addr // 2
        A, B, C, D = self.pair_nbs(p)
        fl = lambda q: self.recs[2 * q].fld
        if yN < 0:
            if xN < 0:                                                   # ---- D region
                if cf:
                    if top:
                        if D is None:
                            return None
                        n, yM = 2 * D + 1, yN
                    else:
                        if A is None:
                            return None
                        if not fl(A):
                            n, yM = 2 * A, yN
                        else:
                            n, yM = 2 * A + 1, (yN + maxH) >> 1
                else:
                    if D is None:
                        return None
                    if top:
                        if not fl(D):
                            n, yM = 2 * D + 1, 2 * yN
                        else:
                            n, yM = 2 * D, yN
                    else:
                        n, yM = 2 * D + 1, yN
            elif xN < maxW:                                              # ---- B region
                if cf:
                    if top:
                        if B is None:
                            return None
                        n, yM = 2 * B + 1, yN
                        if mut("t64_B_top_frame_above_field") and fl(B):
                            n = 2 * B
                    else:
                        n, yM = addr - 1, yN
                        if mut("t64_B_bottom_frame_above_pair"):
                            if B is None:
                                return None
                            n = 2 * B + 1
                else:
                    if B is None:
                        return None
                    if top:
                        if not fl(B):
                            n, yM = 2 * B + 1, 2 * yN
                            if mut("t64_B_top_field_above_frame"):
                                n = 2 * B
                        else:
                            n, yM = 2 * B, yN
                    else:
                        n, yM = 2 * B + 1, yN
                        if mut("t64_B_bottom_field"):
                            n = 2 * B
            else:                                                        # ---- C region
                if cf:
                    if not top or C is None:
                        return None
                    n, yM = 2 * C + 1, yN
                else:
                    if C is None:
                        return None
                    if top:
                        if not fl(C):
                            n, yM = 2 * C + 1, 2 * yN
                        else:
                            n, yM = 2 * C, yN
                    else:
                        n, yM = 2 * C + 1, yN
        else:                                                            # ---- A region (xN < 0, 0 <= yN < maxH)
            if A is None:
                return None
            if cf:
                if not fl(A):
                    n, yM = 2 * A + (0 if top else 1), yN
                else:
                    par = yN % 2
                    if mut("t64_A_frame_cur_field_left_parity"):
                        par = 0
                    n = 2 * A + par
                    yM = (yN >> 1) if top else ((yN + maxH) >> 1)
                    if not top and mut("t64_A_frame_bottom_row"):
                        yM = yN >> 1
            else:
                par = 0 if top else 1
                if not fl(A):
                    if yN < maxH // 2:
                        n, yM = 2 * A, (yN << 1) + par
                    else:
                        n, yM = 2 * A + 1, (yN << 1) + par - maxH
                    if mut("t64_A_field_cur_left_frame_noshift"):
                        yM = yN
                else:
                    n, yM = 2 * A + par, yN
                    if not top and mut("t64_A_bottom_field_cur_top_mb"):
                        n = 2 * A
        return n, (xN + maxW) % maxW, (yM + maxH) % maxH

    def nb_naive(self, addr, xN, yN, maxW, maxH):
        """The WRONG derivation of the mutants: the left pair's MB at the same position, the MB above in the same column
        (previous pair row's bottom MB for a top MB), no frame / field logic."""
        W, top = self.W, addr % 2 == 0
        p = addr // 2
        if yN < 0 and 0 <= xN < maxW:
            if not top:
                n = addr - 1
            else:
                q = p - W
                n = 2 * q + 1
            ok = n in self.recs and n >= self.fm
            return (n, xN, yN % maxH) if ok else None
        if xN < 0 and yN >= 0:
            n = addr - 2
            ok = p % W and n in self.recs and n >= self.fm
            return (n, xN % maxW, yN) if ok else None
        return None

    def mbab(self, rec, mut_name=None):
        """6.4.11.1: the macroblocks A (containing luma location (-1, 0)) and B ((0, -1)) as records (None = not available)."""
        if mut_name and mut(mut_name):
            a, b = self.nb_naive(rec.addr, -1, 0, 16, 16), self.nb_naive(rec.addr, 0, -1, 16, 16)
        else:
            a, b = self.nb(rec.addr, -1, 0), self.nb(rec.addr, 0, -1)
        return (self.recs[a[0]] if a else None, self.recs[b[0]] if b else None)

    def infer_fld(self, pair):
        """7.4.4: inferred mb_field_decoding_flag of a pair whose flag is not (yet) decoded."""
        A, B, _, _ = self.pair_nbs(pair)
        if mut("infer_left_wraps_row"):
            A = pair - 1 if 2 * (pair - 1) in self.recs and 2 * (pair - 1) >= self.fm else A
        if mut("infer_always_frame"):
            return 0
        order = (B, A) if mut("infer_B_first") else (A, B)
        for q in order:
            if q is not None:
                return self.recs[2 * q].fld
        return 0

    # ------------------------------------------------------------ CABAC: mb_skip_flag, mb_field_decoding_flag
    def skip_flag(self, rec):
        A, B = self.mbab(rec, "skip_ctx_naive")
        inc = (0 if A is None or A.skip else 1) + (0 if B is None or B.skip else 1)
        return self.dec((11 if self.t == "P" else 24) + inc, self.pr("p_skip", 0.3))

    def field_flag_cabac(self, rec):
        p = rec.addr // 2
        A, B, _, _ = self.pair_nbs(p)
        ca = 1 if A is not None and self.recs[2 * A].fld else 0
        cb = 1 if B is not None and self.recs[2 * B].fld else 0
        if mut("fld_ctx_unavail1"):
            ca, cb = (1 if A is None else ca), (1 if B is None else cb)
        inc = ca + (0 if mut("fld_ctx_only_A") else cb)
        return self.dec((71 if mut("fld_ctx_base71") else 70) + inc, self.pr("p_field", 0.5))

    # ------------------------------------------------------------ mb_type
    def i_type_body(self, base, islice):
        if self.eng.term_pcm(self.pr("p_pcm", 0.03)):
            return 25
        if islice:
            c_luma, c_cnz, c_c2, c_p1, c_p0 = base + 3, base + 4, base + 5, base + 6, base + 7
        else:
            c_luma, c_cnz, c_c2, c_p1, c_p0 = base + 1, base + 2, base + 2, base + 3, base + 3
        luma = self.dec(c_luma)
        chroma = 0
        if self.dec(c_cnz):
            chroma = 1 + self.dec(c_c2)
        p1 = self.dec(c_p1)
        p0 = self.dec(c_p0)
        return 1 + 2 * p1 + p0 + 4 * chroma + 12 * luma

    def i_type_islice(self, A, B):
        def cond(n):
            return 0 if n is None or n.i_nxn else 1
        inc = cond(A) + cond(B)
        if self.dec(3 + inc, 0.5) == 0:
            return 0
        return self.i_type_body(3, True)

    def i_suffix(self, base):
        if self.dec(base, 0.5) == 0:
            return 0
        return self.i_type_body(base, False)

    def p_type(self):
        if self.dec(14, self.pr("p_intra", 0.2)):
            return True, self.i_suffix(17)
        if self.dec(15) == 0:
            return False, (3 if self.dec(16) else 0)
        return False, (1 if self.dec(17) else 2)

    def b_type(self, A, B):
        def cond(n):
            return 0 if n is None or n.direct else 1
        if self.dec(27 + cond(A) + cond(B), 0.7) == 0:
            return False, 0
        if self.dec(27 + 3) == 0:
            return False, 1 + self.dec(27 + 5)
        bits = self.dec(27 + 4) << 3
        bits |= self.dec(27 + 5) << 2
        bits |= self.dec(27 + 5) << 1
        bits |= self.dec(27 + 5)
        if bits < 8:
            return False, bits + 3
        if bits == 13:
            return True, self.i_suffix(32)
        if bits == 14:
            return False, 11
        if bits == 15:
            return False, 22
        bits = (bits << 1) | self.dec(27 + 5)
        return False, bits - 4

    def mb_type(self, rec, A, B):
        """-> (intra, value): value = I mb_type for intra, else the P / B mb_type of the slice's own table."""
        if self.cabac:
            if self.t == "I":
                return True, self.i_type_islice(A, B)
            if self.t == "P":
                return self.p_type()
            return self.b_type(A, B)
        if self.t == "I":
            v = self.eng.ue([8] + [1] * 24 + [1])
            if v > 25:
                raise Unusable("I mb_type > 25")
            return True, v
        if self.t == "P":
            v = self.eng.ue([12, 6, 6, 6, 1] + [2] * 26)
            if v > 30:
                raise Unusable("P mb_type > 30")
            return (False, v) if v < 5 else (True, v - 5)
        v = self.eng.ue([8, 5, 5, 5] + [2] * 19 + [2] * 26)
        if v > 48:
            raise Unusable("B mb_type > 48")
        return (False, v) if v < 23 else (True, v - 23)

    # ------------------------------------------------------------ intra syntax
    def t8_flag(self, rec, A, B):
        if not self.cabac:
            return self.eng.u(1, self.pr("p_t8", 0.4))
        if mut("t8_naive"):
            A, B = self.mbab(rec, "t8_naive")
        cond = lambda n: 1 if (n is not None and n.t8) else 0
        return self.dec(399 + cond(A) + cond(B), self.pr("p_t8", 0.4))

    def chroma_pred(self, rec, A, B):
        if not self.cabac:
            return self.eng.ue([5, 2, 2, 1])
        if mut("cpred_naive"):
            A, B = self.mbab(rec, "cpred_naive")
        cond = lambda n: 0 if (n is None or not n.intra or n.pcm or n.cpred == 0) else 1
        inc = cond(A) + cond(B)
        if self.dec(64 + inc) == 0:
            return 0
        if self.dec(67) == 0:
            return 1
        return 2 if self.dec(67) == 0 else 3

    def ipm_pred(self, rec, x, y, is8):
        """8.3.1.1 / 8.3.2.1: predIntra4x4PredMode / predIntra8x8PredMode of the block whose top-left sample is (x, y)."""
        if mut("pm_naive"):
            a = self.nb_naive(rec.addr, x - 1, y, 16, 16) if x == 0 else (rec.addr, x - 1, y)
            b = self.nb_naive(rec.addr, x, y - 1, 16, 16) if y == 0 else (rec.addr, x, y - 1)
        else:
            a, b = self.nb(rec.addr, x - 1, y), self.nb(rec.addr, x, y - 1)

        def mode(n, which):
            if n is None:
                return None
            m = self.recs[n[0]]
            if not m.intra and self.cip:
                return None
            if not m.i_nxn:
                return 2
            xW, yW = n[1], n[2]
            if is8 and not m.t8:
                # 8.3.2.1: 4x4 block luma8x8BlkIdxN * 4 + n; n = 2 for B; for A n = 1, except n = 3 when the current MB is a
                # frame MB, mbAddrA a field MB and the current 8x8 block is block 2 (the 4x4 block that holds (xW, yW))
                b8 = (yW // 8) * 2 + xW // 8
                n3 = which == "A" and not rec.fld and m.fld and (y // 8) * 2 + x // 8 == 2 and not mut("pm8_n_always_1")
                bx, by = blk_xy(b8 * 4 + (2 if which == "B" else 3 if n3 else 1))
                return m.ipm[by * 4 + bx]
            return m.ipm[(yW // 4) * 4 + xW // 4]
        ma, mb = mode(a, "A"), mode(b, "B")
        if ma is None or mb is None:
            return 2
        return min(ma, mb)

    def avail_set(self, rec, x, y):
        """Which neighbouring sample groups of the block at (x, y) can be used for intra prediction: 'T' above, 'L' left, 'D'
        above-left (constrained_intra_pred: only intra macroblocks)."""
        s = ""
        for k, (dx, dy) in (("T", (0, -1)), ("L", (-1, 0)), ("D", (-1, -1))):
            n = self.nb(rec.addr, x + dx, y + dy)
            if n is not None and (not self.cip or self.recs[n[0]].intra):
                s += k
        return s

    def intra_modes(self, rec, is8):
        n = 4 if is8 else 16
        out = ""
        for i in range(n):
            if is8:
                x, y = (i & 1) * 8, (i >> 1) * 8
            else:
                bx, by = blk_xy(i)
                x, y = bx * 4, by * 4
            pred = self.ipm_pred(rec, x, y, is8)
            av = self.avail_set(rec, x, y)

            def rd():
                if self.cabac:
                    if self.dec(68, 0.5):
                        return "p"
                    bins = [self.dec(69) for _ in range(3)]
                    return str(bins[0] + 2 * bins[1] + 4 * bins[2])
                if self.eng.u(1, 0.5):
                    return "p"
                return str(self.eng.u(3))

            def actual(s):
                return pred if s == "p" else (int(s) if int(s) < pred else int(s) + 1)

            def ok(s):
                m = actual(s)
                if self.cip:
                    return m == 2
                return all(c in av for c in INTRA_ALLOWED_4[m])
            s = self.pick(rd, ok)
            out += s
            m = actual(s)
            rec.pm.append(m)
            cells = [(y // 4 + dy) * 4 + x // 4 + dx for dy in range(2 if is8 else 1) for dx in range(2 if is8 else 1)]
            for c in cells:
                rec.ipm[c] = m
        return out

    # ------------------------------------------------------------ coded_block_pattern, mb_qp_delta
    def cbp(self, rec, intra):
        if not self.cabac:
            code = self.eng.ue([6] + [2] * 47)
            if code > 47:
                raise Unusable("coded_block_pattern codeNum > 47")
            v = (CR.CBP_INTRA if intra else CR.CBP_INTER)[code]
            return v & 15, v >> 4
        luma = 0
        for b8 in range(4):
            x, y = (b8 & 1) * 8, (b8 >> 1) * 8

            def cond(xN, yN, inner_bit):
                if xN >= 0 and yN >= 0:
                    return 0 if (luma >> inner_bit) & 1 else 1
                n = self.nb(rec.addr, xN, yN)
                if n is None:
                    return 0
                m = self.recs[n[0]]
                if m.pcm:
                    return 0
                if mut("cbp_luma_naive_b8"):
                    bn = b8 + (1 if xN < 0 else 2)
                else:
                    bn = (n[2] // 8) * 2 + n[1] // 8
                return 0 if (m.cbp_l >> bn) & 1 else 1
            ca = cond(x - 1, y, b8 - 1) if x else cond(-1, y, 0)
            cb = cond(x, y - 1, b8 - 2) if y else cond(x, -1, 0)
            luma |= self.dec(73 + ca + 2 * cb) << b8
        A, B = self.mbab(rec)

        def cc(n, two):
            if n is None:
                return 0
            if n.pcm:
                return 1
            return 1 if (n.cbp_c == 2 if two else n.cbp_c != 0) else 0
        chroma = 0
        if self.dec(77 + cc(A, False) + 2 * cc(B, False)):
            chroma = 1 + self.dec(77 + 4 + cc(A, True) + 2 * cc(B, True))
        return luma, chroma

    def qp_delta(self, rec):
        if not self.cabac:
            return self.eng.se([10, 10, 8, 6, 4, 3, 2, 1, 1])
        if mut("qpd_ctx_left"):
            a = self.nb(rec.addr, -1, 0)
            pn = bool(a and self.recs[a[0]].qpd_present and self.recs[a[0]].qpd != 0)
        else:
            pn = self.prev_nz
        k = 0
        while True:
            c = 60 + ((1 if pn else 0) if k == 0 else (2 if k == 1 else 3))
            if not self.dec(c, 0.35):
                break
            k += 1
            if k > 120:
                raise Unusable("mb_qp_delta unary code does not end")
        if k == 0:
            return 0
        v = (k + 1) // 2
        return v if (k & 1) == 1 else -v

    # ------------------------------------------------------------ inter syntax
    def sub_type(self):
        if not self.cabac:
            v = self.eng.ue([5, 3, 3, 2] if self.t == "P" else [4] + [2] * 12)
            if v > (3 if self.t == "P" else 12):
                raise Unusable("sub_mb_type out of range")
            return v
        if self.t == "P":
            if self.dec(21):
                return 0
            if not self.dec(22):
                return 1
            return 2 if self.dec(23) else 3
        if not self.dec(36):
            return 0
        if not self.dec(37):
            return 1 + self.dec(39)
        t = 3
        if self.dec(38):
            if self.dec(39):
                return 11 + self.dec(39)
            t += 4
        hi = self.dec(39)
        lo = self.dec(39)
        return t + 2 * hi + lo

    def ref_idx(self, rec, lst, b8, nref_eff):
        x8, y8 = b8 & 1, b8 >> 1
        if not self.cabac:
            cmax = nref_eff - 1
            if mut("ref_range_not_doubled") and rec.fld:
                cmax = nref_eff // 2 - 1
            if cmax == 1:
                v = 1 - self.eng.u(1)
            else:
                v = self.eng.ue([6, 4, 3, 2, 1] if cmax > 0 else None)
            if v > nref_eff - 1:
                raise Unusable("ref_idx larger than the allowed range")
            return v

        def cond(xN, yN):
            n = self.nb(rec.addr, xN, yN)
            if n is None:
                return 0
            m = self.recs[n[0]]
            b = (n[2] // 8) * 2 + n[1] // 8
            if m.intra or m.direct or m.dir8[b] or not m.used[lst][b]:
                return 0
            thr = 0
            if not rec.fld and m.fld and not mut("ref_ctx_no_adjust"):
                thr = 1
            if rec.fld and not m.fld and mut("ref_ctx_adjust_wrong"):
                thr = 1
            return 1 if m.ref[lst][b] > thr else 0
        inc = cond(8 * x8 - 1, 8 * y8) + 2 * cond(8 * x8, 8 * y8 - 1)
        if not self.dec(54 + inc, 0.4):
            return 0
        if not self.dec(54 + 4, 0.4):
            return 1
        n = 2
        while self.dec(54 + 5, 0.4):
            n += 1
            if n > 64:
                raise Unusable("ref_idx does not end")
        return n

    def mvd_comp(self, rec, lst, x4, y4, comp):
        if not self.cabac:
            return self.eng.se([8, 6, 4, 3, 2, 1, 1, 1])

        def val(xN, yN):
            n = self.nb(rec.addr, xN, yN)
            if n is None:
                return 0
            m = self.recs[n[0]]
            v = m.mvd[lst][(n[2] // 4) * 4 + n[1] // 4][comp]
            if comp == 1 or mut("mvd_scale_x_too"):
                cf, nf = (not rec.fld), bool(m.fld)
                if mut("mvd_scale_swapped"):
                    if cf and nf:
                        pass
                    if (not rec.fld) and m.fld:
                        v = v // 2
                    elif rec.fld and not m.fld:
                        v = v * 2
                elif not mut("mvd_scale_none"):
                    if (not rec.fld) and m.fld:
                        v = v * 2
                    elif rec.fld and not m.fld:
                        v = v // 2
            return v
        s = val(4 * x4 - 1, 4 * y4) + val(4 * x4, 4 * y4 - 1)
        base = 40 if comp == 0 else 47
        if not self.dec(base + (0 if s < 3 else 1 if s <= 32 else 2), 0.35):
            return 0
        n = 1
        incs = [3, 4, 5, 6, 6, 6, 6, 6]
        while n < 9 and self.dec(base + incs[n - 1], 0.4):
            n += 1
        if n >= 9:
            k = 3
            while self.eng.bypass(0.15):
                n += 1 << k
                k += 1
                if k > 24:
                    raise Unusable("mvd suffix does not end")
            while k:
                k -= 1
                n += self.eng.bypass() << k
        neg = self.eng.bypass()
        return -n if neg else n

    def inter_pred(self, rec, mbt):
        info = self.info
        nref = [info.num_ref_idx_l0_active, info.num_ref_idx_l1_active if self.t == "B" else 1]
        mult = 2 if rec.fld else 1
        ref_items, mvd_items = [], []
        rec.noSub = True
        p8x8 = 3 if self.t == "P" else 22
        if mbt in (p8x8, 4 if self.t == "P" else -1):
            tab = P_SUB if self.t == "P" else B_SUB
            subs = [self.pick(self.sub_type) for _ in range(4)]
            rec.sub = subs
            for b8 in range(4):
                shape, mode = tab[subs[b8]]
                if mode == "Direct":
                    rec.dir8[b8] = True
                    if not info.sps.get("direct_8x8_inference_flag", 0):
                        rec.noSub = False
                    continue
                if len(shape) > 1:
                    rec.noSub = False
                ref_items.append((b8, [b8], mode))
                ox, oy = (b8 & 1) * 2, (b8 >> 1) * 2
                for (x, y, w, h) in shape:
                    mvd_items.append(((ox + x, oy + y, w, h), mode))
        else:
            rects, modes = (P_TYPES if self.t == "P" else B_TYPES)[mbt]
            for rect, mode in zip(rects, modes):
                x, y, w, h = rect
                cov = [yy * 2 + xx for yy in range(y // 2, (y + h + 1) // 2) for xx in range(x // 2, (x + w + 1) // 2)]
                ref_items.append(((y // 2) * 2 + x // 2, cov, mode))
                mvd_items.append((rect, mode))
        uses = lambda mode, lst: mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi"))
        ref0 = self.t == "P" and mbt == 4
        for lst in (0, 1):
            for (b8, cov, mode) in ref_items:
                if not uses(mode, lst):
                    continue
                for bb in cov:
                    rec.used[lst][bb] = True
                nr = nref[lst] * mult
                present = (nref[lst] > 1 or (rec.fld and not mut("ref_present_ignores_field"))) and not ref0
                if present:
                    v = self.pick(lambda: self.ref_idx(rec, lst, b8, nr), lambda v: v < nr)
                else:
                    v = 0
                for bb in cov:
                    rec.ref[lst][bb] = v
                rec.refl[lst].append(v)
        for lst in (0, 1):
            for (rect, mode) in mvd_items:
                if not uses(mode, lst):
                    continue
                x, y, w, h = rect
                vx = self.pick(lambda: self.mvd_comp(rec, lst, x, y, 0), lambda v: abs(v) <= self.pr("mvd_max", 24))
                vy = self.pick(lambda: self.mvd_comp(rec, lst, x, y, 1), lambda v: abs(v) <= self.pr("mvd_max", 24))
                for yy in range(y, y + h):
                    for xx in range(x, x + w):
                        rec.mvd[lst][yy * 4 + xx] = (abs(vx), abs(vy))
                rec.mvdl[lst].append((vx, vy))

    # ------------------------------------------------------------ residual
    def sig_bases(self, rec, cat):
        fld = rec.fld and not mut("sig_ctx_frame_in_field")
        if cat == 5:
            return (436, 451, 426) if fld else (402, 417, 426)
        return ((277 if fld else 105) + SIG_OFF[cat], (338 if fld else 166) + SIG_OFF[cat], 227 + ABS_OFF[cat])

    def resid_block_cabac(self, rec, cat, cbf_inc, nmax):
        if cat != 5:
            if not self.dec(85 + CBF_OFF[cat] + cbf_inc, 0.6):
                return None
        sigb, lastb, absb = self.sig_bases(rec, cat)
        s8 = SIG8F if (rec.fld and not mut("sig8_table_frame_in_field")) else SIG8
        sig = []
        i = 0
        while i < nmax - 1:
            if cat == 5:
                sinc, linc = s8[i], LAST8[i]
            else:
                sinc = linc = min(i, 2) if cat == 3 else i
            if self.dec(sigb + sinc, 0.25):
                sig.append(i)
                if self.dec(lastb + linc, 0.3):
                    break
            i += 1
        else:
            sig.append(nmax - 1)
        eq1 = gt1 = 0
        levels = []
        for _ in sig:
            if not self.dec(absb + (0 if gt1 else min(4, 1 + eq1)), 0.3):
                a = 0
                eq1 += 1
            else:
                inc = 5 + min(4 - (1 if cat == 3 else 0), gt1)
                n = 1
                while n < 14 and self.dec(absb + inc, 0.35):
                    n += 1
                a = n
                if n >= 14:
                    k = 0
                    while self.eng.bypass(0.25):
                        a += 1 << k
                        k += 1
                        if k > 24:
                            raise Unusable("coeff_abs_level_minus1 suffix does not end")
                    while k:
                        k -= 1
                        a += self.eng.bypass() << k
                gt1 += 1
            sgn = self.eng.bypass()
            levels.append(-(a + 1) if sgn else a + 1)
        coeffs = [0] * nmax
        for pos, v in zip(reversed(sig), levels):
            coeffs[pos] = v
        return coeffs

    def blk_ok(self, c):
        if c is None:
            return True
        nz = [v for v in c if v]
        if any(abs(v) > self.pr("lvl_max", 30) for v in nz):
            return False
        return 0.5 ** max(0, len(nz) - 3) if len(nz) > 3 else True

    def cbf_cond(self, n, cur_intra, flag):
        """condTermFlagN of a block whose neighbour is in macroblock n (a record, or None = not available)."""
        if n is None:
            return 1 if cur_intra else 0
        if n.pcm:
            return 1
        if n.skip:
            return 0
        f = flag(n)
        return 0 if f is None else f

    def nbmb(self, rec, xN, yN, maxW=16, maxH=16, naive="cbf_naive"):
        if mut(naive):
            n = self.nb_naive(rec.addr, xN, yN, maxW, maxH) if (xN < 0 or yN < 0) else (rec.addr, xN, yN)
        else:
            n = self.nb(rec.addr, xN, yN, maxW, maxH)
        return n

    def residual(self, rec):
        intra = rec.intra
        rec.blocks = []
        cbp_l, cbp_c = rec.cbp_l, rec.cbp_c

        def put(kind, idx, maxnum, coeffs):
            rec.blocks.append(dict(kind=kind, idx=idx, maxnum=maxnum, coeffs=coeffs))
        if not self.cabac:
            return self.residual_cavlc(rec, put)
        A, B = self.mbab(rec)
        if rec.i16:
            ca = self.cbf_cond(A, intra, lambda n: n.dccbf if n.i16 else None)
            cb = self.cbf_cond(B, intra, lambda n: n.dccbf if n.i16 else None)
            c = self.pick(lambda: self.resid_block_cabac(rec, 0, ca + 2 * cb, 16), self.blk_ok)
            rec.dccbf = int(c is not None)
            put("i16dc", 0, 16, c)

        def lcond(x4, y4, dx, dy):
            xN, yN = 4 * x4 + dx, 4 * y4 + dy
            if xN >= 0 and yN >= 0:
                return rec.lcbf[(yN // 4) * 4 + xN // 4]
            n = self.nbmb(rec, xN, yN)
            if n is None:
                return self.cbf_cond(None, intra, None)
            m = self.recs[n[0]]
            cell = (n[2] // 4) * 4 + n[1] // 4
            b8 = (n[2] // 8) * 2 + n[1] // 8
            return self.cbf_cond(m, intra, lambda mm: mm.lcbf[cell] if (mm.cbp_l >> b8) & 1 else None)
        for b8 in range(4):
            if not (cbp_l >> b8) & 1:
                continue
            if rec.t8:
                c = self.pick(lambda: self.resid_block_cabac(rec, 5, 0, 64), self.blk_ok)
                for k in range(4):
                    rec.lcbf[(b8 >> 1) * 8 + (b8 & 1) * 2 + (k & 1) + 4 * (k >> 1)] = 1
                put("luma8", b8, 64, c)
                continue
            for b4 in range(4):
                idx = b8 * 4 + b4
                x4, y4 = blk_xy(idx)
                ca, cb = lcond(x4, y4, -1, 0), lcond(x4, y4, 0, -1)
                nm = 15 if rec.i16 else 16
                c = self.pick(lambda: self.resid_block_cabac(rec, 1 if rec.i16 else 2, ca + 2 * cb, nm), self.blk_ok)
                rec.lcbf[y4 * 4 + x4] = int(c is not None)
                put("i16ac" if rec.i16 else "luma", idx, nm, c)
        if cbp_c:
            for c_ in range(2):
                ca = self.cbf_cond(A, intra, lambda n: n.cdc[c_] if n.cbp_c else None)
                cb = self.cbf_cond(B, intra, lambda n: n.cdc[c_] if n.cbp_c else None)
                co = self.pick(lambda: self.resid_block_cabac(rec, 3, ca + 2 * cb, 4), self.blk_ok)
                rec.cdc[c_] = int(co is not None)
                put(("cbdc", "crdc")[c_], 0, 4, co)
        if cbp_c == 2:
            for c_ in range(2):
                for b in range(4):
                    x, y = b & 1, b >> 1

                    def ccond(dx, dy):
                        xN, yN = 4 * x + dx, 4 * y + dy
                        if xN >= 0 and yN >= 0:
                            return rec.cac[c_][(yN // 4) * 2 + xN // 4]
                        n = self.nbmb(rec, xN, yN, 8, 8)
                        if n is None:
                            return self.cbf_cond(None, intra, None)
                        m = self.recs[n[0]]
                        cell = (n[2] // 4) * 2 + n[1] // 4
                        return self.cbf_cond(m, intra, lambda mm: mm.cac[c_][cell] if mm.cbp_c == 2 else None)
                    ca, cb = ccond(-1, 0), ccond(0, -1)
                    co = self.pick(lambda: self.resid_block_cabac(rec, 4, ca + 2 * cb, 15), self.blk_ok)
                    rec.cac[c_][b] = int(co is not None)
                    put(("cbac", "crac")[c_], b, 15, co)

    # ---- CAVLC residual (nC from 6.4.12.2 neighbours)
    def nc_of(self, rec, x4, y4, chroma=None):
        """nC (9.2.1) of the 4x4 block at (x4, y4) (luma) or chroma 4x4 block of plane `chroma`."""
        mw = 8 if chroma is not None else 16
        res = []
        for dx, dy in ((-1, 0), (0, -1)):
            xN, yN = 4 * x4 + dx, 4 * y4 + dy
            if xN >= 0 and yN >= 0:
                m, cx, cy = rec, xN, yN
            else:
                n = self.nbmb(rec, xN, yN, mw, mw, "nc_naive")
                if n is None:
                    res.append(None)
                    continue
                m, cx, cy = self.recs[n[0]], n[1], n[2]
            if chroma is None:
                res.append(m.nzc[(cy // 4) * 4 + cx // 4])
            else:
                res.append(m.nzcc[chroma][(cy // 4) * 2 + cx // 4])
        na, nb = res
        if na is not None and nb is not None:
            return (na + nb + 1) >> 1
        return na if na is not None else (nb if nb is not None else 0)

    def cavlc_block(self, nc, maxnum):
        tc, t1, coeffs = CRes.residual_block(self.eng, nc, maxnum)
        return tc, coeffs

    def residual_cavlc(self, rec, put):
        cbp_l, cbp_c = rec.cbp_l, rec.cbp_c
        ok = lambda r: self.blk_ok(r[1])
        if rec.i16:
            _, c = self.pick(lambda: self.cavlc_block(self.nc_of(rec, 0, 0), 16), ok)
            put("i16dc", 0, 16, c)
        for b8 in range(4):
            if not (cbp_l >> b8) & 1:
                continue
            ox, oy = (b8 & 1) * 2, (b8 >> 1) * 2
            if rec.t8:
                full = [0] * 64
                for i in range(4):
                    x4, y4 = ox + (i & 1), oy + (i >> 1)
                    nc = self.nc_of(rec, x4, y4)
                    tc, c = self.pick(lambda: self.cavlc_block(nc, 16), lambda r: all(abs(v) <= self.pr("lvl_max", 30) for v in r[1]) and r[0] <= 5)
                    rec.nzc[y4 * 4 + x4] = tc
                    for k, v in enumerate(c):
                        full[4 * k + i] = v
                put("luma8", b8, 64, full)
                continue
            for b4 in range(4):
                idx = b8 * 4 + b4
                x4, y4 = blk_xy(idx)
                nm = 15 if rec.i16 else 16
                nc = self.nc_of(rec, x4, y4)
                tc, c = self.pick(lambda: self.cavlc_block(nc, nm), ok)
                rec.nzc[y4 * 4 + x4] = tc
                put("i16ac" if rec.i16 else "luma", idx, nm, c)
        if cbp_c:
            for c_ in range(2):
                _, co = self.pick(lambda: self.cavlc_block(-1, 4), ok)
                put(("cbdc", "crdc")[c_], 0, 4, co)
        if cbp_c == 2:
            for c_ in range(2):
                for b in range(4):
                    nc = self.nc_of(rec, b & 1, b >> 1, chroma=c_)
                    tc, co = self.pick(lambda: self.cavlc_block(nc, 15), ok)
                    rec.nzcc[c_][b] = tc
                    put(("cbac", "crac")[c_], b, 15, co)

    # ------------------------------------------------------------ one macroblock
    def macroblock(self, rec):
        info = self.info
        A, B = self.mbab(rec, "mbtype_naive")
        eng = self.eng
        t8m = info.transform_8x8_mode

        def i16_ok(v):
            if v == 0 or v == 25:
                return True
            mode = (v - 1) % 4
            if self.cip:
                return mode == 2
            av = self.avail_set(rec, 0, 0)
            return all(c in av for c in ("T", "L", "", "TLD")[mode])

        def mbt_ok(r):
            intra, v = r
            if not intra:
                return v != 4 or 0.1
            return i16_ok(v)
        intra, v = self.pick(lambda: self.mb_type(rec, A, B), mbt_ok)
        if intra:
            ival, inter = v, None
            rec.mbt = ival if self.t == "I" else (5 + ival if self.t == "P" else 23 + ival)
        else:
            ival, inter = None, v
            rec.mbt = v
        rec.intra = intra
        if intra:
            if ival == 25:
                rec.pcm = True
                rec.cbp_l, rec.cbp_c = 15, 2
                rec.lcbf = [1] * 16
                rec.dccbf = 1
                rec.cdc = [1, 1]
                rec.cac = [[1] * 4, [1] * 4]
                rec.nzc = [16] * 16
                rec.nzcc = [[16] * 4, [16] * 4]
                rec.pcm_samples = eng.pcm()
                rec.qp = self.qp
                self.prev_nz = False
                return
            if ival == 0:
                rec.i_nxn = True
                if t8m:
                    rec.t8 = self.pick(lambda: self.t8_flag(rec, A, B))
                rec.modes = self.intra_modes(rec, bool(rec.t8))
            else:
                rec.i16 = True
                rec.ipm = [2] * 16
            rec.cpred = self.pick(lambda: self.chroma_pred(rec, A, B), self.cpred_ok(rec))
            if rec.i_nxn:
                rec.cbp_l, rec.cbp_c = self.pick(lambda: self.cbp(rec, True))
            else:
                rec.cbp_l = 15 if ival >= 13 else 0
                rec.cbp_c = ((ival - 1) // 4) % 3
            has_qpd = rec.i16 or rec.cbp_l or rec.cbp_c
        else:
            if self.t == "B" and inter == 0:
                rec.direct = True
                rec.noSub = bool(info.sps.get("direct_8x8_inference_flag", 0))
            else:
                self.inter_pred(rec, inter)
            rec.cbp_l, rec.cbp_c = self.pick(lambda: self.cbp(rec, False))
            if t8m and rec.cbp_l and rec.noSub:
                rec.t8 = self.pick(lambda: self.t8_flag(rec, A, B))
            has_qpd = bool(rec.cbp_l or rec.cbp_c)
        qpd = 0
        if has_qpd:
            lo, hi = self.pr("qp_lo", 12), self.pr("qp_hi", 44)
            qpd = self.pick(lambda: self.qp_delta(rec), lambda d: lo <= (self.qp + d + 52) % 52 <= hi and -26 <= d <= 25)
            rec.qpd, rec.qpd_present = qpd, True
            base = self.qp
            if mut("qp_pred_left"):
                a = self.nb(rec.addr, -1, 0)
                base = self.recs[a[0]].qp if a else self.qp
            self.qp = (base + qpd + 52) % 52
        rec.qp = self.qp
        if has_qpd:
            self.residual(rec)
        self.prev_nz = has_qpd and qpd != 0

    def cpred_ok(self, rec):
        if self.cip:
            return lambda v: v == 0
        av = self.avail_set(rec, 0, 0)
        return lambda v: all(c in av for c in ("", "L", "T", "TLD")[v])

    # ------------------------------------------------------------ slice_data()
    def new_mb(self, addr):
        rec = Mb(addr)
        self.recs[addr] = rec
        rec.fld = self.infer_fld(addr // 2) if addr % 2 == 0 else self.recs[addr - 1].fld
        return rec

    def skipped(self, rec):
        rec.skip = True
        rec.direct = self.t == "B"
        rec.qp = self.qp
        self.prev_nz = False

    def end_of_pic(self, addr):
        if addr >= self.info.pic_size_in_mbs:
            raise Unusable("no end of slice before the picture ends")

    def parse(self):
        info = self.info
        order = []
        addr = self.fm
        status = "complete"
        try:
            if self.cabac:
                while True:
                    self.end_of_pic(addr)
                    top = addr % 2 == 0
                    rec = self.new_mb(addr)
                    order.append(rec)
                    sk = False
                    if self.t != "I":
                        saved = rec.fld
                        if mut("skip_ctx_cur_frame") and (top or self.recs[addr - 1].skip):
                            rec.fld = 0
                        if mut("bottom_skip_peek_order") and not top and self.recs[addr - 1].skip:
                            f = self.field_flag_cabac(rec)
                            rec.fld = f
                            saved = f
                        sk = bool(self.skip_flag(rec))
                        rec.fld = saved
                    if sk:
                        self.skipped(rec)
                    else:
                        if top or self.recs[addr - 1].skip or mut("bottom_flag_always"):
                            f = self.pick(lambda: self.field_flag_cabac(rec))
                            rec.fld = f
                            if not top and not mut("top_flag_not_propagated"):
                                self.recs[addr - 1].fld = f
                        self.macroblock(rec)
                    last = addr + 1 >= (self.end_addr if self.gen else 1 << 60)
                    if top and not mut("eos_after_top"):
                        pass
                    else:
                        rec.eos = self.eng.term_end(last)
                        if rec.eos:
                            break
                    addr += 1
            else:
                more = True
                prev_skipped = False
                while more:
                    if self.t != "I":
                        run = self.pick(lambda: self.eng.ue(self.pr("run_w", [6, 3, 2, 2, 1, 1, 1, 1])),
                                        lambda r: addr + r <= (self.end_addr if self.gen else 1 << 60))
                        prev_skipped = run > 0
                        for _ in range(run):
                            self.end_of_pic(addr)
                            rec = self.new_mb(addr)
                            order.append(rec)
                            self.skipped(rec)
                            addr += 1
                        if run > 0:
                            more = self.eng.more_data(addr)
                    if more:
                        self.end_of_pic(addr)
                        top = addr % 2 == 0
                        rec = self.new_mb(addr)
                        order.append(rec)
                        want = top or prev_skipped
                        if mut("cavlc_bottom_flag_missing") and not top:
                            want = False
                        if mut("cavlc_top_flag_after_skip_missing") and top and prev_skipped:
                            want = False
                        if want:
                            f = self.eng.u(1, self.pr("p_field", 0.5))
                            rec.fld = f
                            if not top and not mut("top_flag_not_propagated"):
                                self.recs[addr - 1].fld = f
                        self.macroblock(rec)
                        addr += 1
                        more = self.eng.more_data(addr)
        except (Unusable, CR.ParseError, KeyError, IndexError) as e:
            if self.gen:
                raise
            status = f"parse failed: {e}"
        if status == "complete" and not self.gen:
            status = self.end_check(order)
        return order, status

    def end_check(self, order):
        """The proof of correctness: the slice ends exactly where the encoder ended it."""
        info = self.info
        if not order:
            return "empty slice"
        if len(order) % 2:
            return "slice ends in the middle of a macroblock pair"
        if self.cabac:
            bits = "".join(f"{b:08b}" for b in info.nal)
            stop = bits.rfind("1")
            pos = self.eng.pos
            if not (pos - 1 <= stop <= pos + 6):
                return f"end_of_slice_flag = 1 at macroblock {order[-1].addr} but the arithmetic decoder stopped at bit {pos} while the rbsp_stop_one_bit is bit {stop}"
        else:
            if self.eng.br.pos != self.eng.br.stop_bit:
                return f"more_rbsp_data() false at bit {self.eng.br.pos}, rbsp_stop_one_bit is bit {self.eng.br.stop_bit}"
        return "complete"


# ================================================================ driver and text formats
def prepare(infos):
    for s in infos:
        h, p = s.hdr, s.pps
        if h.get("num_ref_idx_active_override_flag", 0):
            s.num_ref_idx_l0_active = h["num_ref_idx_l0_active_minus1"] + 1
            s.num_ref_idx_l1_active = h.get("num_ref_idx_l1_active_minus1", 0) + 1
        else:
            s.num_ref_idx_l0_active = p.get("num_ref_idx_l0_default_active_minus1", 0) + 1
            s.num_ref_idx_l1_active = p.get("num_ref_idx_l1_default_active_minus1", 0) + 1
    return infos


def usable(info):
    s, h, p = info.sps, info.hdr, info.pps
    if info.slice_type in (3, 4):
        return "SP/SI slice"
    if h.get("field_pic_flag", 0) or not s.get("mb_adaptive_frame_field_flag", 0) or s.get("frame_mbs_only_flag", 1):
        return "not an MBAFF frame picture"
    if (s.get("chroma_format_idc", 1) != 1 or s.get("bit_depth_luma_minus8", 0) or s.get("bit_depth_chroma_minus8", 0)
            or s.get("separate_colour_plane_flag", 0)):
        return "not 8-bit 4:2:0"
    if p.get("num_slice_groups_minus1", 0):
        return "slice groups (FMO)"
    return None


def parse_all(infos):
    """-> list of (info, mbs in decoding order, status) for every usable slice."""
    out = []
    for info in infos:
        if usable(info) is not None:
            continue
        if mut("mbaff_slice_start"):
            info.first_mb //= 2
        sl = Slice(info)
        mbs, status = sl.parse()
        if mut("mbaff_slice_start"):
            info.first_mb *= 2
        out.append((info, mbs, status))
    return out


def coef_string(blocks):
    out = []
    for b in blocks:
        off = 1 if b["maxnum"] == 15 else 0
        nz = [f"{k + off}={v}" for k, v in enumerate(b["coeffs"] or []) if v]
        out.append(f"{b['kind']}{b['idx']}:" + (",".join(nz) if nz else "-"))
    return ";".join(out) or "-"


def mb_fields(rec):
    lst = lambda v: ",".join(map(str, v)) or "-"
    mvs = lambda v: ",".join(f"{x}:{y}" for x, y in v) or "-"
    plain = rec.skip or rec.pcm
    return [("pair", str(rec.addr // 2)), ("pos", "T" if rec.addr % 2 == 0 else "B"), ("fld", str(rec.fld)),
            ("mbt", "skip" if rec.skip else str(rec.mbt)),
            ("t8", str(rec.t8) if not rec.skip and not rec.pcm else "-"),
            ("cpred", str(rec.cpred) if rec.intra and not rec.pcm else "-"),
            ("modes", rec.modes if rec.i_nxn else "-"),
            ("pm", lst(rec.pm) if rec.i_nxn else "-"),
            ("sub", lst(rec.sub or [])),
            ("refidx", lst(rec.refl[0])), ("refidx1", lst(rec.refl[1])),
            ("mvd", mvs(rec.mvdl[0])), ("mvd1", mvs(rec.mvdl[1])),
            ("cbp", str(rec.cbp_l + 16 * rec.cbp_c) if not plain else "-"),
            ("qpd", str(rec.qpd) if rec.qpd_present else "-"),
            ("qp", str(rec.qp)), ("coef", coef_string(rec.blocks)),
            ("eos", "-" if rec.eos is None else str(rec.eos))]


def slice_letter(info):
    return {0: "P", 1: "B", 2: "I", 3: "P", 4: "I"}[info.slice_type]


def render_dump(parsed):
    lines = []
    for info, recs, status in parsed:
        lines.append(f"SLICE {info.index} type={slice_letter(info)} first_mb={info.first_mb}")
        for r in recs:
            lines.append(f"MB {info.index} {r.addr} " + " ".join(f"{k}={v}" for k, v in mb_fields(r)))
        lines.append(f"END {info.index} mbs={len(recs)}")
    return "\n".join(lines) + "\n"
