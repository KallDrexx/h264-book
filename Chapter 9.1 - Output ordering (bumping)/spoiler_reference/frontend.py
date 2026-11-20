"""SPOILER: header front end for Chapter 9.1.  Annex B bytes -> one record per decoded frame picture, with everything the
output-order (bumping) process needs: POC (8.2.1), IDR / MMCO 5 / reference flags, no_output_of_prior_pics_flag, the pictures
whose reference marking ends with this picture (8.2.5), and the SPS facts that size the DPB (C.4 / Table A-1).

It parses headers only (no macroblock data), so it is fast on any stream.  Frame pictures only (field pictures and gaps in
frame_num raise Unsupported).  Also provides the SPS rewriter used by the synthetic-stream generator.

    pics = load_pictures(annexb_bytes)     -> list of dict (see PIC_KEYS)
    split_nals(data) -> [(start, end)] of NAL units (without start code); unescape(nal) / escape(rbsp)
"""

PIC_KEYS = ("id", "poc", "idr", "mmco5", "ref", "noout", "unref", "profile", "level", "cs3", "wmbs", "hmbs", "mdfb", "nref")


class Unsupported(Exception):
    pass


def split_nals(data):
    out, i, n = [], 0, len(data)
    starts = []
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while e > s and data[e - 1] == 0:
            e -= 1
        if e > s:
            out.append((s, e))
    return out


def unescape(b):
    out, z = bytearray(), 0
    for c in b:
        if z >= 2 and c == 3:
            z = 0
            continue
        out.append(c)
        z = z + 1 if c == 0 else 0
    return bytes(out)


def escape(b):
    out, z = bytearray(), 0
    for c in b:
        if z >= 2 and c <= 3:
            out.append(3)
            z = 0
        out.append(c)
        z = z + 1 if c == 0 else 0
    return bytes(out)


class BitReader:
    def __init__(self, data):
        self.d, self.pos = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.d[self.pos >> 3] if (self.pos >> 3) < len(self.d) else 0
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 32:
                raise Unsupported("bad Exp-Golomb code")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, n, v):
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def ue(self, v):
        v += 1
        n = v.bit_length()
        self.u(n - 1, 0)
        self.u(n, v)

    def copy_bits(self, data, count):
        for p in range(count):
            self.bits.append((data[p >> 3] >> (7 - (p & 7))) & 1)

    def finish(self):
        self.bits.append(1)
        while len(self.bits) % 8:
            self.bits.append(0)
        return bytes(int("".join(map(str, self.bits[i:i + 8])), 2) for i in range(0, len(self.bits), 8))


def _hrd(r):
    n = r.ue() + 1
    r.u(4), r.u(4)
    for _ in range(n):
        r.ue(), r.ue(), r.u(1)
    r.u(5), r.u(5), r.u(5), r.u(5)


def _scaling_list(r, size):
    last = nxt = 8
    for _ in range(size):
        if nxt:
            nxt = (last + r.se()) % 256
        last = nxt if nxt else last


def parse_sps(rbsp):
    """rbsp = NAL payload without the 1-byte header, unescaped.  -> dict; 'vui_br_pos' = bit position of
    bitstream_restriction_flag (None without VUI), 'mdfb' / 'num_reorder' = VUI values or None."""
    r = BitReader(rbsp)
    s = {}
    s["profile"] = r.u(8)
    cs = r.u(8)
    s["cs3"] = (cs >> 4) & 1
    s["level"] = r.u(8)
    s["id"] = r.ue()
    s["chroma_format_idc"] = 1
    if s["profile"] in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        s["chroma_format_idc"] = r.ue()
        if s["chroma_format_idc"] == 3:
            if r.u(1):
                raise Unsupported("separate_colour_plane_flag")
        r.ue(), r.ue(), r.u(1)
        if r.u(1):
            for i in range(8 if s["chroma_format_idc"] != 3 else 12):
                if r.u(1):
                    _scaling_list(r, 16 if i < 6 else 64)
    s["log2_max_fn"] = r.ue() + 4
    s["poc_type"] = r.ue()
    s["offsets"] = []
    if s["poc_type"] == 0:
        s["log2_max_lsb"] = r.ue() + 4
    elif s["poc_type"] == 1:
        s["always_zero"] = r.u(1)
        s["off_nonref"] = r.se()
        s["off_t2b"] = r.se()
        n = r.ue()
        s["offsets"] = [r.se() for _ in range(n)]
    s["nref"] = r.ue()
    s["gaps"] = r.u(1)
    s["wmbs"] = r.ue() + 1
    mapunits = r.ue() + 1
    s["frame_mbs_only"] = r.u(1)
    if not s["frame_mbs_only"]:
        s["mbaff"] = r.u(1)
    s["hmbs"] = (2 - s["frame_mbs_only"]) * mapunits
    r.u(1)
    s["crop"] = (0, 0, 0, 0)
    if r.u(1):
        s["crop"] = tuple(r.ue() for _ in range(4))      # left right top bottom
    s["vui_br_pos"] = None
    s["mdfb"] = s["num_reorder"] = None
    if r.u(1):
        if r.u(1):
            if r.u(8) == 255:
                r.u(16), r.u(16)
        if r.u(1):
            r.u(1)
        if r.u(1):
            r.u(4)
            if r.u(1):
                r.u(24)
        if r.u(1):
            r.ue(), r.ue()
        if r.u(1):
            r.u(32), r.u(32), r.u(1)
        nal_h = r.u(1)
        if nal_h:
            _hrd(r)
        vcl_h = r.u(1)
        if vcl_h:
            _hrd(r)
        if nal_h or vcl_h:
            r.u(1)
        r.u(1)
        s["vui_br_pos"] = r.pos
        if r.u(1):
            r.u(1), r.ue(), r.ue(), r.ue(), r.ue()
            s["num_reorder"] = r.ue()
            s["mdfb"] = r.ue()
    return s


