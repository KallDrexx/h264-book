"""SPOILER: reference motion vector derivation (8.4.1) for P and B macroblocks including DIRECT prediction (Chapter 7.9).
Do not read before your own decoder passes.  It is Chapter 7.8's bmvpred_ref.py (vector prediction for two lists) plus the direct
modes of direct_ref.py, and it works slice by slice INSIDE the decoding loop because direct prediction needs the reference lists
of the slice and the stored motion data of RefPicList1[0].

For the whole picture one entry per 4x4 luma block is kept (PicState): the id of the slice that decoded it (-1 = not decoded yet),
for each list X refIdxLX, mvLX, and rpid = the identity of the picture that refIdxLX named in that block's slice.  A block that does
not use list X (and an intra block) stores refIdxLX = -1, mvLX = (0, 0): as a neighbour it is AVAILABLE but never matches a refIdx.
A finished picture keeps its PicState as the 'motion data' later B pictures read as co-located data.

fill_slice(st, r, ctx) adds to every macroblock record of one P or B slice (results of common/inter_ref.parse_all at level 3):
    rec.grid        16 tuples (ref0, mv0x, mv0y, ref1, mv1x, mv1y) in raster order of the 4x4 blocks (-1, 0, 0 = list not used)
    rec.pred_parts  the prediction blocks in the order they are predicted: dict(x, y, w, h, mode); direct blocks are merged into
                    16x16 / 8x8 / 4x4 blocks of equal motion (the sample result does not depend on how they are cut)
    rec.mvs         one dict per parsed partition (mvp0, mv0, mvp1, mv1) or dict(direct=True) per direct unit
MUTANT switches ONE deliberately wrong rule on (harness self-test); None = correct.
"""
import direct_ref

