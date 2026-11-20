"""Chapter 8.2 reference: a complete CABAC macroblock-layer parser for I / P / B slices (8-bit 4:2:0, frame pictures,
transform_8x8_mode_flag = 0, no MBAFF), written for the harness.

Two modes share every chapter-level rule (the functions below that decode one syntax element):

  truth  (reader=False)  parses EVERYTHING: mb_type, sub_mb_type, ref_idx, mvd, coded_block_pattern, mb_qp_delta, the
                         residual (coded_block_flag / significant_coeff_flag / last_significant_coeff_flag /
                         coeff_abs_level_minus1 / sign).  The parse must end with end_of_slice_flag = 1 on the last
                         macroblock and zero padding after the rbsp_stop_one_bit: that is the proof of correctness.
                         It also yields, for every macroblock the 8.2 reader cannot finish, a HINT (engine state).
  reader (reader=True)   what a reader of Chapter 8.2 does: decode only this chapter's syntax elements and, where
                         later chapters' syntax would follow, take the engine state from a HINT.
                         This mode is what the harness's --self-test corrupts with MUTANT rules.

Dump / hint grammar: Readme section 5.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cabac_ref as R  # noqa: E402

MUTANT = None
MUTANTS = {   # name -> what is wrong (applied only in reader mode)
    "skip_ctx_only_A": "mb_skip_flag: ctxIdxInc uses neighbour A only",
    "skip_cond_inverted": "mb_skip_flag: condTermFlagN is 1 for a SKIPPED neighbour (instead of 0)",
    "skip_p_base24": "P slice: mb_skip_flag uses the B offset 24 instead of 11",
    "imbt_inc_2B": "I mb_type bin 0: ctxIdxInc = condTermFlagA + 2 * condTermFlagB",
    "imbt_pcm_cond0": "I mb_type bin 0: an I_PCM neighbour counts as 0 (should be 1: only I_NxN gives 0)",
    "imbt_luma_ctx": "I mb_type (I slice): the 'luma 15' bin uses ctxIdx 3+4 instead of 3+3",
    "imbt_mode_swapped": "I_16x16 prediction mode: the two bins are read least significant first",
    "imbt_suffix_I_ctx": "intra suffix in P/B slices uses the I-slice contexts (offset 3) instead of 17 / 32",
    "ptype_prefix_ctx15": "P mb_type: the intra prefix bin uses ctxIdx 15 instead of 14",
    "ptype_b2_ctx": "P mb_type: bin 2 always uses ctxIdx 16 (ignores b1)",
    "btype_cond_skip1": "B mb_type bin 0: a B_Skip / B_Direct_16x16 neighbour counts as 1 (should be 0)",
    "btype_cond_unavail1": "B mb_type bin 0: an unavailable neighbour counts as 1 (should be 0)",
    "prev_flag_ctx": "prev_intra4x4_pred_mode_flag uses ctxIdx 69 instead of 68",
    "rem_msb_first": "rem_intra4x4_pred_mode: the 3 bins are read most significant first",
    "cpred_pcm_counts": "intra_chroma_pred_mode: an I_PCM neighbour counts as 1 (should be 0)",
    "cpred_inter_counts": "intra_chroma_pred_mode: an inter neighbour counts as 1 (should be 0)",
    "cpred_bin1_ctx": "intra_chroma_pred_mode: bins 1 and 2 use the neighbour-dependent ctxIdx 64+inc instead of 64+3",
    "cpred_inc_2B": "intra_chroma_pred_mode: ctxIdxInc = condTermFlagA + 2 * condTermFlagB",
    "cbp_luma_unavail1": "coded_block_pattern luma: an unavailable neighbour gives condTermFlag 1 (should be 0)",
    "cbp_luma_skip0": "coded_block_pattern luma: a skipped neighbour gives condTermFlag 0 (should be 1)",
    "cbp_luma_pcm1": "coded_block_pattern luma: an I_PCM neighbour gives condTermFlag 1 (should be 0)",
    "cbp_luma_inner_ignored": "coded_block_pattern luma: neighbours inside the same macroblock are treated as 'bit set'",
    "cbp_luma_AB_swapped": "coded_block_pattern luma: ctxIdxInc = 2 * condTermFlagA + condTermFlagB",
    "cbp_chroma_bin1_inc": "coded_block_pattern chroma: bin 1 omits the +4 in its ctxIdxInc",
    "cbp_chroma_pcm0": "coded_block_pattern chroma: an I_PCM neighbour gives condTermFlag 0 (should be 1)",
    "cbp_chroma_bin1_nz": "coded_block_pattern chroma: bin 1 tests 'chroma != 0' instead of 'chroma == 2'",
    "cbp_chroma_skip1": "coded_block_pattern chroma: a skipped neighbour gives condTermFlag 1 (should be 0)",
    "qpd_prev_any": "mb_qp_delta bin 0: the previous macroblock counts even if it was skipped / I_PCM / had no mb_qp_delta",
    "qpd_bin1_ctx": "mb_qp_delta: bin 1 uses ctxIdx 60+3 instead of 60+2",
    "qpd_sign_swapped": "mb_qp_delta: odd mapped values become negative, even ones positive",
    "qpd_no_wrap": "QP_Y is clipped to 0..51 instead of wrapping modulo 52",
    "qpd_not_reset": "the 'previous macroblock had a non-zero mb_qp_delta' memory is not reset at a slice start",
    "qp_not_reset": "QP_Y,PRED at a slice start is the last QP_Y of the previous slice instead of SliceQPY",
    "pcm_qp_zero": "after an I_PCM macroblock QP_Y becomes 0",
    "eos_skipped_after_skip": "no end_of_slice_flag is read after a skipped macroblock",
    "pcm_no_align": "after I_PCM the engine restarts without skipping the alignment bits",
    "pcm_samples_383": "I_PCM sample count: 383 bytes instead of 384",
    "hint_offset_ignored": "the HINT's codIOffset is ignored (the engine keeps its own)",
    "hint_bit_plus1": "the HINT's bit position is used one bit too late",
}


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

CBF_OFF = [0, 4, 8, 12, 16]
SIG_OFF = [0, 15, 29, 44, 47]
ABS_OFF = [0, 10, 20, 30, 39]


class MbRec:
    """What later macroblocks need to know about this one, plus the dump fields."""

    def __init__(self, addr):
        self.addr = addr
        self.skip = False
        self.pcm = False
        self.intra = False
        self.i_nxn = False
        self.i16 = False
        self.direct = False        # B_Skip or B_Direct_16x16 (for the B mb_type ctxIdxInc)
        self.mbt = None            # dump: spec mb_type value of the slice's own table, or None for skipped
        self.cbp_l = 0
        self.cbp_c = 0
        self.cpred = 0
        self.qpd = 0
        self.qpd_present = False
        self.modes = None
        self.qp = 0
        self.eos = 0
        self.lcbf = [0] * 16       # coded_block_flag of luma 4x4 blocks, raster order (y4 * 4 + x4)
        self.dccbf = 0
        self.cdc = [0, 0]
        self.cac = [[0] * 4, [0] * 4]
        self.mvd = [[(0, 0)] * 16, [(0, 0)] * 16]   # absolute mvd components per 4x4 block (raster), per list
        self.ref = [[0] * 4, [0] * 4]               # ref_idx per 8x8 block, 0 when not coded / direct
        self.hint_cbp = None       # engine state right before coded_block_pattern (inter macroblocks other than B_Direct_16x16)
        self.hint_end = None       # engine state after the macroblock's last residual bin (macroblocks with residual)


def blk_xy(idx):
    """luma4x4BlkIdx -> (x4, y4)."""
    return (idx % 2) + 2 * ((idx // 4) % 2), ((idx // 2) % 2) + 2 * (idx // 8)


class SliceParser:
    def __init__(self, info, reader=False, hints=None, prev_state=None):
        self.info = info
        self.reader = reader
        self.hints = hints or {}
        self.W = info.pic_width_in_mbs
        self.eng = None
        self.st = None
        self.recs = {}
        self.t = {0: "P", 1: "B", 2: "I", 3: "P"}[info.slice_type]
        self.carry = prev_state if prev_state is not None else {}     # for the 'not reset' mutants

    def mut(self, name):
        return self.reader and MUTANT == name

    # ------------------------------------------------------------ neighbours
    def nb(self, addr):
        fm = self.info.first_mb
        a = self.recs.get(addr - 1) if addr % self.W and addr - 1 >= fm else None
        b = self.recs.get(addr - self.W) if addr - self.W >= fm else None
        return a, b

    def dec(self, ctx):
        return self.eng.decision(self.st[ctx])

    # ------------------------------------------------------------ 9.3.3.1.1.1 mb_skip_flag
    def skip_flag(self, A, B):
        base = 11 if self.t == "P" else 24
        if self.mut("skip_p_base24"):
            base = 24

        def cond(n):
            if n is None:
                return 0
            return (1 if n.skip else 0) if self.mut("skip_cond_inverted") else (0 if n.skip else 1)
        inc = cond(A) + (0 if self.mut("skip_ctx_only_A") else cond(B))
        return self.dec(base + inc)

    # ------------------------------------------------------------ mb_type
    def i_type_body(self, base, islice):
        """Bins b1.. of an I mb_type after b0 = 1 (Table 9-36).  -> mb_type value 1..25."""
        if self.eng.terminate():
            return 25
        if islice:
            c_luma = base + (4 if self.mut("imbt_luma_ctx") else 3)
            c_cnz, c_c2, c_p1, c_p0 = base + 4, base + 5, base + 6, base + 7
        else:
            c_luma, c_cnz, c_c2, c_p1, c_p0 = base + 1, base + 2, base + 2, base + 3, base + 3
        luma = self.dec(c_luma)
        chroma = 0
        if self.dec(c_cnz):
            chroma = 1 + self.dec(c_c2)
        p1 = self.dec(c_p1)
        p0 = self.dec(c_p0)
        if self.mut("imbt_mode_swapped"):
            p1, p0 = p0, p1
        return 1 + 2 * p1 + p0 + 4 * chroma + 12 * luma

    def i_type_islice(self, A, B):
        def cond(n):
            if n is None:
                return 0
            if self.mut("imbt_pcm_cond0") and n.pcm:
                return 0
            return 0 if n.i_nxn else 1
        inc = cond(A) + (2 if self.mut("imbt_inc_2B") else 1) * cond(B)
        if self.dec(3 + inc) == 0:
            return 0
        return self.i_type_body(3, True)

    def i_suffix(self, base):
        if self.mut("imbt_suffix_I_ctx"):
            base = 3
            if self.dec(base) == 0:
                return 0
            return self.i_type_body(base, True)
        if self.dec(base) == 0:
            return 0
        return self.i_type_body(base, False)

    def p_type(self):
        """-> (is_intra, value): value = I mb_type for intra, else the P mb_type 0..3."""
        if self.dec(15 if self.mut("ptype_prefix_ctx15") else 14):
            return True, self.i_suffix(17)
        if self.dec(15) == 0:
            return False, (3 if self.dec(16) else 0)
        b2 = self.dec(16 if self.mut("ptype_b2_ctx") else 17)
        return False, (1 if b2 else 2)

    def b_type(self, A, B):
        def cond(n):
            if n is None:
                return 1 if self.mut("btype_cond_unavail1") else 0
            if n.direct:
                return 1 if self.mut("btype_cond_skip1") else 0
            return 1
        if self.dec(27 + cond(A) + cond(B)) == 0:
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

    # ------------------------------------------------------------ intra prediction syntax
    def intra_modes(self):
        s = ""
        for _ in range(16):
            if self.dec(69 if self.mut("prev_flag_ctx") else 68):
                s += "p"
            else:
                bins = [self.dec(69) for _ in range(3)]
                if self.mut("rem_msb_first"):
                    v = bins[0] * 4 + bins[1] * 2 + bins[2]
                else:
                    v = bins[0] + 2 * bins[1] + 4 * bins[2]
                s += str(v)
        return s

    def chroma_pred(self, A, B):
        def cond(n):
            if n is None:
                return 0
            if not n.intra:
                return 1 if self.mut("cpred_inter_counts") else 0
            if n.pcm:
                return 1 if self.mut("cpred_pcm_counts") else 0
            return 1 if n.cpred != 0 else 0
        inc = cond(A) + (2 if self.mut("cpred_inc_2B") else 1) * cond(B)
        if self.dec(64 + inc) == 0:
            return 0
        c = 64 + 3
        if self.mut("cpred_bin1_ctx"):
            c = 64 + inc
        if self.dec(c) == 0:
            return 1
        return 2 if self.dec(c) == 0 else 3

    # ------------------------------------------------------------ coded_block_pattern
    def cbp(self, A, B, rec):
        luma = 0
        for b8 in range(4):
            def cond(n, bit):
                if n is None:
                    return 1 if self.mut("cbp_luma_unavail1") else 0
                if n.pcm:
                    return 1 if self.mut("cbp_luma_pcm1") else 0
                if n.skip and self.mut("cbp_luma_skip0"):
                    return 0
                return 0 if (n.cbp_l >> bit) & 1 else 1
            if b8 & 1:     # A is inside this macroblock
                ca = 1 if self.mut("cbp_luma_inner_ignored") else (0 if (luma >> (b8 - 1)) & 1 else 1)
            else:
                ca = cond(A, b8 + 1)
            if b8 & 2:
                cb = 1 if self.mut("cbp_luma_inner_ignored") else (0 if (luma >> (b8 - 2)) & 1 else 1)
            else:
                cb = cond(B, b8 + 2)
            inc = (2 * ca + cb) if self.mut("cbp_luma_AB_swapped") else (ca + 2 * cb)
            luma |= self.dec(73 + inc) << b8

        def cc(n, two):
            if n is None:
                return 0
            if n.pcm:
                return 0 if self.mut("cbp_chroma_pcm0") else 1
            if n.skip and self.mut("cbp_chroma_skip1"):
                return 1
            return 1 if (n.cbp_c == 2 if two else n.cbp_c != 0) else 0
        inc = cc(A, False) + 2 * cc(B, False)
        chroma = 0
        if self.dec(77 + inc):
            nz = self.mut("cbp_chroma_bin1_nz")
            inc = (0 if self.mut("cbp_chroma_bin1_inc") else 4) + cc(A, not nz) + 2 * cc(B, not nz)
            chroma = 1 + self.dec(77 + inc)
        return luma, chroma

    # ------------------------------------------------------------ mb_qp_delta
    def qp_delta(self, prev_nz):
        inc0 = 1 if prev_nz else 0
        k = 0
        while True:
            c = 60 + (inc0 if k == 0 else (3 if self.mut("qpd_bin1_ctx") and k == 1 else (2 if k == 1 else 3)))
            if not self.dec(c):
                break
            k += 1
            if k > 120:
                raise Unusable("mb_qp_delta unary code does not end")
        odd_pos = not self.mut("qpd_sign_swapped")
        if k == 0:
            return 0
        v = (k + 1) // 2
        return v if ((k & 1) == 1) == odd_pos else -v

    # ------------------------------------------------------------ inter syntax (truth only: Chapter 8.4)
    def ref_idx(self, rec, A, B, lst, b8):
        x8, y8 = b8 & 1, b8 >> 1

        def refpos(n_rec, bb):
            return n_rec.ref[lst][bb] > 0 if n_rec is not None else False
        ca = refpos(rec, b8 - 1) if x8 else refpos(A, b8 + 1)
        cb = refpos(rec, b8 - 2) if y8 else refpos(B, b8 + 2)
        inc = int(ca) + 2 * int(cb)
        if not self.dec(54 + inc):
            return 0
        if not self.dec(54 + 4):
            return 1
        n = 2
        while self.dec(54 + 5):
            n += 1
            if n > 64:
                raise Unusable("ref_idx does not end")
        return n

    def mvd_comp(self, rec, A, B, lst, x4, y4, comp):
        def val(n_rec, bx, by):
            return n_rec.mvd[lst][by * 4 + bx][comp] if n_rec is not None else 0
        a = val(rec, x4 - 1, y4) if x4 else val(A, 3, y4)
        b = val(rec, x4, y4 - 1) if y4 else val(B, x4, 3)
        s = a + b
        base = 40 if comp == 0 else 47
        if not self.dec(base + (0 if s < 3 else 1 if s <= 32 else 2)):
            return 0
        n = 1
        incs = [3, 4, 5, 6, 6, 6, 6, 6]
        while n < 9 and self.dec(base + incs[n - 1]):
            n += 1
        if n >= 9:
            k = 3
            while self.eng.bypass():
                n += 1 << k
                k += 1
                if k > 24:
                    raise Unusable("mvd suffix does not end")
            while k:
                k -= 1
                n += self.eng.bypass() << k
        self.eng.bypass()          # sign
        return n

    def set_mvd(self, rec, lst, rect, ax, ay):
        x, y, w, h = rect
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                rec.mvd[lst][yy * 4 + xx] = (ax, ay)

    def sub_type(self):
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
        t += 2 * self.dec(39)
        t += self.dec(39)
        return t

    def inter_pred(self, rec, A, B, mbt):
        info = self.info
        nref = [info.num_ref_idx_l0_active, info.num_ref_idx_l1_active if self.t == "B" else 1]
        if mbt == (3 if self.t == "P" else 22):          # 8x8
            tab = P_SUB if self.t == "P" else B_SUB
            subs = [self.sub_type() for _ in range(4)]
            parts = [(tab[s][0], tab[s][1]) for s in subs]
            for lst in (0, 1):
                for b8 in range(4):
                    shape, mode = parts[b8]
                    if nref[lst] > 1 and mode != "Direct" and mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi")):
                        rec.ref[lst][b8] = self.ref_idx(rec, A, B, lst, b8)
            for lst in (0, 1):
                for b8 in range(4):
                    shape, mode = parts[b8]
                    if mode == "Direct" or mode not in (("L0", "Bi") if lst == 0 else ("L1", "Bi")):
                        continue
                    ox, oy = (b8 & 1) * 2, (b8 >> 1) * 2
                    for (x, y, w, h) in shape:
                        x4, y4 = ox + x, oy + y
                        ax = abs(self.mvd_comp(rec, A, B, lst, x4, y4, 0))
                        ay = abs(self.mvd_comp(rec, A, B, lst, x4, y4, 1))
                        self.set_mvd(rec, lst, (x4, y4, w, h), ax, ay)
            return
        rects, modes = (P_TYPES if self.t == "P" else B_TYPES)[mbt]
        for lst in (0, 1):
            for rect, mode in zip(rects, modes):
                if nref[lst] > 1 and mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi")):
                    b8 = (rect[1] // 2) * 2 + rect[0] // 2
                    v = self.ref_idx(rec, A, B, lst, b8)
                    x, y, w, h = rect
                    for yy in range(y // 2, (y + h + 1) // 2):
                        for xx in range(x // 2, (x + w + 1) // 2):
                            rec.ref[lst][yy * 2 + xx] = v
        for lst in (0, 1):
            for rect, mode in zip(rects, modes):
                if mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi")):
                    ax = abs(self.mvd_comp(rec, A, B, lst, rect[0], rect[1], 0))
                    ay = abs(self.mvd_comp(rec, A, B, lst, rect[0], rect[1], 1))
                    self.set_mvd(rec, lst, rect, ax, ay)

    # ------------------------------------------------------------ residual (truth only: Chapter 8.3)
    def cbf_cond_other(self, n, cur_intra, getter):
        if n is None:
            return 1 if cur_intra else 0
        if n.pcm:
            return 1
        return getter(n)

    def resid_block(self, cat, cbf_inc, nmax):
        """One residual_block_cabac.  -> coded_block_flag."""
        if not self.dec(85 + CBF_OFF[cat] + cbf_inc):
            return 0
        sigb, lastb, absb = 105 + SIG_OFF[cat], 166 + SIG_OFF[cat], 227 + ABS_OFF[cat]
        sig = []
        i = 0
        while i < nmax - 1:
            inc = min(i, 2) if cat == 3 else i
            if self.dec(sigb + inc):
                sig.append(i)
                if self.dec(lastb + inc):
                    break
            i += 1
        else:
            sig.append(nmax - 1)
        eq1 = gt1 = 0
        for _ in sig:           # levels in reverse scan order; only the counts matter for the contexts
            inc0 = 0 if gt1 else min(4, 1 + eq1)
            if not self.dec(absb + inc0):
                eq1 += 1
            else:
                inc = 5 + min(4 - (1 if cat == 3 else 0), gt1)
                n = 1
                while n < 14 and self.dec(absb + inc):
                    n += 1
                if n >= 14:
                    k = 0
                    while self.eng.bypass():
                        k += 1
                        if k > 24:
                            raise Unusable("coeff_abs_level suffix does not end")
                    while k:
                        k -= 1
                        self.eng.bypass()
                gt1 += 1
            self.eng.bypass()   # sign
        return 1

    def residual(self, rec, A, B, i16, cbp_l, cbp_c):
        intra = rec.intra
        if i16:
            ca = self.cbf_cond_other(A, intra, lambda n: n.dccbf)
            cb = self.cbf_cond_other(B, intra, lambda n: n.dccbf)
            rec.dccbf = self.resid_block(0, ca + 2 * cb, 16)
        for b8 in range(4):
            if not (cbp_l >> b8) & 1:
                continue
            for b4 in range(4):
                x4, y4 = blk_xy(b8 * 4 + b4)
                ca = rec.lcbf[y4 * 4 + x4 - 1] if x4 else self.cbf_cond_other(A, intra, lambda n: n.lcbf[y4 * 4 + 3])
                cb = rec.lcbf[(y4 - 1) * 4 + x4] if y4 else self.cbf_cond_other(B, intra, lambda n: n.lcbf[12 + x4])
                rec.lcbf[y4 * 4 + x4] = self.resid_block(1 if i16 else 2, ca + 2 * cb, 15 if i16 else 16)
        if cbp_c:
            for c in range(2):
                ca = self.cbf_cond_other(A, intra, lambda n: n.cdc[c])
                cb = self.cbf_cond_other(B, intra, lambda n: n.cdc[c])
                rec.cdc[c] = self.resid_block(3, ca + 2 * cb, 4)
        if cbp_c == 2:
            for c in range(2):
                for b in range(4):
                    x, y = b & 1, b >> 1
                    ca = rec.cac[c][y * 2 + x - 1] if x else self.cbf_cond_other(A, intra, lambda n: n.cac[c][y * 2 + 1])
                    cb = rec.cac[c][(y - 1) * 2 + x] if y else self.cbf_cond_other(B, intra, lambda n: n.cac[c][2 + x])
                    rec.cac[c][b] = self.resid_block(4, ca + 2 * cb, 15)

    # ------------------------------------------------------------ hints
    def take_hint(self, rec, kind):
        h = self.hints.get((self.info.index, rec.addr, kind))
        if h is None:
            raise Unusable(f"no HINT {kind} for slice {self.info.index} macroblock {rec.addr}")
        eng = self.eng
        if not self.mut("hint_offset_ignored"):
            eng.offset = h["offset"]
        eng.range = h["range"]
        eng.pos = h["bit"] + (1 if self.mut("hint_bit_plus1") else 0)

    def make_hint(self, rec, kind):
        e = self.eng
        h = dict(range=e.range, offset=e.offset, bit=e.pos)
        if kind == "cbp":
            rec.hint_cbp = h
        else:
            rec.hint_end = h

    # ------------------------------------------------------------ one macroblock
    def macroblock(self, addr, qp, prev_nz):
        """-> (rec, new qp, new prev_nz)."""
        info = self.info
        rec = MbRec(addr)
        self.recs[addr] = rec
        A, B = self.nb(addr)
        eng = self.eng
        if self.t != "I":
            if self.skip_flag(A, B):
                rec.skip = True
                rec.direct = self.t == "B"
                rec.mbt = None
                rec.qp = qp
                return rec, qp, (prev_nz if self.mut("qpd_prev_any") else False)
        # ---- mb_type
        if self.t == "I":
            ival, inter, mbt = self.i_type_islice(A, B), None, None
            mbt = ival
            intra = True
        elif self.t == "P":
            intra, v = self.p_type()
            mbt, ival, inter = (5 + v, v, None) if intra else (v, None, v)
        else:
            intra, v = self.b_type(A, B)
            mbt, ival, inter = (23 + v, v, None) if intra else (v, None, v)
        rec.mbt = mbt
        rec.intra = intra
        if intra:
            if ival == 25:
                rec.pcm = True
                rec.cbp_l, rec.cbp_c = 15, 2
                rec.lcbf = [1] * 16
                rec.dccbf = 1
                rec.cdc = [1, 1]
                rec.cac = [[1] * 4, [1] * 4]
                if self.mut("pcm_no_align"):
                    eng.pos = eng.pos
                    p = eng.pos
                    eng.start(p + 8 * (383 if self.mut("pcm_samples_383") else 384))
                elif self.mut("pcm_samples_383"):
                    eng.restart_after_pcm(383)
                else:
                    eng.restart_after_pcm(384)
                rec.qp = 0 if self.mut("pcm_qp_zero") else qp
                return rec, rec.qp if self.mut("pcm_qp_zero") else qp, (prev_nz if self.mut("qpd_prev_any") else False)
            if ival == 0:
                rec.i_nxn = True
                rec.modes = self.intra_modes()
            else:
                rec.i16 = True
            rec.cpred = self.chroma_pred(A, B)
            if rec.i_nxn:
                rec.cbp_l, rec.cbp_c = self.cbp(A, B, rec)
            else:
                rec.cbp_l = 15 if ival >= 13 else 0
                rec.cbp_c = ((ival - 1) // 4) % 3
            has_qpd = rec.i16 or rec.cbp_l or rec.cbp_c
            needs_hint = rec.i16 or rec.cbp_l or rec.cbp_c
        else:
            direct = (self.t == "B" and inter == 0)
            if direct:
                rec.direct = True
                rec.cbp_l, rec.cbp_c = self.cbp(A, B, rec)
                has_qpd = bool(rec.cbp_l or rec.cbp_c)
                needs_hint = has_qpd
            else:
                if self.reader:
                    self.take_hint(rec, "cbp")
                else:
                    self.inter_pred(rec, A, B, inter)
                    self.make_hint(rec, "cbp")
                rec.cbp_l, rec.cbp_c = self.cbp(A, B, rec)
                has_qpd = bool(rec.cbp_l or rec.cbp_c)
                needs_hint = has_qpd
        # ---- mb_qp_delta
        qpd = 0
        if has_qpd:
            qpd = self.qp_delta(prev_nz)
            rec.qpd, rec.qpd_present = qpd, True
            qp = self.apply_qp(qp, qpd)
        rec.qp = qp
        if needs_hint:
            if self.reader:
                self.take_hint(rec, "end")
            else:
                self.residual(rec, A, B, rec.i16, rec.cbp_l, rec.cbp_c)
                self.make_hint(rec, "end")
        if has_qpd:
            return rec, qp, qpd != 0
        return rec, qp, (prev_nz if self.mut("qpd_prev_any") else False)

    def apply_qp(self, qp, qpd):
        if self.mut("qpd_no_wrap"):
            return clip3(0, 51, qp + qpd)
        return (qp + qpd + 52) % 52

    # ------------------------------------------------------------ the slice
    def parse(self):
        """-> (list of MbRec in decoding order, status).  status 'complete' (end_of_slice_flag = 1 and, in truth mode, clean
        trailing bits) or a short text."""
        info = self.info
        start = (info.header_end_bit + 7) // 8
        self.eng = R.Engine(info.nal, start * 8)
        self.st = R.init_contexts(self.t, info.hdr.get("cabac_init_idc", 0), info.slice_qp)
        qp = info.slice_qp
        if self.mut("qp_not_reset") and "qp" in self.carry:
            qp = self.carry["qp"]
        prev_nz = bool(self.carry.get("nz")) if self.mut("qpd_not_reset") else False
        out = []
        addr = info.first_mb
        status = "complete"
        while True:
            if addr >= info.pic_size_in_mbs:
                status = "no end_of_slice_flag before the picture ends"
                break
            try:
                rec, qp, prev_nz = self.macroblock(addr, qp, prev_nz)
            except (Unusable, KeyError, IndexError) as e:
                if not self.reader:
                    raise Unusable(f"slice {info.index}, macroblock {addr}: {e}")
                status = f"reader parse failed: {e}"
                break
            out.append(rec)
            if rec.skip and self.mut("eos_skipped_after_skip"):
                rec.eos = 0
            else:
                rec.eos = self.eng.terminate()
            if rec.eos:
                break
            addr += 1
        self.carry["qp"], self.carry["nz"] = qp, prev_nz
        if not self.reader and status == "complete":
            # rbsp_stop_one_bit = the last 1 bit of the NAL unit.  A minimal flush (the spec's, and cabac_enc.py's) puts it
            # exactly at the last bit the engine took (position pos - 1); x264's flush may put it up to 6 bits later (the
            # bits in between are zero).  Anything else means the parse did not end where the encoder ended.
            bits = "".join(f"{b:08b}" for b in info.nal)
            stop = bits.rfind("1")
            if not (self.eng.pos - 1 <= stop <= self.eng.pos + 6):
                raise Unusable(f"slice {info.index}: end_of_slice_flag = 1 at macroblock {out[-1].addr} but the engine stopped at "
                               f"bit {self.eng.pos} while the rbsp_stop_one_bit is bit {stop}")
        return out, status


def usable(info):
    """Reason why this slice cannot be handled by the reference, or None."""
    if not info.entropy:
        return "CAVLC slice"
    if info.slice_type in (3, 4):
        return "SP/SI slice"
    s, h, p = info.sps, info.hdr, info.pps
    if h.get("field_pic_flag", 0) or (s.get("mb_adaptive_frame_field_flag", 0) and not s.get("frame_mbs_only_flag", 1)):
        return "field or MBAFF picture"
    if p.get("transform_8x8_mode_flag", 0):
        return "PPS has transform_8x8_mode_flag = 1 (transform_size_8x8_flag is Chapter 8.5)"
    if (s.get("chroma_format_idc", 1) != 1 or s.get("bit_depth_luma_minus8", 0) or s.get("bit_depth_chroma_minus8", 0)
            or s.get("separate_colour_plane_flag", 0)):
        return "not 8-bit 4:2:0"
    if p.get("num_slice_groups_minus1", 0):
        return "slice groups (FMO)"
    return None


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


def parse_all(infos, reader=False, hints=None):
    """-> list of (info, recs, status) for every usable CABAC slice, stream order."""
    out = []
    carry = {}
    for info in infos:
        if usable(info) is not None:
            continue
        recs, status = SliceParser(info, reader, hints, carry).parse()
        out.append((info, recs, status))
    return out


# ---------------------------------------------------------------- text formats (Readme section 5)
def mb_fields(rec):
    """The dump fields of a macroblock, as strings, in line order."""
    if rec.skip:
        mbt = "skip"
    else:
        mbt = str(rec.mbt)
    pcm_or_skip = rec.skip or rec.pcm
    has_cbp = not pcm_or_skip
    return [("mbt", mbt),
            ("cpred", str(rec.cpred) if rec.intra and not rec.pcm else "-"),
            ("modes", rec.modes if rec.i_nxn else "-"),
            ("cbp", str(rec.cbp_l + 16 * rec.cbp_c) if has_cbp else "-"),
            ("qpd", str(rec.qpd) if rec.qpd_present else "-"),
            ("qp", str(rec.qp)),
            ("eos", str(rec.eos))]


def slice_letter(info):
    return {0: "P", 1: "B", 2: "I", 3: "P"}[info.slice_type]


def render_dump(parsed):
    lines = []
    for info, recs, status in parsed:
        lines.append(f"SLICE {info.index} type={slice_letter(info)} first_mb={info.first_mb}")
        for r in recs:
            lines.append(f"MB {info.index} {r.addr} " + " ".join(f"{k}={v}" for k, v in mb_fields(r)))
        lines.append(f"END {info.index} mbs={len(recs)}")
    return "\n".join(lines) + "\n"


def render_hints(parsed):
    lines = []
    for info, recs, status in parsed:
        for r in recs:
            for kind, h in (("cbp", r.hint_cbp), ("end", r.hint_end)):
                if h is not None:
                    lines.append(f"HINT {info.index} {r.addr} {kind} range={h['range']} offset={h['offset']} bit={h['bit']}")
    return "\n".join(lines) + "\n"


def parse_hints(text):
    hints = {}
    for ln in text.splitlines():
        ln = ln.split("#")[0].split()
        if not ln:
            continue
        kv = dict(x.split("=") for x in ln[4:])
        hints[(int(ln[1]), int(ln[2]), ln[3])] = {k: int(v) for k, v in kv.items()}
    return hints
