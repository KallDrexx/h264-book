"""Helper for the Chapter 3.1 harness: a tiny SPS *writer* used by --make-variants.

It builds SPS NAL units that real encoders will not give you (picture order count types 1
and 2, scaling lists inside the SPS, 4:2:2 / 4:4:4 / 4:0:0, cropping with interlace, full
VUI with HRD parameters).  Reading it is optional; the reader you write only has to parse.
"""


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, n, v):
        assert 0 <= v < (1 << n), (n, v)
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, v):
        assert v >= 0
        v += 1
        n = v.bit_length()
        self.u(n - 1, 0)
        self.u(n, v)

    def se(self, v):
        self.ue(2 * v - 1 if v > 0 else -2 * v)

    def rbsp(self):
        b = self.bits + [1]
        while len(b) % 8:
            b.append(0)
        return bytes(int("".join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))


def escape(rbsp):
    """Insert emulation prevention bytes."""
    out, zeros = bytearray(), 0
    for x in rbsp:
        if zeros >= 2 and x <= 3:
            out.append(3)
            zeros = 0
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


def write_scaling_list(w, deltas):
    """deltas: list of delta_scale values to write (may be shorter than the list size: the
    list then ends early because nextScale becomes 0).  Writes them verbatim."""
    for d in deltas:
        w.se(d)


def sps_nal(p):
    """p: dict of SPS fields (see VARIANTS).  Returns a complete NAL unit (header + escaped RBSP)."""
    w = BitWriter()
    prof = p["profile_idc"]
    w.u(8, prof)
    cs = p.get("constraint", 0)
    w.u(6, cs)
    w.u(2, 0)
    w.u(8, p.get("level_idc", 40))
    w.ue(p["id"])
    cfi = p.get("chroma_format_idc", 1)
    if prof in HIGH_PROFILES:
        w.ue(cfi)
        if cfi == 3:
            w.u(1, p.get("separate_colour_plane_flag", 0))
        w.ue(0)
        w.ue(0)
        w.u(1, 0)
        sm = p.get("scaling")  # None or list (one entry per list) of None | list of deltas
        w.u(1, 1 if sm is not None else 0)
        if sm is not None:
            for entry in sm:
                if entry is None:
                    w.u(1, 0)
                else:
                    w.u(1, 1)
                    write_scaling_list(w, entry)
    w.ue(p.get("log2_max_frame_num_minus4", 0))
    poc = p.get("poc_type", 0)
    w.ue(poc)
    if poc == 0:
        w.ue(p.get("log2_max_poc_lsb_minus4", 2))
    elif poc == 1:
        w.u(1, p.get("delta_pic_order_always_zero_flag", 0))
        w.se(p.get("offset_for_non_ref_pic", -1))
        w.se(p.get("offset_for_top_to_bottom_field", 0))
        offs = p.get("offset_for_ref_frame", [2, 2])
        w.ue(len(offs))
        for o in offs:
            w.se(o)
    w.ue(p.get("max_num_ref_frames", 1))
    w.u(1, p.get("gaps", 0))
    w.ue(p["w_mbs"] - 1)
    w.ue(p["h_map_units"] - 1)
    fmo = p.get("frame_mbs_only", 1)
    w.u(1, fmo)
    if not fmo:
        w.u(1, p.get("mbaff", 0))
    w.u(1, p.get("direct_8x8", 1))
    crop = p.get("crop")
    w.u(1, 1 if crop else 0)
    if crop:
        for c in crop:  # left, right, top, bottom
            w.ue(c)
    vui = p.get("vui")
    w.u(1, 1 if vui else 0)
    if vui:
        write_vui(w, vui)
    return bytes([0x60 | 7]) + escape(w.rbsp())


def write_hrd(w, h):
    n = len(h["bit_rate"])
    w.ue(n - 1)
    w.u(4, h.get("bit_rate_scale", 4))
    w.u(4, h.get("cpb_size_scale", 3))
    for i in range(n):
        w.ue(h["bit_rate"][i])
        w.ue(h["cpb_size"][i])
        w.u(1, h["cbr"][i])
    w.u(5, 23)   # initial_cpb_removal_delay_length_minus1
    w.u(5, 23)   # cpb_removal_delay_length_minus1
    w.u(5, 23)   # dpb_output_delay_length_minus1
    w.u(5, 24)   # time_offset_length


