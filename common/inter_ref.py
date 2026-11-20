"""Independent reference parser for CAVLC P slices (Phase 7 harness truth; extends cavlc_ref.py, does not modify it).

Stdlib only.  Chapter 7.1 introduces this module; chapters 7.3 (motion vector prediction), 7.8 (B slices) and 8.x extend it
the same way 4.2-4.4 extended cavlc_ref.py: they add a LEVEL, new keys for that level, and never change what an earlier
level prints.  Slice HEADER facts come from ffmpeg's trace_headers (cavlc_ref.load_stream); this module adds the two
slice-header facts a P slice needs (num_ref_idx_l0_active, constrained_intra_pred) and re-decides which slices it parses.

LEVELS.   INTER_LEVEL = 1 (Chapter 7.1): complete syntax parsing of I and P slices, CAVLC, 8-bit 4:2:0, frames, no FMO/ASO:
  mb_skip_run, mb_type (Table 7-13, intra types at offset 5), sub_mb_type (7-17), ref_idx_l0 te(v), mvd_l0, coded_block_pattern
  (Table 9-4 inter column), transform_size_8x8_flag rules for inter macroblocks, mb_qp_delta, the residual (nC from
  neighbours: skipped / inter / intra / I_PCM), end-of-slice detection (more_rbsp_data, trailing skip run).
  Not parsed (slice status "skipped"): B slices (7.8), SP/SI, CABAC (8.x), field / MBAFF pictures.
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
EXTRA_KEYS_BY_LEVEL = {1: P_KEYS}
def keys_mb(level=1):
    """MB-line keys printed at a level: the Phase 4 keys, then this module's keys of every level <= level."""
    return (MbRecord.CORE_KEYS + ref.EXTRA_KEYS_BY_LEVEL[2] + ref.EXTRA_KEYS_BY_LEVEL[3] + ref.EXTRA_KEYS_BY_LEVEL[4]
            + [k for lv in sorted(EXTRA_KEYS_BY_LEVEL) if lv <= level for k in EXTRA_KEYS_BY_LEVEL[lv]])


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
SHAPE_CHAR = {"P_L0_16x16": " ", "P_L0_L0_16x8": "-", "P_L0_L0_8x16": "|", "P_8x8": "+", "P_8x8ref0": "+"}


# ================================================================ slice facts
def prepare(infos):
    """Add the P-slice facts to the SliceInfo list of cavlc_ref.load_stream and decide which slices are parsed here."""
    for s in infos:
        h, p = s.hdr, s.pps
        if h.get("num_ref_idx_active_override_flag", 0):
            s.num_ref_idx_l0_active = h["num_ref_idx_l0_active_minus1"] + 1
        else:
            s.num_ref_idx_l0_active = p.get("num_ref_idx_l0_default_active_minus1", 0) + 1
        s.constrained_intra_pred = p.get("constrained_intra_pred_flag", 0)
        field = h.get("field_pic_flag", 0)
        mbaff = s.sps.get("mb_adaptive_frame_field_flag", 0) and not field
        sps = s.sps
        if s.slice_type not in (0, 2):
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


def load(path, workdir):
    return prepare(ref.load_stream(path, workdir))


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
    rec.f.update(bit=bit, skip_run="-", sub="-", refidx="-", mvd="-")
    return rec


def skipped_record(addr, ctx):
    rec = new_record(addr, "-")
    rec.parts = []
    rec.f.update(mb_type="skip", name="P_Skip", i16_mode="-", chroma_cbp="-", luma_cbp="-", t8x8="-", modes="-",
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
    addr = info.first_mb
    n_pic = info.pic_size_in_mbs
    try:
        while True:
            run = None
            if is_p:
                run = br.ue()                                    # mb_skip_run
                if MUTANT == "skip_run_plus1":
                    run += 1
                if addr + run > n_pic:
                    raise ParseError(f"mb_skip_run {run} at macroblock {addr} runs past the end of the picture ({n_pic})")
                for _ in range(run):
                    rec = skipped_record(addr, ctx)
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
            if is_p and raw < 5:
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
    """The first two characters ffmpeg's macroblock map shows for a reference record."""
    n = rec.f["name"]
    if n == "P_Skip":
        return "S "
    if n in SHAPE_CHAR:
        return ">" + SHAPE_CHAR[n]
    if n == "I_NxN":
        return "i "
    if n == "I_PCM":
        return "P "
    return "I "


def check_vs_ffmpeg_cells(path, infos, results, td, match_pictures):
    """Class and partition shape of every macroblock vs ffmpeg's `-debug mb_type` map.  -> (problems, summary)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    same = all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos)
    grid = ffmpeg_cells(path, td, dims=(w0, infos[0].pic_size_in_mbs // w0) if same else None, count=npics)
    if grid is None:
        return [], "ffmpeg macroblock-map cross-check skipped: ffmpeg's log was garbled on every try"
    if len(grid) < npics:
        return [], f"ffmpeg macroblock-map cross-check skipped: ffmpeg showed {len(grid)} pictures, stream has {npics}"
    grid = grid[-npics:]
    usable, note = match_pictures(infos, grid, "ffmpeg macroblock-map cross-check")
    if usable is None:
        return [], note
    problems, n = [], 0
    for r in results:
        if r.info.picture not in usable:
            continue
        rows, w = grid[r.info.picture]["rows"], r.info.pic_width_in_mbs
        for rec in r.mbs:
            y, x = divmod(rec.addr, w)
            if y >= len(rows) or x >= len(rows[y]):
                problems.append(f"slice {r.info.index} addr {rec.addr}: ffmpeg's macroblock map has no such macroblock")
                continue
            n += 1
            e, g = expected_cell(rec), rows[y][x][:2]
            if e != g:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference says {rec.f['name']} (map cell {e!r}), "
                                f"ffmpeg's map shows {g!r}")
    return problems, f"ffmpeg macroblock map: class and partition shape of {n} macroblocks in {len(usable)} picture(s) agree{note}"


def check_vs_ffmpeg_qp(path, infos, results, td, match_pictures):
    """QP_Y of every macroblock (skipped ones included) vs ffmpeg's `-debug qp` map.  -> (problems, summary)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    h0 = infos[0].pic_size_in_mbs // w0
    if not all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos):
        return [], "ffmpeg QP cross-check skipped: pictures of different sizes"
    grid = ref.ffmpeg_qp_grid(path, td, dims=(w0, h0), count=npics)
    if grid is None or len(grid) < npics:
        return [], "ffmpeg QP cross-check skipped: ffmpeg's log was unusable"
    usable, note = match_pictures(infos, grid, "ffmpeg QP cross-check")
    if usable is None:
        return [], note
    problems, n = [], 0
    for r in results:
        if r.info.picture not in usable:
            continue
        rows = grid[r.info.picture]["rows"]
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
