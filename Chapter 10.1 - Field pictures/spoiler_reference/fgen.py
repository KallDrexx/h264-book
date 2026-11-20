"""Chapter 10.1 synthetic field-picture stream generator.

Builds intra-only H.264 streams (High profile, 8-bit 4:2:0, frame_mbs_only_flag = 0, mb_adaptive_frame_field_flag = 0) whose pictures
are frames or FIELDS (field_pic_flag = 1), in any mix and order, with CAVLC or CABAC, POC types 0 / 1 / 2, cropping, several
slices per picture and random macroblocks (I_PCM, Intra16x16, Intra4x4, Intra8x8 with CABAC) with random residuals.
There is no encoder in the world (x264 included) that writes PAFF field pictures from raw video, so the pixel truth is ffmpeg
decoding these streams; the generator's own record of every macroblock (the 'manifest') drives the reference decoder fdec.py.

SCENARIO strings.  A scenario is a list of tokens, one per coded frame / field pair / lone field:
    F  frame (reference)         f  frame (non-reference)
    TB top field then bottom     BT bottom then top            (reference pair; lower case = non-reference pair)
    T  lone top field            B  lone bottom field          (lower case = non-reference)
  a leading '!' makes the first picture of the token an IDR picture ('!TB': IDR top field + non-IDR bottom field).
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import cabacgen  # noqa: E402
import cavlcgen  # noqa: E402
from common import bitwriter  # noqa: E402

LOG2_FN = 4            # MaxFrameNum 16 (so frame_num wraps in the longer scenarios)
LOG2_LSB = 4           # MaxPicOrderCntLsb 16 (so the POC msb wraps)

MUTANTS = {            # generator-level wrong rules: ffmpeg must NOT decode the result like the reference decoder
    "frame_ctx_in_field": "significant / last coefficient contexts of frame-coded blocks used in field pictures",
    "frame_sig8_in_field": "8x8 significant_coeff_flag ctxIdxInc table of frame blocks used in field pictures",
}


def parse_scenario(text):
    groups = []
    for tok in text.split():
        idr = tok.startswith("!")
        tok = tok.lstrip("!")
        ref = tok[0].isupper()
        t = tok.upper()
        groups.append(dict(idr=idr, ref=ref, structs=list(t) if t != "F" else ["F"]))
    return groups


# ---------------------------------------------------------------- the spec's POC equations written as direct formulas
def poc_truth(sps, groups_pics):
    """-> per coded picture dict(top, bot) (None = that parity is not in this picture).  groups_pics: list of dict(struct, idr, ref,
    frame_num, lsb, dbot, d0, d1) in decoding order.  8.2.1.1-8.2.1.3, written per picture with explicit state."""
    out = []
    pt = sps["poc_type"]
    maxlsb = 1 << sps["log2_lsb"]
    maxfn = 1 << LOG2_FN
    prev_msb = prev_lsb = 0
    prev_fn = 0
    fn_off = 0
    for p in groups_pics:
        st = p["struct"]
        if pt == 0:
            if p["idr"]:
                prev_msb = prev_lsb = 0
            lsb = p["lsb"]
            if lsb < prev_lsb and prev_lsb - lsb >= maxlsb // 2:
                msb = prev_msb + maxlsb
            elif lsb > prev_lsb and lsb - prev_lsb > maxlsb // 2:
                msb = prev_msb - maxlsb
            else:
                msb = prev_msb
            if st == "F":
                top = msb + lsb
                bot = top + p["dbot"]
            elif st == "T":
                top, bot = msb + lsb, None
            else:
                top, bot = None, msb + lsb
            if p["ref"]:
                prev_msb, prev_lsb = msb, lsb
        else:
            if p["idr"]:
                fn_off = 0
            elif prev_fn > p["frame_num"]:
                fn_off += maxfn
            prev_fn = p["frame_num"]
            if pt == 1:
                n = sps["cycle_len"]
                absfn = (fn_off + p["frame_num"]) if n else 0
                if not p["ref"] and absfn > 0:
                    absfn -= 1
                exp = 0
                if absfn > 0:
                    cyc, inc = divmod(absfn - 1, n)
                    exp = cyc * sum(sps["offset_ref_frame"]) + sum(sps["offset_ref_frame"][:inc + 1])
                if not p["ref"]:
                    exp += sps["offset_non_ref"]
                if st == "F":
                    top = exp + p["d0"]
                    bot = top + sps["offset_t2b"] + p["d1"]
                elif st == "T":
                    top, bot = exp + p["d0"], None
                else:
                    top, bot = None, exp + sps["offset_t2b"] + p["d0"]
            else:
                absfn = fn_off + p["frame_num"]
                if p["idr"]:
                    t = 0
                else:
                    t = 2 * absfn - (0 if p["ref"] else 1)
                top = t if st in "FT" else None
                bot = t if st in "FB" else None
        out.append(dict(top=top, bot=bot))
    return out


# ---------------------------------------------------------------- headers
def write_sps(sc):
    w = bitwriter.W()
    w.raw(8, 100), w.raw(8, 0), w.raw(8, 40)
    w.ue(0)
    w.ue(1), w.ue(0), w.ue(0), w.raw(1, 0), w.raw(1, 0)
    w.ue(LOG2_FN - 4)
    pt = sc["poc_type"]
    w.ue(pt)
    if pt == 0:
        w.ue(sc["log2_lsb"] - 4)
    elif pt == 1:
        w.raw(1, 0)                                          # delta_pic_order_always_zero_flag
        w.se(sc["offset_non_ref"]), w.se(sc["offset_t2b"]), w.ue(sc["cycle_len"])
        for v in sc["offset_ref_frame"]:
            w.se(v)
    w.ue(1), w.raw(1, 0)                                     # max_num_ref_frames, gaps_in_frame_num_value_allowed_flag
    w.ue(sc["mbw"] - 1), w.ue(sc["map_h"] - 1)
    w.raw(1, 0), w.raw(1, 0), w.raw(1, 1)                    # frame_mbs_only_flag 0, mb_adaptive_frame_field_flag 0, direct_8x8_inference 1
    cl, cr, ct, cb = sc["crop"]
    if any(sc["crop"]):
        w.raw(1, 1), w.ue(cl), w.ue(cr), w.ue(ct), w.ue(cb)
    else:
        w.raw(1, 0)
    if sc.get("reorder"):                                    # VUI bitstream_restriction: max_num_reorder_frames / max_dec_frame_buffering
        w.raw(1, 1)
        w.raw(1, 0), w.raw(1, 0), w.raw(1, 0), w.raw(1, 0), w.raw(1, 0), w.raw(1, 0), w.raw(1, 0), w.raw(1, 0)
        w.raw(1, 1), w.raw(1, 1), w.ue(0), w.ue(0), w.ue(16), w.ue(16), w.ue(sc["reorder"]), w.ue(sc["reorder"] + 1)
    else:
        w.raw(1, 0)
    return w


def write_pps(sc):
    w = bitwriter.W()
    w.ue(0), w.ue(0), w.raw(1, 1 if sc["cabac"] else 0)
    w.raw(1, 1 if sc["bottom_poc_present"] else 0)           # bottom_field_pic_order_in_frame_present_flag
    w.ue(0), w.ue(0), w.ue(0), w.raw(1, 0), w.raw(2, 0)
    w.se(0), w.se(0), w.se(sc["cqo"]), w.raw(1, 1), w.raw(1, 0), w.raw(1, 0)
    w.raw(1, 1 if sc["t8"] else 0), w.raw(1, 0), w.se(sc["cqo2"])
    return w


def slice_header(w, sc, p, sl):
    w.ue(sl["first_mb"])
    w.ue(2)
    w.ue(0)
    w.raw(LOG2_FN, p["frame_num"])
    st = p["struct"]
    w.raw(1, 0 if st == "F" else 1)                          # field_pic_flag
    if st != "F":
        w.raw(1, 1 if st == "B" else 0)                      # bottom_field_flag
    if p["idr"]:
        w.ue(p["idr_id"])
    if sc["poc_type"] == 0:
        w.raw(sc["log2_lsb"], p["lsb"])
        if sc["bottom_poc_present"] and st == "F":
            w.se(p["dbot"])
    elif sc["poc_type"] == 1:
        w.se(p["d0"])
        if sc["bottom_poc_present"] and st == "F":
            w.se(p["d1"])
    if p["ref_idc"]:
        if p["idr"]:
            w.raw(1, 0), w.raw(1, 0)
        else:
            w.raw(1, 0)
    w.se(sl["qp"] - 26)
    idc, a, b = sl["dbf"]
    w.ue(idc)
    if idc != 1:
        w.se(a), w.se(b)


# ---------------------------------------------------------------- scenario -> pictures
def build_pictures(sc, rng):
    """Expand the scenario into the coded pictures in decoding order (fields of a pair are two pictures)."""
    pics = []
    last_ref_fn = None
    idr_id = 0
    maxfn = 1 << LOG2_FN
    poc0 = 0                          # running desired POC for type 0
    for gi, g in enumerate(parse_scenario(sc["scenario"])):
        if sc.get("pocs"):
            poc0 = sc["pocs"][gi]
        fn = 0 if g["idr"] else (last_ref_fn + 1) % maxfn
        if g["idr"]:
            poc0 = 0
        group = []
        for i, st in enumerate(g["structs"]):
            idr = g["idr"] and i == 0
            p = dict(struct=st, idr=idr, ref=g["ref"], ref_idc=rng.randint(1, 3) if g["ref"] else 0, frame_num=fn,
                     idr_id=idr_id if idr else 0, second=(i == 1), group=gi)
            group.append(p)
        if g["idr"]:
            idr_id = (idr_id + 1) % 4
        # type 0: desired POCs (monotone; bottom-first pairs and bottom-first frames get the smaller value on the bottom field)
        if sc["poc_type"] == 0:
            step = rng.choice([2, 2, 2, 4, 6])
            if group[0]["struct"] == "F":
                bff = rng.random() < 0.4
                top = poc0 + (1 if bff else 0)
                bot = top + (-1 if bff else 1)
                if g["idr"] and bff:
                    top, bot = 1, 0
                group[0].update(want_top=top, want_bot=bot)
                grp_min = min(top, bot)
            else:
                base = poc0
                for i, p in enumerate(group):
                    p["want"] = base + i
                grp_min = base
            for p in group:
                if p["struct"] == "F":
                    p["lsb"] = p["want_top"] % (1 << sc["log2_lsb"])
                    p["dbot"] = p["want_bot"] - p["want_top"]
                else:
                    p["lsb"] = p["want"] % (1 << sc["log2_lsb"])
                    p["dbot"] = 0
            poc0 = grp_min + step
            if g["idr"]:
                poc0 = max(poc0, 2) + 0
        else:
            for p in group:
                p["lsb"], p["dbot"] = 0, 0
        for p in group:
            if sc["poc_type"] == 1:
                p["d0"] = rng.choice([0, 0, 1, -1, 2]) if not p["idr"] else 0
                p["d1"] = rng.choice([0, 0, 1, -1])
            else:
                p["d0"] = p["d1"] = 0
            if g["idr"] and sc["poc_type"] == 1 and p["struct"] != "F":
                p["d0"] = 0
        pics += group
        if g["ref"]:
            last_ref_fn = fn
    return pics


def default_scenario(**kw):
    sc = dict(name="x", scenario="!TB TB", cabac=False, t8=False, poc_type=0, log2_lsb=LOG2_LSB, mbw=5, map_h=3, crop=(0, 0, 0, 0),
              bottom_poc_present=True, seed=1, slices=(0, 1, 2), deblock=True,
              offset_non_ref=-1, offset_t2b=1, cycle_len=2, offset_ref_frame=[4, 2])
    sc.update(kw)
    sc["cqo"] = 0
    sc["cqo2"] = 0
    return sc


def build(sc, mutant=None):
    """-> (annexb bytes, manifest)."""
    rng = random.Random(sc["seed"])
    sc = dict(sc)
    sc["cqo"], sc["cqo2"] = rng.randint(-4, 4), rng.randint(-4, 4)
    pics = build_pictures(sc, rng)
    out = bytearray()
    out += bitwriter.nal(0x67, write_sps(sc).rbsp()) + bitwriter.nal(0x68, write_pps(sc).rbsp())
    mbw, mbh = sc["mbw"], sc["map_h"]                         # a field is mbw x map_h macroblocks
    slices = []
    for pi, p in enumerate(pics):
        field = p["struct"] != "F"
        h_mbs = mbh if field else 2 * mbh
        n = mbw * h_mbs
        ncuts = rng.choice(sc["slices"])
        cuts = sorted(rng.sample(range(1, n), ncuts))
        starts, ends = [0] + cuts, cuts + [n]
        for s0, s1 in zip(starts, ends):
            qp = rng.randrange(0, 31)
            dbf = (rng.choice([0, 0, 2, 0, 1]) if sc["deblock"] else 1, rng.randint(-4, 4), rng.randint(-4, 4))
            sl = dict(first_mb=s0, qp=qp, dbf=dbf, pic=pi)
            w = bitwriter.W()
            slice_header(w, sc, p, sl)
            cap = max(1, 24000 // (2000 << max(0, qp // 6 - 2)))
            mbs = []
            if sc["cabac"]:
                while len(w.bits) % 8:
                    w.raw(1, 1)
                se = cabacgen.SliceEnc("I", qp, 0, mbw, s0, rng, 1, cap, field, mutant)
                se.t8mode = sc["t8"]
                for a in range(s0, s1):
                    m = pick_mb(rng, se, a, sc["t8"])
                    se.code_mb(a, m, a == s1 - 1)
                    mbs.append(m)
                bits = w.bits + se.e.bits
                while len(bits) % 8:
                    bits.append(0)
                rbsp = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
            else:
                se = cavlcgen.CavlcEnc(qp, mbw, s0, rng, cap, w, field, mutant)
                for a in range(s0, s1):
                    m = pick_mb(rng, se, a, False)
                    se.code_mb(a, m, a == s1 - 1)
                    mbs.append(m)
                rbsp = w.rbsp()
            sl["mbs"] = mbs
            sl["nal"] = ((p["ref_idc"] << 5) | (5 if p["idr"] else 1))
            out += bitwriter.nal(sl["nal"], rbsp)
            slices.append(sl)
    man = dict(sc=sc, pics=pics, slices=slices, poc=poc_truth(sc, pics))
    return bytes(out), man


def pick_mb(rng, se, addr, t8):
    kinds = [("i4", 3), ("i16", 3), ("pcm", 1)] + ([("i8", 5)] if t8 else [])
    k = rng.choices([a for a, _ in kinds], [b for _, b in kinds])[0]
    m = dict(k=k, cpred=0, modes=None, cbp_l=0, cbp_c=0, qpd=0, mbt=None, t8=0)
    if k in ("i4", "i8"):
        m["mbt"] = 0
        m["t8"] = 1 if k == "i8" else 0
        m["cbp_l"], m["cbp_c"] = cabacgen.rand_cbp(rng)
        m["qpd"] = cabacgen.rand_qpd(rng)
    elif k == "i16":
        m["qpd"] = cabacgen.rand_qpd(rng)
    else:
        m["mbt"] = 25
        m["samples"] = bytes(rng.randrange(256) for _ in range(384))
    if k in ("i4", "i8", "i16"):
        cabacgen.choose_intra(se, addr, m, 0)
    if not (k == "i16" or m["cbp_l"] or m["cbp_c"]):
        m["qpd"] = 0
    return m


def expected_dump(man):
    """The expected slice-level dump (Readme section 5), assembled from the CONSTRUCTION: store numbers from the scenario's groups
    (a token with two fields is a complementary pair), POCs from poc_truth, output order = ascending frame POC inside each IDR period."""
    sc = man["sc"]
    mbw, mh = sc["mbw"], sc["map_h"]
    nsl = {}
    for sl in man["slices"]:
        nsl[sl["pic"]] = nsl.get(sl["pic"], 0) + 1
    lines, stores, order = [], {}, []
    period = 0
    for n, (p, poc) in enumerate(zip(man["pics"], man["poc"])):
        if p["idr"]:
            period += 1
        g = p["group"]
        top, bot = poc["top"], poc["bot"]
        pic_poc = min(x for x in (top, bot) if x is not None)
        if g not in stores:
            stores[g] = dict(id=len(stores), top=None, bot=None, fields="", period=period)
        s = stores[g]
        if top is not None:
            s["top"] = top
        if bot is not None:
            s["bot"] = bot
        s["fields"] += "F" if p["struct"] == "F" else p["struct"]
        mbs = mbw * (2 * mh if p["struct"] == "F" else mh)
        lines.append(f"PIC {n} struct={p['struct']} frame_num={p['frame_num']} idr={int(p['idr'])} ref={int(p['ref_idc'] != 0)} "
                     f"slices={nsl[n]} mbs={mbs} poc={pic_poc} top={'-' if top is None else top} bot={'-' if bot is None else bot} "
                     f"store={s['id']} second={int(p['second'])}")
    for k, s in enumerate(sorted(stores.values(), key=lambda s: (s["period"], min(x for x in (s["top"], s["bot"]) if x is not None), s["id"]))):
        poc = min(x for x in (s["top"], s["bot"]) if x is not None)
        lines.append(f"FRAME {k} store={s['id']} fields={s['fields']} top={'-' if s['top'] is None else s['top']} "
                     f"bot={'-' if s['bot'] is None else s['bot']} poc={poc}")
    return lines


SCENARIOS = {}


def scenarios():
    if SCENARIOS:
        return SCENARIOS
    S = [
        default_scenario(name="cavlc_tff", scenario="!TB TB TB TB TB TB TB TB TB TB", seed=11),
        default_scenario(name="cabac_bff", scenario="!BT BT BT BT BT BT BT BT BT BT", cabac=True, seed=12),
        default_scenario(name="cabac_t8_mixed", scenario="!F TB F BT F TB BT F TB", cabac=True, t8=True, seed=13),
        default_scenario(name="cavlc_mixed_poc1", scenario="!TB F BT TB F BT TB F TB BT", poc_type=1, seed=14),
        default_scenario(name="cabac_poc2", scenario="!BT TB F BT tb F TB bt F", poc_type=2, cabac=True, t8=True, seed=15),
        default_scenario(name="cavlc_crop", scenario="!TB BT F TB", crop=(1, 2, 1, 3), seed=16, bottom_poc_present=True),
        default_scenario(name="cabac_idr_again", scenario="!TB TB !BT BT !F TB", cabac=True, seed=17, t8=True),
        default_scenario(name="cavlc_nonref_pairs", scenario="!TB tb TB bt F f TB", seed=18),
        default_scenario(name="cavlc_lone_fields", scenario="!TB T B F t t T B TB", seed=19),
        default_scenario(name="cavlc_reorder", scenario="!TB TB tb tb F f BT", log2_lsb=6, reorder=2, pocs=[0, 12, 4, 8, 24, 16, 20],
                         seed=21),
        default_scenario(name="cabac_poc1_lone", scenario="!TB T F B TB t F", poc_type=1, cabac=True, seed=20),
    ]
    for s in S:
        SCENARIOS[s["name"]] = s
    return SCENARIOS


if __name__ == "__main__":
    name, out = sys.argv[1], sys.argv[2]
    data, man = build(scenarios()[name])
    open(out, "wb").write(data)
