"""Glue of the Chapter 8.5 reference: parses every slice of a stream, CAVLC (common/inter_ref.py) or CABAC (parse8.py), into ONE
record shape (the one inter_ref.py produces), so that the reconstruction (hdec.py) and the syntax dump do not care which
entropy coder a slice used; derives the intra prediction modes of every macroblock (Intra4x4PredMode / Intra8x8PredMode,
8.3.1.1 / 8.3.2.1); and renders / parses the Chapter 8.5 syntax dump.

Chapter 8.6 adds info.wm: the six + two effective weight matrices of every slice (scaling_ref.py).

Record shape (rec): .addr, .f (dict: name, mb_type, i16_mode, chroma_pred, modes ('-1,3,..' with -1 = prev flag set), cbp, t8x8
(0 / 1 / '-'), qp), .blocks (list of dict(kind, idx, maxnum, coeffs): kinds i16dc i16ac luma luma8 cbdc crdc cbac crac;
'luma8' = one 8x8 block, 64 coefficients in 8x8 zig-zag order, idx = 8x8 block index), .pcm (dict(samples) or None),
.parts (prediction blocks, inter_ref's shape).  A coded block whose coded_block_flag was 0 appears with an all-zero list.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
from common import cavlc_ref, inter_ref as ir  # noqa: E402
import parse8  # noqa: E402
import scaling_ref  # noqa: E402
import intra4ref  # noqa: E402
import intra8ref  # noqa: E402

CABAC_MUTANT = None


class Unsupported(Exception):
    pass


class Res:
    def __init__(self, info):
        self.info = info
        self.mbs = []
        self.status = "skipped"
        self.error = None
        self.note = None


class Rec:
    def __init__(self, addr):
        self.addr = addr
        self.f = {}
        self.blocks = []
        self.pcm = None
        self.parts = []
        self.stop = None


def check_stream_supported(info):
    h, p, s = info.hdr, info.pps, info.sps
    if info.slice_type in (3, 4):
        raise Unsupported("SP / SI slices")
    if h.get("field_pic_flag", 0) or (s.get("mb_adaptive_frame_field_flag", 0) and not s.get("frame_mbs_only_flag", 1)):
        raise Unsupported("field / MBAFF pictures (Phase 10)")
    if (s.get("chroma_format_idc", 1) != 1 or s.get("bit_depth_luma_minus8", 0) or s.get("bit_depth_chroma_minus8", 0)
            or s.get("separate_colour_plane_flag", 0)):
        raise Unsupported("not 8-bit 4:2:0")
    if p.get("num_slice_groups_minus1", 0):
        raise Unsupported("slice groups (FMO)")


def load(path, workdir):
    infos = ir.load(path, workdir, 3)
    raw = path if path.lower().endswith((".264", ".h264")) else os.path.join(workdir, "ref_raw.264")
    with open(raw, "rb") as fh:
        wms = scaling_ref.slice_matrices(fh.read())
    if len(wms) != len(infos):
        raise Unsupported("slice count mismatch between the scaling-list walk and the slice parser")
    for s, wm in zip(infos, wms):
        s.wm = wm
    parse8.prepare(infos)
    for s in infos:
        check_stream_supported(s)
        s.skip_reason = None
    return infos


# ---------------------------------------------------------------- CABAC record -> common record
def cabac_to_rec(m, info):
    r = Rec(m.addr)
    f = r.f
    r.parts = m.parts
    t = {0: "P", 1: "B", 2: "I"}[info.slice_type]
    if m.skip:
        f.update(mb_type="skip", name="B_Skip" if t == "B" else "P_Skip", i16_mode="-", chroma_cbp="-", luma_cbp="-", t8x8="-",
                 modes="-", chroma_pred="-", cbp="-", qp=m.qp)
        return r
    if m.intra:
        raw = m.mbt - (5 if t == "P" else 23 if t == "B" else 0)
        ti = cavlc_ref.mb_type_info(raw)
        f.update(mb_type=m.mbt, name=ti["name"], i16_mode=ti["i16_mode"], chroma_cbp=ti["chroma_cbp"], luma_cbp=ti["luma_cbp"])
        if m.pcm:
            r.pcm = dict(samples=m.pcm_samples)
            f.update(t8x8="-", modes="-", chroma_pred="-", cbp="-", qp=m.qp)
            return r
        f["chroma_pred"] = m.cpred
        if m.i_nxn:
            f["t8x8"] = m.t8
            f["modes"] = ",".join("-1" if c == "p" else c for c in m.modes)
        else:
            f["t8x8"] = "-"
            f["modes"] = f"i16:{ti['i16_mode']}"
        f["cbp"] = m.cbp_l + 16 * m.cbp_c if m.i_nxn else ti["luma_cbp"] + 16 * ti["chroma_cbp"]
    else:
        name = (ir.MB_TYPE_P[m.mbt][0] if t == "P" else ir.MB_TYPE_B[m.mbt][0])
        f.update(mb_type=m.mbt, name=name, i16_mode="-", chroma_cbp="-", luma_cbp="-", modes="-", chroma_pred="-",
                 cbp=m.cbp_l + 16 * m.cbp_c, t8x8=m.t8)
    f["qp"] = m.qp
    r.blocks = [dict(kind=b["kind"], idx=b["idx"], maxnum=b["maxnum"],
                     coeffs=list(b["coeffs"]) if b["coeffs"] is not None else [0] * b["maxnum"]) for b in m.blocks]
    return r


def cavlc_normalise(rec):
    """CAVLC with transform_size_8x8_flag = 1: the four 4x4 'luma' blocks of an 8x8 block carry its coefficients interleaved
    (coefficient 4 * i + k of the 8x8 scan sits in position i of 4x4 block k); merge them into one 'luma8' block."""
    if rec.f.get("t8x8") != 1:
        return
    out, i = [], 0
    bl = rec.blocks
    while i < len(bl):
        b = bl[i]
        if b["kind"] == "luma":
            q = b["idx"] >> 2
            quad = bl[i:i + 4]
            assert [x["idx"] for x in quad] == [4 * q + k for k in range(4)] and all(x["kind"] == "luma" for x in quad)
            co = [0] * 64
            for k, x in enumerate(quad):
                for pos, v in enumerate(x["coeffs"]):
                    co[4 * pos + k] = v
            out.append(dict(kind="luma8", idx=q, maxnum=64, coeffs=co, parts4=[x["coeffs"] for x in quad]))
            i += 4
        else:
            out.append(b)
            i += 1
    rec.blocks = out


def parse_slice(info, carry=None):
    res = Res(info)
    if info.entropy:
        try:
            parse8.MUTANT = CABAC_MUTANT
            recs, status = parse8.SliceParser(info, bool(CABAC_MUTANT), carry if carry is not None else {}).parse()
        except parse8.Unusable as e:
            res.status, res.error = "error", str(e)
            return res
        finally:
            parse8.MUTANT = None
        res.mbs = [cabac_to_rec(m, info) for m in recs]
        res.status = "complete" if status == "complete" else "error"
        res.error = None if status == "complete" else status
        return res
    sr = ir.parse_slice(info, 3)
    res.mbs = sr.mbs
    res.status, res.error, res.note = sr.status, sr.error, sr.note
    for rec in res.mbs:
        cavlc_normalise(rec)
    return res


def parse_all(infos):
    carry = {}
    return [parse_slice(i, carry) for i in infos]


# ---------------------------------------------------------------- intra prediction modes
def slice_imodes(res):
    """-> dict addr -> list of 16 modes (luma4x4BlkIdx order; an Intra 8x8 macroblock repeats each of its 4 modes four times)
    for every I_NxN macroblock of the slice (8.3.1.1 / 8.3.2.1)."""
    info = res.info
    w = info.pic_width_in_mbs
    cip = bool(info.pps.get("constrained_intra_pred_flag", 0))
    recs = {}
    out = {}
    for rec in res.mbs:
        a = rec.addr
        recs[a] = rec

        def avail(n, a=a):
            m = recs.get(n)
            if m is None:
                return False
            if cip and not (m.f["name"].startswith(("I_",))):
                return False
            return True
        av_a = a % w > 0 and avail(a - 1)
        av_b = a >= w and avail(a - w)
        if rec.f["name"] != "I_NxN":
            continue

        def mo(which, a=a):
            return out.get(a - 1 if which == "A" else a - w)
        syn = [int(v) for v in rec.f["modes"].split(",")]
        if rec.f["t8x8"] == 1:
            out[a] = intra8ref.expand8(intra8ref.derive_modes8(syn, av_a, av_b, mo))
        else:
            out[a] = intra4ref.derive_modes(syn, a, av_a, av_b, mo)
    return out


# ---------------------------------------------------------------- the syntax dump (Chapter 8.5, section 5)
def coef_string(blocks):
    out = []
    for b in blocks:
        off = 1 if b["maxnum"] == 15 else 0
        nz = [f"{k + off}={v}" for k, v in enumerate(b["coeffs"]) if v]
        out.append(f"{b['kind']}{b['idx']}:" + (",".join(nz) if nz else "-"))
    return ";".join(out) or "-"


def mb_fields(rec, imodes):
    f = rec.f
    mbt = f["mb_type"]
    nxn = f["name"] == "I_NxN"
    modes = ",".join("p" if v == "-1" else v for v in f["modes"].split(",")) if nxn else "-"
    modes = modes.replace(",", "")
    return [("mbt", str(mbt)),
            ("t8", str(f["t8x8"])),
            ("modes", modes),
            ("imode", ",".join(map(str, (imodes[rec.addr][::4] if f["t8x8"] == 1 else imodes[rec.addr]))) if nxn else "-"),
            ("cbp", str(f["cbp"])),
            ("qp", str(f["qp"])),
            ("coef", coef_string(rec.blocks))]


DUMP_KEYS = ["mbt", "t8", "modes", "imode", "cbp", "qp", "coef"]
SLICE_LETTER = {0: "P", 1: "B", 2: "I"}


def render_dump(results):
    lines = []
    for res in results:
        i = res.info
        lines.append(f"SLICE {i.index} type={SLICE_LETTER[i.slice_type]} first_mb={i.first_mb}")
        im = slice_imodes(res)
        for rec in res.mbs:
            lines.append(f"MB {i.index} {rec.addr} " + " ".join(f"{k}={v}" for k, v in mb_fields(rec, im)))
        lines.append(f"END {i.index} mbs={len(res.mbs)}")
    return "\n".join(lines) + "\n"
