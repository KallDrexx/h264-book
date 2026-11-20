"""SPOILER: reference motion vector prediction (8.4.1) for P and B macroblocks, two reference lists (Chapter 7.8).
Do not read before your own decoder passes.  It is Chapter 7.3's PicState with the data of BOTH lists.

For the whole picture one entry per 4x4 luma block is kept: the id of the slice that decoded it (-1 = not decoded yet) and, for each
list X, refIdxLX and mvLX.  A partition that does not use list X (predFlagLX = 0) and an intra macroblock both store refIdxLX = -1 and
mvLX = (0, 0): a neighbour like that is AVAILABLE (so the C-to-D replacement and the 'B and C unavailable' copy rule do not fire) but
its refIdx never equals the current one (8.4.1.3.2).  Neighbour lookup is purely geometric: sample (X, Y) is 'available' iff it is
inside the picture and its block was already written by the SAME slice.

fill_results(results) adds to every macroblock record of a P or B slice (the results of common/inter_ref.parse_all at level 3):
    rec.grid   16 tuples (ref0, mv0x, mv0y, ref1, mv1x, mv1y) in raster order of the 4x4 blocks (-1, 0, 0 = list not used); None for intra
    rec.mvs    one dict per prediction block (same order as rec.parts): mvp0, mv0, mvp1, mv1 ((x, y) or None for an unused list)
Direct macroblocks / sub-macroblocks (B_Skip, B_Direct_16x16, B_Direct_8x8; Chapter 7.9) raise Direct.

MUTANT switches on one deliberately wrong rule (harness self-test); None = correct.
"""
MUTANT = None


class Direct(Exception):
    pass


def median3(a, b, c):
    return a + b + c - min(a, b, c) - max(a, b, c)


