"""Synthetic P-slice generator for the Chapter 7.1 harness (bit writer: common/bitwriter.py).

It writes tiny H.264 streams (SPS, a few PPSs, one IDR picture of I_PCM macroblocks, then P pictures; CAVLC, 96x80
pixels) whose macroblocks are chosen on purpose: every P mb_type (P_L0_16x16, 16x8, 8x16, P_8x8, P_8x8ref0), every
sub_mb_type, ref_idx_l0 with num_ref_idx_active 1, 2, 3 and 4 (PPS default and slice-header override), skip runs of all
lengths (including a run that ends the slice and a picture that is one single run), intra macroblocks in P slices
(I_PCM, Intra16x16, I_NxN), coded_block_pattern codeNums, mb_qp_delta, transform_size_8x8_flag (High profile mode),
several slices per picture.  Everything is valid, so ffmpeg decodes the streams without errors.

Three modes (make_stream):
  "zero": every mvd is 0 and no macroblock has residual (cbp may be non-zero, but every coeff_token is TotalCoeff = 0).
          All motion vectors are then 0 (the predictor of zeros is zero), so an inter partition is an exact copy of
          reference picture ref_idx and the decoded pixels can be predicted: the manifest lists for every inter
          macroblock its copy regions (x, y, w, h, ref_idx); I_PCM macroblocks carry their raw samples.
  "mvd":  random mvd values (parse-only truth: the manifest lists what was written).
  "high": like "mvd" with a High-profile SPS / PPS (transform_8x8_mode_flag = 1): exercises the inter
          transform_size_8x8_flag rules and I_NxN with 8x8 transform.
The manifest is written by the bit writer, independently of common/inter_ref.py.  coded_block_pattern codeNums use the
tables of Table 9-4 (typed again here from the standard; a wrong entry would make ffmpeg desynchronise, which the harness sees).
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.bitwriter import W, nal  # noqa: E402

W_MBS, H_MBS = 6, 5
N_MBS = W_MBS * H_MBS
MAX_REFS = 4

# Table 9-4, codeNum -> coded_block_pattern, ChromaArrayType 1 (luma bits 0..3, chroma = value >> 4)
CBP_INTER_TAB = [0, 16, 1, 2, 4, 8, 32, 3, 5, 10, 12, 15, 47, 7, 11, 13,
                 14, 6, 9, 31, 35, 37, 42, 44, 33, 34, 36, 40, 39, 43, 45, 46,
                 17, 18, 20, 24, 19, 21, 26, 28, 23, 27, 29, 30, 22, 25, 38, 41]
CBP_INTRA_TAB = [47, 31, 15, 0, 23, 27, 29, 30, 7, 11, 13, 14, 39, 43, 45, 46,
                 16, 3, 5, 10, 12, 19, 21, 26, 28, 35, 37, 42, 44, 1, 2, 4,
                 8, 17, 18, 20, 24, 6, 9, 22, 25, 32, 33, 34, 36, 40, 38, 41]

# (num_ref_idx_l0_default_active_minus1, constrained_intra_pred_flag) of PPS 0..3
PPS_TABLE = [(0, 0), (1, 0), (3, 1), (2, 0)]

P_TYPES = {"p16": (0, "P_L0_16x16", 1), "p16x8": (1, "P_L0_L0_16x8", 2), "p8x16": (2, "P_L0_L0_8x16", 2),
           "p8x8": (3, "P_8x8", 4), "p8x8ref0": (4, "P_8x8ref0", 4)}
SUB_COUNT = [1, 2, 2, 4]                     # mvd pairs of sub_mb_type 0..3
PART_RECTS = {"p16": [(0, 0, 16, 16)], "p16x8": [(0, 0, 16, 8), (0, 8, 16, 8)], "p8x16": [(0, 0, 8, 16), (8, 0, 8, 16)]}
QUAD = [(0, 0), (8, 0), (0, 8), (8, 8)]


def sps_nal(high):
    w = W()
    w.raw(8, 100 if high else 66)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)                               # sps id
    if high:
        w.ue(1)                           # chroma_format_idc
        w.ue(0)
        w.ue(0)
        w.raw(1, 0)                       # qpprime_y_zero_transform_bypass_flag
        w.raw(1, 0)                       # seq_scaling_matrix_present_flag
    w.ue(0)                               # log2_max_frame_num_minus4 -> frame_num has 4 bits
    w.ue(2)                               # pic_order_cnt_type 2 (output order = decoding order)
    w.ue(MAX_REFS)                        # max_num_ref_frames
    w.raw(1, 0)
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)                           # frame_mbs_only
    w.raw(1, 1)                           # direct_8x8_inference
    w.raw(1, 0)                           # cropping
    w.raw(1, 0)                           # vui
    return nal(0x67, w.rbsp())


def pps_nal(pps_id, high):
    default_minus1, cip = PPS_TABLE[pps_id]
    w = W()
    w.ue(pps_id)
    w.ue(0)
    w.raw(1, 0)                           # CAVLC
    w.raw(1, 0)                           # bottom_field_pic_order_in_frame_present_flag
    w.ue(0)                               # one slice group
    w.ue(default_minus1)                  # num_ref_idx_l0_default_active_minus1
    w.ue(0)
    w.raw(1, 0)                           # weighted_pred_flag
    w.raw(2, 0)
    w.se(0)                               # pic_init_qp = 26
    w.se(0)
    w.se(0)                               # chroma_qp_index_offset
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, cip)                         # constrained_intra_pred_flag
    w.raw(1, 0)                           # redundant_pic_cnt_present_flag
    if high:
        w.raw(1, 1)                       # transform_8x8_mode_flag
        w.raw(1, 0)                       # pic_scaling_matrix_present_flag
        w.se(0)                           # second_chroma_qp_index_offset
    return nal(0x68, w.rbsp())


# ---------------------------------------------------------------- coefficient tokens
def token_zero(w, nc):
    """coeff_token for TotalCoeff = 0, TrailingOnes = 0 (Table 9-5)."""
    if nc == -1:
        w.raw(2, 0b01)
    elif nc < 2:
        w.raw(1, 1)
    elif nc < 4:
        w.raw(2, 0b11)
    elif nc < 8:
        w.raw(4, 0b1111)
    else:
        w.raw(6, 0b000011)


def nc_from(na, nb, aa, ab):
    if aa and ab:
        return (na + nb + 1) >> 1
    if aa:
        return na
    if ab:
        return nb
    return 0


class SliceCtx:
    """What the writer needs to know about the slice while it writes macroblocks."""

    def __init__(self, first, active, high, qp):
        self.first, self.active, self.high, self.qp = first, active, high, qp
        self.pcm = {}                      # addr -> True for I_PCM macroblocks already written in this slice

    def avail(self, addr, which):
        x = addr % W_MBS
        if which == "A":
            return x > 0 and addr - 1 >= self.first
        return addr - W_MBS >= self.first

    def neighbour_total(self, addr, which):
        """total_coeff of every block along the edge of the neighbouring macroblock: 16 if I_PCM else 0."""
        a = addr - 1 if which == "A" else addr - W_MBS
        return 16 if self.pcm.get(a) else 0

    def nc_block(self, addr, bx, by):
        """nC of a 4x4 block at block coordinates (bx, by) inside the macroblock (luma 0..3 x 0..3, chroma 0..1 x 0..1)."""
        aA, aB = self.avail(addr, "A"), self.avail(addr, "B")
        na, a_ok = (self.neighbour_total(addr, "A"), aA) if bx == 0 else (0, True)
        nb, b_ok = (self.neighbour_total(addr, "B"), aB) if by == 0 else (0, True)
        return nc_from(na, nb, a_ok, b_ok)


def luma_blocks_in_order(luma_cbp):
    """(bx, by) of the 4x4 luma blocks of the coded 8x8 quadrants, in luma4x4BlkIdx order."""
    out = []
    for q in range(4):
        if luma_cbp >> q & 1:
            for k in range(4):
                out.append(((q & 1) * 2 + (k & 1), (q >> 1) * 2 + (k >> 1)))
    return out


def write_residual_zero(w, sc, addr, luma_cbp, chroma_cbp, i16):
    """residual() with every coeff_token = TotalCoeff 0.  i16: Intra16x16 (DC block first, AC if luma_cbp = 15)."""
    if i16:
        token_zero(w, sc.nc_block(addr, 0, 0))
        if luma_cbp:
            for blk in range(16):
                token_zero(w, sc.nc_block(addr, ((blk >> 2) & 1) * 2 + (blk & 1), ((blk >> 3) & 1) * 2 + ((blk >> 1) & 1)))
    else:
        for bx, by in luma_blocks_in_order(luma_cbp):
            token_zero(w, sc.nc_block(addr, bx, by))
    if chroma_cbp:
        token_zero(w, -1)
        token_zero(w, -1)
    if chroma_cbp == 2:
        for _ in range(2):
            for blk in range(4):
                token_zero(w, sc.nc_block(addr, blk & 1, blk >> 1))


def write_te(w, v, active):
    """ref_idx_l0 as te(v) with range active - 1 (not written when active = 1)."""
    if active == 1:
        assert v == 0
    elif active == 2:
        w.raw(1, 1 - v)                   # range 1: one bit, inverted
    else:
        w.ue(v)


def write_mvd(w, mv):
    w.se(mv[0])
    w.se(mv[1])


# ---------------------------------------------------------------- macroblocks
def name_of_intra(mb):
    if mb["kind"] == "pcm":
        return "I_PCM"
    if mb["kind"] == "nxn":
        return "I_NxN"
    return f"I_16x16_2_{mb['cc']}_{mb['lf']}"


def pick_qpd(rng, qp):
    d = rng.choice([-3, -2, -1, 0, 0, 1, 2, 3])
    return d if 10 <= qp + d <= 40 else 0


def random_mv(rng, mode):
    if mode == "zero":
        return (0, 0)
    r = rng.random()
    if r < 0.4:
        return (0, 0)
    if r < 0.95:
        return (rng.randint(-12, 12), rng.randint(-12, 12))
    return (rng.randint(-300, 300), rng.randint(-300, 300))


def make_mb(rng, kind, sc, mode, n_refs_avail):
    """Random content of one macroblock of the given kind (see the table in write_mb)."""
    mb = dict(kind=kind)
    act = sc.active
    if kind in P_TYPES:
        if kind in ("p8x8", "p8x8ref0"):
            mb["subs"] = [rng.randrange(4) for _ in range(4)]
            if kind == "p8x8ref0" and rng.random() < 0.5:
                mb["subs"] = [0, 0, 0, 0]
            n = 4
            mb["mvds"] = [random_mv(rng, mode) for s in mb["subs"] for _ in range(SUB_COUNT[s])]
        else:
            n = len(PART_RECTS[kind])
            mb["mvds"] = [random_mv(rng, mode) for _ in range(n)]
        mb["refs"] = [0] * n if kind == "p8x8ref0" else [rng.randrange(act) for _ in range(n)]
        code = 0 if rng.random() < 0.4 else rng.randrange(48)
        mb["code"], mb["cbp"] = code, CBP_INTER_TAB[code]
        mb["qpd"] = pick_qpd(rng, sc.qp) if mb["cbp"] else None
        nosub = kind not in ("p8x8", "p8x8ref0") or all(s == 0 for s in mb["subs"])
        mb["t8"] = rng.randrange(2) if (sc.high and mb["cbp"] & 15 and nosub) else None
    elif kind == "pcm":
        mb["samples"] = bytes(rng.randrange(1, 256) for _ in range(384))
    elif kind == "i16":
        mb["cc"], mb["lf"] = rng.randrange(3), rng.randrange(2)
        mb["qpd"] = pick_qpd(rng, sc.qp)
    elif kind == "nxn":
        mb["t8"] = rng.randrange(2) if sc.high else 0
        code = 3 if rng.random() < 0.4 else rng.randrange(48)
        mb["code"], mb["cbp"] = code, CBP_INTRA_TAB[code]
        mb["qpd"] = pick_qpd(rng, sc.qp) if mb["cbp"] else None
    return mb


def write_mb(w, mb, addr, sc, run, mode, entries):
    """Write macroblock_layer() of a non-skipped macroblock and append its manifest entry."""
    kind = mb["kind"]
    start = w.pos()
    e = dict(addr=addr, kind=kind, bit=start, skip_run=run, sub="-", refidx="-", mvd="-", cbp="-", qp_delta="-", t8x8="-")
    if kind in P_TYPES:
        raw, name, _ = P_TYPES[kind]
        w.ue(raw)
        e.update(mb_type=raw, name=name)
        if kind in ("p8x8", "p8x8ref0"):
            for s in mb["subs"]:
                w.ue(s)
            e["sub"] = ",".join(map(str, mb["subs"]))
            if kind == "p8x8":
                for r in mb["refs"]:
                    write_te(w, r, sc.active)
            for m in mb["mvds"]:
                write_mvd(w, m)
            rects = []
            for qi, s in enumerate(mb["subs"]):
                rects.append((QUAD[qi][0], QUAD[qi][1], 8, 8, mb["refs"][qi]))
        else:
            for r in mb["refs"]:
                write_te(w, r, sc.active)
            for m in mb["mvds"]:
                write_mvd(w, m)
            rects = [(x, y, ww, hh, r) for (x, y, ww, hh), r in zip(PART_RECTS[kind], mb["refs"])]
        e["refidx"] = ",".join(map(str, mb["refs"]))
        e["mvd"] = ",".join(f"{a}:{b}" for a, b in mb["mvds"])
        w.ue(mb["code"])
        cbp = mb["cbp"]
        e["cbp"] = cbp
        e["t8x8"] = 0
        if mb["t8"] is not None:
            w.raw(1, mb["t8"])
            e["t8x8"] = mb["t8"]
        if cbp:
            w.se(mb["qpd"])
            sc.qp += mb["qpd"]
            e["qp_delta"] = mb["qpd"]
            write_residual_zero(w, sc, addr, cbp & 15, cbp >> 4, False)
        e["regions"] = rects
    elif kind == "pcm":
        w.ue(30)
        e.update(mb_type=30, name="I_PCM")
        while len(w.bits) % 8:
            w.raw(1, 0)
        for s in mb["samples"]:
            w.raw(8, s)
        e["samples"] = mb["samples"].hex()
        sc.pcm[addr] = True
    elif kind == "i16":
        raw = 5 + 1 + 2 + 4 * mb["cc"] + 12 * mb["lf"]       # Intra16x16 DC prediction
        w.ue(raw)
        e.update(mb_type=raw, name=name_of_intra(mb), cbp=(15 if mb["lf"] else 0) + 16 * mb["cc"])
        w.ue(0)                                              # intra_chroma_pred_mode DC
        w.se(mb["qpd"])
        sc.qp += mb["qpd"]
        e["qp_delta"] = mb["qpd"]
        write_residual_zero(w, sc, addr, 15 if mb["lf"] else 0, mb["cc"], True)
    else:                                                    # I_NxN, every block predicted as DC
        w.ue(5)
        e.update(mb_type=5, name="I_NxN")
        if sc.high:
            w.raw(1, mb["t8"])
        e["t8x8"] = mb["t8"]
        for _ in range(4 if mb["t8"] else 16):
            w.raw(1, 1)
        w.ue(0)
        w.ue(mb["code"])
        cbp = mb["cbp"]
        e["cbp"] = cbp
        if cbp:
            w.se(mb["qpd"])
            sc.qp += mb["qpd"]
            e["qp_delta"] = mb["qpd"]
            write_residual_zero(w, sc, addr, cbp & 15, cbp >> 4, False)
    e["qp"] = sc.qp
    entries.append(e)


def write_slice(plan, pic_index, idr, stype, mode, high, rng):
    """plan: dict(first, kinds=[kind per macroblock], pps, active, override).  -> (NAL bytes, manifest entry)."""
    w = W()
    w.ue(plan["first"])
    w.ue(stype)
    w.ue(plan["pps"])
    w.raw(4, pic_index % 16)               # frame_num
    if idr:
        w.ue(0)                            # idr_pic_id
    if not idr:
        override = plan["override"]
        w.raw(1, 1 if override else 0)     # num_ref_idx_active_override_flag
        if override:
            w.ue(plan["active"] - 1)
        w.raw(1, 0)                        # ref_pic_list_modification_flag_l0
    if idr:
        w.raw(1, 0)
        w.raw(1, 0)
    else:
        w.raw(1, 0)                        # adaptive_ref_pic_marking_mode_flag (nal_ref_idc != 0)
    qpd = plan["qpd"]
    w.se(qpd)
    w.ue(1)                                # disable_deblocking_filter_idc = 1 (pixels stay exact)
    hend = w.pos()
    sc = SliceCtx(plan["first"], plan["active"], high, 26 + qpd)
    n_ref = min(pic_index, MAX_REFS)
    entries, pend = [], 0
    for i, kind in enumerate(plan["kinds"]):
        addr = plan["first"] + i
        if kind == "skip":
            pend += 1
            entries.append(dict(addr=addr, kind="skip", name="P_Skip", mb_type="skip", bit="-", qp=sc.qp,
                                regions=[(0, 0, 16, 16, 0)]))
            continue
        if not idr:
            w.ue(pend)                     # mb_skip_run
        mb = make_mb(rng, kind, sc, mode, n_ref)
        if idr:
            raw = 25
            start = w.pos()
            w.ue(raw)
            while len(w.bits) % 8:
                w.raw(1, 0)
            for s in mb["samples"]:
                w.raw(8, s)
            entries.append(dict(addr=addr, kind="pcm", bit=start, mb_type=25, name="I_PCM", skip_run="-", sub="-",
                                refidx="-", mvd="-", cbp="-", qp_delta="-", t8x8="-", qp=sc.qp,
                                samples=mb["samples"].hex()))
            sc.pcm[addr] = True
        else:
            write_mb(w, mb, addr, sc, pend, mode, entries)
        pend = 0
    if pend:
        w.ue(pend)                         # trailing mb_skip_run: the slice ends with skipped macroblocks
    nb = nal(0x65 if idr else 0x41, w.rbsp())
    return nb, dict(first=plan["first"], header_end_bit=hend, mbs=entries, active=plan["active"], pps=plan["pps"])


def plan_picture(rng, p, kind_name):
    """Slices of picture p (p >= 1): list of plans.  kind_name: 'random' | 'allskip' | 'tailskip'."""
    n_ref = min(p, MAX_REFS)
    cuts = [0]
    if kind_name == "random":
        k = rng.choice([1, 1, 2, 3])
        cuts = sorted({0} | set(rng.sample(range(1, N_MBS), k - 1)))
    cuts.append(N_MBS)
    plans = []
    for a, b in zip(cuts, cuts[1:]):
        # a PPS whose default fits the references that exist, or any PPS with an override that does
        pps = rng.randrange(4)
        default = PPS_TABLE[pps][0] + 1
        if default > n_ref or rng.random() < 0.5:
            active = rng.randint(1, n_ref)
            override = True
        else:
            active, override = default, False
        if override and active == default and rng.random() < 0.5:
            override = False
        n = b - a
        if kind_name == "allskip":
            kinds = ["skip"] * n
        else:
            w = [("skip", 5), ("p16", 3), ("p16x8", 2), ("p8x16", 2), ("p8x8", 3), ("p8x8ref0", 1), ("pcm", 3),
                 ("i16", 1.2), ("nxn", 1.2)]
            kinds = [rng.choices([x for x, _ in w], [y for _, y in w])[0] for _ in range(n)]
            if rng.random() < 0.3:                         # long runs
                s = rng.randrange(n)
                for j in range(s, min(n, s + rng.randint(2, 8))):
                    kinds[j] = "skip"
            if kind_name == "tailskip":
                kinds[-2:] = ["skip"] * len(kinds[-2:])
        plans.append(dict(first=a, kinds=kinds, pps=pps, active=active, override=override, qpd=rng.randint(-4, 4)))
    return plans


def make_stream(mode="zero", seed=11, pictures=16):
    """-> (annex_b_bytes, manifest).  mode: 'zero' | 'mvd' | 'high'."""
    rng = random.Random(seed * 7 + {"zero": 1, "mvd": 2, "high": 3}[mode])
    high = mode == "high"
    data = sps_nal(high) + b"".join(pps_nal(i, high) for i in range(4))
    pics = []
    # picture 0: IDR, all I_PCM (a single slice per picture would hide nothing, two slices exercise first_mb)
    for p in range(pictures):
        entries = []
        if p == 0:
            plans = [dict(first=0, kinds=["pcm"] * N_MBS, pps=0, active=1, override=False, qpd=0)]
            idr = True
        else:
            kind_name = "allskip" if p == 5 else ("tailskip" if p % 4 == 0 else "random")
            plans = plan_picture(rng, p, kind_name)
            idr = False
        stype = (7 if idr else 5) if p % 2 else (2 if idr else 0)
        for pl in plans:
            nb, ent = write_slice(pl, p, idr, stype, "zero" if mode == "zero" else "mvd", high, rng)
            data += nb
            entries.append(ent)
        pics.append(dict(slices=entries))
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, mode=mode, zero_mv=(mode == "zero"), pictures=pics)
