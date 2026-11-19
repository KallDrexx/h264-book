"""Synthetic I-slice generator for the Chapter 4.1 harness (bit writer: common/bitwriter.py).

It writes a tiny H.264 stream (SPS, PPS, IDR pictures made of I slices, CAVLC) whose macroblocks
are chosen on purpose: every mb_type 0..25, I_PCM with random samples, Intra16x16 macroblocks with
all-zero residual, I_NxN macroblocks with every 4x4 block predicted as DC.  Every macroblock is
valid, so ffmpeg decodes the stream without errors, which is what makes the generator trustworthy.
A manifest records what was written (type, bit position, PCM samples ...); it is written by the
bit writer, independently of the reference parser in common/cavlc_ref.py.

Zero-residual macroblocks still contain coefficient tokens: every coded block is a coeff_token for
TotalCoeff = 0.  Its bit pattern depends on nC, which depends on the neighbours' total_coeff; the
only neighbours with non-zero counts are I_PCM macroblocks (16 per block, 9.2.1), which is why the
generator computes nC at all.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.bitwriter import W, escape, nal  # noqa: E402,F401  (shared with Chapter 4.2's generator)

W_MBS, H_MBS = 6, 5                      # 96 x 80 pixels
N_MBS = W_MBS * H_MBS


def sps_nal(log2_fn_minus4=0):
    w = W()
    w.raw(8, 66)                          # Baseline
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)                               # sps id
    w.ue(log2_fn_minus4)                  # log2_max_frame_num_minus4
    w.ue(2)                               # pic_order_cnt_type 2
    w.ue(1)                               # max_num_ref_frames
    w.raw(1, 0)
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)                           # frame_mbs_only
    w.raw(1, 1)                           # direct_8x8_inference
    w.raw(1, 0)                           # cropping
    w.raw(1, 0)                           # vui
    return nal(0x67, w.rbsp())


def pps_nal():
    w = W()
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)                           # CAVLC
    w.raw(1, 0)
    w.ue(0)                               # one slice group
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)
    w.raw(2, 0)
    w.se(0)                               # pic_init_qp = 26
    w.se(0)
    w.se(0)
    w.raw(1, 1)                           # deblocking_filter_control_present
    w.raw(1, 0)                           # constrained_intra_pred
    w.raw(1, 0)
    return nal(0x68, w.rbsp())


# ---------------------------------------------------------------- macroblock descriptions
def mb_type_of(mb):
    if mb["kind"] == "nxn":
        return 0
    if mb["kind"] == "pcm":
        return 25
    return 1 + mb["pred"] + 4 * mb["cc"] + 12 * mb["lf"]


def mb_name(mb):
    if mb["kind"] == "nxn":
        return "I_NxN"
    if mb["kind"] == "pcm":
        return "I_PCM"
    return f"I_16x16_{mb['pred']}_{mb['cc']}_{mb['lf']}"


def avail(addr, first, kind):
    """Neighbour availability inside the slice (no constrained_intra_pred, no FMO)."""
    x = addr % W_MBS
    if kind == "A":
        return x > 0 and addr - 1 >= first
    if kind == "B":
        return addr - W_MBS >= first
    if kind == "D":
        return x > 0 and addr - W_MBS - 1 >= first
    raise ValueError(kind)


def random_mb(rng, addr, first, kind=None):
    """A valid macroblock for this position (prediction modes respect neighbour availability)."""
    a, b, d = avail(addr, first, "A"), avail(addr, first, "B"), avail(addr, first, "D")
    if kind is None:
        kind = rng.choices(["pcm", "nxn", "i16"], [2, 2, 6])[0]
    mb = dict(kind=kind)
    if kind == "pcm":
        mb["samples"] = bytes(rng.randrange(1, 256) for _ in range(384))
        return mb
    cm = [0] + ([1] if a else []) + ([2] if b else []) + ([3] if a and b and d else [])
    mb["cpm"] = rng.choice(cm)                         # intra_chroma_pred_mode: 0 DC, 1 H, 2 V, 3 plane
    if kind == "i16":
        pm = [2] + ([0] if b else []) + ([1] if a else []) + ([3] if a and b and d else [])
        mb["pred"] = rng.choice(pm)                    # 0 V, 1 H, 2 DC, 3 plane
        mb["cc"] = rng.randrange(3)
        mb["lf"] = rng.randrange(2)
        mb["qpd"] = rng.randrange(-2, 3)
    return mb


def skeleton_mb(rng, addr, first):
    mb = random_mb(rng, addr, first, "i16")
    mb["cc"], mb["lf"] = 0, 0
    return mb


def typed_mb(rng, addr, first, t):
    """A macroblock with the given raw mb_type (0..25).  The caller must place Intra16x16 types
    where their prediction mode has the neighbours it needs (V: above, H: left, plane: all three)."""
    if t == 0:
        return random_mb(rng, addr, first, "nxn")
    if t == 25:
        return random_mb(rng, addr, first, "pcm")
    mb = random_mb(rng, addr, first, "i16")
    k = t - 1
    mb["pred"], mb["cc"], mb["lf"] = k % 4, (k // 4) % 3, 1 if k >= 12 else 0
    return mb


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


def write_mb(w, mb, addr, first, slice_mbs):
    """Write macroblock_layer() of one MB.  slice_mbs: dict addr -> mb for earlier MBs of the slice."""
    start = w.pos()
    kind = mb["kind"]
    w.ue(mb_type_of(mb))
    if kind == "pcm":
        while len(w.bits) % 8:
            w.raw(1, 0)
        for s in mb["samples"]:
            w.raw(8, s)
        return start
    aA, aB = avail(addr, first, "A"), avail(addr, first, "B")
    left, up = slice_mbs.get(addr - 1), slice_mbs.get(addr - W_MBS)
    pcmA = 16 if aA and left["kind"] == "pcm" else 0       # total_coeff of neighbouring blocks
    pcmB = 16 if aB and up["kind"] == "pcm" else 0
    if kind == "nxn":
        for _ in range(16):
            w.raw(1, 1)                                    # prev_intra4x4_pred_mode_flag: use the predicted mode
        w.ue(mb["cpm"])
        w.ue(3)                                            # coded_block_pattern 0 (intra column of Table 9-4)
        return start
    w.ue(mb["cpm"])
    w.se(mb["qpd"])
    # Intra16x16DCLevel: nC from luma block 0's neighbours
    token_zero(w, nc_from(pcmA, pcmB, aA, aB))
    if mb["lf"]:
        for blk in range(16):
            bx = ((blk >> 2) & 1) * 2 + (blk & 1)
            by = ((blk >> 3) & 1) * 2 + ((blk >> 1) & 1)
            na, a_ok = (pcmA, aA) if bx == 0 else (0, True)
            nb, b_ok = (pcmB, aB) if by == 0 else (0, True)
            token_zero(w, nc_from(na, nb, a_ok, b_ok))
    if mb["cc"] >= 1:
        token_zero(w, -1)                                  # Cb DC (nC = -1)
        token_zero(w, -1)                                  # Cr DC
    if mb["cc"] == 2:
        for _ in range(2):                                 # Cb then Cr AC, 4 blocks each
            for blk in range(4):
                bx, by = blk & 1, blk >> 1
                na, a_ok = (pcmA, aA) if bx == 0 else (0, True)
                nb, b_ok = (pcmB, aB) if by == 0 else (0, True)
                token_zero(w, nc_from(na, nb, a_ok, b_ok))
    return start


def write_slice(sl, idr_pic_id, slice_type, fn_bits=4):
    """sl: dict(first, mbs=[mb...]).  -> (nal bytes, manifest entry)."""
    w = W()
    w.ue(sl["first"])
    w.ue(slice_type)
    w.ue(0)                                # pps id
    w.raw(fn_bits, 0)                      # frame_num (its length changes the bit parity of slice_data)
    w.ue(idr_pic_id)
    w.raw(1, 0)                            # no_output_of_prior_pics_flag
    w.raw(1, 0)                            # long_term_reference_flag
    w.se(0)                                # slice_qp_delta
    w.ue(1)                                # disable_deblocking_filter_idc = 1 (PCM pixels stay exact)
    hend = w.pos()
    done, entries = {}, []
    for i, mb in enumerate(sl["mbs"]):
        addr = sl["first"] + i
        bit = write_mb(w, mb, addr, sl["first"], done)
        done[addr] = mb
        e = dict(addr=addr, kind=mb["kind"], mb_type=mb_type_of(mb), name=mb_name(mb), bit=bit)
        if mb["kind"] == "i16":
            e.update(pred=mb["pred"], cc=mb["cc"], lf=mb["lf"])
        if mb["kind"] == "pcm":
            e["samples"] = mb["samples"].hex()
        entries.append(e)
    return nal(0x65, w.rbsp()), dict(first=sl["first"], header_end_bit=hend, mbs=entries, slice_type=slice_type)


# ---------------------------------------------------------------- the pictures
def cuts(rng, n, k):
    pts = sorted(rng.sample(range(1, n), k - 1))
    return list(zip([0] + pts, pts + [n]))


PROBE_ADDR = W_MBS + 1                   # x = 1, y = 1: left, above and above-left all available


def picture_probe(rng, t):
    """One slice: PROBE_ADDR zero-residual Intra16x16 macroblocks, then a macroblock of raw
    mb_type t, then random valid macroblocks.  A 4.1 reader prints the probe and stops there."""
    mbs = [skeleton_mb(rng, a, 0) for a in range(PROBE_ADDR)]
    mbs.append(typed_mb(rng, PROBE_ADDR, 0, t))
    mbs += [random_mb(rng, a, 0) for a in range(PROBE_ADDR + 1, N_MBS)]
    return [dict(first=0, mbs=mbs)]


def picture_skeleton(rng):
    return [dict(first=0, mbs=[skeleton_mb(rng, a, 0) for a in range(N_MBS)])]


def picture_skel_then_pcm(rng):
    sls = []
    for a0, a1 in cuts(rng, N_MBS, 4):
        k = rng.randrange(0, a1 - a0 + 1)
        mbs = [skeleton_mb(rng, a, a0) for a in range(a0, a0 + k)] + \
              [random_mb(rng, a, a0, "pcm") for a in range(a0 + k, a1)]
        sls.append(dict(first=a0, mbs=mbs))
    return sls


def picture_pcm_then_skeleton(rng):
    """Skeletons, one I_PCM at the end of the first row, then skeletons.  The macroblock after the
    I_PCM has no I_PCM neighbour (left unavailable, above is a skeleton), so its coeff_token really
    is '1': only the 4.1 rule "after an I_PCM, any non-PCM macroblock stops the slice" stops there."""
    mbs = [skeleton_mb(rng, a, 0) for a in range(W_MBS - 1)] + [random_mb(rng, W_MBS - 1, 0, "pcm")]
    mbs += [skeleton_mb(rng, a, 0) for a in range(W_MBS, N_MBS)]
    return [dict(first=0, mbs=mbs)]


def picture_random(rng, nslices):
    return [dict(first=a0, mbs=[random_mb(rng, a, a0) for a in range(a0, a1)]) for a0, a1 in cuts(rng, N_MBS, nslices)]


def picture_all_pcm(rng):
    return [dict(first=0, mbs=[random_mb(rng, a, 0, "pcm") for a in range(N_MBS)])]


def make_stream(seed=7):
    """-> (annex_b_bytes, manifest) ; manifest = dict(width_mbs, height_mbs, pictures=[[slice entries]])."""
    rng = random.Random(seed)
    plans = [picture_probe(rng, t) for t in range(26)]
    plans += [picture_skeleton(rng)] + [picture_skel_then_pcm(rng) for _ in range(8)]
    plans += [
             picture_random(rng, 1), picture_random(rng, 3), picture_random(rng, 5), picture_all_pcm(rng),
             picture_random(rng, 2), picture_pcm_then_skeleton(rng)]
    data = b""
    pics = []
    for p, sls in enumerate(plans):
        fn = (p // 2) % 2                              # frame_num length 4 or 5 bits: shifts every later bit position by one,
        data += sps_nal(fn) + pps_nal()                # so I_PCM alignment takes all values 0..7 (SPS/PPS may change at each IDR)
        stype = 7 if p % 2 else 2                      # alternate slice_type 2 and 7 (all slices of a picture I)
        entries = []
        for sl in sls:
            nb, ent = write_slice(sl, p % 2, stype, 4 + fn)
            data += nb
            entries.append(ent)
        pics.append(entries)
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, pictures=pics)
