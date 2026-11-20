"""SPOILER-adjacent: synthetic stream generator for the Chapter 7.7 harness (bit writer: common/bitwriter.py).

Tiny CAVLC Main-profile streams (96x80 = 6x5 macroblocks, picture order count type 0, output order = decoding order) with
weighted_pred_flag = 1 and a random but valid pred_weight_table in every P slice header: logWD 0..7 for luma and chroma, negative
weights, positive and negative offsets (also extreme ones that force clipping at 0 and 255), weight flags per reference index and per
plane (luma only, chroma only, none, both), several slices per picture with different tables.  Macroblocks are I_PCM (random samples
with extreme values), P_Skip, and P_L0_16x16 with random ref_idx and small random mvd (fractional vectors included), no residual.
Reference lists are shaped by random ref_pic_list_modification commands that also list one picture at several indices (the
x264 weightp situation).  The generator drives refmgmt_ref.Dpb only to choose valid commands; the truth is ffmpeg's decode.
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common.bitwriter import W, nal  # noqa: E402
import refmgmt_ref as R  # noqa: E402

W_MBS, H_MBS = 6, 5
N_MBS = W_MBS * H_MBS
PPS_DEFAULT_ACTIVE = [1, 2, 4]

# denoms: allowed logWD values; wmode: weight style; flags: (p_luma_flag, p_chroma_flag); p_mod / p_dup: modification probabilities;
# deblock: disable_deblocking_filter_idc (1 = off); pcm_amp: content amplitude; p_skip: share of skip candidates
SCENARIOS = {
    "denoms": dict(pictures=26, refs=3, max_active=3, denoms=range(0, 8), wmode="mixed", flags=(0.8, 0.8), p_mod=0.0, p_dup=0, deblock=1,
                   slices=1, p_skip=0.2, off=40),
    "clip": dict(pictures=22, refs=2, max_active=2, denoms=(0, 1, 2, 5, 6), wmode="big", flags=(0.9, 0.9), p_mod=0.0, p_dup=0, deblock=1,
                 slices=1, p_skip=0.1, off=127),
    "neg": dict(pictures=26, refs=3, max_active=3, denoms=range(0, 8), wmode="neg", flags=(0.9, 0.9), p_mod=0.2, p_dup=0.3, deblock=1,
                slices=1, p_skip=0.1, off=100),
    "flags": dict(pictures=30, refs=4, max_active=4, denoms=range(0, 8), wmode="mixed", flags=(0.4, 0.4), p_mod=0.3, p_dup=0.3, deblock=1,
                  slices=2, p_skip=0.15, off=30),
    "chroma": dict(pictures=26, refs=3, max_active=3, denoms=range(0, 8), wmode="mixed", flags=(0.0, 1.0), p_mod=0.2, p_dup=0.2,
                   deblock=1, slices=1, p_skip=0.15, off=60),
    "lumaonly": dict(pictures=22, refs=3, max_active=3, denoms=range(0, 8), wmode="mixed", flags=(1.0, 0.0), p_mod=0.2, p_dup=0.2,
                     deblock=1, slices=1, p_skip=0.15, off=60),
    "skip": dict(pictures=26, refs=3, max_active=3, denoms=range(0, 8), wmode="mixed", flags=(0.7, 0.7), p_mod=0.5, p_dup=0.5,
                 deblock=1, slices=2, p_skip=0.7, off=30),
    "manyrefs": dict(pictures=44, refs=8, max_active=10, denoms=range(0, 8), wmode="mixed", flags=(0.7, 0.6), p_mod=0.9, p_dup=0.6,
                     deblock=1, slices=2, p_skip=0.2, off=40),
    "dupdeblock": dict(pictures=30, refs=4, max_active=6, denoms=(5, 6, 7), wmode="near", flags=(0.9, 0.9), p_mod=1.0, p_dup=0.9,
                       deblock=0, slices=1, p_skip=0.2, off=6, zero_mvd=True),
}


def sps_nal(refs, log2=4):
    w = W()
    w.raw(8, 77)                          # Main profile
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    w.ue(log2 - 4)
    w.ue(0)                               # pic_order_cnt_type 0
    w.ue(4)                               # 8-bit lsb
    w.ue(refs)
    w.raw(1, 0)
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x67, w.rbsp())


def pps_nal(pps_id, weighted=1):
    w = W()
    w.ue(pps_id)
    w.ue(0)
    w.raw(1, 0)                           # CAVLC
    w.raw(1, 0)
    w.ue(0)
    w.ue(PPS_DEFAULT_ACTIVE[pps_id] - 1)
    w.ue(0)
    w.raw(1, weighted)                    # weighted_pred_flag
    w.raw(2, 0)                           # weighted_bipred_idc
    w.se(0)
    w.se(0)
    w.se(0)
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x68, w.rbsp())


def write_te(w, v, active):
    if active == 1:
        assert v == 0
    elif active == 2:
        w.raw(1, 1 - v)
    else:
        w.ue(v)


def abs_diff_for(pred, target_num, maxp):
    nw = target_num + maxp if target_num < 0 else target_num
    d0 = (pred - nw) % maxp or maxp
    d1 = (nw - pred) % maxp or maxp
    return [(0, d0 - 1), (1, d1 - 1)], nw


def choose_mods(d, rng, cur_fn, active, p_dup):
    cands = list(d.short())
    if not cands:
        return []
    mods, pred, prev = [], cur_fn, None
    for _ in range(rng.randint(1, min(active, 5))):
        f = prev if (prev is not None and rng.random() < p_dup) else rng.choice(cands)
        prev = f
        opts, nw = abs_diff_for(pred, d.wrap(f, cur_fn), d.max_fn)
        mods.append(rng.choice(opts))
        pred = nw
    return mods


def make_weights(rng, sc, active):
    """-> (denom_y, denom_c, [(luma_flag, wy, oy, chroma_flag, wcb, ocb, wcr, ocr)])"""
    dy, dc = rng.choice(list(sc["denoms"])), rng.choice(list(sc["denoms"]))

    def wgt(d):
        base = 1 << d
        m = sc["wmode"]
        if m == "near":
            w = base + rng.randint(-max(1, base // 8), max(1, base // 8))
        elif m == "neg":
            w = round(base * rng.uniform(-1.5, 2.0))
        elif m == "big":
            w = round(base * rng.uniform(1.5, 4.0)) if rng.random() < 0.7 else round(base * rng.uniform(0.0, 0.5))
        else:
            w = round(base * rng.uniform(-0.3, 2.5))
        return max(-128, min(127, w))

    def off():
        return rng.randint(-sc["off"], sc["off"])
    ents = []
    for _ in range(active):
        lf = rng.random() < sc["flags"][0]
        cf = rng.random() < sc["flags"][1]
        ents.append((int(lf), wgt(dy), off(), int(cf), wgt(dc), off(), wgt(dc), off()))
    return dy, dc, ents


def write_weights(w, wt):
    dy, dc, ents = wt
    w.ue(dy)
    w.ue(dc)
    for lf, wy, oy, cf, wcb, ocb, wcr, ocr in ents:
        w.raw(1, lf)
        if lf:
            w.se(wy)
            w.se(oy)
        w.raw(1, cf)
        if cf:
            w.se(wcb)
            w.se(ocb)
            w.se(wcr)
            w.se(ocr)


def pcm_samples(rng, amp):
    """384 samples: smooth ramps (so interpolation matters) with extremes (so clipping matters)."""
    base = rng.randint(0, 255)
    out = []
    for n, (sw, sh) in enumerate(((16, 16), (8, 8), (8, 8))):
        gx, gy = rng.uniform(-amp, amp) / sw, rng.uniform(-amp, amp) / sh
        b = base if n == 0 else rng.randint(0, 255)
        for y in range(sh):
            for x in range(sw):
                v = b + gx * x + gy * y + rng.randint(-3, 3)
                if rng.random() < 0.02:
                    v = rng.choice((0, 255))
                out.append(max(0, min(255, int(v))))
    return bytes(out)


def slice_nal(rng, sc, poc, first, last, idr, ref_idc, fn, idr_id, pps, active, override, mods, lst, wt):
    w = W()
    w.ue(first)
    w.ue(2 if idr else 0)
    w.ue(pps)
    w.raw(4, fn)
    if idr:
        w.ue(idr_id)
    w.raw(8, poc)
    if not idr:
        w.raw(1, 1 if override else 0)
        if override:
            w.ue(active - 1)
        if mods:
            w.raw(1, 1)
            for idc, v in mods:
                w.ue(idc)
                w.ue(v)
            w.ue(3)
        else:
            w.raw(1, 0)
        write_weights(w, wt)
    if ref_idc:
        if idr:
            w.raw(1, 0)
            w.raw(1, 0)
        else:
            w.raw(1, 0)
    w.se(0)
    w.ue(sc["deblock"])
    if sc["deblock"] != 1:
        w.se(0)
        w.se(0)
    valid = [i for i, e in enumerate(lst) if e is not None] if lst else []
    pend = 0
    for addr in range(first, last):
        kinds, wts = ["pcm"], [1]
        if valid:
            kinds += ["p16"]
            wts += [8]
            if 0 in valid:
                kinds += ["skip"]
                wts += [int(10 * sc["p_skip"] / max(0.05, 1 - sc["p_skip"])) or 1]
        kind = "pcm" if idr else rng.choices(kinds, wts)[0]
        if kind == "skip":
            pend += 1
            continue
        if not idr:
            w.ue(pend)
        pend = 0
        if kind == "pcm":
            w.ue(25 if idr else 30)
            while len(w.bits) % 8:
                w.raw(1, 0)
            for s in pcm_samples(rng, 120):
                w.raw(8, s)
        else:
            w.ue(0)
            write_te(w, rng.choice(valid), active)
            if sc.get("zero_mvd"):
                w.se(0)
                w.se(0)
            else:
                w.se(rng.randint(-6, 6))
                w.se(rng.randint(-6, 6))
            w.ue(0)
    if pend:
        w.ue(pend)
    return nal(0x65 if idr else (0x41 if ref_idc else 0x01), w.rbsp())


def make_stream(name, seed=7):
    sc = SCENARIOS[name]
    rng = random.Random(seed * 131 + sum(map(ord, name)))
    data = sps_nal(sc["refs"]) + b"".join(pps_nal(i) for i in range(3))
    d = R.Dpb(sc["refs"], 4, False)
    idr_id, poc_n = 0, 0
    for p in range(sc["pictures"]):
        idr = p == 0
        ref_idc = 3 if idr else (0 if p % 9 == 5 else rng.choice([1, 2, 3]))
        fn = 0 if idr else (d.prev_ref_fn + 1) % 16
        nsl = 1 if idr else rng.randint(1, sc["slices"])
        cut = rng.randrange(1, N_MBS) if nsl == 2 else None
        bounds = [(0, N_MBS)] if nsl == 1 else [(0, cut), (cut, N_MBS)]
        pps = rng.randrange(3)
        for a, b in bounds:
            if idr:
                active, override, mods, lst, wt = 1, False, [], None, None
            else:
                nfr = len(d.frames)
                if rng.random() < 0.5 and PPS_DEFAULT_ACTIVE[pps] <= max(nfr, 1) + 3:
                    active, override = PPS_DEFAULT_ACTIVE[pps], False
                else:
                    active, override = rng.randint(1, sc["max_active"]), True
                mods = choose_mods(d, rng, fn, active, sc["p_dup"]) if rng.random() < sc["p_mod"] else []
                lst = d.build_list(fn, active, mods)
                if lst[0] is None:                      # list 0 must be usable: fall back to a modification that names a frame
                    mods = choose_mods(d, rng, fn, active, sc["p_dup"])
                    lst = d.build_list(fn, active, mods)
                wt = make_weights(rng, sc, active)
            data += slice_nal(rng, sc, (2 * poc_n) % 256, a, b, idr, ref_idc, fn, idr_id, pps, active, override, mods, lst, wt)
        d.finish_picture(p, fn, idr, ref_idc, 0, None)
        poc_n += 1
    return data