def write_vui(w, v):
    ar = v.get("aspect_ratio_idc")
    w.u(1, ar is not None)
    if ar is not None:
        w.u(8, ar)
        if ar == 255:
            w.u(16, v["sar_width"])
            w.u(16, v["sar_height"])
    ov = v.get("overscan_appropriate_flag")
    w.u(1, ov is not None)
    if ov is not None:
        w.u(1, ov)
    vs = v.get("video_signal")  # (video_format, full_range, colour_primaries, transfer, matrix) or (fmt, full) only
    w.u(1, vs is not None)
    if vs is not None:
        w.u(3, vs[0])
        w.u(1, vs[1])
        w.u(1, len(vs) > 2)
        if len(vs) > 2:
            w.u(8, vs[2])
            w.u(8, vs[3])
            w.u(8, vs[4])
    cl = v.get("chroma_loc")
    w.u(1, cl is not None)
    if cl is not None:
        w.ue(cl[0])
        w.ue(cl[1])
    ti = v.get("timing")
    w.u(1, ti is not None)
    if ti is not None:
        w.u(32, ti[0])
        w.u(32, ti[1])
        w.u(1, ti[2])
    nal, vcl = v.get("nal_hrd"), v.get("vcl_hrd")
    w.u(1, nal is not None)
    if nal is not None:
        write_hrd(w, nal)
    w.u(1, vcl is not None)
    if vcl is not None:
        write_hrd(w, vcl)
    if nal is not None or vcl is not None:
        w.u(1, v.get("low_delay_hrd_flag", 0))
    w.u(1, v.get("pic_struct_present_flag", 0))
    br = v.get("restriction")
    w.u(1, br is not None)
    if br is not None:
        w.u(1, 1)
        w.ue(2)
        w.ue(1)
        w.ue(15)
        w.ue(15)
        w.ue(br[0])  # max_num_reorder_frames
        w.ue(br[1])  # max_dec_frame_buffering


# Scaling-list delta recipes (see the Readme).  Sizes: 16 for lists 0-5, 64 for lists 6-11.
L16_FULL = [8, 2, -3, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, -5, 2, 2]   # explicit, 16 entries
L16_USEDEFAULT = [-8]                                            # nextScale=0 at j=0: use default
L16_EARLY = [2, -10]                                          # j=0: nextScale=10; j=1: nextScale=0 so entries 1..15 repeat 10
L64_FULL = [8] + [1] * 63


def make_variants():
    """Return list of (description, sps dict)."""
    base = dict(profile_idc=100, level_idc=40)
    sm8 = [L16_FULL, L16_USEDEFAULT, None, L16_EARLY, None, [0] * 16, L64_FULL, [-8]]
    v = []
    v.append(("poc type 1, cycle of 3 offsets, Main", dict(profile_idc=77, id=1, w_mbs=22, h_map_units=18,
              poc_type=1, offset_for_ref_frame=[2, -3, 70], offset_for_non_ref_pic=-1,
              offset_for_top_to_bottom_field=1, max_num_ref_frames=3)))
    v.append(("poc type 2, Baseline, log2_max_frame_num 16", dict(profile_idc=66, constraint=0b110000, id=2, w_mbs=11,
              h_map_units=9, poc_type=2, log2_max_frame_num_minus4=12)))
    v.append(("High, SPS scaling lists (4:2:0: 8 lists)", dict(base, id=3, w_mbs=22, h_map_units=18, scaling=sm8)))
    v.append(("High 4:4:4, 12 scaling lists, separate colour planes", dict(profile_idc=244, id=4, chroma_format_idc=3,
              separate_colour_plane_flag=1, w_mbs=8, h_map_units=6,
              scaling=[L16_FULL, None, None, None, None, None, L64_FULL, None, None, None, None, [-8]],
              crop=(1, 2, 3, 4))))
    v.append(("High, 1080p interlaced (PAFF/MBAFF), crop 8 rows", dict(base, id=5, w_mbs=120, h_map_units=34,
              frame_mbs_only=0, mbaff=1, crop=(0, 0, 0, 2), level_idc=41, max_num_ref_frames=4)))
    v.append(("High, 1080p progressive, crop 8 rows", dict(base, id=6, w_mbs=120, h_map_units=68,
              crop=(0, 0, 0, 4), level_idc=41, max_num_ref_frames=4)))
    v.append(("High 4:2:2, crop left/right 3,5", dict(profile_idc=122, id=7, chroma_format_idc=2, w_mbs=45,
              h_map_units=36, crop=(3, 5, 1, 1))))
    v.append(("High 4:0:0 monochrome, cropped", dict(profile_idc=100, id=8, chroma_format_idc=0, w_mbs=20,
              h_map_units=15, crop=(1, 3, 2, 5))))
    hrd = dict(bit_rate=[1000, 2000], cpb_size=[500, 900], cbr=[1, 0])
    v.append(("Main, full VUI: SAR 255, overscan, signal type + colour, chroma loc, timing, NAL+VCL HRD, restriction",
              dict(profile_idc=77, id=9, w_mbs=44, h_map_units=36, max_num_ref_frames=4,
                   vui=dict(aspect_ratio_idc=255, sar_width=4, sar_height=3, overscan_appropriate_flag=1,
                            video_signal=(5, 1, 1, 1, 1), chroma_loc=(1, 2), timing=(1001, 60000, 1),
                            nal_hrd=hrd, vcl_hrd=dict(bit_rate=[800], cpb_size=[400], cbr=[0]),
                            low_delay_hrd_flag=1, pic_struct_present_flag=1, restriction=(2, 4)))))
    v.append(("Main, VUI: SAR idc 1, video_signal without colour description, timing only",
              dict(profile_idc=77, id=10, w_mbs=44, h_map_units=36,
                   vui=dict(aspect_ratio_idc=1, video_signal=(2, 0), timing=(1, 50, 0)))))
    v.append(("Main, VUI: only VCL HRD, bitstream_restriction",
              dict(profile_idc=77, id=11, w_mbs=44, h_map_units=36,
                   vui=dict(vcl_hrd=dict(bit_rate=[5], cpb_size=[7], cbr=[1]), restriction=(0, 1)))))
    return v
