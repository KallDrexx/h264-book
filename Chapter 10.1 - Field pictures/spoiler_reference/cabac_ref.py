"""Reference CABAC decoding engine and context initialisation (H.264 clause 9.3.1.1, 9.3.1.2, 9.3.3.2).

Copied from the Chapter 8.1 harness (its MUTANT hooks are unused here); shares only the constant tables (tables_data.py, cross-checked against libavcodec and
libx264 by extract_tables.py) with cabac_enc.py, the independent encoder that produces the test vectors.

MUTANT: the harness's --self-test sets cabac_ref.MUTANT to a name from MUTANTS; the decoder then applies ONE deliberately
wrong rule.  Every mutant must be caught by at least one of the three checks (context dump, engine vectors, stream).
"""
import tables_data as T

MUTANT = None
MUTANTS = {   # name -> what is wrong
    "init_range_511": "codIRange starts at 511 instead of 510",
    "init_offset_8bit": "codIOffset is read with 8 bits instead of 9",
    "qrange_shift5": "qCodIRangeIdx = (codIRange >> 5) & 3",
    "lps_cell_wrong": "one entry of rangeTabLPS is wrong (state 12, column 2)",
    "trans_lps_wrong": "one entry of transIdxLPS is wrong (state 20 -> 15)",
    "no_mps_flip": "valMPS is not flipped when an LPS occurs in state 0",
    "mps_cap_63": "transIdxMPS saturates at 63 instead of 62",
    "decision_gt": "the LPS test is codIOffset > codIRange instead of >=",
    "renorm_le": "renormalisation loops while codIRange <= 256",
    "bypass_gt": "DecodeBypass compares with > instead of >=",
    "bypass_no_shift": "DecodeBypass compares codIOffset with codIRange before shifting the new bit in",
    "term_minus1": "DecodeTerminate subtracts 1 instead of 2 from codIRange",
    "term_no_renorm": "DecodeTerminate skips renormalisation when the bin is 0",
    "term_renorm_on_1": "DecodeTerminate renormalises also when the bin is 1",
    "pcm_no_align": "after a terminate bin of 1 the engine restarts at the current bit instead of the next byte boundary",
    "qp_no_clip": "SliceQPY is not clipped to 0..51 in the context initialisation",
    "pre_clip_0_127": "preCtxState is clipped to 0..127 instead of 1..126",
    "pre_shift3": "(m * qp) >> 3 instead of >> 4",
    "state_off_by_one": "pStateIdx = preCtxState - 63 for the MPS=1 case",
    "mps_threshold": "valMPS = 1 when preCtxState >= 63 (instead of > 63)",
    "idc_plus1": "P/B slices use the table of cabac_init_idc + 1 (mod 3)",
    "i_uses_idc0": "I slices use the cabac_init_idc 0 table",
    "mn_swapped": "m and n exchanged",
    "table_entry_wrong": "one (m, n) entry of the I table is wrong (ctxIdx 1000)",
    "table_entry_wrong_pb": "one (m, n) entry of the idc 2 table is wrong (ctxIdx 227)",
    "data_start_floor": "slice data is assumed to start at the byte before the aligned position (header_end_bit // 8 - 1)",
    "skip_ctx_wrong": "mb_skip_flag of a P slice uses ctxIdx 12 instead of 11",
    "imbt_ctx_wrong": "the I mb_type bin 0 uses ctxIdx 4 instead of 3",
}


def mut(name):
    return MUTANT == name


def clip3(lo, hi, x):
    return lo if x < lo else hi if x > hi else x


# ---------------------------------------------------------------- tables (mutable views)
def range_lps(p, q):
    v = T.RANGE_LPS[p][q]
    if mut("lps_cell_wrong") and p == 12 and q == 2:
        v += 1
    return v


def trans_lps(p):
    if mut("trans_lps_wrong") and p == 20:
        return 15
    return T.TRANS_LPS[p]


def trans_mps(p):
    if p >= 62:
        return 63 if (p == 63 or mut("mps_cap_63")) else 62
    return p + 1


# ---------------------------------------------------------------- 9.3.1.1 context initialisation
def mn_table(slice_type, idc):
    """(m, n) table (1024 entries) for slice_type 'I' | 'P' | 'B' (SP counts as P) and cabac_init_idc."""
    if slice_type == "I":
        t = T.CTX_I
        if mut("table_entry_wrong"):
            t = list(t)
            t[1000] = (t[1000][0] + 1, t[1000][1])
        return t
    if mut("idc_plus1"):
        idc = (idc + 1) % 3
    t = T.CTX_PB[idc]
    if mut("table_entry_wrong_pb") and idc == 2:
        t = list(t)
        t[227] = (t[227][0], t[227][1] + 1)
    return t


def defined(slice_type, ctx):
    """True if the standard gives (m, n) for this ctxIdx in this slice type: all of 0..1023 except 276 (end_of_slice_flag /
    I_PCM terminate: no context variable), and ctxIdx 11..59 only for P / B (SP) slices."""
    if ctx == 276:
        return False
    if slice_type == "I" and 11 <= ctx <= 59:
        return False
    return True


def init_state(m, n, slice_qp):
    """9.3.1.1: -> (pStateIdx, valMPS)."""
    qp = slice_qp if mut("qp_no_clip") else clip3(0, 51, slice_qp)
    if mut("mn_swapped"):
        m, n = n, m
    pre = ((m * qp) >> (3 if mut("pre_shift3") else 4)) + n
    pre = clip3(0, 127, pre) if mut("pre_clip_0_127") else clip3(1, 126, pre)
    if pre <= 63 and not (mut("mps_threshold") and pre == 63):
        return 63 - pre, 0
    return (pre - 63 if mut("state_off_by_one") else pre - 64), 1


