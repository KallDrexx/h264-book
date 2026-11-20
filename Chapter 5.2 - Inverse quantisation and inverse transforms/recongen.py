"""Synthetic stream generator for the Chapter 5.2 ffmpeg check (an encoder-side CAVLC writer, shares no code with
the decoder-side helpers).

Every macroblock is the ONLY macroblock of its slice, so every neighbour is unavailable: Intra16x16 DC prediction and
chroma DC prediction give 128 everywhere, and I_NxN with Intra 4x4 DC prediction (prev_intra4x4_pred_mode_flag = 1
everywhere) gives 128 for block 0 and the rounded mean of reconstructed neighbours inside the macroblock after that.
Deblocking is off.  The generator picks known coefficient arrays and a QP_Y per macroblock (all of 0..51 several
times), a chroma_qp_index_offset / second_chroma_qp_index_offset pair per picture, and writes them as CAVLC.

What this can check: luma 4x4 scaling + inverse transform (I_NxN, DC not pre-scaled), Intra16x16 luma DC Hadamard +
scaling + AC blocks with DC inserted, chroma DC 2x2 + scaling + AC blocks, the chroma QP mapping with both offsets,
rounding and Clip1.  It cannot check: scaling matrices (Phase 8), 8x8 transform (Phase 8), bit depths above 8,
anything involving prediction from neighbouring macroblocks (Chapter 5.3).
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.bitwriter import W, nal  # noqa: E402
from common import cavlc_tables as T  # noqa: E402
import transref  # noqa: E402

W_MBS, H_MBS = 4, 3
N_MBS = W_MBS * H_MBS
_INTRA_ORDER = ("47 31 15 0 23 27 29 30 7 11 13 14 39 43 45 46 16 3 5 10 12 19 21 26 28 35 37 42 44 "
                "1 2 4 8 17 18 20 24 6 9 22 25 32 33 34 36 40 38 41").split()
CBP_TO_CODENUM = {int(v): i for i, v in enumerate(_INTRA_ORDER)}
LUMA_XY = transref.BLK_XY
LUMA_IDX = {xy: i for i, xy in enumerate(LUMA_XY)}
CH_XY = [(0, 0), (1, 0), (0, 1), (1, 1)]
CH_IDX = {xy: i for i, xy in enumerate(CH_XY)}


def sps_nal():
    w = W()
    w.raw(8, 100); w.raw(8, 0); w.raw(8, 30)
    w.ue(0); w.ue(1); w.ue(0); w.ue(0)    # sps id, 4:2:0, bit depths 8
    w.raw(1, 0); w.raw(1, 0)              # no transform bypass, no scaling matrices
    w.ue(0)                               # log2_max_frame_num - 4
    w.ue(2)                               # pic_order_cnt_type 2
    w.ue(1); w.raw(1, 0)                  # max_num_ref_frames, gaps
    w.ue(W_MBS - 1); w.ue(H_MBS - 1)
    w.raw(1, 1); w.raw(1, 1); w.raw(1, 0); w.raw(1, 0)   # frame_mbs_only, direct_8x8, no crop, no VUI
    return nal(0x67, w.rbsp())


def pps_nal(init_qp, off_cb, off_cr):
    w = W()
    w.ue(0); w.ue(0); w.raw(1, 0); w.raw(1, 0)          # ids, CAVLC, no bottom_field_pic_order
    w.ue(0); w.ue(0); w.ue(0); w.raw(1, 0); w.raw(2, 0)  # slice groups, ref idx defaults, weighted
    w.se(init_qp - 26); w.se(0); w.se(off_cb)
    w.raw(1, 1); w.raw(1, 0); w.raw(1, 0)                # deblocking control present, no constrained intra, no redundant
    w.raw(1, 0); w.raw(1, 0)                             # transform_8x8_mode, pic_scaling_matrix_present
    w.se(off_cr)
    return nal(0x68, w.rbsp())


# ------------------------------------------------------------------ CAVLC block writer
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


def nc_inside(counts, xy, idx, blk):
    x, y = xy[blk]
    na = counts[idx[(x - 1, y)]] if x > 0 else None
    nb = counts[idx[(x, y - 1)]] if y > 0 else None
    if na is not None and nb is not None:
        return (na + nb + 1) >> 1
    return na if na is not None else (nb if nb is not None else 0)


# ------------------------------------------------------------------ macroblock content
def rand_array(rng, n, amp, density):
    return [rng.choice((-1, 1)) * rng.randint(1, amp) if rng.random() < density else 0 for _ in range(n)]


def draw_arrays(rng, kind, cbp_or_cc, amp, density):
    """-> arrays dict, cbp-like info."""
    arr = {}
    if kind == "i16":
        cc, lf = cbp_or_cc
        arr[("i16dc", 0)] = rand_array(rng, 16, amp, max(density, 0.3))
        if lf:
            for b in range(16):
                arr[("i16ac", b)] = rand_array(rng, 15, amp, density)
        chroma = cc
    else:
        cbp = cbp_or_cc
        for q in range(4):
            if cbp >> q & 1:
                for k in range(4):
                    arr[("luma", 4 * q + k)] = rand_array(rng, 16, amp, density)
        chroma = cbp >> 4
    if chroma:
        arr[("cbdc", 0)] = rand_array(rng, 4, amp, 0.6)
        arr[("crdc", 0)] = rand_array(rng, 4, amp, 0.6)
    if chroma == 2:
        for n in ("cbac", "crac"):
            for b in range(4):
                arr[(n, b)] = rand_array(rng, 15, amp, density)
    return arr


def make_mb(rng, qp, offs, mode):
    """mode 'near': amplitude chosen so that most samples are unclipped; 'wild': any amplitude (clipping common)."""
    kind = rng.choice(["i16", "nxn", "i16"])
    if kind == "i16":
        sel = (rng.randrange(3), rng.randrange(2))
        cbp = 15 * sel[1] + 16 * sel[0]
    else:
        sel = cbp = rng.randrange(1, 48)
    density = rng.choice((0.1, 0.3, 0.7, 1.0))
    top = {"near": 400, "wild": 2000}[mode]
    amp = rng.choice((1, 2, 3, 8, 30, top)) if mode == "wild" else max(1, int(rng.uniform(0.3, 2) * 4000 / (2 ** (qp // 6) * (qp % 6 + 5) * 4)))
    while True:
        arr = draw_arrays(rng, kind, sel, amp, density)
        pix, ok = transref.reconstruct_mb(kind, qp, offs, arr)
        if ok:
            return dict(kind=kind, cbp=cbp, cc=cbp >> 4, lf=bool(cbp & 15), arrays=arr, qp=qp)
        amp = max(1, amp // 2)
        density = max(0.05, density * 0.8)


def write_mb(w, mb, slice_qp):
    kind, cbp = mb["kind"], mb["cbp"]
    if kind == "i16":
        w.ue(1 + 2 + 4 * mb["cc"] + 12 * (cbp & 15 != 0))      # Intra16x16, DC prediction (mode 2)
    else:
        w.ue(0)
        for _ in range(16):
            w.raw(1, 1)
    w.ue(0)                                                     # intra_chroma_pred_mode = DC
    if kind == "nxn":
        w.ue(CBP_TO_CODENUM[cbp])
    w.se(mb["qp"] - slice_qp)
    arr = mb["arrays"]
    z = lambda n: [0] * n  # noqa: E731
    lc, cc = [0] * 16, [[0] * 4, [0] * 4]
    luma, chroma = cbp & 15, cbp >> 4
    if kind == "i16":
        write_block(w, 0, 16, arr[("i16dc", 0)])
        if luma:
            for b in range(16):
                lc[b] = write_block(w, nc_inside(lc, LUMA_XY, LUMA_IDX, b), 15, arr[("i16ac", b)])
    else:
        for q in range(4):
            if luma >> q & 1:
                for k in range(4):
                    b = 4 * q + k
                    lc[b] = write_block(w, nc_inside(lc, LUMA_XY, LUMA_IDX, b), 16, arr[("luma", b)])
    if chroma:
        write_block(w, -1, 4, arr[("cbdc", 0)])
        write_block(w, -1, 4, arr[("crdc", 0)])
    if chroma == 2:
        for p, n in enumerate(("cbac", "crac")):
            for b in range(4):
                cc[p][b] = write_block(w, nc_inside(cc[p], CH_XY, CH_IDX, b), 15, arr[(n, b)])


def make_stream(seed=52, qp_rounds=4):
    """-> (annex_b_bytes, pictures) where pictures = list of dict(offs=(cb, cr), mbs=[mb dicts in address order])."""
    rng = random.Random(seed)
    qps = []
    for r in range(qp_rounds):
        qs = list(range(52))
        rng.shuffle(qs)
        qps += qs
    while len(qps) % N_MBS:
        qps.append(rng.randrange(52))
    data, pics = b"", []
    for p in range(len(qps) // N_MBS):
        offs = (rng.randrange(-12, 13), rng.randrange(-12, 13)) if p % 3 else (0, 0)
        init_qp = rng.randrange(15, 40)
        data += sps_nal() + pps_nal(init_qp, *offs)
        mbs = []
        for addr in range(N_MBS):
            qp = qps[p * N_MBS + addr]
            mb = make_mb(rng, qp, offs, "near" if rng.random() < 0.6 else "wild")
            mbs.append(mb)
            slice_qp = max(0, min(51, qp + rng.choice((0, 0, 1, -1, 3, -5, 8))))
            w = W()
            w.ue(addr); w.ue(7); w.ue(0)
            w.raw(4, 0)                      # frame_num
            w.ue(p % 2)                      # idr_pic_id
            w.raw(1, 0); w.raw(1, 0)         # no_output_of_prior_pics, long_term_reference_flag
            w.se(slice_qp - init_qp)
            w.ue(1)                          # disable_deblocking_filter_idc
            write_mb(w, mb, slice_qp)
            data += nal(0x65, w.rbsp())
        pics.append(dict(offs=offs, mbs=mbs))
    return data, pics
