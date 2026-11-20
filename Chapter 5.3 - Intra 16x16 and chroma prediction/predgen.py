"""Synthetic stream generator for Chapter 5.3 (an encoder-side CAVLC writer; shares no code with the decoder-side
helpers).  It writes I pictures made only of Intra16x16 macroblocks (every luma mode, every chroma mode) and a few I_PCM
macroblocks, with random residual (all CBP combinations, all QPs, both chroma QP offsets), and with slice layouts that
produce every neighbour availability case: picture edges, one slice per row, and slices that start in the middle of
a row (so the macroblock above can lie in another slice).  Deblocking is off, constrained_intra_pred is 0.

It does NOT compute the decoded picture.  The truth is ffmpeg's decode of the stream.  The generator only has to
produce a valid stream: the prediction mode of every macroblock is drawn from the modes that are legal for its neighbour
availability, and the nC of every coeff_token is derived with the rules of 9.2.1 (neighbours in other slices are not
available, an I_PCM neighbour counts as 16, an Intra16x16 neighbour contributes its AC block counts only).
"""
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common.bitwriter import W, nal  # noqa: E402
from common import cavlc_tables as T  # noqa: E402
import transref  # noqa: E402

LUMA_XY = transref.BLK_XY
LUMA_IDX = {xy: i for i, xy in enumerate(LUMA_XY)}
CH_XY = [(0, 0), (1, 0), (0, 1), (1, 1)]
CH_IDX = {xy: i for i, xy in enumerate(CH_XY)}