def rewrite_sps_restriction(nal, num_reorder, mdfb):
    """nal = escaped NAL unit bytes (header byte included).  Returns a new SPS NAL whose VUI bitstream_restriction is
    replaced: num_reorder / mdfb None -> no bitstream_restriction at all (the DPB size then comes from the level).
    The SPS must already contain a VUI."""
    rbsp = unescape(nal[1:])
    s = parse_sps(rbsp)
    if s["vui_br_pos"] is None:
        raise Unsupported("SPS without VUI")
    w = BitWriter()
    w.copy_bits(rbsp, s["vui_br_pos"])
    if num_reorder is None:
        w.u(1, 0)
    else:
        w.u(1, 1)
        w.u(1, 1)
        w.ue(0), w.ue(0), w.ue(16), w.ue(16)
        w.ue(num_reorder), w.ue(mdfb)
    return nal[:1] + escape(w.finish())


def parse_pps(rbsp):
    r = BitReader(rbsp)
    p = {"id": r.ue(), "sps": r.ue()}
    p["cabac"] = r.u(1)
    p["bottom_poc"] = r.u(1)
    if r.ue():
        raise Unsupported("slice groups (FMO)")
    p["l0"] = r.ue() + 1
    p["l1"] = r.ue() + 1
    p["wp"] = r.u(1)
    p["wbi"] = r.u(2)
    r.se(), r.se(), r.se()
    r.u(1), r.u(1)
    p["redundant"] = r.u(1)
    return p


def parse_slice_header(rbsp, nal_type, nal_ref_idc, spss, ppss):
    """-> dict with the picture-boundary fields, marking syntax and 'noout_bit' (bit position, in rbsp, of
    no_output_of_prior_pics_flag for IDR slices)."""
    r = BitReader(rbsp)
    h = {"first_mb": r.ue(), "slice_type": r.ue() % 5, "pps": r.ue()}
    if h["pps"] not in ppss:
        raise Unsupported("slice refers to a missing PPS")
    p = ppss[h["pps"]]
    if p["sps"] not in spss:
        raise Unsupported("slice refers to a missing SPS")
    s = spss[p["sps"]]
    h["sps"] = s
    h["frame_num"] = r.u(s["log2_max_fn"])
    if not s["frame_mbs_only"] and r.u(1):
        raise Unsupported("field pictures")
    idr = nal_type == 5
    h["idr"] = idr
    h["idr_pic_id"] = r.ue() if idr else None
    h["lsb"] = h["dbot"] = h["d0"] = h["d1"] = 0
    if s["poc_type"] == 0:
        h["lsb"] = r.u(s["log2_max_lsb"])
        if p["bottom_poc"]:
            h["dbot"] = r.se()
    elif s["poc_type"] == 1 and not s["always_zero"]:
        h["d0"] = r.se()
        if p["bottom_poc"]:
            h["d1"] = r.se()
    if p["redundant"]:
        if r.ue():
            h["redundant"] = True
    st = h["slice_type"]
    if st == 1:
        r.u(1)
    a0, a1 = p["l0"], p["l1"]
    if st in (0, 1, 3):
        if r.u(1):
            a0 = r.ue() + 1
            if st == 1:
                a1 = r.ue() + 1
        for lst in range(2 if st == 1 else 1):
            if r.u(1):
                while True:
                    idc = r.ue()
                    if idc == 3:
                        break
                    r.ue()
        if (p["wp"] and st in (0, 3)) or (p["wbi"] == 1 and st == 1):
            r.ue()
            if s["chroma_format_idc"] != 0:
                r.ue()
            for n in (a0, a1) if st == 1 else (a0,):
                for _ in range(n):
                    if r.u(1):
                        r.se(), r.se()
                    if s["chroma_format_idc"] != 0 and r.u(1):
                        r.se(), r.se(), r.se(), r.se()
    h["noout_bit"] = None
    h["mmco"] = None
    h["lt_flag"] = 0
    h["noout"] = 0
    if nal_ref_idc:
        if idr:
            h["noout_bit"] = r.pos
            h["noout"] = r.u(1)
            h["lt_flag"] = r.u(1)
        elif r.u(1):
            ops = []
            while True:
                op = r.ue()
                if op == 0:
                    break
                a = {1: ("diff",), 2: ("ltpn",), 3: ("diff", "idx"), 4: ("max1",), 5: (), 6: ("idx",)}[op]
                ops.append((op, [r.ue() for _ in a]))
            h["mmco"] = ops
    return h


