"""Chapter 8.4 reference: a complete CABAC macroblock-layer parser for I / P / B slices (8-bit 4:2:0, frame pictures,
transform_8x8_mode_flag = 0, no MBAFF), written for the harness.  Extends Chapter 8.3's parser: the inter syntax
(sub_mb_type, ref_idx, mvd) is now part of the dump (keys `sub refidx refidx1 mvd mvd1`) and the 'cbp' hints are gone: the
parse of a slice needs nothing but the slice data.

The parse must end with end_of_slice_flag = 1 on the last macroblock and zero padding after the rbsp_stop_one_bit: that is the
proof of correctness.  `reader=True` only switches the deliberately wrong rules of MUTANT on (the harness's --self-test);
the correct parser is the same code with MUTANT = None.

Dump grammar: Readme section 5.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cabac_ref as R  # noqa: E402

MUTANT = None
MUTANTS = {   # name -> what is wrong (applied only when reader=True)
    "psub_swap_2_3": "P sub_mb_type: values 2 (4x8) and 3 (4x4) exchanged (bins 0 1 1 / 0 1 0 read the wrong way round)",
    "psub_bin1_ctx21": "P sub_mb_type: bin 1 uses ctxIdx 21 instead of 22",
    "psub_bin2_ctx22": "P sub_mb_type: bin 2 uses ctxIdx 22 instead of 23",
    "bsub_bin0_ctx37": "B sub_mb_type: bin 0 uses ctxIdx 37 instead of 36",
    "bsub_bin2_ctx39_always": "B sub_mb_type: bin 2 always uses ctxIdx 39 (it is 38 when bin 1 is 1)",
    "bsub_11_12_swapped": "B sub_mb_type: the values 11 and 12 (B_L1_4x4, B_Bi_4x4) exchanged",
    "bsub_tail_order": "B sub_mb_type 7..10: the last two bins read least significant first",
    "ref_AB_swapped": "ref_idx: ctxIdxInc = 2 * condTermFlagA + condTermFlagB",
    "ref_cond_ge0": "ref_idx: a neighbouring partition counts when it uses the list at all (refIdx >= 0), not only when refIdx > 0",
    "ref_direct_counts": "ref_idx: B_Skip / B_Direct_16x16 / direct sub-macroblock neighbours count 1 (they count 0 whatever their derived refIdx)",
    "ref_intra_counts": "ref_idx: an intra neighbour counts 1 (it counts 0)",
    "ref_unavail_counts": "ref_idx: an unavailable neighbour counts 1 (it counts 0)",
    "ref_inner_ignored": "ref_idx: neighbours inside the current macroblock are ignored (count 0)",
    "ref_list_shared": "ref_idx_l1 takes its context from the neighbours' ref_idx_l0 (should be their ref_idx_l1)",
    "ref_bin1_ctx5": "ref_idx: bin 1 uses ctxIdx 54 + 5 instead of 54 + 4",
    "ref_bin2_ctx4": "ref_idx: bins 2 and up use ctxIdx 54 + 4 instead of 54 + 5",
    "ref_gate_missing": "ref_idx is read even when num_ref_idx_active is 1 (it must be inferred 0 then)",
    "ref_store_first8x8": "ref_idx of a 16x8 / 8x16 partition is remembered for its first 8x8 block only (should cover both)",
    "order_ref_interleaved": "ref_idx_l0 / ref_idx_l1 are read partition by partition (all l0 first, then all l1 is right)",
    "order_mvd_interleaved": "mvd_l0 / mvd_l1 are read partition by partition (all mvd_l0 first, then mvd_l1 is right)",
    "direct_sub_reads_ref": "B_8x8: a direct sub-macroblock reads a ref_idx_l0 (it has no ref_idx / mvd)",
    "mvd_AB_sum_signed": "mvd ctxIdxInc: the neighbours' SIGNED mvd are summed (absolute values are summed)",
    "mvd_thr_3_to_0": "mvd ctxIdxInc: a sum of exactly 3 gives increment 0 (thresholds are < 3, 3..32, > 32)",
    "mvd_thr_32_to_2": "mvd ctxIdxInc: a sum of exactly 32 gives increment 2",
    "mvd_only_A": "mvd ctxIdxInc: only neighbour A counts",
    "mvd_y_uses_x_ctx": "mvd_y uses the contexts 40..46 of mvd_x (they are 47..53)",
    "mvd_y_uses_x_sum": "mvd_y: the neighbour sum is taken from the x components",
    "mvd_prefix_ctx_nocap": "mvd prefix bins 3 and up use contexts 6, 7, 8, ... (they stay at ctxIdxInc 6)",
    "mvd_ucoff8": "mvd: the prefix is cut at 8 ones instead of 9 (uCoff = 9)",
    "mvd_suffix_k2": "mvd suffix: Exp-Golomb order 2 instead of 3",
    "mvd_suffix_lsb_first": "mvd suffix: the escape bits are read least significant first",
    "mvd_sign_inverted": "mvd sign: 1 means positive",
    "mvd_store_first4x4": "mvd of a partition is remembered for its top-left 4x4 block only (should cover the whole partition)",
    "mvd_list_shared": "mvd_l1 takes its neighbour sums from the neighbours' mvd_l0",
    "mvd_inner_ignored": "mvd: neighbours inside the current macroblock are ignored (count 0)",
    "mvd_A_row0": "mvd: neighbour A in the left macroblock is taken from row 0 instead of the partition's row",
    "mvd_B_col0": "mvd: neighbour B in the macroblock above is taken from column 0 instead of the partition's column",
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
        self.mvd = [[(0, 0)] * 16, [(0, 0)] * 16]   # ABSOLUTE mvd components per 4x4 block (raster), per list (context source)
        self.mvds = [[(0, 0)] * 16, [(0, 0)] * 16]  # signed mvd per 4x4 block
        self.ref = [[0] * 4, [0] * 4]               # ref_idx per 8x8 block, 0 when not coded / direct / list not used
        self.used = [[False] * 4, [False] * 4]      # the 8x8 block's partition uses this list (predFlagLX = 1, not direct)
        self.dir8 = [False] * 4                     # B_8x8: this sub-macroblock is B_Direct_8x8
        self.sub = None                             # dump: the four sub_mb_type values of a P_8x8 / B_8x8
        self.refl = [[], []]                        # dump: ref_idx per partition using the list, syntax order
        self.mvdl = [[], []]                        # dump: signed mvd (x, y) per prediction block using the list, syntax order
        self.blocks = []           # residual blocks in decoding order: dict(kind, idx, maxnum, coeffs or None)
        self.pcm_samples = None


def blk_xy(idx):
    """luma4x4BlkIdx -> (x4, y4)."""
    return (idx % 2) + 2 * ((idx // 4) % 2), ((idx // 2) % 2) + 2 * (idx // 8)


class SliceParser:
    def __init__(self, info, reader=False, prev_state=None):
        self.info = info
        self.reader = reader
        self.W = info.pic_width_in_mbs
        self.eng = None
        self.st = None
        self.recs = {}
        self.t = {0: "P", 1: "B", 2: "I", 3: "P"}[info.slice_type]
        self.carry = prev_state if prev_state is not None else {}     # for the 'not reset' mutants
        self.carry_cnt = (0, 0)

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

    # ------------------------------------------------------------ inter syntax (Chapter 8.4)
    def ref_idx(self, rec, A, B, lst, b8):
        """ref_idx_lX of the partition whose top-left 8x8 block is b8 (9.3.2.2 unary, 9.3.3.1.1.6 contexts)."""
        x8, y8 = b8 & 1, b8 >> 1
        sl = 0 if (lst == 1 and self.mut("ref_list_shared")) else lst

        def cond(n_rec, bb, inner):
            if n_rec is None:
                return self.mut("ref_unavail_counts")
            if inner and self.mut("ref_inner_ignored"):
                return False
            if n_rec.intra:
                return self.mut("ref_intra_counts")
            if n_rec.direct or n_rec.dir8[bb]:
                return self.mut("ref_direct_counts")
            if self.mut("ref_cond_ge0"):
                return n_rec.used[sl][bb]
            return n_rec.ref[sl][bb] > 0
        ca = cond(rec, b8 - 1, True) if x8 else cond(A, b8 + 1, False)
        cb = cond(rec, b8 - 2, True) if y8 else cond(B, b8 + 2, False)
        inc = (2 * int(ca) + int(cb)) if self.mut("ref_AB_swapped") else (int(ca) + 2 * int(cb))
        if not self.dec(54 + inc):
            return 0
        if not self.dec(54 + (5 if self.mut("ref_bin1_ctx5") else 4)):
            return 1
        n = 2
        while self.dec(54 + (4 if self.mut("ref_bin2_ctx4") else 5)):
            n += 1
            if n > 64:
                raise Unusable("ref_idx does not end")
        return n

    def mvd_comp(self, rec, A, B, lst, x4, y4, comp):
        """One mvd component (signed).  Neighbour partitions A (left of the top-left 4x4 block) and B (above it)."""
        sl = 0 if (lst == 1 and self.mut("mvd_list_shared")) else lst
        sc = 0 if (comp == 1 and self.mut("mvd_y_uses_x_sum")) else comp

        def val(n_rec, bx, by, inner):
            if n_rec is None or (inner and self.mut("mvd_inner_ignored")):
                return 0
            if self.mut("mvd_AB_sum_signed"):
                return n_rec.mvds[sl][by * 4 + bx][sc]
            return n_rec.mvd[sl][by * 4 + bx][sc]
        a = val(rec, x4 - 1, y4, True) if x4 else val(A, 3, 0 if self.mut("mvd_A_row0") else y4, False)
        b = 0 if self.mut("mvd_only_A") else (val(rec, x4, y4 - 1, True) if y4 else val(B, 0 if self.mut("mvd_B_col0") else x4, 3, False))
        s = a + b
        base = 40 if (comp == 0 or self.mut("mvd_y_uses_x_ctx")) else 47
        lo = 3 if not self.mut("mvd_thr_3_to_0") else 4
        hi = 32 if not self.mut("mvd_thr_32_to_2") else 31
        if not self.dec(base + (0 if s < lo else 1 if s <= hi else 2)):
            return 0
        n = 1
        incs = [3, 4, 5, 6, 7, 8, 9, 10] if self.mut("mvd_prefix_ctx_nocap") else [3, 4, 5, 6, 6, 6, 6, 6]
        ucoff = 8 if self.mut("mvd_ucoff8") else 9
        while n < ucoff and self.dec(base + incs[n - 1]):
            n += 1
        if n >= ucoff:
            k = 2 if self.mut("mvd_suffix_k2") else 3
            while self.eng.bypass():
                n += 1 << k
                k += 1
                if k > 24:
                    raise Unusable("mvd suffix does not end")
            if self.mut("mvd_suffix_lsb_first"):
                for j in range(k):
                    n += self.eng.bypass() << j
            else:
                while k:
                    k -= 1
                    n += self.eng.bypass() << k
        neg = self.eng.bypass()
        if self.mut("mvd_sign_inverted"):
            neg = 1 - neg
        return -n if neg else n

    def set_mvd(self, rec, lst, rect, vx, vy):
        x, y, w, h = rect
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if self.mut("mvd_store_first4x4") and (xx, yy) != (x, y):
                    continue
                rec.mvd[lst][yy * 4 + xx] = (abs(vx), abs(vy))
                rec.mvds[lst][yy * 4 + xx] = (vx, vy)

    def sub_type(self):
        if self.t == "P":
            if self.dec(21):
                return 0
            if not self.dec(21 if self.mut("psub_bin1_ctx21") else 22):
                return 1
            b2 = self.dec(22 if self.mut("psub_bin2_ctx22") else 23)
            if self.mut("psub_swap_2_3"):
                return 3 if b2 else 2
            return 2 if b2 else 3
        if not self.dec(37 if self.mut("bsub_bin0_ctx37") else 36):
            return 0
        if not self.dec(37):
            return 1 + self.dec(39)
        t = 3
        if self.dec(39 if self.mut("bsub_bin2_ctx39_always") else 38):
            if self.dec(39):
                v = self.dec(39)
                return (12 - v) if self.mut("bsub_11_12_swapped") else 11 + v
            t += 4
        hi = self.dec(39)
        lo = self.dec(39)
        t += (2 * lo + hi) if self.mut("bsub_tail_order") else (2 * hi + lo)
        return t

    def inter_pred(self, rec, A, B, mbt):
        """mb_pred / sub_mb_pred of an inter macroblock other than B_Direct_16x16 (7.3.5.1, 7.3.5.2): sub_mb_type x4 (8x8
        only), then ref_idx_l0 of every partition using list 0, ref_idx_l1, mvd_l0, mvd_l1."""
        info = self.info
        nref = [info.num_ref_idx_l0_active, info.num_ref_idx_l1_active if self.t == "B" else 1]
        ref_items, mvd_items = [], []          # (b8 holding the partition's top-left, covered 8x8 blocks, mode) / (rect, mode)
        if mbt == (3 if self.t == "P" else 22):          # 8x8
            tab = P_SUB if self.t == "P" else B_SUB
            subs = [self.sub_type() for _ in range(4)]
            rec.sub = subs
            for b8 in range(4):
                shape, mode = tab[subs[b8]]
                if mode == "Direct":
                    rec.dir8[b8] = True
                    if self.mut("direct_sub_reads_ref"):
                        ref_items.append((b8, [b8], "L0"))
                    continue
                ref_items.append((b8, [b8], mode))
                ox, oy = (b8 & 1) * 2, (b8 >> 1) * 2
                for (x, y, w, h) in shape:
                    mvd_items.append(((ox + x, oy + y, w, h), mode))
        else:
            rects, modes = (P_TYPES if self.t == "P" else B_TYPES)[mbt]
            for rect, mode in zip(rects, modes):
                x, y, w, h = rect
                cov = [yy * 2 + xx for yy in range(y // 2, (y + h + 1) // 2) for xx in range(x // 2, (x + w + 1) // 2)]
                if self.mut("ref_store_first8x8"):
                    cov = cov[:1]
                ref_items.append(((y // 2) * 2 + x // 2, cov, mode))
                mvd_items.append((rect, mode))

        def uses(mode, lst):
            return mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi"))
        order = ([(l, it) for it in ref_items for l in (0, 1)] if self.mut("order_ref_interleaved")
                 else [(l, it) for l in (0, 1) for it in ref_items])
        for lst, (b8, cov, mode) in order:
            if not uses(mode, lst):
                continue
            for bb in cov:
                rec.used[lst][bb] = True
            v = self.ref_idx(rec, A, B, lst, b8) if (nref[lst] > 1 or self.mut("ref_gate_missing")) else 0
            for bb in cov:
                rec.ref[lst][bb] = v
            rec.refl[lst].append(v)
        order = ([(l, it) for it in mvd_items for l in (0, 1)] if self.mut("order_mvd_interleaved")
                 else [(l, it) for l in (0, 1) for it in mvd_items])
        for lst, (rect, mode) in order:
            if not uses(mode, lst):
                continue
            vx = self.mvd_comp(rec, A, B, lst, rect[0], rect[1], 0)
            vy = self.mvd_comp(rec, A, B, lst, rect[0], rect[1], 1)
            self.set_mvd(rec, lst, rect, vx, vy)
            rec.mvdl[lst].append((vx, vy))

    # ------------------------------------------------------------ residual (Chapter 8.3)
    def cbf_cond_other(self, n, cur_intra, flag):
        """condTermFlagN of a block whose neighbour lies in ANOTHER macroblock n (9.3.3.1.1.9).  flag(n) -> the neighbour
        block's coded_block_flag, or None when that block does not exist / was not coded (transBlockN not available)."""
        if n is None:
            if cur_intra:
                return 0 if self.mut("cbf_unavail_intra0") else 1
            return 1 if self.mut("cbf_unavail_inter1") else 0
        if n.pcm:
            return 0 if self.mut("cbf_pcm0") else 1
        if n.skip:
            return 1 if self.mut("cbf_skip1") else 0
        f = flag(n)
        if f is None:
            return 1 if self.mut("cbf_notcoded_is_1") else 0
        return f

    def cbf_inc(self, ca, cb):
        return 2 * ca + cb if self.mut("cbf_inc_AB_swapped") else ca + 2 * cb

    def inner(self, v):
        return 0 if self.mut("cbf_inner_ignored") else v

    def resid_block(self, cat, cbf_inc, nmax):
        """One residual_block_cabac.  -> list of nmax levels in scan order, or None when coded_block_flag = 0."""
        ccat = cat
        if cat == 1 and self.mut("cbf_i16ac_as_luma"):
            ccat = 2
        if cat == 4 and self.mut("cbf_cac_as_cdc"):
            ccat = 3
        if not self.dec(85 + CBF_OFF[ccat] + cbf_inc):
            return None
        sigb = 105 + (44 if cat == 4 and self.mut("sig_off_cat4") else SIG_OFF[cat])
        lastb = (105 if self.mut("last_uses_sig_base") else 166) + (44 if cat == 4 and self.mut("sig_off_cat4") else SIG_OFF[cat])
        absb = 227 + (38 if cat == 4 and self.mut("abs_off_cat_wrong") else ABS_OFF[cat])
        sig = []
        i = 0
        nloop = nmax if self.mut("sig_reads_last_position") else nmax - 1
        while i < nloop:
            if cat == 3:
                inc = min(i // 2 if self.mut("sig_cdc_inc_div2") else i, 2)
            elif cat in (1, 4) and self.mut("sig_ac_inc_plus1"):
                inc = i + 1
            else:
                inc = i
            if self.dec(sigb + inc):
                sig.append(i)
                if self.dec(lastb + (0 if self.mut("last_inc_zero") else inc)):
                    break
            i += 1
        else:
            if nloop == nmax - 1:
                sig.append(nmax - 1)     # the last position is inferred to be significant
        if self.mut("abs_counters_not_reset"):
            eq1, gt1 = self.carry_cnt
        else:
            eq1 = gt1 = 0
        cmax = 15 if self.mut("abs_prefix_cmax15") else 14
        levels = []
        for _ in sig:           # levels come in REVERSE scan order
            inc0 = min(4, 1 + eq1) if self.mut("abs_bin0_ignores_gt1") else (0 if gt1 else min(4, 1 + eq1))
            if not self.dec(absb + inc0):
                a = 0
                eq1 += 1
            else:
                inc = 5 + min(3 if self.mut("abs_cap3_all") else 4 - (1 if cat == 3 else 0), gt1)
                n = 1
                while n < cmax and self.dec(absb + inc):
                    n += 1
                a = n
                if n >= cmax:               # UEG0 suffix: Exp-Golomb, bypass bins
                    k = 1 if self.mut("abs_suffix_k1") else 0
                    while self.eng.bypass():
                        a += 1 << k
                        k += 1
                        if k > 24:
                            raise Unusable("coeff_abs_level_minus1 suffix does not end")
                    if self.mut("abs_suffix_lsb_first"):
                        for j in range(k):
                            a += self.eng.bypass() << j
                    else:
                        while k:
                            k -= 1
                            a += self.eng.bypass() << k
                gt1 += 1
            val = a if self.mut("abs_plus1_missing") else a + 1
            sgn = self.eng.bypass()
            if self.mut("sign_inverted"):
                sgn = 1 - sgn
            levels.append(-val if sgn else val)
        self.carry_cnt = (eq1, gt1)
        coeffs = [0] * nmax
        for pos, v in zip(sig if self.mut("levels_forward_order") else reversed(sig), levels):
            coeffs[pos] = v
        return coeffs

    def residual(self, rec, A, B, i16, cbp_l, cbp_c):
        intra = rec.intra
        rec.blocks = []

        def put(kind, idx, maxnum, coeffs):
            rec.blocks.append(dict(kind=kind, idx=idx, maxnum=maxnum, coeffs=coeffs))
        if i16:
            ca = self.cbf_cond_other(A, intra, lambda n: n.dccbf if n.i16 else None)
            cb = self.cbf_cond_other(B, intra, lambda n: n.dccbf if n.i16 else None)
            if self.mut("cbf_dc_from_ac"):
                ca = self.cbf_cond_other(A, intra, lambda n: n.lcbf[15] if n.cbp_l else None)
                cb = self.cbf_cond_other(B, intra, lambda n: n.lcbf[15] if n.cbp_l else None)
            c = self.resid_block(0, self.cbf_inc(ca, cb), 16)
            rec.dccbf = int(c is not None)
            put("i16dc", 0, 16, c)

        def lflag(x4, y4):
            return lambda n: n.lcbf[y4 * 4 + x4] if (n.cbp_l >> ((y4 // 2) * 2 + x4 // 2)) & 1 else None
        for b8 in range(4):
            if not (cbp_l >> b8) & 1:
                continue
            for b4 in range(4):
                idx = b8 * 4 + b4
                x4, y4 = blk_xy(idx)
                ca = self.inner(rec.lcbf[y4 * 4 + x4 - 1]) if x4 else self.cbf_cond_other(A, intra, lflag(3, y4))
                cb = self.inner(rec.lcbf[(y4 - 1) * 4 + x4]) if y4 else self.cbf_cond_other(B, intra, lflag(x4, 3))
                c = self.resid_block(1 if i16 else 2, self.cbf_inc(ca, cb), 15 if i16 else 16)
                rec.lcbf[y4 * 4 + x4] = int(c is not None)
                put("i16ac" if i16 else "luma", idx, 15 if i16 else 16, c)
        if cbp_c:
            for c in range(2):
                cs = 0 if self.mut("cbf_cdc_cr_like_cb") else c
                ca = self.cbf_cond_other(A, intra, lambda n: n.cdc[cs] if n.cbp_c else None)
                cb = self.cbf_cond_other(B, intra, lambda n: n.cdc[cs] if n.cbp_c else None)
                co = self.resid_block(3, self.cbf_inc(ca, cb), 4)
                rec.cdc[c] = int(co is not None)
                put(("cbdc", "crdc")[c], 0, 4, co)
        if cbp_c == 2:
            for c in range(2):
                for b in range(4):
                    x, y = b & 1, b >> 1
                    ca = (self.inner(rec.cac[c][y * 2 + x - 1]) if x
                          else self.cbf_cond_other(A, intra, lambda n: n.cac[c][y * 2 + 1] if n.cbp_c == 2 else None))
                    cb = (self.inner(rec.cac[c][(y - 1) * 2 + x]) if y
                          else self.cbf_cond_other(B, intra, lambda n: n.cac[c][2 + x] if n.cbp_c == 2 else None))
                    co = self.resid_block(4, self.cbf_inc(ca, cb), 15)
                    rec.cac[c][b] = int(co is not None)
                    put(("cbac", "crac")[c], b, 15, co)

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
                p0 = (eng.pos + 7) & ~7
                rec.pcm_samples = bytes(info.nal[p0 // 8:p0 // 8 + 384])
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
            has_resid = rec.i16 or rec.cbp_l or rec.cbp_c
        else:
            direct = (self.t == "B" and inter == 0)
            if direct:
                rec.direct = True
                rec.cbp_l, rec.cbp_c = self.cbp(A, B, rec)
                has_qpd = bool(rec.cbp_l or rec.cbp_c)
                has_resid = has_qpd
            else:
                self.inter_pred(rec, A, B, inter)
                rec.cbp_l, rec.cbp_c = self.cbp(A, B, rec)
                has_qpd = bool(rec.cbp_l or rec.cbp_c)
                has_resid = has_qpd
        # ---- mb_qp_delta
        qpd = 0
        if has_qpd:
            qpd = self.qp_delta(prev_nz)
            rec.qpd, rec.qpd_present = qpd, True
            qp = self.apply_qp(qp, qpd)
        rec.qp = qp
        if has_resid:          # the macroblock has residual syntax (Intra16x16, or CBP != 0)
            self.residual(rec, A, B, rec.i16, rec.cbp_l, rec.cbp_c)
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


def parse_all(infos, reader=False):
    """-> list of (info, recs, status) for every usable CABAC slice, stream order."""
    out = []
    carry = {}
    for info in infos:
        if usable(info) is not None:
            continue
        recs, status = SliceParser(info, reader, carry).parse()
        out.append((info, recs, status))
    return out


# ---------------------------------------------------------------- text formats (Readme section 5)
def coef_string(blocks):
    """The `coef=` value (same shape as the CAVLC chapters'): coded blocks in decoding order separated by ';', each
    <kind><idx>:<list>, <list> = '-' (coded_block_flag 0, all zero) or comma separated pos=value pairs (non-zero
    coefficients, ascending pos).  pos = position in the 4x4 zig-zag scan; for the 15-coefficient blocks (i16ac, cbac, crac)
    list entry k sits at scan position k + 1; for chroma DC the index 0..3 of the 2x2 DC coefficient.  '-' when none."""
    out = []
    for b in blocks:
        off = 0 if MUTANT == "ac_offset_missing" else (1 if b["maxnum"] == 15 else 0)
        nz = [f"{k + off}={v}" for k, v in enumerate(b["coeffs"] or []) if v]
        out.append(f"{b['kind']}{b['idx']}:" + (",".join(nz) if nz else "-"))
    return ";".join(out) or "-"


def mb_fields(rec):
    """The dump fields of a macroblock, as strings, in line order."""
    if rec.skip:
        mbt = "skip"
    else:
        mbt = str(rec.mbt)
    pcm_or_skip = rec.skip or rec.pcm
    has_cbp = not pcm_or_skip
    def lst(v):
        return ",".join(map(str, v)) or "-"

    def mvs(v):
        return ",".join(f"{x}:{y}" for x, y in v) or "-"
    return [("mbt", mbt),
            ("cpred", str(rec.cpred) if rec.intra and not rec.pcm else "-"),
            ("modes", rec.modes if rec.i_nxn else "-"),
            ("sub", lst(rec.sub or [])),
            ("refidx", lst(rec.refl[0])),
            ("refidx1", lst(rec.refl[1])),
            ("mvd", mvs(rec.mvdl[0])),
            ("mvd1", mvs(rec.mvdl[1])),
            ("cbp", str(rec.cbp_l + 16 * rec.cbp_c) if has_cbp else "-"),
            ("qpd", str(rec.qpd) if rec.qpd_present else "-"),
            ("qp", str(rec.qp)),
            ("coef", coef_string(rec.blocks)),
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
