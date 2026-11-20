"""Stream sources and helpers for Chapter 10.6: the built-in sweep set, Annex B scanning and the deterministic corruptions.

Nothing here knows about the reader's decoder.  Streams are generated into <repo>/.cache/conformance/ (git-ignored)
with x264 (through ffmpeg) and with the synthetic generators in the spoiler_reference folders of Chapters 10.1 - 10.4.
"""
import glob
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
from common import ffref  # noqa: E402

CACHE = os.path.join(ROOT, ".cache", "conformance")
W, H, NFRAMES = 128, 96, 16

# name, x264 profile, x264-params, extra ffmpeg args, tags (what the stream is meant to exercise)
X264_CASES = [
    ("cavlc_intra", "baseline", "keyint=1", [], "baseline CAVLC intra"),
    ("cavlc_p_ref4", "baseline", "keyint=12:partitions=all:ref=4", ["-bf", "0"], "baseline CAVLC P multi-ref partitions"),
    ("cavlc_multislice", "baseline", "keyint=8:slices=4", ["-bf", "0"], "baseline CAVLC 4 slices IDR every 8"),
    ("cavlc_cip_aq", "baseline", "keyint=12:constrained-intra=1:aq-mode=2", ["-bf", "0", "-crf", "28"], "constrained intra, QP deltas"),
    ("deblock_off", "baseline", "keyint=12:no-deblock=1", ["-bf", "0"], "deblocking disabled"),
    ("deblock_offsets", "main", "keyint=12:deblock=3,-2:cabac=0", ["-bf", "0"], "deblock alpha/beta offsets"),
    ("main_cavlc_b_spatial", "main", "cabac=0:bframes=2:b-pyramid=none:direct=spatial:weightb=0:keyint=12", [], "CAVLC B spatial direct"),
    ("main_cavlc_b_temporal", "main", "cabac=0:bframes=3:direct=temporal:weightb=1:b-pyramid=normal:keyint=12", [], "CAVLC B temporal direct, implicit weights"),
    ("weighted_p", "main", "cabac=0:bframes=0:weightp=2:keyint=16", ["-vf", "fade=in:0:16"], "explicit weighted P"),
    ("weighted_b_implicit", "main", "bframes=3:weightb=1:direct=spatial:keyint=12", [], "CABAC B implicit weights"),
    ("cabac_intra", "main", "cabac=1:keyint=1", [], "CABAC intra"),
    ("cabac_p", "main", "keyint=12:ref=3", ["-bf", "0"], "CABAC P"),
    ("cabac_b_pyramid", "main", "bframes=4:b-pyramid=normal:ref=4:keyint=10", [], "CABAC B pyramid, reordering"),
    ("cabac_slices_aq", "main", "slices=4:aq-mode=2:keyint=8", ["-crf", "28"], "CABAC 4 slices, QP deltas, IDR every 8"),
    ("cabac_qp46", "main", "keyint=12", ["-qp", "46"], "very high QP"),
    ("cabac_qp8", "main", "keyint=12:bframes=2", ["-qp", "8"], "very low QP, large coefficients"),
    ("high_t8_cabac", "high", "keyint=12", [], "High 8x8 transform CABAC"),
    ("high_t8_cavlc", "high", "keyint=12:cabac=0", [], "High 8x8 transform CAVLC"),
    ("high_intra_t8", "high", "keyint=1", [], "High intra 8x8"),
    ("high_cqm_jvt", "high", "keyint=12:cqm=jvt", [], "scaling lists (JVT matrices)"),
    ("high_p_ref16", "high", "keyint=16:ref=16", ["-bf", "0"], "16 reference frames"),
    ("mbaff_cabac", "high", "keyint=8:interlaced=1:tff=1", [], "MBAFF CABAC"),
    ("mbaff_cavlc", "high", "keyint=8:interlaced=1:cabac=0", [], "MBAFF CAVLC"),
    ("mbaff_b_weighted", "high", "keyint=12:interlaced=1:bframes=3:weightb=1", [], "MBAFF B implicit weights"),
]
QUICK_X264 = ["cavlc_p_ref4", "cavlc_multislice", "main_cavlc_b_temporal", "weighted_p", "cabac_b_pyramid", "cabac_slices_aq",
              "high_t8_cabac", "high_cqm_jvt", "mbaff_cabac", "cabac_intra", "cavlc_intra", "high_t8_cavlc", "mbaff_cavlc",
              "weighted_b_implicit", "deblock_offsets", "cabac_qp8"]

