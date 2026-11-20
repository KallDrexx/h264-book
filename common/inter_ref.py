"""Independent reference parser for CAVLC P slices (Phase 7 harness truth; extends cavlc_ref.py, does not modify it).

Stdlib only.  Chapter 7.1 introduces this module; chapters 7.3 (motion vector prediction), 7.8 (B slices) and 8.x extend it
the same way 4.2-4.4 extended cavlc_ref.py: they add a LEVEL, new keys for that level, and never change what an earlier
level prints.  Slice HEADER facts come from ffmpeg's trace_headers (cavlc_ref.load_stream); this module adds the two
slice-header facts a P slice needs (num_ref_idx_l0_active, constrained_intra_pred) and re-decides which slices it parses.

LEVELS.   INTER_LEVEL = 1 (Chapter 7.1): complete syntax parsing of I and P slices, CAVLC, 8-bit 4:2:0, frames, no FMO/ASO:
  mb_skip_run, mb_type (Table 7-13, intra types at offset 5), sub_mb_type (7-17), ref_idx_l0 te(v), mvd_l0, coded_block_pattern
  (Table 9-4 inter column), transform_size_8x8_flag rules for inter macroblocks, mb_qp_delta, the residual (nC from
  neighbours: skipped / inter / intra / I_PCM), end-of-slice detection (more_rbsp_data, trailing skip run).
  Not parsed (slice status "skipped"): B slices (at levels below 3; Chapter 7.8 parses them), SP/SI, CABAC (8.x), field / MBAFF pictures.
  Extension points for later chapters: every macroblock record has rec.parts = [dict(x, y, w, h, ref, mvd)] in SYNTAX order
  (luma sample units inside the macroblock; ref = ref_idx_l0, mvd = (x, y) in quarter samples; P_Skip has parts = [] and
  rec.f["name"] == "P_Skip"); ctx["recs"] maps macroblock address -> record for neighbour lookups; render_dump(results, level)
  prints keys_mb(level), so a new level adds EXTRA_KEYS_BY_LEVEL[level], fills those rec.f keys, and its harness passes
  level (and compares keys_mb(level)); level 1 output, and so Chapter 7.1's expected dump, stays unchanged.

DUMP FORMAT (superset of the Phase 4 format; see Chapter 7.1's Readme, section 5):
    SLICE <i> type=<I|P|B|SP|SI> first_mb=<n> pic_size_in_mbs=<n> header_end_bit=<n>
    MB <i> <addr> mb_type=.. name=.. i16_mode=.. chroma_cbp=.. luma_cbp=.. bit=.. t8x8=.. modes=.. chroma_pred=.. cbp=..
       qp_delta=.. qp=.. nc=.. tc=.. tcl=.. tccb=.. tccr=.. coef=.. skip_run=.. sub=.. refidx=.. mvd=..
    PCM <i> <addr> align_bits=.. sum_y=.. sum_cb=.. sum_cr=..
    END <i> mbs=<n> status=<complete|stopped|skipped|error> end_bit=<n or ->
    HIST <name>:<count> ...            (macroblock names, alphabetical, skipped macroblocks counted as P_Skip)

LEVEL 3 (Chapter 7.8): B slices.  prepare(infos, level=3) / load(path, workdir, level=3) stop skipping B slices (levels 1 and 2 are
  exactly as before).  Syntax: mb_skip_run in B slices (B_Skip), mb_type of Table 7-14 (intra types at offset 23), sub_mb_type of
  Table 7-18, then in this order ref_idx_l0 (all partitions that use list 0), ref_idx_l1, mvd_l0, mvd_l1, with te(v) ranges taken from
  num_ref_idx_l0_active / num_ref_idx_l1_active.  Direct macroblocks and sub-macroblocks are PARSED (they carry no ref_idx / mvd) but
  their vectors are Chapter 7.9's business.  The level-3 MB line is the level-1 line plus the keys refidx1 and mvd1 (the level-2 keys
  mvp / mv belong to 7.3's P-only reference and are not part of level 3):
      refidx   ref_idx_l0, one entry per partition (sub-macroblock) that uses list 0, syntax order; entries are 0 where not coded
      refidx1  the same for list 1
      mvd      mvd_l0, one x:y per prediction block (sub-partition) that uses list 0
      mvd1     the same for list 1                ('-' = the macroblock has no entry of that kind: intra, B_Skip, B_Direct_16x16,
                                                  or no partition uses that list)
      sub      the four sub_mb_type values of a B_8x8 macroblock (Table 7-18)
  rec.parts of a B macroblock = [dict(x, y, w, h, mode, ref, ref1, mvd, mvd1)] in prediction-block order (mode 'L0' | 'L1' | 'Bi' |
  'Direct'; ref / mvd belong to list 0, ref1 / mvd1 to list 1, None where the list is not used).

MUTANT: switch used by harness self-tests to run a deliberately wrong parser; None = the correct rules.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import cavlc_ref as ref, cavlc_resid, ffref  # noqa: E402
from common.cavlc_ref import ParseError, BitReader, MbRecord  # noqa: E402

INTER_LEVEL = 1
MUTANT = None

P_KEYS = ["skip_run", "sub", "refidx", "mvd"]
MV_KEYS = ["mvp", "mv"]                 # level 2 (Chapter 7.3): filled by that chapter's spoiler_reference/mvpred_ref.py, not here
B_KEYS = ["refidx1", "mvd1"]            # level 3 (Chapter 7.8)
EXTRA_KEYS_BY_LEVEL = {1: P_KEYS, 2: MV_KEYS, 3: B_KEYS}
LEVEL_CHAIN = {1: (1,), 2: (1, 2), 3: (1, 3)}      # level 3 extends level 1 (the dump of 7.1), not level 2 (7.3's P-only vector keys)
def keys_mb(level=1):
    """MB-line keys printed at a level: the Phase 4 keys, then this module's keys of the level's chain."""
    return (MbRecord.CORE_KEYS + ref.EXTRA_KEYS_BY_LEVEL[2] + ref.EXTRA_KEYS_BY_LEVEL[3] + ref.EXTRA_KEYS_BY_LEVEL[4]
            + [k for lv in LEVEL_CHAIN.get(level, (1,)) for k in EXTRA_KEYS_BY_LEVEL[lv]])


KEYS_MB = keys_mb(1)                    # Chapter 7.1's key list (later levels never change it)

# Table 7-13 (P slices): raw mb_type -> (name, partition rectangles (x, y, w, h) in luma samples)
MB_TYPE_P = {
    0: ("P_L0_16x16", [(0, 0, 16, 16)]),
    1: ("P_L0_L0_16x8", [(0, 0, 16, 8), (0, 8, 16, 8)]),
    2: ("P_L0_L0_8x16", [(0, 0, 8, 16), (8, 0, 8, 16)]),
    3: ("P_8x8", None),
    4: ("P_8x8ref0", None),
}
# Table 7-17: sub_mb_type -> (name, sub-partition rectangles (x, y, w, h) relative to the 8x8 block)
SUB_MB_P = {
    0: ("P_L0_8x8", [(0, 0, 8, 8)]),
    1: ("P_L0_8x4", [(0, 0, 8, 4), (0, 4, 8, 4)]),
    2: ("P_L0_4x8", [(0, 0, 4, 8), (4, 0, 4, 8)]),
    3: ("P_L0_4x4", [(0, 0, 4, 4), (4, 0, 4, 4), (0, 4, 4, 4), (4, 4, 4, 4)]),
}
# Table 7-14 (B slices), raw mb_type 1..22 -> (name, partition rectangles, prediction mode per partition); 0 = B_Direct_16x16,
# 23.. = intra (raw - 23).  Table 7-18: sub_mb_type -> (name, sub-partition rectangles, mode); 0 = B_Direct_8x8.
def _build_b_tables():
    mb = {0: ("B_Direct_16x16", None, None)}
    for k, m in enumerate(("L0", "L1", "Bi")):
        mb[1 + k] = (f"B_{m}_16x16", [(0, 0, 16, 16)], (m,))
    pairs = [("L0", "L0"), ("L1", "L1"), ("L0", "L1"), ("L1", "L0"), ("L0", "Bi"), ("L1", "Bi"), ("Bi", "L0"), ("Bi", "L1"),
             ("Bi", "Bi")]
    for k, (a, b) in enumerate(pairs):
        mb[4 + 2 * k] = (f"B_{a}_{b}_16x8", [(0, 0, 16, 8), (0, 8, 16, 8)], (a, b))
        mb[5 + 2 * k] = (f"B_{a}_{b}_8x16", [(0, 0, 8, 16), (8, 0, 8, 16)], (a, b))
    mb[22] = ("B_8x8", None, None)
    sub = {0: ("B_Direct_8x8", None, "Direct")}
    r88, r84, r48 = [(0, 0, 8, 8)], [(0, 0, 8, 4), (0, 4, 8, 4)], [(0, 0, 4, 8), (4, 0, 4, 8)]
    r44 = [(0, 0, 4, 4), (4, 0, 4, 4), (0, 4, 4, 4), (4, 4, 4, 4)]
    for raw, (m, shape, rs) in {1: ("L0", "8x8", r88), 2: ("L1", "8x8", r88), 3: ("Bi", "8x8", r88), 4: ("L0", "8x4", r84),
                                5: ("L0", "4x8", r48), 6: ("L1", "8x4", r84), 7: ("L1", "4x8", r48), 8: ("Bi", "8x4", r84),
                                9: ("Bi", "4x8", r48), 10: ("L0", "4x4", r44), 11: ("L1", "4x4", r44),
                                12: ("Bi", "4x4", r44)}.items():
        sub[raw] = (f"B_{m}_{shape}", rs, m)
    return mb, sub


MB_TYPE_B, SUB_MB_B = _build_b_tables()
SHAPE_CHAR = {"P_L0_16x16": " ", "P_L0_L0_16x8": "-", "P_L0_L0_8x16": "|", "P_8x8": "+", "P_8x8ref0": "+"}


# ================================================================ slice facts
def prepare(infos, level=1):
    """Add the P-slice facts to the SliceInfo list of cavlc_ref.load_stream and decide which slices are parsed here
    (B slices only from level 3 on)."""
    for s in infos:
        h, p = s.hdr, s.pps
        if h.get("num_ref_idx_active_override_flag", 0):
            s.num_ref_idx_l0_active = h["num_ref_idx_l0_active_minus1"] + 1
        else:
            s.num_ref_idx_l0_active = p.get("num_ref_idx_l0_default_active_minus1", 0) + 1
        if h.get("num_ref_idx_active_override_flag", 0) and s.slice_type == 1:
            s.num_ref_idx_l1_active = h["num_ref_idx_l1_active_minus1"] + 1
        else:
            s.num_ref_idx_l1_active = p.get("num_ref_idx_l1_default_active_minus1", 0) + 1
        s.direct_8x8_inference = s.sps.get("direct_8x8_inference_flag", 0)
        s.constrained_intra_pred = p.get("constrained_intra_pred_flag", 0)
        field = h.get("field_pic_flag", 0)
        mbaff = s.sps.get("mb_adaptive_frame_field_flag", 0) and not field
        sps = s.sps
        if s.slice_type not in (0, 2) and not (s.slice_type == 1 and level >= 3):
            s.skip_reason = {1: "B slice (Chapter 7.8)", 3: "SP slice", 4: "SI slice"}[s.slice_type]
        elif s.entropy:
            s.skip_reason = "CABAC slice"
        elif field or mbaff:
            s.skip_reason = "field picture or MBAFF"
        elif (sps.get("chroma_format_idc", 1) != 1 or sps.get("bit_depth_luma_minus8", 0)
              or sps.get("bit_depth_chroma_minus8", 0) or sps.get("separate_colour_plane_flag", 0)):
            s.skip_reason = "not 8-bit 4:2:0"
        else:
            s.skip_reason = None
    return infos


def load(path, workdir, level=1):
    return prepare(ref.load_stream(path, workdir), level)


# ================================================================ syntax elements
def read_te(br, rng):
    """te(v) with range rng = num_ref_idx_active - 1 >= 1 (9.1): range 1 is one inverted bit, else ue(v)."""
    if MUTANT == "te_range1_ue" and rng == 1:
        return br.ue()
    if rng == 1:
        b = br.u(1)
        return b if MUTANT == "te_no_invert" else 1 - b
    return br.ue()


def rects_of_inter(raw, subs):
    """Prediction blocks of an inter macroblock in syntax order: [(x, y, w, h, sub_mb_index or None)]."""
    if raw < 3:
        return [(*r, None) for r in MB_TYPE_P[raw][1]]
    out = []
    for i, s in enumerate(subs):
        ox, oy = (i & 1) * 8, (i >> 1) * 8
        rs = SUB_MB_P[s][1]
        if MUTANT == "sub_4x4_two" and s == 3:
            rs = rs[:2]
        out += [(ox + x, oy + y, w, h, i) for x, y, w, h in rs]
    return out


def read_inter_pred(br, info, rec, raw):
    """mb_pred() / sub_mb_pred() of a P macroblock (7.3.5.1 / 7.3.5.2): all ref_idx_l0, then all mvd_l0.
    Returns noSubMbPartSizeLessThan8x8Flag.  Fills rec.parts and the sub / refidx / mvd keys."""
    f = rec.f
    nref = info.num_ref_idx_l0_active
    rng = nref - 1
    subs = None
    if raw >= 3:
        subs = [br.ue() for _ in range(4)]
        for s in subs:
            if s > 3:
                raise ParseError(f"sub_mb_type {s} is not valid in a P macroblock (0..3)")
        nparts = 4
    else:
        nparts = len(MB_TYPE_P[raw][1])
    rects = rects_of_inter(raw, subs)
    reads_ref = (rng > 0 or MUTANT == "ref_when_one") and not (raw == 4 and MUTANT != "ref0_reads_ref")

    def one_ref():
        if MUTANT == "ref_when_one" and rng == 0:
            v = br.ue()
        else:
            v = read_te(br, rng) if rng > 0 else 0
        if v > rng:
            raise ParseError(f"ref_idx_l0 {v} is larger than num_ref_idx_l0_active_minus1 {rng}")
        return v
    refs = [0] * nparts
    mvds = []
    if MUTANT == "mvd_interleaved" and raw < 3:
        for i in range(nparts):                       # wrong: ref_idx and mvd of partition i before partition i + 1
            if reads_ref:
                refs[i] = one_ref()
            mvds.append(read_mvd(br))
    else:
        if reads_ref:
            for i in range(nparts):
                refs[i] = one_ref()
        for _ in rects:
            mvds.append(read_mvd(br))
    parts = []
    for k, ((x, y, w, h, sm), mv) in enumerate(zip(rects, mvds)):
        parts.append(dict(x=x, y=y, w=w, h=h, ref=refs[k if sm is None else sm], mvd=mv))
    rec.parts = parts
    f["sub"] = ",".join(map(str, subs)) if subs else "-"
    f["refidx"] = ",".join(map(str, refs))
    f["mvd"] = ",".join(f"{a}:{b}" for a, b in mvds)
    return subs is None or all(s == 0 for s in subs)


def read_mvd(br):
    if MUTANT == "mvd_yx":
        y = br.se()
        x = br.se()
        return x, y
    x = br.se()
    y = br.se()
    return x, y


def read_inter_macroblock(br, info, rec, raw, ctx):
    """Everything after mb_type of a P macroblock (raw 0..4)."""
    f = rec.f
    name = MB_TYPE_P[raw][0]
    f.update(mb_type=raw, name=name, i16_mode="-", chroma_cbp="-", luma_cbp="-", modes="-", chroma_pred="-")
    no_sub_lt8 = read_inter_pred(br, info, rec, raw)
    code = br.ue()
    if code > 47:
        raise ParseError(f"coded_block_pattern codeNum {code} > 47")
    cbp = (ref.CBP_INTRA if MUTANT == "cbp_intra_col" else ref.CBP_INTER)[code]
    f["cbp"] = cbp
    t8 = 0
    if MUTANT == "t8x8_inter_never":
        pass
    elif (cbp & 15) > 0 and info.transform_8x8_mode and (no_sub_lt8 or MUTANT == "t8x8_ignores_sub"):
        t8 = br.u(1)
    f["t8x8"] = t8
    if cbp > 0 or MUTANT == "qpd_always":
        d = br.se()
        if not -26 <= d <= 25:
            raise ParseError(f"mb_qp_delta {d} outside -26..25")
        f["qp_delta"] = d
        ctx["qp"] = (ctx["qp"] + d + 52) % 52
    else:
        f["qp_delta"] = "-"
    f["qp"] = ctx["qp"]
    ctx["res3"].macroblock(br, rec, "inter", cbp)


def read_b_pred(br, info, rec, raw):
    """mb_pred() / sub_mb_pred() of a B macroblock (7.3.5.1 / 7.3.5.2), not direct 16x16: sub_mb_type x4 (B_8x8), then ref_idx_l0
    of every partition that uses list 0, ref_idx_l1 of every partition that uses list 1, mvd_l0 of every prediction block that uses
    list 0, mvd_l1 likewise.  Returns noSubMbPartSizeLessThan8x8Flag.  Fills rec.parts and the sub / refidx / mvd / refidx1 / mvd1 keys."""
    f = rec.f
    n0, n1 = info.num_ref_idx_l0_active, info.num_ref_idx_l1_active
    sub_raw = None
    if raw == 22:
        sub_raw = [br.ue() for _ in range(4)]
        for s_ in sub_raw:
            if s_ > 12:
                raise ParseError(f"sub_mb_type {s_} is not valid in a B macroblock (0..12)")
        if MUTANT == "b_sub_table":                 # wrong: the 8x4 / 4x8 rows of Table 7-18 swapped
            sub_raw = [{4: 5, 5: 4, 6: 7, 7: 6, 8: 9, 9: 8}.get(v, v) for v in sub_raw]
        units = []                                   # one unit per partition / sub-macroblock: (mode, [rects in the macroblock])
        for i, sr in enumerate(sub_raw):
            ox, oy = (i & 1) * 8, (i >> 1) * 8
            nm, rs, mode = SUB_MB_B[sr]
            if rs is None:                           # B_Direct_8x8: no syntax here, 7.9 derives it
                units.append(("Direct", [(ox, oy, 8, 8)]))
            else:
                units.append((mode, [(ox + x, oy + y, w, h) for x, y, w, h in rs]))
    else:
        nm, rs, modes = MB_TYPE_B[raw]
        units = [(m, [r]) for m, r in zip(modes, rs)]

    def uses(mode, lst):
        if mode == "Direct":
            return False
        if MUTANT == "b_bi_is_l0" and mode == "Bi":
            return lst == 0
        return mode in ("Bi", "L0" if lst == 0 else "L1")

    def one_ref(lst):
        rng = (n1 if lst == 1 and MUTANT != "b_te_range_l0" else n0) - 1
        if rng <= 0:
            return 0
        v = read_te(br, rng)
        if v > rng:
            raise ParseError(f"ref_idx_l{lst} {v} is larger than num_ref_idx_l{lst}_active_minus1 {rng}")
        return v
    refs = [[None] * len(units), [None] * len(units)]
    mvds = [[None] * sum(len(u[1]) for u in units), [None] * sum(len(u[1]) for u in units)]
    first_block = [sum(len(u[1]) for u in units[:k]) for k in range(len(units))]
    order = (1, 0) if MUTANT == "b_list_order" else (0, 1)
    if MUTANT == "b_mvd_interleaved":
        for lst in order:
            for k, (mode, rs) in enumerate(units):
                if uses(mode, lst):
                    refs[lst][k] = one_ref(lst)
        for k, (mode, rs) in enumerate(units):
            for lst in (0, 1):
                if uses(mode, lst):
                    for j in range(len(rs)):
                        mvds[lst][first_block[k] + j] = read_mvd(br)
    else:
        for lst in order:
            for k, (mode, rs) in enumerate(units):
                if uses(mode, lst):
                    refs[lst][k] = one_ref(lst)
        for lst in order:
            for k, (mode, rs) in enumerate(units):
                if uses(mode, lst):
                    for j in range(len(rs)):
                        mvds[lst][first_block[k] + j] = read_mvd(br)
    parts = []
    for k, (mode, rs) in enumerate(units):
        for j, (x, y, w, h) in enumerate(rs):
            b = first_block[k] + j
            parts.append(dict(x=x, y=y, w=w, h=h, mode=mode, ref=refs[0][k], ref1=refs[1][k], mvd=mvds[0][b], mvd1=mvds[1][b],
                              unit=k))
    rec.parts = parts
    f["sub"] = ",".join(map(str, sub_raw)) if sub_raw else "-"
    for lst, rk, mk in ((0, "refidx", "mvd"), (1, "refidx1", "mvd1")):
        rl = [str(v) for v in refs[lst] if v is not None]
        ml = [f"{v[0]}:{v[1]}" for v in mvds[lst] if v is not None]
        f[rk] = ",".join(rl) if rl else "-"
        f[mk] = ",".join(ml) if ml else "-"
    if sub_raw is None:
        return True
    return all(s_ == 0 and info.direct_8x8_inference or (s_ in (1, 2, 3)) for s_ in sub_raw)


def read_b_macroblock(br, info, rec, raw, ctx):
    """Everything after mb_type of a B macroblock (raw 0..22)."""
    f = rec.f
    name = MB_TYPE_B[raw][0]
    f.update(mb_type=raw, name=name, i16_mode="-", chroma_cbp="-", luma_cbp="-", modes="-", chroma_pred="-")
    if raw == 0:
        rec.parts = [dict(x=0, y=0, w=16, h=16, mode="Direct", ref=None, ref1=None, mvd=None, mvd1=None, unit=0)]
        no_sub_lt8 = bool(info.direct_8x8_inference)
    else:
        no_sub_lt8 = read_b_pred(br, info, rec, raw)
    code = br.ue()
    if code > 47:
        raise ParseError(f"coded_block_pattern codeNum {code} > 47")
    cbp = ref.CBP_INTER[code]
    f["cbp"] = cbp
    t8 = 0
    if (cbp & 15) > 0 and info.transform_8x8_mode and no_sub_lt8:
        t8 = br.u(1)
    f["t8x8"] = t8
    if cbp > 0:
        d = br.se()
        if not -26 <= d <= 25:
            raise ParseError(f"mb_qp_delta {d} outside -26..25")
        f["qp_delta"] = d
        ctx["qp"] = (ctx["qp"] + d + 52) % 52
    else:
        f["qp_delta"] = "-"
    f["qp"] = ctx["qp"]
    ctx["res3"].macroblock(br, rec, "inter", cbp)


def read_intra_macroblock(br, info, rec, raw_i, coded_raw, ctx):
    """An intra macroblock of an I or P slice; raw_i = mb_type of Table 7-11 (0..25), coded_raw what was in the stream."""
    ti = ref.mb_type_info(raw_i)
    rec.f.update(mb_type=coded_raw, name=ti["name"], i16_mode=ti["i16_mode"], chroma_cbp=ti["chroma_cbp"],
                 luma_cbp=ti["luma_cbp"])
    if ti["kind"] == "pcm":
        ref.read_pcm(br, rec)
        rec.f.update(t8x8="-", modes="-", chroma_pred="-", cbp="-", qp_delta="-", qp=ctx["qp"])
        ctx["res3"].pcm(rec.addr)
        rec.blocks = []
        rec.f.update(nc="-", tc="-", coef="-")
        ctx["res3"].stored(rec)
    else:
        ref.finish_macroblock_l2(br, info, rec, ti["kind"], ctx)


def new_record(addr, bit):
    rec = MbRecord(addr)
    rec.parts = []
    rec.f.update(bit=bit, skip_run="-", sub="-", refidx="-", mvd="-", refidx1="-", mvd1="-")
    return rec


def skipped_record(addr, ctx, name="P_Skip"):
    rec = new_record(addr, "-")
    rec.parts = []
    rec.f.update(mb_type="skip", name=name, i16_mode="-", chroma_cbp="-", luma_cbp="-", t8x8="-", modes="-",
                 chroma_pred="-", cbp="-", qp_delta="-", qp=ctx["qp"], nc="-", tc="-", coef="-")
    rec.blocks = []
    if MUTANT != "skip_unavail":
        res3 = ctx["res3"]
        res3.tcl[addr] = [0] * 16                       # a skipped macroblock has total_coeff 0 in every block
        res3.tcc[addr] = [[0] * 4, [0] * 4]
        res3.stored(rec)
    else:
        rec.f.update(tcl=",".join(["0"] * 16), tccb="0,0,0,0", tccr="0,0,0,0")
    return rec


# ================================================================ slice_data()
def parse_slice(info, level=1):
    """slice_data() of an I or P slice, CAVLC (7.3.4).  -> cavlc_ref.SliceResult."""
    res = ref.SliceResult(info)
    if info.skip_reason:
        return res
    br = BitReader(info.nal, info.header_end_bit)
    ctx = dict(qp=info.slice_qp, seen_pcm=False, res3=cavlc_resid.SliceResidual(info.pic_width_in_mbs), recs={})
    is_p = info.slice_type == 0
    is_b = info.slice_type == 1
    addr = info.first_mb
    n_pic = info.pic_size_in_mbs
    try:
        while True:
            run = None
            if is_p or is_b:
                run = br.ue()                                    # mb_skip_run
                if MUTANT == "skip_run_plus1":
                    run += 1
                if addr + run > n_pic:
                    raise ParseError(f"mb_skip_run {run} at macroblock {addr} runs past the end of the picture ({n_pic})")
                for _ in range(run):
                    rec = skipped_record(addr, ctx, "B_Skip" if is_b else "P_Skip")
                    if MUTANT == "qp_after_skip":
                        ctx["qp"] = info.slice_qp
                    res.mbs.append(rec)
                    ctx["recs"][addr] = rec
                    addr += 1
                if run > 0 and not br.more_rbsp_data() and MUTANT != "no_trailing_skip":
                    res.status, res.end_bit = "complete", br.pos        # the slice ended with a skip run
                    return res
            if addr >= n_pic:
                raise ParseError(f"macroblock address {addr} is beyond PicSizeInMbs {n_pic}")
            rec = new_record(addr, br.pos)
            raw = br.ue()
            if MUTANT == "p8x16_swap" and is_p and raw in (1, 2):
                raw = 3 - raw
            if run is not None:
                rec.f["skip_run"] = run
            if is_b and raw < 23:
                if MUTANT == "b_swap_l0_l1" and raw in (1, 2):
                    raw = 3 - raw
                if MUTANT == "b_8x16_swap" and 4 <= raw <= 21:
                    raw ^= 1                                 # wrong: 16x8 and 8x16 rows of Table 7-14 exchanged
                read_b_macroblock(br, info, rec, raw, ctx)
            elif is_b:
                read_intra_macroblock(br, info, rec, raw - (22 if MUTANT == "b_intra_offset22" else 23), raw, ctx)
            elif is_p and raw < 5:
                read_inter_macroblock(br, info, rec, raw, ctx)
            elif is_p:
                read_intra_macroblock(br, info, rec, raw - (4 if MUTANT == "intra_offset4" else 5), raw, ctx)
            else:
                read_intra_macroblock(br, info, rec, raw, raw, ctx)
            res.mbs.append(rec)
            ctx["recs"][addr] = rec
            if br.pos > br.stop_bit:
                raise ParseError(f"macroblock {addr} ends at bit {br.pos}, after the rbsp_stop_one_bit at bit {br.stop_bit}")
            if not br.more_rbsp_data():
                res.status, res.end_bit = "complete", br.pos
                return res
            addr += 1
    except ParseError as e:
        res.status, res.error = "error", str(e)
        return res


def parse_all(infos, level=1):
    return [parse_slice(i, level) for i in infos]


# ================================================================ dump rendering
def mb_line(slice_index, rec, level=1):
    return f"MB {slice_index} {rec.addr} " + " ".join(f"{k}={rec.f[k]}" for k in keys_mb(level))


def render_slice(res, level=1):
    i = res.info
    out = [f"SLICE {i.index} type={ref.SLICE_NAMES[i.slice_type]} first_mb={i.first_mb} "
           f"pic_size_in_mbs={i.pic_size_in_mbs} header_end_bit={i.header_end_bit}"]
    for rec in res.mbs:
        out.append(mb_line(i.index, rec, level))
        if rec.pcm is not None:
            p = rec.pcm
            out.append(f"PCM {i.index} {rec.addr} align_bits={p['align_bits']} sum_y={p['sum_y']} "
                       f"sum_cb={p['sum_cb']} sum_cr={p['sum_cr']}")
    out.append(f"END {i.index} mbs={len(res.mbs)} status={res.status} "
               f"end_bit={res.end_bit if res.status == 'complete' else '-'}")
    return out


def histogram(results):
    h = {}
    for r in results:
        for rec in r.mbs:
            h[rec.f["name"]] = h.get(rec.f["name"], 0) + 1
    return h


def hist_line(h):
    return "HIST" + "".join(f" {k}:{h[k]}" for k in sorted(h))


def render_dump(results, level=1):
    out = []
    for r in results:
        out += render_slice(r, level)
    out.append(hist_line(histogram(results)))
    return "\n".join(out) + "\n"


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def parse_dump(path):
    """Parse a dump (reader output or expected) -> (slices, hist); slices = list of dict(index, slice=kv, mbs=[(addr, kv)],
    pcm={addr: kv}, end=kv); hist = {name: count}.  Exits with code 2 on malformed text."""
    try:
        text = open(path, "r").read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    slices, cur, hist = [], None, None

    def kvs(tokens, no, line):
        d = {}
        for t in tokens:
            if "=" not in t:
                die(f"malformed reader output, line {no}: expected key=value, found {t!r}: {line!r}")
            k, v = t.split("=", 1)
            d[k] = v
        return d

    def need_int(s, what, no, line):
        if not re.fullmatch(r"\d+", s):
            die(f"malformed reader output, line {no}: {what} must be a non-negative integer, found {s!r}: {line!r}")
        return int(s)
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        kind = tok[0]
        if hist is not None:
            die(f"malformed reader output, line {no}: nothing may follow the HIST line")
        if kind == "SLICE":
            if len(tok) < 2:
                die(f"malformed reader output, line {no}: SLICE needs an index: {line!r}")
            cur = dict(index=need_int(tok[1], "slice index", no, line), slice=kvs(tok[2:], no, line), mbs=[], pcm={},
                       end=None, line=no)
            slices.append(cur)
        elif kind in ("MB", "PCM", "END"):
            if cur is None or cur["end"] is not None:
                die(f"malformed reader output, line {no}: {kind} line outside a SLICE ... END block: {line!r}")
            if len(tok) < 2 or need_int(tok[1], "slice index", no, line) != cur["index"]:
                die(f"malformed reader output, line {no}: slice index missing or not {cur['index']} inside the block "
                    f"of slice {cur['index']}: {line!r}")
            if kind == "END":
                cur["end"] = kvs(tok[2:], no, line)
            else:
                if len(tok) < 3:
                    die(f"malformed reader output, line {no}: {kind} needs a macroblock address: {line!r}")
                addr = need_int(tok[2], "macroblock address", no, line)
                kv = kvs(tok[3:], no, line)
                if kind == "MB":
                    cur["mbs"].append((addr, kv))
                else:
                    cur["pcm"][addr] = kv
        elif kind == "HIST":
            hist = {}
            for t in tok[1:]:
                m = re.fullmatch(r"([A-Za-z0-9_]+):(\d+)", t)
                if not m:
                    die(f"malformed reader output, line {no}: HIST entries look like P_Skip:17, found {t!r}")
                hist[m.group(1)] = int(m.group(2))
        else:
            die(f"malformed reader output, line {no}: unknown line type {kind!r} (allowed: SLICE MB PCM END HIST, "
                f"or # comments)")
    if cur is not None and cur["end"] is None:
        die(f"malformed reader output: the block of slice {cur['index']} has no END line")
    if hist is None:
        die("malformed reader output: no HIST line at the end")
    return slices, hist


def hist_diff(a, b):
    keys = sorted(k for k in set(a) | set(b) if a.get(k, 0) != b.get(k, 0))
    return ", ".join(f"{k}: {a.get(k, 0)} vs {b.get(k, 0)}" for k in keys[:10]) + (" ..." if len(keys) > 10 else "")


# ================================================================ ffmpeg's view (cross-checks of the reference)
_ROW = re.compile(r"(\d+) (\D*\S\D*)$")


def _cells_once(path, workdir, timeout):
    log = os.path.join(workdir, "grid3.log")
    cmd = ["ffmpeg", "-hide_banner", "-v", "repeat+debug", "-threads", "1", "-debug", "mb_type"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-f", "null", "-"]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f, timeout=timeout, check=False)
    pics = []
    with open(log, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = ref.FRAME_LINE.search(line)
            if m:
                pics.append(dict(type=m.group(1), rows=[]))
                continue
            if not pics:
                continue
            rows = pics[-1]["rows"]
            for m in _ROW.finditer(line):
                if int(m.group(1)) == 16 * len(rows):
                    s = m.group(2)
                    s = s.ljust(-(-len(s) // 3) * 3)
                    rows.append([s[i:i + 3] for i in range(0, len(s), 3)])
                    break
    return pics


def ffmpeg_cells(path, workdir, timeout=300, dims=None, tries=4, count=None):
    """Like cavlc_ref.ffmpeg_mb_grid but every cell is the full 3-character string ffmpeg prints for a macroblock:
    char 0 = class ('i' Intra4x4, 'I' Intra16x16, 'P' PCM, 'S' skip, '>' inter predicted from list 0 only, '<' list 1 only,
    'X' both, 'd' direct), char 1 = partition shape (' ' 16x16 or intra, '-' 16x8, '|' 8x16, '+' 8x8), char 2 = ' '
    (or '=' for field macroblocks).  Same duplicates / retry conventions as ffmpeg_mb_grid."""
    for _ in range(tries):
        pics = _cells_once(path, workdir, timeout)
        if count is not None:
            if len(pics) < count:
                return pics
            pics = pics[-count:]
        if dims is None or all(len(p["rows"]) == dims[1] and all(len(r) == dims[0] for r in p["rows"]) for p in pics):
            return pics
    return None


def expected_cell(rec):
    """The first two characters ffmpeg's macroblock map shows for a reference record (None = not comparable: direct)."""
    n = rec.f["name"]
    if n.startswith("B_"):
        if "Direct" in n or n == "B_Skip" or any(p["mode"] == "Direct" for p in rec.parts):
            return None
        used0 = any(p["mode"] in ("L0", "Bi") for p in rec.parts)
        used1 = any(p["mode"] in ("L1", "Bi") for p in rec.parts)
        if n == "B_8x8":                    # ffmpeg marks every B_8x8 macroblock as using both lists
            used0 = used1 = True
        cls = "X" if used0 and used1 else (">" if used0 else "<")
        shape = " " if n.endswith("16x16") else ("-" if n.endswith("16x8") else ("|" if n.endswith("8x16") else "+"))
        return cls + shape
    if n == "P_Skip":
        return "S "
    if n in SHAPE_CHAR:
        return ">" + SHAPE_CHAR[n]
    if n == "I_NxN":
        return "i "
    if n == "I_PCM":
        return "P "
    return "I "


