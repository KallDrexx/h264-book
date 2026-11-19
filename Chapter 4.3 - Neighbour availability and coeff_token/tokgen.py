"""Synthetic stream generator for the Chapter 4.3 harness (independent of common/cavlc_ref.py and cavlc_resid.py).

Writes a small H.264 stream (SPS, PPS, IDR pictures of I slices, CAVLC) with a bit writer and a manifest of
everything it wrote, and ffmpeg must decode the stream without errors.  What is special about it: the
generator CHOOSES every block's (TotalCoeff, TrailingOnes) and encodes the complete block (coeff_token,
trailing-ones signs, levels, total_zeros, run_before), while it derives nC from its own neighbour grid
(per-picture arrays indexed by 4x4 block position and a slice number per macroblock; no code shared with
the reference parser, which looks neighbours up in a per-slice dictionary).

  * COVERAGE slices: one macroblock per slice, so every neighbour outside the macroblock is unavailable and
    nC is fully controlled by the generator: I_NxN block 0 is a "seed" with TotalCoeff s (nC = 0), block 1 then has
    nC = s (only its left neighbour exists).  Seeds 0, 2, 4, 8 select the four nC ranges.  Every one of the
    4 x 62 coeff_token entries of Table 9-5 (nC ranges 0-1, 2-3, 4-7, 8+) is the target of block 1 of one slice;
    the 14 chroma DC entries (nC = -1) are targets of Cb / Cr DC blocks.
  * MIXED pictures: random macroblocks (I_PCM next to coded blocks, Intra16x16 with and without AC, I_NxN with every
    CBP, the 8x8 transform flag), several slices per picture (neighbours across slice edges are unavailable),
    sparse and dense coefficient counts, so that nC takes every value 0..16 with all kinds of neighbours.

To keep the syntax before the residual trivial, all intra prediction modes are DC (mode 2: the predicted mode is
always 2, so every prev_intra4x4_pred_mode_flag is 1) and intra_chroma_pred_mode is 0 (DC); both are legal anywhere.

The manifest entry of a macroblock holds the blocks in decoding order (kind, idx, nC, TotalCoeff, TrailingOnes,
start bit, end bit), the stored total_coeff of the 4x4 blocks, the QP chain and, for I_PCM, the samples.
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.bitwriter import W, nal  # noqa: E402
from common import cavlc_tables as T  # noqa: E402

W_MBS, H_MBS = 6, 5
N_MBS = W_MBS * H_MBS

# The code tables the writer uses (copies, so that a self-test can corrupt them).
CT = [dict(t) for t in T.COEFF_TOKEN]
CT_DC = dict(T.COEFF_TOKEN_DC)
TZ = {k: dict(v) for k, v in T.TOTAL_ZEROS.items()}
TZ_DC = {k: dict(v) for k, v in T.TOTAL_ZEROS_DC.items()}
RB = {k: dict(v) for k, v in T.RUN_BEFORE.items()}

# Table 9-4 (Intra column), inverse mapping coded_block_pattern -> codeNum
_INTRA_ORDER = ("47 31 15 0 23 27 29 30 7 11 13 14 39 43 45 46 16 3 5 10 12 19 21 26 28 35 37 42 44 "
                "1 2 4 8 17 18 20 24 6 9 22 25 32 33 34 36 40 38 41").split()
CBP_TO_CODENUM = {int(v): i for i, v in enumerate(_INTRA_ORDER)}


# ---------------------------------------------------------------- parameter sets (as in the other Phase 4 generators)
def sps_nal(profile, log2_fn_minus4):
    w = W()
    w.raw(8, 66 if profile == "base" else 100)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    if profile != "base":
        w.ue(1)
        w.ue(0)
        w.ue(0)
        w.raw(1, 0)
        w.raw(1, 0)
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


def pps_nal(profile, init_qp):
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
    w.se(0)
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    if profile != "base":
        w.raw(1, 1 if profile == "high1" else 0)      # transform_8x8_mode_flag
        w.raw(1, 0)
        w.se(0)
    return nal(0x68, w.rbsp())


# ---------------------------------------------------------------- residual block encoder
def class_of(nc):
    """nC -> index of the coeff_token column used: 0..3, or 'dc' for nC = -1."""
    if nc == -1:
        return "dc"
    return 0 if nc <= 1 else 1 if nc <= 3 else 2 if nc <= 7 else 3


def write_token(w, nc, tc, t1):
    cls = class_of(nc)
    code = (CT_DC if cls == "dc" else CT[cls])[(tc, t1)]
    for ch in code:
        w.raw(1, int(ch))


def write_bits(w, code):
    for ch in code:
        w.raw(1, int(ch))


def make_levels(rng, tc, t1, big):
    """Levels in bitstream order (highest frequency first): t1 trailing +-1, then others; if t1 < 3 the first
    non-trailing level has |level| > 1 (else it would have been counted as a trailing one)."""
    lv = [rng.choice((-1, 1)) for _ in range(t1)]
    for i in range(t1, tc):
        r = rng.random()
        mag = 1 + (rng.randrange(3) if r < 0.7 else rng.randrange(20) if r < 0.95 else rng.randrange(400 if big else 40))
        if i == t1 and t1 < 3 and mag == 1:
            mag = 2
        lv.append(mag * rng.choice((-1, 1)))
    return lv


def write_level(w, level, suffix_length, first_after_t1):
    """One level_prefix / level_suffix pair (9.2.2.1 run backwards).  -> new suffix_length."""
    code = 2 * level - 2 if level > 0 else -2 * level - 1
    if first_after_t1:
        code -= 2
    assert code >= 0
    sl = suffix_length
    if sl == 0 and code < 14:
        prefix, suffix, size = code, 0, 0
    elif sl == 0 and code < 30:
        prefix, suffix, size = 14, code - 14, 4
    elif sl == 0:
        prefix, suffix, size = 15, code - 30, 12
    elif code < (15 << sl):
        prefix, suffix, size = code >> sl, code & ((1 << sl) - 1), sl
    else:
        prefix, suffix, size = 15, code - (15 << sl), 12
    assert suffix < (1 << size) or size == 0, (level, sl, code)
    w.raw(prefix, 0)
    w.raw(1, 1)
    if size:
        w.raw(size, suffix)
    if sl == 0:
        sl = 1
    if abs(level) > (3 << (sl - 1)) and sl < 6:
        sl += 1
    return sl


def write_block(w, rng, nc, tc, t1, maxnum, big=False):
    """A complete residual block with the chosen TotalCoeff / TrailingOnes."""
    assert 0 <= t1 <= min(3, tc) and tc <= maxnum
    write_token(w, nc, tc, t1)
    if tc == 0:
        return
    levels = make_levels(rng, tc, t1, big)
    sl = 1 if tc > 10 and t1 < 3 else 0
    for i, lv in enumerate(levels):
        if i < t1:
            w.raw(1, 1 if lv < 0 else 0)
        else:
            sl = write_level(w, lv, sl, i == t1 and t1 < 3)
    if tc < maxnum:
        tz = rng.choice([0, 0, 1, 2, rng.randrange(maxnum - tc + 1), rng.randrange(maxnum - tc + 1)])
        tz = min(tz, maxnum - tc)
        write_bits(w, (TZ_DC if maxnum == 4 else TZ)[tc][tz])
    else:
        tz = 0
    left = tz
    for i in range(tc - 1):
        if left > 0:
            run = rng.choice([0, 0, rng.randrange(left + 1), left])
            write_bits(w, RB[min(left, 7)][run])
            left -= run


# ---------------------------------------------------------------- picture state and neighbour derivation
def blk_xy(i):
    """luma4x4BlkIdx -> (x, y) in units of 4x4 blocks (z-order of the four 8x8 quadrants, 6.4.3)."""
    return ((i >> 2) & 1) * 2 + (i & 1), ((i >> 3) & 1) * 2 + ((i >> 1) & 1)


class Pic:
    def __init__(self):
        self.tcl = [[0] * (4 * W_MBS) for _ in range(4 * H_MBS)]                       # [Y][X] luma total_coeff
        self.tcc = [[[0] * (2 * W_MBS) for _ in range(2 * H_MBS)] for _ in range(2)]   # [plane][Y][X]
        self.slice_of = [None] * N_MBS                                                 # slice number of each macroblock

    def avail(self, mb, addr):
        """Is macroblock mb (an address, possibly outside the picture) available to the macroblock addr?"""
        return 0 <= mb < N_MBS and self.slice_of[mb] is not None and self.slice_of[mb] == self.slice_of[addr]


def nc_combine(na, nb):
    if na is not None and nb is not None:
        return (na + nb + 1) >> 1
    return na if na is not None else (nb if nb is not None else 0)


def luma_nc(pic, addr, bx, by):
    mbx, mby = addr % W_MBS, addr // W_MBS
    X, Y = mbx * 4 + bx, mby * 4 + by
    left_ok = bx > 0 or (mbx > 0 and pic.avail(addr - 1, addr))
    above_ok = by > 0 or pic.avail(addr - W_MBS, addr)
    return nc_combine(pic.tcl[Y][X - 1] if left_ok else None, pic.tcl[Y - 1][X] if above_ok else None)


def chroma_nc(pic, addr, plane, bx, by):
    mbx, mby = addr % W_MBS, addr // W_MBS
    X, Y = mbx * 2 + bx, mby * 2 + by
    left_ok = bx > 0 or (mbx > 0 and pic.avail(addr - 1, addr))
    above_ok = by > 0 or pic.avail(addr - W_MBS, addr)
    t = pic.tcc[plane]
    return nc_combine(t[Y][X - 1] if left_ok else None, t[Y - 1][X] if above_ok else None)


# ---------------------------------------------------------------- macroblocks
def default_picker(rng, density):
    """-> function (kind, nc, maxnum) -> (TotalCoeff, TrailingOnes)."""
    def pick(kind, nc, maxnum):
        r = rng.random()
        if density == "sparse":
            tc = 0 if r < 0.45 else rng.choice([1, 1, 2, 3, 4]) if r < 0.9 else rng.randrange(maxnum + 1)
        elif density == "dense":
            tc = rng.randrange(max(0, maxnum - 9), maxnum + 1) if r < 0.8 else rng.randrange(maxnum + 1)
        else:
            tc = rng.randrange(maxnum + 1)
        tc = min(tc, maxnum)
        t1 = rng.randrange(min(3, tc) + 1)
        return tc, t1
    return pick


def write_mb(w, rng, pic, addr, t8_mode, qp_state, spec):
    """Write one macroblock.  spec: kind ('pcm' | 'nxn' | 'i16'), picker, and optional cbp / i16 parameters /
    t8.  Returns the manifest entry."""
    mbx, mby = addr % W_MBS, addr // W_MBS
    kind = spec["kind"]
    e = dict(addr=addr, kind=kind, bit=w.pos(), blocks=[])
    for y in range(4):
        for x in range(4):
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
        e.update(samples=s.hex(), qp=qp_state[0], cbp="-")
        e["stored"] = stored(pic, mbx, mby)
        return e
    if kind == "i16":
        cc, lf = spec.get("cc", rng.randrange(3)), spec.get("lf", rng.randrange(2))
        mb_type = 1 + 2 + 4 * cc + 12 * lf                      # Intra16x16 prediction mode 2 (DC)
        cbp = 15 * lf + 16 * cc
        e.update(mb_type=mb_type, name=f"I_16x16_2_{cc}_{lf}")
        t8 = 0
    else:
        cbp = spec["cbp"] if "cbp" in spec else rng.randrange(48)
        mb_type = 0
        e.update(mb_type=0, name="I_NxN")
        t8 = spec.get("t8", rng.randrange(2)) if t8_mode else 0
    w.ue(mb_type)
    if kind == "nxn":
        if t8_mode:
            w.raw(1, t8)
        for _ in range(4 if t8 else 16):
            w.raw(1, 1)                                         # prev_intra_pred_mode_flag = 1 (predicted mode is DC)
        e["t8"] = t8
    w.ue(0)                                                     # intra_chroma_pred_mode = DC
    if kind == "nxn":
        w.ue(CBP_TO_CODENUM[cbp])
    e["cbp"] = cbp
    if cbp > 0 or kind == "i16":
        d = spec.get("qpd", rng.choice([0, 0, 1, -1, 2, -3, 5, -7]))
        w.se(d)
        qp_state[0] = (qp_state[0] + d + 52) % 52
        e["qpd"] = d
    else:
        e["qpd"] = None
    e["qp"] = qp_state[0]
    luma, chroma = cbp & 15, cbp >> 4
    pick = spec["picker"]
    big = spec.get("big", False)

    def emit(name, idx, nc, maxnum, forced=None):
        tc, t1 = forced if forced is not None else pick(name, nc, maxnum)
        start = w.pos()
        write_block(w, rng, nc, tc, t1, maxnum, big)
        e["blocks"].append(dict(kind=name, idx=idx, nc=nc, tc=tc, t1=t1, start=start, end=w.pos()))
        return tc

    forced = spec.get("forced", {})
    if kind == "i16":
        emit("i16dc", 0, luma_nc(pic, addr, 0, 0), 16, forced.get(("i16dc", 0)))          # not stored in any 4x4 block
        if luma:
            for i in range(16):
                bx, by = blk_xy(i)
                pic.tcl[mby * 4 + by][mbx * 4 + bx] = emit("i16ac", i, luma_nc(pic, addr, bx, by), 15,
                                                           forced.get(("i16ac", i)))
    else:
        for q in range(4):
            if luma >> q & 1:
                for k in range(4):
                    i = q * 4 + k
                    bx, by = blk_xy(i)
                    pic.tcl[mby * 4 + by][mbx * 4 + bx] = emit("luma", i, luma_nc(pic, addr, bx, by), 16,
                                                               forced.get(("luma", i)))
    if chroma:
        emit("cbdc", 0, -1, 4, forced.get(("cbdc", 0)))
        emit("crdc", 0, -1, 4, forced.get(("crdc", 0)))
    if chroma == 2:
        for p, name in enumerate(("cbac", "crac")):
            for b in range(4):
                bx, by = b & 1, b >> 1
                pic.tcc[p][mby * 2 + by][mbx * 2 + bx] = emit(name, b, chroma_nc(pic, addr, p, bx, by), 15,
                                                              forced.get((name, b)))
    e["stored"] = stored(pic, mbx, mby)
    return e


def stored(pic, mbx, mby):
    """The total_coeff stored for the macroblock's 4x4 blocks: luma by luma4x4BlkIdx, Cb and Cr by chroma4x4BlkIdx."""
    luma = []
    for i in range(16):
        bx, by = blk_xy(i)
        luma.append(pic.tcl[mby * 4 + by][mbx * 4 + bx])
    ch = [[pic.tcc[p][mby * 2 + (b >> 1)][mbx * 2 + (b & 1)] for b in range(4)] for p in range(2)]
    return dict(luma=luma, cb=ch[0], cr=ch[1])


