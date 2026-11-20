#!/usr/bin/env python3
"""SPOILER: complete reference decoder for MBAFF streams (Chapter 10.4): frame pictures with macroblock pairs coded as frame or field
pairs; I, P and B slices; CAVLC and CABAC (the macroblock layer is parsed by mbaff.py, Chapter 10.3's parser); 8-bit 4:2:0,
transform 8x8, flat scaling lists, several slices, explicit / implicit weighted prediction, spatial and temporal direct, deblocking.
Output in display order (ascending POC inside each IDR period).  Do not read before your own decoder passes.

usage: mbdec.py <in.264|.ts> <out.yuv> [--no-deblock] [--nodeblock-out file.yuv] [--mv-dump file] [--frames N]

What is MBAFF specific here (the Readme explains each):
  * a field macroblock covers every second line of its pair (top MB: lines 0, 2, ..; bottom MB: lines 1, 3, ..); its prediction,
    residual and intra neighbours live in those lines; intra neighbours are the nearest samples in the same line parity (Table 6-4
    yields exactly that geometry; only the AVAILABILITY of the samples is derived from the macroblocks it names);
  * inter prediction of a field MB reads reference FIELDS: refIdx >> 1 picks the frame, refIdx & 1 the parity (same / opposite),
    vectors are in field units, the chroma vector gets the Table 8-9 offset when the parities differ;
  * neighbouring motion data is converted between frame and field units (8.4.1.3.2): field MB next to frame MB: vy / 2 (toward
    zero), refIdx * 2; frame MB next to field MB: vy * 2, refIdx >> 1;
  * direct modes pick the co-located macroblock with the frame / field mapping of Table 8-6 (8.4.1.2.1);
  * implicit weights and temporal scaling use field POCs for field MBs; explicit weights use refIdx >> 1;
  * field scans for field MBs; deblocking of mixed frame / field edges (mbdeblock.py).
MUTANT switches ONE deliberately wrong rule on (MUTANTS here and in mbdeblock.py; harness self-test).
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from common import cavlc_ref as CR  # noqa: E402
from common import yuvref  # noqa: E402
import mbaff as M  # noqa: E402
import bslicehdr_ref  # noqa: E402
import brefmgmt_ref  # noqa: E402
import poc_ref  # noqa: E402
import direct_ref  # noqa: E402
import interp_ref  # noqa: E402
import intra4ref  # noqa: E402
import intra8ref  # noqa: E402
import intraref  # noqa: E402
import transref  # noqa: E402
import mbdeblock  # noqa: E402

MUTANT = None
MUTANTS = {
    "line_interleave_halves": "a field MB is written to 16 consecutive lines of its pair half (top field MB: lines 0..15) instead of every second line",
    "line_parity_swap": "the top field MB is written to the odd lines of its pair and the bottom field MB to the even lines",
    "intra_nb_frame_rows": "intra neighbours of a field MB are the lines directly above / left in frame order (the previous frame line) instead of the same parity",
    "intra_left_avail_first": "frame MB with a field pair on the left under constrained intra prediction: only the first left MB decides availability",
    "chroma_dc_left_whole": "chroma DC prediction: left availability is one flag for the whole MB (no per-4x4-block left availability)",
    "ref_parity_swap": "field MB: refIdx & 1 == 0 selects the OPPOSITE parity field",
    "ref_idx_no_halve": "field MB: refIdx indexes the frame list directly (refIdx >> 1 missing)",
    "mv_scale_trunc_floor": "neighbour scaling field MB / frame neighbour: vy >> 1 (floor) instead of division toward zero",
    "mv_scale_swapped": "neighbour scaling: frame MB with field neighbour halves the vector, field MB with frame neighbour doubles it",
    "ref_scale_none": "neighbour refIdx not scaled between frame and field MBs (vectors are)",
    "mv_scale_x": "neighbour scaling also changes the horizontal component",
    "chroma_off_sign": "chroma vector offset for field MBs referring to the opposite parity has the wrong sign",
    "chroma_off_none": "no chroma vector offset for field MBs",
    "field_ref_height": "field MB reference clamping uses a field height one line too short",
    "col_parity_top": "direct: the field MB of the co-located pair for a frame MB is always the top field MB",
    "col_frm_to_fld_row": "direct, field MB with a frame co-located pair: 8x8 block row 1 reads the top MB (not the bottom MB)",
    "col_fld_to_frm_rows": "direct, frame MB with a field co-located pair: the bottom frame MB reads the same 4x4 rows as the top one",
    "col_mv_not_scaled": "temporal direct: mvCol vertical component not converted between frame and field",
    "col_ref_frame_only": "temporal direct, field MB with a field co-located MB: refIdxL0 = 2 * frame index (the parity of the field refPicCol is ignored)",
    "col_zero_scaled": "spatial direct: colZeroFlag uses the converted vertical mvCol",
    "implicit_frame_poc": "implicit weights / temporal scaling of field MBs use frame POCs",
    "wp_refidx_not_halved": "explicit weights of field MBs index the table with refIdx (not refIdx >> 1)",
    "field_scan_missing": "field MBs use the frame zig-zag scans",
    "pskip_no_scale": "P_Skip zero test uses the unscaled neighbour (vector and refIdx of a neighbour of the other kind)",
    "avail_left_naive": "intra availability of the left samples taken from the left MB at the same position in its pair (no Table 6-4)",
}
ALL_MUTANTS = dict(MUTANTS)
ALL_MUTANTS.update(mbdeblock.MUTANTS)

DEBLOCK = True


class Unsupported(Exception):
    pass


def set_mutant(name):
    global MUTANT
    MUTANT = name
    mbdeblock.MUTANT = name if name in mbdeblock.MUTANTS else None


def mut(n):
    return MUTANT == n


# ---------------------------------------------------------------- scans
FIELD4 = [(0, 0), (1, 0), (0, 1), (2, 0), (3, 0), (1, 1), (2, 1), (3, 1), (0, 2), (1, 2), (2, 2), (3, 2), (0, 3), (1, 3), (2, 3), (3, 3)]
_F8 = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (0, 3), (0, 4), (1, 2), (2, 0), (1, 3), (0, 5), (0, 6), (0, 7), (1, 4), (2, 1), (3, 0),
       (2, 2), (1, 5), (1, 6), (1, 7), (2, 3), (3, 1), (4, 0), (3, 2), (2, 4), (2, 5), (2, 6), (2, 7), (3, 3), (4, 1), (5, 0), (4, 2),
       (3, 4), (3, 5), (3, 6), (3, 7), (4, 3), (5, 1), (6, 0), (5, 2), (4, 4), (4, 5), (4, 6), (4, 7), (5, 3), (6, 1), (6, 2), (5, 4),
       (5, 5), (5, 6), (5, 7), (6, 3), (7, 0), (7, 1), (6, 4), (6, 5), (6, 6), (6, 7), (7, 2), (7, 3), (7, 4), (7, 5), (7, 6), (7, 7)]
FIELD8 = [(y, x) for x, y in _F8]           # Table 8-13 is written as (x, y); transref wants (row, column)
FRAME4, FRAME8 = list(transref.ZIGZAG), list(transref.ZIGZAG8)


def set_scan(field):
    f = field and not mut("field_scan_missing")
    transref.ZIGZAG[:] = FIELD4 if f else FRAME4
    transref.ZIGZAG8[:] = FIELD8 if f else FRAME8


IDX4 = [[0] * 4 for _ in range(4)]               # luma4x4BlkIdx of the block at (row, column)
for _i in range(16):
    _bx, _by = M.blk_xy(_i)
    IDX4[_by][_bx] = _i


def tdiv2(v):
    """v / 2 with truncation toward zero (5.7)."""
    return -((-v) >> 1) if v < 0 else v >> 1


def median(a, b, c):
    return a + b + c - min(a, b, c) - max(a, b, c)


# ---------------------------------------------------------------- pictures and per-macroblock records
def new_planes(wm, hm):
    return ([[128] * (16 * wm) for _ in range(16 * hm)], [[128] * (8 * wm) for _ in range(8 * hm)], [[128] * (8 * wm) for _ in range(8 * hm)])


class Frame:
    """A decoded frame (both fields)."""

    def __init__(self, wm, hm, pid):
        self.wm, self.hm, self.pid = wm, hm, pid
        self.W, self.H = 16 * wm, 16 * hm
        self.planes = new_planes(wm, hm)
        self.poc_t = self.poc_b = 0
        self.mbs = {}
        self._f = {}

    @property
    def poc(self):
        return min(self.poc_t, self.poc_b)

    def poc_of(self, par):
        return self.poc if par == 0 else self.poc_t if par == 1 else self.poc_b

    def field(self, par):
        """The planes of the top (1) / bottom (2) field: views onto the frame rows (no copy)."""
        if par not in self._f:
            o = par - 1
            self._f[par] = tuple(pl[o::2] for pl in self.planes)
        return self._f[par]


class MI:
    """What later macroblocks / the deblocking filter / later pictures need to know about a macroblock."""

    def __init__(self, addr, rec, slice_idx):
        self.addr, self.rec, self.slice = addr, rec, slice_idx
        self.fld = rec.fld
        self.par = 1 + (addr & 1)                        # parity of a field MB: 1 top, 2 bottom
        self.intra = bool(rec.intra)
        self.pcm = bool(rec.pcm)
        self.skip = bool(rec.skip)
        self.qp = rec.qp
        self.qpd = 0 if rec.pcm else rec.qp               # QP_Y as the deblocking filter sees it (I_PCM: 0)
        self.t8 = rec.t8
        self.nz = [True] * 16 if (rec.intra or rec.pcm) else [False] * 16
        self.ref = [[-1] * 16, [-1] * 16]
        self.mv = [[(0, 0)] * 16, [(0, 0)] * 16]
        self.rid = [[None] * 16, [None] * 16]
        self.filled = [False] * 16
        self.dbf = (0, 0, 0)


class SliceCtx:
    pass


# ---------------------------------------------------------------- the decoder
class Picture:
    pass


class Dpb:
    def __init__(self, sps):
        self.d = brefmgmt_ref.Dpb(sps["max_num_ref_frames"], sps["log2_max_frame_num_minus4"] + 4,
                                  bool(sps.get("gaps_in_frame_num_allowed_flag", 0)))
        self.pix = {}

    def start_picture(self, frame_num, idr):
        try:
            labels = self.d.check_frame_num(frame_num, idr)
        except brefmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        if labels:
            raise Unsupported("frame_num gap")

    def slots(self, cur_fn, cur_poc, stype, a0, a1, m0, m1):
        try:
            if stype == 0:
                return self.d.build_list(cur_fn, a0, m0), []
            return self.d.build_lists_b(cur_fn, cur_poc, a0, a1, m0, m1)
        except brefmgmt_ref.StreamError as e:
            raise Unsupported(str(e))

    def mark(self, label, pic, frame_num, idr, ref_idc, lt_flag, mmcos, poc):
        try:
            self.d.finish_picture(label, frame_num, idr, ref_idc, lt_flag, mmcos, poc)
        except brefmgmt_ref.StreamError as e:
            raise Unsupported(str(e))
        if ref_idc:
            self.pix[label] = pic
        live = {f.label for f in self.d.frames}
        for k in list(self.pix):
            if k not in live:
                del self.pix[k]


def clip1(v):
    return 0 if v < 0 else 255 if v > 255 else v


class Decoder:
    def __init__(self, infos, parsed, pixels=True, deblock=True, stats=None):
        self.infos, self.parsed = infos, parsed
        self.pixels, self.deblock = pixels, deblock and DEBLOCK
        self.stats = stats

    def st(self, key):
        if self.stats is not None and MUTANT is None:
            self.stats[key] = self.stats.get(key, 0) + 1

    # ------------------------------------------------------------ geometry
    def geom(self, mi):
        """-> (x0, y0, dy, cx0, cy0): first luma line / column of the macroblock, line step; chroma likewise."""
        pair = mi.addr >> 1
        bot = mi.addr & 1
        mbx, pr = pair % self.wm, pair // self.wm
        if mi.fld:
            if mut("line_interleave_halves"):
                return 16 * mbx, 32 * pr + 16 * bot, 1, 8 * mbx, 16 * pr + 8 * bot, 1
            par = bot ^ 1 if mut("line_parity_swap") else bot
            return 16 * mbx, 32 * pr + par, 2, 8 * mbx, 16 * pr + par, 2
        return 16 * mbx, 32 * pr + 16 * bot, 1, 8 * mbx, 16 * pr + 8 * bot, 1

    # ------------------------------------------------------------ neighbour availability (6.4.12.2 through mbaff.Slice.nb)
    def avail(self, sc, addr, xN, yN, mw=16, mh=16):
        if mut("avail_left_naive") and xN < 0 and yN >= 0 and (addr >> 1) % self.wm:
            n = (2 * ((addr >> 1) - 1) + (addr & 1), 0, 0) if 2 * ((addr >> 1) - 1) in sc.recs else None
        else:
            n = sc.sl.nb(addr, xN, yN, mw, mh)
        if n is None:
            return False
        if sc.cip and not sc.recs[n[0]].intra:
            return False
        return True

    # ------------------------------------------------------------ one picture
    def decode_picture(self, rs, dpb, pid, raw_poc_state):
        s0 = rs[0][0]
        w = s0.pic_width_in_mbs
        h = s0.pic_size_in_mbs // w
        sps = s0.sps
        self.wm, self.hm = w, h
        fr = Frame(w, h, pid)
        fr.poc_t, fr.poc_b = raw_poc_state
        self.fr = fr
        self.planes = fr.planes
        dpb.start_picture(s0.hdr["frame_num"], s0.nal_type == 5)
        self.cur_fn = s0.hdr["frame_num"]
        o1 = s0.pps["chroma_qp_index_offset"]
        self.offs = (o1, s0.pps.get("second_chroma_qp_index_offset", o1))
        self.slice_types = []
        for info, tab, sl, recs in rs:
            self.decode_slice(info, tab, sl, recs, dpb, fr)
        if self.pixels and self.deblock:
            mbdeblock.filter_picture(fr.planes, w, fr.mbs, self.offs)
        t0 = rs[0][1]
        dpb.mark(pid, fr, self.cur_fn, s0.nal_type == 5, t0["nal_ref_idc"], t0["lt_flag"], t0["mmco"], fr.poc)
        return fr

    def decode_slice(self, info, tab, sl, recs, dpb, fr):
        sc = SliceCtx()
        sc.info, sc.tab, sc.sl, sc.recs = info, tab, sl, sl.recs
        sc.t = sl.t
        sc.cip = sl.cip
        sc.pps, sc.sps, sc.hdr = info.pps, info.sps, info.hdr
        sc.idx = info.index
        self.slice_types.append(sc.t)
        st = info.slice_type % 5
        sc.L = [[], []]
        sc.lt = [[], []]
        sc.wt = None
        sc.implicit = False
        sc.spatial = bool(info.hdr.get("direct_spatial_mv_pred_flag", 0))
        if sc.t in ("P", "B"):
            slots = dpb.slots(self.cur_fn, fr.poc, st, tab["active0"], tab["active1"], tab["mods0"], tab["mods1"])
            for k in (0, 1):
                for s in slots[k]:
                    if s is None:
                        sc.L[k].append(None)
                        sc.lt[k].append(False)
                    else:
                        sc.L[k].append(dpb.pix[s[0]])
                        sc.lt[k].append(s[1] == "L")
            sc.wt = tab["wt"]
            sc.implicit = sc.t == "B" and tab["bipred_idc"] == 2
        facts = (info.hdr.get("disable_deblocking_filter_idc", 0), 2 * info.hdr.get("slice_alpha_c0_offset_div2", 0),
                 2 * info.hdr.get("slice_beta_offset_div2", 0))
        self.sc = sc
        for rec in recs:
            mi = MI(rec.addr, rec, sc.idx)
            mi.dbf = facts
            fr.mbs[rec.addr] = mi
            set_scan(bool(rec.fld))
            self.macroblock(sc, rec, mi)
        # the co-located data of later pictures need the slice type of this picture; nothing else is slice specific

    # ------------------------------------------------------------ macroblock dispatch
    def macroblock(self, sc, rec, mi):
        if rec.pcm:
            self.pcm_mb(rec, mi)
            for lst in (0, 1):
                pass
            return
        if rec.intra:
            self.intra_mb(sc, rec, mi)
        else:
            self.inter_mb(sc, rec, mi)

    # ------------------------------------------------------------ sample access
    def put_rows(self, plane, x, ybase, dy, block):
        for r, row in enumerate(block):
            plane[ybase + dy * r][x:x + len(row)] = row

    def pcm_mb(self, rec, mi):
        if not self.pixels:
            return
        x0, y0, dy, cx0, cy0, cdy = self.geom(mi)
        d = rec.pcm_samples
        self.put_rows(self.planes[0], x0, y0, dy, [list(d[16 * k:16 * k + 16]) for k in range(16)])
        for p in range(2):
            b = 256 + 64 * p
            self.put_rows(self.planes[1 + p], cx0, cy0, cdy, [list(d[b + 8 * k:b + 8 * k + 8]) for k in range(8)])

    # ------------------------------------------------------------ intra
    def blockmap(self, rec):
        return {(b["kind"], b["idx"]): b["coeffs"] for b in rec.blocks}

    @staticmethod
    def lv(bm, kind, idx, n):
        c = bm.get((kind, idx))
        if c is None:
            return [0] * n
        return list(c) + [0] * (n - len(c)) if len(c) < n else c

    def intra_mb(self, sc, rec, mi):
        ival = rec.mbt if sc.t == "I" else rec.mbt - (5 if sc.t == "P" else 23)
        self.st("intra mb %s" % ("field" if mi.fld else "frame"))
        if not self.pixels:
            return
        x0, y0, dy, cx0, cy0, cdy = self.geom(mi)
        bm = self.blockmap(rec)
        tr = transref.Tracker()
        qp = rec.qp
        addr = mi.addr
        Y = self.planes[0]
        # the nearest line above a macroblock / block (same parity for field MBs)
        up = 1 if mut("intra_nb_frame_rows") and mi.fld else dy

        def pix(xN, yN):
            return Y[y0 + (dy * yN if yN >= 0 else -up)][x0 + xN]

        if rec.i_nxn:
            nxn8 = bool(rec.t8)
            nb = 4 if nxn8 else 16
            for i in range(nb):
                if nxn8:
                    x, y, n = (i & 1) * 8, (i >> 1) * 8, 8
                else:
                    bx, by = M.blk_xy(i)
                    x, y, n = 4 * bx, 4 * by, 4

                def A(dx, dy_, x=x, y=y, i=i):
                    xN, yN = x + dx, y + dy_
                    if 0 <= xN < 16 and 0 <= yN < 16:        # inside the MB: available when that block is decoded already
                        return ((yN >> 3) * 2 + (xN >> 3) if nxn8 else IDX4[yN >> 2][xN >> 2]) < i
                    return self.avail(sc, addr, xN, yN)
                a = all(A(-1, k) for k in range(n))
                b = A(0, -1)
                c = A(n, -1)
                d = A(-1, -1)
                win = [[0] * (2 * n + 1) for _ in range(n + 1)]
                if d:
                    win[0][0] = pix(x - 1, y - 1)
                if b:
                    for k in range(n):
                        win[0][1 + k] = pix(x + k, y - 1)
                    if c:
                        for k in range(n):
                            win[0][1 + n + k] = pix(x + n + k, y - 1)
                if a:
                    for k in range(n):
                        win[1 + k][0] = pix(x - 1, y + k)
                mode = rec.pm[i]
                try:
                    pred = intra8ref.pred8(win, 1, 1, mode, a, b, c, d) if nxn8 else intra4ref.pred4(win, 1, 1, mode, a, b, c, d)
                except ValueError as e:
                    raise Unsupported(f"MB {addr}: {e}")
                self.st("intra %dx%d mode %d" % (n, n, mode))
                if nxn8:
                    lv = self.lv(bm, "luma8", i, 64)
                    res = transref.residual8x8(lv, qp, tr) if any(lv) else None
                else:
                    lv = self.lv(bm, "luma", i, 16)
                    res = transref.residual4x4(transref.inverse_zigzag(lv), qp, False, tr) if any(lv) else None
                for r in range(n):
                    row = Y[y0 + dy * (y + r)]
                    prow = pred[r]
                    if res is None:
                        row[x0 + x:x0 + x + n] = prow
                    else:
                        rr = res[r]
                        row[x0 + x:x0 + x + n] = [clip1(prow[k] + rr[k]) for k in range(n)]
        else:
            mode = (ival - 1) % 4
            a = all(self.avail(sc, addr, -1, k) for k in range(16))
            if mut("intra_left_avail_first") and not mi.fld:
                a = self.avail(sc, addr, -1, 0)
            b = self.avail(sc, addr, 0, -1)
            d = self.avail(sc, addr, -1, -1)
            win = [[0] * 17 for _ in range(17)]
            if d:
                win[0][0] = pix(-1, -1)
            if b:
                for k in range(16):
                    win[0][1 + k] = pix(k, -1)
            if a:
                for k in range(16):
                    win[1 + k][0] = pix(-1, k)
            try:
                pred = intraref.i16_pred(win, 1, 1, mode, a, b, d)
            except ValueError as e:
                raise Unsupported(f"MB {addr}: {e}")
            self.st("intra 16x16 mode %d" % mode)
            res = transref.i16_residual(qp, self.lv(bm, "i16dc", 0, 16), [self.lv(bm, "i16ac", k, 15) for k in range(16)], tr)
            for r in range(16):
                Y[y0 + dy * r][x0:x0 + 16] = [clip1(pred[r][k] + res[r][k]) for k in range(16)]
        self.chroma_intra(sc, rec, mi, bm, tr)
        if not tr.range_ok:
            self.st("coefficient range violation")

    def chroma_intra(self, sc, rec, mi, bm, tr):
        x0, y0, dy, cx0, cy0, cdy = self.geom(mi)
        addr = mi.addr
        mode = rec.cpred
        la = [all(self.avail(sc, addr, -1, 4 * g + k, 8, 8) for k in range(4)) for g in range(2)]
        if mut("chroma_dc_left_whole"):
            la = [la[0] and la[1]] * 2
        ta = self.avail(sc, addr, 0, -1, 8, 8)
        da = self.avail(sc, addr, -1, -1, 8, 8)
        for p in range(2):
            plane = self.planes[1 + p]
            up = 1 if mut("intra_nb_frame_rows") and mi.fld else cdy

            def pix(xN, yN):
                return plane[cy0 + (cdy * yN if yN >= 0 else -up)][cx0 + xN]
            top = [pix(k, -1) for k in range(8)] if ta else None
            left = [pix(-1, k) for k in range(8)] if (la[0] or la[1]) else None
            if mode == 0:
                pred = [[0] * 8 for _ in range(8)]
                for blk in range(4):
                    xo, yo = 4 * (blk & 1), 4 * (blk >> 1)
                    lav = la[yo >> 2]
                    st = sum(top[xo:xo + 4]) if ta else None
                    sl = sum(left[yo:yo + 4]) if lav else None
                    if (xo, yo) in ((0, 0), (4, 4)):
                        v = (st + sl + 4) >> 3 if (ta and lav) else (sl + 2) >> 2 if lav else (st + 2) >> 2 if ta else 128
                    elif yo == 0:
                        v = (st + 2) >> 2 if ta else (sl + 2) >> 2 if lav else 128
                    else:
                        v = (sl + 2) >> 2 if lav else (st + 2) >> 2 if ta else 128
                    for y in range(4):
                        for x in range(4):
                            pred[yo + y][xo + x] = v
            else:
                a = la[0] and la[1]
                win = [[0] * 9 for _ in range(9)]
                if da:
                    win[0][0] = pix(-1, -1)
                if ta:
                    win[0][1:9] = top
                if a:
                    for k in range(8):
                        win[1 + k][0] = left[k]
                try:
                    pred = intraref.chroma_pred(win, 1, 1, mode, a, ta, da)
                except ValueError as e:
                    raise Unsupported(f"MB {addr}: chroma {e}")
            name = ("cb", "cr")[p]
            qpc = transref.chroma_qp(rec.qp, self.offs[p])
            if rec.cbp_c:
                res = transref.chroma_residual(qpc, self.lv(bm, name + "dc", 0, 4), [self.lv(bm, name + "ac", k, 15) for k in range(4)], tr)
            else:
                res = None
            for r in range(8):
                row = plane[cy0 + cdy * r]
                row[cx0:cx0 + 8] = pred[r] if res is None else [clip1(pred[r][k] + res[r][k]) for k in range(8)]

    # ------------------------------------------------------------ motion data of neighbours (8.4.1.3.2)
    def nbi(self, sc, mi, lst, xN, yN):
        """-> None (partition not available) or (refIdx, (mvx, mvy)) as seen from the current macroblock."""
        if 0 <= xN < 16 and 0 <= yN < 16:
            cell = (yN >> 2) * 4 + (xN >> 2)
            if not mi.filled[cell]:
                return None
            return mi.ref[lst][cell], mi.mv[lst][cell]
        n = sc.sl.nb(mi.addr, xN, yN)
        if n is None:
            return None
        m = self.fr.mbs[n[0]]
        if m.intra:
            return -1, (0, 0)
        cell = (n[2] >> 2) * 4 + (n[1] >> 2)
        r = m.ref[lst][cell]
        if r < 0:
            return -1, (0, 0)
        mx, my = m.mv[lst][cell]
        if mut("pskip_no_scale") and getattr(self, "_pskip", False):
            return r, (mx, my)
        if mi.fld and not m.fld:
            if mut("mv_scale_swapped"):
                return r >> 1, (mx, my * 2)
            my = my >> 1 if mut("mv_scale_trunc_floor") else tdiv2(my)
            if mut("mv_scale_x"):
                mx = tdiv2(mx)
            r = r if mut("ref_scale_none") else r * 2
        elif not mi.fld and m.fld:
            if mut("mv_scale_swapped"):
                return r * 2, (mx, tdiv2(my))
            my = my * 2
            if mut("mv_scale_x"):
                mx = mx * 2
            r = r if mut("ref_scale_none") else r >> 1
        return r, (mx, my)

    def mvp(self, sc, mi, lst, x4, y4, w4, ref, shape=None, part=0):
        """8.4.1.3: predicted vector of the partition whose top-left 4x4 block is (x4, y4), width w4 blocks."""
        x, y, w = 4 * x4, 4 * y4, 4 * w4
        A = self.nbi(sc, mi, lst, x - 1, y)
        B = self.nbi(sc, mi, lst, x, y - 1)
        C = self.nbi(sc, mi, lst, x + w, y - 1)
        if C is None:
            C = self.nbi(sc, mi, lst, x - 1, y - 1)
        if shape == "16x8":
            if part == 0 and B is not None and B[0] == ref:
                return B[1]
            if part == 1 and A is not None and A[0] == ref:
                return A[1]
        elif shape == "8x16":
            if part == 0 and A is not None and A[0] == ref:
                return A[1]
            if part == 1 and C is not None and C[0] == ref:
                return C[1]
        if B is None and C is None and A is not None:
            B = C = A
        ent = [e if e is not None else (-1, (0, 0)) for e in (A, B, C)]
        hit = [e for e in ent if e[0] == ref]
        if len(hit) == 1:
            return hit[0][1]
        return (median(ent[0][1][0], ent[1][1][0], ent[2][1][0]), median(ent[0][1][1], ent[1][1][1], ent[2][1][1]))

    # ------------------------------------------------------------ reference identification
    def ident(self, sc, mi, lst, r):
        L = sc.L[lst]
        if mi.fld:
            fi = r if mut("ref_idx_no_halve") else r >> 1
            f = L[min(fi, len(L) - 1)] if mut("ref_idx_no_halve") else (L[fi] if fi < len(L) else None)
            same = (r & 1) == 0
            if mut("ref_parity_swap"):
                same = not same
            par = mi.par if same else 3 - mi.par
        else:
            f = L[r] if r < len(L) else None
            par = 0
        if f is None:
            raise Unsupported(f"refIdx {r} of list {lst} names 'no reference picture'")
        return f, par

    def poc_cur(self, mi):
        if mi.fld and not mut("implicit_frame_poc"):
            return self.fr.poc_t if mi.par == 1 else self.fr.poc_b
        return self.fr.poc

    # ------------------------------------------------------------ inter macroblocks
    def fill(self, mi, cells, lst, ref, mv):
        for c in cells:
            mi.ref[lst][c] = ref
            mi.mv[lst][c] = mv

    @staticmethod
    def cells(x4, y4, w4, h4):
        return [(y4 + j) * 4 + x4 + i for j in range(h4) for i in range(w4)]

    def inter_mb(self, sc, rec, mi):
        t = sc.t
        mbt = rec.mbt
        quad_cells = [self.cells(2 * (q & 1), 2 * (q >> 1), 2, 2) for q in range(4)]
        self.st("inter mb %s %s" % (t, "field" if mi.fld else "frame"))
        if rec.skip and t == "P":
            self._pskip = True
            A = self.nbi(sc, mi, 0, -1, 0)
            B = self.nbi(sc, mi, 0, 0, -1)
            self._pskip = False
            if mut("pskip_no_scale"):
                self._pskip = True
                A = self.nbi(sc, mi, 0, -1, 0)
                B = self.nbi(sc, mi, 0, 0, -1)
                self._pskip = False
            if A is None or B is None or (A[0] == 0 and A[1] == (0, 0)) or (B[0] == 0 and B[1] == (0, 0)):
                mv = (0, 0)
                self.st("P_Skip zero mv")
            else:
                mv = self.mvp(sc, mi, 0, 0, 0, 4, 0)
            self.fill(mi, range(16), 0, 0, mv)
            mi.filled = [True] * 16
        elif rec.skip or (t == "B" and mbt == 0):
            self.direct_prep(sc, mi)
            for q in range(4):
                self.direct_quad(sc, mi, q, quad_cells[q])
            mi.filled = [True] * 16
        else:
            self.partitions(sc, rec, mi)
        # reference identities
        for lst in (0, 1):
            for c in range(16):
                if mi.ref[lst][c] >= 0:
                    f, par = self.ident(sc, mi, lst, mi.ref[lst][c])
                    mi.rid[lst][c] = (f.pid, par)
        if not self.pixels:
            self.finish_flags(rec, mi, None)
            return
        bm = self.blockmap(rec)
        self.predict_and_residual(sc, rec, mi, bm)

    def partitions(self, sc, rec, mi):
        t = sc.t
        mbt = rec.mbt
        uses = lambda mode, lst: mode in (("L0", "Bi") if lst == 0 else ("L1", "Bi"))   # noqa: E731
        p8 = (3, 4) if t == "P" else (22,)
        parts = []          # (rect (x4, y4, w4, h4), b8 or None, direct, modes tuple-of-modes, shape, idx)
        if mbt in p8:
            tab = M.P_SUB if t == "P" else M.B_SUB
            for b8 in range(4):
                shape, mode = tab[rec.sub[b8]]
                ox, oy = (b8 & 1) * 2, (b8 >> 1) * 2
                if mode == "Direct":
                    parts.append(((ox, oy, 2, 2), b8, True, None))
                else:
                    for (x, y, w, h) in shape:
                        parts.append(((ox + x, oy + y, w, h), b8, False, mode))
            ref_keys = [(b8, None) for b8 in range(4) if not (tab[rec.sub[b8]][1] == "Direct")]
            ref_modes = {b8: tab[rec.sub[b8]][1] for b8 in range(4)}
            shape_name = None
        else:
            rects, modes = (M.P_TYPES if t == "P" else M.B_TYPES)[mbt]
            for k, (rect, mode) in enumerate(zip(rects, modes)):
                parts.append((rect, k, False, mode))
            ref_modes = {k: modes[k] for k in range(len(rects))}
            shape_name = "16x8" if rects == M.R168 else "8x16" if rects == M.R816 else None
        # consume ref_idx / mvd in the parser's order: all refs of list 0, all of list 1, mvds of list 0, list 1
        refv, mvdv = {}, {}
        for lst in (0, 1):
            pos = 0
            for key in sorted(ref_modes):
                if ref_modes[key] != "Direct" and uses(ref_modes[key], lst):
                    refv[(lst, key)] = rec.refl[lst][pos]
                    pos += 1
        for lst in (0, 1):
            pos = 0
            for pi, (rect, key, direct, mode) in enumerate(parts):
                if not direct and uses(mode, lst):
                    mvdv[(lst, pi)] = rec.mvdl[lst][pos]
                    pos += 1
        spatial_ready = False
        for pi, (rect, key, direct, mode) in enumerate(parts):
            x4, y4, w4, h4 = rect
            cells = self.cells(x4, y4, w4, h4)
            if direct:
                if not spatial_ready:
                    self.direct_prep(sc, mi)
                    spatial_ready = True
                self.direct_quad(sc, mi, key, cells)
                for c in cells:
                    mi.filled[c] = True
                continue
            for lst in (0, 1):
                if not uses(mode, lst):
                    continue
                ref = refv[(lst, key)]
                if mbt in p8:
                    mvp = self.mvp(sc, mi, lst, x4, y4, w4, ref)
                else:
                    mvp = self.mvp(sc, mi, lst, x4, y4, w4, ref, shape_name, key)
                d = mvdv[(lst, pi)]
                self.fill(mi, cells, lst, ref, (mvp[0] + d[0], mvp[1] + d[1]))
            for c in cells:
                mi.filled[c] = True

    # ------------------------------------------------------------ direct prediction
    def direct_prep(self, sc, mi):
        """Per-macroblock part of spatial direct (8.4.1.2.2): reference indices and predicted vectors."""
        mi.dz = False
        if not sc.spatial:
            return
        refs = [-1, -1]
        for lst in (0, 1):
            A = self.nbi(sc, mi, lst, -1, 0)
            B = self.nbi(sc, mi, lst, 0, -1)
            C = self.nbi(sc, mi, lst, 16, -1)
            if C is None:
                C = self.nbi(sc, mi, lst, -1, -1)

            def mp(a, b):
                return min(a, b) if a >= 0 and b >= 0 else max(a, b)
            ra, rb, rc = (e[0] if e is not None else -1 for e in (A, B, C))
            refs[lst] = mp(ra, mp(rb, rc))
        mi.sref = refs
        if refs[0] < 0 and refs[1] < 0:
            mi.dz = True
            mi.sref = [0, 0]
            mi.smv = [(0, 0), (0, 0)]
            self.st("spatial direct: zero prediction")
        else:
            mi.smv = [self.mvp(sc, mi, lst, 0, 0, 4, refs[lst]) if refs[lst] >= 0 else (0, 0) for lst in (0, 1)]

    def col_block(self, sc, mi, y8, x8):
        """8.4.1.2.1 with direct_8x8_inference: -> (MI of the co-located macroblock, 4x4 cell, vertMvScale)."""
        F1 = sc.L[1][0]
        if F1 is None:
            raise Unsupported("RefPicList1[0] missing")
        pair = mi.addr >> 1
        colfld = bool(F1.mbs[2 * pair].fld)
        top = (mi.addr & 1) == 0
        if not mi.fld:
            if not colfld:
                return F1.mbs[mi.addr], (3 * y8) * 4 + 3 * x8, "one"
            cur = self.fr.poc
            dt, db = abs(F1.poc_t - cur), abs(F1.poc_b - cur)
            cp = 1 if dt >= db else 0
            if mut("col_parity_top"):
                cp = 0
            row = (0 if top else 2) + y8
            if mut("col_fld_to_frm_rows"):
                row = y8
            return F1.mbs[2 * pair + cp], row * 4 + 3 * x8, "f2f"
        if colfld:
            return F1.mbs[mi.addr], (3 * y8) * 4 + 3 * x8, "one"
        row = 0 if y8 == 0 else 2
        mb = F1.mbs[2 * pair + y8]
        if mut("col_frm_to_fld_row"):
            mb = F1.mbs[2 * pair]
        return mb, row * 4 + 3 * x8, "t2f"

    def direct_quad(self, sc, mi, q, cells):
        y8, x8 = q >> 1, q & 1
        if sc.spatial:
            colzero = False
            if not mi.dz:
                F1 = sc.L[1][0]
                if F1 is None:
                    raise Unsupported("RefPicList1[0] missing")
                if not sc.lt[1][0]:
                    cm, cell, vs = self.col_block(sc, mi, y8, x8)
                    if not cm.intra:
                        l = 0 if cm.ref[0][cell] >= 0 else 1
                        rc, mvc = cm.ref[l][cell], cm.mv[l][cell]
                        if mut("col_zero_scaled"):
                            mvc = (mvc[0], mvc[1] * 2 if vs == "f2f" else tdiv2(mvc[1]) if vs == "t2f" else mvc[1])
                        colzero = rc == 0 and abs(mvc[0]) <= 1 and abs(mvc[1]) <= 1
                if colzero:
                    self.st("spatial direct: colZero")
            for lst in (0, 1):
                r = mi.sref[lst]
                if mi.dz:
                    self.fill(mi, cells, lst, 0, (0, 0))
                elif r < 0:
                    self.fill(mi, cells, lst, -1, (0, 0))
                elif r == 0 and colzero:
                    self.fill(mi, cells, lst, 0, (0, 0))
                else:
                    self.fill(mi, cells, lst, r, mi.smv[lst])
            return
        # ---- temporal
        F1 = sc.L[1][0]
        cm, cell, vs = self.col_block(sc, mi, y8, x8)
        self.st("temporal direct %s" % vs)
        if cm.intra:
            ref0, mvc = 0, (0, 0)
        else:
            l = 0 if cm.ref[0][cell] >= 0 else 1
            rid = cm.rid[l][cell]
            mvc = cm.mv[l][cell]
            my = mvc[1]
            if vs == "t2f" and not mut("col_mv_not_scaled"):
                my = tdiv2(my)
            elif vs == "f2f" and not mut("col_mv_not_scaled"):
                my = my * 2
            mvc = (mvc[0], my)
            fpid, par = rid
            L0 = sc.L[0]
            fi = next((i for i, f in enumerate(L0) if f is not None and f.pid == fpid), None)
            if fi is None:
                raise Unsupported("temporal direct: the co-located reference is not in RefPicList0")
            if not mi.fld:
                ref0 = fi
            elif vs == "t2f":
                ref0 = 2 * fi
            elif mut("col_ref_frame_only"):
                ref0 = 2 * fi
            else:
                ref0 = 2 * fi + (0 if par == mi.par else 1)
        f0, p0 = self.ident(sc, mi, 0, ref0)
        f1, p1 = self.ident(sc, mi, 1, 0)
        cur = self.poc_cur(mi)
        poc0, poc1 = f0.poc_of(p0), f1.poc_of(p1)
        lt0 = sc.lt[0][ref0 >> 1 if mi.fld else ref0]
        if lt0 or poc1 - poc0 == 0:
            mv0, mv1 = mvc, (0, 0)
        else:
            dsf = direct_ref.dist_scale_factor(cur, poc0, poc1)
            mv0 = ((dsf * mvc[0] + 128) >> 8, (dsf * mvc[1] + 128) >> 8)
            mv1 = (mv0[0] - mvc[0], mv0[1] - mvc[1])
        self.fill(mi, cells, 0, ref0, mv0)
        self.fill(mi, cells, 1, 0, mv1)

    # ------------------------------------------------------------ prediction
    def chroma_offset(self, mi, ref_par):
        if not mi.fld or mut("chroma_off_none"):
            return 0
        if ref_par == mi.par:
            return 0
        off = 2 if mi.par == 2 else -2           # bottom field MB referring to the top field: +2; top referring to bottom: -2
        return -off if mut("chroma_off_sign") else off

    def predict_block(self, sc, mi, lst, ref, mv, X, Y, w, h):
        """-> (luma block, cb block, cr block) of one prediction block; X, Y = position in the picture / field of the MB's lines."""
        f, par = self.ident(sc, mi, lst, ref)
        if par == 0:
            planes, H = f.planes, f.H
        else:
            planes, H = f.field(par), (f.H // 2 - 1 if mut("field_ref_height") else f.H // 2)
        W = f.W
        yb = interp_ref.luma_block(planes[0], W, H, X, Y, w, h, mv[0], mv[1])
        cmy = mv[1] + self.chroma_offset(mi, par)
        cb = interp_ref.chroma_block(planes[1], W // 2, H // 2, X // 2, Y // 2, w // 2, h // 2, mv[0], cmy)
        cr = interp_ref.chroma_block(planes[2], W // 2, H // 2, X // 2, Y // 2, w // 2, h // 2, mv[0], cmy)
        return yb, cb, cr

    def wp_index(self, mi, r, n=None):
        if not mi.fld:
            return r
        return min(r, (n or r + 1) - 1) if mut("wp_refidx_not_halved") else r >> 1

    def combine(self, sc, mi, blocks, refs, plane):
        """blocks: [(lst, block)]; refs: {lst: refIdx}.  Default / explicit / implicit weighting -> block."""
        wt = sc.wt
        if len(blocks) == 1:
            lst, b = blocks[0]
            if wt is None:
                return b
            e = wt[f"entries{lst}"][self.wp_index(mi, refs[lst], len(wt[f"entries{lst}"]))]
            w, o, lw = self.entry(wt, e, plane)
            return [[clip1(((v * w + (1 << (lw - 1))) >> lw) + o) if lw >= 1 else clip1(v * w + o) for v in row] for row in b]
        b0, b1 = blocks[0][1], blocks[1][1]
        if sc.implicit:
            f0, p0 = self.ident(sc, mi, 0, refs[0])
            f1, p1 = self.ident(sc, mi, 1, refs[1])
            cur = self.poc_cur(mi)
            lt0 = sc.lt[0][refs[0] >> 1 if mi.fld else refs[0]]
            lt1 = sc.lt[1][refs[1] >> 1 if mi.fld else refs[1]]
            w0, w1 = direct_ref.implicit_weights(cur, f0.poc_of(p0), f1.poc_of(p1), lt0, lt1)
            if (w0, w1) != (32, 32):
                self.st("implicit weights %s" % ("field" if mi.fld else "frame"))
            return [[direct_ref.implicit_combine(x, y, w0, w1) for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]
        if wt is None:
            return [[(x + y + 1) >> 1 for x, y in zip(r0, r1)] for r0, r1 in zip(b0, b1)]
        e0 = wt["entries0"][self.wp_index(mi, refs[0], len(wt["entries0"]))]
        e1 = wt["entries1"][self.wp_index(mi, refs[1], len(wt["entries1"]))]
        w0, o0, lw = self.entry(wt, e0, plane)
        w1, o1, _ = self.entry(wt, e1, plane)
        return [[clip1(((x * w0 + y * w1 + (1 << lw)) >> (lw + 1)) + ((o0 + o1 + 1) >> 1)) for x, y in zip(r0, r1)]
                for r0, r1 in zip(b0, b1)]

    @staticmethod
    def entry(wt, e, plane):
        if plane == 0:
            return e["wy"], e["oy"], wt["denom_y"]
        return (e["wcb"], e["ocb"], wt["denom_c"]) if plane == 1 else (e["wcr"], e["ocr"], wt["denom_c"])

    def predict_and_residual(self, sc, rec, mi, bm):
        x0, y0, dy, cx0, cy0, cdy = self.geom(mi)
        pr = (mi.addr >> 1) // self.wm
        if mi.fld:
            Ybase = 16 * pr                      # first line of the MB inside its field
        else:
            Ybase = 32 * pr + 16 * (mi.addr & 1)
        pred = ([[0] * 16 for _ in range(16)], [[0] * 8 for _ in range(8)], [[0] * 8 for _ in range(8)])

        def same(c1, c2):
            return all(mi.ref[l][c1] == mi.ref[l][c2] and mi.mv[l][c1] == mi.mv[l][c2] for l in (0, 1))
        # regions: whole MB, 8x8 quadrants, 4x4 blocks (uniform vectors / references)
        regions = []
        if all(same(0, c) for c in range(1, 16)):
            regions.append((0, 0, 16))
        else:
            for q in range(4):
                qx, qy = (q & 1) * 8, (q >> 1) * 8
                c0 = (qy >> 2) * 4 + (qx >> 2)
                if all(same(c0, c) for c in self.cells(qx >> 2, qy >> 2, 2, 2)):
                    regions.append((qx, qy, 8))
                else:
                    for j in range(2):
                        for i in range(2):
                            regions.append((qx + 4 * i, qy + 4 * j, 4))
        for (x, y, n) in regions:
            cell = (y >> 2) * 4 + (x >> 2)
            blocks = [[], [], []]
            refs = {}
            for lst in (0, 1):
                r = mi.ref[lst][cell]
                if r < 0:
                    continue
                refs[lst] = r
                bl = self.predict_block(sc, mi, lst, r, mi.mv[lst][cell], 16 * ((mi.addr >> 1) % self.wm) + x, Ybase + y, n, n)
                for pl in range(3):
                    blocks[pl].append((lst, bl[pl]))
            for pl in range(3):
                blk = self.combine(sc, mi, blocks[pl], refs, pl)
                if pl == 0:
                    for r in range(n):
                        pred[0][y + r][x:x + n] = blk[r]
                else:
                    for r in range(n // 2):
                        pred[pl][y // 2 + r][x // 2:x // 2 + n // 2] = blk[r]
        tr = transref.Tracker()
        resy, resc = self.inter_residual(rec, mi, bm, tr)
        Y = self.planes[0]
        for r in range(16):
            Y[y0 + dy * r][x0:x0 + 16] = pred[0][r] if resy is None else [clip1(pred[0][r][k] + resy[r][k]) for k in range(16)]
        for p in range(2):
            pl = self.planes[1 + p]
            for r in range(8):
                pl[cy0 + cdy * r][cx0:cx0 + 8] = pred[1 + p][r] if resc is None else [clip1(pred[1 + p][r][k] + resc[p][r][k]) for k in range(8)]
        self.finish_flags(rec, mi, bm)
        if not tr.range_ok:
            self.st("coefficient range violation")

    def inter_residual(self, rec, mi, bm, tr):
        qp = rec.qp
        resy = None
        if rec.cbp_l:
            resy = [[0] * 16 for _ in range(16)]
            if rec.t8:
                for b8 in range(4):
                    lv = self.lv(bm, "luma8", b8, 64)
                    if (rec.cbp_l >> b8) & 1 and any(lv):
                        r = transref.residual8x8(lv, qp, tr)
                        for i in range(8):
                            resy[8 * (b8 >> 1) + i][8 * (b8 & 1):8 * (b8 & 1) + 8] = r[i]
            else:
                for b in range(16):
                    lv = self.lv(bm, "luma", b, 16)
                    if (rec.cbp_l >> (b >> 2)) & 1 and any(lv):
                        bx, by = M.blk_xy(b)
                        r = transref.residual4x4(transref.inverse_zigzag(lv), qp, False, tr)
                        for i in range(4):
                            resy[4 * by + i][4 * bx:4 * bx + 4] = r[i]
        resc = None
        if rec.cbp_c:
            resc = []
            for p, name in enumerate(("cb", "cr")):
                qpc = transref.chroma_qp(qp, self.offs[p])
                resc.append(transref.chroma_residual(qpc, self.lv(bm, name + "dc", 0, 4),
                                                     [self.lv(bm, name + "ac", k, 15) for k in range(4)] if rec.cbp_c == 2 else [[0] * 15] * 4, tr))
        return resy, resc

    def finish_flags(self, rec, mi, bm):
        """nz[16]: 4x4 luma blocks with non-zero coefficients (bS 2 test)."""
        if rec.t8:
            for b8 in range(4):
                lv = (bm or self.blockmap(rec)).get(("luma8", b8))
                v = bool(lv is not None and any(lv))
                for c in self.cells((b8 & 1) * 2, (b8 >> 1) * 2, 2, 2):
                    mi.nz[c] = v
        else:
            bmm = bm or self.blockmap(rec)
            for b in range(16):
                lv = bmm.get(("luma", b))
                bx, by = M.blk_xy(b)
                mi.nz[by * 4 + bx] = bool(lv is not None and any(lv))


# ---------------------------------------------------------------- driver
def picture_meta(pics, order):
    """POC (top, bottom, frame) and IDR period of every picture and the OUTPUT order."""
    ps = poc_ref.PocState()
    meta, period = {}, 0
    for pi in order:
        s0, t0 = pics[pi][0][0], pics[pi][0][1]
        idr = s0.nal_type == 5
        top, bot, poc = ps.next(s0.hdr, s0.sps, t0["nal_ref_idc"], idr)
        if t0["mmco"] and any(op == 5 for op, _ in t0["mmco"]):
            raise Unsupported("memory_management_control_operation 5")
        if idr:
            period += 1
        meta[pi] = dict(top=top, bot=bot, poc=poc, period=period)
        ps.finish(False, top, bot, t0["frame_num"], t0["nal_ref_idc"])
    return meta, sorted(order, key=lambda pi: (meta[pi]["period"], meta[pi]["poc"], pi))


def load_parsed(path, td, cache=None):
    if cache is not None and "parsed" in cache:
        return cache["parsed"]
    infos = M.prepare(CR.load_stream(path, td))
    tabs = bslicehdr_ref.load(os.path.join(td, "ref_trace.txt"))
    out = []
    for s, t in zip(infos, tabs):
        s.tab = t
        why = M.usable(s)
        if why:
            raise Unsupported(f"slice {s.index}: {why}")
        if s.sps.get("seq_scaling_matrix_present_flag", 0) or s.pps.get("pic_scaling_matrix_present_flag", 0):
            raise Unsupported("scaling matrices (Chapter 8.6)")
        sl = M.Slice(s)
        recs, status = sl.parse()
        if status != "complete":
            raise Unsupported(f"slice {s.index}: parser status {status}")
        out.append((s, t, sl, recs))
    if cache is not None:
        cache["parsed"] = out
    return out


def decode(path, workdir=None, pixels=True, deblock=True, cache=None, stats=None, upto=None):
    """-> list of decoded Frames in OUTPUT order, each with .pixels geometry attributes: x0, y0, width, height (cropped size)."""
    td = workdir or tempfile.mkdtemp(prefix="mbdec_")
    try:
        parsed = load_parsed(path, td, cache)
    finally:
        if workdir is None:
            shutil.rmtree(td, ignore_errors=True)
    pics = {}
    for item in parsed:
        pics.setdefault(item[0].picture, []).append(item)
    order = sorted(pics)
    meta, out_order = picture_meta(pics, order)
    s0 = parsed[0][0]
    dpb = Dpb(s0.sps)
    dec = Decoder([p[0] for p in parsed], parsed, pixels, deblock, stats)
    decoded = {}
    need = out_order if upto is None else out_order[:upto]
    last = max(need) if need else -1
    transref.VIOLATIONS = 0
    for pi in order:
        if pi > last:
            break
        fr = dec.decode_picture(pics[pi], dpb, pi, (meta[pi]["top"], meta[pi]["bot"]))
        sps = pics[pi][0][0].sps
        cw, ch, x0, y0 = yuvref.cropped_size(fr.wm, sps["pic_height_in_map_units_minus1"] + 1, sps.get("frame_mbs_only_flag", 1),
                                              sps.get("frame_crop_left_offset", 0), sps.get("frame_crop_right_offset", 0),
                                              sps.get("frame_crop_top_offset", 0), sps.get("frame_crop_bottom_offset", 0))
        fr.crop = (cw, ch, x0, y0)
        fr.slice_types = list(dec.slice_types)
        dec.slice_types = []
        decoded[pi] = fr
    return [decoded[pi] for pi in need]


def frame_bytes(fr):
    cw, ch, x0, y0 = fr.crop
    b = bytearray()
    for row in fr.planes[0][y0:y0 + ch]:
        b += bytes(row[x0:x0 + cw])
    for pl in fr.planes[1:]:
        for row in pl[y0 // 2:y0 // 2 + (ch + 1) // 2]:
            b += bytes(row[x0 // 2:x0 // 2 + (cw + 1) // 2])
    return bytes(b)


def write_yuv(frames, out):
    with open(out, "wb") as f:
        for fr in frames:
            f.write(frame_bytes(fr))


def mv_dump_lines(frames):
    """One line per macroblock: MV <output index> <addr> fld=<0|1> l0=<refIdx>:<mvx>,<mvy>/... (16 cells, raster 4x4) l1=...; intra: 'intra'."""
    out = []
    for n, fr in enumerate(frames):
        for addr in sorted(fr.mbs):
            mi = fr.mbs[addr]
            if mi.intra:
                out.append(f"MV {n} {addr} fld={int(bool(mi.fld))} intra")
                continue
            parts = []
            for l in (0, 1):
                parts.append(f"l{l}=" + "/".join("-" if mi.ref[l][c] < 0 else f"{mi.ref[l][c]}:{mi.mv[l][c][0]},{mi.mv[l][c][1]}" for c in range(16)))
            out.append(f"MV {n} {addr} fld={int(bool(mi.fld))} " + " ".join(parts))
    return out


def main(argv):
    args = list(argv)
    opt = {}
    for flag, key, val in (("--no-deblock", "nodeblock", False), ):
        if flag in args:
            args.remove(flag)
            opt[key] = True
    nodeblock_out = mvout = None
    n = None
    for flag in ("--nodeblock-out", "--mv-dump", "--frames"):
        if flag in args:
            i = args.index(flag)
            v = args[i + 1]
            del args[i:i + 2]
            if flag == "--nodeblock-out":
                nodeblock_out = v
            elif flag == "--mv-dump":
                mvout = v
            else:
                n = int(v)
    if len(args) != 2:
        print(__doc__)
        return 2
    try:
        frames = decode(args[0], deblock=not opt.get("nodeblock"), upto=n)
    except Unsupported as e:
        print("unsupported:", e)
        return 2
    write_yuv(frames, args[1])
    if mvout:
        open(mvout, "w").write("\n".join(mv_dump_lines(frames)) + "\n")
    if nodeblock_out:
        write_yuv(decode(args[0], deblock=False, upto=n), nodeblock_out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
