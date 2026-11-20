"""Synthetic stream generator for Chapter 6.1 (derived from Chapter 5.5's generator: an encoder-side CAVLC writer that
shares no code with the decoder-side helpers).  It writes I pictures built to stress the DEBLOCKING decisions:

  * disable_deblocking_filter_idc 0, 1 and 2 chosen per slice (so one picture mixes them), slice_alpha_c0_offset_div2 and
    slice_beta_offset_div2 from -6 to +6 (and the extremes), written in every slice header;
  * slice layouts that put slice boundaries on and off macroblock-row boundaries (one slice, rows, one macroblock per slice,
    ragged), so that idc 2 has left and top neighbours in other slices, and idc 0 / 1 / 2 slices touch each other;
  * I_PCM macroblocks (their qP is 0 for the filter), Intra16x16 and Intra4x4 macroblocks, QP_Y from 0 to 51, chroma QP offsets
    -12 .. +12 (the chroma edges use chroma QPs);
  * "calm" content: small levels, so neighbouring blocks differ by a few steps only.  The filter is a no-op when the step across
    an edge is >= alpha or the texture beside it is >= beta, and random noise at high QP is mostly such a case; calm content
    keeps the filter ACTIVE, which is what makes a wrong bS visible in the pixels (see the Readme, section 6).
It does not compute the decoded picture; the truth is ffmpeg's decode of the stream.  No cropping, no 8x8 transform (the 8x8
transform case comes from an x264 High-profile stream, see test_bs.py --make-variants).
"""
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from common.bitwriter import W, nal  # noqa: E402
from common import cavlc_tables as T  # noqa: E402
import transref  # noqa: E402

LUMA_XY = transref.BLK_XY
LUMA_IDX = {xy: i for i, xy in enumerate(LUMA_XY)}
CH_XY = [(0, 0), (1, 0), (0, 1), (1, 1)]
CH_IDX = {xy: i for i, xy in enumerate(CH_XY)}

# name -> dict(w, h (macroblocks), pictures, slices, qp, pcm (probability of I_PCM), nxn (probability of I_NxN among the others;
#              the rest are Intra16x16), dbk (deblocking parameters), calm (level amplitude scale), seed)
# slices: 'one' | 'rows' | 'single' (one macroblock per slice) | 'midrow' (every slice 1.5 rows long) | 'ragged'
# qp: 'mid' (small steps, QP 15..40) | 'lo' (QP 8..30) | 'hi' (QP 28..51) | 'rand' (any delta, QP_Y wraps) | 'wrap'
# dbk: 'idc0' | 'idc1' | 'idc2' | 'mix' (idc drawn per slice) -- each with random alpha/beta offsets, except 'idc0z': idc 0, offsets 0
VARIANTS = {
    "bs_idc0":    dict(w=7, h=5, pictures=6, slices="one", qp="mid", pcm=0.02, nxn=0.7, dbk="idc0", calm=0.5, seed=611),
    "bs_idc0z":   dict(w=7, h=5, pictures=4, slices="one", qp="mid", pcm=0.0, nxn=0.6, dbk="idc0z", calm=0.5, seed=612),
    "bs_mix":     dict(w=7, h=6, pictures=10, slices="ragged", qp="mid", pcm=0.03, nxn=0.7, dbk="mix", calm=0.5, seed=613),
    "bs_idc2":    dict(w=7, h=6, pictures=8, slices="ragged", qp="mid", pcm=0.03, nxn=0.7, dbk="idc2", calm=0.5, seed=614),
    "bs_idc2rows": dict(w=6, h=6, pictures=6, slices="rows", qp="mid", pcm=0.03, nxn=0.7, dbk="idc2", calm=0.5, seed=615),
    "bs_idc2single": dict(w=5, h=4, pictures=6, slices="single", qp="hi", pcm=0.05, nxn=0.7, dbk="idc2", calm=0.4, seed=616),
    "bs_idc1":    dict(w=5, h=4, pictures=3, slices="ragged", qp="mid", pcm=0.03, nxn=0.7, dbk="idc1", calm=0.5, seed=617),
    "bs_mixrows": dict(w=6, h=6, pictures=8, slices="rows", qp="mid", pcm=0.03, nxn=0.7, dbk="mix", calm=0.5, seed=618),
    "bs_midrow":  dict(w=6, h=6, pictures=8, slices="midrow", qp="mid", pcm=0.03, nxn=0.7, dbk="mix", calm=0.5, seed=619),
    "bs_lowqp":   dict(w=7, h=5, pictures=6, slices="ragged", qp="lo", pcm=0.02, nxn=0.7, dbk="mix", calm=0.25, seed=620),
    "bs_hiqp":    dict(w=7, h=5, pictures=6, slices="ragged", qp="hi", pcm=0.02, nxn=0.7, dbk="mix", calm=0.4, seed=621),
    "bs_pcm":     dict(w=6, h=5, pictures=8, slices="ragged", qp="mid", pcm=0.3, nxn=0.5, dbk="mix", calm=0.5, seed=622),
    "bs_big":     dict(w=22, h=18, pictures=2, slices="ragged", qp="mid", pcm=0.02, nxn=0.8, dbk="mix", calm=0.5, seed=623),
}