# (chapter folder glob, generator script, CLI: script name out, quick names, tag)
SYNTH = [
    ("paff", "Chapter 10.1 - *", "fgen.py",
     ["cavlc_tff", "cabac_t8_mixed", "cavlc_lone_fields", "cabac_idr_again", "cavlc_reorder", "cabac_poc2"], "PAFF field pictures"),
    ("fref", "Chapter 10.2 - *", "fpgen.py",
     ["p_mixed", "b_mixed", "mmco_mixed", "dbk_fields", "lt_fields", "d_temporal"], "field reference lists / marking"),
    ("mbaff", "Chapter 10.4 - *", "mbgen.py",
     ["g_i_cabac", "g_b_cavlc", "g_field_cabac", "g_wp_implicit_cabac", "g_temporal_cavlc", "g_t8_cabac", "g_pcm_cavlc"], "synthetic MBAFF"),
]


def _run(cmd, timeout=180, cwd=None):
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, cwd=cwd, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.decode(errors='replace')[-400:]}")


def _synth_dir(glob_pat):
    g = sorted(glob.glob(os.path.join(ROOT, glob_pat, "spoiler_reference")))
    return g[0] if g else None


def synth_names(prefix, full):
    for pre, gl, script, quick, tag in SYNTH:
        if pre != prefix:
            continue
        if not full:
            return quick
        d = _synth_dir(gl)
        mod = script[:-3]
        attr = {"fgen": "scenarios()", "fpgen": "SCENARIOS", "mbgen": "SCENARIOS"}[mod]
        r = subprocess.run([sys.executable, "-c", f"import {mod}; print(' '.join(list({mod}.{attr})))"], cwd=d,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        return r.stdout.decode().split() or quick
    return []


def builtin_cases(which="quick"):
    """-> list of dicts(name, tags, make) in a fixed order.  which = 'quick' | 'full'."""
    full = which == "full"
    cases = []
    for name, prof, params, extra, tags in X264_CASES:
        if full or name in QUICK_X264:
            cases.append(dict(name="x264_" + name, tags=tags, make=("x264", name)))
    for pre, gl, script, quick, tag in SYNTH:
        if _synth_dir(gl) is None:
            continue
        for n in synth_names(pre, full):
            cases.append(dict(name=f"{pre}_{n}", tags=tag, make=("synth", pre, n)))
    if full:
        from common import testfiles
        for n in testfiles.RECIPES:
            cases.append(dict(name="common_" + n, tags="common/testfiles recipe " + n, make=("recipe", n)))
    return cases


def make_stream(case, outdir=CACHE):
    """Return the path of the case's Annex B file, generating it into the cache if needed."""
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, case["name"] + ".264")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    kind = case["make"]
    tmp = out + ".tmp"
    if kind[0] == "x264":
        name, prof, params, extra, _ = next(c for c in X264_CASES if c[0] == kind[1])
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=25", "-frames:v", str(NFRAMES),
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-threads", "1"]
        if "-qp" not in extra and "-crf" not in extra:
            cmd += ["-qp", "26"]
        cmd += extra + ["-profile:v", prof, "-x264-params", params + ":threads=1", "-f", "h264", tmp]
        _run(cmd)
    elif kind[0] == "synth":
        pre, n = kind[1], kind[2]
        _, gl, script, _, _ = next(s for s in SYNTH if s[0] == pre)
        _run([sys.executable, script, n, tmp], cwd=_synth_dir(gl), timeout=300)
        for side in glob.glob(tmp + ".*"):                    # .json / .gen side files of the generators
            os.replace(side, out + side[len(tmp):])
    else:
        from common import testfiles
        ts = testfiles.get(kind[1])
        ffref.raw_h264(ts, tmp)
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise RuntimeError(f"could not generate {case['name']}")
    os.replace(tmp, out)
    return out


