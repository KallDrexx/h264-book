"""Independent reference parser for CAVLC macroblock data (Phase 4 harness truth).

Stdlib only.  Shared by the Phase 4 chapters (4.1 .. 4.4); every chapter extends it a little and
none of them breaks the dump format of the earlier ones.

WHY THIS EXISTS.  There is no JM reference decoder here and ffmpeg's trace_headers stops after the
slice header, so the truth for macroblock-level syntax has to come from a parser written for the
purpose.  To keep it honest it is validated from the outside (see the Phase 4 chapters):
  * ffmpeg's `-debug mb_type` grid (ffmpeg_mb_grid) gives the macroblock class (I4x4 / I16x16 /
    PCM) of every macroblock, from ffmpeg's own parser;
  * synthetic streams (Chapter 4.1's generator) come with a manifest written by the bit writer,
    and ffmpeg decoding them must succeed (I_PCM pixels equal the raw samples);
  * from Chapter 4.4 on, every slice must end exactly on rbsp_trailing_bits.
Slice HEADER facts (header_end_bit, slice type, picture size ...) are taken from ffmpeg's
trace_headers output, not parsed here.

STRUCTURE.  load_stream() -> list of SliceInfo (one per slice NAL).  parse_slice(info, level) walks
slice_data() macroblock by macroblock and returns a SliceResult holding one MbRecord per
macroblock.  A MbRecord is a dict-like set of named fields; render_dump(results, level) prints them.

LEVELS (how chapters 4.2-4.4 extend this file without changing earlier chapters' truth).  `level`
is the chapter's parsing capability: 1 = Chapter 4.1, 2 = 4.2, 3 = 4.3, 4 = 4.4.  Each harness passes
its own level, so Chapter 4.1's expected output (and its stop rule) stays the same after 4.2 adds
I_NxN parsing.  A later chapter (1) adds a branch for its level in finish_macroblock(), filling more
keys of rec.f, (2) lists the new keys under its level in EXTRA_KEYS_BY_LEVEL (printed on the MB line
for that level and above), and (3) relaxes level-gated rules (e.g. the 4.1 "stop after I_PCM" rule in
read_macroblock() applies at level 1 only; level 2 narrows it to "stop at a macroblock that needs a
coeff_token next to/after I_PCM"; level 3 removes it).  MAX_LEVEL is the highest level implemented.

LEVEL 2 (Chapter 4.2) in one paragraph.  Every non-PCM macroblock now has ALL syntax before its
residual() parsed and printed: transform_size_8x8_flag (I_NxN, only if the PPS enables it), the
prev/rem intra pred modes (16, or 4 with transform_size_8x8_flag) or the Intra16x16 mode from
mb_type, intra_chroma_pred_mode, coded_block_pattern (me(v), Table 9-4 intra column; for Intra16x16
it comes from mb_type), mb_qp_delta (present if CBP > 0 or Intra16x16) and the running QP_Y.  The
residual is a scaffold: a macroblock is finished only if every coeff_token it contains is a
TotalCoeff=0 token (all neighbouring total_coeff are then 0, so nC < 2; chroma DC uses nC = -1).
The first token that is not a zero token, or any token whose nC would depend on an I_PCM
neighbour, stops the slice (the MB line of that macroblock is complete anyway).

LEVEL 3 (Chapter 4.3) in one paragraph.  The scaffold is gone: every residual block of every macroblock is parsed
(common/cavlc_resid.py: nC from the neighbours' stored total_coeff, Table 9-5, levels, total_zeros, run_before), I_PCM
neighbours count 16, and a slice is finished only when its macroblocks end exactly on the rbsp_stop_one_bit (else
status=error).  Five keys are added to the MB line: nc, tc (per coded block, decoding order), tcl, tccb, tccr (the
total_coeff stored for every 4x4 block after the macroblock).  render_blocks() writes the "block boundary" file that
Chapter 4.3's reader consumes in place of the levels / total_zeros / run_before syntax it has not learnt yet.

LEVEL 4 (Chapter 4.4): no new parsing; the residual parser already reads levels, total_zeros and run_before (level 3 needed
them to find block boundaries).  Level 4 prints their result: the key `coef` (non-zero coefficients of every coded block,
see cavlc_resid.coef_string) on the MB line and `end_bit` on the END line.

DUMP FORMAT (shared by all Phase 4 chapters; see Chapter 4.1's Readme for the full grammar):
    SLICE <i> type=<I|P|B|SP|SI> first_mb=<n> pic_size_in_mbs=<n> header_end_bit=<n>
    MB <i> <addr> mb_type=<raw> name=<NAME> i16_mode=<0-3|-> chroma_cbp=<0-2|-> luma_cbp=<0|15|-> bit=<n>
    PCM <i> <addr> align_bits=<0-7> sum_y=<n> sum_cb=<n> sum_cr=<n>
    END <i> mbs=<n> status=<complete|stopped|skipped|error>
    HIST <raw>:<count> ...
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import ffref, cavlc_resid  # noqa: E402

# Highest parsing level implemented (see LEVELS above).
# 1 = Chapter 4.1 (mb_type, I_PCM, zero-residual Intra16x16 "skeleton").
# 2 = Chapter 4.2 (mb_pred modes, intra_chroma_pred_mode, coded_block_pattern, mb_qp_delta, QP chain,
#     transform_size_8x8_flag; residual only as a "zero coeff_token" scaffold).
# 4 = Chapter 4.4 (adds the coefficient arrays `coef` to the MB line and `end_bit` to the END line; the parsing itself
#     is the level-3 residual parser).
# 3 = Chapter 4.3 (neighbour availability, nC, coeff_token; the whole residual of every block is parsed so that
#     the slice runs to its end; see LEVEL 3 below and cavlc_resid.py).
MAX_LEVEL = 4

SLICE_NAMES = ["P", "B", "I", "SP", "SI"]


# ================================================================ bytes and bits
def nal_units(data):
    """Annex B scan -> list of NAL unit byte strings (start codes removed)."""
    pos = [m.start() for m in re.finditer(b"\x00\x00\x01", data)]
    out = []
    for i, p in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else len(data)
        out.append(data[p + 3:end].rstrip(b"\x00"))
    return [n for n in out if n]


def unescape(nal):
    """Remove emulation_prevention_three_byte (00 00 03 -> 00 00)."""
    out, zeros = bytearray(), 0
    for x in nal:
        if zeros >= 2 and x == 3:
            zeros = 0
            continue
        out.append(x)
        zeros = zeros + 1 if x == 0 else 0
    return bytes(out)


class ParseError(Exception):
    pass


class BitReader:
    """Reads the UNESCAPED NAL unit (NAL header byte included); pos counts bits from its start,
    the same convention as ffmpeg's trace_headers bit positions."""

    def __init__(self, data, pos=0):
        self.d, self.pos = data, pos
        self.nbits = 8 * len(data)
        last = len(data) - 1
        while last >= 0 and data[last] == 0:
            last -= 1
        if last < 0:
            self.stop_bit = -1
        else:
            b, k = data[last], 0
            while not (b >> k) & 1:
                k += 1
            self.stop_bit = 8 * last + 7 - k      # position of the rbsp_stop_one_bit

    def u(self, n):
        if self.pos + n > self.nbits:
            raise ParseError(f"read of {n} bits at bit {self.pos} runs past the end of the NAL unit")
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def peek(self, n):
        save = self.pos
        try:
            return self.u(n)
        finally:
            self.pos = save

    def ue(self):
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise ParseError(f"ue(v) with more than 32 leading zeros near bit {self.pos}")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self):
        k = self.ue()
        return (k + 1) >> 1 if k & 1 else -(k >> 1)

    def more_rbsp_data(self):
        return self.pos < self.stop_bit

    def byte_aligned(self):
        return self.pos % 8 == 0