def sps_nal(w_mbs, h_mbs, crop):
    w = W()
    w.raw(8, 100); w.raw(8, 0); w.raw(8, 30)
    w.ue(0); w.ue(1); w.ue(0); w.ue(0)    # sps id, 4:2:0, bit depths 8
    w.raw(1, 0); w.raw(1, 0)              # no transform bypass, no scaling matrices
    w.ue(0)                               # log2_max_frame_num - 4
    w.ue(2)                               # pic_order_cnt_type 2
    w.ue(1); w.raw(1, 0)                  # max_num_ref_frames, gaps
    w.ue(w_mbs - 1); w.ue(h_mbs - 1)
    w.raw(1, 1); w.raw(1, 1)              # frame_mbs_only, direct_8x8
    if any(crop):
        w.raw(1, 1)
        for c in crop:                    # left, right, top, bottom
            w.ue(c)
    else:
        w.raw(1, 0)
    w.raw(1, 0)                           # no VUI
    return nal(0x67, w.rbsp())


def pps_nal(init_qp, off_cb, off_cr, cip=0):
    w = W()
    w.ue(0); w.ue(0); w.raw(1, 0); w.raw(1, 0)          # ids, CAVLC, no bottom_field_pic_order
    w.ue(0); w.ue(0); w.ue(0); w.raw(1, 0); w.raw(2, 0)  # slice groups, ref idx defaults, weighted
    w.se(init_qp - 26); w.se(0); w.se(off_cb)
    w.raw(1, 1); w.raw(1, cip); w.raw(1, 0)              # deblocking control present, constrained_intra_pred_flag, no redundant
    w.raw(1, 0); w.raw(1, 0)                             # transform_8x8_mode, pic_scaling_matrix_present
    w.se(off_cr)
    return nal(0x68, w.rbsp())


# ------------------------------------------------------------------ CAVLC block writer
def bits(w, code):
    for ch in code:
        w.raw(1, int(ch))


def class_of(nc):
    return "dc" if nc == -1 else 0 if nc <= 1 else 1 if nc <= 3 else 2 if nc <= 7 else 3


def write_level(w, level, sl, first_after_t1):
    code = 2 * level - 2 if level > 0 else -2 * level - 1
    if first_after_t1:
        code -= 2
    if sl == 0 and code < 14:
        prefix, suffix, size = code, 0, 0
    elif sl == 0 and code < 30:
        prefix, suffix, size = 14, code - 14, 4
    elif sl > 0 and code < (15 << sl):
        prefix, suffix, size = code >> sl, code & ((1 << sl) - 1), sl
    else:
        base0 = (15 << sl) + (15 if sl == 0 else 0)
        prefix = 15
        while True:
            base = base0 + ((1 << (prefix - 3)) - 4096 if prefix >= 16 else 0)
            if base <= code < base + (1 << (prefix - 3)):
                break
            prefix += 1
        suffix, size = code - base, prefix - 3
    w.raw(prefix, 0); w.raw(1, 1)
    if size:
        w.raw(size, suffix)
    if sl == 0:
        sl = 1
    if abs(level) > (3 << (sl - 1)) and sl < 6:
        sl += 1
    return sl