# name -> dict(w, h (macroblocks), crop (l, r, t, b in 2-sample units), pictures, slices, qp, pcm (probability), seed)
# slices: 'one' | 'rows' | 'ragged' (random lengths 1 .. 2*w macroblocks, so slices start in the middle of rows)
# qp: 'mid' (QP 15..40) | 'sweep' (all 0..51) | 'high' (38..51) | 'low' (0..14)
VARIANTS = {
    "i16_edges":  dict(w=6, h=5, crop=(0, 0, 0, 0), pictures=6, slices="one", qp="mid", pcm=0.0, seed=531),
    "i16_rows":   dict(w=6, h=5, crop=(0, 0, 0, 0), pictures=5, slices="rows", qp="mid", pcm=0.0, seed=532),
    "i16_ragged": dict(w=7, h=6, crop=(0, 0, 0, 0), pictures=8, slices="ragged", qp="mid", pcm=0.04, seed=533),
    "i16_qp":     dict(w=5, h=4, crop=(0, 0, 0, 0), pictures=10, slices="one", qp="sweep", pcm=0.0, seed=534),
    "i16_pcm":    dict(w=6, h=5, crop=(0, 0, 0, 0), pictures=6, slices="ragged", qp="mid", pcm=0.2, seed=535),
    "i16_crop":   dict(w=5, h=4, crop=(1, 2, 1, 3), pictures=4, slices="ragged", qp="mid", pcm=0.03, seed=536),
    "i16_big":    dict(w=22, h=18, crop=(0, 0, 0, 0), pictures=3, slices="ragged", qp="mid", pcm=0.02, seed=537),
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


def pps_nal(init_qp, off_cb, off_cr):
    w = W()
    w.ue(0); w.ue(0); w.raw(1, 0); w.raw(1, 0)          # ids, CAVLC, no bottom_field_pic_order
    w.ue(0); w.ue(0); w.ue(0); w.raw(1, 0); w.raw(2, 0)  # slice groups, ref idx defaults, weighted
    w.se(init_qp - 26); w.se(0); w.se(off_cb)
    w.raw(1, 1); w.raw(1, 0); w.raw(1, 0)                # deblocking control present, no constrained intra, no redundant
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
        amp = max(1, min(300, int(rng.uniform(0.3, 2) * 4000 / (2 ** (qp // 6) * (qp % 6 + 5) * 4))))
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
    starts, a = [0], 0
    while True:
        a += rng.choice((1, 2, 3, w - 1, w, w, w + 1, 2 * w, rng.randrange(1, 2 * w + 1)))
        if a >= n:
            return starts
        starts.append(a)


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
    cc, lf = mb["cc"], mb["lf"]
    w.ue(1 + mb["mode"] + 4 * cc + 12 * lf)
    w.ue(mb["cmode"])
    w.se(mb["qp"] - prev_qp)
    arr = mb["arrays"]
    lc, ccount = [0] * 16, ([0] * 4, [0] * 4)
    write_block(w, cnt.nc(addr, cur_slice, 0, 0, lc), 16, arr[("i16dc", 0)])
    if lf:
        for k in range(16):
            lc[k] = write_block(w, cnt.nc(addr, cur_slice, 0, k, lc), 15, arr[("i16ac", k)])
    if cc:
        write_block(w, -1, 4, arr[("cbdc", 0)])
        write_block(w, -1, 4, arr[("crdc", 0)])
    if cc == 2:
        for p, n in enumerate(("cbac", "crac")):
            for k in range(4):
                ccount[p][k] = write_block(w, cnt.nc(addr, cur_slice, 1 + p, k, ccount[p]), 15, arr[(n, k)])
    cnt.luma[addr] = lc
    cnt.chroma[addr] = ccount
    return mb["qp"]


def make_stream(spec):
    """-> (annex_b_bytes, info) with info = dict(w, h, pictures, cover=Counter of (plane, mode, A, B, D), n_slices)."""
    rng = random.Random(spec["seed"])
    wm, hm = spec["w"], spec["h"]
    n = wm * hm
    data, cover, nsl = b"", Counter(), 0
    for p in range(spec["pictures"]):
        offs = (rng.randrange(-12, 13), rng.randrange(-12, 13)) if p % 3 != 2 else (0, 0)
        init_qp = rng.randrange(15, 40)
        data += sps_nal(wm, hm, spec["crop"]) + pps_nal(init_qp, *offs)
        starts = slice_starts(rng, spec["slices"], n, wm)
        cnt = Counts(wm)
        for si, s in enumerate(starts):
            end = starts[si + 1] if si + 1 < len(starts) else n
            slice_qp = pick_qp(rng, spec["qp"])
            w = W()
            w.ue(s); w.ue(7); w.ue(0)
            w.raw(4, 0)                      # frame_num
            w.ue(p % 2)                      # idr_pic_id
            w.raw(1, 0); w.raw(1, 0)         # no_output_of_prior_pics, long_term_reference_flag
            w.se(slice_qp - init_qp)
            w.ue(1)                          # disable_deblocking_filter_idc
            qp = slice_qp
            for addr in range(s, end):
                cnt.slice_of[addr] = si
                mx, my = addr % wm, addr // wm
                ok = lambda o, cond: cond and cnt.slice_of.get(o) == si     # noqa: E731
                avail = (ok(addr - 1, mx > 0), ok(addr - wm, my > 0), ok(addr - wm - 1, mx > 0 and my > 0))
                if rng.random() < spec["pcm"]:
                    mb = make_pcm(rng)
                else:
                    want = pick_qp(rng, spec["qp"])
                    want = qp + max(-26, min(25, want - qp))     # mb_qp_delta is limited to -26..25
                    mb = make_i16(rng, want, offs, avail, rng.random() < 0.3)
                    cover[("luma", mb["mode"]) + tuple(map(int, avail))] += 1
                    cover[("chroma", mb["cmode"]) + tuple(map(int, avail))] += 1
                qp = write_mb(w, mb, slice_qp, qp, cnt, addr, si)
            data += nal(0x65, w.rbsp())
            nsl += 1
    return data, dict(w=wm, h=hm, pictures=spec["pictures"], cover=cover, n_slices=nsl)


# Availability patterns (A, B, D) that can occur, and the modes that each pattern allows
PATTERNS = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0), (1, 1, 1)]


def required_cover():
    req = set()
    for plane, modes in (("luma", (0, 1, 2, 3)), ("chroma", (0, 1, 2, 3))):
        for a, b, d in PATTERNS:
            for m in modes:
                legal = {"luma": {0: b, 1: a, 2: 1, 3: a and b and d},
                         "chroma": {0: 1, 1: a, 2: b, 3: a and b and d}}[plane][m]
                if legal:
                    req.add((plane, m, a, b, d))
    return req


def coverage_missing(cover):
    return sorted(required_cover() - set(cover))


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "i16_edges"
    out = sys.argv[2] if len(sys.argv) > 2 else name + ".264"
    d, info = make_stream(VARIANTS[name])
    open(out, "wb").write(d)
    print(out, len(d), "bytes,", info["n_slices"], "slices")
