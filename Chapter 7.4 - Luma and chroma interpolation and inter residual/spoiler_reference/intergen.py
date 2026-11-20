"""SPOILER: synthetic stream generator for Chapter 7.4 (bit writer: common/bitwriter.py; vector predictor: mvpred_ref.PicState;
CAVLC block writer copied from Chapter 5.5's qsgen.py).  It does NOT compute the decoded pictures: the truth is ffmpeg.

Tiny CAVLC streams (SPS/PPS/slices written by hand, 96x64 pixels = 6x4 macroblocks, deblocking off) built so that every
interpolation case and every residual path is exercised with SENSITIVE content:
  * picture 0 is an IDR of I_PCM macroblocks in random textures: full-range noise (every filter tap and the Clip1 after the
    6-tap filter matter), mild noise around mid grey (no clipping: rounding constants matter), 0/255 checkerboards (extreme
    overshoot of the 6-tap filter), smooth ramps plus noise;
  * later pictures hold P macroblocks of every partition shape with chosen vectors, P_Skip, I_PCM (fresh texture) and I_NxN with
    cbp 0, with and without residual (random coefficients, random cbp, mb_qp_delta chains, chroma_qp_index_offset);
  * the generator chooses the TARGET vector of each partition (a fractional position xFrac, yFrac in 0..3 cycled so that all 16
    appear, and a position class: inside / touching an edge / half outside / far outside the picture) and writes mvd = target - mvp
    with mvp from the Chapter 7.3 predictor.
Modes: "frac" "edge" "resid" "multiref" "sub" "cip".  make_stream(mode, seed) -> (bytes, manifest).
manifest["coverage"] counts luma (xFrac, yFrac, outside?) over all inter partitions.
"""
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common.bitwriter import W, nal  # noqa: E402
from common import cavlc_tables as T, cavlc_ref  # noqa: E402
import mvpred_ref  # noqa: E402

W_MBS, H_MBS = 6, 4
PW, PH = W_MBS * 16, H_MBS * 16
N_MBS = W_MBS * H_MBS
MAX_REFS = 4
RAW = {"p16": 0, "p16x8": 1, "p8x16": 2, "p8x8": 3, "p8x8ref0": 4}
PARTS = {"p16": [(0, 0, 16, 16)], "p16x8": [(0, 0, 16, 8), (0, 8, 16, 8)], "p8x16": [(0, 0, 8, 16), (8, 0, 8, 16)]}
SUBS = {0: [(0, 0, 8, 8)], 1: [(0, 0, 8, 4), (0, 4, 8, 4)], 2: [(0, 0, 4, 8), (4, 0, 4, 8)],
        3: [(0, 0, 4, 4), (4, 0, 4, 4), (0, 4, 4, 4), (4, 4, 4, 4)]}
KINDNAME = {"p16": "16x16", "p16x8": "16x8", "p8x16": "8x16", "p8x8": "sub", "p8x8ref0": "sub"}
CBP_CODE = {v: k for k, v in enumerate(cavlc_ref.CBP_INTER)}
LUMA_XY = [(2 * ((b >> 2) & 1) + (b & 1), 2 * (b >> 3) + ((b >> 1) & 1)) for b in range(16)]
LUMA_IDX = {xy: i for i, xy in enumerate(LUMA_XY)}
CH_XY = [(0, 0), (1, 0), (0, 1), (1, 1)]
CH_IDX = {xy: i for i, xy in enumerate(CH_XY)}