MUTANT = None


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
        self.rpid = [[None] * n, [None] * n]
        self.slice_lists = {}                 # slice id -> (pid list 0, pid list 1 or None)

    def ambiguous(self):
        """True when the slices of this picture used different reference lists (a co-located read must then use each block's own
        slice lists; ffmpeg keeps just one table per picture and decodes such streams differently from the standard)."""
        ls = list(self.slice_lists.values())
        if len({l[0] for l in ls}) > 1:
            return True
        return len({l[1] for l in ls if l[1] is not None}) > 1

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

    def store(self, sl, addr, x, y, w, h, refs, mvs, pids=(None, None)):
        """refs = (ref0, ref1), mvs = ((x, y), (x, y)); -1 / (0, 0) for a list that is not used; pids = the pictures they name."""
        X0 = (addr % self.wm) * 16 + x
        Y0 = (addr // self.wm) * 16 + y
        for yy in range(Y0, Y0 + h, 4):
            for xx in range(X0, X0 + w, 4):
                i = self.idx(xx, yy)
                self.sl[i] = sl
                for L in (0, 1):
                    self.ref[L][i], self.mvx[L][i], self.mvy[L][i] = refs[L], mvs[L][0], mvs[L][1]
                    self.rpid[L][i] = pids[L] if refs[L] >= 0 else None

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


def pid_of(lst, i):
    return lst[i] if 0 <= i < len(lst) else None


def merge_parts(grid, x0, y0, w, h):
    """Prediction blocks covering the rectangle (x0, y0, w, h) of the macroblock, merged to the coarsest uniform cut (16x16 / 8x8 /
    4x4) of the 16 grid entries."""
    def same(cells):
        return all(grid[c] == grid[cells[0]] for c in cells)
    cells = lambda x, y, ww, hh: [(yy >> 2) * 4 + (xx >> 2) for yy in range(y, y + hh, 4) for xx in range(x, x + ww, 4)]  # noqa: E731
    if same(cells(x0, y0, w, h)):
        return [dict(x=x0, y=y0, w=w, h=h, mode="Direct")]
    if w == 16:
        out = []
        for q in range(4):
            out += merge_parts(grid, x0 + (q & 1) * 8, y0 + (q >> 1) * 8, 8, 8)
        return out
    return [dict(x=x0 + (j & 1) * 4, y=y0 + (j >> 1) * 4, w=4, h=4, mode="Direct") for j in range(4)]


def fill_slice(st, r, ctx):
    """Derive and store the motion of every macroblock of one parsed slice (r = SliceResult); ctx = direct_ref.SliceCtx (lists)."""
    info = r.info
    sl = info.index
    pid0, pid1 = ctx.pid0, ctx.pid1
    if info.slice_type in (0, 1):
        st.slice_lists[sl] = (tuple(pid0), tuple(pid1) if info.slice_type == 1 else None)
    for rec in r.mbs:
        name, a = rec.f["name"], rec.addr
        rec.mvs = []
        rec.pred_parts = None
        if name == "P_Skip":
            mv = st.skip_mv(sl, a)
            st.store(sl, a, 0, 0, 16, 16, (0, -1), (mv, (0, 0)), (pid_of(pid0, 0), None))
            rec.mvs = [dict(mvp0=mv, mv0=mv, mvp1=None, mv1=None)]
        elif name in ("B_Skip", "B_Direct_16x16") or any(p.get("mode") == "Direct" for p in rec.parts):
            mbinfo = direct_ref.spatial_mb(ctx, st, sl, a) if ctx.spatial and ctx.mot1 is not None and pid1 and pid1[0] is not None \
                and pid0 and pid0[0] is not None else None
            if mbinfo is None and ctx.spatial:
                raise direct_ref.DirectInvalid("direct prediction needs RefPicList0[0] and RefPicList1[0]")
            if name in ("B_Skip", "B_Direct_16x16"):
                units = [dict(x=0, y=0, w=16, h=16, mode="Direct")]
            else:
                units = rec.parts
            rec.pred_parts = []
            for p in units:
                if p.get("mode") == "Direct" and direct_ref.MUTANT == "d_sub_direct_as_l0" and name == "B_8x8":
                    _non_direct(st, sl, a, dict(p, mode="L0", ref=0, mvd=(0, 0), ref1=None, mvd1=None), name, rec, ctx)
                elif p.get("mode") == "Direct":
                    quarters = range(4) if p["w"] == 16 else [(p["y"] >> 3) * 2 + (p["x"] >> 3)]
                    for k in quarters:
                        ent = direct_ref.derive(ctx, st, sl, a, k, mbinfo)
                        qx, qy = (k & 1) * 8, (k >> 1) * 8
                        for j, (r0, mv0, r1, mv1) in enumerate(ent):
                            st.store(sl, a, qx + (j & 1) * 4, qy + (j >> 1) * 4, 4, 4, (r0, r1), (mv0, mv1),
                                     (pid_of(pid0, r0), pid_of(pid1, r1)))
                    rec.mvs.append(dict(direct=True))
                else:
                    _non_direct(st, sl, a, p, name, rec, ctx, units.index(p))
            g = st.grid(a)
            rec.grid = g
            if direct_ref.MUTANT == "d_direct_not_stored" and name in ("B_Skip", "B_Direct_16x16"):
                st.store_intra(sl, a)
            rec.pred_parts = []
            for p in units:
                if p.get("mode") == "Direct" and not (direct_ref.MUTANT == "d_sub_direct_as_l0" and name == "B_8x8"):
                    rec.pred_parts += merge_parts(g, p["x"], p["y"], p["w"], p["h"])
                else:
                    rec.pred_parts.append(p)
            continue
        elif name.startswith("P_") or name.startswith("B_"):
            kind = kind_of(name)
            for k, p in enumerate(rec.parts):
                _non_direct(st, sl, a, p, name, rec, ctx, k)
        else:
            st.store_intra(sl, a)
        rec.grid = None if not (name.startswith("P_") or name.startswith("B_")) else st.grid(a)
    return r


def _non_direct(st, sl, a, p, name, rec, ctx, k=0):
    kind = kind_of(name)
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
    pids = (pid_of(ctx.pid0, refs[0]) if refs[0] >= 0 else None, pid_of(ctx.pid1, refs[1]) if refs[1] >= 0 else None)
    st.store(sl, a, p["x"], p["y"], p["w"], p["h"], tuple(refs), tuple(mvs), pids)
    rec.mvs.append(dict(mvp0=mvps[0], mv0=mvs[0] if refs[0] >= 0 else None, mvp1=mvps[1],
                        mv1=mvs[1] if refs[1] >= 0 else None))
