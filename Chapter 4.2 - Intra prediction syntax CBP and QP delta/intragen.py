"""Synthetic I-slice generator for the Chapter 4.2 harness (independent of common/cavlc_ref.py).

Like Chapter 4.1's mbgen.py it writes a tiny H.264 stream (SPS, PPS, IDR pictures of I slices, CAVLC)
with a bit writer and a manifest of what it wrote, and ffmpeg must decode the stream without errors.
Compared with 4.1 it chooses, per macroblock, everything 4.2 parses:

  * I_NxN with real prev_intra4x4_pred_mode_flag / rem_intra4x4_pred_mode values (or the 4 entries of
    the 8x8 case when the PPS enables transform_8x8_mode_flag and the macroblock sets it).  The writer
    DERIVES each block's predicted mode from its neighbours (8.3.1.1), picks a wanted mode that is valid
    for the block's neighbour availability and encodes it as flag / rem, so the stream is legal;
  * intra_chroma_pred_mode, Intra16x16 modes, coded_block_pattern through the inverse of Table 9-4, and
    mb_qp_delta over its whole range -26..25 (so QP_Y wraps around 0 / 51), with the running QP_Y kept in
    the manifest;
  * residual blocks: zero coeff_tokens, plus (when asked) blocks with one coefficient (TotalCoeff = 1,
    TrailingOnes = 1) so that "the first non-zero token stops the reader" is testable.  total_coeff of
    every 4x4 block is tracked to derive nC (I_PCM counts as 16).  Blocks with a coefficient never appear
    in macroblocks with transform_size_8x8_flag = 1 (their CAVLC total_coeff bookkeeping is Chapter 4.3's).

The bit writer and NAL helpers are common/bitwriter.py (shared with Chapter 4.1's generator).
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.bitwriter import W, nal  # noqa: E402

W_MBS, H_MBS = 6, 5                      # 96 x 80 pixels
N_MBS = W_MBS * H_MBS

# Table 9-4, Intra column, ChromaArrayType 1/2, typed independently of common/cavlc_ref.py as the
# INVERSE mapping: coded_block_pattern -> codeNum, written as lists of CBP values per codeNum ranges.
_INTRA_ORDER = ("47 31 15 0 23 27 29 30 7 11 13 14 39 43 45 46 16 3 5 10 12 19 21 26 28 35 37 42 44 "
                "1 2 4 8 17 18 20 24 6 9 22 25 32 33 34 36 40 38 41").split()
CBP_TO_CODENUM = {int(v): i for i, v in enumerate(_INTRA_ORDER)}
assert len(CBP_TO_CODENUM) == 48


# ---------------------------------------------------------------- parameter sets
def sps_nal(profile, log2_fn_minus4):
    w = W()
    w.raw(8, 66 if profile == "base" else 100)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    if profile != "base":
        w.ue(1)                           # chroma_format_idc 4:2:0
        w.ue(0)
        w.ue(0)
        w.raw(1, 0)                       # qpprime_y_zero_transform_bypass_flag
        w.raw(1, 0)                       # seq_scaling_matrix_present_flag
    w.ue(log2_fn_minus4)
    w.ue(2)                               # pic_order_cnt_type 2
    w.ue(1)
    w.raw(1, 0)
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x67, w.rbsp())


def pps_nal(profile, init_qp):
    """profile: 'base' (no PPS tail), 'high0' (tail, transform_8x8_mode_flag 0), 'high1' (flag 1)."""
    w = W()
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)                           # CAVLC
    w.raw(1, 0)
    w.ue(0)
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)
    w.raw(2, 0)
    w.se(init_qp - 26)                    # pic_init_qp_minus26
    w.se(0)
    w.se(0)
    w.raw(1, 1)                           # deblocking_filter_control_present
    w.raw(1, 0)
    w.raw(1, 0)
    if profile != "base":
        w.raw(1, 1 if profile == "high1" else 0)   # transform_8x8_mode_flag
        w.raw(1, 0)                       # pic_scaling_matrix_present_flag
        w.se(0)                           # second_chroma_qp_index_offset
    return nal(0x68, w.rbsp())


# ---------------------------------------------------------------- picture state
class Pic:
    """Per-picture bookkeeping the writer needs: availability, pred modes, total_coeff."""

    def __init__(self):
        self.modes = [[None] * (4 * W_MBS) for _ in range(4 * H_MBS)]   # 4x4 pred mode per 4x4 block
        self.tcl = [[0] * (4 * W_MBS) for _ in range(4 * H_MBS)]        # luma total_coeff per 4x4 block
        self.tcc = [[[0] * (2 * W_MBS) for _ in range(2 * H_MBS)] for _ in range(2)]   # Cb, Cr per 4x4 block


def blk_xy(i):
    """luma4x4BlkIdx -> (x, y) in units of 4x4 blocks inside the macroblock (6.4.3)."""
    return ((i >> 2) & 1) * 2 + (i & 1), ((i >> 3) & 1) * 2 + ((i >> 1) & 1)


def mb_avail(addr, first):
    x = addr % W_MBS
    return dict(A=x > 0 and addr - 1 >= first, B=addr - W_MBS >= first, D=x > 0 and addr - W_MBS - 1 >= first)


def mode_valid(mode, bx, by, av):
    """Is Intra4x4/8x8 prediction mode `mode` allowed for the block at (bx, by)?  (8.3.1.2 / 8.3.2.2)"""
    left = bx > 0 or av["A"]
    top = by > 0 or av["B"]
    tl = (bx > 0 and by > 0) or (bx == 0 and by > 0 and av["A"]) or (by == 0 and bx > 0 and av["B"]) \
        or (bx == 0 and by == 0 and av["D"])
    return {0: top, 1: left, 2: True, 3: top, 4: left and top and tl, 5: left and top and tl,
            6: left and top and tl, 7: top, 8: left}[mode]


def pred_mode(pic, mbx, mby, bx, by, av):
    """predIntra4x4PredMode / predIntra8x8PredMode (8.3.1.1): min of the left and above neighbour modes,
    2 (DC) if either neighbour macroblock is unavailable.  Neighbours that are not I_NxN hold mode 2."""
    X, Y = mbx * 4 + bx, mby * 4 + by
    if (bx == 0 and not av["A"]) or (by == 0 and not av["B"]):
        return 2
    return min(pic.modes[Y][X - 1], pic.modes[Y - 1][X])


# ---------------------------------------------------------------- coeff_token and blocks
def nc_of(na, nb, aa, ab):
    if aa and ab:
        return (na + nb + 1) >> 1
    return na if aa else (nb if ab else 0)


def write_block(w, nc, nonzero, rng):
    """One residual block: a zero token, or TotalCoeff=1 / TrailingOnes=1 at the first scan position.
    The nC values reachable here are 0, 1 (below 2) and >= 8 (next to I_PCM), or -1 (chroma DC)."""
    assert nc in (-1, 0, 1) or nc >= 8, nc
    if nc == -1:
        w.raw(1, 1) if nonzero else w.raw(2, 0b01)
    elif nc < 2:
        w.raw(2, 0b01) if nonzero else w.raw(1, 1)
    else:
        w.raw(6, 0b000001) if nonzero else w.raw(6, 0b000011)
    if nonzero:
        w.raw(1, rng.randrange(2))        # trailing_ones_sign_flag
        w.raw(1, 1)                       # total_zeros = 0 (tzVlcIndex 1: '1')


def luma_nc(pic, av, mbx, mby, bx, by):
    X, Y = mbx * 4 + bx, mby * 4 + by
    aa = bx > 0 or av["A"]
    ab = by > 0 or av["B"]
    return nc_of(pic.tcl[Y][X - 1] if aa else 0, pic.tcl[Y - 1][X] if ab else 0, aa, ab)


def chroma_nc(pic, av, plane, mbx, mby, bx, by):
    X, Y = mbx * 2 + bx, mby * 2 + by
    aa = bx > 0 or av["A"]
    ab = by > 0 or av["B"]
    t = pic.tcc[plane]
    return nc_of(t[Y][X - 1] if aa else 0, t[Y - 1][X] if ab else 0, aa, ab)


# ---------------------------------------------------------------- macroblocks
def write_mb(w, rng, pic, addr, first, t8_mode, qp_state, want):
    """Choose and write one macroblock.  want: dict of generation preferences.  Returns manifest entry."""
    mbx, mby = addr % W_MBS, addr // W_MBS
    av = mb_avail(addr, first)
    kind = want["kind"]
    start = w.pos()
    e = dict(addr=addr, kind=kind, bit=start)
    # clear this macroblock's bookkeeping (non-NxN macroblocks keep mode 2)
    for y in range(4):
        for x in range(4):
            pic.modes[mby * 4 + y][mbx * 4 + x] = 2
            pic.tcl[mby * 4 + y][mbx * 4 + x] = 0
    for p in range(2):
        for y in range(2):
            for x in range(2):
                pic.tcc[p][mby * 2 + y][mbx * 2 + x] = 0
    if kind == "pcm":
        e.update(mb_type=25, name="I_PCM")
        w.ue(25)
        while len(w.bits) % 8:
            w.raw(1, 0)
        s = bytes(rng.randrange(1, 256) for _ in range(384))
        for b in s:
            w.raw(8, b)
        for y in range(4):
            for x in range(4):
                pic.tcl[mby * 4 + y][mbx * 4 + x] = 16
        for p in range(2):
            for y in range(2):
                for x in range(2):
                    pic.tcc[p][mby * 2 + y][mbx * 2 + x] = 16
        e.update(samples=s.hex(), qp=qp_state[0])
        return e
    # ---- choose syntax elements
    cm = [0] + ([1] if av["A"] else []) + ([2] if av["B"] else []) + ([3] if av["A"] and av["B"] and av["D"] else [])
    cpm = rng.choice(cm)
    if kind == "i16":
        pm = [2] + ([0] if av["B"] else []) + ([1] if av["A"] else []) + ([3] if av["A"] and av["B"] and av["D"] else [])
        pred, cc, lf = rng.choice(pm), rng.randrange(3), rng.randrange(2)
        mb_type = 1 + pred + 4 * cc + 12 * lf
        cbp = 15 * lf + 16 * cc
        e.update(mb_type=mb_type, name=f"I_16x16_{pred}_{cc}_{lf}", pred=pred, cc=cc, lf=lf)
        t8 = "-"
    else:
        mb_type = 0
        cbp = 0 if rng.random() < want.get("p_cbp0", 0.3) else rng.randrange(48)
        e.update(mb_type=0, name="I_NxN")
        t8 = rng.randrange(2) if t8_mode else 0
    w.ue(mb_type)
    # ---- transform_size_8x8_flag and mb_pred
    if kind == "nxn":
        if t8_mode:
            w.raw(1, t8)
        modes_txt = []
        step = 2 if t8 else 1
        for i in range(4 if t8 else 16):
            if t8:
                bx, by = (i & 1) * 2, (i >> 1) * 2           # top-left 4x4 of the 8x8 block, in 4x4 units
            else:
                bx, by = blk_xy(i)
            pm = pred_mode(pic, mbx, mby, bx, by, av)
            cands = [m for m in range(9) if mode_valid(m, bx, by, av)]
            actual = pm if (pm in cands and rng.random() < 0.4) else rng.choice(cands)
            if actual == pm:
                w.raw(1, 1)
                modes_txt.append(-1)
            else:
                w.raw(1, 0)
                rem = actual if actual < pm else actual - 1
                w.raw(3, rem)
                modes_txt.append(rem)
            for dy in range(step):
                for dx in range(step):
                    pic.modes[mby * 4 + by + dy][mbx * 4 + bx + dx] = actual
        e["modes"] = ",".join(str(m) for m in modes_txt)
        e["t8"] = t8
    else:
        e["modes"] = f"i16:{e['pred']}"
        e["t8"] = "-"
    w.ue(cpm)
    e["cpm"] = cpm
    # ---- coded_block_pattern
    if kind == "nxn":
        w.ue(CBP_TO_CODENUM[cbp])
    e["cbp"] = cbp
    # ---- mb_qp_delta
    if cbp > 0 or kind == "i16":
        r = rng.random()
        if want.get("qpd") is not None:
            d = want["qpd"]
        elif r < 0.25:
            d = 0
        elif r < 0.65:
            d = rng.randrange(-3, 4)
        else:
            d = rng.randrange(-26, 26)
        w.se(d)
        qp_state[0] = (qp_state[0] + d + 52) % 52
        e["qpd"] = d
    else:
        e["qpd"] = None
    e["qp"] = qp_state[0]
    # ---- residual (zero tokens, optionally blocks with one coefficient)
    pnz = want.get("p_nz", 0.0) if t8 != 1 else 0.0
    nz_any = False

    def nz():
        nonlocal nz_any
        v = rng.random() < pnz
        nz_any |= v
        return v
    luma, chroma = cbp & 15, cbp >> 4
    if kind == "i16":
        write_block(w, luma_nc(pic, av, mbx, mby, 0, 0), nz(), rng)               # Intra16x16DCLevel
        if luma:
            for i in range(16):
                bx, by = blk_xy(i)
                v = nz()
                write_block(w, luma_nc(pic, av, mbx, mby, bx, by), v, rng)
                pic.tcl[mby * 4 + by][mbx * 4 + bx] = 1 if v else 0
    else:
        for q in range(4):
            if luma >> q & 1:
                for k in range(4):
                    bx, by = blk_xy(q * 4 + k)
                    v = nz()
                    write_block(w, luma_nc(pic, av, mbx, mby, bx, by), v, rng)
                    pic.tcl[mby * 4 + by][mbx * 4 + bx] = 1 if v else 0
    if chroma:
        for _ in range(2):
            write_block(w, -1, nz(), rng)
    if chroma == 2:
        for p in range(2):
            for b in range(4):
                bx, by = b & 1, b >> 1
                v = nz()
                write_block(w, chroma_nc(pic, av, p, mbx, mby, bx, by), v, rng)
                pic.tcc[p][mby * 2 + by][mbx * 2 + bx] = 1 if v else 0
    e["nz"] = nz_any
    return e


def write_slice(rng, pic, sl, idr_pic_id, slice_type, fn_bits, init_qp, slice_qp, t8_mode):
    w = W()
    w.ue(sl["first"])
    w.ue(slice_type)
    w.ue(0)
    w.raw(fn_bits, 0)
    w.ue(idr_pic_id)
    w.raw(1, 0)
    w.raw(1, 0)
    w.se(slice_qp - init_qp)               # slice_qp_delta
    w.ue(1)                                # disable_deblocking_filter_idc = 1
    hend = w.pos()
    qp_state = [slice_qp]
    entries = []
    for addr in range(sl["first"], sl["last"]):
        entries.append(write_mb(w, rng, pic, addr, sl["first"], t8_mode, qp_state, sl["want"](addr)))
    return nal(0x65, w.rbsp()), dict(first=sl["first"], header_end_bit=hend, slice_qp=slice_qp, mbs=entries,
                                     slice_type=slice_type, t8_mode=t8_mode)


# ---------------------------------------------------------------- picture plans
def cuts(rng, n, k):
    pts = sorted(rng.sample(range(1, n), k - 1))
    return list(zip([0] + pts, pts + [n]))


def kinds_picker(rng, weights):
    names = ["pcm", "nxn", "i16"]
    return lambda addr: dict(kind=rng.choices(names, weights)[0])


def plan_pictures(rng):
    """-> list of dict(profile, init_qp, slice_qp list, slices=[dict(first,last,want)])."""
    plans = []

    def pic(profile, nslices, weights, p_nz=0.0, qp_range=(10, 40), qpd=None, p_cbp0=0.3):
        sls = []
        for a0, a1 in (cuts(rng, N_MBS, nslices) if nslices > 1 else [(0, N_MBS)]):
            def want(addr, weights=weights):
                d = dict(kind=rng.choices(["pcm", "nxn", "i16"], weights)[0], p_nz=p_nz, p_cbp0=p_cbp0)
                if qpd is not None:
                    d["qpd"] = qpd(addr)
                return d
            sls.append(dict(first=a0, last=a1, want=want, qp=rng.randrange(*qp_range)))
        plans.append(dict(profile=profile, init_qp=rng.randrange(*qp_range), slices=sls))
    # clean pictures (no I_PCM, only zero tokens): a correct 4.2 reader parses every slice to its end
    for profile in ("base", "base", "high0", "high1", "high1", "high1", "base", "high1"):
        pic(profile, rng.choice([1, 1, 2, 3]), [0, 5, 5])
    pic("base", 1, [0, 1, 0], p_cbp0=0.0)                      # I_NxN only, always CBP > 0
    pic("high1", 1, [0, 1, 0], p_cbp0=0.0)
    pic("base", 1, [0, 1, 0], p_cbp0=1.0)                      # I_NxN only, CBP 0: no mb_qp_delta at all
    pic("high1", 2, [0, 1, 0], p_cbp0=1.0)
    pic("base", 1, [0, 0, 1])                                  # Intra16x16 only
    # extreme qp: the whole delta range and the wrap at both ends
    for init in (0, 3, 48, 51):
        pic("base", 1, [0, 3, 5], qp_range=(init, init + 1), qpd=lambda a: rng.choice([-26, 25, -26, 25, 0, -1, 1]))
    pic("base", 1, [0, 3, 5], qp_range=(51, 52), qpd=lambda a: 25)     # climbs and wraps repeatedly
    pic("base", 1, [0, 3, 5], qp_range=(0, 1), qpd=lambda a: -26)
    # stop probes: a few coefficient blocks / I_PCM somewhere
    for profile in ("base", "high1", "base", "high0", "high1", "base"):
        pic(profile, rng.choice([1, 2, 4]), [0, 5, 5], p_nz=0.02)
    for profile in ("base", "high1", "base", "high1", "base", "high0"):
        pic(profile, rng.choice([1, 2, 3]), [2, 4, 4])
    pic("base", 1, [1, 0, 0])                                  # I_PCM only
    pic("high1", 3, [1, 3, 3], p_nz=0.05)
    return plans


def make_stream(seed=11):
    """-> (annex_b_bytes, manifest).  manifest = dict(width_mbs, height_mbs, pictures=[[slice entries]])."""
    rng = random.Random(seed)
    plans = plan_pictures(rng)
    data, pics = b"", []
    for p, plan in enumerate(plans):
        fn = (p // 2) % 2
        data += sps_nal(plan["profile"], fn) + pps_nal(plan["profile"], plan["init_qp"])
        pic = Pic()
        stype = 7 if p % 2 else 2
        t8_mode = 1 if plan["profile"] == "high1" else 0
        entries = []
        for sl in plan["slices"]:
            nb, ent = write_slice(rng, pic, sl, p % 2, stype, 4 + fn, plan["init_qp"], sl["qp"], t8_mode)
            data += nb
            entries.append(ent)
        pics.append(entries)
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, pictures=pics)