# mode -> dict(weights of macroblock kinds, resid probability, location weights, qp range, chroma offset, cip, pictures, slices)
MODES = {
    "frac": dict(kinds=[("skip", 2), ("p16", 8), ("p16x8", 3), ("p8x16", 3), ("p8x8", 2), ("pcm", 0.4), ("nxn", 0.4)],
                 resid=0.3, loc=(60, 15, 15, 10), qp=(16, 32), cqp=0, cip=0, pictures=14, maxslices=2),
    "edge": dict(kinds=[("skip", 1), ("p16", 5), ("p16x8", 3), ("p8x16", 3), ("p8x8", 5), ("pcm", 0.3), ("nxn", 0.3)],
                 resid=0.25, loc=(10, 25, 30, 35), qp=(14, 30), cqp=3, cip=0, pictures=14, maxslices=2),
    "resid": dict(kinds=[("skip", 2), ("p16", 6), ("p16x8", 3), ("p8x16", 3), ("p8x8", 3), ("pcm", 0.6), ("nxn", 0.8)],
                  resid=0.9, loc=(70, 10, 10, 10), qp=(2, 40), cqp=-7, cip=0, pictures=14, maxslices=3),
    "multiref": dict(kinds=[("skip", 3), ("p16", 5), ("p16x8", 3), ("p8x16", 3), ("p8x8", 3), ("p8x8ref0", 1), ("pcm", 0.5),
                            ("nxn", 0.5)],
                     resid=0.5, loc=(50, 20, 15, 15), qp=(10, 34), cqp=5, cip=0, pictures=14, maxslices=4),
    "sub": dict(kinds=[("skip", 0.5), ("p16", 1), ("p16x8", 1), ("p8x16", 1), ("p8x8", 12), ("pcm", 0.3), ("nxn", 0.3)],
                resid=0.5, loc=(45, 20, 20, 15), qp=(12, 34), cqp=-3, cip=0, pictures=12, maxslices=2),
    "cip": dict(kinds=[("skip", 2), ("p16", 5), ("p16x8", 2), ("p8x16", 2), ("p8x8", 2), ("pcm", 0.8), ("nxn", 2)],
                resid=0.6, loc=(60, 15, 15, 10), qp=(10, 36), cqp=2, cip=1, pictures=12, maxslices=3),
}


# ------------------------------------------------------------------ parameter sets
def sps_nal():
    w = W()
    w.raw(8, 66); w.raw(8, 0); w.raw(8, 30)
    w.ue(0)
    w.ue(0)                               # log2_max_frame_num_minus4 -> 4 bits
    w.ue(2)                               # pic_order_cnt_type 2: output order = decoding order
    w.ue(MAX_REFS)
    w.raw(1, 0)
    w.ue(W_MBS - 1); w.ue(H_MBS - 1)
    w.raw(1, 1); w.raw(1, 1); w.raw(1, 0); w.raw(1, 0)
    return nal(0x67, w.rbsp())


def pps_nal(cqp, cip):
    w = W()
    w.ue(0); w.ue(0)
    w.raw(1, 0); w.raw(1, 0)
    w.ue(0)
    w.ue(0); w.ue(0)
    w.raw(1, 0); w.raw(2, 0)
    w.se(0); w.se(0); w.se(cqp)           # pic_init_qp 26, pic_init_qs, chroma_qp_index_offset
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, cip); w.raw(1, 0)
    return nal(0x68, w.rbsp())


# ------------------------------------------------------------------ CAVLC block writer (copied from Chapter 5.5)
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


class Counts:
    """total_coeff of every 4x4 block so far (for nC, 9.2.1); slice ownership for availability."""

    def __init__(self):
        self.luma, self.chroma, self.slice_of = {}, {}, {}

    def nc(self, addr, cur_slice, plane, blk, mine):
        xy, idx, n = (LUMA_XY, LUMA_IDX, 4) if plane == 0 else (CH_XY, CH_IDX, 2)
        x, y = xy[blk]

        def neighbour(dx, dy):
            nx, ny = x + dx, y + dy
            if nx >= 0 and ny >= 0:
                return mine[idx[(nx, ny)]]
            if nx < 0:
                o = addr - 1
                ok = addr % W_MBS > 0 and self.slice_of.get(o) == cur_slice
                nx = n - 1
            else:
                o = addr - W_MBS
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


