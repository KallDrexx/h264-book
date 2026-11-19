"""Helper for the Chapter 3.4 harness: a writer for synthetic H.264 streams that exercise picture boundary
detection and all three picture order count types, including MMCO 5.  Used by --make-variants.

x264 only writes pic_order_cnt_type 0 and 2 and never MMCO 5, so the other cases are written here.  Every
picture is one or more slice NAL units with a REAL slice header and random filler instead of macroblock data
(no decoder can make a picture of it; only the headers are real).  The writer knows what POC every picture
is supposed to get, because it chooses the POC first and then derives the coded fields from it (the lsb, or
the delta_pic_order_cnt values).  That gives the harness a truth that does not depend on the harness's own
POC implementation.  Reading this file is optional; the reader you write only has to parse.

make_stream() returns (annex_b bytes, pictures) where pictures is a list of dicts, in decode order:
    idr, ref_idc (first slice), type ('I','P','B'), nslices, frame_num, top, bottom (as decoded),
    mmco5, first_nal (index of the first slice NAL unit among ALL NAL units of the stream)
The POC of a frame is min(top, bottom).
"""
import random


class W:
    def __init__(self):
        self.bits = []

    def raw(self, n, v):
        assert 0 <= v < (1 << n), (n, v)
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, v):
        assert v >= 0
        v += 1
        n = v.bit_length()
        self.raw(n - 1, 0)
        self.raw(n, v)

    def se(self, v):
        self.ue(2 * v - 1 if v > 0 else -2 * v)

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


def sps_nal(p):
    w = W()
    w.raw(8, 77)                      # Main profile: no chroma_format_idc etc.
    w.raw(8, 0)
    w.raw(8, 40)
    w.ue(p["id"])
    w.ue(p["log2_fn"])                # log2_max_frame_num_minus4
    w.ue(p["poc"])
    if p["poc"] == 0:
        w.ue(p["log2_poc"])
    elif p["poc"] == 1:
        w.raw(1, p.get("always_zero", 0))
        w.se(p.get("nonref", 0))
        w.se(p.get("t2b", 0))
        w.ue(len(p.get("cycle", [])))
        for o in p.get("cycle", []):
            w.se(o)
    w.ue(4)                           # max_num_ref_frames
    w.raw(1, 0)
    w.ue(21)
    w.ue(17)
    w.raw(1, 1)                       # frame_mbs_only_flag
    w.raw(1, 1)                       # direct_8x8_inference_flag
    w.raw(1, 0)
    w.raw(1, 0)
    return bytes([0x60 | 7]) + escape(w.rbsp())


def pps_nal(p):
    w = W()
    w.ue(p["id"])
    w.ue(p["sps"])
    w.raw(1, 0)                       # CAVLC
    w.raw(1, p["bottom"])             # bottom_field_pic_order_in_frame_present_flag
    w.ue(0)
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)
    w.raw(2, 0)
    w.se(0)
    w.se(0)
    w.se(0)
    w.raw(1, 0)                       # deblocking_filter_control_present_flag
    w.raw(1, 0)
    w.raw(1, 0)
    return bytes([0x60 | 8]) + escape(w.rbsp())


AUD = bytes([0x09, 0xF0])
SEI = bytes([0x06, 0x05, 0x11]) + bytes(range(1, 17)) + bytes([0x42, 0x80])


def slice_nal(sps, pps, s, rng):
    w = W()
    idr = s["idr"]
    st = s["slice_type"] % 5
    w.ue(s["first_mb"])
    w.ue(s["slice_type"])
    w.ue(pps["id"])
    w.raw(sps["log2_fn"] + 4, s["frame_num"])
    if idr:
        w.ue(s["idr_id"])
    if sps["poc"] == 0:
        w.raw(sps["log2_poc"] + 4, s["lsb"])
        if pps["bottom"]:
            w.se(s["dbot"])
    elif sps["poc"] == 1 and not sps.get("always_zero", 0):
        w.se(s["d0"])
        if pps["bottom"]:
            w.se(s["d1"])
    if st == 1:
        w.raw(1, 1)                   # direct_spatial_mv_pred_flag
    if st in (0, 1):
        w.raw(1, 0)                   # num_ref_idx_active_override_flag
        w.raw(1, 0)                   # ref_pic_list_modification_flag_l0
        if st == 1:
            w.raw(1, 0)               # ..._l1
    if s["ref_idc"]:
        if idr:
            w.raw(1, 0)
            w.raw(1, 0)
        elif s["mmco5"]:
            w.raw(1, 1)               # adaptive_ref_pic_marking_mode_flag
            w.ue(5)
            w.ue(0)
        else:
            w.raw(1, 0)
    w.se(0)                           # slice_qp_delta
    for _ in range(rng.randint(8, 40)):
        w.raw(1, rng.randint(0, 1))
    return bytes([(s["ref_idc"] << 5) | (5 if idr else 1)]) + escape(w.rbsp())


