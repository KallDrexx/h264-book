"""Chapter 8.1 stream-level reference: what the engine reads at the very start of every CABAC slice.

slice_line()   the expected reader line for one slice (grammar in the Readme, section 5.3)
synth_stream() a valid Annex B stream whose every slice holds ONE macroblock, built with the independent encoder
               (I_PCM macroblocks in I and P slices, P_Skip and B_Skip macroblocks), plus the expected lines
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import cabac_enc  # noqa: E402
import cabac_ref as R  # noqa: E402
from common import bitwriter  # noqa: E402

TYPE_LETTER = {0: "P", 1: "B", 2: "I", 3: "P"}      # slice_type % 5 (SP is treated like P; SI has no CABAC)


# ---------------------------------------------------------------- decoding the first macroblock (9.3.2.5 / 9.3.3.1.2)
def decode_i_mb_type(eng, st, base, in_slice_i):
    """mb_type of an I macroblock (Table 9-36).  base = ctxIdxOffset (3 in I slices, 17 for the suffix in P/B slices).
    The first macroblock of a slice has no available neighbour, so ctxIdxInc of bin 0 is 0."""
    c0 = base + (1 if R.mut("imbt_ctx_wrong") and in_slice_i else 0)
    if eng.decision(st[c0]) == 0:
        return 0                                  # I_NxN
    if eng.terminate():
        return 25                                 # I_PCM
    if in_slice_i:      # binIdx 2: +3, 3: +4, 4: +5/+6, 5: +6/+7, 6: +7
        luma = eng.decision(st[base + 3])
        chroma_nz = eng.decision(st[base + 4])
        chroma = 0
        if chroma_nz:
            chroma = 1 + eng.decision(st[base + 5])
            p1 = eng.decision(st[base + 6])
            p0 = eng.decision(st[base + 7])
        else:
            p1 = eng.decision(st[base + 6])
            p0 = eng.decision(st[base + 7])
    else:               # suffix: binIdx 2: +1, 3: +2, 4: +2/+3, 5: +3, 6: +3
        luma = eng.decision(st[base + 1])
        chroma_nz = eng.decision(st[base + 2])
        chroma = 0
        if chroma_nz:
            chroma = 1 + eng.decision(st[base + 2])
        p1 = eng.decision(st[base + 3])
        p0 = eng.decision(st[base + 3])
    return 1 + 2 * p1 + p0 + 4 * chroma + 12 * luma


def slice_line(index, stype, idc, qp, nal, header_end_bit, pcm_bytes=384):
    """Decode the first macroblock of one slice.  nal = UNESCAPED NAL unit bytes (header byte included);
    header_end_bit = bit position (from the NAL header's first bit) where slice_data() starts, i.e. AFTER the
    cabac_alignment_one_bits.  stype = slice_type % 5.  pcm_bytes = size of the pcm samples of one I_PCM macroblock
    (384 for 8-bit 4:2:0).  `eos` (end_of_slice_flag) is added when the first macroblock is skipped or I_PCM, i.e. when
    nothing but that flag can follow the first macroblock's own syntax."""
    t = TYPE_LETTER[stype]
    start = (header_end_bit + 7) // 8
    if R.mut("data_start_floor"):
        start = header_end_bit // 8 - 1
    eng = R.Engine(nal, start * 8)
    st = R.init_contexts(t, idc, qp)
    f = f"SLICE {index} {t} idc={'-' if t == 'I' else idc} qp={qp} off={eng.offset}"
    def eos_after_pcm():
        eng.restart_after_pcm(pcm_bytes)
        return f" eos={eng.terminate()}"
    if t == "I":
        v = decode_i_mb_type(eng, st, 3, True)
        return f + f" mbt={v}" + (eos_after_pcm() if v == 25 else "")
    skip_ctx = (11 + (1 if R.mut("skip_ctx_wrong") else 0)) if t == "P" else 24
    skip = eng.decision(st[skip_ctx])
    f += f" skip={skip}"
    if skip:
        return f + f" eos={eng.terminate()}"
    if t == "P":
        b0 = eng.decision(st[14])
        f += f" b0={b0}"
        if b0:
            v = decode_i_mb_type(eng, st, 17, False)
            f += f" mbt={v}" + (eos_after_pcm() if v == 25 else "")
        return f
    return f + f" b0={eng.decision(st[27])}"


# ---------------------------------------------------------------- synthetic stream
MBW, MBH = 13, 4          # 52 macroblocks per picture = one slice each; 52 = QP 0..51
PCM_BYTES = 384


def _slice_nal(kind, mb, qp, idc, pic, rng):
    """kind: 'I' | 'PCM' (in a P slice) | 'PSKIP' | 'BSKIP'.  -> (nal bytes with start code, expected-line pieces)."""
    stype = {"I": 2, "PCM": 0, "PSKIP": 0, "BSKIP": 1}[kind]
    w = bitwriter.W()
    w.ue(mb)
    w.ue(stype)
    w.ue(0)                                   # pic_parameter_set_id
    w.raw(4, pic["frame_num"])
    if pic["idr"]:
        w.ue(0)                               # idr_pic_id
    w.raw(6, pic["poc"])                      # pic_order_cnt_lsb
    if stype == 1:
        w.raw(1, pic.get("spatial", 1) if pic.get("spatial_by_mb") is None else pic["spatial_by_mb"](mb))
    if stype != 2:
        w.raw(1, 0)                           # num_ref_idx_active_override_flag
        w.raw(1, 0)                           # ref_pic_list_modification_flag_l0
        if stype == 1:
            w.raw(1, 0)                       # ..._l1
    if pic["ref_idc"]:                        # dec_ref_pic_marking
        if pic["idr"]:
            w.raw(2, 0)
        else:
            w.raw(1, 0)
    if stype != 2:
        w.ue(idc)                             # cabac_init_idc
    w.se(qp - 26)                             # slice_qp_delta
    w.ue(1)                                   # disable_deblocking_filter_idc
    while len(w.bits) % 8:
        w.raw(1, 1)                           # cabac_alignment_one_bit
    header_end_bit = 8 + len(w.bits)
    t = "I" if stype == 2 else ("P" if stype == 0 else "B")
    st = R.init_contexts(t, idc, qp)
    e = cabac_enc.Encoder()
    pcm = bytes(rng.randrange(1, 256) for _ in range(PCM_BYTES))
    if kind == "I":
        e.decision(st[3], 1)
        e.terminate(1)
    elif kind == "PCM":
        e.decision(st[11], 0)
        e.decision(st[14], 1)
        e.decision(st[17], 1)
        e.terminate(1)
    elif kind == "PSKIP":
        e.decision(st[11], 1)
    else:
        e.decision(st[24], 1)
    if kind in ("I", "PCM"):
        e.align_zero()
        e.raw_bytes(pcm)
        e.restart()
    e.terminate(1)                            # end_of_slice_flag = 1 (its flush ends in the rbsp_stop_one_bit)
    bits = list(w.bits) + e.bits
    while len(bits) % 8:
        bits.append(0)
    rbsp = bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
    hdr = (pic["ref_idc"] << 5) | (5 if pic["idr"] else 1)
    return bitwriter.nal(hdr, rbsp), dict(stype=stype, idc=idc, qp=qp, pcm=pcm if kind in ("I", "PCM") else None)


def synth_stream(seed=1):
    """-> (annexb bytes, pictures) ; pictures = list (decode order) of dict(kind per slice, pcm per mb ...)."""
    rng = random.Random(seed)
    sps = bitwriter.W()
    sps.raw(8, 77), sps.raw(8, 0), sps.raw(8, 30)
    sps.ue(0), sps.ue(0), sps.ue(0), sps.ue(2), sps.ue(2), sps.raw(1, 0)
    sps.ue(MBW - 1), sps.ue(MBH - 1), sps.raw(1, 1), sps.raw(1, 1), sps.raw(1, 0), sps.raw(1, 0)
    pps = bitwriter.W()
    pps.ue(0), pps.ue(0), pps.raw(1, 1), pps.raw(1, 0), pps.ue(0), pps.ue(0), pps.ue(0), pps.raw(1, 0), pps.raw(2, 0)
    pps.se(0), pps.se(0), pps.se(0), pps.raw(1, 1), pps.raw(1, 0), pps.raw(1, 0)
    out = bytearray()
    out += bitwriter.nal(0x67, sps.rbsp()) + bitwriter.nal(0x68, pps.rbsp())
    n = MBW * MBH
    pics = [
        dict(idr=True, ref_idc=3, frame_num=0, poc=0, kinds=["I"] * n, qp=lambda i: i, idc=lambda i: 0),
        dict(idr=False, ref_idc=2, frame_num=1, poc=8, kinds=["PSKIP" if i % 2 == 0 else "PCM" for i in range(n)],
             qp=lambda i: i, idc=lambda i: i % 3),
        dict(idr=False, ref_idc=2, frame_num=2, poc=16, kinds=["PCM" if i % 2 == 0 else "PSKIP" for i in range(n)],
             qp=lambda i: 51 - i, idc=lambda i: (i + 1) % 3),
        dict(idr=False, ref_idc=0, frame_num=3, poc=12, kinds=["BSKIP"] * n, qp=lambda i: (i * 7) % 52,
             idc=lambda i: (i + 2) % 3, spatial_by_mb=lambda i: i % 2),
    ]
    info = []
    for pic in pics:
        recs = []
        for mb in range(n):
            nal, meta = _slice_nal(pic["kinds"][mb], mb, pic["qp"](mb), pic["idc"](mb), pic, rng)
            out += nal
            meta["kind"] = pic["kinds"][mb]
            recs.append(meta)
        info.append(recs)
    return bytes(out), info


def synth_expected_lines(info):
    """Expected reader lines straight from the construction (independent of any decoding)."""
    lines, i = [], 0
    for recs in info:
        for m in recs:
            k = m["kind"]
            t = {"I": "I", "PCM": "P", "PSKIP": "P", "BSKIP": "B"}[k]
            idc = "-" if t == "I" else m["idc"]
            # off = first 9 bits of the arithmetic code: the harness derives it from the stream; the construction
            # knows only the symbols, so 'off' is not in this list (see slice_fields)
            f = f"SLICE {i} {t} idc={idc} qp={m['qp']}"
            if k == "I":
                f += " mbt=25 eos=1"
            elif k == "PCM":
                f += " skip=0 b0=1 mbt=25 eos=1"
            else:
                f += " skip=1 eos=1"
            lines.append(f)
            i += 1
    return lines


def strip_off(line):
    return " ".join(w for w in line.split() if not w.startswith("off="))