class PicState:
    def __init__(self, wm, hm):
        self.wm, self.hm = wm, hm
        self.W, self.H = wm * 16, hm * 16
        n = (wm * 4) * (hm * 4)
        self.sl = [-1] * n
        self.ref = [[-1] * n, [-1] * n]
        self.mvx = [[0] * n, [0] * n]
        self.mvy = [[0] * n, [0] * n]

    def idx(self, X, Y):
        return (Y >> 2) * (self.wm * 4) + (X >> 2)

    def nb(self, sl, lst, X, Y):
        """-> (available, refIdxLX, mvx, mvy) of the partition covering luma sample (X, Y) of the picture."""
        if X < 0 or Y < 0 or X >= self.W or Y >= self.H:
            return (False, -1, 0, 0)
        i = self.idx(X, Y)
        if self.sl[i] < 0 or self.sl[i] != sl:
            return (False, -1, 0, 0)
        src = 0 if MUTANT == "b_mvp_l1_uses_l0_nb" else lst
        r = self.ref[src][i]
        if r < 0:
            if MUTANT == "b_nb_unused_list_unavail":
                return (False, -1, 0, 0)
            if MUTANT == "b_nb_unused_list_ref0" and self.sl[i] >= 0 and self.ref[1 - src][i] >= 0:
                return (True, 0, 0, 0)
            return (True, -1, 0, 0)
        return (True, r, self.mvx[src][i], self.mvy[src][i])

    def neighbours(self, sl, lst, X0, Y0, pw):
        A = self.nb(sl, lst, X0 - 1, Y0)
        B = self.nb(sl, lst, X0, Y0 - 1)
        C = self.nb(sl, lst, X0 + pw, Y0 - 1)
        if not C[0]:
            C = self.nb(sl, lst, X0 - 1, Y0 - 1)
        return A, B, C

    def median(self, A, B, C, ref):
        if not B[0] and not C[0] and A[0]:
            B = C = A
        match = [n for n in (A, B, C) if n[1] == ref]
        if len(match) == 1:
            return match[0][2], match[0][3]
        return median3(A[2], B[2], C[2]), median3(A[3], B[3], C[3])

    def predict(self, sl, addr, lst, x, y, w, h, ref, kind, idx):
        """8.4.1.3 mvpLX of one partition.  kind: '16x16' | '16x8' | '8x16' | 'sub'; idx = mbPartIdx for 16x8 / 8x16."""
        X0 = (addr % self.wm) * 16 + x
        Y0 = (addr // self.wm) * 16 + y
        A, B, C = self.neighbours(sl, lst, X0, Y0, w)
        if kind == "16x8":
            n = B if idx == 0 else A
            if n[1] == ref:
                return n[2], n[3]
        elif kind == "8x16":
            n = A if idx == 0 else C
            if n[1] == ref:
                return n[2], n[3]
        return self.median(A, B, C, ref)

    def skip_mv(self, sl, addr):
        """8.4.1.1 P_Skip."""
        X0, Y0 = (addr % self.wm) * 16, (addr // self.wm) * 16
        A, B, C = self.neighbours(sl, 0, X0, Y0, 16)
        if not A[0] or not B[0]:
            return 0, 0
        if A[1] == 0 and (A[2], A[3]) == (0, 0):
            return 0, 0
        if B[1] == 0 and (B[2], B[3]) == (0, 0):
            return 0, 0
        return self.median(A, B, C, 0)

    def store(self, sl, addr, x, y, w, h, refs, mvs):
        """refs = (ref0, ref1), mvs = ((x, y), (x, y)); -1 / (0, 0) for a list that is not used."""
        X0 = (addr % self.wm) * 16 + x
        Y0 = (addr // self.wm) * 16 + y
        for yy in range(Y0, Y0 + h, 4):
            for xx in range(X0, X0 + w, 4):
                i = self.idx(xx, yy)
                self.sl[i] = sl
                for L in (0, 1):
                    self.ref[L][i], self.mvx[L][i], self.mvy[L][i] = refs[L], mvs[L][0], mvs[L][1]

    def store_intra(self, sl, addr):
        self.store(sl, addr, 0, 0, 16, 16, (-1, -1), ((0, 0), (0, 0)))

    def grid(self, addr):
        X0, Y0 = (addr % self.wm) * 16, (addr // self.wm) * 16
        out = []
        for by in range(4):
            for bx in range(4):
                i = self.idx(X0 + bx * 4, Y0 + by * 4)
                out.append((self.ref[0][i], self.mvx[0][i], self.mvy[0][i], self.ref[1][i], self.mvx[1][i], self.mvy[1][i]))
        return out


def kind_of(name):
    if name.endswith("16x16"):
        return "16x16"
    if name.endswith("16x8"):
        return "16x8"
    if name.endswith("8x16"):
        return "8x16"
    return "sub"


def fill_results(results):
    """Add rec.grid and rec.mvs to every record of every parsed P / B slice (and rec.f['mv'] strings for convenience)."""
    pics = {}
    for r in results:
        info = r.info
        if info.skip_reason:
            continue
        st = pics.setdefault(info.picture, PicState(info.pic_width_in_mbs, info.pic_size_in_mbs // info.pic_width_in_mbs))
        sl = info.index
        for rec in r.mbs:
            name, a = rec.f["name"], rec.addr
            rec.mvs = []
            if name == "P_Skip":
                mv = st.skip_mv(sl, a)
                st.store(sl, a, 0, 0, 16, 16, (0, -1), (mv, (0, 0)))
                rec.mvs = [dict(mvp0=mv, mv0=mv, mvp1=None, mv1=None)]
            elif name in ("B_Skip", "B_Direct_16x16") or any(p.get("mode") == "Direct" for p in rec.parts):
                raise Direct(f"slice {sl} macroblock {a}: {name} needs direct prediction (Chapter 7.9)")
            elif name.startswith("P_") or name.startswith("B_"):
                kind = kind_of(name)
                for k, p in enumerate(rec.parts):
                    mode = p.get("mode", "L0")
                    idx = p["unit"] if "unit" in p else k
                    idx = idx if kind in ("16x8", "8x16") else 0
                    refs, mvs, mvps = [-1, -1], [(0, 0), (0, 0)], [None, None]
                    for lst in (0, 1):
                        use = mode in ("L0", "Bi") if lst == 0 else mode in ("L1", "Bi")
                        if not use:
                            continue
                        rf = p["ref"] if lst == 0 else p["ref1"]
                        d = p["mvd"] if lst == 0 else p["mvd1"]
                        mvp = st.predict(sl, a, lst, p["x"], p["y"], p["w"], p["h"], rf, kind, idx)
                        mvps[lst] = mvp
                        refs[lst], mvs[lst] = rf, (mvp[0] + d[0], mvp[1] + d[1])
                    # both lists are stored only after both vectors are predicted: list 1 of this partition never sees list 0 of it
                    st.store(sl, a, p["x"], p["y"], p["w"], p["h"], tuple(refs), tuple(mvs))
                    rec.mvs.append(dict(mvp0=mvps[0], mv0=mvs[0] if refs[0] >= 0 else None, mvp1=mvps[1],
                                        mv1=mvs[1] if refs[1] >= 0 else None))
            else:
                st.store_intra(sl, a)
            rec.grid = None if not (name.startswith("P_") or name.startswith("B_")) else st.grid(a)
    return results