# ------------------------------------------------------------------ expected POC (closed form)
def expected_poc_type1(sps, a, ref):
    """expectedPicOrderCnt of 8.2.1.2 from the UNWRAPPED count a of reference pictures before this
    picture in its period (the harness's reader uses FrameNumOffset and frame_num wrapping instead)."""
    cyc = sps.get("cycle", [])
    n = len(cyc)
    absn = a if n else 0
    if not ref and absn > 0:
        absn -= 1
    e = 0
    if absn > 0:
        c, k = divmod(absn - 1, n)
        e = c * sum(cyc) + sum(cyc[:k + 1])
    if not ref:
        e += sps.get("nonref", 0)
    return e


# ------------------------------------------------------------------ period shapes (decode order)
def pyramid(groups):
    """(type, is_ref, display index) in decode order: P at 4g, reference B at 4g-2, two non-reference B."""
    out = []
    for g in range(1, groups + 1):
        out += [("P", True, 4 * g), ("B", True, 4 * g - 2), ("B", False, 4 * g - 3), ("B", False, 4 * g - 1)]
    return out


def ipbb(groups):
    """P at 3g followed by two non-reference B (display 3g-2 and 3g-1)."""
    out = []
    for g in range(1, groups + 1):
        out += [("P", True, 3 * g), ("B", False, 3 * g - 2), ("B", False, 3 * g - 1)]
    return out


def pattern(refs):
    """Decode order = display order; True = reference P, False = non-reference P."""
    return [("P", r, i + 1) for i, r in enumerate(refs)]