def write_slice(rng, pic, sl_no, sl, idr_pic_id, fn_bits, init_qp, t8_mode):
    w = W()
    w.ue(sl["first"])
    w.ue(7 if idr_pic_id else 2)
    w.ue(0)
    w.raw(fn_bits, 0)
    w.ue(idr_pic_id)
    w.raw(1, 0)
    w.raw(1, 0)
    w.se(sl["qp"] - init_qp)
    w.ue(1)                                # disable_deblocking_filter_idc = 1
    hend = w.pos()
    qp_state = [sl["qp"]]
    for addr in range(sl["first"], sl["last"]):
        pic.slice_of[addr] = sl_no
    entries = [write_mb(w, rng, pic, addr, t8_mode, qp_state, sl["spec"](addr)) for addr in range(sl["first"], sl["last"])]
    return nal(0x65, w.rbsp()), dict(first=sl["first"], header_end_bit=hend, slice_qp=sl["qp"], mbs=entries,
                                     t8_mode=t8_mode)


# ---------------------------------------------------------------- picture plans
SEEDS = {0: 0, 1: 2, 2: 4, 3: 8}        # seed TotalCoeff that produces nC 0, 2, 4, 8 at block 1


def all_entries(maxnum=16):
    return [(tc, t1) for tc in range(maxnum + 1) for t1 in range(min(3, tc) + 1)]