# ----------------------------------------------------------------------------------------------- Annex B scanning
class Seg:
    """One NAL unit together with its start code: data[start:end]."""
    __slots__ = ("start", "end", "type", "first_mb0", "au")

    def __init__(self, start, end, typ, first_mb0):
        self.start, self.end, self.type, self.first_mb0, self.au = start, end, typ, first_mb0, 0


def scan(data):
    """Split an Annex B byte string into Seg objects and number the access units (a coarse AU detector:
    a new AU starts at a delimiter / SPS / PPS / SEI after a slice, or at a slice whose first_mb_in_slice is 0)."""
    pos, i, n = [], 0, len(data)
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= n:
            break
        s = i
        while s > 0 and data[s - 1] == 0 and i - s < 1:
            s -= 1
        pos.append((s, i + 3))
        i += 3
    segs = []
    for k, (s, p) in enumerate(pos):
        e = pos[k + 1][0] if k + 1 < len(pos) else n
        typ = data[p] & 0x1F
        segs.append(Seg(s, e, typ, p + 1 < n and typ in (1, 5) and bool(data[p + 1] & 0x80)))
    au, seen_vcl = -1, True
    for sg in segs:
        vcl = sg.type in (1, 5)
        if (vcl and (sg.first_mb0 and seen_vcl)) or (not vcl and sg.type in (6, 7, 8, 9) and seen_vcl) or au < 0:
            au += 1
            seen_vcl = False
        if vcl:
            seen_vcl = True
        sg.au = au
    return segs


def idr_aus(segs):
    return sorted({s.au for s in segs if s.type == 5})


# ----------------------------------------------------------------------------------------------- corruptions
KINDS = ["bitflip", "zerofill", "cutmid", "truncate", "dropau", "loseslice", "garbage", "noparam", "swap"]


class Damage:
    """touched = indices of the access units of the ORIGINAL stream that the corruption may have damaged."""

    def __init__(self, kind, desc, touched):
        self.kind, self.desc, self.touched = kind, desc, set(touched)
        self.last_au = max(self.touched) if self.touched else -1
        self.new_ps = False        # the damage created an SPS/PPS that is not in the original (e.g. a bit flip in a NAL header)


def _au_at(segs, off):
    for s in segs:
        if s.start <= off < s.end:
            return s.au
    return segs[-1].au


def _ps_payloads(data):
    return {data[s.start:s.end].lstrip(b"\x00").rstrip(b"\x00") for s in scan(data) if s.type in (7, 8)}


def corrupt(data, kind, seed, name=""):
    """Deterministic corruption of an Annex B stream.  -> (new bytes, Damage)."""
    new, dmg = _corrupt(data, kind, seed, name)
    dmg.new_ps = bool(_ps_payloads(new) - _ps_payloads(data))
    return new, dmg


