"""SPOILER-adjacent: synthetic stream generator for the Chapter 7.9 harness (bit writer: common/bitwriter.py).

Chapter 7.8's generator (bgen.py) extended with everything direct prediction and implicit weights need.  Tiny CAVLC Main-profile
streams (96x80 = 6x5 macroblocks), picture order count type 0, I, P and B pictures in a random but valid structure (an anchor
every 1..maxb+1 display positions, the B pictures between two anchors in random decoding order, as reference pictures or not).

New in this chapter:
  * B_Skip (mb_skip_run in B slices), B_Direct_16x16 (mb_type 0, coded_block_pattern 0) and B_Direct_8x8 sub-macroblocks inside
    B_8x8, with direct_spatial_mv_pred_flag chosen per slice (scenario probability) and direct_8x8_inference_flag per stream;
  * weighted_bipred_idc 2 (implicit weights), and POC distances that are not all 2: random steps between display positions, so
    DistScaleFactor takes many values, including ones that hit the clipping and the 32/32 fallbacks (POC lsb has 10 bits);
  * memory management control operations (MMCO 1 = forget a short-term frame, MMCO 4 + 6 / 3 = long-term frames) and
    long_term_pic_num entries (idc 2) in list modifications, so long-term pictures appear in lists, as RefPicList1[0] as well;
  * 'static' content (mvd = 0 most of the time) next to 'moving' content (mvd of +-1..2), so colZeroFlag goes both ways;
  * intra macroblocks (I_PCM, Intra16x16) inside P and B pictures: intra co-located blocks.
Every macroblock is residual-free (coded_block_pattern 0); other types: the 22 non-direct B types, P types, I_PCM, Intra16x16.

Validity: a temporal-direct slice is only legal when every picture the co-located blocks refer to is in RefPicList0.  The generator
does not know the motion data; write_stream() therefore asks the reference decoder (pixels=False) and, when it reports a slice that
is not legal, regenerates the stream with that slice made spatial (or direct-free).  Every slice has its own random stream, so such
a change touches nothing else's syntax.  The truth for the pixels is ffmpeg's decode.  The JSON manifest lists the macroblocks the
generator wrote (name, mb_type, sub, refidx, mvd, refidx1, mvd1).
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common.bitwriter import W, nal  # noqa: E402
import brefmgmt_ref as R  # noqa: E402

W_MBS, H_MBS = 6, 5
N_MBS = W_MBS * H_MBS
LOG2_FN, LOG2_POC = 4, 10          # LOG2_POC is set per scenario in make_stream
PPS_ACTIVE0 = [1, 2, 3]
PPS_ACTIVE1 = [1, 1, 2]

BASE = dict(pictures=30, refs=3, maxb=2, p_refb=0.3, p_tail=0.0, p_idr=0.0, idr_lt=False, p_mod=0.0, p_dup=0.0, max_a0=3, max_a1=2,
            slices=1, mixed=False, weights=None, deblock=1, p_intra16=0.05, p_pcm=0.08, p_skip=0.2, mvd=6, p_i_anchor=0.05,
            p_big_mvd=0.0, bipred_idc=2, idr_lt_prob=0.5,
            # new in 7.9
            inference=1, p_bskip=0.25, p_direct16=0.1, p_sub_direct=0.25, spatial=0.5, mmco=0.0, mvd0=0.5, poc_log2=10,
            poc_steps=[2], lt_mods=0.0, ffmpeg_ok=True)
SCENARIOS = {
    # spatial / temporal x direct_8x8_inference_flag, with moving and static content
    "sp_inf1": dict(BASE, spatial=1.0, pictures=40, refs=4, maxb=3, p_refb=0.4, p_mod=0.2, max_a0=3, max_a1=3),
    "sp_inf0": dict(BASE, spatial=1.0, inference=0, pictures=40, refs=4, maxb=3, p_refb=0.4, p_mod=0.2, max_a0=3, max_a1=3),
    "tmp_inf1": dict(BASE, spatial=0.0, pictures=40, refs=4, maxb=3, p_refb=0.4, p_mod=0.15, max_a0=4, max_a1=2, poc_steps=[2, 2, 3, 5]),
    "tmp_inf0": dict(BASE, spatial=0.0, inference=0, pictures=40, refs=4, maxb=3, p_refb=0.4, p_mod=0.15, max_a0=4, max_a1=2,
                     poc_steps=[2, 2, 3, 5]),
    # slices of different modes in one picture, co-located data read across slices, P and B slices mixed
    "mixed": dict(BASE, pictures=40, refs=4, maxb=2, slices=3, mixed=True, p_mod=0.3, max_a0=4, max_a1=3, p_refb=0.5, deblock=2,
                  poc_steps=[2, 3, 4]),
    # long-term pictures (MMCO 4 / 6 / 3, idc 2 entries): L1[0] long-term (colZeroFlag needs a short-term picture), td / mv fallbacks
    "lt": dict(BASE, pictures=48, refs=4, maxb=2, p_refb=0.4, mmco=0.6, lt_mods=0.5, p_mod=0.6, max_a0=4, max_a1=3, mvd0=0.7,
               poc_steps=[2, 3]),
    "lt_tmp": dict(BASE, spatial=0.0, pictures=48, refs=4, maxb=2, p_refb=0.4, mmco=0.6, lt_mods=0.5, p_mod=0.6, max_a0=4, max_a1=3,
                   mvd0=0.7, poc_steps=[2, 3]),
    # implicit weights with large and varied POC distances: clipping of tb / td, the 32/32 fallbacks, extreme DistScaleFactor
    "poc_far": dict(BASE, pictures=44, refs=4, maxb=3, p_refb=0.5, p_mod=0.4, max_a0=4, max_a1=3, mvd0=0.2, poc_steps=[1, 4, 9, 30, 70, 140],
                    spatial=0.3, deblock=0),
    "poc_far_tmp": dict(BASE, pictures=44, refs=4, maxb=3, p_refb=0.5, p_mod=0.4, max_a0=4, max_a1=3, mvd0=0.2,
                        poc_steps=[1, 4, 9, 30, 70, 140], spatial=0.0, deblock=0),
    # B pictures whose references are all in the past (list 1 = list 0 with the swap rule: pic0 == pic1 happens, td = 0)
    "tail": dict(BASE, pictures=36, refs=4, maxb=1, p_tail=0.4, max_a0=3, max_a1=3, p_mod=0.3, poc_steps=[2, 3]),
    # many intra macroblocks: intra co-located blocks (refIdxCol = -1), neighbours that are intra
    "intra": dict(BASE, pictures=36, refs=3, maxb=2, p_intra16=0.25, p_pcm=0.2, deblock=0, p_skip=0.05, p_refb=0.5, mvd0=0.8),
    # almost everything static: colZeroFlag is 1 for most blocks
    "static": dict(BASE, pictures=36, refs=3, maxb=3, p_refb=0.5, mvd0=0.95, mvd=2, p_pcm=0.03, p_intra16=0.02, p_bskip=0.4,
                   p_direct16=0.15, deblock=1),
    # moving content: vectors around the +-1 limit of colZeroFlag
    "moving": dict(BASE, pictures=36, refs=3, maxb=3, p_refb=0.5, mvd0=0.35, mvd=2, p_pcm=0.03, p_intra16=0.02, p_bskip=0.3,
                   deblock=1),
    # many references, modification commands: refIdxL0 mapping of temporal direct (MapColToList0, lowest index with duplicates)
    "map": dict(BASE, spatial=0.0, pictures=48, refs=6, maxb=3, p_refb=0.6, p_mod=0.9, p_dup=0.6, max_a0=6, max_a1=4, slices=2,
                poc_steps=[2, 3, 5]),
    # a co-located picture whose slices used DIFFERENT reference lists: refIdxCol must be mapped through the lists of the slice that
    # holds the co-located block.  ffmpeg keeps one table per picture and decodes this differently from the standard, so the truth of
    # this stream is the reference decoder (ffmpeg_ok = False); the other scenarios never read such a picture in temporal mode.
    "colslices": dict(BASE, spatial=0.1, pictures=44, refs=5, maxb=2, slices=3, p_refb=0.5, p_mod=0.9, p_dup=0.3, max_a0=5, max_a1=3,
                      poc_steps=[2, 3, 5], ffmpeg_ok=False, deblock=2),
    # temporal direct with duplicate entries in RefPicList0 and EXPLICIT weights (idc 1): the lowest index of MapColToList0 selects the
    # weights (two list entries name the same picture but carry different weights)
    "dup_w": dict(BASE, spatial=0.0, pictures=44, refs=5, maxb=3, p_refb=0.5, p_mod=1.0, p_dup=0.8, max_a0=6, max_a1=3,
                  weights="explicit", bipred_idc=1, poc_steps=[2, 3, 4], mvd0=0.6),
    # explicit weights next to implicit ones (idc 2 in B, explicit tables in P)
    "wp": dict(BASE, pictures=36, refs=3, maxb=2, weights="explicit", p_mod=0.3, poc_steps=[2, 4]),
    "idc1": dict(BASE, pictures=30, refs=3, maxb=2, weights="explicit", bipred_idc=1, p_mod=0.3, poc_steps=[2, 3]),
}


# ---------------------------------------------------------------- Table 7-14 and 7-18, typed independently of common/inter_ref.py
PAIRS = [("L0", "L0"), ("L1", "L1"), ("L0", "L1"), ("L1", "L0"), ("L0", "Bi"), ("L1", "Bi"), ("Bi", "L0"), ("Bi", "L1"), ("Bi", "Bi")]
SUBS = {1: ("L0", 1), 2: ("L1", 1), 3: ("Bi", 1), 4: ("L0", 2), 5: ("L0", 2), 6: ("L1", 2), 7: ("L1", 2), 8: ("Bi", 2), 9: ("Bi", 2),
        10: ("L0", 4), 11: ("L1", 4), 12: ("Bi", 4)}                      # sub_mb_type -> (mode, number of sub-partitions)


def b_type(raw):
    """-> (name, [mode of each partition]) for raw 1..21; raw 22 = B_8x8."""
    if raw in (1, 2, 3):
        m = ("L0", "L1", "Bi")[raw - 1]
        return f"B_{m}_16x16", [m]
    k, odd = divmod(raw - 4, 2)
    a, b = PAIRS[k]
    return f"B_{a}_{b}_{'8x16' if odd else '16x8'}", [a, b]


def uses(mode, lst):
    return mode == "Bi" or mode == ("L0" if lst == 0 else "L1")


def sps_nal(refs, reorder, inference=1):
    w = W()
    w.raw(8, 77)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    w.ue(LOG2_FN - 4)
    w.ue(0)                               # pic_order_cnt_type 0
    w.ue(LOG2_POC - 4)
    w.ue(refs)
    w.raw(1, 0)
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)                           # frame_mbs_only
    w.raw(1, inference)                   # direct_8x8_inference_flag
    w.raw(1, 0)                           # no cropping
    w.raw(1, 1)                           # VUI
    for _ in range(4):
        w.raw(1, 0)                       # aspect ratio, overscan, video signal type, chroma loc
    w.raw(1, 0)                           # timing info
    w.raw(1, 0)
    w.raw(1, 0)                           # nal / vcl hrd
    w.raw(1, 0)                           # pic_struct_present
    w.raw(1, 1)                           # bitstream_restriction_flag
    w.raw(1, 1)
    w.ue(0)
    w.ue(0)
    w.ue(16)
    w.ue(16)
    w.ue(reorder)
    w.ue(max(refs, reorder) + 1)
    return nal(0x67, w.rbsp())


def pps_nal(pps_id, weighted_pred, bipred_idc):
    w = W()
    w.ue(pps_id)
    w.ue(0)
    w.raw(1, 0)
    w.raw(1, 0)
    w.ue(0)
    w.ue(PPS_ACTIVE0[pps_id] - 1)
    w.ue(PPS_ACTIVE1[pps_id] - 1)
    w.raw(1, weighted_pred)
    w.raw(2, bipred_idc)
    w.se(0)
    w.se(0)
    w.se(0)
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x68, w.rbsp())


def write_te(w, v, n):
    if n == 1:
        assert v == 0
    elif n == 2:
        w.raw(1, 1 - v)
    else:
        w.ue(v)


def abs_diff_for(pred, target_num, maxp):
    nw = target_num + maxp if target_num < 0 else target_num
    d0 = (pred - nw) % maxp or maxp
    d1 = (nw - pred) % maxp or maxp
    return [(0, d0 - 1), (1, d1 - 1)], nw


def choose_mods(d, rng, cur_fn, active, p_dup, p_lt=0.0):
    cands = [f for f in d.short()]
    lts = d.long()
    if not cands and not lts:
        return []
    mods, pred, prev = [], cur_fn, None
    for _ in range(rng.randint(1, min(active, 5))):
        if lts and (rng.random() < p_lt or not cands):
            f = rng.choice(lts)
            mods.append((2, f.lt))                     # long_term_pic_num = LongTermFrameIdx for frames
            prev = f
            continue
        f = prev if (prev is not None and prev.lt is None and rng.random() < p_dup) else rng.choice(cands)
        prev = f
        opts, nw = abs_diff_for(pred, d.wrap(f, cur_fn), d.max_fn)
        mods.append(rng.choice(opts))
        pred = nw
    return mods


def make_weights(rng, active0, active1):
    dy, dc = rng.randint(0, 5), rng.randint(0, 5)

    def table(n):
        ents = []
        for _ in range(n):
            lf, cf = rng.random() < 0.7, rng.random() < 0.6

            def wgt(d):
                base = 1 << d
                return max(-128, min(127, round(base * rng.uniform(-0.3, 1.5))))
            ents.append((int(lf), wgt(dy), rng.randint(-40, 40), int(cf), wgt(dc), rng.randint(-40, 40), wgt(dc), rng.randint(-40, 40)))
        return ents
    return dy, dc, table(active0), (table(active1) if active1 else None)


def write_table(w, ents):
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


def pcm_samples(rng, amp=120):
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


# ---------------------------------------------------------------- the picture plan
def make_plan(rng, sc):
    """-> list of picture dicts in decoding order: kind 'I'|'P'|'B', idr, ref_idc, disp (display index in the IDR period)."""
    plan = [dict(kind="I", idr=True, ref_idc=3, disp=0)]
    last = 0
    while len(plan) < sc["pictures"]:
        if rng.random() < sc["p_idr"]:
            plan.append(dict(kind="I", idr=True, ref_idc=3, disp=0))
            last = 0
            continue
        gap = 1 + rng.randint(0, sc["maxb"])
        disp = last + gap
        kind = "B" if rng.random() < sc["p_tail"] else ("I" if rng.random() < sc["p_i_anchor"] else "P")
        plan.append(dict(kind=kind, idr=False, ref_idc=rng.choice([1, 2, 3]), disp=disp))
        mid = list(range(last + 1, disp))
        rng.shuffle(mid)
        for d in mid:
            ref = rng.random() < sc["p_refb"]
            plan.append(dict(kind="B", idr=False, ref_idc=rng.choice([1, 2]) if ref else 0, disp=d))
        last = disp
    return plan[:sc["pictures"]]


# ---------------------------------------------------------------- macroblock writers
def nc_token(w, nc):
    w.raw(*{0: (1, 1), 1: (2, 3), 2: (4, 15), 3: (6, 3)}[0 if nc < 2 else 1 if nc < 4 else 2 if nc < 8 else 3])


SUBS[0] = ("Direct", 0)


def rand_mvd(rng, sc):
    """mvd of the 'static' / 'moving' content: zero with probability mvd0, else small (+-2), sometimes large."""
    if rng.random() < sc["mvd0"]:
        return (0, 0)
    if rng.random() < sc["p_big_mvd"]:
        a = sc["mvd"]
    else:
        a = min(sc["mvd"], 6)
    return (rng.randint(-a, a), rng.randint(-a, a))


def write_b_inter(w, rng, sc, valid, n_active, direct_ok):
    """One non-skipped, non-intra B macroblock after mb_skip_run: returns the manifest entry.  valid[l] = usable ref_idx values of
    list l.  direct_ok: B_Direct_16x16 and B_Direct_8x8 may be used."""
    can = [bool(valid[0]), bool(valid[1])]
    okmodes = ["L0"] * can[0] + ["L1"] * can[1] + ["Bi"] * (can[0] and can[1])
    if not okmodes and not direct_ok:
        return None
    if direct_ok and rng.random() < sc["p_direct16"]:
        w.ue(0)
        w.ue(0)                           # coded_block_pattern 0 (inter)
        return dict(kind="b", name="B_Direct_16x16", mb_type=0, sub="-", refidx="-", mvd="-", refidx1="-", mvd1="-")
    if not okmodes:
        raw = 22
    else:
        for _ in range(30):
            raw = rng.randint(1, 22)
            if raw == 22 or all(m in okmodes for m in b_type(raw)[1]):
                break
        else:
            raw = {"L0": 1, "L1": 2, "Bi": 3}[okmodes[0]]
    w.ue(raw)
    if raw == 22:
        subs = []
        for _ in range(4):
            if direct_ok and (rng.random() < sc["p_sub_direct"] or not okmodes):
                s = 0
            else:
                s = rng.choice([k for k, (m, n) in SUBS.items() if m in okmodes])
            subs.append(s)
            w.ue(s)
        units = [SUBS[s] for s in subs]
        name = "B_8x8"
    else:
        name, modes = b_type(raw)
        units = [(m, 1) for m in modes]
        subs = None
    refs = [[], []]
    for lst in (0, 1):
        for mode, n in units:
            if uses(mode, lst):
                v = rng.choice(valid[lst])
                refs[lst].append(v)
                write_te(w, v, n_active[lst])
    mvds = [[], []]
    for lst in (0, 1):
        for mode, n in units:
            if uses(mode, lst):
                for _ in range(n):
                    d = rand_mvd(rng, sc)
                    mvds[lst].append(d)
                    w.se(d[0])
                    w.se(d[1])
    w.ue(0)                               # coded_block_pattern 0 (inter)
    j = lambda xs: ",".join(map(str, xs)) if xs else "-"  # noqa: E731
    jm = lambda xs: ",".join(f"{a}:{b}" for a, b in xs) if xs else "-"  # noqa: E731
    return dict(kind="b", name=name, mb_type=raw, sub=j(subs) if subs else "-", refidx=j(refs[0]), mvd=jm(mvds[0]),
                refidx1=j(refs[1]), mvd1=jm(mvds[1]))


P_PARTS = {0: ("P_L0_16x16", 1), 1: ("P_L0_L0_16x8", 2), 2: ("P_L0_L0_8x16", 2)}


def write_p_inter(w, rng, sc, valid0, n0):
    raw = rng.choice([0, 0, 1, 2, 3])
    w.ue(raw)
    j = lambda xs: ",".join(map(str, xs)) if xs else "-"  # noqa: E731
    jm = lambda xs: ",".join(f"{a}:{b}" for a, b in xs) if xs else "-"  # noqa: E731

    def mvd():
        return rand_mvd(rng, sc)
    if raw < 3:
        name, n = P_PARTS[raw]
        refs = [rng.choice(valid0) for _ in range(n)]
        for r in refs:
            write_te(w, r, n0)
        mv = [mvd() for _ in range(n)]
        subs = None
    else:
        name = "P_8x8"
        subs = [rng.randint(0, 3) for _ in range(4)]
        for s in subs:
            w.ue(s)
        refs = [rng.choice(valid0) for _ in range(4)]
        for r in refs:
            write_te(w, r, n0)
        mv = [mvd() for s in subs for _ in range((1, 2, 2, 4)[s])]
    for d in mv:
        w.se(d[0])
        w.se(d[1])
    w.ue(0)
    return dict(kind="p", name=name, mb_type=raw, sub=j(subs) if subs else "-", refidx=j(refs), mvd=jm(mv), refidx1="-", mvd1="-")


def write_mmco(w, mmcos):
    w.raw(1, 1)                           # adaptive_ref_pic_marking_mode_flag
    for op, a in mmcos:
        w.ue(op)
        for v in a.values():
            w.ue(v)
    w.ue(0)


def write_slice(rng, sc, ctx):
    """ctx: dict(pic, slice_type 0/1/2, first, last, idr, ref_idc, fn, poc, pps, a0, a1, override, mods0, mods1, L0, L1, wt, deblock,
    alpha, beta, lt_flag, spatial, direct_ok, mmcos).  -> (nal bytes, manifest slice)"""
    w = W()
    st = ctx["slice_type"]
    w.ue(ctx["first"])
    w.ue(st)
    w.ue(ctx["pps"])
    w.raw(LOG2_FN, ctx["fn"])
    if ctx["idr"]:
        w.ue(ctx["idr_id"])
    w.raw(LOG2_POC, ctx["poc"] % (1 << LOG2_POC))
    if st == 1:
        w.raw(1, ctx["spatial"])          # direct_spatial_mv_pred_flag
    if st in (0, 1):
        w.raw(1, 1 if ctx["override"] else 0)
        if ctx["override"]:
            w.ue(ctx["a0"] - 1)
            if st == 1:
                w.ue(ctx["a1"] - 1)
        for mods in ([ctx["mods0"], ctx["mods1"]] if st == 1 else [ctx["mods0"]]):
            if mods:
                w.raw(1, 1)
                for idc, v in mods:
                    w.ue(idc)
                    w.ue(v)
                w.ue(3)
            else:
                w.raw(1, 0)
        if ctx["wt"] is not None:
            dy, dc, t0, t1 = ctx["wt"]
            w.ue(dy)
            w.ue(dc)
            write_table(w, t0)
            if st == 1:
                write_table(w, t1)
    if ctx["ref_idc"]:
        if ctx["idr"]:
            w.raw(1, 0)
            w.raw(1, ctx["lt_flag"])
        elif ctx["mmcos"]:
            write_mmco(w, ctx["mmcos"])
        else:
            w.raw(1, 0)
    w.se(0)
    w.ue(ctx["deblock"])
    if ctx["deblock"] != 1:
        w.se(ctx["alpha"])
        w.se(ctx["beta"])
    L0, L1 = ctx["L0"], ctx["L1"]
    valid = [[i for i, e in enumerate(L0) if e is not None], [i for i, e in enumerate(L1) if e is not None]]
    n_active = [ctx["a0"], ctx["a1"]]
    direct_ok = bool(ctx["direct_ok"])
    mbs, pend = [], 0
    tc = {}                               # addr -> total_coeff of the blocks next to the neighbours (16 = I_PCM, else 0)
    first = ctx["first"]
    for addr in range(ctx["first"], ctx["last"]):
        r = rng.random()
        kind = "inter"
        p0 = sc["p_pcm"] + sc["p_intra16"]
        if st == 2 or r < sc["p_pcm"]:
            kind = "pcm"
        elif r < p0:
            kind = "i16"
        elif st == 0 and valid[0] and r < p0 + sc["p_skip"]:
            kind = "skip"
        elif st == 1 and direct_ok and r < p0 + sc["p_bskip"]:
            kind = "bskip"
        if kind == "inter" and not (valid[0] or valid[1]):
            kind = "pcm"
        if kind == "inter" and st == 0 and not valid[0]:
            kind = "pcm"
        if kind in ("skip", "bskip"):
            pend += 1
            mbs.append(dict(kind="skip", name="P_Skip" if kind == "skip" else "B_Skip", mb_type="skip", sub="-", refidx="-", mvd="-",
                            refidx1="-", mvd1="-"))
            tc[addr] = 0
            continue
        if st in (0, 1):
            w.ue(pend)
        pend = 0
        if kind == "pcm":
            w.ue({2: 25, 0: 30, 1: 48}[st])
            while len(w.bits) % 8:
                w.raw(1, 0)
            for s in pcm_samples(rng):
                w.raw(8, s)
            tc[addr] = 16
            mbs.append(dict(kind="pcm", name="I_PCM", mb_type={2: 25, 0: 30, 1: 48}[st], sub="-", refidx="-", mvd="-", refidx1="-",
                            mvd1="-"))
        elif kind == "i16":
            w.ue({2: 3, 0: 8, 1: 26}[st])
            w.ue(0)                       # intra_chroma_pred_mode DC
            w.se(0)                       # mb_qp_delta
            nb = []
            if addr % W_MBS and addr - 1 >= first:
                nb.append(tc.get(addr - 1, 0))
            if addr >= W_MBS and addr - W_MBS >= first:
                nb.append(tc.get(addr - W_MBS, 0))
            nc_token(w, 0 if not nb else (nb[0] if len(nb) == 1 else (nb[0] + nb[1] + 1) >> 1))
            tc[addr] = 0
            mbs.append(dict(kind="i16", name="I_16x16_2_0_0", mb_type={2: 3, 0: 8, 1: 26}[st], sub="-", refidx="-", mvd="-",
                            refidx1="-", mvd1="-"))
        else:
            e = write_b_inter(w, rng, sc, valid, n_active, direct_ok) if st == 1 else write_p_inter(w, rng, sc, valid[0], n_active[0])
            tc[addr] = 0
            mbs.append(e)
    if pend:
        w.ue(pend)
    hdr = (ctx["ref_idc"] << 5) | (5 if ctx["idr"] else 1)
    return nal(hdr, w.rbsp()), dict(first=ctx["first"], type=st, mbs=mbs, spatial=ctx["spatial"] if st == 1 else None,
                                    direct_ok=direct_ok, poc=ctx["poc"], mmco=bool(ctx["mmcos"]))


def plan_mmco(d, rng, fn, poc, ref_idc, sc):
    """Random valid memory_management_control_operations for a non-IDR reference picture, or None (sliding window).
    Valid = the model accepts them and the DPB does not overflow."""
    if rng.random() >= sc["mmco"]:
        return None
    mode = rng.choice(["lt_cur", "lt_cur", "forget", "short_to_lt"])
    shorts = d.short()
    ops = []
    if mode in ("lt_cur", "short_to_lt") and (d.max_lt is None or d.max_lt < 0):
        ops.append((4, dict(max_long_term_frame_idx_plus1=1)))
    if mode == "lt_cur":
        ops.append((6, dict(long_term_frame_idx=0)))
    elif mode == "short_to_lt" and shorts:
        f = rng.choice(shorts)
        ops.append((3, dict(difference_of_pic_nums_minus1=fn - d.wrap(f, fn) - 1, long_term_frame_idx=0)))
    elif mode == "forget" and shorts:
        f = rng.choice(shorts)
        ops.append((1, dict(difference_of_pic_nums_minus1=fn - d.wrap(f, fn) - 1)))
    for attempt in range(6):
        sim = d.clone()
        try:
            sim.finish_picture("x", fn, False, ref_idc, 0, ops, poc)
            return ops or None
        except R.StreamError:
            pass
        sh = [f for f in d.short() if all(not (o == 1 and a["difference_of_pic_nums_minus1"] == fn - d.wrap(f, fn) - 1)
                                          for o, a in ops)]
        if not sh:
            return None
        f = min(sh, key=lambda f: d.wrap(f, fn))
        ops.insert(0, (1, dict(difference_of_pic_nums_minus1=fn - d.wrap(f, fn) - 1)))
    return None


def poc_table(sc, seed):
    prng = random.Random(seed * 977 + 3)
    out, v = [0], 0
    for _ in range(400):
        v += prng.choice(sc["poc_steps"])
        out.append(v)
    return out


def make_stream(name, seed=7, avoid=None):
    """avoid: {(picture, slice in picture): 1 | 2}: 1 = that B slice is forced to spatial direct, 2 = it has no direct macroblock."""
    global LOG2_POC
    avoid = avoid or {}
    sc = SCENARIOS[name]
    LOG2_POC = sc["poc_log2"]
    rng = random.Random(seed * 131 + sum(map(ord, name)))
    plan = make_plan(rng, sc)
    pt = poc_table(sc, seed)
    weighted_pred = 1 if sc["weights"] else 0
    data = sps_nal(sc["refs"], 5, sc["inference"]) + b"".join(pps_nal(i, weighted_pred, sc["bipred_idc"]) for i in range(3))
    d = R.Dpb(sc["refs"], LOG2_FN, False)
    manifest = dict(width_mbs=W_MBS, height_mbs=H_MBS, scenario=name, inference=sc["inference"], pictures=[])
    idr_id = 0
    lt_first = sc["idr_lt"]
    for n, p in enumerate(plan):
        idr = p["idr"]
        fn = 0 if idr else (d.prev_ref_fn + 1) % (1 << LOG2_FN)
        poc = pt[p["disp"]]
        lt_flag = 1 if (idr and lt_first and rng.random() < sc['idr_lt_prob']) else 0
        nsl = 1 if idr else rng.randint(1, sc["slices"])
        cuts = sorted(rng.sample(range(1, N_MBS), nsl - 1)) if nsl > 1 else []
        bounds = list(zip([0] + cuts, cuts + [N_MBS]))
        pps = rng.randrange(3)
        mmcos = plan_mmco(d, rng, fn, poc, p["ref_idc"], sc) if (p["ref_idc"] and not idr) else None
        pic = dict(idr=idr, ref_idc=p["ref_idc"], frame_num=fn, poc=poc, kind=p["kind"], slices=[])
        for si, (a, b) in enumerate(bounds):
            srng = random.Random(f"{seed}/{name}/{n}/{si}")          # every slice has a random stream of its own
            st = {"I": 2, "P": 0, "B": 1}[p["kind"]]
            if sc["mixed"] and not idr and st != 2 and srng.random() < 0.4:
                st = srng.choice([0, 1])
            lvl = avoid.get((n, si), 0)
            spatial = 1 if srng.random() < sc["spatial"] else 0
            if lvl >= 1:
                spatial = 1
            ctx = dict(first=a, last=b, idr=idr, ref_idc=p["ref_idc"], fn=fn, poc=poc, pps=pps, idr_id=idr_id, slice_type=st,
                       a0=1, a1=1, override=False, mods0=[], mods1=[], L0=[], L1=[], wt=None, lt_flag=lt_flag,
                       deblock=sc["deblock"], alpha=srng.randint(-3, 3), beta=srng.randint(-3, 3), spatial=spatial,
                       direct_ok=False, mmcos=mmcos)
            if st in (0, 1) and not idr:
                nfr = len(d.frames)
                if srng.random() < 0.5 and PPS_ACTIVE0[pps] <= max(nfr, 1) + 2:
                    ctx["a0"], ctx["a1"] = PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]
                else:
                    ctx["a0"], ctx["a1"] = srng.randint(1, sc["max_a0"]), srng.randint(1, sc["max_a1"])
                    ctx["override"] = True
                if not ctx["override"] and (ctx["a0"], ctx["a1"]) != (PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]):
                    ctx["override"] = True
                for attempt in range(3):
                    if srng.random() < sc["p_mod"] or attempt:
                        ctx["mods0"] = choose_mods(d, srng, fn, ctx["a0"], sc["p_dup"], sc["lt_mods"])
                        ctx["mods1"] = choose_mods(d, srng, fn, ctx["a1"], sc["p_dup"], sc["lt_mods"]) \
                            if st == 1 and srng.random() < 0.8 else []
                    if st == 1:
                        ctx["L0"], ctx["L1"] = d.build_lists_b(fn, poc, ctx["a0"], ctx["a1"], ctx["mods0"], ctx["mods1"])
                    else:
                        ctx["L0"], ctx["L1"] = d.build_list(fn, ctx["a0"], ctx["mods0"]), []
                    if any(e is not None for e in ctx["L0"]) and (st == 0 or any(e is not None for e in ctx["L1"])):
                        break
                else:
                    ctx["mods0"] = ctx["mods1"] = []
                if sc["weights"] and (st == 0 or sc["bipred_idc"] == 1):
                    ctx["wt"] = make_weights(srng, ctx["a0"], ctx["a1"] if st == 1 else 0)
                ctx["direct_ok"] = (st == 1 and lvl < 2 and ctx["L0"][0] is not None and ctx["L1"][0] is not None)
            elif st in (0, 1):
                st = ctx["slice_type"] = 2
            nal_bytes, msl = write_slice(srng, sc, ctx)
            data += nal_bytes
            msl["active0"], msl["active1"] = ctx["a0"], ctx["a1"]
            pic["slices"].append(msl)
        d.finish_picture(n, fn, idr, p["ref_idc"], lt_flag, mmcos, poc)
        if idr:
            idr_id = (idr_id + 1) % 8
        manifest["pictures"].append(pic)
    return data, manifest


def write_stream(name, path, seed=7, validate=True):
    """Write the stream (and path + '.json').  With validate, the reference decoder checks that every temporal-direct slice is
    legal (see the module docstring) and the stream is regenerated with the offending slices made spatial / direct-free."""
    import ddec
    sc = SCENARIOS[name]
    ddec.direct_ref.STRICT = bool(sc["ffmpeg_ok"])
    avoid = {}
    for it in range(40):
        data, manifest = make_stream(name, seed, avoid)
        with open(path, "wb") as f:
            f.write(data)
        if not validate:
            break
        try:
            ddec.decode(path, pixels=False)
            break
        except ddec.direct_ref.DirectInvalid as e:
            flat = [(n, si) for n, pc in enumerate(manifest["pictures"]) for si in range(len(pc["slices"]))]
            key = flat[e.slice]
            avoid[key] = avoid.get(key, 0) + 1
    else:
        raise RuntimeError(f"{name}: no legal stream found")
    ddec.direct_ref.STRICT = False
    manifest["avoided"] = len(avoid)
    manifest["ffmpeg_ok"] = bool(sc["ffmpeg_ok"])
    with open(path + ".json", "w") as f:
        json.dump(manifest, f)
    return manifest


if __name__ == "__main__":
    write_stream(sys.argv[1], sys.argv[2])
