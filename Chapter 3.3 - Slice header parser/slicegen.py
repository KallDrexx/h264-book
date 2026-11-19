"""Helper for the Chapter 3.3 harness: a tiny SPS / PPS / *slice header* writer used by --make-variants.

It builds parameter sets and slice NAL units that x264 will not give you: field pictures (PAFF),
all three picture-order-count types, explicit weight tables for both lists (P and B), long-term
reference operations (all six memory management commands), reordering commands of every kind,
monochrome / 4:4:4 / separate colour planes, redundant slices, SP/SI slices.  The slice data after
each header is random filler (no decoder will make a picture of it); only the headers are real.
Reading this file is optional; the reader you write only has to parse.

Every header is written through a recorder that remembers (name, value) for each syntax element in
the order and with the names ffmpeg's trace_headers uses, so the harness self-test can check this
writer against ffmpeg.
"""
import random


class W:
    def __init__(self):
        self.bits = []
        self.fields = []

    def raw(self, n, v):
        assert 0 <= v < (1 << n), (n, v)
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def _ue(self, v):
        assert v >= 0
        v += 1
        n = v.bit_length()
        self.raw(n - 1, 0)
        self.raw(n, v)

    def u(self, name, n, v):
        self.raw(n, v)
        if name:
            self.fields.append((name, v))

    def ue(self, name, v):
        self._ue(v)
        if name:
            self.fields.append((name, v))

    def se(self, name, v):
        self._ue(2 * v - 1 if v > 0 else -2 * v)
        if name:
            self.fields.append((name, v))

    def rbsp(self):
        b = self.bits + [1]
        while len(b) % 8:
            b.append(0)
        return bytes(int("".join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))


def escape(rbsp):
    out, zeros = bytearray(), 0
    for x in rbsp:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


HIGH = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


def sps_nal(p):
    """p keys: id, profile (default 100), cfi, separate, log2_fn (minus4), poc (type), log2_poc (minus4),
    always_zero, fmo (frame_mbs_only), mbaff."""
    w = W()
    prof = p.get("profile", 100)
    w.raw(8, prof)
    w.raw(8, 0)
    w.raw(8, 40)
    w._ue(p["id"])
    if prof in HIGH:
        cfi = p.get("cfi", 1)
        w._ue(cfi)
        if cfi == 3:
            w.raw(1, p.get("separate", 0))
        w._ue(0)
        w._ue(0)
        w.raw(1, 0)
        w.raw(1, 0)
    w._ue(p.get("log2_fn", 0))
    poc = p.get("poc", 0)
    w._ue(poc)
    if poc == 0:
        w._ue(p.get("log2_poc", 2))
    elif poc == 1:
        w.raw(1, p.get("always_zero", 0))
        w._ue(1)          # offset_for_non_ref_pic = -1
        w._ue(0)          # offset_for_top_to_bottom_field = 0
        w._ue(2)          # num_ref_frames_in_pic_order_cnt_cycle
        w._ue(3)          # offset_for_ref_frame[0] = 2
        w._ue(3)          # offset_for_ref_frame[1] = 2
    w._ue(16)             # max_num_ref_frames
    w.raw(1, 0)
    w._ue(21)             # 22 macroblocks wide
    w._ue(17)             # 18 map units high
    fmo = p.get("fmo", 1)
    w.raw(1, fmo)
    if not fmo:
        w.raw(1, p.get("mbaff", 0))
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    return bytes([0x60 | 7]) + escape(w.rbsp())


def pps_nal(p):
    """p keys: id, sps, cabac, bottom_poc, l0, l1 (default counts minus 1), wp, bipred, qp, deblock_ctl, redundant."""
    w = W()
    w._ue(p["id"])
    w._ue(p["sps"])
    w.raw(1, p.get("cabac", 0))
    w.raw(1, p.get("bottom_poc", 0))
    w._ue(0)
    w._ue(p.get("l0", 0))
    w._ue(p.get("l1", 0))
    w.raw(1, p.get("wp", 0))
    w.raw(2, p.get("bipred", 0))
    w.se("", p.get("qp", 0))
    w.se("", 0)
    w.se("", 0)
    w.raw(1, p.get("deblock_ctl", 1))
    w.raw(1, 0)
    w.raw(1, p.get("redundant", 0))
    return bytes([0x60 | 8]) + escape(w.rbsp())