def _pair_pictures(infos, grid, what, match_pictures, out_rank):
    """-> (usable decode-order picture set or None, note, rank) ; rank[p] = index of picture p in ffmpeg's (output-ordered) maps.
    out_rank (Chapter 7.8): list mapping decode index -> output index, verified against the picture types of the maps."""
    npics = infos[-1].picture + 1
    if out_rank is None:
        u, note = match_pictures(infos, grid, what)
        return u, note, list(range(npics))
    first = {}
    for i in infos:
        first.setdefault(i.picture, {0: "P", 1: "B", 2: "I"}.get(i.slice_type, "?"))
    ok = all(out_rank[p] < len(grid) and grid[out_rank[p]]["type"] == first[p] for p in range(npics))
    if not ok:
        return None, f"{what} skipped: could not match ffmpeg's pictures (output order) to slices", out_rank
    return set(range(npics)), "", out_rank


def check_vs_ffmpeg_cells(path, infos, results, td, match_pictures, out_rank=None):
    """Class and partition shape of every macroblock vs ffmpeg's `-debug mb_type` map.  -> (problems, summary).
    out_rank: see _pair_pictures (streams with reordered B pictures; ffmpeg prints its maps in output order)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    same = all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos)
    grid = ffmpeg_cells(path, td, dims=(w0, infos[0].pic_size_in_mbs // w0) if same else None, count=npics)
    if grid is None:
        return [], "ffmpeg macroblock-map cross-check skipped: ffmpeg's log was garbled on every try"
    if len(grid) < npics:
        return [], f"ffmpeg macroblock-map cross-check skipped: ffmpeg showed {len(grid)} pictures, stream has {npics}"
    grid = grid[-npics:]
    usable, note, rank = _pair_pictures(infos, grid, "ffmpeg macroblock-map cross-check", match_pictures, out_rank)
    if usable is None:
        return [], note
    problems, n = [], 0
    for r in results:
        if r.info.picture not in usable:
            continue
        rows, w = grid[rank[r.info.picture]]["rows"], r.info.pic_width_in_mbs
        for rec in r.mbs:
            y, x = divmod(rec.addr, w)
            if y >= len(rows) or x >= len(rows[y]):
                problems.append(f"slice {r.info.index} addr {rec.addr}: ffmpeg's macroblock map has no such macroblock")
                continue
            n += 1
            e, g = expected_cell(rec), rows[y][x][:2]
            if e is None:
                continue
            if e != g:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference says {rec.f['name']} (map cell {e!r}), "
                                f"ffmpeg's map shows {g!r}")
    return problems, f"ffmpeg macroblock map: class and partition shape of {n} macroblocks in {len(usable)} picture(s) agree{note}"


def check_vs_ffmpeg_qp(path, infos, results, td, match_pictures, out_rank=None):
    """QP_Y of every macroblock (skipped ones included) vs ffmpeg's `-debug qp` map.  -> (problems, summary)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    h0 = infos[0].pic_size_in_mbs // w0
    if not all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos):
        return [], "ffmpeg QP cross-check skipped: pictures of different sizes"
    grid = ref.ffmpeg_qp_grid(path, td, dims=(w0, h0), count=npics)
    if grid is None or len(grid) < npics:
        return [], "ffmpeg QP cross-check skipped: ffmpeg's log was unusable"
    usable, note, rank = _pair_pictures(infos, grid, "ffmpeg QP cross-check", match_pictures, out_rank)
    if usable is None:
        return [], note
    problems, n = [], 0
    for r in results:
        if r.info.picture not in usable:
            continue
        rows = grid[rank[r.info.picture]]["rows"]
        for rec in r.mbs:
            y, x = divmod(rec.addr, r.info.pic_width_in_mbs)
            if y >= len(rows):
                continue
            mine = 0 if rec.pcm is not None else rec.f["qp"]       # ffmpeg stores 0 for I_PCM (the deblocking value)
            n += 1
            if rows[y][x] != mine:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference QP_Y {mine}, ffmpeg {rows[y][x]}")
    return problems, f"ffmpeg QP map: QP_Y of {n} macroblocks (skipped ones included) in {len(usable)} picture(s) agree{note}"


