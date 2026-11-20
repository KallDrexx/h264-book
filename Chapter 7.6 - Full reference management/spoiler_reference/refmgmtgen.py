"""SPOILER-adjacent: synthetic stream generator for the Chapter 7.6 harness (bit writer: common/bitwriter.py).

Tiny CAVLC Baseline streams (96x80 = 6x5 macroblocks, deblocking off, picture order count type 0: output order = decoding order).  As in Chapter 7.2 every inter
macroblock is P_Skip (copy of list entry 0) or P_L0_16x16 with ref_idx r, mvd 0 and no residual, so it is an exact copy of
RefPicList0[r]; I_PCM macroblocks carry random samples unique to their picture.  The decoded pixels therefore reveal which picture
every list entry is.  What is new: the slice headers carry random but VALID ref_pic_list_modification commands (idc 0, 1, 2, also
repeated targets = the same picture at two indices), the pictures carry random valid MMCO 1-6 / IDR long_term_reference_flag, and
some scenarios skip frame_num values (gaps_in_frame_num_value_allowed_flag = 1).

The generator needs the DPB state to choose valid commands, so it drives refmgmt_ref.Dpb.  That is not circular: the harness checks the
Dpb's lists and marking against ffmpeg's decoded pixels and `-debug mmco` output, never against the generator.
Manifest: width_mbs, height_mbs, pictures: [{slices: [{first, mbs: [{addr, kind pcm|skip|p16, ref, samples?}]}], idr, ref_idc,
frame_num, gap (number of non-existing frames inserted before it)}].
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common.bitwriter import W, nal  # noqa: E402
import refmgmt_ref as R  # noqa: E402

W_MBS, H_MBS = 6, 5
N_MBS = W_MBS * H_MBS
PPS_DEFAULT_ACTIVE = [1, 2, 4]

# name -> parameters.  p_mod: probability that a P slice has modification commands; p_mmco: that a reference picture uses MMCO;
# ops: allowed MMCO operations; p_gap: probability of a frame_num gap before a reference picture; idr: picture indexes;
# idr_lt: IDR pictures with long_term_reference_flag = 1; nonref: rule.
SCENARIOS = {
    "modshort": dict(pictures=44, log2=4, refs=6, p_mod=0.85, p_mmco=0.0, ops=(), p_gap=0, idr=set(), idr_lt=set(),
                     nonref=lambda p: p % 7 == 5, slices=2, max_active=7),
    "modwrap": dict(pictures=60, log2=4, refs=10, p_mod=0.9, p_mmco=0.0, ops=(), p_gap=0, idr=set(), idr_lt=set(),
                    nonref=lambda p: False, slices=2, max_active=11),
    "lt": dict(pictures=60, log2=4, refs=5, p_mod=0.6, p_mmco=0.55, ops=(1, 2, 3, 4, 6), p_gap=0, idr=set(), idr_lt=set(),
               nonref=lambda p: p % 9 == 4, slices=2, max_active=6),
    "mmco5": dict(pictures=60, log2=4, refs=4, p_mod=0.5, p_mmco=0.6, ops=(1, 2, 3, 4, 5, 6), p_gap=0, idr=set(), idr_lt=set(),
                  nonref=lambda p: p % 8 == 3, slices=1, max_active=5, p_5=0.25),
    "gaps": dict(pictures=60, log2=4, refs=4, p_mod=0.4, p_mmco=0.15, ops=(1, 3, 4, 6), p_gap=0.25, idr=set(), idr_lt=set(),
                 nonref=lambda p: False, slices=1, max_active=6, gaps=True),
    "idrlt": dict(pictures=50, log2=5, refs=3, p_mod=0.5, p_mmco=0.35, ops=(1, 2, 3, 4, 6), p_gap=0, idr={8, 9, 21, 33, 40},
                  idr_lt={8, 21, 40}, nonref=lambda p: p % 6 == 2, slices=1, max_active=4),
    "all": dict(pictures=90, log2=4, refs=6, p_mod=0.6, p_mmco=0.4, ops=(1, 2, 3, 4, 5, 6), p_gap=0.1, idr={30, 61},
                idr_lt={30}, nonref=lambda p: p % 10 == 7, slices=2, max_active=8, gaps=True, p_5=0.08),
}


def sps_nal(log2, refs, gaps):
    w = W()
    w.raw(8, 66)
    w.raw(8, 0)
    w.raw(8, 30)
    w.ue(0)
    w.ue(log2 - 4)                        # log2_max_frame_num_minus4
    w.ue(0)                               # pic_order_cnt_type 0 (explicit lsb; see make_stream)
    w.ue(4)                               # log2_max_pic_order_cnt_lsb_minus4 -> 8 bits
    w.ue(refs)                            # max_num_ref_frames
    w.raw(1, 1 if gaps else 0)            # gaps_in_frame_num_value_allowed_flag
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
    w.raw(1, 0)
    w.raw(1, 0)
    w.ue(0)
    w.ue(PPS_DEFAULT_ACTIVE[pps_id] - 1)
    w.ue(0)
    w.raw(1, 0)
    w.raw(2, 0)
    w.se(0)
    w.se(0)
    w.se(0)
    w.raw(1, 1)
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


# ---------------------------------------------------------------- choosing valid commands
def abs_diff_for(pred, target_num, curr, maxp):
    """-> list of (idc, abs_diff_pic_num_minus1) that move picNumLXPred=pred to the PicNum target_num."""
    nw = target_num + maxp if target_num < 0 else target_num          # picNumLXNoWrap that gives PicNum target_num
    d0 = (pred - nw) % maxp or maxp
    d1 = (nw - pred) % maxp or maxp
    return [(0, d0 - 1), (1, d1 - 1)], nw


def choose_mods(d, rng, cur_fn, active):
    """Random valid modification commands for the current DPB state -> (mods list incl. final idc 3 NOT included, resulting)."""
    cands = [f for f in d.frames if not f.nonexist]
    if not cands:
        return []
    mods, pred = [], cur_fn
    for _ in range(rng.randint(1, min(active, 4))):
        if mods and rng.random() < 0.25:                    # same picture again: it will sit at two indices
            f = prev_target
        else:
            f = rng.choice(cands)
        prev_target = f
        if f.lt is not None:
            mods.append((2, f.lt))
        else:
            num = d.wrap(f, cur_fn)
            opts, nw = abs_diff_for(pred, num, cur_fn, d.max_fn)
            mods.append(rng.choice(opts))
            pred = nw
    return mods


def choose_mmcos(d, rng, cur_fn, sc, force_short):
    """Random valid MMCO list for a reference picture; None = not possible / not wanted.  `force_short` frames must be unmarked."""
    t = d.clone()
    cur = R.Frame("cur", cur_fn, -1)
    ops, used = [], set()
    ops_allowed = list(sc["ops"])

    def diff_of(f):
        return cur_fn - t.wrap(f, cur_fn) - 1

    def do(op, a):
        t.mmco_step(op, a, cur_fn, cur)
        ops.append((op, a))
        used.add(op)
    for f in force_short:
        do(1, dict(difference_of_pic_nums_minus1=diff_of(f)))
    if 5 in ops_allowed and not ops and rng.random() < sc.get("p_5", 0.15):
        do(5, {})
        return ops
    for _ in range(rng.randint(1, 3)):
        sh, lg = t.short(), t.long()
        choices = []
        if sh:
            choices += [1, 3, 3]
        if lg:
            choices += [2, 2]
        choices += [4, 6]
        op = rng.choice([c for c in choices if c in ops_allowed and c not in (5,) and not (c in (4, 6) and c in used)] or [0])
        if op == 0:
            continue
        if op in (3, 6) and (t.max_lt is None):
            if 4 in used or 4 not in ops_allowed:
                continue
            do(4, dict(max_long_term_frame_idx_plus1=rng.randint(1, min(4, t.max_refs))))
        if op == 1:
            do(1, dict(difference_of_pic_nums_minus1=diff_of(rng.choice(sh))))
        elif op == 2:
            do(2, dict(long_term_pic_num=rng.choice(lg).lt))
        elif op == 3:
            idxs = [i for i in range(t.max_lt + 1) if i != cur.lt]       # giving the current picture's own index away would unmark it
            if idxs:
                do(3, dict(difference_of_pic_nums_minus1=diff_of(rng.choice(sh)), long_term_frame_idx=rng.choice(idxs)))
        elif op == 4:
            lo = 0 if cur.lt is None else cur.lt + 1      # MMCO 4 below the index just given to the current picture would unmark it
            do(4, dict(max_long_term_frame_idx_plus1=rng.randint(lo, max(lo, min(4, t.max_refs)))))
        elif op == 6:
            do(6, dict(long_term_frame_idx=rng.randint(0, t.max_lt)))
    # make room for the current picture
    while len(t.frames) + 1 > t.max_refs:
        sh, lg = t.short(), t.long()
        if sh and (not lg or rng.random() < 0.7):
            do(1, dict(difference_of_pic_nums_minus1=diff_of(min(sh, key=lambda f: t.wrap(f, cur_fn)))))
        elif lg:
            do(2, dict(long_term_pic_num=rng.choice(lg).lt))
        else:
            break
    return ops or None


def write_marking(w, idr, long_term_flag, mmcos):
    if idr:
        w.raw(1, 0)
        w.raw(1, 1 if long_term_flag else 0)
    elif mmcos is None:
        w.raw(1, 0)
    else:
        w.raw(1, 1)
        for op, a in mmcos:
            w.ue(op)
            if op in (1, 3):
                w.ue(a["difference_of_pic_nums_minus1"])
            if op == 2:
                w.ue(a["long_term_pic_num"])
            if op in (3, 6):
                w.ue(a["long_term_frame_idx"])
            if op == 4:
                w.ue(a["max_long_term_frame_idx_plus1"])
        w.ue(0)


def slice_nal(rng, sc, poc, first, last, idr, ref_idc, fn, idr_id, pps, active, override, mods, lst, long_term_flag, mmcos):
    w = W()
    w.ue(first)
    w.ue(2 if idr else 0)
    w.ue(pps)
    w.raw(sc["log2"], fn)
    if idr:
        w.ue(idr_id)
    w.raw(8, poc)
    if not idr:
        w.raw(1, 1 if override else 0)
        if override:
            w.ue(active - 1)
        if mods:
            w.raw(1, 1)
            for idc, v in mods:
                w.ue(idc)
                w.ue(v)
            w.ue(3)
        else:
            w.raw(1, 0)
    if ref_idc:
        write_marking(w, idr, long_term_flag, mmcos)
    w.se(0)
    w.ue(1)
    valid = [i for i, e in enumerate(lst) if e is not None and not str(e[0]).startswith("g")] if lst else []
    mbs, pend = [], 0
    for addr in range(first, last):
        kinds, wts = ["pcm"], [3]
        if valid:
            kinds += ["p16"]
            wts += [5]
            if 0 in valid:
                kinds += ["skip"]
                wts += [2]
        kind = "pcm" if idr else rng.choices(kinds, wts)[0]
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
            r = rng.choice(valid)
            w.ue(0)
            write_te(w, r, active)
            w.se(0)
            w.se(0)
            w.ue(0)
            mbs.append(dict(addr=addr, kind="p16", ref=r))
    if pend:
        w.ue(pend)
    hdr = 0x65 if idr else (0x41 if ref_idc else 0x01)
    return nal(hdr, w.rbsp()), dict(first=first, mbs=mbs)


def make_stream(name, seed=7):
    sc = SCENARIOS[name]
    rng = random.Random(seed * 131 + sum(map(ord, name)))
    gaps = sc.get("gaps", False)
    data = sps_nal(sc["log2"], sc["refs"], gaps) + b"".join(pps_nal(i) for i in range(3))
    d = R.Dpb(sc["refs"], sc["log2"], gaps)
    maxfn = 1 << sc["log2"]
    pictures, idr_id, poc_n = [], 0, 0         # poc_n: pictures since the last IDR / MMCO 5 (POC = 2 * poc_n, lsb 8 bits)
    for p in range(sc["pictures"]):
        idr = p == 0 or p in sc["idr"]
        ref_idc = 0 if (not idr and sc["nonref"](p)) else rng.choice([1, 2, 3])
        if idr:
            fn = 0
            poc_n = 0
            if p and (p - 1) in sc["idr"]:
                idr_id += 1
        else:
            fn = (d.prev_ref_fn + 1) % maxfn
            if ref_idc and rng.random() < sc["p_gap"]:
                fn = (fn + rng.randint(1, 3)) % maxfn
        # frames that must leave the DPB now so that no short-term frame_num can be reached again (7.4.3)
        force = [f for f in d.short() if (d.prev_ref_fn - f.frame_num) % maxfn >= maxfn - 6] if ref_idc and not idr else []
        gap_labels = []
        t0 = d.clone()
        if not idr:
            gap_labels = d.check_frame_num(fn, idr)
        mmcos, lt_flag = None, 0
        if idr:
            lt_flag = 1 if p in sc["idr_lt"] else 0
        elif ref_idc:
            try:
                dd = d.clone()
                if not force and not (rng.random() < sc["p_mmco"] and sc["ops"]):
                    dd.finish_picture(p, fn, False, ref_idc, 0, None)           # sliding window must be possible
                else:
                    raise R.StreamError("want mmco")
            except R.StreamError:
                for _ in range(20):
                    m = choose_mmcos(d, rng, fn, sc, force) if sc["ops"] else None
                    if m is None and not sc["ops"]:
                        m = choose_mmcos(d, rng, fn, dict(sc, ops=(1, 2)), force)
                    try:
                        if m is not None:
                            d.clone().finish_picture(p, fn, False, ref_idc, 0, m)
                            mmcos = m
                            break
                    except R.StreamError:
                        pass
                else:
                    raise SystemExit(f"generator could not find valid marking for picture {p} of {name}")
        nsl = rng.randint(1, sc["slices"]) if not idr else 1
        cut = rng.randrange(1, N_MBS) if nsl == 2 else None
        bounds = [(0, N_MBS)] if nsl == 1 else [(0, cut), (cut, N_MBS)]
        pps = rng.randrange(3)
        slices = []
        for a, b in bounds:
            if idr:
                active, override, mods, lst = 1, False, [], None
            else:
                if rng.random() < 0.5:
                    active, override = PPS_DEFAULT_ACTIVE[pps], False
                else:
                    active, override = rng.randint(1, sc["max_active"]), True
                mods = choose_mods(d, rng, fn, active) if rng.random() < sc["p_mod"] else []
                lst = d.build_list(fn, active, mods)
            nb, ent = slice_nal(rng, sc, (2 * poc_n) % 256, a, b, idr, ref_idc, fn, idr_id, pps, active, override, mods, lst, lt_flag, mmcos)
            data += nb
            ent["mods"] = mods
            slices.append(ent)
        d.finish_picture(p, fn, idr, ref_idc, lt_flag, mmcos)
        own = poc_n
        poc_n += 1
        if mmcos and any(op == 5 for op, _ in mmcos):
            poc_n = 1 if own != 1 else 2      # must differ from the MMCO-5 picture's own lsb, or 7.4.1.2.4 cannot tell the pictures apart
                                    # 8.2.1: after MMCO 5 the picture's POC is 0 and the next ones count from it
        pictures.append(dict(slices=slices, idr=idr, ref_idc=ref_idc, frame_num=fn, gap=len(gap_labels)))
    return data, dict(width_mbs=W_MBS, height_mbs=H_MBS, scenario=name, pictures=pictures)