# ---------------------------------------------------------------- the configurations
# (sps dict, pps dict) pairs.  Chosen so that every branch of 7.3.3 is reachable.
SPSS = [
    dict(id=0, profile=100, log2_fn=4, poc=0, log2_poc=6, fmo=0, mbaff=0),        # PAFF, 8-bit frame_num, 10-bit POC lsb
    dict(id=1, profile=77, log2_fn=0, poc=1, always_zero=0, fmo=1),               # POC type 1, 4-bit frame_num
    dict(id=2, profile=77, log2_fn=2, poc=2, fmo=1),                              # POC type 2
    dict(id=3, profile=100, log2_fn=1, poc=1, always_zero=1, fmo=0, mbaff=1),     # MBAFF, delta_pic_order_always_zero
    dict(id=4, profile=100, cfi=0, log2_fn=3, poc=0, log2_poc=0, fmo=1),          # monochrome
    dict(id=5, profile=244, cfi=3, separate=1, log2_fn=0, poc=2, fmo=1),          # 4:4:4, separate colour planes
    dict(id=6, profile=244, cfi=3, separate=0, log2_fn=12, poc=0, log2_poc=12, fmo=1),   # 4:4:4, 16-bit frame_num
    dict(id=7, profile=122, cfi=2, log2_fn=0, poc=0, log2_poc=0, fmo=0, mbaff=0),  # 4:2:2 PAFF
]
PPSS = [
    dict(id=0, sps=0, cabac=0, bottom_poc=1, l0=2, l1=1, wp=1, bipred=1, deblock_ctl=1, redundant=1),
    dict(id=1, sps=1, cabac=1, bottom_poc=1, l0=0, l1=0, wp=0, bipred=2, deblock_ctl=0, redundant=0),
    dict(id=2, sps=2, cabac=0, bottom_poc=0, l0=0, l1=3, wp=1, bipred=0, deblock_ctl=1, redundant=0),
    dict(id=3, sps=3, cabac=1, bottom_poc=1, l0=1, l1=1, wp=0, bipred=1, deblock_ctl=1, redundant=0),
    dict(id=4, sps=4, cabac=1, bottom_poc=0, l0=1, l1=0, wp=1, bipred=1, deblock_ctl=1, redundant=0),
    dict(id=5, sps=5, cabac=0, bottom_poc=0, l0=0, l1=0, wp=1, bipred=1, deblock_ctl=1, redundant=1),
    dict(id=6, sps=6, cabac=1, bottom_poc=1, l0=1, l1=1, wp=1, bipred=1, deblock_ctl=1, redundant=0),
    dict(id=7, sps=7, cabac=0, bottom_poc=1, l0=0, l1=0, wp=1, bipred=1, deblock_ctl=0, redundant=0),
    dict(id=200, sps=0, cabac=1, bottom_poc=0, l0=4, l1=2, wp=0, bipred=2, qp=-3, deblock_ctl=1, redundant=0),
]


def _find(lst, i):
    return next(d for d in lst if d["id"] == i)