def _corrupt(data, kind, seed, name):
    rng = random.Random(f"{name}|{kind}|{seed}")
    segs = scan(data)
    n = len(data)
    vcl = [s for s in segs if s.type in (1, 5)]
    if kind == "bitflip":
        k = rng.randint(1, 6)
        b = bytearray(data)
        offs = sorted(rng.randrange(0, n) for _ in range(k))
        for o in offs:
            b[o] ^= 1 << rng.randrange(8)
        return bytes(b), Damage(kind, f"flip {k} bit(s) at byte offsets {offs}", [_au_at(segs, o) for o in offs])
    if kind == "zerofill":
        ln = rng.randint(4, 64)
        o = rng.randrange(0, max(1, n - ln))
        b = bytearray(data)
        b[o:o + ln] = bytes(ln)
        return bytes(b), Damage(kind, f"zero {ln} bytes at offset {o}", [_au_at(segs, o), _au_at(segs, min(n - 1, o + ln - 1))])
    if kind == "cutmid":
        ln = rng.randint(8, max(9, n // 6))
        o = rng.randrange(n // 10, max(n // 10 + 1, n - ln))
        return data[:o] + data[o + ln:], Damage(kind, f"remove {ln} bytes at offset {o}", [_au_at(segs, o), _au_at(segs, min(n - 1, o + ln))])
    if kind == "truncate":
        o = rng.randrange(n // 5, n)
        return data[:o], Damage(kind, f"truncate to {o} of {n} bytes", range(_au_at(segs, o - 1), segs[-1].au + 1))
    if kind == "dropau":
        a = rng.choice(sorted({s.au for s in segs}))
        return b"".join(data[s.start:s.end] for s in segs if s.au != a), Damage(kind, f"drop all NAL units of access unit {a}", [a])
    if kind == "loseslice":
        multi = [s for s in vcl if sum(1 for t in vcl if t.au == s.au) > 1]
        s = rng.choice(multi or vcl)
        return data[:s.start] + data[s.end:], Damage(kind, f"drop one slice NAL unit (AU {s.au}, offset {s.start})", [s.au])
    if kind == "garbage":
        o = rng.choice([s.start for s in segs] + [rng.randrange(0, n)])
        g = bytearray(rng.randrange(256) for _ in range(rng.randint(8, 160)))
        if rng.random() < 0.6:
            hdr = rng.choice([0x01, 0x21, 0x41, 0x61, 0x05, 0x25, 0x45, 0x65, 0x06, 0x09, 0x0C, 0x1F, 0x7F, 0x00])
            g = bytearray(b"\x00\x00\x01" + bytes([hdr])) + g
        return data[:o] + bytes(g) + data[o:], Damage(kind, f"insert {len(g)} garbage bytes at offset {o}",
                                                      [_au_at(segs, min(n - 1, o)), _au_at(segs, max(0, o - 1))])
    if kind == "noparam":
        ps = [s for s in segs if s.type in (7, 8)]
        if not ps:
            return data, Damage(kind, "no parameter sets to drop", [])
        mode = rng.choice(["first_au", "one", "all"])
        drop = [s for s in ps if s.au == ps[0].au] if mode == "first_au" else ([rng.choice(ps)] if mode == "one" else ps)
        ids = {id(s) for s in drop}
        return (b"".join(data[s.start:s.end] for s in segs if id(s) not in ids),
                Damage(kind, f"drop {len(drop)} SPS/PPS NAL unit(s) ({mode})", [s.au for s in drop]))
    if kind == "swap":
        if len(segs) < 3:
            return data, Damage(kind, "nothing to swap", [])
        i = rng.randrange(0, len(segs) - 1)
        a, b = segs[i], segs[i + 1]
        return (data[:a.start] + data[b.start:b.end] + data[a.start:a.end] + data[b.end:],
                Damage(kind, f"swap NAL units {i} and {i + 1}", [a.au, b.au]))
    raise ValueError(kind)


def recovery_ordinal(segs, dmg, guard=1):
    """-> ordinal (among the IDR pictures of the original stream) of the first IDR at which a decoder MUST be correct again,
    or None when there is none.  The IDR must lie after every damaged access unit (plus `guard` more AUs: a decoder may finish the damaged picture
    late and bleed it into the next one), and an SPS and a PPS that were not
    damaged must precede it (or be in its own AU).  When the damage created a parameter set that was not in the original
    stream, that bogus SPS/PPS may replace a good one, so the good SPS and PPS must be re-sent after the damage."""
    if dmg.last_au < 0:
        return None
    lo = dmg.last_au if dmg.new_ps else -1
    for k, a in enumerate(idr_aus(segs)):
        if a <= dmg.last_au + guard:
            continue
        types = {s.type for s in segs if s.type in (7, 8) and lo < s.au <= a and s.au not in dmg.touched}
        if types >= {7, 8}:
            return k
    return None