def coverage_specs(rng):
    """One spec per coverage slice (single macroblock), see the module docstring."""
    specs = []
    for cls in range(4):
        for tc, t1 in all_entries():
            seed = SEEDS[cls] + (rng.randrange(2) if cls == 0 else 0)
            seed = min(seed, 1) if cls == 0 else seed
            st1 = rng.randrange(min(3, seed) + 1)
            specs.append(dict(kind="nxn", cbp=1 + 16 * rng.choice([0, 0, 1, 2]), t8=0, qpd=0,
                              forced={("luma", 0): (seed, st1), ("luma", 1): (tc, t1)},
                              picker=default_picker(rng, "any"), cover=("luma", cls, tc, t1)))
    dc = all_entries(4)
    rng.shuffle(dc)
    for i in range(0, len(dc), 2):
        a, b = dc[i], dc[(i + 1) % len(dc)]
        specs.append(dict(kind="nxn", cbp=16 * rng.choice([1, 2]), qpd=0, t8=0,
                          forced={("cbdc", 0): a, ("crdc", 0): b}, picker=default_picker(rng, "any"),
                          cover=("dc", a, b)))
    rng.shuffle(specs)
    return specs


def plan_pictures(rng):
    """-> list of dict(profile, init_qp, slices=[dict(first, last, spec, qp)])."""
    plans = []
    cov = coverage_specs(rng)
    # coverage pictures: every macroblock is its own slice
    for i in range(0, len(cov), N_MBS):
        chunk = cov[i:i + N_MBS]
        while len(chunk) < N_MBS:
            chunk.append(dict(kind="i16", picker=default_picker(rng, "any")))
        plans.append(dict(profile="base" if (i // N_MBS) % 3 else "high1", init_qp=rng.randrange(15, 40),
                          slices=[dict(first=a, last=a + 1, spec=(lambda addr, s=s: s), qp=rng.randrange(15, 40))
                                  for a, s in enumerate(chunk)]))

    def cuts(n, k):
        pts = sorted(rng.sample(range(1, n), k - 1)) if k > 1 else []
        return list(zip([0] + pts, pts + [n]))

    def mixed(profile, nslices, weights, density, big=False, cbp=None, lf=None):
        sls = []
        for a0, a1 in cuts(N_MBS, nslices):
            pick = default_picker(rng, density)

            def spec(addr, pick=pick):
                d = dict(kind=rng.choices(["pcm", "nxn", "i16"], weights)[0], picker=pick, big=big)
                if cbp is not None:
                    d["cbp"] = cbp
                if lf is not None:
                    d["lf"] = lf
                return d
            sls.append(dict(first=a0, last=a1, spec=spec, qp=rng.randrange(10, 40)))
        plans.append(dict(profile=profile, init_qp=rng.randrange(10, 40), slices=sls))
    for profile, ns, wts, dens in [
            ("base", 1, [0, 5, 5], "any"), ("base", 3, [0, 5, 5], "any"), ("high0", 2, [0, 5, 5], "any"),
            ("high1", 1, [0, 6, 4], "any"), ("high1", 3, [0, 6, 4], "any"), ("base", 2, [2, 4, 4], "any"),
            ("high1", 2, [2, 4, 4], "any"), ("base", 1, [3, 3, 4], "sparse"), ("high1", 2, [1, 6, 3], "sparse"),
            ("base", 1, [0, 1, 0], "dense"), ("high1", 1, [0, 1, 0], "dense"), ("base", 2, [1, 4, 5], "dense"),
            ("high1", 1, [2, 4, 4], "dense"), ("base", 1, [4, 3, 3], "any"), ("base", 4, [1, 3, 3], "any"),
            ("high1", 4, [1, 3, 3], "dense")]:
        mixed(profile, ns, wts, dens, big=(dens == "dense"))
    mixed("base", 1, [0, 0, 1], "any", lf=0)          # Intra16x16 without AC: DC blocks only
    mixed("base", 1, [0, 0, 1], "dense", lf=1)        # Intra16x16 with AC everywhere
    mixed("high1", 1, [0, 1, 0], "any", cbp=47)
    mixed("base", 1, [0, 1, 0], "any", cbp=47)
    mixed("base", 1, [0, 1, 0], "any", cbp=15)        # luma only: chroma blocks absent
    mixed("base", 2, [0, 1, 0], "any", cbp=0)         # I_NxN with CBP 0: stored counts are all 0
    return plans


def make_stream(seed=43):
    """-> (annex_b_bytes, manifest).  manifest = dict(width_mbs, height_mbs, pictures=[[slice entries]])."""
    rng = random.Random(seed)
    plans = plan_pictures(rng)
    data, pics = b"", []
    for p, plan in enumerate(plans):
        fn = (p // 2) % 2
        data += sps_nal(plan["profile"], fn) + pps_nal(plan["profile"], plan["init_qp"])
        pic = Pic()
        t8_mode = 1 if plan["profile"] == "high1" else 0
        entries = []
        for k, sl in enumerate(plan["slices"]):
            nb, ent = write_slice(rng, pic, k, sl, p % 2, 4 + fn, plan["init_qp"], t8_mode)
            data += nb
            entries.append(ent)
        pics.append(entries)
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, pictures=pics)