# ------------------------------------------------------------------ sections
def build_section(sec, rng, state):
    """sec: dict(sps, ppss, periods, aud, sei, resend).  state carries the global NAL counter, the idr_pic_id
    counter and the output byte string.  Returns the list of picture dicts."""
    sps = sec["sps"]
    ppss = {p["id"]: p for p in sec["ppss"]}
    maxfn = 1 << (sps["log2_fn"] + 4)
    maxlsb = 1 << (sps.get("log2_poc", 0) + 4)
    sc = b"\x00\x00\x00\x01"
    pics = []

    def put(nal):
        state["out"] += sc + nal
        state["nal"] += 1

    def put_params():
        put(sps_nal(sps))
        for p in sec["ppss"]:
            put(pps_nal(p))

    put_params()
    a_next = 0
    maxT = 0
    first_period = True
    for per in sec["periods"]:
        leader, lpps = per["leader"], per.get("lpps")
        if leader == "IDR" and not first_period and sec.get("resend", False):
            put_params()
        first_period = False
        # ---- the leader picture
        if leader == "IDR":
            a, top = 0, 0
        else:                         # "MMCO5": a reference P that resets everything after it
            a = a_next
            top = {0: maxT + 4, 1: maxT + 6, 2: 2 * a}[sps["poc"]]
        body = [dict(stype="I" if leader == "IDR" else "P", ref=True, a=a, top=top, idr=(leader == "IDR"),
                     mmco5=(leader == "MMCO5"), pps=lpps, bdelta=per.get("lbdelta"),
                     key=0)]
        local_a = 1
        local_max = 0
        # ---- the body pictures: a is counted from the leader
        for stype, ref, disp in per["body"]:
            if sps["poc"] == 2:
                a_b = local_a
                top = 2 * a_b - (0 if ref else 1)
            else:
                a_b = local_a
                top = 2 * disp
            body.append(dict(stype=stype, ref=ref, a=a_b, top=top, idr=False, mmco5=False,
                             pps=rng.choice(sorted(ppss)), bdelta=None, key=top))
            if ref:
                local_a += 1
            local_max = max(local_max, top)
        # ---- write it
        for i, pc in enumerate(body):
            pps = ppss[pc["pps"]] if pc["pps"] is not None else ppss[sorted(ppss)[0]]
            pc["pps"] = pps["id"]
            ref = pc["ref"]
            top = pc["top"]
            # bottom field POC
            if sps["poc"] == 0:
                if pps["bottom"]:
                    if pc["bdelta"] is not None:
                        d = pc["bdelta"]
                    elif pc["idr"]:
                        d = rng.choice([0, 1])
                    else:
                        d = rng.choice([-1, 0, 1])
                else:
                    d = 0
                bot = top + d
                s_fields = dict(lsb=top % maxlsb, dbot=d)
            elif sps["poc"] == 1:
                e = expected_poc_type1(sps, pc["a"], ref)
                t2b = sps.get("t2b", 0)
                if sps.get("always_zero", 0):
                    top = pc["top"] = e
                    pc["key"] = top
                    bot = top + t2b
                    s_fields = {}
                else:
                    if pps["bottom"]:
                        if pc["bdelta"] is not None:
                            d1 = pc["bdelta"]
                        elif pc["idr"]:
                            d1 = rng.choice([0, 1])
                        else:
                            d1 = rng.choice([-1, 0, 1])
                    else:
                        d1 = 0
                    bot = top + t2b + d1
                    s_fields = dict(d0=top - e, d1=d1)
            else:
                bot = top
                s_fields = {}
            poc = min(top, bot)
            pc["bot"] = bot
            pc["poc"] = poc
            if pc["mmco5"]:
                pc["key"] = 0
            elif pc["idr"]:
                pc["key"] = 0
                assert poc == 0, "IDR must have POC 0"
            else:
                pc["key"] = poc
            ns = rng.choice([1, 1, 2, 3])
            idr_id = None
            if pc["idr"]:
                idr_id = state["idr_id"] = state["idr_id"] + 1
                idr_id = (idr_id - 1) % 4
            if pc["stype"] == "I":
                stv = rng.choice([2, 7]) if not pc["idr"] else 7
            elif pc["stype"] == "P":
                stv = rng.choice([0, 5])
            else:
                stv = rng.choice([1, 6])
            if sec.get("aud"):
                put(AUD)
            if sec.get("sei") and (pc["idr"] or pc["mmco5"]):
                put(SEI)
            first_nal = state["nal"]
            ref_idcs = [rng.choice([1, 2, 3]) if ref else 0 for _ in range(ns)]
            for k in range(ns):
                s = dict(idr=pc["idr"], first_mb=[0, 66, 198][k], slice_type=stv, ref_idc=ref_idcs[k],
                         frame_num=pc["a"] % maxfn, idr_id=idr_id, mmco5=pc["mmco5"], **s_fields)
                put(slice_nal(sps, pps, s, rng))
            pics.append(dict(idr=int(pc["idr"]), ref_idc=ref_idcs[0], type=pc["stype"], nslices=ns,
                             frame_num=pc["a"] % maxfn, poc=poc, top=top, bottom=bot, mmco5=int(pc["mmco5"]),
                             first_nal=first_nal, key=pc["key"],
                             new_period=int(pc["idr"] or pc["mmco5"])))
        a_next = local_a              # the next MMCO5 leader continues this counter
        maxT = local_max
    return pics