def init_contexts(slice_type, idc, slice_qp):
    """-> list of 1024 [pStateIdx, valMPS] (None where the standard defines no value)."""
    if mut("i_uses_idc0") and slice_type == "I":
        tab = T.CTX_PB[0]
    else:
        tab = mn_table(slice_type, idc)
    return [list(init_state(m, n, slice_qp)) if defined(slice_type, c) else None for c, (m, n) in enumerate(tab)]


# ---------------------------------------------------------------- 9.3.1.2 / 9.3.3.2 decoding engine
class Engine:
    """Arithmetic decoder over `data` (bytes) starting at bit `bitpos` (byte aligned in every H.264 use)."""

    def __init__(self, data, bitpos=0):
        self.data = data
        self.start(bitpos)

    def bit(self):
        p = self.pos
        self.pos += 1
        i = p >> 3
        return (self.data[i] >> (7 - (p & 7))) & 1 if i < len(self.data) else 0

    def start(self, bitpos):
        self.pos = bitpos
        self.range = 511 if mut("init_range_511") else 510
        v = 0
        for _ in range(8 if mut("init_offset_8bit") else 9):
            v = (v << 1) | self.bit()
        self.offset = v

    def renorm(self):
        while (self.range <= 256) if mut("renorm_le") else (self.range < 256):
            self.range <<= 1
            self.offset = (self.offset << 1) | self.bit()

    def decision(self, st):
        """st = [pStateIdx, valMPS], updated in place."""
        q = ((self.range >> 5) if mut("qrange_shift5") else (self.range >> 6)) & 3
        lps = range_lps(st[0], q)
        self.range -= lps
        hit = (self.offset > self.range) if mut("decision_gt") else (self.offset >= self.range)
        if hit:
            b = 1 - st[1]
            self.offset -= self.range
            self.range = lps
            if st[0] == 0 and not mut("no_mps_flip"):
                st[1] = 1 - st[1]
            st[0] = trans_lps(st[0])
        else:
            b = st[1]
            st[0] = trans_mps(st[0])
        self.renorm()
        return b

    def bypass(self):
        if mut("bypass_no_shift"):
            b = self.bit()
            if self.offset >= self.range:
                self.offset = ((self.offset - self.range) << 1) | b
                return 1
            self.offset = (self.offset << 1) | b
            return 0
        self.offset = (self.offset << 1) | self.bit()
        if (self.offset > self.range) if mut("bypass_gt") else (self.offset >= self.range):
            self.offset -= self.range
            return 1
        return 0

    def terminate(self):
        self.range -= 1 if mut("term_minus1") else 2
        if self.offset >= self.range:
            if mut("term_renorm_on_1"):
                self.renorm()
            return 1
        if not mut("term_no_renorm"):
            self.renorm()
        return 0

    def restart_after_pcm(self, nbytes):
        """After a terminate bin of 1 (mb_type I_PCM): pcm_alignment_zero_bits up to a byte boundary, nbytes of samples, then
        the engine is initialised again (9.3.1.2).  -> byte offset where the new arithmetic code starts."""
        p = self.pos if mut("pcm_no_align") else (self.pos + 7) & ~7
        self.start(p + 8 * nbytes)
        return (p + 8 * nbytes) // 8


# ---------------------------------------------------------------- vector files (grammar in the Readme)
def parse_vectors(text):
    vecs, cur = [], None
    for ln in text.splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        w = ln.split()
        if w[0] == "VECTOR":
            cur = dict(name=w[1], init=None, ctx=[], data=b"", ops=[])
            vecs.append(cur)
        elif w[0] == "INIT":
            cur["init"] = (w[1], 0 if w[2] == "-" else int(w[2]), int(w[3]))
        elif w[0] == "CTX":
            cur["ctx"].append((int(w[1]), int(w[2]), int(w[3])))
        elif w[0] == "DATA":
            cur["data"] += bytes.fromhex("".join(w[1:]))
        elif w[0] in ("D", "B", "T", "P"):
            cur["ops"].append((w[0], int(w[1]) if len(w) > 1 else None))
    return vecs


def run_vector(v):
    """Decode one vector -> output lines (grammar in the Readme)."""
    st = {}
    if v["init"]:
        for c, s in enumerate(init_contexts(*v["init"])):
            if s is not None:
                st[c] = s
    for c, p, m in v["ctx"]:
        st[c] = [p, m]
    touched = set()
    eng = Engine(v["data"], 0)
    out = [f"VECTOR {v['name']}"]
    for op, arg in v["ops"]:
        if op == "D":
            touched.add(arg)
            out.append(f"D {arg} {eng.decision(st[arg])}")
        elif op == "B":
            out.append(f"B {eng.bypass()}")
        elif op == "T":
            b = eng.terminate()
            out.append(f"T {b} {eng.pos}")
        elif op == "P":
            out.append(f"P {eng.restart_after_pcm(arg)}")
    for c in sorted(touched):
        out.append(f"STATE {c} {st[c][0]} {st[c][1]}")
    out.append("END")
    return out


def run_ctx_cases(text):
    """text: lines 'I - 26' / 'P 1 40'.  -> 'CASE k type idc qp' then 'ctxIdx pStateIdx valMPS' per defined ctxIdx."""
    out, k = [], 0
    for ln in text.splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        t, idc, qp = ln.split()
        idc = 0 if idc == "-" else int(idc)
        out.append(f"CASE {k} {t} {'-' if t == 'I' else idc} {int(qp)}")
        for c, s in enumerate(init_contexts(t, idc, int(qp))):
            if s is not None:
                out.append(f"{c} {s[0]} {s[1]}")
        k += 1
    return out