def write_slice(sps, pps, s):
    """Write one slice header.  s: dict with nal_type (1/5), ref_idc, slice_type (0..9), and everything
    else used below (see random_slice).  Returns (nal bytes, recorded fields, header end bit incl. alignment)."""
    w = W()
    idr = s["nal_type"] == 5
    st = s["slice_type"] % 5
    cfi = sps.get("cfi", 1) if sps.get("profile", 100) in HIGH else 1
    sep = sps.get("separate", 0) if cfi == 3 else 0
    cat = 0 if sep else cfi
    fmo = sps.get("fmo", 1)
    field = s.get("field", 0) if not fmo else 0
    w.ue("first_mb_in_slice", s["first_mb"])
    w.ue("slice_type", s["slice_type"])
    w.ue("pic_parameter_set_id", pps["id"])
    if sep:
        w.u("colour_plane_id", 2, s["plane"])
    w.u("frame_num", sps.get("log2_fn", 0) + 4, s["frame_num"])
    if not fmo:
        w.u("field_pic_flag", 1, field)
        if field:
            w.u("bottom_field_flag", 1, s["bottom"])
    if idr:
        w.ue("idr_pic_id", s["idr_id"])
    poc = sps.get("poc", 0)
    bp = pps.get("bottom_poc", 0)
    if poc == 0:
        w.u("pic_order_cnt_lsb", sps.get("log2_poc", 2) + 4, s["poc_lsb"])
        if bp and not field:
            w.se("delta_pic_order_cnt_bottom", s["dpoc_bottom"])
    if poc == 1 and not sps.get("always_zero", 0):
        w.se("delta_pic_order_cnt[0]", s["dpoc0"])
        if bp and not field:
            w.se("delta_pic_order_cnt[1]", s["dpoc1"])
    if pps.get("redundant", 0):
        w.ue("redundant_pic_cnt", s["redundant"])
    if st == 1:
        w.u("direct_spatial_mv_pred_flag", 1, s["direct_spatial"])
    n0 = n1 = 0
    if st in (0, 1, 3):
        w.u("num_ref_idx_active_override_flag", 1, 1 if s["override"] else 0)
        n0, n1 = pps.get("l0", 0) + 1, pps.get("l1", 0) + 1
        if s["override"]:
            n0 = s["n0"]
            w.ue("num_ref_idx_l0_active_minus1", n0 - 1)
            if st == 1:
                n1 = s["n1"]
                w.ue("num_ref_idx_l1_active_minus1", n1 - 1)
    # ref_pic_list_modification
    for lst, cond in ((0, st not in (2, 4)), (1, st == 1)):
        if not cond:
            continue
        cmds = s["rplm"][lst]
        w.u(f"ref_pic_list_modification_flag_l{lst}", 1, 1 if cmds else 0)
        if cmds:
            for idc, val in cmds:
                w.ue("modification_of_pic_nums_idc", idc)
                w.ue("abs_diff_pic_num_minus1" if idc in (0, 1) else "long_term_pic_num", val)
            w.ue("modification_of_pic_nums_idc", 3)
    # pred_weight_table
    if (pps.get("wp", 0) and st in (0, 3)) or (pps.get("bipred", 0) == 1 and st == 1):
        pw = s["pwt"]
        w.ue("luma_log2_weight_denom", pw["ld"])
        if cat:
            w.ue("chroma_log2_weight_denom", pw["cd"])
        for lst, n in ((0, n0), (1, n1 if st == 1 else 0)):
            for i in range(n):
                e = pw["ent"][lst][i]
                w.u(f"luma_weight_l{lst}_flag[{i}]", 1, 1 if e["l"] else 0)
                if e["l"]:
                    w.se(f"luma_weight_l{lst}[{i}]", e["l"][0])
                    w.se(f"luma_offset_l{lst}[{i}]", e["l"][1])
                if cat:
                    w.u(f"chroma_weight_l{lst}_flag[{i}]", 1, 1 if e["c"] else 0)
                    if e["c"]:
                        for j in range(2):
                            w.se(f"chroma_weight_l{lst}[{i}][{j}]", e["c"][j][0])
                            w.se(f"chroma_offset_l{lst}[{i}][{j}]", e["c"][j][1])
    # dec_ref_pic_marking
    if s["ref_idc"] != 0:
        if idr:
            w.u("no_output_of_prior_pics_flag", 1, s["no_output"])
            w.u("long_term_reference_flag", 1, s["long_term"])
        else:
            w.u("adaptive_ref_pic_marking_mode_flag", 1, 1 if s["mmco"] else 0)
            if s["mmco"]:
                for op, a, b in s["mmco"]:
                    w.ue("memory_management_control_operation", op)
                    if op in (1, 3):
                        w.ue("difference_of_pic_nums_minus1", a)
                    if op == 2:
                        w.ue("long_term_pic_num", a)
                    if op in (3, 6):
                        w.ue("long_term_frame_idx", b)
                    if op == 4:
                        w.ue("max_long_term_frame_idx_plus1", a)
                w.ue("memory_management_control_operation", 0)
    if pps.get("cabac", 0) and st not in (2, 4):
        w.ue("cabac_init_idc", s["cabac_init"])
    w.se("slice_qp_delta", s["qp_delta"])
    if st in (3, 4):
        if st == 3:
            w.u("sp_for_switch_flag", 1, s["sp_switch"])
        w.se("slice_qs_delta", s["qs_delta"])
    if pps.get("deblock_ctl", 1):
        w.ue("disable_deblocking_filter_idc", s["dbk"])
        if s["dbk"] != 1:
            w.se("slice_alpha_c0_offset_div2", s["alpha"])
            w.se("slice_beta_offset_div2", s["beta"])
    if pps.get("cabac", 0):
        while len(w.bits) % 8:
            w.raw(1, 1)
    end = len(w.bits) + 8
    for _ in range(s["filler_bits"]):
        w.raw(1, s["rng_bit"]())
    hdr = bytes([(s["ref_idc"] << 5) | s["nal_type"]]) + escape(w.rbsp())
    return hdr, w.fields, end