class PocState:
    """8.2.1 for frame pictures, all three pic_order_cnt_type values."""

    def __init__(self):
        self.pmsb = self.plsb = self.pfo = self.pfn = 0

    def compute(self, h, s, ref):
        idr, fn = h["idr"], h["frame_num"]
        maxfn = 1 << s["log2_max_fn"]
        cur = {}
        if s["poc_type"] == 0:
            maxlsb = 1 << s["log2_max_lsb"]
            pm, pl = (0, 0) if idr else (self.pmsb, self.plsb)
            lsb = h["lsb"]
            if lsb < pl and pl - lsb >= maxlsb // 2:
                msb = pm + maxlsb
            elif lsb > pl and lsb - pl > maxlsb // 2:
                msb = pm - maxlsb
            else:
                msb = pm
            top = msb + lsb
            bot = top + h["dbot"]
            cur.update(msb=msb, lsb=lsb)
        else:
            off = 0 if idr else (self.pfo + maxfn if self.pfn > fn else self.pfo)
            if s["poc_type"] == 2:
                t = 0 if idr else (2 * (off + fn) - (0 if ref else 1))
                top = bot = t
            else:
                offs = s["offsets"]
                n = len(offs)
                absfn = off + fn if n else 0
                if not ref and absfn > 0:
                    absfn -= 1
                exp = 0
                if absfn > 0:
                    cyc, inc = divmod(absfn - 1, n)
                    exp = cyc * sum(offs) + sum(offs[:inc + 1])
                if not ref:
                    exp += s["off_nonref"]
                top = exp + h["d0"]
                bot = top + s["off_t2b"] + h["d1"]
            cur.update(off=off)
        self.cur = cur
        return top, bot

    def finish(self, mmco5, top, bot, h, ref):
        c = self.cur
        if "msb" in c:
            if ref:
                self.pmsb, self.plsb = (0, top - min(top, bot)) if mmco5 else (c["msb"], c["lsb"])
        else:
            self.pfo = 0 if mmco5 else c["off"]
            self.pfn = 0 if mmco5 else h["frame_num"]


class Marking:
    """8.2.5 for frame pictures: the set of reference frames, each {'id', 'fn', 'lt'(None | LongTermFrameIdx)}."""

    def __init__(self):
        self.refs = []
        self.max_lt = 0                 # MaxLongTermFrameIdx + 1 (0 = "no long-term frame indices")

    def run(self, pid, h, s, cur_fn):
        """Marks for the current reference picture; returns the ids that become unused for reference.
        The current picture is appended (as short-term or long-term) when done."""
        gone = []
        maxfn = 1 << s["log2_max_fn"]

        def drop(f):
            self.refs.remove(f)
            gone.append(f["id"])

        def wrap(f):
            return f["fn"] - maxfn if f["fn"] > cur_fn else f["fn"]

        newlt = None
        if h["idr"]:
            for f in list(self.refs):
                drop(f)
            self.max_lt = 1 if h["lt_flag"] else 0
            newlt = 0 if h["lt_flag"] else None
        elif h["mmco"] is None:
            short = [f for f in self.refs if f["lt"] is None]
            if len(self.refs) >= max(s["nref"], 1) and short:
                drop(min(short, key=wrap))
        else:
            for op, a in h["mmco"]:
                if op == 1:
                    pn = cur_fn - (a[0] + 1)
                    for f in list(self.refs):
                        if f["lt"] is None and wrap(f) == pn:
                            drop(f)
                elif op == 2:
                    for f in list(self.refs):
                        if f["lt"] == a[0]:
                            drop(f)
                elif op == 3:
                    pn = cur_fn - (a[0] + 1)
                    for f in list(self.refs):
                        if f["lt"] == a[1]:
                            drop(f)
                    for f in self.refs:
                        if f["lt"] is None and wrap(f) == pn:
                            f["lt"] = a[1]
                elif op == 4:
                    self.max_lt = a[0]
                    for f in list(self.refs):
                        if f["lt"] is not None and f["lt"] >= a[0]:
                            drop(f)
                elif op == 5:
                    for f in list(self.refs):
                        drop(f)
                    self.max_lt = 0
                elif op == 6:
                    for f in list(self.refs):
                        if f["lt"] == a[0]:
                            drop(f)
                    newlt = a[0]
        mmco5 = bool(h["mmco"]) and any(op == 5 for op, _ in h["mmco"])
        self.refs.append({"id": pid, "fn": 0 if mmco5 else cur_fn, "lt": newlt})
        return gone


