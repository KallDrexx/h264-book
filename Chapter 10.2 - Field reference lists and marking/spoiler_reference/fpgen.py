"""SPOILER-adjacent: synthetic stream generator for the Chapter 10.2 harness (bit writer: common/bitwriter.py).

Tiny CAVLC Main-profile streams (80x96, 5x6 macroblocks; a field is 5x3 macroblocks), frame_mbs_only_flag 0, mb_adaptive_frame_field_flag 0,
direct_8x8_inference_flag 1, pic_order_cnt_type 0.  I, P and B pictures that are FRAMES or FIELDS (complementary pairs, top or bottom
first) in a random but valid structure (an anchor every 1..maxb+1 display positions, the B pictures between two anchors in random
decoding order, as reference pictures or not).  No x264 / JM exists for PAFF material: ffmpeg decoding these streams is the pixel
truth, fdpb.py + fpdec.py (driven by the manifest written here) the truth for lists and marking.

What the streams exercise: field list initialisation (P and B), list modification commands with the field picNum rules, ref_idx up to
31, sliding window and MMCO 1-6 on fields (including long-term fields and the second field of a frame), implicit weights and
temporal / spatial direct with field POCs and co-located fields (also co-located pictures coded the other way: d_mixed), deblocking of field pictures, mixed frame / field coding.
Every macroblock is residual-free (coded_block_pattern 0); types: P and B inter types, skips, direct, I_PCM, Intra16x16 (DC).

Validity: a temporal-direct slice is only legal when every picture its co-located blocks
refer to is in RefPicList0.  write_stream() asks the reference decoder (pixels=False) and, when it reports a slice that is not legal,
regenerates the stream with that slice made spatial (or direct-free); every slice has its own random stream.
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common.bitwriter import W, nal  # noqa: E402
import fdpb  # noqa: E402
import fpdec  # noqa: E402

PPS_ACTIVE0 = [1, 2, 4]
PPS_ACTIVE1 = [1, 1, 3]

BASE = dict(units=14, refs=3, maxb=2, p_pair=0.8, p_bff=0.4, p_refb=0.4, p_i_anchor=0.05, p_idr=0.0, p_mod=0.4, p_dup=0.15,
            p_lt_mod=0.0, max_a0=3, max_a1=2, slices=1, p_mmco=0.0, p_idr_lt=0.0, spatial=0.5, bipred_idc=0, deblock=0,
            p_skip=0.2, p_bskip=0.25, p_direct16=0.1, p_sub_direct=0.2, p_intra=0.1, p_pcm=0.5, mvd=6, mvd0=0.5, log2_fn=4,
            log2_poc=7, poc_steps=[2, 2, 4, 6], p_second_i=0.1, reorder=5, mbw=5, map_h=3, p_big_mvd=0.0, ffmpeg_ok=True)

SCENARIOS = {
    # --- P field lists: alternating parity, FrameNumWrap (frame_num wraps at 16), the first field of the current frame
    "p_fields": dict(BASE, units=24, refs=4, maxb=0, p_pair=1.0, p_mod=0.0, max_a0=6, seed_base=1, slices=2),
    "p_mixed": dict(BASE, units=24, refs=4, maxb=0, p_pair=0.6, p_mod=0.0, max_a0=5, seed_base=2),
    "p_mod": dict(BASE, units=24, refs=4, maxb=0, p_pair=0.75, p_mod=0.9, p_dup=0.3, max_a0=6, slices=2, seed_base=3),
    "p_wrap": dict(BASE, units=40, refs=5, maxb=0, p_pair=0.8, p_mod=0.5, max_a0=8, log2_fn=4, seed_base=4, deblock=1),
    # --- sliding window with second fields, unpaired reference fields' absence; many references
    "slide": dict(BASE, units=30, refs=2, maxb=0, p_pair=0.9, p_mod=0.3, max_a0=5, seed_base=5),
    "slide1": dict(BASE, units=24, refs=1, maxb=0, p_pair=0.9, p_mod=0.2, max_a0=3, seed_base=6),
    # --- B field lists: POC order, list 1 swap, the first field of the current (reference) B frame
    "b_fields": dict(BASE, units=26, refs=4, maxb=3, p_pair=1.0, p_refb=0.5, p_mod=0.0, max_a0=6, max_a1=6, seed_base=7, spatial=0.5),
    "b_mixed": dict(BASE, units=26, refs=4, maxb=2, p_pair=0.6, p_refb=0.5, p_mod=0.2, max_a0=5, max_a1=4, seed_base=8),
    "b_tail": dict(BASE, units=24, refs=4, maxb=0, p_pair=0.85, p_tail=0.5, p_mod=0.0, max_a0=6, max_a1=6, seed_base=9),
    "b_mod": dict(BASE, units=26, refs=5, maxb=3, p_pair=0.85, p_refb=0.5, p_mod=0.9, p_dup=0.3, max_a0=7, max_a1=6, slices=2,
                  seed_base=10),
    # --- direct and implicit weights with field POCs
    "d_spatial": dict(BASE, units=26, refs=4, maxb=3, p_pair=1.0, p_refb=0.5, spatial=1.0, max_a0=4, max_a1=3, seed_base=11,
                      mvd0=0.6, deblock=1),
    "d_temporal": dict(BASE, units=26, refs=4, maxb=3, p_pair=1.0, p_refb=0.5, spatial=0.0, max_a0=5, max_a1=3, p_mod=0.3,
                       seed_base=12, mvd0=0.5, poc_steps=[2, 4, 6, 8]),
    "d_mixed": dict(BASE, units=30, refs=4, maxb=3, p_pair=0.5, p_refb=0.5, spatial=0.0, max_a0=5, max_a1=3, p_mod=0.2,
                    seed_base=30, mvd0=0.3, poc_steps=[2, 4, 6, 8], mixed_col=True, p_direct16=0.3, p_sub_direct=0.5),
    "w_implicit": dict(BASE, units=26, refs=4, maxb=3, p_pair=1.0, p_refb=0.5, bipred_idc=2, spatial=0.5, max_a0=5, max_a1=4,
                       p_mod=0.3, seed_base=13, poc_steps=[2, 4, 6, 10, 14], log2_poc=9),
    "w_temporal": dict(BASE, units=26, refs=4, maxb=3, p_pair=1.0, p_refb=0.5, bipred_idc=2, spatial=0.0, max_a0=5, max_a1=4,
                       p_mod=0.3, seed_base=14, poc_steps=[2, 4, 6, 10, 14], log2_poc=9, slices=2),
    # --- long-term fields and MMCO
    "lt_fields": dict(BASE, units=32, refs=4, maxb=2, p_pair=0.9, p_refb=0.4, p_mmco=0.7, p_lt_mod=0.5, p_mod=0.5, max_a0=6, max_a1=4,
                      seed_base=15),
    "lt_pairs": dict(BASE, units=32, refs=3, maxb=0, p_pair=1.0, p_mmco=0.8, p_lt_mod=0.5, p_mod=0.5, max_a0=6, seed_base=16,
                     p_idr_lt=0.5, p_idr=0.08),
    "lt_b": dict(BASE, units=30, refs=4, maxb=2, p_pair=0.9, p_refb=0.5, p_mmco=0.6, p_lt_mod=0.6, p_mod=0.6, max_a0=6, max_a1=5,
                 seed_base=17, spatial=0.5, bipred_idc=2),
    "lt_direct": dict(BASE, units=30, refs=4, maxb=2, p_pair=1.0, p_refb=0.5, p_mmco=0.6, p_lt_mod=0.6, p_mod=0.6, max_a0=6, max_a1=5,
                      seed_base=18, spatial=0.5, bipred_idc=2),
    "mmco_mixed": dict(BASE, units=34, refs=5, maxb=2, p_pair=0.6, p_refb=0.4, p_mmco=0.8, p_lt_mod=0.4, p_mod=0.4, max_a0=7, max_a1=5,
                       seed_base=19, p_idr=0.05, p_idr_lt=0.3),
    # --- deblocking of field P and B pictures (bS 3, vertical vector limit 2), several slices, motion with small vertical differences
    "mmco5_frames": dict(BASE, units=26, refs=3, maxb=0, p_pair=0.6, p_mmco=0.5, p_mmco5=0.25, p_mod=0.4, max_a0=5, seed_base=22),
    # --- the standard's rules where ffmpeg 8.0 decodes differently: the truth of these streams is the reference decoder (ffmpeg_ok = False)
    "mmco6_fields": dict(BASE, units=30, refs=4, maxb=2, p_pair=1.0, p_refb=0.4, p_mmco=0.8, p_lt_mod=0.5, p_mod=0.5, max_a0=6, max_a1=4,
                         seed_base=23, mmco6_fields=True, p_idr_lt=0.5, p_idr=0.06, ffmpeg_ok=False, bipred_idc=2),
    # --- field POCs of one frame far apart or equal, frames interleaving in POC: entry POC = minimum, <= for list 0, cur POC = the field's
    "b_spread": dict(BASE, units=30, refs=4, maxb=3, p_pair=0.85, p_refb=0.5, p_spread=0.6, p_equal_poc=0.2, poc_steps=[4, 6, 8], log2_poc=9,
                     bipred_idc=2, spatial=0.5, max_a0=6, max_a1=5, p_mod=0.2, seed_base=25),
    "b_equal": dict(BASE, units=30, refs=4, maxb=2, p_pair=1.0, p_refb=0.8, p_equal_poc=0.5, p_spread=0.2, poc_steps=[4, 6, 8], log2_poc=9,
                    bipred_idc=2, spatial=0.5, max_a0=6, max_a1=5, p_mod=0.2, seed_base=27),
    "d_spread": dict(BASE, units=30, refs=4, maxb=3, p_pair=1.0, p_refb=0.5, p_spread=0.6, p_equal_poc=0.2, poc_steps=[4, 6, 8], log2_poc=9,
                     bipred_idc=2, spatial=0.0, max_a0=5, max_a1=4, seed_base=26, mvd0=0.4),
    "dbk_fields": dict(BASE, units=24, refs=3, maxb=2, p_pair=1.0, p_refb=0.4, slices=2, deblock=2, mvd=3, mvd0=0.2, max_a0=4,
                       max_a1=3, seed_base=20, p_intra=0.2, spatial=0.5),
    "dbk_mixed": dict(BASE, units=24, refs=3, maxb=2, p_pair=0.5, p_refb=0.4, slices=2, deblock=2, mvd=3, mvd0=0.2, max_a0=4,
                      max_a1=3, seed_base=21, p_intra=0.2, spatial=0.5),
}


# ---------------------------------------------------------------- tables
PAIRS = [("L0", "L0"), ("L1", "L1"), ("L0", "L1"), ("L1", "L0"), ("L0", "Bi"), ("L1", "Bi"), ("Bi", "L0"), ("Bi", "L1"), ("Bi", "Bi")]
SUBS = {0: ("Direct", 0), 1: ("L0", 1), 2: ("L1", 1), 3: ("Bi", 1), 4: ("L0", 2), 5: ("L0", 2), 6: ("L1", 2), 7: ("L1", 2), 8: ("Bi", 2),
        9: ("Bi", 2), 10: ("L0", 4), 11: ("L1", 4), 12: ("Bi", 4)}                 # sub_mb_type -> (mode, number of sub-partitions)
SUB_SHAPE = {1: (8, 8), 2: (8, 8), 3: (8, 8), 4: (8, 4), 5: (4, 8), 6: (8, 4), 7: (4, 8), 8: (8, 4), 9: (4, 8), 10: (4, 4), 11: (4, 4),
             12: (4, 4)}
P_SUB_SHAPE = {0: (8, 8), 1: (8, 4), 2: (4, 8), 3: (4, 4)}


def b_type(raw):
    if raw in (1, 2, 3):
        m = ("L0", "L1", "Bi")[raw - 1]
        return f"B_{m}_16x16", [m], "16x16"
    k, odd = divmod(raw - 4, 2)
    a, b = PAIRS[k]
    return f"B_{a}_{b}_{'8x16' if odd else '16x8'}", [a, b], "8x16" if odd else "16x8"


def uses(mode, lst):
    return mode == "Bi" or mode == ("L0" if lst == 0 else "L1")


def write_te(w, v, n):
    if n == 1:
        assert v == 0
    elif n == 2:
        w.raw(1, 1 - v)
    else:
        w.ue(v)


def sps_nal(sc):
    w = W()
    w.raw(8, 77)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    w.ue(sc["log2_fn"] - 4)
    w.ue(0)
    w.ue(sc["log2_poc"] - 4)
    w.ue(sc["refs"])
    w.raw(1, 0)
    w.ue(sc["mbw"] - 1)
    w.ue(sc["map_h"] - 1)
    w.raw(1, 0)                           # frame_mbs_only_flag
    w.raw(1, 0)                           # mb_adaptive_frame_field_flag
    w.raw(1, 1)                           # direct_8x8_inference_flag
    w.raw(1, 0)                           # no cropping
    w.raw(1, 1)                           # VUI
    for _ in range(4):
        w.raw(1, 0)
    w.raw(1, 0)
    w.raw(1, 0)
    w.raw(1, 0)
    w.raw(1, 0)
    w.raw(1, 1)                           # bitstream_restriction_flag
    w.raw(1, 1)
    w.ue(0)
    w.ue(0)
    w.ue(16)
    w.ue(16)
    w.ue(sc["reorder"])
    w.ue(max(sc["refs"], sc["reorder"]) + 1)
    return nal(0x67, w.rbsp())


def pps_nal(sc, pps_id):
    w = W()
    w.ue(pps_id)
    w.ue(0)
    w.raw(1, 0)                           # CAVLC
    w.raw(1, 1)                           # bottom_field_pic_order_in_frame_present_flag
    w.ue(0)
    w.ue(PPS_ACTIVE0[pps_id] - 1)
    w.ue(PPS_ACTIVE1[pps_id] - 1)
    w.raw(1, 0)                           # weighted_pred_flag
    w.raw(2, sc["bipred_idc"])
    w.se(0)
    w.se(0)
    w.se(sc["cqo"])
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x68, w.rbsp())


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


def nc_token(w, nc):
    w.raw(*{0: (1, 1), 1: (2, 3), 2: (4, 15), 3: (6, 3)}[0 if nc < 2 else 1 if nc < 4 else 2 if nc < 8 else 3])


def rand_mvd(rng, sc):
    if rng.random() < sc["mvd0"]:
        return (0, 0)
    a = sc["mvd"] if rng.random() < sc["p_big_mvd"] else min(sc["mvd"], 6)
    return (rng.randint(-a, a), rng.randint(-a, a))


# ---------------------------------------------------------------- macroblock writers (each returns the manifest entry)
def write_b_inter(w, rng, sc, valid, n_active, direct_ok):
    can = [bool(valid[0]), bool(valid[1])]
    okmodes = ["L0"] * can[0] + ["L1"] * can[1] + ["Bi"] * (can[0] and can[1])
    if not okmodes and not direct_ok:
        return None
    if direct_ok and rng.random() < sc["p_direct16"]:
        w.ue(0)
        w.ue(0)
        return dict(kind="inter", name="B_Direct_16x16", parts=[])
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
    units = []                            # (mode, [part geometry (x, y, w, h)], ref slot)
    if raw == 22:
        subs = []
        for _ in range(4):
            if direct_ok and (rng.random() < sc["p_sub_direct"] or not okmodes):
                s = 0
            else:
                s = rng.choice([k for k, (m, n) in SUBS.items() if k and m in okmodes])
            subs.append(s)
            w.ue(s)
        name = "B_8x8"
        for q, s in enumerate(subs):
            x0, y0 = (q & 1) * 8, (q >> 1) * 8
            mode, n = SUBS[s]
            if s == 0:
                units.append(("Direct", [(x0, y0, 8, 8)]))
                continue
            sw, sh = SUB_SHAPE[s]
            units.append((mode, [(x0 + (j % (8 // sw)) * sw, y0 + (j // (8 // sw)) * sh, sw, sh) for j in range(n)]))
    else:
        name, modes, shape = b_type(raw)
        geo = {"16x16": [(0, 0, 16, 16)], "16x8": [(0, 0, 16, 8), (0, 8, 16, 8)], "8x16": [(0, 0, 8, 16), (8, 0, 8, 16)]}[shape]
        units = [(m, [g]) for m, g in zip(modes, geo)]
    parts = [dict(x=g[0], y=g[1], w=g[2], h=g[3], mode=m, ref=-1, ref1=-1, mvd=(0, 0), mvd1=(0, 0)) for m, gl in units for g in gl]
    base = 0
    owners = []
    for ui, (m, gl) in enumerate(units):
        owners.append(list(range(base, base + len(gl))))
        base += len(gl)
    for lst in (0, 1):
        for (mode, gl), idxs in zip(units, owners):
            if mode != "Direct" and uses(mode, lst):
                v = rng.choice(valid[lst])
                write_te(w, v, n_active[lst])
                for i in idxs:
                    parts[i]["ref" if lst == 0 else "ref1"] = v
    for lst in (0, 1):
        for (mode, gl), idxs in zip(units, owners):
            if mode != "Direct" and uses(mode, lst):
                for i in idxs:
                    d = rand_mvd(rng, sc)
                    w.se(d[0])
                    w.se(d[1])
                    parts[i]["mvd" if lst == 0 else "mvd1"] = d
    w.ue(0)
    return dict(kind="inter", name=name, parts=parts)


def write_p_inter(w, rng, sc, valid0, n0):
    raw = rng.choice([0, 0, 1, 2, 3])
    w.ue(raw)
    if raw < 3:
        name = ("P_L0_16x16", "P_L0_L0_16x8", "P_L0_L0_8x16")[raw]
        geo = {0: [(0, 0, 16, 16)], 1: [(0, 0, 16, 8), (0, 8, 16, 8)], 2: [(0, 0, 8, 16), (8, 0, 8, 16)]}[raw]
        parts = [dict(x=g[0], y=g[1], w=g[2], h=g[3], mode="L0", ref=rng.choice(valid0), mvd=(0, 0)) for g in geo]
        for p_ in parts:
            write_te(w, p_["ref"], n0)
    else:
        name = "P_8x8"
        subs = [rng.randint(0, 3) for _ in range(4)]
        for s in subs:
            w.ue(s)
        parts = []
        for q, s in enumerate(subs):
            x0, y0 = (q & 1) * 8, (q >> 1) * 8
            sw, sh = P_SUB_SHAPE[s]
            n = (8 // sw) * (8 // sh)
            ref = rng.choice(valid0)
            for j in range(n):
                parts.append(dict(x=x0 + (j % (8 // sw)) * sw, y=y0 + (j // (8 // sw)) * sh, w=sw, h=sh, mode="L0", ref=ref, mvd=(0, 0)))
        seen = set()
        for q in range(4):
            r = parts[sum((8 // P_SUB_SHAPE[s][0]) * (8 // P_SUB_SHAPE[s][1]) for s in subs[:q])]["ref"]
            write_te(w, r, n0)
    for p_ in parts:
        p_["mvd"] = rand_mvd(rng, sc)
        w.se(p_["mvd"][0])
        w.se(p_["mvd"][1])
    w.ue(0)
    return dict(kind="inter", name=name, parts=parts)


def write_mmco(w, mmcos):
    w.raw(1, 1)
    for op, a in mmcos:
        w.ue(op)
        for v in a.values():
            w.ue(v)
    w.ue(0)


def write_slice(rng, sc, ctx):
    """ctx: dict(pic (the picture dict), slice_type 0/1/2, first, last, ...).  -> (nal bytes, manifest slice)"""
    w = W()
    p = ctx["pic"]
    st = ctx["slice_type"]
    w.ue(ctx["first"])
    w.ue(st)
    w.ue(ctx["pps"])
    w.raw(sc["log2_fn"], p["frame_num"])
    w.raw(1, 0 if p["struct"] == "F" else 1)
    if p["struct"] != "F":
        w.raw(1, 1 if p["struct"] == "B" else 0)
    if p["idr"]:
        w.ue(p["idr_id"])
    w.raw(sc["log2_poc"], p["lsb"])
    if p["struct"] == "F":
        w.se(p["dbot"])
    if st == 1:
        w.raw(1, ctx["spatial"])
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
    if p["ref_idc"]:
        if p["idr"]:
            w.raw(1, 0)
            w.raw(1, p["lt_flag"])
        elif p["mmcos"]:
            write_mmco(w, p["mmcos"])
        else:
            w.raw(1, 0)
    w.se(ctx["qp"] - 26)
    w.ue(ctx["dbf"][0])
    if ctx["dbf"][0] != 1:
        w.se(ctx["dbf"][1])
        w.se(ctx["dbf"][2])
    slots0, slots1 = ctx["L0"], ctx["L1"]
    valid = [[i for i, e in enumerate(slots0) if e is not None], [i for i, e in enumerate(slots1) if e is not None]]
    n_active = [ctx["a0"], ctx["a1"]]
    direct_ok = bool(ctx["direct_ok"])
    mbs, pend = [], 0
    tc = {}
    first = ctx["first"]
    mbw = sc["mbw"]
    for addr in range(ctx["first"], ctx["last"]):
        r = rng.random()
        kind = "inter"
        pi = sc["p_intra"]
        if st == 2:
            kind = "pcm" if r < sc["p_pcm"] else "i16"
        elif r < pi:
            kind = "pcm" if rng.random() < sc["p_pcm"] else "i16"
        elif st == 0 and valid[0] and r < pi + sc["p_skip"]:
            kind = "skip"
        elif st == 1 and direct_ok and r < pi + sc["p_bskip"]:
            kind = "bskip"
        if kind == "inter" and not (valid[0] or valid[1]):
            kind = "pcm"
        if kind == "inter" and st == 0 and not valid[0]:
            kind = "pcm"
        if kind in ("skip", "bskip"):
            pend += 1
            mbs.append(dict(kind="skip", name="P_Skip" if kind == "skip" else "B_Skip", parts=[]))
            tc[addr] = 0
            continue
        if st in (0, 1):
            w.ue(pend)
        pend = 0
        if kind == "pcm":
            w.ue({2: 25, 0: 30, 1: 48}[st])
            while len(w.bits) % 8:
                w.raw(1, 0)
            smp = pcm_samples(rng)
            for s in smp:
                w.raw(8, s)
            tc[addr] = 16
            mbs.append(dict(kind="pcm", name="I_PCM", samples=list(smp), parts=[]))
        elif kind == "i16":
            w.ue({2: 3, 0: 8, 1: 26}[st])
            w.ue(0)
            w.se(0)
            nb = []
            if addr % mbw and addr - 1 >= first:
                nb.append(tc.get(addr - 1, 0))
            if addr >= mbw and addr - mbw >= first:
                nb.append(tc.get(addr - mbw, 0))
            nc_token(w, 0 if not nb else (nb[0] if len(nb) == 1 else (nb[0] + nb[1] + 1) >> 1))
            tc[addr] = 0
            mbs.append(dict(kind="i16", name="I_16x16_2_0_0", parts=[]))
        else:
            e = write_b_inter(w, rng, sc, valid, n_active, direct_ok) if st == 1 else write_p_inter(w, rng, sc, valid[0], n_active[0])
            tc[addr] = 0
            mbs.append(e)
    if pend:
        w.ue(pend)
    hdr = (p["ref_idc"] << 5) | (5 if p["idr"] else 1)
    return nal(hdr, w.rbsp()), dict(first=ctx["first"], type=st, mbs=mbs, spatial=ctx["spatial"] if st == 1 else None,
                                    a0=ctx["a0"], a1=ctx["a1"], mods0=ctx["mods0"], mods1=ctx["mods1"], qp=ctx["qp"], dbf=ctx["dbf"],
                                    direct_ok=direct_ok)


# ---------------------------------------------------------------- planning
def make_plan(rng, sc):
    """-> units in decoding order: dict(kind 'I'|'P'|'B', idr, ref (bool), disp, struct 'F'|'TB'|'BT')."""
    def struct():
        if rng.random() < sc["p_pair"]:
            return "BT" if rng.random() < sc["p_bff"] else "TB"
        return "F"
    plan = [dict(kind="I", idr=True, ref=True, disp=0, struct=struct())]
    last = 0
    while len(plan) < sc["units"]:
        if rng.random() < sc["p_idr"]:
            plan.append(dict(kind="I", idr=True, ref=True, disp=0, struct=struct()))
            last = 0
            continue
        gap = 1 + (rng.randint(0, sc["maxb"]) if sc["maxb"] else 0)
        disp = last + gap
        kind = "B" if rng.random() < sc.get("p_tail", 0.0) else ("I" if rng.random() < sc["p_i_anchor"] else "P")
        plan.append(dict(kind=kind, idr=False, ref=True, disp=disp, struct=struct()))
        mid = list(range(last + 1, disp))
        rng.shuffle(mid)
        for d in mid:
            plan.append(dict(kind="B", idr=False, ref=rng.random() < sc["p_refb"], disp=d, struct=struct()))
        last = disp
    return plan[:sc["units"]]


def abs_diff_for(pred, target_num, maxp):
    nw = target_num + maxp if target_num < 0 else target_num
    d0 = (pred - nw) % maxp or maxp
    d1 = (nw - pred) % maxp or maxp
    return [(0, d0 - 1), (1, d1 - 1)], nw


def choose_mods(d, rng, cur, active, p_dup, p_lt):
    cands = d.candidates(cur)
    if not cands:
        return []
    shorts = [c for c in cands if c[2] == "S"]
    longs = [c for c in cands if c[2] == "L"]
    mods, pred, prev = [], d.curr_pic_num(cur), None
    maxp = d.max_pic_num(cur)
    for _ in range(rng.randint(1, min(active, 5))):
        if longs and (rng.random() < p_lt or not shorts):
            s, p, k = rng.choice(longs)
            mods.append((2, d.lt_pic_num(s, p, cur)))
            prev = (s, p, k)
            continue
        c = prev if (prev is not None and prev[2] == "S" and rng.random() < p_dup) else rng.choice(shorts)
        prev = c
        opts, nw = abs_diff_for(pred, d.pic_num(c[0], c[1], cur), maxp)
        mods.append(rng.choice(opts))
        pred = nw
    return mods


def _ops_state(d, cur, ops):
    """The model after applying `ops` (without marking the current picture): used to find what is left to remove."""
    sim = d.clone()
    sim.lenient = True
    curfs = next((s for s in sim.stores if s.id == cur["store"]), None) if cur["second"] else None
    if curfs is None:
        curfs = fdpb.FS(cur["store"], cur["frame_num"], cur["coded"])
    c2 = dict(cur)
    for op, a in ops:
        sim.mmco_step(op, a, c2, curfs)
    return sim


def fix_ops(d, cur, ops):
    """Append MMCO 1 / 2 commands that free the oldest frames until the model accepts the picture; None when impossible."""
    cpn = d.curr_pic_num(cur)
    for attempt in range(6):
        sim = d.clone()
        try:
            sim.finish_picture(cur, 0, ops or None)
            return ops or None
        except fdpb.StreamError:
            pass
        after = _ops_state(d, cur, ops)
        shorts = [s for s in after.stores if s.marked("S") and s.id != cur["store"]]
        longs = [s for s in after.stores if s.marked("L") and s.id != cur["store"]]
        if shorts:
            v = min(shorts, key=lambda s: after.fnw(s, cur["frame_num"]))
            for p in ("T", "B"):
                if v.mark[p] == "S":
                    if cur["struct"] == "F":
                        if p == "T":
                            if v.mark["B"] != "S":
                                return None
                            ops = ops + [(1, dict(difference_of_pic_nums_minus1=cpn - d.pic_num(v, "F", cur) - 1))]
                    else:
                        ops = ops + [(1, dict(difference_of_pic_nums_minus1=cpn - d.pic_num(v, p, cur) - 1))]
        elif longs:
            v = min(longs, key=lambda s: s.lt)
            for p in ("T", "B"):
                if v.mark[p] == "L":
                    if cur["struct"] == "F":
                        if p == "T":
                            ops = ops + [(2, dict(long_term_pic_num=d.lt_pic_num(v, "F", cur)))]
                    else:
                        ops = ops + [(2, dict(long_term_pic_num=d.lt_pic_num(v, p, cur)))]
        else:
            return None
    return None


def plan_mmco(d, rng, cur, sc):
    """Random valid memory_management_control_operations for a non-IDR reference picture, or None (sliding window).  Valid = the
    model accepts them and the DPB does not overflow.  Raises fdpb.StreamError when no valid marking exists (the caller retries)."""
    curfs = next((s for s in d.stores if s.id == cur["store"]), None) if cur["second"] else None
    forced_lt = curfs is not None and curfs.marked("L") and not curfs.marked("S")
    ops = []
    if forced_lt:
        ops.append((6, dict(long_term_frame_idx=curfs.lt)))
    elif rng.random() < sc["p_mmco"]:
        mx = d.max_lt
        mode = rng.choice(["lt_cur", "lt_cur", "forget", "short_to_lt", "short_to_lt", "forget_lt", "trunc"])
        if cur["struct"] == "F" and sc.get("p_mmco5", 0) and rng.random() < sc["p_mmco5"]:
            mode = "mmco5"
        if mode == "lt_cur" and cur["struct"] != "F" and not sc.get("mmco6_fields"):
            mode = "short_to_lt"                 # MMCO 6 on a field is not exercised: ffmpeg drops the first field of the frame (see Readme)
        cands = d.candidates(cur)
        shorts = [c for c in cands if c[2] == "S" and c[0].id != cur["store"]]
        longs = [c for c in cands if c[2] == "L"]
        cpn = d.curr_pic_num(cur)
        nidx = rng.randint(0, 2)
        if mode == "mmco5":
            ops.append((5, {}))
            if rng.random() < 0.5:
                ops.append((4, dict(max_long_term_frame_idx_plus1=1)))
                ops.append((6, dict(long_term_frame_idx=0)))
        elif mode == "lt_cur":
            if mx is None or mx < nidx:
                ops.append((4, dict(max_long_term_frame_idx_plus1=nidx + 1 + rng.randint(0, 1))))
            ops.append((6, dict(long_term_frame_idx=nidx)))
        elif mode == "short_to_lt" and shorts:
            s, p, k = rng.choice(shorts)
            idx = s.lt if (s.lt is not None and s.marked("L")) else nidx
            if mx is None or idx > mx:
                ops.append((4, dict(max_long_term_frame_idx_plus1=idx + 1 + rng.randint(0, 1))))
            targets = [(s, p)] if p == "F" else [(s, q) for q in "TB" if s.mark[q] == "S"]
            for s_, q in targets:
                ops.append((3, dict(difference_of_pic_nums_minus1=cpn - d.pic_num(s_, q, cur) - 1, long_term_frame_idx=idx)))
        elif mode == "forget" and shorts:
            s, p, k = rng.choice(shorts)
            ops.append((1, dict(difference_of_pic_nums_minus1=cpn - d.pic_num(s, p, cur) - 1)))
            if p != "F" and rng.random() < 0.5 and s.mark["T" if p == "B" else "B"] == "S":
                q = "T" if p == "B" else "B"
                ops.append((1, dict(difference_of_pic_nums_minus1=cpn - d.pic_num(s, q, cur) - 1)))
        elif mode == "forget_lt" and longs:
            s, p, k = rng.choice(longs)
            ops.append((2, dict(long_term_pic_num=d.lt_pic_num(s, p, cur))))
        elif mode == "trunc" and mx is not None:
            ops.append((4, dict(max_long_term_frame_idx_plus1=rng.randint(0, mx + 1))))
    fixed = fix_ops(d, cur, ops)
    if fixed is None and ops:
        fixed = fix_ops(d, cur, [])
    if fixed is None:
        sim = d.clone()
        sim.finish_picture(cur, 0, None)          # raises StreamError when even the sliding window fails
    return fixed


def poc_table(sc, seed):
    prng = random.Random(seed * 977 + 3)
    out, v = [0], 0
    for _ in range(400):
        v += prng.choice(sc["poc_steps"])
        out.append(v)
    return out


def make_stream(name, seed=7, avoid=None, sc=None):
    """Retries with another random plan (salt) when the planned marking cannot be made valid."""
    for salt in range(40):
        try:
            return _make_stream(name, seed, avoid, sc, salt)
        except fdpb.StreamError:
            continue
    raise RuntimeError(f"{name}: no valid plan")


def _make_stream(name, seed, avoid, sc, salt):
    """avoid: {(picture, slice in picture): 1 | 2}: 1 = that B slice is forced to spatial direct, 2 = it has no direct macroblock."""
    avoid = avoid or {}
    sc = dict(sc or SCENARIOS[name])
    sc["name"] = name
    rng = random.Random(seed * 131 + sc.get("seed_base", 0) * 7919 + salt * 104729)
    sc["cqo"] = rng.randint(-3, 3)
    plan = make_plan(rng, sc)
    pt = poc_table(sc, seed)
    data = sps_nal(sc) + b"".join(pps_nal(sc, i) for i in range(3))
    d = fdpb.Dpb(sc["refs"], sc["log2_fn"])
    maxfn = 1 << sc["log2_fn"]
    maxlsb = 1 << sc["log2_poc"]
    pocst = fpdec.Poc0(sc["log2_poc"])
    poc_off = 0
    used_pocs = set()
    pics = []
    idr_id = 0
    store_n = 0
    pic_no = 0
    for u in plan:
        idr = u["idr"]
        if u["struct"] == "F" and not idr and u["kind"] in ("P", "B") and not sc.get("frame_half") and any(
                (s.marked("S") or s.marked("L")) and not (s.mark["T"] == s.mark["B"] and s.mark["T"] is not None) for s in d.stores):
            u = dict(u, struct=rng.choice(["TB", "BT"]))     # ffmpeg puts frames with ONE marked field into frame lists (the standard: both)
        fn = 0 if idr else (d.prev_ref_fn + 1) % maxfn
        b = pt[u["disp"]] - poc_off if not idr else 0
        if idr:
            poc_off = pt[u["disp"]]
            used_pocs.clear()
        ref_idc = rng.choice([1, 2, 3]) if u["ref"] else 0
        structs = ["F"] if u["struct"] == "F" else list(u["struct"])
        off = 1
        if sc.get("p_spread") and rng.random() < sc["p_spread"]:
            off = rng.choice([3, 5, 7, 9, 11, 13])
        elif sc.get("p_equal_poc") and rng.random() < sc["p_equal_poc"]:
            off = 0
        if sc.get("p_spread") or sc.get("p_equal_poc"):
            for cand in [off, 1, 2, 3, 0]:
                if b not in used_pocs and (b + cand) not in used_pocs or (cand == 0 and b not in used_pocs):
                    off = cand
                    break
            else:
                raise fdpb.StreamError("POC collision")
            used_pocs.update({b, b + off})
        if u["struct"] == "F":
            bff = rng.random() < sc["p_bff"]
            ptop, pbot = (b + off, b) if bff else (b, b + off)
        sid = store_n
        store_n += 1
        for i, stt in enumerate(structs):
            this_idr = idr and i == 0
            pic = dict(n=pic_no, struct=stt, frame_num=fn, idr=this_idr, ref_idc=ref_idc, idr_id=idr_id if this_idr else 0, store=sid,
                       second=(i == 1), lt_flag=0, mmcos=None, kind=u["kind"])
            if stt == "F":
                pic.update(lsb=ptop % maxlsb, dbot=pbot - ptop)
            else:
                pic.update(lsb=(b + i * off) % maxlsb, dbot=0)
            top, bot = pocst.picture(dict(pic, idr=this_idr))
            want = (ptop, pbot) if stt == "F" else ((b + i * off, None) if stt == "T" else (None, b + i * off))
            assert (top, bot) == want, (top, bot, want, pic)
            cur = dict(struct=stt, frame_num=fn, idr=this_idr, ref=bool(ref_idc), second=(i == 1), store=sid,
                       coded="F" if stt == "F" else "P", poc_t=top, poc_b=bot,
                       poc=min(x for x in (top, bot) if x is not None) if stt == "F" else (top if stt == "T" else bot))
            other = next((s_ for s_ in d.stores if s_.id == sid), None) if i == 1 else None
            fp = [x for x in (top, bot) if x is not None]
            if other is not None:
                fp += [other.poc[q] for q in "TB" if other.has[q]]
            cur["frame_poc"] = min(fp)
            # picture kind
            if u["kind"] == "B":
                kind = "B"
            elif this_idr:
                kind = "I"
            elif i == 1:
                kind = "I" if (u["kind"] == "I" and rng.random() < 0.5) or rng.random() < sc["p_second_i"] else "P"
            else:
                kind = u["kind"]
            if u["idr"] and i == 1 and u["kind"] == "I":
                kind = "P" if rng.random() < 0.7 else "I"
            if idr and i == 0 and (stt == "F" or sc.get("mmco6_fields")) and sc["p_idr_lt"] and rng.random() < sc["p_idr_lt"]:
                pic["lt_flag"] = 1
            if ref_idc and not this_idr:
                pic["mmcos"] = plan_mmco(d, rng, cur, sc)
            has5 = bool(pic["mmcos"]) and any(op == 5 for op, _ in pic["mmcos"])
            new_top = None
            if has5:
                new_top = (top - min(top, bot)) if stt == "F" else None
                poc_off = b + poc_off if not idr else poc_off
                used_pocs.clear()
                used_pocs.update({x for x in ((top - min(top, bot)) if top is not None else None, (bot - min(top, bot)) if bot is not None else None) if x is not None})
            pocst.finish(bool(ref_idc), has5, stt, new_top)
            nsl = 1 if this_idr else rng.randint(1, sc["slices"])
            n_mbs = sc["mbw"] * (sc["map_h"] * (2 if stt == "F" else 1))
            cuts = sorted(rng.sample(range(1, n_mbs), nsl - 1)) if nsl > 1 else []
            bounds = list(zip([0] + cuts, cuts + [n_mbs]))
            pps = rng.randrange(3)
            pic["slices"] = []
            for si, (a, bnd) in enumerate(bounds):
                srng = random.Random(f"{seed}/{name}/{pic_no}/{si}")
                st = {"I": 2, "P": 0, "B": 1}[kind]
                lvl = avoid.get((pic_no, si), 0)
                qp = srng.randint(18, 36)
                dbf = {0: (1, 0, 0), 1: (0, srng.randint(-3, 3), srng.randint(-3, 3)),
                       2: (srng.choice([0, 0, 2]), srng.randint(-3, 3), srng.randint(-3, 3))}[sc["deblock"]]
                spatial = 1 if (srng.random() < sc["spatial"] or lvl >= 1) else 0
                ctx = dict(pic=pic, first=a, last=bnd, pps=pps, slice_type=st, a0=1, a1=1, override=False, mods0=[], mods1=[], L0=[], L1=[],
                           qp=qp, dbf=dbf, spatial=spatial, direct_ok=False)
                if st in (0, 1) and not this_idr:
                    fld = stt != "F"
                    cap = 2 if fld else 1
                    if srng.random() < 0.4 and PPS_ACTIVE0[pps] <= 32:
                        ctx["a0"], ctx["a1"] = PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]
                    else:
                        ctx["a0"] = srng.randint(1, min(32, cap * sc["max_a0"]))
                        ctx["a1"] = srng.randint(1, min(32, cap * sc["max_a1"]))
                        ctx["override"] = True
                    if (ctx["a0"], ctx["a1"] if st == 1 else PPS_ACTIVE1[pps]) != (PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]):
                        ctx["override"] = True
                    for attempt in range(3):
                        if srng.random() < sc["p_mod"] or attempt:
                            ctx["mods0"] = choose_mods(d, srng, cur, ctx["a0"], sc["p_dup"], sc["p_lt_mod"])
                            ctx["mods1"] = choose_mods(d, srng, cur, ctx["a1"], sc["p_dup"], sc["p_lt_mod"]) \
                                if st == 1 and srng.random() < 0.8 else []
                        ctx["L0"], ctx["L1"] = d.lists(cur, st == 1, ctx["a0"], ctx["a1"], ctx["mods0"], ctx["mods1"])
                        if any(e is not None for e in ctx["L0"]) and (st == 0 or any(e is not None for e in ctx["L1"])):
                            break
                    else:
                        ctx["mods0"] = ctx["mods1"] = []
                        ctx["L0"], ctx["L1"] = d.lists(cur, st == 1, ctx["a0"], ctx["a1"])
                    if not ctx["override"] and (ctx["a0"], ctx["a1"]) != (PPS_ACTIVE0[pps], PPS_ACTIVE1[pps]):
                        ctx["override"] = True
                    if not any(e is not None for e in ctx["L0"]) or (st == 1 and not any(e is not None for e in ctx["L1"])):
                        ctx["slice_type"] = st = 2
                        ctx["mods0"] = ctx["mods1"] = []
                    elif st == 1:
                        e0, e1 = ctx["L0"][0], ctx["L1"][0]
                        okcol = False
                        if e0 is not None and e1 is not None:
                            s1 = next((s for s in d.stores if s.id == e1[0]), None)
                            okcol = (s1 is not None and (s1.coded == ("F" if stt == "F" else "P") or sc.get("mixed_col"))
                                     and s1.id != sid)   # (ffmpeg disagrees with the standard when the co-located field is the first field of the current frame)
                        ctx["direct_ok"] = lvl < 2 and okcol
                elif st in (0, 1):
                    st = ctx["slice_type"] = 2
                nb, msl = write_slice(srng, sc, ctx)
                data += nb
                pic["slices"].append(msl)
            # POC / marking bookkeeping for the planning model
            d.finish_picture(cur, pic["lt_flag"], pic["mmcos"])
            if this_idr:
                idr_id = (idr_id + 1) % 8
            pic.pop("kind")
            pics.append(pic)
            pic_no += 1
    man = dict(sc=sc, pics=pics)
    return data, man


def write_stream(name, path, seed=7, validate=True, sc=None):
    """Write the stream (and path + '.json').  With validate, the reference decoder checks that every direct slice is legal and the
    stream is regenerated with the offending slices made spatial / direct-free."""
    avoid = {}
    for it in range(60):
        data, man = make_stream(name, seed, avoid, sc)
        with open(path, "wb") as f:
            f.write(data)
        if not validate:
            break
        try:
            fpdec.decode(man, pixels=False, strict=True)
            break
        except fpdec.direct_ref.DirectInvalid as e:
            key = (e.pic, e.slice)
            avoid[key] = avoid.get(key, 0) + 1
    else:
        raise RuntimeError(f"{name}: no legal stream found")
    man["avoided"] = len(avoid)
    with open(path + ".json", "w") as f:
        json.dump(man, f)
    return man


if __name__ == "__main__":
    write_stream(sys.argv[1], sys.argv[2])