def random_slice(rng, sps, pps):
    fmo = sps.get("fmo", 1)
    cabac = pps.get("cabac", 0)
    idr = rng.random() < 0.2
    kinds = [0, 1, 2] if cabac else [0, 1, 2, 3, 4]
    st = 2 if idr else rng.choice(kinds)
    if cabac and not idr and rng.random() < 0.0:
        st = 0
    s = dict(nal_type=5 if idr else 1)
    s["ref_idc"] = rng.randint(1, 3) if idr else rng.choice([0, 1, 2, 3])
    s["slice_type"] = st + 5 * rng.randint(0, 1)
    s["first_mb"] = rng.choice([0, 0, 1, 7, 100, 395])
    s["plane"] = rng.randint(0, 2)
    maxfn = 1 << (sps.get("log2_fn", 0) + 4)
    s["frame_num"] = rng.randrange(maxfn)
    s["field"] = rng.randint(0, 1)
    field = s["field"] and not fmo
    s["bottom"] = rng.randint(0, 1)
    s["idr_id"] = rng.choice([0, 1, 255, 65535])
    s["poc_lsb"] = rng.randrange(1 << (sps.get("log2_poc", 2) + 4))
    s["dpoc_bottom"] = rng.randint(-5, 5)
    s["dpoc0"] = rng.randint(-40, 40)
    s["dpoc1"] = rng.randint(-5, 5)
    s["redundant"] = rng.choice([0, 0, 1, 3])
    s["direct_spatial"] = rng.randint(0, 1)
    s["override"] = rng.random() < 0.6
    maxrefs = 32 if field else 16
    s["n0"] = rng.randint(1, min(maxrefs, 5))
    s["n1"] = rng.randint(1, min(maxrefs, 4))
    n0 = s["n0"] if s["override"] else pps.get("l0", 0) + 1
    n1 = s["n1"] if s["override"] else pps.get("l1", 0) + 1
    maxpic = (1 << (sps.get("log2_fn", 0) + 4)) * (2 if field else 1)
    rp = []
    for n in (n0, n1):
        cmds = []
        if rng.random() < 0.5:
            for _ in range(rng.randint(1, min(n, 4))):
                idc = rng.choice([0, 1, 2])
                cmds.append((idc, rng.randrange(min(maxpic, 40)) if idc < 2 else rng.randint(0, 9)))
        rp.append(cmds)
    s["rplm"] = rp
    ent = []
    for n in (n0, n1):
        lst = []
        for _ in range(n):
            lst.append(dict(
                l=(rng.randint(-128, 127), rng.randint(-128, 127)) if rng.random() < 0.6 else None,
                c=[(rng.randint(-128, 127), rng.randint(-128, 127)) for _ in range(2)] if rng.random() < 0.6 else None))
        ent.append(lst)
    s["pwt"] = dict(ld=rng.randint(0, 7), cd=rng.randint(0, 7), ent=ent)
    s["no_output"] = rng.randint(0, 1)
    s["long_term"] = rng.randint(0, 1)
    mm = []
    if rng.random() < 0.5:
        for _ in range(rng.randint(1, 5)):
            op = rng.randint(1, 6)
            mm.append((op, rng.randint(0, 15) if op != 4 else rng.randint(0, 16), rng.randint(0, 15)))
        if rng.random() < 0.3:
            mm.append((5, 0, 0))
    s["mmco"] = mm
    s["cabac_init"] = rng.randint(0, 2)
    s["qp_delta"] = rng.randint(-20, 20)
    s["sp_switch"] = rng.randint(0, 1)
    s["qs_delta"] = rng.randint(-10, 10)
    s["dbk"] = rng.choice([0, 1, 2])
    s["alpha"] = rng.randint(-6, 6)
    s["beta"] = rng.randint(-6, 6)
    s["filler_bits"] = rng.randint(8, 40)
    s["rng_bit"] = lambda: rng.randint(0, 1)
    return s


def make_stream(seed=1, slices_per_config=36):
    """-> (annex_b bytes, expected) where expected is a list of (fields, end_bit) per slice NAL."""
    rng = random.Random(seed)
    sc = b"\x00\x00\x00\x01"
    out = b""
    for sp in SPSS:
        out += sc + sps_nal(sp)
    live = {p["id"]: dict(p) for p in PPSS}
    for p in live.values():
        out += sc + pps_nal(p)
    expected = []
    for round_ in range(slices_per_config):
        if round_ == slices_per_config // 2:
            # re-send two PPSs with different content: the slice parser must use the new version
            live[2] = dict(live[2], cabac=1, wp=0, bipred=1, bottom_poc=1, l0=2)
            live[1] = dict(live[1], cabac=0, bipred=1, deblock_ctl=1, redundant=1)
            out += sc + pps_nal(live[2]) + sc + pps_nal(live[1])
        for pid in sorted(live):
            pps = live[pid]
            sps = _find(SPSS, pps["sps"])
            s = random_slice(rng, sps, pps)
            nal, fields, end = write_slice(sps, pps, s)
            out += sc + nal
            expected.append((fields, end))
    return out, expected


if __name__ == "__main__":
    data, exp = make_stream()
    print(len(data), len(exp))
