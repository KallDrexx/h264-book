"""Helper for the Chapter 3.2 harness: a tiny PPS (and minimal SPS) *writer* used by --make-variants.

It builds parameter-set NAL units that real encoders will not give you: PPSs with and without the
optional trailing fields, scaling matrices whose list count depends on the SPS chroma format
(4:2:0, 4:2:2, 4:4:4), extreme field values, slice groups (FMO) and re-sent PPSs.
Reading it is optional; the reader you write only has to parse.
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


def sps_nal(sps_id, profile_idc=100, chroma_format_idc=1, w_mbs=22, h_mbs=18):
    """Minimal SPS (no VUI, no cropping, POC type 2).  Only what a PPS needs to refer to:
    the id and chroma_format_idc."""
    w = BitWriter()
    w.u(8, profile_idc)
    w.u(8, 0)            # constraint flags + reserved
    w.u(8, 40)           # level_idc
    w.ue(sps_id)
    if profile_idc >= 100:
        w.ue(chroma_format_idc)
        if chroma_format_idc == 3:
            w.u(1, 0)    # separate_colour_plane_flag
        w.ue(0)
        w.ue(0)
        w.u(1, 0)
        w.u(1, 0)        # seq_scaling_matrix_present_flag
    w.ue(0)              # log2_max_frame_num_minus4
    w.ue(2)              # pic_order_cnt_type
    w.ue(1)              # max_num_ref_frames
    w.u(1, 0)
    w.ue(w_mbs - 1)
    w.ue(h_mbs - 1)
    w.u(1, 1)            # frame_mbs_only_flag
    w.u(1, 1)            # direct_8x8_inference_flag
    w.u(1, 0)            # frame_cropping_flag
    w.u(1, 0)            # vui_parameters_present_flag
    return bytes([0x60 | 7]) + escape(w.rbsp())


def pps_nal(p):
    """p: dict of PPS fields (defaults below).  'trailing' is None (no optional fields) or a dict
    with transform_8x8_mode_flag, 'scaling' (None or a list with one entry per scaling list: None for
    'not present', else the list of delta_scale values written verbatim) and 'second_cqo' (None
    to omit the field - only legal as the last field, we always write it when 'trailing' is set)."""
    w = BitWriter()
    w.ue(p["id"])
    w.ue(p["sps_id"])
    w.u(1, p.get("cabac", 0))
    w.u(1, p.get("bottom_field_pic_order", 0))
    groups = p.get("slice_groups", 0)
    w.ue(groups)
    if groups > 0:
        t = p.get("slice_group_map_type", 1)
        w.ue(t)
        if t == 0:
            for i in range(groups + 1):
                w.ue(3 + i)
        elif t == 2:
            for i in range(groups):
                w.ue(i)
                w.ue(i + 5)
        elif t in (3, 4, 5):
            w.u(1, 1)
            w.ue(2)
        elif t == 6:
            n = 22 * 18   # pic_size_in_map_units: must equal the SPS size in map units (22x18 here)
            w.ue(n - 1)
            bits = groups.bit_length()   # Ceil(Log2(num_slice_groups_minus1 + 1)) for groups >= 1
            if (1 << bits) < groups + 1:
                bits += 1
            for i in range(n):
                w.u(bits, i % (groups + 1))
    w.ue(p.get("l0", 0))
    w.ue(p.get("l1", 0))
    w.u(1, p.get("weighted_pred", 0))
    w.u(2, p.get("weighted_bipred", 0))
    w.se(p.get("qp", 0))
    w.se(p.get("qs", 0))
    w.se(p.get("cqo", 0))
    w.u(1, p.get("deblock_ctl", 1))
    w.u(1, p.get("constrained_intra", 0))
    w.u(1, p.get("redundant_pic_cnt", 0))
    tr = p.get("trailing")
    if tr is not None:
        w.u(1, tr.get("t8x8", 0))
        sm = tr.get("scaling")
        w.u(1, 1 if sm is not None else 0)
        if sm is not None:
            for entry in sm:
                if entry is None:
                    w.u(1, 0)
                else:
                    w.u(1, 1)
                    for d in entry:
                        w.se(d)
        w.se(tr.get("second_cqo", 0))
    return bytes([0x60 | 8]) + escape(w.rbsp())


L16_FULL = [8, 2, -3, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, -5, 2, 2]   # explicit 16 entries
L16_USEDEFAULT = [-8]                                            # nextScale 0 at j=0: use default
L16_EARLY = [2, -10]                                             # ends after 2 deltas
L64_FULL = [8] + [1] * 63


def make_variants():
    """-> (list of (description, nal_bytes)) in stream order: SPS and PPS NAL units.
    SPS ids: 1 Main 4:2:0 (no chroma_format_idc field), 2 High 4:2:0, 3 High 4:4:4, 4 High 4:2:2."""
    v = []

    def add(desc, nal):
        v.append((desc, nal))

    add("SPS 1: Main, 4:2:0 (inferred)", sps_nal(1, 77))
    add("SPS 2: High 4:2:0", sps_nal(2, 100, 1))
    add("SPS 3: High 4:4:4 Predictive", sps_nal(3, 244, 3))
    add("SPS 4: High 4:2:2", sps_nal(4, 122, 2))
    add("PPS 1 (SPS 1): Main, no trailing fields (CAVLC, all defaults)", pps_nal(dict(id=1, sps_id=1)))
    add("PPS 2 (SPS 2): trailing present: transform_8x8_mode_flag 0, no matrix, second_chroma_qp_index_offset -3",
        pps_nal(dict(id=2, sps_id=2, cabac=1, cqo=-3, trailing=dict(t8x8=0, second_cqo=-3))))
    add("PPS 3 (SPS 2): transform_8x8 1, matrix with 8 lists (explicit, use-default, absent, early end, zeros, 64-entry)",
        pps_nal(dict(id=3, sps_id=2, cabac=1, cqo=2, trailing=dict(
            t8x8=1, second_cqo=5,
            scaling=[L16_FULL, L16_USEDEFAULT, None, L16_EARLY, None, [0] * 16, L64_FULL, [-8]]))))
    add("PPS 4 (SPS 2): transform_8x8 0, matrix present -> only 6 lists",
        pps_nal(dict(id=4, sps_id=2, trailing=dict(t8x8=0, scaling=[L16_FULL, None, None, None, None, L16_EARLY],
                                                    second_cqo=0))))
    add("PPS 5 (SPS 3, 4:4:4): transform_8x8 1, matrix -> 6 + 6 = 12 lists",
        pps_nal(dict(id=5, sps_id=3, cabac=1, trailing=dict(
            t8x8=1, second_cqo=1,
            scaling=[L16_FULL, None, None, None, None, None, L64_FULL, None, [-8], None, L64_FULL, [-8]]))))
    add("PPS 6 (SPS 3, 4:4:4): transform_8x8 0, matrix -> only 6 lists",
        pps_nal(dict(id=6, sps_id=3, trailing=dict(t8x8=0, scaling=[None] * 5 + [L16_FULL], second_cqo=-1))))
    add("PPS 7 (SPS 4, 4:2:2): transform_8x8 1, matrix -> 6 + 2 = 8 lists",
        pps_nal(dict(id=7, sps_id=4, cabac=1, trailing=dict(
            t8x8=1, second_cqo=0, scaling=[None] * 6 + [L64_FULL, L64_FULL]))))
    add("PPS 8 (SPS 2): extreme values: id 255, qp -26, qs +25, cqo -12 (second +12), 32 refs, bipred 2, "
        "bottom_field flag, constrained intra, redundant_pic_cnt",
        pps_nal(dict(id=255, sps_id=2, cabac=1, bottom_field_pic_order=1, l0=31, l1=31, weighted_pred=1,
                     weighted_bipred=2, qp=-26, qs=25, cqo=-12, deblock_ctl=0, constrained_intra=1,
                     redundant_pic_cnt=1, trailing=dict(t8x8=1, second_cqo=12))))
    add("PPS 2 re-sent with changed content (CAVLC, qp +9, cqo +4, no trailing fields)",
        pps_nal(dict(id=2, sps_id=2, cabac=0, qp=9, qs=-4, cqo=4, l0=2, l1=1, weighted_bipred=1)))
    add("PPS 2 re-sent again, identical to the one before",
        pps_nal(dict(id=2, sps_id=2, cabac=0, qp=9, qs=-4, cqo=4, l0=2, l1=1, weighted_bipred=1)))
    return v


def make_fmo():
    """Streams with slice groups (FMO), which the book does not decode."""
    v = [("SPS 1: Baseline", sps_nal(1, 66)),
         ("PPS 0: slice groups, map type 0 (interleaved), 3 groups", pps_nal(dict(id=0, sps_id=1, slice_groups=2, slice_group_map_type=0))),
         ("PPS 1: plain, no slice groups", pps_nal(dict(id=1, sps_id=1, qp=3))),
         ("PPS 2: slice groups, map type 2 (foreground), 2 groups", pps_nal(dict(id=2, sps_id=1, slice_groups=1, slice_group_map_type=2))),
         ("PPS 3: slice groups, map type 6 (explicit), 4 groups", pps_nal(dict(id=3, sps_id=1, slice_groups=3, slice_group_map_type=6)))]
    return v
