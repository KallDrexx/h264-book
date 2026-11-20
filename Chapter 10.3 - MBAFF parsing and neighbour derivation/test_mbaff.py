#!/usr/bin/env python3
"""Chapter 10.3 harness: MBAFF parsing and neighbour derivation.

  python3 test_mbaff.py STREAM READER_DUMP            check one stream (.ts / .264) against your dump
  python3 test_mbaff.py --expected STREAM OUT         write the expected dump of a stream (the reference parse) to OUT
  python3 test_mbaff.py --make DIR                    write the suite (synthetic + x264 MBAFF streams) to DIR
  python3 test_mbaff.py --check DIR OUTDIR [--only a,b]   OUTDIR/<case>.dump of every DIR/<case>.264|.ts
  python3 test_mbaff.py --suite DIR --reader './mydec {stream} {dump}'    --make, run your program per case, --check
  python3 test_mbaff.py --nb-make FILE                write the Table 6-4 queries (section 5.3)
  python3 test_mbaff.py --nb-check FILE ANSWERS       check your answers to them
  python3 test_mbaff.py --self-test                   check the harness itself (takes several minutes)
Exit 0 pass, 1 fail, 2 missing / malformed input or no usable truth.  Needs ffmpeg + libx264 on PATH; Python 3 stdlib only.
Truth: spoiler_reference/mbaff.py (an independent MBAFF parser; every slice must end exactly on its end_of_slice_flag / stop bit)
cross-checked against ffmpeg's macroblock map (class, partition shape, field marker per macroblock) and QP map; the synthetic
streams of spoiler_reference/mbgen.py must additionally decode in ffmpeg without a single warning.
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import cavlc_ref as CR, ffref, inter_ref as IR, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import mbaff as M  # noqa: E402
import mbgen  # noqa: E402

KEYS = ["pair", "pos", "fld", "mbt", "t8", "cpred", "modes", "pm", "sub", "refidx", "refidx1", "mvd", "mvd1", "cbp", "qpd", "qp",
        "coef", "eos"]
X264 = {   # name -> (extra ffmpeg filter, x264 options, rate-control arguments, source)
    "x_cabac_b": ("-profile:v high", "interlaced=1:tff=1:ref=3:bframes=3:b-pyramid=normal", ["-qp", "26"], "move"),
    "x_cavlc_main": ("-profile:v main", "interlaced=1:tff=1:cabac=0:bframes=2", ["-qp", "28"], "move2"),
    "x_cavlc_high": ("-profile:v high", "interlaced=1:tff=1:cabac=0:ref=2", ["-qp", "24"], "move"),
    "x_cabac_slices": ("-profile:v high", "interlaced=1:tff=1:slices=3:bframes=0:aq-mode=2", ["-crf", "27"], "move2"),
    "x_cabac_cip": ("-profile:v main", "interlaced=1:tff=1:constrained-intra=1:bframes=1", ["-qp", "30"], "move"),
    "x_static_cabac": ("-profile:v high", "interlaced=1:tff=1:bframes=3", ["-qp", "30"], "still"),
    "x_static_cavlc": ("-profile:v main", "interlaced=1:tff=1:cabac=0:bframes=2", ["-qp", "30"], "still"),
}
SOURCES = {"move": "testsrc2=size=176x144:rate=50,scroll=vertical=0.007:horizontal=0.011,format=yuv420p,"
                   "tinterlace=mode=interleave_top,fps=25",
           "move2": "testsrc2=size=176x144:rate=50,scroll=vertical=0.013,format=yuv420p,tinterlace=mode=interleave_top,fps=25",
           "still": "testsrc2=size=176x144:rate=25,format=yuv420p,trim=end_frame=1,loop=loop=-1:size=1,"
                    "tinterlace=mode=interleave_top"}


def die(msg, code=2):
    print(msg)
    sys.exit(code)


# ================================================================ suite
def make_x264(name, path):
    prof, params, rc, src = X264[name]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SOURCES[src], "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
           *rc, *prof.split(), "-x264-params", params, "-f", "h264", path + ".tmp"]
    r = ffref.run(cmd, timeout=180)
    if r.returncode != 0:
        die(f"ffmpeg/x264 failed for {name}: {r.stderr.decode()[-300:]}")
    os.replace(path + ".tmp", path)


def make_suite(d, only=None):
    os.makedirs(d, exist_ok=True)
    names = []
    for n in mbgen.SCENARIOS:
        if only is None or n in only:
            data, _, _ = mbgen.build(n)
            open(os.path.join(d, n + ".264"), "wb").write(data)
            names.append(n)
    for n in X264:
        if only is None or n in only:
            make_x264(n, os.path.join(d, n + ".264"))
            names.append(n)
    if only is None or "T12" in only:
        shutil.copy(testfiles.get("T12"), os.path.join(d, "T12.ts"))
        names.append("T12")
    return names


def find_stream(d, name):
    for ext in (".264", ".ts"):
        p = os.path.join(d, name + ext)
        if os.path.exists(p):
            return p
    return None


# ================================================================ truth
def poc_rank(infos):
    """Decoding-order picture index -> output-order index (POC type 0 / 2; ffmpeg prints its maps in output order)."""
    first = {}
    for i in infos:
        first.setdefault(i.picture, i)
    n = len(first)
    keys, period, pmsb, plsb = [], 0, 0, 0
    for p in range(n):
        i = first[p]
        h, s = i.hdr, i.sps
        if s.get("pic_order_cnt_type", 0) != 0:
            keys.append((0, p))
            continue
        maxl = 1 << (s["log2_max_pic_order_cnt_lsb_minus4"] + 4)
        idr = i.nal_type == 5
        if idr:
            period += 1
            pmsb = plsb = 0
        lsb = h.get("pic_order_cnt_lsb", 0)
        if lsb < plsb and plsb - lsb >= maxl // 2:
            msb = pmsb + maxl
        elif lsb > plsb and lsb - plsb > maxl // 2:
            msb = pmsb - maxl
        else:
            msb = pmsb
        top = msb + lsb
        bot = top + h.get("delta_pic_order_cnt_bottom", 0)
        keys.append((period, min(top, bot)))
        if h.get("nal_ref_idc", 1):
            pmsb, plsb = msb, lsb
    order = sorted(range(n), key=lambda p: keys[p])
    rank = [0] * n
    for o, p in enumerate(order):
        rank[p] = o
    return rank


def expected_cell(rec):
    """First two characters of ffmpeg's macroblock-map cell for a macroblock (None = not comparable) + the field marker."""
    fm = "=" if rec.fld else " "
    if rec.skip:
        return ("S " if rec.mbt is None and not rec.direct else None), fm
    if rec.pcm:
        return "P ", fm
    if rec.intra:
        return ("i " if rec.i_nxn else "I "), fm
    return None, fm


