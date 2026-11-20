"""SPOILER-adjacent: synthetic stream generator for the Chapter 7.2 harness (bit writer: common/bitwriter.py).

Writes tiny CAVLC Baseline streams (96x80 pixels = 6x5 macroblocks) whose pictures are built so that the decoded pixels reveal which
earlier picture each ref_idx names: every macroblock is either I_PCM (random samples, unique to this picture), P_Skip (copy of list
entry 0) or P_L0_16x16 with ref_idx r, mvd 0 and no residual (an exact copy of list entry r, because all motion vectors are 0).
Deblocking is off.  The generator never needs to know the reference list: it only avoids ref_idx values that would point at a
"no reference picture" entry, using a trivial count of the references that exist.

Scenarios (SCENARIOS) cover frame_num wrap-around (log2_max_frame_num = 4), non-reference P pictures (nal_ref_idc = 0, never two in
a row because pic_order_cnt_type 2 forbids it), IDR pictures in the middle (also two in a row), several max_num_ref_frames, num_ref_idx_active
from the PPS default and from the slice-header override (also larger than the references that exist, and larger than
max_num_ref_frames), two slices per picture with different num_ref_idx_active.
Manifest: width_mbs, height_mbs, pictures: [{slices: [{first, mbs: [{addr, kind pcm|skip|p16, ref, samples?}]}]}].
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from common.bitwriter import W, nal  # noqa: E402

W_MBS, H_MBS = 6, 5
N_MBS = W_MBS * H_MBS
PPS_DEFAULT_ACTIVE = [1, 2, 4]            # num_ref_idx_l0_default_active of PPS 0, 1, 2

# name -> dict(pictures, log2 (log2_max_frame_num), refs (max_num_ref_frames), idr: set of picture indexes,
#              nonref: rule, slices: max slices per picture, max_active)
SCENARIOS = {
    "wrap":   dict(pictures=45, log2=4, refs=4, idr=set(), nonref=lambda p: False, slices=2, max_active=5),
    "nonref": dict(pictures=40, log2=4, refs=3, idr=set(), nonref=lambda p: p % 3 == 2, slices=2, max_active=5),
    "idr":    dict(pictures=38, log2=4, refs=2, idr={9, 23, 24}, nonref=lambda p: p % 4 == 3, slices=1, max_active=4),
    "ref8":   dict(pictures=56, log2=4, refs=8, idr=set(), nonref=lambda p: p in (20, 21 + 6, 40), slices=2, max_active=9),
    "ref1":   dict(pictures=30, log2=5, refs=1, idr={15}, nonref=lambda p: p % 2 == 1 and p % 5 == 1, slices=1, max_active=3),
}


def sps_nal(log2, refs):
    w = W()
    w.raw(8, 66)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    w.ue(log2 - 4)                        # log2_max_frame_num_minus4
    w.ue(2)                               # pic_order_cnt_type 2: output order = decoding order
    w.ue(refs)                            # max_num_ref_frames
    w.raw(1, 0)                           # gaps_in_frame_num_value_allowed_flag
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x67, w.rbsp())


def pps_nal(pps_id):
    w = W()
    w.ue(pps_id)
    w.ue(0)
    w.raw(1, 0)                           # CAVLC
    w.raw(1, 0)
    w.ue(0)
    w.ue(PPS_DEFAULT_ACTIVE[pps_id] - 1)  # num_ref_idx_l0_default_active_minus1
    w.ue(0)
    w.raw(1, 0)
    w.raw(2, 0)
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


def slice_nal(rng, sc, pic, first, last, idr, ref_idc, fn, idr_id, pps, active, override, avail):
    """One slice covering macroblocks first..last-1.  -> (nal bytes, manifest entry)"""
    w = W()
    w.ue(first)
    w.ue(2 if idr else 0)
    w.ue(pps)
    w.raw(sc["log2"], fn)
    if idr:
        w.ue(idr_id)
    else:
        w.raw(1, 1 if override else 0)
        if override:
            w.ue(active - 1)
        w.raw(1, 0)                       # ref_pic_list_modification_flag_l0
    if ref_idc:
        if idr:
            w.raw(1, 0)
            w.raw(1, 0)
        else:
            w.raw(1, 0)                   # adaptive_ref_pic_marking_mode_flag
    w.se(0)
    w.ue(1)                               # disable_deblocking_filter_idc = 1
    mbs, pend = [], 0
    for addr in range(first, last):
        if idr:
            kind = "pcm"
        else:
            kind = rng.choices(["pcm", "skip", "p16"], [3, 2, 5])[0]
        if kind == "skip":
            pend += 1
            mbs.append(dict(addr=addr, kind="skip", ref=0))
            continue
        if not idr:
            w.ue(pend)
        pend = 0
        if kind == "pcm":
            w.ue(25 if idr else 30)
            while len(w.bits) % 8:
                w.raw(1, 0)
            samples = bytes(rng.randrange(1, 256) for _ in range(384))
            for s in samples:
                w.raw(8, s)
            mbs.append(dict(addr=addr, kind="pcm", ref=-1, samples=samples.hex()))
        else:
            r = rng.randrange(min(avail, active))
            w.ue(0)                       # P_L0_16x16
            write_te(w, r, active)
            w.se(0)
            w.se(0)
            w.ue(0)                       # coded_block_pattern codeNum 0 -> cbp 0 (inter)
            mbs.append(dict(addr=addr, kind="p16", ref=r))
    if pend:
        w.ue(pend)
    hdr = 0x65 if idr else (0x41 if ref_idc else 0x01)
    return nal(hdr, w.rbsp()), dict(first=first, mbs=mbs)


def make_stream(name, seed=5):
    sc = SCENARIOS[name]
    rng = random.Random(seed * 101 + sum(map(ord, name)))
    data = sps_nal(sc["log2"], sc["refs"]) + b"".join(pps_nal(i) for i in range(3))
    pictures = []
    maxfn = 1 << sc["log2"]
    prev_ref_fn, avail, idr_id = 0, 0, 0
    for p in range(sc["pictures"]):
        idr = p == 0 or p in sc["idr"]
        ref_idc = 0 if (not idr and sc["nonref"](p)) else rng.choice([1, 2, 3])
        if idr:
            fn, avail = 0, 0
            if p and (p - 1) in sc["idr"]:
                idr_id += 1               # two IDR pictures in a row need different idr_pic_id
        else:
            fn = (prev_ref_fn + 1) % maxfn
        nsl = rng.randint(1, sc["slices"]) if not idr else 1
        cut = rng.randrange(1, N_MBS) if nsl == 2 else None
        bounds = [(0, N_MBS)] if nsl == 1 else [(0, cut), (cut, N_MBS)]
        slices = []
        pps = rng.randrange(3)            # all slices of a picture use the same PPS (7.4.1.2.4)
        for a, b in bounds:
            if idr:
                active, override = 1, False
            elif rng.random() < 0.5:
                active, override = PPS_DEFAULT_ACTIVE[pps], False
            else:
                active, override = rng.randint(1, sc["max_active"]), True
            nb, ent = slice_nal(rng, sc, p, a, b, idr, ref_idc, fn, idr_id, pps, active, override, max(avail, 1))
            data += nb
            slices.append(ent)
        pictures.append(dict(slices=slices, idr=idr, ref_idc=ref_idc, frame_num=fn))
        if ref_idc:
            prev_ref_fn = fn
            avail = min(avail + 1, sc["refs"])
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, scenario=name, pictures=pictures)