# ------------------------------------------------------------------ content
def texture(rng):
    """384 samples (16x16 Y, 8x8 Cb, 8x8 Cr), values 1..255 (0 would be fine in I_PCM, but 1 avoids emulation-prevention noise)."""
    kind = rng.choice(["noise", "noise", "mild", "checker", "ramp", "mildlow", "mildhigh"])
    out = []
    for plane_n, n in ((0, 16), (1, 8), (2, 8)):
        for y in range(n):
            for x in range(n):
                if kind == "noise":
                    v = rng.randrange(1, 256)
                elif kind == "mild":
                    v = 118 + rng.randrange(0, 20)
                elif kind == "mildlow":
                    v = 2 + rng.randrange(0, 8)
                elif kind == "mildhigh":
                    v = 247 + rng.randrange(0, 9)
                elif kind == "checker":
                    v = 255 if (x + y) & 1 else 1
                else:
                    v = max(1, min(255, 16 * x + 8 * y + rng.randrange(0, 24)))
                out.append(v)
    return out


def rand_coeffs(rng, n, amp, density):
    return [rng.choice((-1, 1)) * (rng.randint(1, amp) if rng.random() < 0.8 else 1) if rng.random() < density else 0
            for _ in range(n)]


def amp_for(qp):
    return max(1, min(40, 1200 // (29 << (qp // 6))))


def write_te(w, v, active):
    if active == 1:
        assert v == 0
    elif active == 2:
        w.raw(1, 1 - v)
    else:
        w.ue(v)


class Gen:
    def __init__(self, mode, rng):
        self.m = MODES[mode]
        self.rng = rng
        self.fracs = [(a, b) for a in range(4) for b in range(4)]
        self.fi = rng.randrange(16)
        self.cov = Counter()

    def pick_target(self, X, Y, w, h):
        """Target vector (quarter samples) for a block at (X, Y) of the current picture."""
        rng = self.rng
        xf, yf = self.fracs[self.fi % 16]
        self.fi += 1
        loc = rng.choices(["in", "touch", "half", "far"], self.m["loc"])[0]

        def pos(coord, size, limit):
            if loc == "in":
                return rng.randint(0, max(0, limit - size))
            if loc == "touch":
                return rng.choice([rng.randint(-3, 2), rng.randint(limit - size - 2, limit - size + 3)])
            if loc == "half":
                return rng.choice([rng.randint(-size + 1, -1), rng.randint(limit - size + 1, limit - 1)])
            return rng.choice([-1, 1]) * rng.randint(20, 200) + (0 if rng.random() < 0.5 else limit)
        xi, yi = pos(X, w, PW), pos(Y, h, PH)
        if rng.random() < 0.3:                      # keep one component inside: edge in one direction only
            if rng.random() < 0.5:
                xi = rng.randint(0, max(0, PW - w))
            else:
                yi = rng.randint(0, max(0, PH - h))
        return ((xi - X) * 4 + xf, (yi - Y) * 4 + yf)

    def note(self, X, Y, w, h, mv):
        xi, yi = X + (mv[0] >> 2), Y + (mv[1] >> 2)
        out = xi - 2 < 0 or yi - 2 < 0 or xi + w + 3 > PW or yi + h + 3 > PH
        self.cov[(mv[0] & 3, mv[1] & 3, out)] += 1


def write_slice(g, plan, p, st, cnt, entries):
    rng = g.rng
    idr = p == 0
    w = W()
    sl = plan["index"]
    w.ue(plan["first"]); w.ue(2 if idr else 0); w.ue(0)
    w.raw(4, p % 16)
    if idr:
        w.ue(0)
    else:
        w.raw(1, 1 if plan["override"] else 0)
        if plan["override"]:
            w.ue(plan["active"] - 1)
        w.raw(1, 0)
    if idr:
        w.raw(1, 0); w.raw(1, 0)
    else:
        w.raw(1, 0)
    slice_qp = plan["slice_qp"]
    w.se(slice_qp - 26)
    w.ue(1)                               # disable_deblocking_filter_idc = 1
    hend = w.pos()
    active = plan["active"]
    qlo, qhi = g.m["qp"]
    qp = slice_qp
    pend = 0
    for i, kind in enumerate(plan["kinds"]):
        a = plan["first"] + i
        cnt.slice_of[a] = sl
        if idr:
            w.ue(25)
            while len(w.bits) % 8:
                w.raw(1, 0)
            for v in texture(rng):
                w.raw(8, v)
            st.store_intra(sl, a)
            cnt.luma[a], cnt.chroma[a] = [16] * 16, ([16] * 4, [16] * 4)
            continue
        if kind == "skip":
            pend += 1
            mv = st.skip_mv(sl, a)
            st.store(sl, a, 0, 0, 16, 16, 0, mv)
            g.note(16 * (a % W_MBS), 16 * (a // W_MBS), 16, 16, mv)
            cnt.luma[a], cnt.chroma[a] = [0] * 16, ([0] * 4, [0] * 4)
            continue
        w.ue(pend)
        pend = 0
        if kind == "pcm":
            w.ue(30)
            while len(w.bits) % 8:
                w.raw(1, 0)
            for v in texture(rng):
                w.raw(8, v)
            st.store_intra(sl, a)
            cnt.luma[a], cnt.chroma[a] = [16] * 16, ([16] * 4, [16] * 4)
            continue
        if kind == "nxn":
            w.ue(5)
            for _ in range(16):
                w.raw(1, 1)               # prev_intra4x4_pred_mode_flag = 1 (predicted mode; every block ends up DC)
            w.ue(0)                       # intra_chroma_pred_mode DC
            w.ue(3)                       # coded_block_pattern codeNum 3 = 0 (intra column)
            st.store_intra(sl, a)
            cnt.luma[a], cnt.chroma[a] = [0] * 16, ([0] * 4, [0] * 4)
            continue
        # ---- inter macroblock
        w.ue(RAW[kind])
        if kind in ("p8x8", "p8x8ref0"):
            subs = [rng.randrange(4) for _ in range(4)]
            if g.mode == "sub" and rng.random() < 0.6:
                subs = [rng.choice([1, 2, 3, 3]) for _ in range(4)]
            for s in subs:
                w.ue(s)
            refs = [0] * 4 if kind == "p8x8ref0" else [rng.randrange(active) for _ in range(4)]
            blocks = []
            for qi, s in enumerate(subs):
                ox, oy = (qi & 1) * 8, (qi >> 1) * 8
                for (x, y, ww, hh) in SUBS[s]:
                    blocks.append((ox + x, oy + y, ww, hh, refs[qi]))
            ref_list = refs
        else:
            refs = [rng.randrange(active) for _ in PARTS[kind]]
            blocks = [(x, y, ww, hh, r) for (x, y, ww, hh), r in zip(PARTS[kind], refs)]
            ref_list = refs
        if kind != "p8x8ref0":
            for r in ref_list:
                write_te(w, r, active)
        mvds = []
        for k, (x, y, ww, hh, r) in enumerate(blocks):
            idx = k if kind in ("p16x8", "p8x16") else 0
            mvp = st.predict(sl, a, x, y, ww, hh, r, KINDNAME[kind], idx)
            X, Y = 16 * (a % W_MBS) + x, 16 * (a // W_MBS) + y
            tgt = g.pick_target(X, Y, ww, hh) if rng.random() < 0.9 else mvp
            g.note(X, Y, ww, hh, tgt)
            mvds.append((tgt[0] - mvp[0], tgt[1] - mvp[1]))
            st.store(sl, a, x, y, ww, hh, r, tgt)
        for d in mvds:
            w.se(d[0]); w.se(d[1])
        # ---- residual
        if rng.random() < g.m["resid"]:
            cbp_l = rng.choice([0, 1, 2, 4, 8, 15, 15, rng.randrange(16)])
            cbp_c = rng.choice([0, 1, 2, 2])
            cbp = cbp_l + 16 * cbp_c
        else:
            cbp = 0
        w.ue(CBP_CODE[cbp])
        lc, cc = [0] * 16, ([0] * 4, [0] * 4)
        if cbp:
            newqp = max(qlo, min(qhi, qp + rng.choice([0, 0, rng.randint(-6, 6), rng.randint(-30, 30)])))
            delta = newqp - qp
            if not -26 <= delta <= 25:
                newqp = qp
                delta = 0
            if rng.random() < 0.05:                    # a wrapping delta: QP_Y = (QP_pred + delta + 52) % 52
                d2 = rng.choice([-26, 25])
                newqp = (qp + d2 + 52) % 52
                delta = d2
                newqp = max(0, min(51, newqp))
            qp = newqp
            w.se(delta)
            amp = amp_for(qp)
            dens = rng.choice([0.08, 0.3, 0.8])
            for blk in range(16):
                if (cbp_l >> (blk >> 2)) & 1:
                    lc[blk] = write_block(w, cnt.nc(a, sl, 0, blk, lc), 16, rand_coeffs(rng, 16, amp, dens))
            if cbp_c:
                for _ in range(2):
                    write_block(w, -1, 4, rand_coeffs(rng, 4, amp, 0.6))
            if cbp_c == 2:
                for p_ in range(2):
                    for k in range(4):
                        cc[p_][k] = write_block(w, cnt.nc(a, sl, 1 + p_, k, cc[p_]), 15, rand_coeffs(rng, 15, amp, 0.3))
        cnt.luma[a], cnt.chroma[a] = lc, cc
    if pend:
        w.ue(pend)
    return nal(0x65 if idr else 0x41, w.rbsp()), dict(first=plan["first"], header_end_bit=hend)


def make_stream(mode="frac", seed=1):
    rng = random.Random(seed * 1009 + sum(map(ord, mode)))
    g = Gen(mode, rng)
    g.mode = mode
    m = g.m
    data = sps_nal() + pps_nal(m["cqp"], m["cip"])
    pics = []
    index = 0
    for p in range(m["pictures"]):
        st = mvpred_ref.PicState(W_MBS, H_MBS)
        cnt = Counts()
        if p == 0:
            cuts = [0, N_MBS]
        else:
            k = rng.randint(1, m["maxslices"])
            cuts = sorted({0} | set(rng.sample(range(1, N_MBS), k - 1))) + [N_MBS]
        entries = []
        for a, b in zip(cuts, cuts[1:]):
            n_ref = min(p, MAX_REFS)
            active = (rng.randint(1, max(1, n_ref)) if mode in ("multiref", "cip", "resid") else min(2, n_ref)) if p else 1
            if mode in ("frac", "edge", "sub") and p and rng.random() < 0.5:
                active = 1
            kinds = [rng.choices([x for x, _ in m["kinds"]], [y for _, y in m["kinds"]])[0] for _ in range(b - a)]
            plan = dict(first=a, kinds=kinds, index=index, active=active, override=(active != 1),
                        slice_qp=max(m["qp"][0], min(m["qp"][1], 26 + rng.randint(-8, 8))))
            nb, ent = write_slice(g, plan, p, st, cnt, entries)
            data += nb
            entries.append(ent)
            index += 1
        pics.append(dict(slices=entries))
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, mode=mode, pictures=pics, coverage=dict(g.cov))


def missing_coverage(cov):
    """Luma positions (xFrac, yFrac) x {window inside, window touches/leaves the picture} never produced."""
    return [(xf, yf, o) for xf in range(4) for yf in range(4) for o in (False, True) if not cov.get((xf, yf, o))]


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "frac"
    out = sys.argv[2] if len(sys.argv) > 2 else mode + ".264"
    d, man = make_stream(mode)
    open(out, "wb").write(d)
    print(out, len(d), "bytes; missing coverage:", missing_coverage(man["coverage"]))
