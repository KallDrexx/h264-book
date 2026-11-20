"""Synthetic MBAFF stream generator (Chapter 10.3's mbgen.py, extended in Chapter 10.4 with temporal / mixed direct modes and
explicit / implicit weighted prediction: scenario keys direct, wp) for the Chapter 10.3 / 10.4 harnesses (bit writer: common/bitwriter.py, CABAC encoder: cabac_enc.py).

The macroblock layer is produced by the SAME parser code the harness uses as its reference (mbaff.py, generate mode: every bin /
bit is drawn at random, written by the encoder, and the parser's state follows it; rejection sampling keeps the syntax legal).  So
the stream and its manifest (the parse) agree by construction.  Their correctness against the standard is NOT established by that,
but by ffmpeg: it decodes the streams with its own, independently written context derivation, and its macroblock map
(`-debug mb_type`: class, partition shape and the field-macroblock marker '=') and QP map must agree with the manifest.

Streams: High profile 4:2:0 8 bit, frame_mbs_only_flag 0, mb_adaptive_frame_field_flag 1, direct_8x8_inference_flag 1,
pic_order_cnt_type 2 (output order = decoding order, B pictures predict from the past only, spatial direct), I / P / B pictures
made of one or more slices that start at arbitrary macroblock pairs.
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import mbaff  # noqa: E402
from common import bitwriter  # noqa: E402

BASE = dict(cabac=1, t8=0, cip=0, mbw=5, pair_rows=3, pics="IPPBPB", slices=1, p_skip=0.3, p_field=0.5, p_pcm=0.03, p_intra=0.2,
            max_ref=3, direct=0, wp=0, qp_lo=14, qp_hi=40, deblock=1, run_w=[6, 3, 2, 2, 1, 1, 1, 1], mvd_max=24, seed=1)
SCENARIOS = {
    "g_i_cabac": dict(pics="IIII", seed=11),
    "g_i_cavlc": dict(cabac=0, pics="IIII", seed=12),
    "g_p_cabac": dict(pics="IPPPP", seed=13, slices=2),
    "g_p_cavlc": dict(cabac=0, pics="IPPPP", seed=14, slices=2),
    "g_b_cabac": dict(pics="IPBPB", seed=15, max_ref=3),
    "g_b_cavlc": dict(cabac=0, pics="IPBPB", seed=16, max_ref=3),
    "g_t8_cabac": dict(t8=1, pics="IPBPB", seed=17, slices=2),
    "g_t8_cavlc": dict(cabac=0, t8=1, pics="IPBPB", seed=18, slices=2),
    "g_skip_cabac": dict(pics="IPPBPB", seed=19, p_skip=0.8, p_field=0.5),
    "g_skip_cavlc": dict(cabac=0, pics="IPPBPB", seed=20, p_skip=0.8, run_w=[1, 1, 2, 3, 4, 3, 2, 2, 1, 1, 1, 1]),
    "g_field_cabac": dict(t8=1, pics="IPBPB", seed=21, p_field=0.8, p_skip=0.2, max_ref=3),
    "g_frame_cabac": dict(t8=1, pics="IPBPB", seed=22, p_field=0.15, p_skip=0.2),
    "g_cip_cabac": dict(cip=1, t8=0, pics="IPBPB", seed=23, p_intra=0.5, slices=2),
    "g_cip_cavlc": dict(cabac=0, cip=1, t8=0, pics="IPBPB", seed=24, p_intra=0.5),
    "g_pcm_cabac": dict(pics="IPPBPB", seed=25, p_pcm=0.25, p_intra=0.4),
    "g_pcm_cavlc": dict(cabac=0, pics="IPPBPB", seed=26, p_pcm=0.25, p_intra=0.4),
    "g_ref_cabac": dict(pics="IPPPBPB", seed=27, max_ref=4, p_field=0.6, slices=2),
    "g_ref_cavlc": dict(cabac=0, pics="IPPPBPB", seed=28, max_ref=4, p_field=0.6, slices=2),
    "g_mvd_cabac": dict(pics="IPBPB", seed=29, mvd_max=80, p_field=0.5, p_skip=0.1, p_intra=0.05, mbw=7, pair_rows=4),
    "g_wide_cabac": dict(t8=1, pics="IPBPB", seed=30, mbw=9, pair_rows=4, slices=3, p_field=0.5),
    "g_wide_cavlc": dict(cabac=0, t8=1, pics="IPBPB", seed=31, mbw=9, pair_rows=4, slices=3, p_field=0.5),
    "g_qp_cabac": dict(pics="IPBPB", seed=32, qp_lo=2, qp_hi=48, slices=2),
    "g_pm8_cabac": dict(t8=1, pics="IIII", seed=35, p_field=0.5, mbw=7, pair_rows=4, p_pcm=0),
    # ---- Chapter 10.4 additions
    "g_pskip_mix_cabac": dict(pics="IPPPPPPP", seed=94, p_skip=0.75, p_field=0.5, max_ref=4, mbw=7, pair_rows=4, p_intra=0.05, mvd_max=3),
    "g_pskip_mix_cavlc": dict(cabac=0, pics="IPPPPPPP", seed=52, p_skip=0.75, p_field=0.5, max_ref=4, mbw=7, pair_rows=4, p_intra=0.05, mvd_max=3, run_w=[1, 2, 3, 3, 2, 2, 1, 1, 1]),
    "g_temporal_cabac": dict(pics="IPPBPBPB", seed=500, direct=1, max_ref=3, p_skip=0.5, p_field=0.5),
    "g_temporal_cavlc": dict(cabac=0, pics="IPPBPBPB", seed=42, direct=1, max_ref=3, p_skip=0.5, p_field=0.5),
    "g_direct_mix_cabac": dict(t8=1, pics="IPPBPBPB", seed=500, direct=2, max_ref=3, slices=2, p_skip=0.4),
    "g_wp_explicit_cabac": dict(pics="IPPBPBPB", seed=44, wp=1, max_ref=3, p_field=0.6, p_skip=0.2),
    "g_wp_explicit_cavlc": dict(cabac=0, pics="IPPBPBPB", seed=45, wp=1, max_ref=3, p_field=0.6, p_skip=0.2),
    "g_wp_implicit_cabac": dict(pics="IPPBPBPB", seed=500, wp=2, max_ref=3, p_field=0.6, p_skip=0.2, direct=2),
    "g_wp_implicit_cavlc": dict(cabac=0, t8=1, pics="IPPBPBPB", seed=47, wp=2, max_ref=3, p_field=0.5, direct=2),
}


def sps_nal(sc):
    w = bitwriter.W()
    w.raw(8, 100), w.raw(8, 0), w.raw(8, 40)
    w.ue(0)
    w.ue(1), w.ue(0), w.ue(0), w.raw(1, 0), w.raw(1, 0)      # chroma_format_idc 1, bit depths, bypass, scaling matrix
    w.ue(0)                                                  # log2_max_frame_num_minus4 -> 4 bits
    w.ue(2)                                                  # pic_order_cnt_type 2
    w.ue(4), w.raw(1, 0)                                     # max_num_ref_frames, gaps
    w.ue(sc["mbw"] - 1), w.ue(sc["pair_rows"] - 1)           # map units are macroblock PAIR rows
    w.raw(1, 0), w.raw(1, 1), w.raw(1, 1)                    # frame_mbs_only 0, mb_adaptive_frame_field 1, direct_8x8_inference 1
    w.raw(1, 0), w.raw(1, 0)                                 # cropping, VUI
    return w


def pps_nal(sc):
    w = bitwriter.W()
    w.ue(0), w.ue(0), w.raw(1, sc["cabac"]), w.raw(1, 0)
    w.ue(0), w.ue(0), w.ue(0), w.raw(1, 1 if sc['wp'] else 0), w.raw(2, {0: 0, 1: 1, 2: 2}[sc['wp']])
    w.se(0), w.se(0), w.se(sc["cqo"]), w.raw(1, 1), w.raw(1, sc["cip"]), w.raw(1, 0)
    w.raw(1, sc["t8"]), w.raw(1, 0), w.se(sc["cqo2"])
    return w


def slice_header(w, sc, pic, sl):
    w.ue(sl["first_pair"])
    w.ue({"P": 0, "B": 1, "I": 2}[pic["type"]])
    w.ue(0)
    w.raw(4, pic["frame_num"])
    w.raw(1, 0)                                              # field_pic_flag
    if pic["idr"]:
        w.ue(0)
    if pic["type"] == "B":
        w.raw(1, sl["spatial"])                              # direct_spatial_mv_pred_flag
    if pic["type"] in "PB":
        w.raw(1, 1), w.ue(sl["n0"] - 1)                      # num_ref_idx_active_override_flag
        if pic["type"] == "B":
            w.ue(sl["n1"] - 1)
        w.raw(1, 0)                                          # ref_pic_list_modification_flag_l0
        if pic["type"] == "B":
            w.raw(1, 0)
    if sl.get("wtab") is not None and ((pic["type"] == "P" and sc["wp"]) or (pic["type"] == "B" and sc["wp"] == 1)):
        wtab = sl["wtab"]
        w.ue(wtab["dy"]), w.ue(wtab["dc"])
        for lst in range(2 if pic["type"] == "B" else 1):
            for e in wtab["l%d" % lst]:
                w.raw(1, e["lf"])
                if e["lf"]:
                    w.se(e["wy"]), w.se(e["oy"])
                w.raw(1, e["cf"])
                if e["cf"]:
                    for j in range(2):
                        w.se(e["wc"][j]), w.se(e["oc"][j])
    if pic["ref"]:
        if pic["idr"]:
            w.raw(1, 0), w.raw(1, 0)
        else:
            w.raw(1, 0)
    if sc["cabac"] and pic["type"] != "I":
        w.ue(sl["idc"])
    w.se(sl["qp"] - 26)
    w.ue(sl["dbf"][0])
    if sl["dbf"][0] != 1:
        w.se(sl["dbf"][1]), w.se(sl["dbf"][2])


def build(name, seed=None):
    """-> (Annex B bytes, dump text, manifest dict)."""
    sc = dict(BASE, **SCENARIOS[name])
    if seed is not None:
        sc["seed"] = seed
    rng = random.Random(sc["seed"])
    sc["cqo"], sc["cqo2"] = rng.randint(-3, 3), rng.randint(-3, 3)
    out = bytearray()
    out += bitwriter.nal(0x67, sps_nal(sc).rbsp()) + bitwriter.nal(0x68, pps_nal(sc).rbsp())
    mbw, rows = sc["mbw"], sc["pair_rows"]
    npairs = mbw * rows
    dump = []
    refs_avail = 0
    frame_num = 0
    sidx = 0
    pics = []
    for pi, ch in enumerate(sc["pics"]):
        idr = pi == 0
        ref = 0 if (ch == "B" and sc["pics"][pi - 1] != "B") else 1
        if idr:
            ch = "I"
        pic = dict(type=ch, idr=idr, ref=ref, frame_num=frame_num)
        ncuts = rng.randint(0, sc["slices"] - 1) if sc["slices"] > 1 else 0
        cuts = sorted(rng.sample(range(1, npairs), ncuts)) if ncuts else []
        starts, ends = [0] + cuts, cuts + [npairs]
        for p0, p1 in zip(starts, ends):
            spatial = 1 if sc["direct"] == 0 else 0 if sc["direct"] == 1 else rng.randint(0, 1)
            n0 = rng.randint(1, max(1, min(sc["max_ref"], refs_avail))) if ch in "PB" else 1
            if sc["direct"]:
                # ffmpeg keeps ONE table of reference pictures per picture ("FIXME need per slice" in libavcodec) and so maps the
                # co-located reference wrongly when slices of the co-located picture have different lists: every slice of a
                # picture gets the same list sizes
                if "n0" not in pic:
                    pic["n0"] = rng.randint(1, max(1, min(sc["max_ref"], refs_avail)))
                n0 = pic["n0"]
            if ch == "B" and not spatial:
                n0 = max(1, refs_avail)                       # temporal direct needs the co-located reference in RefPicList0
            n1 = rng.randint(1, max(1, min(2, refs_avail))) if ch == "B" else 1
            wtab = None
            if sc["wp"] and ch in "PB":
                dy_, dc_ = rng.randint(0, 6), rng.randint(0, 6)
                wtab = dict(dy=dy_, dc=dc_)
                for lst, cnt in ((0, n0), (1, n1)):
                    ents = []
                    for _ in range(cnt):
                        lf, cf = rng.randint(0, 1), rng.randint(0, 1)
                        # 7.4.3.2: w0 + w1 must not exceed 128 (a bi-predicted block sums 255 * (w0 + w1) in 16 bits in some decoders:
                        # ffmpeg's SSE2 code saturates beyond it), so no weight exceeds 64
                        cl = lambda v: max(-128, min(64, v))     # noqa: E731
                        ents.append(dict(lf=lf, cf=cf, wy=cl((1 << dy_) + rng.randint(-6, 6)) if lf else 1 << dy_, oy=rng.randint(-12, 12),
                                         wc=[cl((1 << dc_) + rng.randint(-6, 6)) for _ in range(2)], oc=[rng.randint(-12, 12) for _ in range(2)]))
                    wtab["l%d" % lst] = ents
            sl = dict(first_pair=p0, spatial=spatial, wtab=wtab, qp=rng.randint(sc["qp_lo"], sc["qp_hi"]), idc=rng.randrange(3), n0=n0, n1=n1,
                      dbf=(rng.choice([0, 2, 1]) if sc["deblock"] else 1, rng.randint(-3, 3), rng.randint(-3, 3)))
            info = mbaff.Info(index=sidx, slice_type={"P": 0, "B": 1, "I": 2}[ch], first_mb=2 * p0, pic_width_in_mbs=mbw,
                              pic_size_in_mbs=npairs * 2, entropy=sc["cabac"], slice_qp=sl["qp"],
                              transform_8x8_mode=sc["t8"], num_ref_idx_l0_active=n0, num_ref_idx_l1_active=n1,
                              sps=dict(direct_8x8_inference_flag=1), pps=dict(constrained_intra_pred_flag=sc["cip"]),
                              hdr=dict(cabac_init_idc=sl["idc"]))
            w = bitwriter.W()
            slice_header(w, sc, pic, sl)
            sp = mbaff.Slice(info, gen_rng=rng, end_addr=2 * p1, params=sc)
            if not sc["cabac"]:
                sp.eng.base = len(w.bits)
            recs, status = sp.parse()
            if sc["cabac"]:
                while len(w.bits) % 8:
                    w.raw(1, 1)
                bits = w.bits + sp.eng.result()
                rbsp = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
            else:
                w.bits += sp.eng.bits
                rbsp = w.rbsp()
            out += bitwriter.nal((pic["ref"] << 5 | 0x40 if pic["ref"] else 0) | (5 if idr else 1), rbsp)
            dump.append((info, recs, "complete"))
            sidx += 1
        pics.append(pic)
        if ref:
            frame_num = (frame_num + 1) % 16
            refs_avail = min(refs_avail + 1, 4)
    return bytes(out), mbaff.render_dump(dump), dict(sc=sc, pics=pics, slices=sidx)


if __name__ == "__main__":
    name = sys.argv[1]
    data, dump, man = build(name)
    open(sys.argv[2], "wb").write(data)
    open(sys.argv[2] + ".gen", "w").write(dump)
