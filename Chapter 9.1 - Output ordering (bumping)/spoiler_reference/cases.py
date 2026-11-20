"""SPOILER: hand-made picture traces for Chapter 9.1 (situations x264 cannot produce: MMCO 5, no_output_of_prior_pics_flag in
odd places, level 1b, tiny pictures, negative POC, size changes, DPB sizes that are too small).

hand_cases() -> {name: pics}, each pics a list in the shape of frontend.load_pictures() (plus the keys the trace needs).
Kinds in a pattern: I = IDR, N = IDR with no_output_of_prior_pics_flag = 1, P = reference picture, B = non-reference picture,
M = reference picture with MMCO 5.  Each item is (kind, poc) where poc is the value 8.2.1 gives (before the MMCO 5 rebase).
Reference marking is the sliding window with nref frames (sliding window only; an I / N / M picture clears everything).
"""


def build(items, nref=3, mdfb=None, profile=100, level=40, cs3=0, wmbs=22, hmbs=18, start_id=0, base=None):
    pics = []
    refs = []
    for k, (kind, poc) in enumerate(items):
        pid = start_id + k
        unref = []
        ref = kind != "B"
        if kind in ("I", "N", "M"):
            unref, refs = list(refs), []
        elif kind == "P" and len(refs) >= nref:
            unref.append(refs.pop(0))
        if ref:
            refs.append(pid)
        pics.append(dict(id=pid, poc=poc, idr=int(kind in ("I", "N")), mmco5=int(kind == "M"), ref=int(ref),
                         noout=int(kind == "N"), unref=unref, profile=profile, level=level, cs3=cs3, wmbs=wmbs, hmbs=hmbs,
                         mdfb=mdfb, nref=nref))
    return pics


def _gop(first, kinds_pocs):
    return [(first, 0)] + kinds_pocs


# decoding-order pattern of one classic "I P B B" group of pictures: POCs in units of 2 per displayed frame
def _ipbb(n_groups, first="I", base=0):
    out = [(first, base)]
    for g in range(n_groups):
        b = base + 6 * g
        out += [("P", b + 6), ("B", b + 2), ("B", b + 4)]
    return out


def _pyr(n_groups, first="I", base=0):
    """pyramid: P(8) B-ref(4) b(2) b(6) per group of four displayed frames, POC step 2 per frame"""
    out = [(first, base)]
    for g in range(n_groups):
        b = base + 8 * g
        out += [("P", b + 8), ("P", b + 4), ("B", b + 2), ("B", b + 6)]
    return out


def hand_cases():
    c = {}
    # --- MMCO 5: flush, POC restart, leading picture with negative POC afterwards ---------------------------------------------------
    items = _pyr(2) + [("M", 24), ("B", -4), ("P", 6), ("B", 2), ("P", 12), ("B", 8), ("B", 10)]
    c["h_mmco5"] = build(items, nref=3, mdfb=4)
    # MMCO 5 right after IDR period without waiting pictures, then a second MMCO 5 with a full DPB
    items = [("I", 0), ("P", 2), ("M", 4), ("P", 2), ("P", 4), ("P", 6), ("M", 8), ("P", 2)] + _ipbb(2, "P", 4)[1:]
    c["h_mmco5_twice"] = build(items, nref=2, mdfb=3)
    # --- no_output_of_prior_pics_flag ------------------------------------------------------------------------------------------------
    items = _pyr(2) + [("N", 0)] + _pyr(2, "P", 0)[1:] + [("I", 0)] + _ipbb(2, "P", 0)[1:] + [("N", 0), ("P", 2), ("N", 0)]
    c["h_noout"] = build(items, nref=3, mdfb=5)
    items = [("N", 0), ("P", 4), ("B", 2), ("I", 0), ("P", 4), ("B", 2), ("N", 0)]      # first picture flagged: nothing to discard
    c["h_noout_edge"] = build(items, nref=2, mdfb=4)
    # --- negative POC (leading pictures after an IDR) ---------------------------------------------------------------------------------
    items = [("I", 0), ("P", 4), ("B", -6), ("B", -4), ("P", 10), ("B", 2), ("B", -2), ("P", 16), ("B", 8), ("B", 12)]
    c["h_negpoc"] = build(items, nref=2, mdfb=4)
    # --- DPB size from the level: level 1b (level_idc 11 + constraint_set3) vs level 1.1, 176x144 (99 MBs) -------------------------
    c["h_level1b"] = build(_ipbb(8), nref=2, profile=66, level=11, cs3=1, wmbs=11, hmbs=9, mdfb=None)
    c["h_level11_main"] = build(_ipbb(8), nref=2, profile=77, level=11, cs3=0, wmbs=11, hmbs=9, mdfb=None)
    c["h_level30_cif"] = build(_pyr(6), nref=4, profile=100, level=30, wmbs=22, hmbs=18, mdfb=None)         # 8100/396 = 20 -> 16
    c["h_level30_720p"] = build(_pyr(6), nref=4, profile=100, level=31, wmbs=80, hmbs=45, mdfb=None)       # 18000/3600 = 5
    # intra-only profile (High 10 Intra: profile_idc 110 + constraint_set3_flag) without bitstream_restriction: size 0 (E.2.1)
    c["h_intra_profile"] = build([("I", 0)] + [("P", 2 * k) for k in range(1, 8)], nref=1, profile=110, level=30, cs3=1,
                                 wmbs=22, hmbs=18, mdfb=None)
    # tiny pictures: the 16-frame cap (184320 / 1 MB would be a huge number)
    c["h_cap16"] = build(_ipbb(14), nref=4, profile=100, level=51, wmbs=1, hmbs=1, mdfb=None)
    # --- non-reference picture output immediately ---------------------------------------------------------------------------------------
    c["h_nonref_now"] = build(_pyr(5), nref=2, mdfb=3)
    # a reference picture with the lowest POC arrives while the DPB is full (leading reference pictures)
    c["h_ref_lowest_small_dpb"] = build([("I", 0), ("P", 10), ("P", -4), ("P", -2), ("P", 4), ("B", 2), ("P", 16), ("B", 12)], nref=2, mdfb=3)
    # --- DPB sizes: 0, 1, big ----------------------------------------------------------------------------------------------------------
    c["h_mdfb0"] = build([("I", 0), ("P", 2), ("P", 4), ("I", 0), ("P", 2), ("B", 3)], nref=1, mdfb=0)
    c["h_mdfb1"] = build([("I", 0), ("P", 2), ("P", 4), ("B", 3), ("I", 0), ("P", 2), ("P", 4)], nref=1, mdfb=1)
    c["h_mdfb16"] = build(_pyr(5) + [("I", 0)] + _pyr(3, "P", 0)[1:], nref=4, mdfb=16)
    # --- size change at an IDR (the spec's optional inference of no_output_of_prior_pics_flag is NOT applied) --------------------------------
    a = build(_ipbb(3), nref=2, mdfb=3)
    b = build([("I", 0), ("P", 6), ("B", 2), ("B", 4), ("P", 12), ("B", 8), ("B", 10)], nref=2, mdfb=3, wmbs=40, hmbs=23, start_id=len(a))
    b[0]["noout"] = 0
    c["h_sizechange"] = a + b
    return c
