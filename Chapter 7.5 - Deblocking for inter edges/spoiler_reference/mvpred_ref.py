"""SPOILER: reference motion vector prediction (8.4.1) for Chapter 7.3.  Do not read before your own decoder passes.

PicState keeps, for the whole picture, one entry per 4x4 luma block: the id of the slice that decoded it (-1 = not decoded yet),
refIdxL0 (-1 = intra) and the motion vector.  Neighbour lookup is then purely geometric: sample (X, Y) of the picture is
"available" iff it is inside the picture and its block was already written by the SAME slice.  This one rule covers picture
edges, slice edges, "later in decoding order" (C of a partition inside the macroblock) and the macroblock to the right.

fill_results(results) adds the keys `mvp` and `mv` (the level-2 keys of common/inter_ref.py) to every macroblock record.
MUTANT switches on one deliberately wrong rule (used by the harness self-test); None = correct.
"""
MUTANT = None


def median3(a, b, c):
    return a + b + c - min(a, b, c) - max(a, b, c)


class PicState:
    def __init__(self, wm, hm):
        self.wm, self.hm = wm, hm
        self.W, self.H = wm * 16, hm * 16
        n = (wm * 4) * (hm * 4)
        self.sl = [-1] * n
        self.ref = [-1] * n
        self.mvx = [0] * n
        self.mvy = [0] * n
        self.cur_addr = None

    def idx(self, X, Y):
        return (Y >> 2) * (self.wm * 4) + (X >> 2)

    # ---- neighbour lookup: -> (available, refIdx, mvx, mvy)
    def nb(self, sl, X, Y):
        if X < 0 or Y < 0 or X >= self.W or Y >= self.H:
            return (False, -1, 0, 0)
        i = self.idx(X, Y)
        mine = self.cur_addr is not None and (Y >> 4) * self.wm + (X >> 4) == self.cur_addr
        if self.sl[i] < 0 or (self.sl[i] != sl and MUTANT != "slice_edge_ignored"):
            if MUTANT == "c_inside_mb_available" and mine and self.sl[i] < 0:
                return (True, -1, 0, 0)          # wrong: a partition that is not decoded yet counts as available (intra-like)
            return (False, -1, 0, 0)
        if self.ref[i] < 0:
            if MUTANT == "intra_unavailable":
                return (False, -1, 0, 0)
            return (True, -1, 0, 0)               # intra: available, refIdx -1, mv 0
        return (True, self.ref[i], self.mvx[i], self.mvy[i])

    def neighbours(self, sl, X0, Y0, pw):
        A = self.nb(sl, X0 - 1, Y0)
        B = self.nb(sl, X0, Y0 - 1)
        C = self.nb(sl, X0 + pw, Y0 - 1)
        if not C[0] and MUTANT != "no_c_to_d":
            C = self.nb(sl, X0 - 1, Y0 - 1)       # D replaces an unavailable C
        return A, B, C

    # ---- 8.4.1.3.1 median prediction
    def median(self, A, B, C, ref):
        if not B[0] and not C[0] and A[0] and MUTANT != "no_bc_copy":
            B = C = A
        match = [n for n in (A, B, C) if (n[1] == ref if MUTANT != "ref_ge" else n[1] >= 0)]
        if len(match) == 1 and MUTANT != "no_single_ref_rule":
            return match[0][2], match[0][3]
        if MUTANT == "median_is_a":
            return A[2], A[3]
        if MUTANT == "mean_not_median":
            return (A[2] + B[2] + C[2]) // 3, (A[3] + B[3] + C[3]) // 3
        return median3(A[2], B[2], C[2]), median3(A[3], B[3], C[3])

    # ---- 8.4.1.3 mvp of one partition
    def predict(self, sl, addr, x, y, w, h, ref, kind, idx):
        """kind: '16x16' | '16x8' | '8x16' | 'sub' (partition of a P_8x8 sub-macroblock); idx = mbPartIdx for 16x8 / 8x16."""
        self.cur_addr = addr
        X0 = (addr % self.wm) * 16 + x
        Y0 = (addr // self.wm) * 16 + y
        pw = w
        if MUTANT == "sub_width8" and kind == "sub":
            pw = 8
        A, B, C = self.neighbours(sl, X0, Y0, pw)
        if kind == "16x8":
            first, second = (A, B) if MUTANT == "dir16x8_swap" else (B, A)
            n = first if idx == 0 else second
            if n[1] == ref:
                return n[2], n[3]
        elif kind == "8x16":
            first, second = (C, A) if MUTANT == "dir8x16_swap" else (A, C)
            n = first if idx == 0 else second
            if n[1] == ref:
                return n[2], n[3]
        return self.median(A, B, C, ref)

    # ---- 8.4.1.1 P_Skip
    def skip_mv(self, sl, addr):
        self.cur_addr = addr
        X0, Y0 = (addr % self.wm) * 16, (addr // self.wm) * 16
        A, B, C = self.neighbours(sl, X0, Y0, 16)
        if MUTANT == "skip_ignores_availability":
            pass
        elif not A[0] or not B[0]:
            return 0, 0
        if A[1] == 0 and (A[2], A[3]) == (0, 0):
            return 0, 0
        if MUTANT != "skip_zero_a_only" and B[1] == 0 and (B[2], B[3]) == (0, 0):
            return 0, 0
        if MUTANT == "skip_zero_any_ref" and (A[2], A[3]) == (0, 0):
            return 0, 0
        return self.median(A, B, C, 0)

    # ---- storage
    def store(self, sl, addr, x, y, w, h, ref, mv):
        X0 = (addr % self.wm) * 16 + x
        Y0 = (addr // self.wm) * 16 + y
        for yy in range(Y0, Y0 + h, 4):
            for xx in range(X0, X0 + w, 4):
                i = self.idx(xx, yy)
                self.sl[i], self.ref[i], self.mvx[i], self.mvy[i] = sl, ref, mv[0], mv[1]

    def store_intra(self, sl, addr):
        self.store(sl, addr, 0, 0, 16, 16, -1, (0, 0))

    def grid(self, addr):
        """The 16 (ref, mvx, mvy) of macroblock addr, raster order of 4x4 blocks."""
        X0, Y0 = (addr % self.wm) * 16, (addr // self.wm) * 16
        out = []
        for by in range(4):
            for bx in range(4):
                i = self.idx(X0 + bx * 4, Y0 + by * 4)
                out.append((self.ref[i], self.mvx[i], self.mvy[i]))
        return out


def kind_of(name):
    return {"P_L0_16x16": "16x16", "P_L0_L0_16x8": "16x8", "P_L0_L0_8x16": "8x16", "P_8x8": "sub", "P_8x8ref0": "sub"}[name]


def fill_results(results):
    """Add rec.f['mvp'], rec.f['mv'] and rec.grid to every record of every parsed slice (results of inter_ref.parse_all)."""
    pics = {}
    for r in results:
        info = r.info
        if info.skip_reason:
            continue
        st = pics.setdefault(info.picture, PicState(info.pic_width_in_mbs, info.pic_size_in_mbs // info.pic_width_in_mbs))
        sl = info.index
        for rec in r.mbs:
            name = rec.f["name"]
            a = rec.addr
            if name == "P_Skip":
                mv = st.skip_mv(sl, a)
                rec.f["mvp"] = f"{mv[0]}:{mv[1]}"
                st.store(sl, a, 0, 0, 16, 16, 0 if MUTANT != "skip_mv_not_stored" else -1, mv)
            elif name in ("P_L0_16x16", "P_L0_L0_16x8", "P_L0_L0_8x16", "P_8x8", "P_8x8ref0"):
                kind = kind_of(name)
                mvps = []
                for k, p in enumerate(rec.parts):
                    idx = k if kind in ("16x8", "8x16") else 0
                    mvp = st.predict(sl, a, p["x"], p["y"], p["w"], p["h"], p["ref"], kind, idx)
                    mv = (mvp[0] + p["mvd"][0], mvp[1] + p["mvd"][1])
                    st.store(sl, a, p["x"], p["y"], p["w"], p["h"], p["ref"], mv)
                    mvps.append(mvp)
                rec.f["mvp"] = ",".join(f"{x}:{y}" for x, y in mvps)
            else:
                st.store_intra(sl, a)
                rec.f["mvp"] = "-"
            if name.startswith("P_"):
                rec.grid = st.grid(a)
                rec.f["mv"] = ",".join(f"{r_}:{x}:{y}" for r_, x, y in rec.grid)
            else:
                rec.grid = None
                rec.f["mv"] = "-"
    return results