# ================================================================ trace_headers -> slice contexts
FIELD_RE = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
PREFIX_RE = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header")


def _parse_trace(path):
    """-> list of (title, is_extradata, [(bitpos, name, nbits, value)])."""
    blocks, cur, started = [], None, False
    with open(path, "r", errors="replace") as f:
        for raw in f:
            if (raw.startswith("[") and not raw.startswith("[trace_headers")) or raw.startswith("    Last message"):
                continue
            line = PREFIX_RE.sub("", raw.rstrip("\n"))
            if line.startswith("Packet:"):
                started, cur = True, None
                continue
            m = FIELD_RE.match(line)
            if m and cur is not None:
                cur[2].append((int(m.group(1)), m.group(2), len(m.group(3)), int(m.group(4))))
            elif line in TITLES:
                cur = (line, not started, [])
                blocks.append(cur)
            else:
                cur = None
    return blocks


def _first(items):
    d = {}
    for _, n, _, v in items:
        d.setdefault(n, v)
    return d


class SliceInfo:
    """Everything the macroblock parser needs to know about one slice NAL unit."""
    index = 0            # ordinal among all slice NAL units (types 1 and 5), stream order
    picture = 0          # ordinal of the picture this slice belongs to
    nal = b""            # UNESCAPED NAL unit bytes, NAL header included
    nal_type = 0
    slice_type = 0       # slice_type % 5
    first_mb = 0         # first_mb_in_slice * (1 + MbaffFrameFlag)
    pic_size_in_mbs = 0
    header_end_bit = 0   # bit position (from the NAL header's first bit) where slice_data() starts
    pic_width_in_mbs = 0
    entropy = 0
    slice_qp = 26        # SliceQPY = 26 + pic_init_qp_minus26 + slice_qp_delta
    transform_8x8_mode = 0   # PPS transform_8x8_mode_flag (0 when the PPS has no extension)
    skip_reason = None   # None if this chapter's parser handles the slice, else a short text
    sps = None           # dict of first values of the SPS fields (trace names)
    pps = None
    hdr = None           # dict of first values of the slice header fields