def load_pictures(data):
    """Annex B bytes -> list of picture dicts in decoding order (keys PIC_KEYS)."""
    spss, ppss = {}, {}
    pics, cur, prev = [], None, None
    poc, mk = PocState(), Marking()
    last_was_vcl = False
    expected_fn = None

    def finish_picture(c):
        h, s = c["h"], c["h"]["sps"]
        ref = c["nal_ref_idc"] != 0
        top, bot = poc.compute(h, s, ref)
        mmco5 = bool(h["mmco"]) and any(op == 5 for op, _ in h["mmco"])
        gaps_fn = c["h"]["frame_num"]
        pid = len(pics)
        pf = state["prev_ref_fn"]
        if not h["idr"] and pf is not None and gaps_fn not in (pf, (pf + 1) % (1 << s["log2_max_fn"])):
            raise Unsupported(f"gap in frame_num before picture {pid}")
        unref = mk.run(pid, h, s, gaps_fn) if ref else []
        p = dict(id=pid, poc=min(top, bot), idr=int(h["idr"]), mmco5=int(mmco5), ref=int(ref), noout=h["noout"], unref=unref,
                 profile=s["profile"], level=s["level"], cs3=s["cs3"], wmbs=s["wmbs"], hmbs=s["hmbs"], mdfb=s["mdfb"],
                 nref=s["nref"], reorder=s["num_reorder"], crop=s["crop"], noout_pos=c.get("noout_pos"))
        pics.append(p)
        poc.finish(mmco5, top, bot, h, ref)
        if ref:
            state["prev_ref_fn"] = 0 if mmco5 else gaps_fn

    state = {"prev_ref_fn": None}
    # non-reference pictures carry frame_num = prev ref frame_num + 1; compare against the last reference picture
    for (a, b) in split_nals(data):
        nal = data[a:b]
        t = nal[0] & 31
        ref_idc = (nal[0] >> 5) & 3
        if t == 7:
            s = parse_sps(unescape(nal[1:]))
            spss[s["id"]] = s
        elif t == 8:
            p = parse_pps(unescape(nal[1:]))
            ppss[p["id"]] = p
        if t in (6, 7, 8, 9) or 14 <= t <= 18:
            if last_was_vcl and cur is not None:
                finish_picture(cur)
                cur = None
            last_was_vcl = False
        if t in (1, 5):
            rbsp = unescape(nal[1:])
            h = parse_slice_header(rbsp, t, ref_idc, spss, ppss)
            if h.get("redundant"):
                continue
            new = cur is None
            if cur is not None:
                c = cur["h"]
                cs, hs = c["sps"], h["sps"]
                if (h["frame_num"] != c["frame_num"] or h["pps"] != c["pps"] or h["idr"] != c["idr"]
                        or (ref_idc == 0) != (cur["nal_ref_idc"] == 0) or h["lsb"] != c["lsb"] or h["dbot"] != c["dbot"]
                        or h["d0"] != c["d0"] or h["d1"] != c["d1"]
                        or (h["idr"] and h["idr_pic_id"] != c["idr_pic_id"])):
                    new = True
            if new:
                if cur is not None:
                    finish_picture(cur)
                cur = {"h": h, "nal_ref_idc": ref_idc}
                if h["noout_bit"] is not None:
                    cur["noout_pos"] = (a, h["noout_bit"])
            last_was_vcl = True
    if cur is not None:
        finish_picture(cur)
    return pics
