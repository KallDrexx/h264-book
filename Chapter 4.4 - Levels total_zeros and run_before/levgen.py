"""Synthetic stream generator for the Chapter 4.4 harness: an ENCODER-side implementation of CAVLC residual coding
(independent of common/cavlc_ref.py and common/cavlc_resid.py, which are decoder-side).

The generator starts from KNOWN coefficient arrays (levels in scan order, chosen by the generator), derives
TotalCoeff, TrailingOnes, total_zeros and the run_before values from them, and writes coeff_token, the trailing-ones
signs, level_prefix / level_suffix with the adaptive suffixLength, total_zeros and run_before with a bit writer.  It
writes every coded array into a manifest.  What the manifest allows (see test_levels.py):

  * the reference parser's arrays must equal the manifest's arrays (decoder vs encoder, written separately);
  * ffmpeg must decode the stream without errors, and its PIXELS must equal an independent mini reconstruction
    (minirecon.py) of the manifest's arrays: ffmpeg's decoder, a third implementation, agrees on the VALUES.

To make a pixel comparison possible without implementing intra prediction, every macroblock is the only macroblock of its
slice: all neighbours are unavailable, Intra16x16 / chroma DC prediction is 128, and I_NxN uses Intra 4x4 DC prediction
(prev_intra4x4_pred_mode_flag = 1 everywhere: the predicted mode is DC).  nC is derived from the neighbours inside the
macroblock only.  Deblocking is off (disable_deblocking_filter_idc = 1).  Each macroblock's QP_Y is chosen as high as
possible with no sample clipped (so that small levels still change pixels) and without leaving the 16-bit range of
8.5.12 (a conforming stream must not).

COVERAGE (queues of arrays, consumed by the macroblocks in random order):
  * every (TotalCoeff, total_zeros) pair for 16-coefficient blocks, 15-coefficient blocks and chroma DC;
  * every (zerosLeft, run_before) pair of Table 9-10 (including zerosLeft > 6) via two-coefficient blocks;
  * several hundred level sequences built by a random walk that tracks suffixLength and aims at its boundaries:
    |level| = 3 << (suffixLength - 1) and one more (the increment test), levelCode at 13 / 14 / 29 / 30 for
    suffixLength 0 and at (15 << suffixLength) - 1 and 15 << suffixLength (the escape), the +2 adjustment of the first
    level after fewer than 3 trailing ones, suffixLength starting at 1 for TotalCoeff > 10 with TrailingOnes < 3,
    suffixLength growing to 6, level_prefix 14, 15 and (a few blocks) 16;
  * dedicated big-level blocks: Intra16x16 DC levels up to +-3200 and I_NxN blocks with levels > 2063 (level_prefix 16).
    (level_prefix > 15 is outside Baseline / Main / Extended; the stream is High profile, which ffmpeg decodes.)
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common.bitwriter import W, nal  # noqa: E402
from common import cavlc_tables as T  # noqa: E402
import minirecon  # noqa: E402

W_MBS, H_MBS = 4, 3
N_MBS = W_MBS * H_MBS

# Code tables used by the writer (copies, so that a self-test can corrupt them).
CT = [dict(t) for t in T.COEFF_TOKEN]
CT_DC = dict(T.COEFF_TOKEN_DC)
TZ = {k: dict(v) for k, v in T.TOTAL_ZEROS.items()}
TZ_DC = {k: dict(v) for k, v in T.TOTAL_ZEROS_DC.items()}
RB = {k: dict(v) for k, v in T.RUN_BEFORE.items()}

# A deliberately wrong writer rule for the harness self-test (None = correct).
BUG = None

_INTRA_ORDER = ("47 31 15 0 23 27 29 30 7 11 13 14 39 43 45 46 16 3 5 10 12 19 21 26 28 35 37 42 44 "
                "1 2 4 8 17 18 20 24 6 9 22 25 32 33 34 36 40 38 41").split()
CBP_TO_CODENUM = {int(v): i for i, v in enumerate(_INTRA_ORDER)}
BLK_XY = minirecon.BLK_XY


# ---------------------------------------------------------------- parameter sets
def sps_nal(log2_fn_minus4):
    w = W()
    w.raw(8, 100)                         # High profile
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    w.ue(1)                               # chroma_format_idc 4:2:0
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)
    w.raw(1, 0)                           # no scaling matrices
    w.ue(log2_fn_minus4)
    w.ue(2)                               # pic_order_cnt_type 2
    w.ue(1)
    w.raw(1, 0)
    w.ue(W_MBS - 1)
    w.ue(H_MBS - 1)
    w.raw(1, 1)                           # frame_mbs_only_flag
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x67, w.rbsp())


def pps_nal(init_qp, off_cb, off_cr):
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
    w.se(init_qp - 26)
    w.se(0)
    w.se(off_cb)                          # chroma_qp_index_offset
    w.raw(1, 1)                           # deblocking_filter_control_present_flag
    w.raw(1, 0)
    w.raw(1, 0)
    w.raw(1, 0)                           # transform_8x8_mode_flag
    w.raw(1, 0)
    w.se(off_cr)                          # second_chroma_qp_index_offset (Cr)
    return nal(0x68, w.rbsp())


# ---------------------------------------------------------------- residual block encoder
def bits(w, code):
    for ch in code:
        w.raw(1, int(ch))


def class_of(nc):
    return "dc" if nc == -1 else 0 if nc <= 1 else 1 if nc <= 3 else 2 if nc <= 7 else 3


def level_to_code(level):
    return 2 * level - 2 if level > 0 else -2 * level - 1


def code_to_level(code):
    return (code + 2) // 2 if code % 2 == 0 else -((code + 1) // 2)


def write_level(w, level, sl, first_after_t1):
    """One level_prefix / level_suffix pair (9.2.2.1 run backwards).  -> new suffixLength."""
    code = level_to_code(level)
    if first_after_t1 and BUG != "no_adjust":
        code -= 2
    assert code >= 0
    if sl == 0 and code < 14:
        prefix, suffix, size = code, 0, 0
    elif sl == 0 and code < 30:
        prefix, suffix, size = 14, code - 14, 4
    elif sl > 0 and code < (15 << sl):
        prefix, suffix, size = code >> sl, code & ((1 << sl) - 1), sl
    else:                                                    # escape: prefix 15, 16, ...
        base0 = (15 << sl) + (15 if sl == 0 else 0)
        prefix = 15
        while True:
            base = base0 + ((1 << (prefix - 3)) - 4096 if prefix >= 16 else 0)
            if base <= code < base + (1 << (prefix - 3)):
                break
            prefix += 1
            assert prefix < 26
        suffix, size = code - base, prefix - 3
    w.raw(prefix, 0)
    w.raw(1, 1)
    if size:
        w.raw(size, suffix)
    if sl == 0:
        sl = 1
    thr = 3 << (sl - 1)
    if (abs(level) >= thr if BUG == "inc_ge" else abs(level) > thr) and sl < 6:
        sl += 1
    return sl


def write_block(w, nc, maxnum, coeffs):
    """Write one residual block holding exactly `coeffs` (maxnum levels in the block's own order)."""
    assert len(coeffs) == maxnum
    nz = [k for k, v in enumerate(coeffs) if v]
    tc = len(nz)
    levels = [coeffs[p] for p in reversed(nz)]               # highest frequency first
    t1 = 0
    while t1 < min(3, tc) and abs(levels[t1]) == 1:
        t1 += 1
    cls = class_of(nc)
    bits(w, (CT_DC if cls == "dc" else CT[cls])[(tc, t1)])
    if tc == 0:
        return dict(tc=0, t1=0, tz=0)
    sl = 1 if tc > 10 and t1 < 3 else 0
    for i, lv in enumerate(levels):
        if i < t1:
            w.raw(1, 1 if lv < 0 else 0)
        else:
            sl = write_level(w, lv, sl, i == t1 and t1 < 3)
    tz = nz[-1] + 1 - tc
    if tc < maxnum:
        bits(w, (TZ_DC if maxnum == 4 else TZ)[tc][tz])
    left = tz
    for i in range(tc - 1):
        run = nz[-1 - i] - nz[-2 - i] - 1
        if left > 0:
            bits(w, RB[min(left, 7)][run])
            left -= run
    return dict(tc=tc, t1=t1, tz=tz)


# ---------------------------------------------------------------- arrays
def place(maxnum, positions, levels_bs):
    """positions ascending; levels_bs in bitstream order (highest frequency first)."""
    a = [0] * maxnum
    for p, lv in zip(reversed(positions), levels_bs):
        a[p] = lv
    return a


def rand_levels(rng, tc):
    out = []
    for i in range(tc):
        r = rng.random()
        mag = 1 if r < 0.55 else 2 if r < 0.75 else rng.randrange(3, 9) if r < 0.95 else rng.randrange(9, 40)
        out.append(mag * rng.choice((-1, 1)))
    return out


def walk_levels(rng, tc, big):
    """A level sequence (bitstream order) that aims at the suffixLength / escape boundaries (module docstring).
    The walk simulates the suffixLength of the writer so that it can aim at the thresholds."""
    lv, sl = [], None
    for i in range(tc):
        in_run = i < 3 and all(abs(x) == 1 for x in lv)          # still inside the (up to 3) trailing ones
        if in_run and rng.random() < 0.5:
            lv.append(rng.choice((-1, 1)))
            continue
        adj = 0
        if sl is None:                                           # first level after the trailing ones
            t1 = min(i, 3)
            sl = 1 if tc > 10 and t1 < 3 else 0
            adj = 2 if t1 < 3 else 0
        r = rng.random()
        if r < 0.28:                                             # increment boundary: 3 << (sl - 1) and one more
            lvl = ((3 << (max(sl, 1) - 1)) + rng.choice((0, 1))) * rng.choice((-1, 1))
        elif r < 0.45:                                           # escape boundary (levelCode as coded: + adj)
            base = (15 << sl) + (15 if sl == 0 else 0)
            cands = [base - 1, base, base + 1] + ([base + 4095, base + 4096] if big else [])
            if sl == 0:
                cands += [13, 14, 15]
            lvl = code_to_level(rng.choice(cands) + adj)
        elif r < 0.5 and big:
            lvl = code_to_level(rng.randrange((15 << sl) + 15, (15 << sl) + 8200) + adj)
        else:
            lvl = rand_levels(rng, 1)[0]
            if rng.random() < 0.15:
                lvl = rng.choice((-1, 1)) * rng.randrange(10, 60)
        if adj and abs(lvl) < 2:
            lvl = 2 if lvl > 0 else -2
        lv.append(lvl)
        if sl == 0:
            sl = 1
        if abs(lvl) > (3 << (sl - 1)) and sl < 6:
            sl += 1
    return lv


def positions_for(rng, maxnum, tc, tz=None):
    if tz is None:
        tz = rng.choice([0, 0, 1, 2, rng.randrange(maxnum - tc + 1), rng.randrange(maxnum - tc + 1)])
    tz = min(tz, maxnum - tc)
    last = tz + tc - 1
    return sorted(rng.sample(range(last), tc - 1)) + [last]


def targeted_arrays(rng, m):
    """For every (suffixLength s, level_prefix p) a block whose level sequence ramps up to suffixLength s (each ramp level is
    just above the increment threshold) and then ends with one level coded with prefix p: s = 0 with p = 0..16, s = 1..6 with
    p = 0..15.  Trailing ones (0..3) in front vary the +2 adjustment."""
    out = []
    for s in range(7):
        for p in range(17 if s == 0 else 16):
            for _ in range(2):
                t1 = rng.randrange(4)
                lv = [rng.choice((-1, 1)) for _ in range(t1)]
                cur = 0
                ramp = []
                while cur < s:
                    if cur == 0:
                        mag = rng.randrange(4, 8) if s >= 2 else (rng.choice((2, 3)) if t1 < 3 else rng.randrange(1, 4))
                        cur = 2 if s >= 2 else 1
                    else:
                        mag = (3 << (cur - 1)) + 1 + rng.randrange(3)
                        cur += 1
                    ramp.append(mag * rng.choice((-1, 1)))
                # the target level: levelCode as coded is `code`; the decoder adds 2 when it is the first level after < 3 ones
                is_first = not ramp
                a = 2 if (is_first and t1 < 3) else 0
                if s == 0:
                    code = p if p < 14 else 14 + rng.randrange(16) if p == 14 else 30 + rng.randrange(300) if p == 15 else 4126 + rng.randrange(300)
                else:
                    base = 15 << s
                    code = (p << s) + rng.randrange(1 << s) if p < 15 else base + rng.randrange(300)
                lvl = code_to_level(code + a)
                if is_first and t1 < 3 and abs(lvl) < 2:
                    continue
                lv += ramp + [lvl]
                tc = len(lv)
                if tc > m:
                    continue
                pos = sorted(rng.sample(range(m), tc))
                if p == 16:
                    pos = [rng.choice([0, 3, 5])] + sorted(rng.sample(range(6, m), tc - 1))
                out.append(place(m, pos, lv))
    return out


def build_queues(rng):
    """-> {16: [...], 15: [...], 4: [...]} lists of (arrays) that the macroblocks will consume."""
    q = {16: [], 15: [], 4: []}
    for m in (16, 15, 4):
        for tc in range(1, m + 1):                                       # (TotalCoeff, total_zeros)
            for tz in range(0, m - tc + 1):
                q[m].append(place(m, positions_for(rng, m, tc, tz), rand_levels(rng, tc)))
        for z in range(1, m - 1):                                        # (zerosLeft, run_before)
            for r in range(z + 1):
                q[m].append(place(m, [z - r, z + 1], rand_levels(rng, 2)))
        for _ in range({16: 260, 15: 130, 4: 40}[m]):                    # level sequences
            tc = rng.choice([m, m, m - 1, rng.randrange(1, m + 1), rng.randrange(1, m + 1), rng.randrange(1, min(m, 6) + 1)])
            tc = max(1, tc)
            q[m].append(place(m, positions_for(rng, m, tc), walk_levels(rng, tc, big=rng.random() < 0.2)))
    for m in (16, 15):
        q[m] += targeted_arrays(rng, m)
    # dedicated big-level blocks (level_prefix 15 and 16); positions with the smallest normAdjust (even, even)
    for _ in range(6):
        L = rng.randrange(2064, 3200) * rng.choice((-1, 1))
        q[16].append(place(16, [0], [L]))                                # Intra16x16 DC or luma DC position
        q[16].append(place(16, [rng.choice([0, 3, 5]), 11], [L, rng.choice((-1, 1)) * rng.randrange(2, 30)]))
        q[16].append(place(16, [rng.choice([0, 3, 5])], [rng.randrange(16, 2063) * rng.choice((-1, 1))]))
        q[4].append(place(4, [rng.randrange(4)], [rng.randrange(16, 1500) * rng.choice((-1, 1))]))
        q[15].append(place(15, [rng.choice([2, 4, 10])], [rng.randrange(16, 2000) * rng.choice((-1, 1))]))
    for m in q:
        rng.shuffle(q[m])
    return q


# ---------------------------------------------------------------- macroblock
def nc_inside(counts, idx_to_xy, xy_to_idx, blk):
    x, y = idx_to_xy[blk]
    na = counts[xy_to_idx[(x - 1, y)]] if x > 0 else None
    nb = counts[xy_to_idx[(x, y - 1)]] if y > 0 else None
    if na is not None and nb is not None:
        return (na + nb + 1) >> 1
    return na if na is not None else (nb if nb is not None else 0)


LUMA_XY = BLK_XY
LUMA_IDX = {xy: i for i, xy in enumerate(LUMA_XY)}
CH_XY = [(0, 0), (1, 0), (0, 1), (1, 1)]
CH_IDX = {xy: i for i, xy in enumerate(CH_XY)}


def draw_mb(rng, queues):
    """Choose the macroblock kind / CBP and take its arrays from the queues (random arrays when a queue is empty).
    -> dict(kind, cbp, cc, lf, arrays={(block kind, idx): list})."""
    def take(m):
        stash = []
        try:
            while queues[m]:
                a = queues[m].pop()
                if max(abs(v) for v in a) > 150 and mb.get("big"):         # at most one big-level block per macroblock
                    stash.append(a)
                    continue
                if max(abs(v) for v in a) > 150:
                    mb["big"] = True
                return a
        finally:
            queues[m][:0] = stash
        tc = rng.randrange(0, m + 1)
        return place(m, positions_for(rng, m, tc), rand_levels(rng, tc)) if tc else [0] * m
    kind = rng.choice(["i16", "nxn", "nxn"])
    mb = dict(kind=kind, arrays={}, big=False)
    if kind == "i16":
        cc, lf = rng.randrange(3), rng.randrange(2)
        mb.update(cc=cc, lf=lf, cbp=15 * lf + 16 * cc)
        mb["arrays"][("i16dc", 0)] = take(16)
        if lf:
            for i in range(16):
                mb["arrays"][("i16ac", i)] = take(15)
        chroma = cc
    else:
        cbp = rng.choice([15, 15, 47, 47, rng.randrange(48)])
        mb.update(cc=cbp >> 4, lf=None, cbp=cbp)
        for q in range(4):
            if cbp >> q & 1:
                for k in range(4):
                    mb["arrays"][("luma", q * 4 + k)] = take(16)
        chroma = cbp >> 4
    if chroma:
        mb["arrays"][("cbdc", 0)] = take(4)
        mb["arrays"][("crdc", 0)] = take(4)
    if chroma == 2:
        for name in ("cbac", "crac"):
            for b in range(4):
                mb["arrays"][(name, b)] = take(15)
    return mb


def clamp_levels(arrays, limit):
    for k, a in arrays.items():
        arrays[k] = [max(-limit, min(limit, v)) for v in a]


def choose_qp(rng, mb, offs):
    """QP_Y for the macroblock: among the QPs that keep the 16-bit range, those with (nearly) the most samples that are not
    clipped to 0 / 255, the highest one (so that small levels still move pixels; sometimes a random one of them).
    Levels are clamped if even QP 0 leaves the 16-bit range."""
    for limit in (None, 400, 120, 40):
        if limit:
            clamp_levels(mb["arrays"], limit)
        info = []
        for qp in range(52):
            y, cb, cr, range_ok, clipped = minirecon.reconstruct(mb["kind"], qp, mb["arrays"], offs)
            if range_ok:
                info.append((sum(1 for v in minirecon.flat_bytes(y, cb, cr) if 0 < v < 255), qp))
        if info:
            best = max(c for c, _ in info)
            good = [qp for c, qp in info if c >= 0.95 * best]
            return max(good) if rng.random() < 0.7 else rng.choice(good)
    raise AssertionError("cannot make the macroblock fit the 16-bit range")


def write_mb(w, rng, mb, qp, slice_qp):
    """macroblock_layer() of the only macroblock of a slice.  -> manifest entry."""
    e = dict(kind=mb["kind"], cbp=mb["cbp"], qp=qp, bit=w.pos())
    if mb["kind"] == "i16":
        mb_type = 1 + 2 + 4 * mb["cc"] + 12 * mb["lf"]                 # Intra16x16 prediction mode 2 (DC)
    else:
        mb_type = 0
    e["mb_type"] = mb_type
    w.ue(mb_type)
    if mb["kind"] == "nxn":
        for _ in range(16):
            w.raw(1, 1)                                                # prev_intra4x4_pred_mode_flag = 1 (predicted = DC)
    w.ue(0)                                                            # intra_chroma_pred_mode = DC
    if mb["kind"] == "nxn":
        w.ue(CBP_TO_CODENUM[mb["cbp"]])
    if mb["cbp"] > 0 or mb["kind"] == "i16":
        w.se(qp - slice_qp)
    cbp = mb["cbp"]
    luma, chroma = cbp & 15, cbp >> 4
    blocks = []
    arr = mb["arrays"]
    lc = [0] * 16
    cc = [[0] * 4, [0] * 4]

    def emit(kind, idx, nc, maxnum):
        a = arr[(kind, idx)]
        start = w.pos()
        info = write_block(w, nc, maxnum, a)
        blocks.append(dict(kind=kind, idx=idx, nc=nc, tc=info["tc"], t1=info["t1"], tz=info["tz"], maxnum=maxnum,
                           coeffs=list(a), start=start, end=w.pos()))
        return info["tc"]
    if mb["kind"] == "i16":
        emit("i16dc", 0, 0, 16)                                        # block 0's neighbours are unavailable: nC = 0
        if luma:
            for i in range(16):
                lc[i] = emit("i16ac", i, nc_inside(lc, LUMA_XY, LUMA_IDX, i), 15)
    else:
        for q in range(4):
            if luma >> q & 1:
                for k in range(4):
                    i = q * 4 + k
                    lc[i] = emit("luma", i, nc_inside(lc, LUMA_XY, LUMA_IDX, i), 16)
    if chroma:
        emit("cbdc", 0, -1, 4)
        emit("crdc", 0, -1, 4)
    if chroma == 2:
        for p, name in enumerate(("cbac", "crac")):
            for b in range(4):
                cc[p][b] = emit(name, b, nc_inside(cc[p], CH_XY, CH_IDX, b), 15)
    e["blocks"] = blocks
    return e


def write_slice(rng, addr, mb, qp, idr_pic_id, fn_bits, init_qp):
    w = W()
    slice_qp = max(0, min(51, qp - rng.choice([0, 0, 1, -1, 3, -5, 8])))
    w.ue(addr)
    w.ue(7)
    w.ue(0)
    w.raw(fn_bits, 0)
    w.ue(idr_pic_id)
    w.raw(1, 0)
    w.raw(1, 0)
    w.se(slice_qp - init_qp)
    w.ue(1)                                                            # disable_deblocking_filter_idc = 1
    hend = w.pos()
    e = write_mb(w, rng, mb, qp, slice_qp)
    return nal(0x65, w.rbsp()), dict(first=addr, header_end_bit=hend, slice_qp=slice_qp, mb=e)


def make_stream(seed=44):
    """-> (annex_b_bytes, manifest).  manifest = dict(width_mbs, height_mbs, pictures=[dict(offsets, slices=[...])])."""
    rng = random.Random(seed)
    queues = build_queues(rng)
    data, pics = b"", []
    p = 0
    while any(queues.values()) or p % 2:
        offs = (rng.randrange(-12, 13), rng.randrange(-12, 13)) if p % 3 else (0, 0)
        init_qp = rng.randrange(15, 40)
        fn = (p // 2) % 2
        data += sps_nal(fn) + pps_nal(init_qp, *offs)
        slices = []
        for addr in range(N_MBS):
            mb = draw_mb(rng, queues)
            qp = choose_qp(rng, mb, offs)
            nb, ent = write_slice(rng, addr, mb, qp, p % 2, 4 + fn, init_qp)
            data += nb
            slices.append(ent)
        pics.append(dict(offsets=list(offs), slices=slices))
        p += 1
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, pictures=pics)