def load_stream(path, workdir):
    """Return list of SliceInfo for a .ts or raw Annex B (.264) file.  Needs ffmpeg."""
    ffref.require_tools()
    raw = path
    if not path.lower().endswith((".264", ".h264")):
        raw = os.path.join(workdir, "ref_raw.264")
        ffref.raw_h264(path, raw)
    nals = [unescape(n) for n in nal_units(open(raw, "rb").read()) if n[0] & 31 in (1, 5)]
    trace = os.path.join(workdir, "ref_trace.txt")
    if path.lower().endswith((".264", ".h264")):
        with open(trace, "wb") as f:
            subprocess.run(["ffmpeg", "-hide_banner", "-f", "h264", "-i", path, "-c:v", "copy", "-bsf:v",
                            "trace_headers", "-f", "null", "-"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=f, timeout=300, check=False)
    else:
        ffref.trace_headers(path, trace)
    sps_t, pps_t, hdrs = {}, {}, []
    for title, extradata, items in _parse_trace(trace):
        d = _first(items)
        if title == "Sequence Parameter Set":
            sps_t[d["seq_parameter_set_id"]] = d
        elif title == "Picture Parameter Set":
            pps_t[d["pic_parameter_set_id"]] = d
        elif not extradata and d.get("nal_unit_type") in (1, 5):
            pps = pps_t.get(d["pic_parameter_set_id"])
            sps = sps_t.get(pps["seq_parameter_set_id"]) if pps else None
            if sps is None:
                raise SystemExit("input problem: a slice refers to a PPS/SPS that is not in the stream")
            last = items[-1]
            hdrs.append((d, sps, pps, last[0] + last[2]))
    if len(hdrs) != len(nals):
        raise SystemExit(f"input problem: {len(nals)} slice NAL units but {len(hdrs)} slice headers in the trace")
    out, pic, prev_first = [], -1, None
    for i, (nal, (d, sps, pps, hend)) in enumerate(zip(nals, hdrs)):
        s = SliceInfo()
        s.index, s.nal, s.nal_type, s.sps, s.pps, s.hdr = i, nal, nal[0] & 31, sps, pps, d
        s.slice_type = d["slice_type"] % 5
        field = d.get("field_pic_flag", 0)
        fmo = sps.get("frame_mbs_only_flag", 1)
        mbaff = 1 if sps.get("mb_adaptive_frame_field_flag", 0) and not field else 0
        s.pic_width_in_mbs = sps["pic_width_in_mbs_minus1"] + 1
        frame_h = (2 - fmo) * (sps["pic_height_in_map_units_minus1"] + 1)
        s.pic_size_in_mbs = s.pic_width_in_mbs * (frame_h // (1 + field))
        s.first_mb = d["first_mb_in_slice"] * (1 + mbaff)
        s.header_end_bit = hend
        s.entropy = pps["entropy_coding_mode_flag"]
        s.slice_qp = 26 + pps["pic_init_qp_minus26"] + d["slice_qp_delta"]
        s.transform_8x8_mode = pps.get("transform_8x8_mode_flag", 0)
        if prev_first is None or s.first_mb <= prev_first:
            pic += 1                       # no ASO/FMO: a new picture starts when first_mb does not increase
        prev_first = s.first_mb
        s.picture = pic
        if s.slice_type != 2:
            s.skip_reason = "not an I slice (P/B/SP/SI slices come in later phases)"
        elif s.entropy:
            s.skip_reason = "CABAC slice"
        elif field or mbaff:
            s.skip_reason = "field picture or MBAFF"
        elif (sps.get("chroma_format_idc", 1) != 1 or sps.get("bit_depth_luma_minus8", 0)
              or sps.get("bit_depth_chroma_minus8", 0) or sps.get("separate_colour_plane_flag", 0)):
            s.skip_reason = "not 8-bit 4:2:0"
        out.append(s)
    return out


# ================================================================ macroblock records
class MbRecord:
    """Named fields of one macroblock.  Core keys are printed on the MB line in this order;
    chapters 4.2+ append more keys (see EXTRA_KEYS_BY_LEVEL)."""
    CORE_KEYS = ["mb_type", "name", "i16_mode", "chroma_cbp", "luma_cbp", "bit"]

    def __init__(self, addr):
        self.addr = addr
        self.f = {}            # all fields by name; '-' means "does not apply"
        self.pcm = None        # dict(align_bits, sum_y, sum_cb, sum_cr) for I_PCM
        self.stop = None       # None, or why this macroblock could not be completed


# key names added by later chapters, in order of appearance on the MB line
EXTRA_KEYS_BY_LEVEL = {
    1: [],   # 4.1: nothing extra (chroma_pred_mode / mb_qp_delta are stored in .f but not printed)
    2: ["t8x8", "modes", "chroma_pred", "cbp", "qp_delta", "qp"],   # 4.2 (see Chapter 4.2's Readme, section 5)
    3: ["nc", "tc", "tcl", "tccb", "tccr"],                          # 4.3 (see Chapter 4.3's Readme, section 5)
    4: ["coef"],                                                     # 4.4 (see Chapter 4.4's Readme, section 5)
}

# Table 9-4, column Intra_4x4/Intra_8x8, ChromaArrayType 1 or 2: codeNum -> coded_block_pattern
# (value = CodedBlockPatternLuma + 16 * CodedBlockPatternChroma).  Typed from the standard.
CBP_INTRA = [47, 31, 15, 0, 23, 27, 29, 30, 7, 11, 13, 14, 39, 43, 45, 46,
             16, 3, 5, 10, 12, 19, 21, 26, 28, 35, 37, 42, 44, 1, 2, 4,
             8, 17, 18, 20, 24, 6, 9, 22, 25, 32, 33, 34, 36, 40, 38, 41]
# (For reference, not used here: the Inter column, and the ChromaArrayType 0 / 3 columns.)
CBP_INTER = [0, 16, 1, 2, 4, 8, 32, 3, 5, 10, 12, 15, 47, 7, 11, 13,
             14, 6, 9, 31, 35, 37, 42, 44, 33, 34, 36, 40, 39, 43, 45, 46,
             17, 18, 20, 24, 19, 21, 26, 28, 23, 27, 29, 30, 22, 25, 38, 41]
CBP_INTRA_MONO = [15, 0, 7, 11, 13, 14, 3, 5, 10, 12, 1, 2, 4, 8, 6, 9]
CBP_INTER_MONO = [0, 1, 2, 4, 8, 3, 5, 10, 12, 15, 7, 11, 13, 14, 6, 9]


def mb_type_info(raw):
    """Table 7-11 for I slices.  -> dict(name, kind, i16_mode, chroma_cbp, luma_cbp)."""
    if raw == 0:
        return dict(name="I_NxN", kind="nxn", i16_mode="-", chroma_cbp="-", luma_cbp="-")
    if raw == 25:
        return dict(name="I_PCM", kind="pcm", i16_mode="-", chroma_cbp="-", luma_cbp="-")
    if 1 <= raw <= 24:
        t = raw - 1
        mode, cc, lf = t % 4, (t // 4) % 3, 1 if t >= 12 else 0
        return dict(name=f"I_16x16_{mode}_{cc}_{lf}", kind="i16", i16_mode=mode, chroma_cbp=cc,
                    luma_cbp=15 if lf else 0)
    raise ParseError(f"mb_type {raw} is not valid in an I slice (0..25)")


class SliceResult:
    def __init__(self, info):
        self.info = info
        self.mbs = []
        self.status = "skipped"      # complete | stopped | skipped | error
        self.error = None
        self.note = info.skip_reason
        self.end_bit = None          # bit position where the last macroblock ended (complete slices: the stop bit)


# ================================================================ the macroblock parser
def read_pcm(br, rec):
    """pcm_alignment_zero_bits + 256 luma + 2*64 chroma samples (8-bit 4:2:0)."""
    align = (-br.pos) % 8
    if align and br.u(align) != 0:
        raise ParseError(f"pcm_alignment_zero_bit is 1 near bit {br.pos}")
    if br.pos + 384 * 8 > br.nbits:
        raise ParseError("I_PCM samples run past the end of the NAL unit")
    p = br.pos >> 3
    d = br.d
    rec.pcm = dict(align_bits=align, sum_y=sum(d[p:p + 256]), sum_cb=sum(d[p + 256:p + 320]),
                   sum_cr=sum(d[p + 320:p + 384]), samples=bytes(d[p:p + 384]))
    br.pos += 384 * 8


def finish_macroblock(br, st, rec, kind, level=1, ctx=None):
    """Everything in macroblock_layer() after mb_type, for non-PCM macroblocks.

    Chapter 4.1: only the zero-residual Intra16x16 'skeleton' is understood:
        intra_chroma_pred_mode ue(v), mb_qp_delta se(v), Intra16x16DCLevel coeff_token == '1'
    (TotalCoeff 0 with nC < 2; nC is 0 because only skeletons precede it in the slice).
    Chapters 4.2-4.4 add branches for their level (full mb_pred / CBP / residual parsing) and keep
    the level-1 branch unchanged.  Sets rec.stop to a reason string if the macroblock could not be
    completed at this level; leaves it None when the macroblock is complete."""
    if level > MAX_LEVEL:
        raise ValueError(f"cavlc_ref implements levels 1..{MAX_LEVEL}, not {level}")
    if level >= 2:
        finish_macroblock_l2(br, st, rec, kind, ctx if ctx is not None else dict(qp=st.slice_qp, seen_pcm=False))
        return
    if kind == "i16" and rec.f["chroma_cbp"] == 0 and rec.f["luma_cbp"] == 0:
        rec.f["chroma_pred_mode"] = br.ue()
        if rec.f["chroma_pred_mode"] > 3:
            raise ParseError(f"intra_chroma_pred_mode {rec.f['chroma_pred_mode']} > 3")
        rec.f["mb_qp_delta"] = br.se()
        if br.peek(1) == 1:
            br.u(1)
            return
        rec.stop = "Intra16x16 DC coefficients present (needs residual parsing)"
        return
    rec.stop = "macroblock needs mb_pred / residual syntax (not in this chapter)"


def finish_macroblock_l2(br, st, rec, kind, ctx):
    """Chapter 4.2: macroblock_layer() after mb_type for a non-PCM macroblock (7.3.5, 7.3.5.1).
    ctx: dict(qp=running QP_Y of the slice, seen_pcm=bool)."""
    f = rec.f
    # transform_size_8x8_flag (only I_NxN here) and mb_pred()
    if kind == "nxn":
        t8 = br.u(1) if st.transform_8x8_mode else 0
        modes = []
        for _ in range(4 if t8 else 16):
            modes.append(-1 if br.u(1) else br.u(3))      # prev flag 1 -> -1, else rem_intra_pred_mode
        f["t8x8"] = t8
        f["modes"] = ",".join(str(m) for m in modes)
    else:
        f["t8x8"] = "-"
        f["modes"] = f"i16:{f['i16_mode']}"
    cpm = br.ue()
    if cpm > 3:
        raise ParseError(f"intra_chroma_pred_mode {cpm} > 3")
    f["chroma_pred"] = cpm
    # coded_block_pattern: me(v) for I_NxN, packed in mb_type for Intra16x16
    if kind == "nxn":
        code = br.ue()
        if code > 47:
            raise ParseError(f"coded_block_pattern codeNum {code} > 47")
        cbp = CBP_INTRA[code]
    else:
        cbp = f["luma_cbp"] + 16 * f["chroma_cbp"]
    f["cbp"] = cbp
    # mb_qp_delta and the QP_Y chain (7.4.5, equation 7-37 with QpBdOffset = 0)
    if cbp > 0 or kind == "i16":
        d = br.se()
        if not -26 <= d <= 25:
            raise ParseError(f"mb_qp_delta {d} outside -26..25")
        f["qp_delta"] = d
        ctx["qp"] = (ctx["qp"] + d + 52) % 52
    else:
        f["qp_delta"] = "-"                               # not present: inferred 0, QP_Y = QP_Y,PRED
    f["qp"] = ctx["qp"]
    if ctx.get("res3") is not None:                       # level 3: the real residual
        ctx["res3"].macroblock(br, rec, kind, cbp)
        return
    # residual(): scaffold (see the module docstring)
    luma, chroma = cbp & 15, cbp >> 4
    if kind != "i16" and cbp == 0:
        return                                            # no coded block at all: nothing to read
    if ctx["seen_pcm"]:
        rec.stop = "coeff_token after an I_PCM macroblock needs neighbour-based nC (Chapter 4.3)"
        return
    # order of coded blocks and the zero coeff_token of each (nC = 0 -> '1', chroma DC nC = -1 -> '01')
    plan = []
    if kind == "i16":
        plan.append(1)                                    # Intra16x16DCLevel
        plan += [1] * (16 if luma else 0)                 # Intra16x16ACLevel
    else:
        for q in range(4):
            if luma >> q & 1:
                plan += [1] * 4                           # four 4x4 blocks per 8x8 quadrant (also with 8x8 transform in CAVLC)
    if chroma:
        plan += [2, 2]                                    # Cb DC, Cr DC
    if chroma == 2:
        plan += [1] * 8                                   # Cb AC x4, Cr AC x4
    for want in plan:
        if want == 1:
            if br.u(1) != 1:
                rec.stop = "a coeff_token is not the TotalCoeff=0 token: the block has coefficients (Chapter 4.3/4.4)"
                return
        elif br.u(2) != 1:
            rec.stop = "a chroma DC coeff_token is not the TotalCoeff=0 token (Chapter 4.3/4.4)"
            return


def read_macroblock(br, st, addr, seen_pcm, level=1, ctx=None):
    rec = MbRecord(addr)
    start = br.pos
    raw = br.ue()
    info = mb_type_info(raw)
    rec.f.update(mb_type=raw, name=info["name"], i16_mode=info["i16_mode"], chroma_cbp=info["chroma_cbp"],
                 luma_cbp=info["luma_cbp"], bit=start)
    if info["kind"] == "pcm":
        read_pcm(br, rec)
    elif seen_pcm and level == 1:
        # nC for coeff_token would depend on I_PCM neighbours (total_coeff 16): Chapter 4.3
        rec.stop = "non-PCM macroblock after an I_PCM macroblock needs neighbour-based nC (Chapter 4.3)"
    else:
        finish_macroblock(br, st, rec, info["kind"], level, ctx)
    return rec


def parse_slice(info, level=1):
    """slice_data() for an I slice, CAVLC (7.3.4).  -> SliceResult."""
    res = SliceResult(info)
    if info.skip_reason:
        return res
    br = BitReader(info.nal, info.header_end_bit)
    addr, seen_pcm = info.first_mb, False
    ctx = dict(qp=info.slice_qp, seen_pcm=False)
    if level >= 3:
        ctx["res3"] = cavlc_resid.SliceResidual(info.pic_width_in_mbs)
    try:
        while True:
            if addr >= info.pic_size_in_mbs:
                raise ParseError(f"macroblock address {addr} is beyond PicSizeInMbs {info.pic_size_in_mbs}")
            ctx["seen_pcm"] = seen_pcm
            rec = read_macroblock(br, info, addr, seen_pcm, level, ctx)
            res.mbs.append(rec)
            if rec.pcm is not None:
                seen_pcm = True
                if level >= 2:                        # an I_PCM macroblock has no mb_qp_delta: QP_Y is inherited
                    rec.f.update(t8x8="-", modes="-", chroma_pred="-", cbp="-", qp_delta="-", qp=ctx["qp"])
                if level >= 3:
                    ctx["res3"].pcm(addr)
                    rec.blocks = []
                    rec.f.update(nc="-", tc="-", coef="-")
                    ctx["res3"].stored(rec)
            if level >= 3 and br.pos > br.stop_bit:
                raise ParseError(f"macroblock {addr} ends at bit {br.pos}, after the rbsp_stop_one_bit at bit {br.stop_bit}")
            if rec.stop:
                res.status, res.note = "stopped", rec.stop
                return res
            if not br.more_rbsp_data():          # moreDataFlag = more_rbsp_data()
                res.status = "complete"
                res.end_bit = br.pos
                return res
            addr += 1                            # no FMO: NextMbAddress is addr + 1
    except ParseError as e:
        res.status, res.error = "error", str(e)
        return res


# ================================================================ dump rendering
def mb_line(slice_index, rec, level=1):
    keys = MbRecord.CORE_KEYS + [k for lv in sorted(EXTRA_KEYS_BY_LEVEL) if lv <= level
                                 for k in EXTRA_KEYS_BY_LEVEL[lv]]
    return f"MB {slice_index} {rec.addr} " + " ".join(f"{k}={rec.f[k]}" for k in keys)


def render_slice(res, level=1):
    i = res.info
    out = [f"SLICE {i.index} type={SLICE_NAMES[i.slice_type]} first_mb={i.first_mb} "
           f"pic_size_in_mbs={i.pic_size_in_mbs} header_end_bit={i.header_end_bit}"]
    for rec in res.mbs:
        out.append(mb_line(i.index, rec, level))
        if rec.pcm is not None:
            p = rec.pcm
            out.append(f"PCM {i.index} {rec.addr} align_bits={p['align_bits']} sum_y={p['sum_y']} "
                       f"sum_cb={p['sum_cb']} sum_cr={p['sum_cr']}")
    extra = ""
    if level >= 4:                    # Chapter 4.4: where the last macroblock ended (the rbsp_stop_one_bit of a complete slice)
        extra = f" end_bit={res.end_bit if res.status == 'complete' else '-'}"
    out.append(f"END {i.index} mbs={len(res.mbs)} status={res.status}{extra}")
    return out


def render_blocks(results):
    """The block boundary file of Chapter 4.3 (level 3): one BLK line per coded residual block, decoding order.
        SLICE <slice_index> blocks=<n>
        BLK <slice_index> <mb_addr> <kind> <idx> start=<bit> end=<bit>
    kind: i16dc luma i16ac cbdc crdc cbac crac; idx: luma4x4BlkIdx (0-15) or chroma4x4BlkIdx (0-3), 0 for DC blocks;
    start = first bit of the block's coeff_token, end = first bit after the block (= start of the next block, or
    of the next macroblock), both counted from the first bit of the NAL unit header."""
    out = []
    for r in results:
        if r.status in ("skipped",):
            continue
        blks = [(rec.addr, b) for rec in r.mbs for b in getattr(rec, "blocks", [])]
        out.append(f"SLICE {r.info.index} blocks={len(blks)}")
        for addr, b in blks:
            out.append(f"BLK {r.info.index} {addr} {b['kind']} {b['idx']} start={b['start']} end={b['end']}")
    return "\n".join(out) + "\n"


def histogram(results):
    h = {}
    for r in results:
        for rec in r.mbs:
            h[rec.f["mb_type"]] = h.get(rec.f["mb_type"], 0) + 1
    return h


def hist_line(h):
    return "HIST" + "".join(f" {k}:{h[k]}" for k in sorted(h))


def render_dump(results, level=1):
    out = []
    for r in results:
        out += render_slice(r, level)
    out.append(hist_line(histogram(results)))
    return "\n".join(out) + "\n"


# ================================================================ ffmpeg's own view (cross-check)
# A map row is "<y*16> <cell><cell>..." (3 characters per cell).  Usually prefixed with "[h264 @ 0x..] ",
# but ffmpeg's other threads sometimes eat the prefix ("in[0] 'default': 224 I  I ..."), so the row is found
# by its row label anywhere in the line (see _ffmpeg_mb_grid_once).
GRID_ROW = re.compile(r"(\d+) (\D*\S\D*)$")
FRAME_LINE = re.compile(r"New frame, type: (\w)")
CLASS_OF_CHAR = {"i": "I_NxN", "I": "I_16x16", "P": "I_PCM"}


def ffmpeg_mb_grid(path, workdir, timeout=300, dims=None, tries=4, count=None):
    """Run ffmpeg with -debug mb_type and parse its macroblock map.
    -> list of pictures in OUTPUT order: dict(type='I'|'P'|'B', rows=[[cell char, ...], ...]).
    (-v repeat+debug: without "repeat" ffmpeg collapses identical log lines into "Last message repeated".)
    NOTE: ffmpeg's stream probing decodes the first few frames once before the real decode starts,
    so the list begins with duplicates; callers that know the picture count n take the LAST n.
    count=n returns only the last n pictures (the real decode).  dims=(mb_width, mb_height) lets the
    function detect a damaged log and retry; returns None if every try was damaged.  The cell character is ffmpeg's first character per macroblock: 'i' Intra4x4, 'I' Intra16x16,
    'P' PCM, anything else is an inter class ('S' skip, 'd' direct, '<' '>' 'X' ...)."""
    for _ in range(tries):
        pics = _ffmpeg_mb_grid_once(path, workdir, timeout)
        if count is not None:
            if len(pics) < count:
                return pics
            pics = pics[-count:]
        if dims is None or all(len(p["rows"]) == dims[1] and all(len(r) == dims[0] for r in p["rows"])
                               for p in pics):
            return pics
        # ffmpeg's decoder thread and its main thread log concurrently; a message from the other
        # thread can land in the middle of a grid row.  Rare; simply run again.
    return None


def _ffmpeg_mb_grid_once(path, workdir, timeout):
    log = os.path.join(workdir, "grid.log")
    cmd = ["ffmpeg", "-hide_banner", "-v", "repeat+debug", "-threads", "1", "-debug", "mb_type"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-f", "null", "-"]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f,
                       timeout=timeout, check=False)
    pics = []
    with open(log, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = FRAME_LINE.search(line)
            if m:
                pics.append(dict(type=m.group(1), rows=[]))
                continue
            if not pics:
                continue
            rows = pics[-1]["rows"]
            for m in GRID_ROW.finditer(line):
                if int(m.group(1)) == 16 * len(rows):     # the next expected row label
                    rows.append(list(m.group(2)[0::3]))
                    break
    return pics


QP_FRAME = re.compile(r"New frame, type: (\w)")


def ffmpeg_qp_grid(path, workdir, timeout=300, dims=None, tries=4, count=None):
    """Run ffmpeg with -debug qp and parse its per-macroblock QP map (every cell is two characters, the
    QP_Y that ffmpeg stored for the macroblock; I_PCM macroblocks show 0 there, the value the deblocking
    filter uses (8.7.2.2), not the inherited QP_Y of 7.4.5).  Same conventions as ffmpeg_mb_grid:
    -> list of pictures in output order, dict(type, rows=[[int, ...], ...]), last `count` only."""
    for _ in range(tries):
        pics = _ffmpeg_qp_grid_once(path, workdir, timeout, dims)
        if count is not None:
            if len(pics) < count:
                return pics
            pics = pics[-count:]
        if dims is None or all(len(p["rows"]) == dims[1] for p in pics):
            return pics
    return None


def _ffmpeg_qp_grid_once(path, workdir, timeout, dims):
    log = os.path.join(workdir, "qpgrid.log")
    cmd = ["ffmpeg", "-hide_banner", "-v", "repeat+debug", "-threads", "1", "-debug", "qp"]
    if path.lower().endswith((".264", ".h264")):
        cmd += ["-f", "h264"]
    cmd += ["-i", path, "-map", "0:v:0", "-f", "null", "-"]
    with open(log, "wb") as f:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=f,
                       timeout=timeout, check=False)
    pics, width = [], dims[0] if dims else None
    with open(log, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = QP_FRAME.search(line)
            if m:
                pics.append(dict(type=m.group(1), rows=[]))
                continue
            if not pics or width is None:
                continue
            rows = pics[-1]["rows"]
            # "<label> <2*width characters>": the label is the next expected row (16 * row index)
            tail = line[-(2 * width):]
            head = line[:-(2 * width)].rstrip()
            if len(tail) == 2 * width and head.endswith(str(16 * len(rows))) \
                    and re.fullmatch(r"(?:[ \d]\d){%d}" % width, tail):
                rows.append([int(tail[2 * i:2 * i + 2]) for i in range(width)])
    return pics
