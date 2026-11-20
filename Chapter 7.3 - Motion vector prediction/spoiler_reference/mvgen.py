"""SPOILER: synthetic stream generator for Chapter 7.3 (bit writer: common/bitwriter.py; predictor: mvpred_ref.PicState).

Tiny CAVLC streams (SPS/PPS/slices written by hand, 128x96 pixels, deblocking off) built so that the final motion vectors can be
proved by PIXELS, independently of any parser:
  * picture 0 is an IDR of random-texture I_PCM macroblocks;
  * every later picture is made of P macroblocks WITHOUT residual (cbp 0), P_Skip, and a few intra macroblocks (I_PCM = fresh
    random texture, I_NxN with cbp 0 = flat DC blocks) that act as "available but intra" neighbours;
  * the generator chooses the TARGET vector of every partition as a whole-pixel vector (a multiple of 4 quarter samples) or the
    predictor itself (mvd = 0) and writes mvd = target - mvp, computing mvp with mvpred_ref.PicState.  If that predictor
    were wrong, ffmpeg (which adds the TRUE predictor) would copy a different block, and the pixel check of the harness fails.
  * P_Skip vectors are whatever the predictor says; they are whole-pixel as well (a median of whole-pixel vectors).
All pictures are reference pictures; reference list 0 is the previous pictures, newest first (ref_idx r = picture p - 1 - r).
Modes: "mv" (1..3 slices), "mvslices" (5..8 slices: slice-edge availability), "mvsub" (sub-macroblock heavy), "mvskip" (skip heavy).
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.bitwriter import W, nal  # noqa: E402
import mvpred_ref  # noqa: E402

W_MBS, H_MBS = 8, 6
N_MBS = W_MBS * H_MBS
MAX_REFS = 4
RAW = {"p16": 0, "p16x8": 1, "p8x16": 2, "p8x8": 3, "p8x8ref0": 4}
PARTS = {"p16": [(0, 0, 16, 16)], "p16x8": [(0, 0, 16, 8), (0, 8, 16, 8)], "p8x16": [(0, 0, 8, 16), (8, 0, 8, 16)]}
SUBS = {0: [(0, 0, 8, 8)], 1: [(0, 0, 8, 4), (0, 4, 8, 4)], 2: [(0, 0, 4, 8), (4, 0, 4, 8)],
        3: [(0, 0, 4, 4), (4, 0, 4, 4), (0, 4, 4, 4), (4, 4, 4, 4)]}
KINDNAME = {"p16": "16x16", "p16x8": "16x8", "p8x16": "8x16", "p8x8": "sub", "p8x8ref0": "sub"}


def sps_nal():
    w = W()
    w.raw(8, 66); w.raw(8, 0); w.raw(8, 30)
    w.ue(0)
    w.ue(0)                               # log2_max_frame_num_minus4 -> 4 bits
    w.ue(2)                               # pic_order_cnt_type 2
    w.ue(MAX_REFS)
    w.raw(1, 0)
    w.ue(W_MBS - 1); w.ue(H_MBS - 1)
    w.raw(1, 1); w.raw(1, 1); w.raw(1, 0); w.raw(1, 0)
    return nal(0x67, w.rbsp())


def pps_nal():
    w = W()
    w.ue(0); w.ue(0)
    w.raw(1, 0); w.raw(1, 0)
    w.ue(0)                               # one slice group
    w.ue(0); w.ue(0)                      # num_ref_idx default minus1 (both lists)
    w.raw(1, 0); w.raw(2, 0)
    w.se(0); w.se(0); w.se(0)
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, 0); w.raw(1, 0)
    return nal(0x68, w.rbsp())


def write_te(w, v, active):
    if active == 1:
        assert v == 0
    elif active == 2:
        w.raw(1, 1 - v)
    else:
        w.ue(v)


def rand_target(rng, wide):
    r = rng.random()
    if r < 0.8 or not wide:
        return (4 * rng.randint(-6, 6), 4 * rng.randint(-6, 6))
    return (4 * rng.randint(-60, 60), 4 * rng.randint(-40, 40))


WEIGHTS = {
    "mv": [("skip", 4), ("p16", 4), ("p16x8", 3), ("p8x16", 3), ("p8x8", 4), ("p8x8ref0", 1), ("pcm", 1), ("nxn", 1)],
    "mvslices": [("skip", 3), ("p16", 3), ("p16x8", 3), ("p8x16", 3), ("p8x8", 3), ("p8x8ref0", 1), ("pcm", 1), ("nxn", 1)],
    "mvsub": [("skip", 1), ("p16", 1), ("p16x8", 1), ("p8x16", 1), ("p8x8", 10), ("p8x8ref0", 2), ("pcm", 0.5), ("nxn", 0.7)],
    "mvskip": [("skip", 12), ("p16", 3), ("p16x8", 1), ("p8x16", 1), ("p8x8", 1), ("p8x8ref0", 0.3), ("pcm", 0.6), ("nxn", 0.8)],
}


def write_slice(plan, p, st, rng, mode, entries):
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
    w.se(0)
    w.ue(1)                               # disable_deblocking_filter_idc = 1
    hend = w.pos()
    active = plan["active"]
    pend = 0
    for i, kind in enumerate(plan["kinds"]):
        a = plan["first"] + i
        if idr:
            w.ue(25)
            while len(w.bits) % 8:
                w.raw(1, 0)
            for _ in range(384):
                w.raw(8, rng.randrange(1, 256))
            st.store_intra(sl, a)
            continue
        if kind == "skip":
            pend += 1
            mv = st.skip_mv(sl, a)
            st.store(sl, a, 0, 0, 16, 16, 0, mv)
            continue
        w.ue(pend)
        pend = 0
        if kind == "pcm":
            w.ue(30)
            while len(w.bits) % 8:
                w.raw(1, 0)
            for _ in range(384):
                w.raw(8, rng.randrange(1, 256))
            st.store_intra(sl, a)
            continue
        if kind == "nxn":
            w.ue(5)
            for _ in range(16):
                w.raw(1, 1)               # prev_intra4x4_pred_mode_flag = 1 (predicted mode; every block is DC)
            w.ue(0)                       # intra_chroma_pred_mode DC
            w.ue(3)                       # coded_block_pattern codeNum 3 = 0 (intra column)
            st.store_intra(sl, a)
            continue
        # inter macroblock
        w.ue(RAW[kind])
        if kind in ("p8x8", "p8x8ref0"):
            subs = [rng.randrange(4) for _ in range(4)]
            if mode == "mvsub" and rng.random() < 0.5:
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
            tgt = mvp if rng.random() < 0.3 else rand_target(rng, mode != "mvskip")
            mvd = (tgt[0] - mvp[0], tgt[1] - mvp[1])
            st.store(sl, a, x, y, ww, hh, r, tgt)
            mvds.append(mvd)
        for d in mvds:
            w.se(d[0]); w.se(d[1])
        w.ue(0)                           # coded_block_pattern 0: no residual, no mb_qp_delta
    if pend:
        w.ue(pend)
    return nal(0x65 if idr else 0x41, w.rbsp()), dict(first=plan["first"], header_end_bit=hend)


def make_stream(mode="mv", seed=5, pictures=14):
    rng = random.Random(seed * 31 + len(mode))
    data = sps_nal() + pps_nal()
    pics = []
    index = 0
    for p in range(pictures):
        st = mvpred_ref.PicState(W_MBS, H_MBS)
        if p == 0:
            cuts = [0, N_MBS]
        else:
            k = {"mv": rng.choice([1, 2, 3]), "mvslices": rng.randint(5, 8)}.get(mode, rng.choice([1, 2]))
            cuts = sorted({0} | set(rng.sample(range(1, N_MBS), k - 1))) + [N_MBS]
        entries = []
        for a, b in zip(cuts, cuts[1:]):
            n_ref = min(p, MAX_REFS)
            active = rng.randint(1, max(1, n_ref)) if p else 1
            kinds = [rng.choices([x for x, _ in WEIGHTS[mode]], [y for _, y in WEIGHTS[mode]])[0] for _ in range(b - a)]
            plan = dict(first=a, kinds=kinds, index=index, active=active, override=(active != 1))
            nb, ent = write_slice(plan, p, st, rng, mode, entries)
            data += nb
            entries.append(ent)
            index += 1
        pics.append(dict(slices=entries))
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, mode=mode, pictures=pics)