MV_BYTES = 40                                   # sizeof(AVMotionVector) in ffmpeg 8.x
_SHOW_N = re.compile(r"\bn:\s*(\d+)\s")
_SHOW_MV = re.compile(r"side data - Motion vectors: \((\d+) bytes\)")


def ffmpeg_mv_counts(path, td, timeout=300):
    """Number of motion vectors ffmpeg exports per output frame (`-flags2 +export_mvs`, counted from the showinfo filter's
    'side data - Motion vectors: (N bytes)' line, 40 bytes each) -> list of ints, or None if the log is unusable.
    ffmpeg's exported list has one entry per macroblock partition of list 0 / list 1: 1 for a 16x16 macroblock or P_Skip,
    2 for 16x8 / 8x16, 4 for an 8x8 macroblock (one per 8x8 sub-macroblock whatever its sub_mb_type: ffmpeg exports at 8x8
    granularity, so sub-partition shapes are NOT visible in this count)."""
    log = os.path.join(td, "mvcount.log")
    cmd = ["ffmpeg", "-hide_banner", "-threads", "1", "-flags2", "+export_mvs"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-vf", "showinfo", "-f", "null", "-"]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f, timeout=timeout, check=False)
    counts = []
    with open(log, "r", errors="replace") as f:
        for line in f:
            m = _SHOW_N.search(line) if "pts_time" in line else None
            if m:
                counts.append(0)
                continue
            m = _SHOW_MV.search(line)
            if m and counts:
                if int(m.group(1)) % MV_BYTES:
                    return None
                counts[-1] += int(m.group(1)) // MV_BYTES
    return counts


def check_vs_ffmpeg_mv_counts(path, infos, results, td, match_pictures):
    """Per picture: number of macroblock partitions (skip / 16x16 = 1, 16x8 / 8x16 = 2, 8x8 = 4) vs the motion vectors
    ffmpeg exports."""
    if any(i.slice_type not in (0, 2) for i in infos):
        return [], "ffmpeg motion-vector count check skipped: the stream has B slices"
    npics = infos[-1].picture + 1
    got = ffmpeg_mv_counts(path, td)
    if got is None or len(got) < npics:
        return [], "ffmpeg motion-vector count check skipped: ffmpeg's showinfo log was unusable"
    got = got[-npics:] if len(got) > npics else got
    mine = [0] * npics
    for r in results:
        for rec in r.mbs:
            n = rec.f["name"]
            mine[r.info.picture] += {"P_Skip": 1, "P_L0_16x16": 1, "P_L0_L0_16x8": 2, "P_L0_L0_8x16": 2, "P_8x8": 4,
                                     "P_8x8ref0": 4}.get(n, 0)
    problems = [f"picture {p}: reference finds {mine[p]} macroblock partitions, ffmpeg exports {got[p]} motion vectors"
                for p in range(npics) if mine[p] != got[p]]
    return problems, (f"ffmpeg exported-motion-vector count: {sum(mine)} macroblock partitions in {npics} picture(s) agree "
                      f"(skip / 16x16 = 1, 16x8 / 8x16 = 2, 8x8 = 4)")