def write_block(w, nc, maxnum, coeffs):
    nz = [k for k, v in enumerate(coeffs) if v]
    tc = len(nz)
    levels = [coeffs[p] for p in reversed(nz)]
    t1 = 0
    while t1 < min(3, tc) and abs(levels[t1]) == 1:
        t1 += 1
    cls = class_of(nc)
    bits(w, (T.COEFF_TOKEN_DC if cls == "dc" else T.COEFF_TOKEN[cls])[(tc, t1)])
    if tc == 0:
        return 0
    sl = 1 if tc > 10 and t1 < 3 else 0
    for i, lv in enumerate(levels):
        if i < t1:
            w.raw(1, 1 if lv < 0 else 0)
        else:
            sl = write_level(w, lv, sl, i == t1 and t1 < 3)
    tz = nz[-1] + 1 - tc
    if tc < maxnum:
        bits(w, (T.TOTAL_ZEROS_DC if maxnum == 4 else T.TOTAL_ZEROS)[tc][tz])
    left = tz
    for i in range(tc - 1):
        run = nz[-1 - i] - nz[-2 - i] - 1
        if left > 0:
            bits(w, T.RUN_BEFORE[min(left, 7)][run])
            left -= run
    return tc


# ------------------------------------------------------------------ neighbour bookkeeping for nC (9.2.1)
class Counts:
    """total_coeff of every 4x4 block of the picture so far, per macroblock; slice ownership for availability."""

    def __init__(self, w_mbs):
        self.w = w_mbs
        self.luma, self.chroma, self.slice_of = {}, {}, {}

    def avail(self, addr, other, cur_slice, same_row=None):
        return other >= 0 and self.slice_of.get(other) == cur_slice and (same_row is None or other // self.w == same_row)

    def nc(self, addr, cur_slice, plane, blk, mine):
        """plane 0 luma, 1/2 chroma; mine = counts of the blocks of this macroblock written so far."""
        xy, idx, n = (LUMA_XY, LUMA_IDX, 4) if plane == 0 else (CH_XY, CH_IDX, 2)
        x, y = xy[blk]

        def neighbour(dx, dy):
            nx, ny = x + dx, y + dy
            if nx >= 0 and ny >= 0:
                return mine[idx[(nx, ny)]]
            if nx < 0:
                o = addr - 1
                ok = addr % self.w > 0 and self.slice_of.get(o) == cur_slice
                nx = n - 1
            else:
                o = addr - self.w
                ok = o >= 0 and self.slice_of.get(o) == cur_slice
                ny = n - 1
            if not ok:
                return None
            tab = self.luma[o] if plane == 0 else self.chroma[o][plane - 1]
            return tab[idx[(nx, ny)]]
        na, nb = neighbour(-1, 0), neighbour(0, -1)
        if na is not None and nb is not None:
            return (na + nb + 1) >> 1
        return na if na is not None else (nb if nb is not None else 0)


# ------------------------------------------------------------------ macroblock content
def rand_array(rng, n, amp, density):
    return [rng.choice((-1, 1)) * rng.randint(1, amp) if rng.random() < density else 0 for _ in range(n)]


def pick_qp(rng, kind):
    if kind == "sweep":
        return rng.randrange(52)
    if kind == "high":
        return rng.randrange(38, 52)
    if kind == "low":
        return rng.randrange(0, 15)
    return rng.randrange(15, 41)


def make_i16(rng, qp, offs, avail, wild):
    """Draw an Intra16x16 macroblock: modes legal for the availability, CBP, levels (kept inside the 16-bit
    intermediate range required of conforming streams)."""
    a, b, d = avail
    lmodes = [2] + ([0] if b else []) + ([1] if a else []) + ([3] if a and b and d else [])
    cmodes = [0] + ([1] if a else []) + ([2] if b else []) + ([3] if a and b and d else [])
    mode, cmode = rng.choice(lmodes), rng.choice(cmodes)
    cc, lf = rng.randrange(3), rng.randrange(2)
    density = rng.choice((0.1, 0.3, 0.7, 1.0))
    if wild:
        amp = rng.choice((1, 2, 3, 8, 30, 300))
    else:
        amp = max(1, min(300, int(CALM * rng.uniform(0.3, 2) * 4000 / (2 ** (qp // 6) * (qp % 6 + 5) * 4))))
    while True:
        arr = {("i16dc", 0): rand_array(rng, 16, amp, max(density, 0.3))}
        if lf:
            for k in range(16):
                arr[("i16ac", k)] = rand_array(rng, 15, amp, density)
        if cc:
            arr[("cbdc", 0)] = rand_array(rng, 4, amp, 0.6)
            arr[("crdc", 0)] = rand_array(rng, 4, amp, 0.6)
        if cc == 2:
            for n in ("cbac", "crac"):
                for k in range(4):
                    arr[(n, k)] = rand_array(rng, 15, amp, density)
        tr = transref.Tracker()
        g = lambda k, n: arr.get(k, [0] * n)  # noqa: E731
        transref.i16_residual(qp, g(("i16dc", 0), 16), [g(("i16ac", k), 15) for k in range(16)], tr)
        for name, off in zip(("cb", "cr"), offs):
            transref.chroma_residual(transref.chroma_qp(qp, off), g((name + "dc", 0), 4),
                                     [g((name + "ac", k), 15) for k in range(4)], tr)
        if tr.range_ok:
            return dict(kind="i16", mode=mode, cmode=cmode, cc=cc, lf=lf, arrays=arr, qp=qp)
        amp = max(1, amp // 2)
        density = max(0.05, density * 0.8)


def make_pcm(rng):
    """Raw samples: pseudo-random mixtures of ramps (to give the plane predictor of the next macroblocks something
    steep, including clipping) and noise.  Samples are 1..255."""
    style = rng.choice(("ramp_x", "ramp_y", "noise", "flat", "diag"))
    base, step = rng.randrange(1, 256), rng.choice((-17, -9, -4, 4, 9, 17))
    clamp = lambda v: max(1, min(255, v))  # noqa: E731

    def sample(x, y, size):
        k = 16 // size
        if style == "ramp_x":
            return clamp(base + step * x * k // 2)
        if style == "ramp_y":
            return clamp(base + step * y * k // 2)
        if style == "diag":
            return clamp(base + step * (x + y) * k // 3)
        if style == "flat":
            return base
        return rng.randrange(1, 256)
    y = bytes(sample(x, yy, 16) for yy in range(16) for x in range(16))
    cb = bytes(clamp(sample(x, yy, 8) + 20) for yy in range(8) for x in range(8))
    cr = bytes(clamp(sample(x, yy, 8) - 20) for yy in range(8) for x in range(8))
    return dict(kind="pcm", samples=y + cb + cr)


def slice_starts(rng, kind, n, w):
    if kind == "one":
        return [0]
    if kind == "rows":
        return list(range(0, n, w))
    if kind == "single":
        return list(range(n))
    if kind == "midrow":
        return list(range(0, n, w + w // 2))
    starts, a = [0], 0
    while True:
        a += rng.choice((1, 2, 3, w - 1, w, w, w + 1, 2 * w, rng.randrange(1, 2 * w + 1)))
        if a >= n:
            return starts
        starts.append(a)


# ------------------------------------------------------------------ Intra 4x4 (encoder side)
# Table 9-4, Intra column: codeNum -> coded_block_pattern (luma + 16 * chroma).  Typed from the standard.
CBP_INTRA = [47, 31, 15, 0, 23, 27, 29, 30, 7, 11, 13, 14, 39, 43, 45, 46,
             16, 3, 5, 10, 12, 19, 21, 26, 28, 35, 37, 42, 44, 1, 2, 4,
             8, 17, 18, 20, 24, 6, 9, 22, 25, 32, 33, 34, 36, 40, 38, 41]
CBP_CODE = {v: k for k, v in enumerate(CBP_INTRA)}


def legal_i4_modes(a, b, d):
    """Intra4x4 modes whose neighbours exist (a left, b above, d corner)."""
    return [m for m, ok in enumerate((b, a, True, b, a and b and d, a and b and d, a and b and d, b, a)) if ok]


def block_avail(blk, mb):
    """(a, b, d, c) for the 4x4 block blkIdx of a macroblock whose (A, B, C, D) availability is mb.  c = the four
    up-right samples are available (derived here from decoding order, not from a table)."""
    bx, by = LUMA_XY[blk]
    ma, mb_, mc, md = mb
    a = ma if bx == 0 else True
    b = mb_ if by == 0 else True
    d = (md if (bx == 0 and by == 0) else mb_ if by == 0 else ma if bx == 0 else True)
    if by == 0:
        c = mb_ if bx < 3 else mc
    else:
        c = bx < 3 and LUMA_IDX[(bx + 1, by - 1)] < blk       # the up-right block exists and is already decoded
    return a, b, d, c


def predicted_mode(blk, cur, addr, wm, avail_a, avail_b, kind, modes):
    """predIntra4x4PredMode of block blk (8.3.1.1): min of the modes of the left and the above block, or DC (2) when
    one of the two lies in an unavailable macroblock; a neighbouring macroblock that is not I_NxN counts as DC.
    Returns (pred, (kindA, kindB)) where kind is 'na', 'nxn', 'i16' or 'pcm'."""
    bx, by = LUMA_XY[blk]
    got, kinds, dc_flag = [], [], False
    for which, (nx, ny) in (("A", (bx - 1, by)), ("B", (bx, by - 1))):
        if nx >= 0 and ny >= 0:
            got.append(cur[LUMA_IDX[(nx, ny)]])
            kinds.append("nxn")
            continue
        o = addr - 1 if which == "A" else addr - wm
        if not (avail_a if which == "A" else avail_b):
            dc_flag = True
            kinds.append("na")
            got.append(2)
        elif kind[o] != "nxn":
            kinds.append(kind[o])
            got.append(2)
        else:
            kinds.append("nxn")
            got.append(modes[o][LUMA_IDX[((3, ny) if which == "A" else (nx, 3))]])
    return (2 if dc_flag else min(got)), tuple(kinds)


def make_nxn(rng, qp, offs, mbav, addr, wm, kind, modes, wild, cover):
    """Draw an I_NxN macroblock.  mbav = (A, B, C, D); kind/modes = what the earlier macroblocks of the slice were."""
    a, b, c, d = mbav
    cur = []
    syn = []
    pmode = rng.choice((0.1, 0.35, 0.6))      # how often the mode equals the prediction (prev_..._flag = 1)
    for blk in range(16):
        ba, bb, bd, bc = block_avail(blk, mbav)
        legal = legal_i4_modes(ba, bb, bd)
        pred, kinds = predicted_mode(blk, cur, addr, wm, a, b, kind, modes)
        if pred in legal and rng.random() < pmode:
            m = pred
        else:
            m = rng.choice(legal)
        cur.append(m)
        if m == pred:
            syn.append((1, 0))
            cover[("mpflag", "flag1")] += 1
        else:
            rem = m if m < pred else m - 1
            syn.append((0, rem))
            cover[("mpflag", "rem<pred" if m < pred else "rem>=pred")] += 1
        cover[("mp",) + kinds] += 1
        cover[("avail", m, int(ba), int(bb), int(bd))] += 1
        if all((a, b, c, d)):
            cover[("blk", blk, m)] += 1
        if m in (3, 7):
            cover[("ur", blk, "real" if bc else "subst")] += 1
    luma_cbp = rng.choice((0, 15, rng.randrange(16), rng.randrange(16)))
    cc = rng.randrange(3)
    cbp = luma_cbp + 16 * cc
    if cbp == 0:
        qp = None                                # no mb_qp_delta: QP_Y stays (the caller fills it in)
    density = rng.choice((0.1, 0.3, 0.7, 1.0))
    q = qp if qp is not None else 26
    amp = rng.choice((1, 2, 3, 8, 30, 300)) if wild else max(1, min(300, int(CALM * rng.uniform(0.3, 2) * 4000 / (2 ** (q // 6) * (q % 6 + 5) * 4))))
    return dict(kind="nxn", modes=cur, syn=syn, cmode=None, cc=cc, luma_cbp=luma_cbp, cbp=cbp, qp=qp, density=density,
                amp=amp, arrays=None)


def finish_nxn(rng, mb, qp, offs, avail_chroma):
    """Chroma mode and level arrays, once the macroblock's QP is known.  Keeps the transform intermediates inside
    16 bits (a conformance requirement)."""
    a, b, d = avail_chroma
    cmodes = [0] + ([1] if a else []) + ([2] if b else []) + ([3] if a and b and d else [])
    mb["cmode"] = rng.choice(cmodes)
    mb["qp"] = qp
    density, amp, cc, lc = mb["density"], mb["amp"], mb["cc"], mb["luma_cbp"]
    while True:
        arr = {}
        for blk in range(16):
            if lc >> (blk >> 2) & 1:
                arr[("luma", blk)] = rand_array(rng, 16, amp, density)
        if cc:
            arr[("cbdc", 0)] = rand_array(rng, 4, amp, 0.6)
            arr[("crdc", 0)] = rand_array(rng, 4, amp, 0.6)
        if cc == 2:
            for n in ("cbac", "crac"):
                for k in range(4):
                    arr[(n, k)] = rand_array(rng, 15, amp, density)
        tr = transref.Tracker()
        for blk in range(16):
            if ("luma", blk) in arr:
                transref.residual4x4(transref.inverse_zigzag(arr[("luma", blk)]), qp, False, tr)
        g = lambda k, n: arr.get(k, [0] * n)  # noqa: E731
        for name, off in zip(("cb", "cr"), offs):
            transref.chroma_residual(transref.chroma_qp(qp, off), g((name + "dc", 0), 4),
                                     [g((name + "ac", k), 15) for k in range(4)], tr)
        if tr.range_ok:
            mb["arrays"] = arr
            return mb
        amp = max(1, amp // 2)
        density = max(0.05, density * 0.8)


def write_mb(w, mb, slice_qp, prev_qp, cnt, addr, cur_slice):
    """Write one macroblock; returns its QP_Y after it (the running QP of the slice)."""
    if mb["kind"] == "pcm":
        w.ue(25)
        while len(w.bits) % 8:
            w.raw(1, 0)
        for v in mb["samples"]:
            w.raw(8, v)
        cnt.luma[addr] = [16] * 16
        cnt.chroma[addr] = ([16] * 4, [16] * 4)
        return prev_qp
    cc = mb["cc"]
    arr = mb["arrays"]
    lc, ccount = [0] * 16, ([0] * 4, [0] * 4)
    if mb["kind"] == "i16":
        lf = mb["lf"]
        w.ue(1 + mb["mode"] + 4 * cc + 12 * lf)
        w.ue(mb["cmode"])
        w.se(mb["qp_delta"])
        write_block(w, cnt.nc(addr, cur_slice, 0, 0, lc), 16, arr[("i16dc", 0)])
        if lf:
            for k in range(16):
                lc[k] = write_block(w, cnt.nc(addr, cur_slice, 0, k, lc), 15, arr[("i16ac", k)])
        qp_after = mb["qp"]
    else:
        w.ue(0)                                            # I_NxN (transform_size_8x8_flag absent: PPS flag 0)
        for flag, rem in mb["syn"]:
            w.raw(1, flag)
            if not flag:
                w.raw(3, rem)
        w.ue(mb["cmode"])
        w.ue(CBP_CODE[mb["cbp"]])
        qp_after = prev_qp
        if mb["cbp"] > 0:
            w.se(mb["qp_delta"])
            qp_after = mb["qp"]
        for blk in range(16):
            if mb["luma_cbp"] >> (blk >> 2) & 1:
                lc[blk] = write_block(w, cnt.nc(addr, cur_slice, 0, blk, lc), 16, arr[("luma", blk)])
    if cc:
        write_block(w, -1, 4, arr[("cbdc", 0)])
        write_block(w, -1, 4, arr[("crdc", 0)])
    if cc == 2:
        for p, n in enumerate(("cbac", "crac")):
            for k in range(4):
                ccount[p][k] = write_block(w, cnt.nc(addr, cur_slice, 1 + p, k, ccount[p]), 15, arr[(n, k)])
    cnt.luma[addr] = lc
    cnt.chroma[addr] = ccount
    return qp_after


RANGES = {"mid": (15, 40), "lo": (8, 30), "hi": (28, 51)}
CALM = 1.0       # amplitude scale of the levels, set per stream by make_stream


def pick_dbk(rng, mode):
    """-> (disable_deblocking_filter_idc, alpha_div2, beta_div2) of one slice."""
    idc = {"idc0": 0, "idc0z": 0, "idc1": 1, "idc2": 2}.get(mode) if mode != "mix" else rng.choice((0, 0, 1, 2, 2, 2))
    if mode == "idc0z":
        return 0, 0, 0
    return idc, rng.choice((0, rng.randrange(-6, 7), rng.randrange(-6, 7), -6, 6)), rng.choice((0, rng.randrange(-6, 7), rng.randrange(-6, 7), -6, 6))


def wrap_qp(pred, delta):
    """7.4.5 (7-37) for 8-bit video, written independently of the decoder's version."""
    return (pred + 52 + delta) % 52


def pick_delta(rng, mode, prev):
    if mode in RANGES:
        lo, hi = RANGES[mode]
        want = max(lo, min(hi, prev + rng.randint(-3, 3)))
        return want - prev
    if mode == "wrap":
        return rng.choice((-26, 25, -25, 24, -26, 25, rng.randint(-26, 25), rng.randint(-26, 25), 0, 1, -1))
    return rng.choice((rng.randint(-26, 25), rng.randint(-26, 25), rng.randint(-26, 25), -26, 25, 0))


def pick_slice_qp(rng, mode):
    if mode in RANGES:
        return rng.randrange(RANGES[mode][0], RANGES[mode][1] + 1)
    if mode == "wrap":
        return rng.choice((0, 1, 2, 3, 48, 49, 50, 51, rng.randrange(52)))
    return rng.choice((rng.randrange(52), rng.randrange(52), 0, 51))


def make_stream(spec):
    """-> (annex_b_bytes, info) with info = dict(w, h, pictures, cover=Counter, n_slices)."""
    rng = random.Random(spec["seed"])
    wm, hm = spec["w"], spec["h"]
    n = wm * hm
    data, cover, nsl = b"", Counter(), 0
    global CALM
    CALM = spec["calm"]
    for p in range(spec["pictures"]):
        edge = (-12, 12, rng.randrange(-12, 13))
        offs = (rng.choice(edge), rng.choice(edge)) if p % 3 != 2 else (0, 0)
        init_qp = rng.choice((rng.randrange(52), rng.randrange(52), rng.randrange(15, 40)))
        data += sps_nal(wm, hm, (0, 0, 0, 0)) + pps_nal(init_qp, *offs)
        starts = slice_starts(rng, spec["slices"], n, wm)
        cnt = Counts(wm)
        kind, modes = {}, {}
        end_qp = None
        for si, s in enumerate(starts):
            end = starts[si + 1] if si + 1 < len(starts) else n
            slice_qp = pick_slice_qp(rng, spec["qp"])
            cover[("slice", "qp_delta<0" if slice_qp < init_qp else "qp_delta>0" if slice_qp > init_qp else "qp_delta=0")] += 1
            if end_qp is not None and end_qp != slice_qp:
                cover[("qp", "slice_start_differs")] += 1
            if end - s == 1:
                cover[("slice", "one_mb")] += 1
            if s % wm:
                cover[("slice", "mid_row_start")] += 1
            elif s:
                cover[("slice", "row_start")] += 1
            w = W()
            w.ue(s); w.ue(7); w.ue(0)
            w.raw(4, 0)                      # frame_num
            w.ue(p % 2)                      # idr_pic_id
            w.raw(1, 0); w.raw(1, 0)         # no_output_of_prior_pics, long_term_reference_flag
            w.se(slice_qp - init_qp)         # slice_qp_delta
            idc, alpha, beta = pick_dbk(rng, spec["dbk"])
            w.ue(idc)                        # disable_deblocking_filter_idc
            if idc != 1:
                w.se(alpha); w.se(beta)      # slice_alpha_c0_offset_div2, slice_beta_offset_div2
            cover[("idc", idc)] += 1
            if idc and alpha:
                cover[("idc", idc, "offset")] += 1
            if idc != 1 and (alpha or beta):
                cover[("offsets", "nonzero")] += 1
            if idc == 2 and s % wm == 0 and s:
                cover[("idc2", "slice_starts_at_row_start")] += 1
            if idc == 2 and s % wm:
                cover[("idc2", "slice_starts_mid_row")] += 1
            if idc == 2 and end - s > wm:
                cover[("idc2", "slice_longer_than_row")] += 1
            qp = slice_qp
            prev_kind, prev_nodelta = None, 0     # what the previous macroblock of the slice was
            for addr in range(s, end):
                cnt.slice_of[addr] = si
                mx, my = addr % wm, addr // wm
                ok = lambda o, cond: cond and cnt.slice_of.get(o) == si     # noqa: E731
                mbav = (ok(addr - 1, mx > 0), ok(addr - wm, my > 0), ok(addr - wm + 1, my > 0 and mx < wm - 1),
                        ok(addr - wm - 1, mx > 0 and my > 0))
                avail = (mbav[0], mbav[1], mbav[3])
                for name, o, inside in (("A", addr - 1, mx > 0), ("B", addr - wm, my > 0),
                                        ("C", addr - wm + 1, my > 0 and mx < wm - 1), ("D", addr - wm - 1, mx > 0 and my > 0)):
                    if inside and cnt.slice_of.get(o) != si:
                        cover[("slice", name + "_other")] += 1
                delta = pick_delta(rng, spec["qp"], qp)
                want = wrap_qp(qp, delta)
                if rng.random() < spec["pcm"]:
                    mb = make_pcm(rng)
                elif rng.random() < spec["nxn"]:
                    mb = make_nxn(rng, want, offs, mbav, addr, wm, kind, modes, rng.random() < 0.3, cover)
                    finish_nxn(rng, mb, want if mb["cbp"] else qp, offs, avail)
                    mb["qp_delta"] = delta
                    cover[("chroma", mb["cmode"]) + tuple(map(int, avail))] += 1
                    modes[addr] = mb["modes"]
                    for name, o, inside in (("A", addr - 1, mx > 0), ("B", addr - wm, my > 0)):
                        if inside and cnt.slice_of.get(o) != si and kind.get(o) == "nxn":
                            cover[("slice", "nxn_neighbour_in_other_slice_" + name)] += 1
                else:
                    mb = make_i16(rng, want, offs, avail, rng.random() < 0.3)
                    mb["qp_delta"] = delta
                    cover[("i16", mb["mode"]) + tuple(map(int, avail))] += 1
                    cover[("chroma", mb["cmode"]) + tuple(map(int, avail))] += 1
                kind[addr] = mb["kind"]
                has_delta = mb["kind"] == "i16" or (mb["kind"] == "nxn" and mb["cbp"] > 0)
                if has_delta:
                    if qp + delta > 51:
                        cover[("qp", "wrap_up")] += 1
                    if qp + delta < 0:
                        cover[("qp", "wrap_down")] += 1
                    if delta in (-26, 25):
                        cover[("qp", "delta%+d" % delta)] += 1
                    if addr == s and delta:
                        cover[("qp", "first_mb_delta")] += 1
                    if prev_kind == "pcm":
                        cover[("qp", "after_pcm_delta")] += 1
                    if prev_nodelta and delta:
                        cover[("qp", "after_nodelta_delta")] += 1
                    if want in (0, 51):
                        cover[("qp", "qp%d" % want)] += 1
                    cc = mb["cc"]
                    if cc:
                        for off in offs:
                            raw = want + off
                            cover[("cqp", "clip_low" if raw < 0 else "clip_high" if raw > 51 else "table" if raw >= 30 else "plain")] += 1
                    prev_nodelta = 0
                else:
                    cover[("qp", "no_delta_" + mb["kind"])] += 1
                    prev_nodelta = 1
                if prev_nodelta and prev_kind == "pcm":
                    cover[("qp", "pcm_then_nodelta")] += 1
                prev_kind = mb["kind"]
                qp_before = qp
                qp = write_mb(w, mb, slice_qp, qp, cnt, addr, si)
                assert qp == (want if has_delta else qp_before)
            end_qp = qp
            data += nal(0x65, w.rbsp())
            nsl += 1
    return data, dict(w=wm, h=hm, pictures=spec["pictures"], cover=cover, n_slices=nsl)


# ------------------------------------------------------------------ coverage the synthetic streams must provide
def required_cover():
    req = {("idc", 0), ("idc", 1), ("idc", 2), ("offsets", "nonzero")}
    req |= {("idc2", k) for k in ("slice_starts_at_row_start", "slice_starts_mid_row", "slice_longer_than_row")}
    req |= {("slice", k) for k in ("one_mb", "mid_row_start", "row_start")}
    req |= {("qp", k) for k in ("no_delta_pcm",)}
    return req


def coverage_missing(cover):
    return sorted(k for k in required_cover() if cover.get(k, 0) <= 0)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "bs_mix"
    out = sys.argv[2] if len(sys.argv) > 2 else name + ".264"
    d, info = make_stream(VARIANTS[name])
    open(out, "wb").write(d)
    print(out, len(d), "bytes,", info["n_slices"], "slices")
