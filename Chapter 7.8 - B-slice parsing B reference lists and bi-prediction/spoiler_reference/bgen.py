"""SPOILER-adjacent: synthetic stream generator for the Chapter 7.8 harness (bit writer: common/bitwriter.py).

Tiny CAVLC Main-profile streams (96x80 = 6x5 macroblocks), picture order count type 0 (POC = 2 * display index inside an IDR period;
pic_order_cnt_lsb has 5 bits, so it wraps every 16 pictures), with I, P and B pictures in a random but valid structure:
an anchor picture (P, occasionally I or a B picture whose references are all in the past) every 1..maxb+1 display positions and the
B pictures between two anchors in random decoding order, as reference pictures (nal_ref_idc > 0) or not.  The SPS carries VUI
bitstream_restriction (max_num_reorder_frames) so that ffmpeg outputs in display order.

Every B macroblock is one of the 22 non-direct B mb_types (Table 7-14: B_L0/L1/Bi_16x16, the 18 16x8 / 8x16 combinations, B_8x8
with sub_mb_type 1..12 of Table 7-18), with random valid ref_idx_l0 / ref_idx_l1 and random mvd; there is no B_Skip / direct macroblock
(they are Chapter 7.9) and no residual (coded_block_pattern 0).  Other macroblocks: I_PCM (random samples), Intra16x16 DC
prediction without coefficients, P_Skip and P macroblocks in P slices.  Slice headers carry random valid
ref_pic_list_modification commands for BOTH lists (repeated targets included), random num_ref_idx_active overrides, and, for
the weighted scenarios, a random pred_weight_table (weighted_pred_flag = 1; weighted_bipred_idc = 1 gives tables for both lists).

The generator drives brefmgmt_ref.Dpb only to choose valid commands and ref_idx values; the truth is ffmpeg's decode.  Manifest
(JSON next to the stream): pictures in decoding order with their slices and, for every macroblock, the Chapter 7.8 dump values
(name, mb_type, sub, refidx, mvd, refidx1, mvd1) as the parser must print them.
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
LOG2_FN, LOG2_POC = 4, 5
PPS_ACTIVE0 = [1, 2, 3]
PPS_ACTIVE1 = [1, 1, 2]

BASE = dict(pictures=30, refs=3, maxb=2, p_refb=0.3, p_tail=0.0, p_idr=0.0, idr_lt=False, p_mod=0.0, p_dup=0.0, max_a0=3, max_a1=2,
            slices=1, mixed=False, weights=None, deblock=1, p_intra16=0.05, p_pcm=0.08, p_skip=0.2, mvd=6, p_i_anchor=0.05,
            p_big_mvd=0.0, bipred_idc=0, idr_lt_prob=0.5)
SCENARIOS = {
    "basic": dict(BASE),
    "types": dict(BASE, pictures=40, refs=4, maxb=3, max_a0=3, max_a1=3, p_mod=0.3, p_pcm=0.05),
    "lists": dict(BASE, pictures=48, refs=6, maxb=3, p_refb=0.5, p_mod=0.8, p_dup=0.5, max_a0=6, max_a1=5, slices=2),
    "tail": dict(BASE, pictures=36, refs=4, maxb=1, p_tail=0.4, max_a0=3, max_a1=3, p_mod=0.15),
    "dbk": dict(BASE, pictures=30, refs=3, maxb=2, deblock=0, mvd=2, p_pcm=0.04, p_intra16=0.06, p_skip=0.1),
    "wbi": dict(BASE, pictures=30, refs=3, maxb=2, weights="explicit", bipred_idc=1, max_a0=3, max_a1=3, p_mod=0.4, slices=2),
    "idr2": dict(BASE, pictures=44, refs=4, maxb=3, p_idr=0.09, idr_lt=True, p_mod=0.2, max_a0=3, max_a1=3),
    "slices": dict(BASE, pictures=36, refs=4, maxb=2, slices=3, mixed=True, p_mod=0.4, max_a0=4, max_a1=3, deblock=0),
    "intra": dict(BASE, pictures=30, refs=3, maxb=2, p_intra16=0.3, p_pcm=0.2, deblock=0, p_skip=0.05),
    "mvfar": dict(BASE, pictures=26, refs=3, maxb=2, mvd=40, p_big_mvd=0.25, p_pcm=0.1),
    "ltref": dict(BASE, pictures=30, refs=3, maxb=2, idr_lt=True, idr_lt_prob=1.0, p_mod=0.3, max_a0=3, max_a1=3, p_refb=0.4),
    "wp_only": dict(BASE, pictures=30, refs=3, maxb=2, weights="explicit", bipred_idc=0, p_mod=0.3),
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


def sps_nal(refs, reorder):
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
    w.raw(1, 1)                           # direct_8x8_inference
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


def choose_mods(d, rng, cur_fn, active, p_dup):
    cands = [f for f in d.short()]
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


def write_b_inter(w, rng, sc, valid, n_active):
    """One non-direct B macroblock after mb_skip_run: returns the manifest entry.  valid[l] = usable ref_idx values of list l."""
    can = [bool(valid[0]), bool(valid[1])]
    okmodes = ["L0"] * can[0] + ["L1"] * can[1] + ["Bi"] * (can[0] and can[1])
    if not okmodes:
        return None
    for _ in range(30):
        raw = rng.randint(1, 22)
        if raw == 22:
            break
        if all(m in okmodes for m in b_type(raw)[1]):
            break
    else:
        raw = {"L0": 1, "L1": 2, "Bi": 3}[okmodes[0]]
    if raw == 22 and not okmodes:
        return None
    if raw == 22 and False:
        pass
    w.ue(raw)
    if raw == 22:
        subs, units = [], []
        for _ in range(4):
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
                    if rng.random() < sc["p_big_mvd"]:
                        a = sc["mvd"]
                        d = (rng.randint(-a, a), rng.randint(-a, a))
                    else:
                        a = min(sc["mvd"], 6)
                        d = (rng.randint(-a, a), rng.randint(-a, a))
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
    a = min(sc["mvd"], 6)

    def mvd():
        if rng.random() < sc["p_big_mvd"]:
            return (rng.randint(-sc["mvd"], sc["mvd"]), rng.randint(-sc["mvd"], sc["mvd"]))
        return (rng.randint(-a, a), rng.randint(-a, a))
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


def write_slice(rng, sc, ctx):
    """ctx: dict(pic, slice_type 0/1/2, first, last, idr, ref_idc, fn, poc, pps, a0, a1, override, mods0, mods1, L0, L1, wt, deblock,
    alpha, beta, lt_flag).  -> (nal bytes, manifest slice)"""
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
        w.raw(1, 1)                       # direct_spatial_mv_pred_flag (unused: there is no direct macroblock)
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
    mbs, pend = [], 0
    tc = {}                               # addr -> total_coeff of the blocks next to the neighbours (16 = I_PCM, else 0)
    first = ctx["first"]
    for addr in range(ctx["first"], ctx["last"]):
        r = rng.random()
        kind = "inter"
        if st == 2 or r < sc["p_pcm"]:
            kind = "pcm"
        elif r < sc["p_pcm"] + sc["p_intra16"]:
            kind = "i16"
        elif st == 0 and valid[0] and r < sc["p_pcm"] + sc["p_intra16"] + sc["p_skip"]:
            kind = "skip"
        if kind == "inter" and not (valid[0] or valid[1]):
            kind = "pcm"
        if kind == "inter" and st == 0 and not valid[0]:
            kind = "pcm"
        if kind == "skip":
            pend += 1
            mbs.append(dict(kind="skip", name="P_Skip"))
            tc[addr] = 0
            continue
        if st == 0:
            w.ue(pend)
        elif st == 1:
            w.ue(0)                       # mb_skip_run 0: no B_Skip (it would be a direct macroblock)
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
            e = write_b_inter(w, rng, sc, valid, n_active) if st == 1 else write_p_inter(w, rng, sc, valid[0], n_active[0])
            tc[addr] = 0
            mbs.append(e)
    if pend:
        w.ue(pend)
    hdr = (ctx["ref_idc"] << 5) | (5 if ctx["idr"] else 1)
    return nal(hdr, w.rbsp()), dict(first=ctx["first"], type=st, mbs=mbs)


def make_stream(name, seed=7):
    sc = SCENARIOS[name]
    rng = random.Random(seed * 131 + sum(map(ord, name)))
    plan = make_plan(rng, sc)
    weighted_pred = 1 if sc["weights"] else 0
    data = sps_nal(sc["refs"], 5) + b"".join(pps_nal(i, weighted_pred, sc["bipred_idc"]) for i in range(3))
    d = R.Dpb(sc["refs"], LOG2_FN, False)
    manifest = dict(width_mbs=W_MBS, height_mbs=H_MBS, scenario=name, pictures=[])
    idr_id = 0
    period_start = 0
    lt_first = sc["idr_lt"]
    for n, p in enumerate(plan):
        idr = p["idr"]
        fn = 0 if idr else (d.prev_ref_fn + 1) % (1 << LOG2_FN)
        poc = 2 * p["disp"]
        lt_flag = 1 if (idr and lt_first and rng.random() < sc['idr_lt_prob']) else 0
        nsl = 1 if idr else rng.randint(1, sc["slices"])
        cuts = sorted(rng.sample(range(1, N_MBS), nsl - 1)) if nsl > 1 else []
        bounds = list(zip([0] + cuts, cuts + [N_MBS]))
        pps = rng.randrange(3)
        pic = dict(idr=idr, ref_idc=p["ref_idc"], frame_num=fn, poc=poc, kind=p["kind"], slices=[])
        for a, b in bounds:
            st = {"I": 2, "P": 0, "B": 1}[p["kind"]]
            if sc["mixed"] and not idr and st != 2 and rng.random() < 0.4:
                st = rng.choice([0, 1])
            ctx = dict(first=a, last=b, idr=idr, ref_idc=p["ref_idc"], fn=fn, poc=poc, pps=pps, idr_id=idr_id, slice_type=st,
                       a0=1, a1=1, override=False, mods0=[], mods1=[], L0=[], L1=[], wt=None, lt_flag=lt_flag,
                       deblock=sc["deblock"], alpha=rng.randint(-3, 3), beta=rng.randint(-3, 3))
            if st in (0, 1) and not idr:
                nfr = len(d.frames)
                if rng.random() < 0.5 and PPS_ACTIVE0[pps] <= max(nfr, 1) + 2:
                    ctx["a0"], ctx["a1"] = PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]
                else:
                    ctx["a0"], ctx["a1"] = rng.randint(1, sc["max_a0"]), rng.randint(1, sc["max_a1"])
                    ctx["override"] = True
                if st == 0 and not ctx["override"]:
                    pass
                if not ctx["override"] and (ctx["a0"], ctx["a1"]) != (PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]):
                    ctx["override"] = True
                for attempt in range(3):
                    if rng.random() < sc["p_mod"] or attempt:
                        ctx["mods0"] = choose_mods(d, rng, fn, ctx["a0"], sc["p_dup"])
                        ctx["mods1"] = choose_mods(d, rng, fn, ctx["a1"], sc["p_dup"]) if st == 1 and rng.random() < 0.8 else []
                    if st == 1:
                        ctx["L0"], ctx["L1"] = d.build_lists_b(fn, poc, ctx["a0"], ctx["a1"], ctx["mods0"], ctx["mods1"])
                    else:
                        ctx["L0"], ctx["L1"] = d.build_list(fn, ctx["a0"], ctx["mods0"]), []
                    if any(e is not None for e in ctx["L0"]) and (st == 0 or any(e is not None for e in ctx["L1"])):
                        break
                else:
                    ctx["mods0"] = ctx["mods1"] = []
                if sc["weights"] and (st == 0 or sc["bipred_idc"] == 1):
                    ctx["wt"] = make_weights(rng, ctx["a0"], ctx["a1"] if st == 1 else 0)
            elif st in (0, 1):
                st = ctx["slice_type"] = 2
            nal_bytes, msl = write_slice(rng, sc, ctx)
            data += nal_bytes
            msl["active0"], msl["active1"] = ctx["a0"], ctx["a1"]
            pic["slices"].append(msl)
        d.finish_picture(n, fn, idr, p["ref_idc"], lt_flag, None, poc)
        if idr:
            idr_id = (idr_id + 1) % 8
        manifest["pictures"].append(pic)
    return data, manifest


def write_stream(name, path, seed=7):
    data, manifest = make_stream(name, seed)
    with open(path, "wb") as f:
        f.write(data)
    with open(path + ".json", "w") as f:
        json.dump(manifest, f)
    return manifest


if __name__ == "__main__":
    write_stream(sys.argv[1], sys.argv[2])