def inter_cell(rec, t):
    if rec.skip:
        return None if t == "B" else "S "
    if t == "P":
        return ">" + {0: " ", 1: "-", 2: "|", 3: "+", 4: "+"}[rec.mbt]
    if rec.mbt == 0 or any(rec.dir8):
        return None
    if rec.mbt == 22:
        return "X+"
    rects, modes = M.B_TYPES[rec.mbt]
    u0 = any(m in ("L0", "Bi") for m in modes)
    u1 = any(m in ("L1", "Bi") for m in modes)
    return ("X" if u0 and u1 else ">" if u0 else "<") + (" " if len(rects) == 1 else "-" if rects == M.R168 else "|")


def ffmpeg_check(path, infos, parsed, td):
    """Per macroblock: field marker, class, partition shape (ffmpeg -debug mb_type) and QP_Y (ffmpeg -debug qp) of every picture."""
    if not infos:
        return [], "no slices"
    npics = infos[-1].picture + 1
    w0 = infos[0].pic_width_in_mbs
    h0 = infos[0].pic_size_in_mbs // w0
    if not all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos):
        return [], "ffmpeg cross-check skipped: pictures of different sizes"
    cells = IR.ffmpeg_cells(path, td, dims=(w0, h0), count=npics)
    qps = CR.ffmpeg_qp_grid(path, td, dims=(w0, h0), count=npics)
    if cells is None or qps is None or len(cells) < npics or len(qps) < npics:
        return [], "ffmpeg cross-check skipped: ffmpeg's log was unusable"
    rank = poc_rank(infos)
    first = {}
    for i in infos:
        first.setdefault(i.picture, "PBI"[[0, 1, 2, 3, 4].index(i.slice_type) if i.slice_type < 3 else 0])
    ok = all(cells[rank[p]]["type"] == first[p] for p in range(npics))
    if not ok:
        return [], "ffmpeg cross-check skipped: could not match ffmpeg's pictures (output order) to the slices"
    problems, n = [], 0
    for info, mbs, st in parsed:
        t = M.slice_letter(info)
        crow, qrow = cells[rank[info.picture]]["rows"], qps[rank[info.picture]]["rows"]
        for rec in mbs:
            p = rec.addr // 2
            x, y = p % w0, 2 * (p // w0) + rec.addr % 2
            cell = crow[y][x]
            n += 1
            where = f"slice {info.index} MB {rec.addr} (pair {p} {'bottom' if rec.addr % 2 else 'top'})"
            e, fm = expected_cell(rec)
            if not rec.skip and not rec.pcm and not rec.intra:
                e = inter_cell(rec, t)
            elif rec.skip and t == "B":
                e = None
            if cell[2] != fm:
                problems.append(f"{where}: reference says {'field' if rec.fld else 'frame'} macroblock, ffmpeg's map shows "
                                f"{'field' if cell[2] == '=' else 'frame'}")
            if e is not None and cell[:2] != e:
                problems.append(f"{where}: reference class/shape {e!r}, ffmpeg {cell[:2]!r}")
            mine = 0 if rec.pcm else rec.qp
            if qrow[y][x] != mine:
                problems.append(f"{where}: reference QP_Y {mine}, ffmpeg {qrow[y][x]}")
    return problems, f"ffmpeg maps: field marker, class, shape and QP of {n} macroblocks agree"


def ffmpeg_clean(path):
    r = ffref.run(["ffmpeg", "-v", "warning", "-i", path, "-f", "null", "-"], timeout=120)
    return r.stderr.decode(errors="replace").strip()


def load(path, td):
    infos = M.prepare(CR.load_stream(path, td))
    return infos


def truth(path, td, need_ffmpeg=True):
    """-> (infos, parsed, dump text, notes).  Dies (exit 2) when the reference cannot establish the truth."""
    infos = load(path, td)
    parsed = M.parse_all(infos)
    if not parsed:
        die(f"{path}: no MBAFF slice in this stream (needs mb_adaptive_frame_field_flag 1, frame pictures)")
    bad = [(i.index, st) for i, _, st in parsed if st != "complete"]
    if bad:
        die(f"{path}: the reference parser does not end cleanly: slice {bad[0][0]}: {bad[0][1]}")
    notes = []
    if need_ffmpeg:
        probs, summ = ffmpeg_check(path, infos, parsed, td)
        notes.append(summ)
        if probs:
            die(f"{path}: the reference disagrees with ffmpeg:\n  " + "\n  ".join(probs[:8]))
    return infos, parsed, M.render_dump(parsed), notes


# ================================================================ reader dump
def parse_dump_text(text, what):
    """-> list of slices: dict(idx, type, first_mb, mbs=[dict], end=n or None)."""
    slices, cur = [], None
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        w = line.split()
        kv = {}
        for t in w[1:]:
            if "=" in t:
                k, v = t.split("=", 1)
                kv[k] = v
        try:
            if w[0] == "SLICE":
                cur = dict(idx=int(w[1]), type=kv.get("type"), first_mb=int(kv["first_mb"]), mbs=[], end=None)
                slices.append(cur)
            elif w[0] == "MB":
                if cur is None or int(w[1]) != cur["idx"]:
                    die(f"{what}:{no}: MB line outside its SLICE")
                d = dict(addr=int(w[2]), **kv)
                cur["mbs"].append(d)
            elif w[0] == "END":
                if cur is None:
                    die(f"{what}:{no}: END without SLICE")
                cur["end"] = int(kv["mbs"])
            else:
                die(f"{what}:{no}: unknown line type {w[0]!r}")
        except (ValueError, KeyError, IndexError):
            die(f"{what}:{no}: malformed line: {line[:80]}")
    return slices


def compare(exp, act, rep):
    rep.compare("number of SLICE lines", len(exp), len(act))
    for se, sa in zip(exp, act):
        tag = f"slice {se['idx']}"
        rep.compare(f"{tag} index", se["idx"], sa["idx"])
        rep.compare(f"{tag} type", se["type"], sa["type"])
        rep.compare(f"{tag} first_mb", se["first_mb"], sa["first_mb"])
        rep.compare(f"{tag} END mbs", len(se["mbs"]), sa["end"])
        rep.compare(f"{tag} number of MB lines", len(se["mbs"]), len(sa["mbs"]))
        for me, ma in zip(se["mbs"], sa["mbs"]):
            if me["addr"] != ma["addr"]:
                rep.check(False, f"{tag}: MB line for address {me['addr']} expected, got {ma['addr']}")
                break
            for k in KEYS:
                if me.get(k) != ma.get(k):
                    rep.check(False, f"{tag} MB {me['addr']} (pair {me['pair']} {'bottom' if me['pos'] == 'B' else 'top'}, "
                              f"fld={me['fld']}) {k}: expected {me.get(k)!r}, got {ma.get(k, '(no ' + k + '= word)')!r}")
                else:
                    rep.check(True, "")
    return rep


def check_stream(path, dump_path, quiet=False):
    td = tempfile.mkdtemp(prefix="mbaff_")
    try:
        _, _, text, notes = truth(path, td)
        if not os.path.exists(dump_path):
            die(f"reader output {dump_path} not found")
        act = parse_dump_text(open(dump_path).read(), dump_path)
        rep = Report(os.path.basename(path), max_shown=10)
        compare(parse_dump_text(text, "reference"), act, rep)
        return rep, notes
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ================================================================ Table 6-4 queries
POS_A = [(-1, y, 16) for y in range(16)] + [(-1, y, 8) for y in range(8)]
POS_OTHER = [(0, -1, 16), (9, -1, 16), (15, -1, 16), (16, -1, 16), (-1, -1, 16), (0, -1, 8), (7, -1, 8), (8, -1, 8), (-1, -1, 8),
             (16, 0, 16), (0, 16, 16), (-1, 16, 16), (4, 8, 16)]
STATES = ["-", "frame", "field"]


def nb_query(cur, st, x, y, mx):
    """cur = (top?, field?), st = states of pairs A, B, C, D -> (where, xW, yW) or None, from mbaff.Slice.nb."""
    W, p = 3, 4                                        # current pair index 4 = column 1, row 1 of a picture 3 pairs wide
    sl = M.Slice(M.Info(slice_type=2, entropy=1, first_mb=0, pic_width_in_mbs=W, pic_size_in_mbs=18, slice_qp=26,
                        pps={}, nal=b"\0" * 8))
    names = {"A": p - 1, "B": p - W, "C": p - W + 1, "D": p - W - 1}
    for k, s in zip("ABCD", st):
        if s != "-":
            for j in (0, 1):
                m = M.Mb(2 * names[k] + j)
                m.fld = 1 if s == "field" else 0
                sl.recs[m.addr] = m
    top, fld = cur
    for j in range(2 if not top else 1):
        m = M.Mb(2 * p + j)
        m.fld = 1 if fld else 0
        sl.recs[m.addr] = m
    addr = 2 * p + (0 if top else 1)
    r = sl.nb(addr, x, y, mx, mx)
    if r is None:
        return None
    n, xw, yw = r
    if n == addr:
        return "CUR", xw, yw
    if n == addr - 1 and n // 2 == p:
        return "PREV", xw, yw
    q = {v: k for k, v in names.items()}[n // 2]
    return f"{q}{n % 2}", xw, yw


def geom_query(cur, st, x, y, mx):
    """The independent oracle: find the physically adjacent sample (same field for a field MB, same frame for a frame MB)."""
    top, fld = cur
    if y > mx - 1 or (x > mx - 1 and y >= 0):                      # 6.4.12: below the macroblock / right of it: never available
        return None
    ph = 2 * mx
    r = (0 if top else 1) + 2 * y if fld else (0 if top else mx) + y
    dx = x // mx
    dy = r // ph
    xx, rr = x % mx, r % ph
    pos = {(-1, 0): "A", (0, -1): "B", (1, -1): "C", (-1, -1): "D"}.get((dx, dy))
    if (dx, dy) == (0, 0):
        if fld:
            return ("CUR", xx, rr // 2) if rr % 2 == (0 if top else 1) else None
        mb, yy = rr // mx, rr % mx
        if mb == (0 if top else 1):
            return "CUR", xx, yy
        return ("PREV", xx, yy) if (mb == 0 and not top) else None
    if pos is None:
        return None
    s = st["ABCD".index(pos)]
    if s == "-":
        return None
    if s == "frame":
        return f"{pos}{rr // mx}", xx, rr % mx
    return f"{pos}{rr % 2}", xx, rr // 2


def nb_queries():
    rnd = random.Random(64)
    out = []
    for cur in ((1, 0), (0, 0), (1, 1), (0, 1)):
        for k in range(81):
            st = [STATES[(k // 27) % 3], STATES[(k // 9) % 3], STATES[(k // 3) % 3], STATES[k % 3]]
            if k % 7 == 0:
                pos = POS_A + POS_OTHER
            else:
                pos = rnd.sample(POS_A, 6) + rnd.sample(POS_OTHER, 4)
            for x, y, mx in pos:
                out.append((cur, st, x, y, mx))
    return out


def qline(i, q):
    cur, st, x, y, mx = q
    return (f"NBQ {i} cur={'top' if cur[0] else 'bottom'}-{'field' if cur[1] else 'frame'} "
            f"A={st[0]} B={st[1]} C={st[2]} D={st[3]} x={x} y={y} max={mx}")


def nb_selftest(quiet=False):
    """The table-driven neighbour derivation equals the geometric oracle for every query (harness consistency)."""
    bad = 0
    for q in nb_queries():
        if nb_query(*q) != geom_query(*q):
            bad += 1
            if bad < 4 and not quiet:
                print("  table vs geometry:", qline(0, q), nb_query(*q), geom_query(*q))
    return bad


def nb_make(path):
    with open(path, "w") as f:
        f.write("# NBQ <id> cur=<top|bottom>-<frame|field> A=.. B=.. C=.. D=.. x=<xN> y=<yN> max=<16|8>\n")
        for i, q in enumerate(nb_queries()):
            f.write(qline(i, q) + "\n")


def nb_check(qpath, apath):
    exp = {}
    for i, q in enumerate(nb_queries()):
        r = nb_query(*q)
        exp[i] = "-" if r is None else f"{r[0]} {r[1]} {r[2]}"
    got = {}
    if not os.path.exists(apath):
        die(f"answers file {apath} not found")
    for line in open(apath):
        w = line.split()
        if not w or w[0].startswith("#"):
            continue
        if w[0] != "NBA":
            die(f"{apath}: unknown line {line.strip()[:60]!r}")
        try:
            got[int(w[1])] = "-" if w[2] == "-" else f"{w[2]} {int(w[3])} {int(w[4])}"
        except (ValueError, IndexError):
            die(f"{apath}: malformed line {line.strip()[:60]!r}")
    rep = Report("Table 6-4 neighbour derivation", 10)
    qs = nb_queries()
    for i in exp:
        rep.check(got.get(i) == exp[i], f"{qline(i, qs[i])}: expected {exp[i]!r}, got "
                  + (repr(got[i]) if i in got else "no NBA line for this id"))
    rep.finish()


# ================================================================ coverage (self-test)
def coverage(parsed, cov):
    """Count the situations of the neighbour rules that a stream exercises (cov: Counter-like dict)."""
    def inc(k):
        cov[k] = cov.get(k, 0) + 1
    for info, mbs, _ in parsed:
        recs = {r.addr: r for r in mbs}
        W = info.pic_width_in_mbs
        if info.first_mb % W:
            inc("slice starts inside a pair row")
        for r in mbs:
            t = M.slice_letter(info)
            p = r.addr // 2
            if r.addr % 2 == 0:
                bot = recs.get(r.addr + 1)
                if bot is not None:
                    if r.skip and bot.skip:
                        inc("both MBs skipped (flag inferred), pair is " + ("field" if r.fld else "frame"))
                    elif r.skip:
                        inc("top skipped, bottom not (flag read with the bottom MB), pair is " + ("field" if r.fld else "frame"))
                    elif bot.skip:
                        inc("bottom skipped, top not")
                for nm, q in (("left", p - 1 if p % W else None), ("above", p - W)):
                    if q is not None and 2 * q in recs:
                        inc(f"cur {'field' if r.fld else 'frame'} pair, {nm} pair {'field' if recs[2 * q].fld else 'frame'}")
            if r.skip:
                continue
            if r.pcm:
                inc("I_PCM")
            if r.t8:
                inc("transform 8x8 MB (" + ("field" if r.fld else "frame") + ")")
            if r.intra and r.i_nxn:
                inc("I_NxN " + ("field" if r.fld else "frame"))
            if r.intra and not r.pcm and t != "I":
                inc("intra MB in a P/B slice")
            if not r.intra:
                if r.mbt == (3 if t == "P" else 22):
                    inc("8x8 sub-macroblocks")
                big = [v for l in r.refl for v in l if v > 1]
                if big and r.fld:
                    inc("field MB with ref_idx > 1")
                if r.fld and any(len(l) for l in r.refl) and any(v >= 0 for l in r.refl for v in l) and info.num_ref_idx_l0_active == 1:
                    inc("field MB with num_ref_idx_active 1 (ref_idx present)")
                if r.mvdl[0] or r.mvdl[1]:
                    inc("mvd MB " + ("field" if r.fld else "frame"))
            if r.qpd_present and r.qpd:
                inc("non-zero mb_qp_delta")
    return cov


# ================================================================ self-test
def run_mutant(name, cases, truths):
    """True when the mutated reader is caught on at least one case."""
    M.MUTANT = name
    try:
        if name.startswith("t64_") and nb_selftest(quiet=True):
            return "the Table 6-4 queries"
        for cname, (infos, text) in cases.items():
            exp = parse_dump_text(truths[cname], "reference")
            try:
                parsed = M.parse_all(infos)
                text_m = M.render_dump(parsed)
                bad = any(st != "complete" for _, _, st in parsed)
            except Exception:
                return cname
            if bad:
                return cname
            rep = compare(exp, parse_dump_text(text_m, "mutant"), Report("m", 1000))
            if rep.failures:
                return cname
        return None
    finally:
        M.MUTANT = None


def self_test(args):
    ok = True
    td = tempfile.mkdtemp(prefix="mbaff_st_")
    try:
        bad = nb_selftest()
        print(f"Table 6-4 (table-driven) vs geometric oracle: {len(nb_queries())} queries, {bad} differences")
        ok &= bad == 0
        d = os.path.join(td, "suite")
        names = make_suite(d)
        cases, truths, cov = {}, {}, {}
        order = []
        for n in names:
            p = find_stream(d, n)
            w = os.path.join(td, "w_" + n)
            os.makedirs(w)
            infos, parsed, text, notes = truth(p, w)
            if n.startswith("g_"):
                gen_text = mbgen.build(n)[1]
                if gen_text != text:
                    print(f"  FAIL: {n}: the generator's manifest differs from the reference parse of its stream")
                    ok = False
                warn = ffmpeg_clean(p)
                if warn:
                    print(f"  FAIL: {n}: ffmpeg warns about the synthetic stream: {warn[:200]}")
                    ok = False
            coverage(parsed, cov)
            cases[n], truths[n] = (infos, text), text
            order.append(n)
            print(f"  {n}: {sum(len(m) for _, m, _ in parsed)} MBs, {sum(r.fld for _, m, _ in parsed for r in m)} field MBs; {notes[0] if notes else ''}")
        print("  coverage over the suite:")
        for k in sorted(cov):
            print(f"    {cov[k]:6d}  {k}")
        for need in ("both MBs skipped (flag inferred), pair is field", "both MBs skipped (flag inferred), pair is frame",
                     "top skipped, bottom not (flag read with the bottom MB), pair is field",
                     "top skipped, bottom not (flag read with the bottom MB), pair is frame",
                     "cur frame pair, left pair field", "cur field pair, left pair frame", "cur frame pair, above pair field",
                     "cur field pair, above pair frame", "I_PCM", "field MB with ref_idx > 1", "slice starts inside a pair row",
                     "transform 8x8 MB (field)", "intra MB in a P/B slice", "8x8 sub-macroblocks"):
            if not cov.get(need):
                print(f"  FAIL: the suite never exercises: {need}")
                ok = False
        # the correct reader passes, corrupted ones fail
        n0 = order[0]
        exp = parse_dump_text(truths[n0], "reference")
        rep = compare(exp, parse_dump_text(truths[n0], "copy"), Report("copy"))
        print("  good reader output:", "PASS" if not rep.failures else "FAIL")
        ok &= not rep.failures
        lines = truths[n0].splitlines()
        for what, mod in (("a flipped field flag", lambda L: [l.replace("fld=0", "fld=1", 1) if l.startswith("MB 0 3 ") else l for l in L]),
                          ("a dropped MB line", lambda L: [l for l in L if not l.startswith("MB 0 5 ")]),
                          ("a wrong QP", lambda L: [l.replace(" qp=", " qp=9", 1) if l.startswith("MB 0 4 ") else l for l in L])):
            rep = compare(exp, parse_dump_text("\n".join(mod(lines)), "bad"), Report("bad"))
            print(f"  corrupted output ({what}):", "caught" if rep.failures else "NOT CAUGHT")
            ok &= bool(rep.failures)
        # the mutants
        prio = [n for n in order if n.startswith("g_")] + [n for n in order if not n.startswith("g_")]
        cases = {n: cases[n] for n in prio}
        missed = []
        for m in M.MUTANTS:
            hit = run_mutant(m, cases, truths)
            print(f"  mutant {m}: {'caught by ' + hit if hit else 'NOT CAUGHT'}")
            if not hit:
                missed.append(m)
        ok &= not missed
        print(f"{len(M.MUTANTS)} wrong rules, {len(M.MUTANTS) - len(missed)} caught")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


# ================================================================ main
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if argv else 2)
    a = argv[0]
    if a == "--self-test":
        ffref.require_tools()
        return self_test(argv[1:])
    ffref.require_tools()
    if a == "--make":
        names = make_suite(argv[1])
        print(f"wrote {len(names)} streams to {argv[1]}: {', '.join(names)}")
        return
    if a == "--expected":
        td = tempfile.mkdtemp(prefix="mbaff_")
        try:
            _, _, text, _ = truth(argv[1], td)
        finally:
            shutil.rmtree(td, ignore_errors=True)
        open(argv[2], "w").write(text)
        print(f"wrote the expected dump of {argv[1]} to {argv[2]}")
        return
    if a == "--nb-make":
        nb_make(argv[1])
        print(f"wrote {len(nb_queries())} queries to {argv[1]}")
        return
    if a == "--nb-check":
        return nb_check(argv[1], argv[2])
    if a in ("--check", "--suite"):
        d = argv[1]
        only = None
        if "--only" in argv:
            only = argv[argv.index("--only") + 1].split(",")
        if a == "--suite":
            if "--reader" not in argv:
                die("--suite needs --reader 'command {stream} {dump}'")
            cmdt = argv[argv.index("--reader") + 1]
            names = make_suite(d, only)
            out = d
            for n in names:
                p = find_stream(d, n)
                cmd = cmdt.replace("{stream}", p).replace("{dump}", os.path.join(out, n + ".dump"))
                try:
                    subprocess.run(cmd, shell=True, stdin=subprocess.DEVNULL, timeout=300, check=False)
                except subprocess.TimeoutExpired:
                    print(f"{n}: your program timed out (300 s)")
        else:
            out = argv[2]
            names = [n for n in (list(mbgen.SCENARIOS) + list(X264) + ["T12"]) if (only is None or n in only)]
        failed = 0
        for n in names:
            p = find_stream(d, n)
            if p is None:
                die(f"{d}: stream {n} not found (run --make first)")
            dp = os.path.join(out, n + ".dump")
            if not os.path.exists(dp):
                print(f"{n}: missing reader output {dp}")
                failed = max(failed, 2)
                continue
            rep, notes = check_stream(p, dp)
            print(f"[{n}] ", end="")
            try:
                rep.finish()
            except SystemExit as e:
                failed = max(failed, e.code or 0)
        sys.exit(failed)
    if len(argv) == 2 and not a.startswith("-"):
        rep, notes = check_stream(argv[0], argv[1])
        for n in notes:
            print(n)
        rep.finish()
    die(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