def sections(rng):
    s = []
    # S1: POC type 0, 5-bit lsb (wraps), reference B pyramid, bottom delta, two PPSs, IDR periods
    s.append(dict(
        sps=dict(id=0, log2_fn=0, poc=0, log2_poc=1),
        ppss=[dict(id=0, sps=0, bottom=1), dict(id=1, sps=0, bottom=0)],
        periods=[dict(leader="IDR", body=pyramid(9)), dict(leader="IDR", body=pyramid(6))],
        aud=True, sei=False, resend=True))
    # S2: POC type 0 with MMCO 5 (bottom delta 0, then bottom delta -2), AUD and SEI present
    s.append(dict(
        sps=dict(id=1, log2_fn=0, poc=0, log2_poc=2),
        ppss=[dict(id=2, sps=1, bottom=0), dict(id=3, sps=1, bottom=1)],
        periods=[dict(leader="IDR", body=pyramid(3)),
                 dict(leader="MMCO5", lpps=2, lbdelta=0, body=pyramid(3)),
                 dict(leader="MMCO5", lpps=3, lbdelta=-2, body=pyramid(2)),
                 dict(leader="IDR", body=pyramid(1))],
        aud=True, sei=True, resend=False))
    # S3: POC type 1, cycle of three, non-reference offset, top-to-bottom offset; no AUD at all
    s.append(dict(
        sps=dict(id=2, log2_fn=0, poc=1, always_zero=0, nonref=-4, t2b=1, cycle=[6, 4, 8]),
        ppss=[dict(id=4, sps=2, bottom=1), dict(id=5, sps=2, bottom=0)],
        periods=[dict(leader="IDR", body=ipbb(14)),
                 dict(leader="MMCO5", lpps=5, body=ipbb(4)),
                 dict(leader="IDR", body=ipbb(3))],
        aud=False, sei=False, resend=False))
    # S4: POC type 1, delta_pic_order_always_zero_flag = 1 (no delta fields at all)
    pat = [True, True, False, True, True, False, True, False, True, True, True, False] * 3
    s.append(dict(
        sps=dict(id=3, log2_fn=0, poc=1, always_zero=1, nonref=-1, t2b=1, cycle=[2, 4]),
        ppss=[dict(id=6, sps=3, bottom=0)],
        periods=[dict(leader="IDR", body=pattern(pat)), dict(leader="IDR", body=pattern(pat[:11]))],
        aud=True, sei=False, resend=False))
    # S5: POC type 1 with an EMPTY cycle: the POC is just the deltas
    s.append(dict(
        sps=dict(id=4, log2_fn=0, poc=1, always_zero=0, nonref=-2, t2b=0, cycle=[]),
        ppss=[dict(id=7, sps=4, bottom=1)],
        periods=[dict(leader="IDR", body=ipbb(7)), dict(leader="IDR", body=ipbb(2))],
        aud=False, sei=True, resend=True))
    # S6: POC type 2: two IDR pictures in a row (differ only by idr_pic_id), frame_num wrap,
    #     non-reference pictures followed by reference pictures (differ only by nal_ref_idc), MMCO 5
    pat2 = [True, True, False, True, False, True, True, True, False, True] * 3
    s.append(dict(
        sps=dict(id=5, log2_fn=0, poc=2),
        ppss=[dict(id=8, sps=5, bottom=0)],
        periods=[dict(leader="IDR", body=[]), dict(leader="IDR", body=pattern(pat2)),
                 dict(leader="MMCO5", body=pattern(pat2[:13])), dict(leader="IDR", body=pattern(pat2[:5]))],
        aud=False, sei=False, resend=False))
    return s


def make_stream(seed=7):
    rng = random.Random(seed)
    state = dict(out=b"", nal=0, idr_id=0)
    pics = []
    for sec in sections(rng):
        pics += build_section(sec, rng, state)
    return state["out"], pics


DESCRIPTION = [
    "S1 POC type 0 (5-bit lsb, wraps), pyramid with a reference B, 2 PPSs (bottom delta present / absent), "
    "AUD before every picture, 2 IDR periods",
    "S2 POC type 0 (6-bit lsb) with two MMCO 5 pictures (bottom delta 0, then -2), AUD + SEI",
    "S3 POC type 1 (cycle 6,4,8; non-ref offset -4; top-to-bottom 1), IPbb, no AUD, MMCO 5, frame_num wraps",
    "S4 POC type 1 with delta_pic_order_always_zero_flag = 1, reference and non-reference P",
    "S5 POC type 1 with an empty cycle (POC = deltas only), SEI before IDR",
    "S6 POC type 2: IDR directly after IDR, frame_num wrap, non-ref P then ref P, MMCO 5",
]

if __name__ == "__main__":
    data, pics = make_stream()
    print(len(data), len(pics))
