#!/usr/bin/env python3
"""Chapter 9.2 harness: YCbCr 4:2:0 -> RGB and PPM output.  Python 3 standard library only (+ ffmpeg / ffprobe).

  python3 test_colour.py --make-variants DIR          write DIR/cases.txt, the YUV files and the .ts streams (reader input)
  python3 test_colour.py DIR --out OUT [--case NAME ...] [--vui vui.txt]
                                                      check OUT/<case>/frame_NNNNN.ppm for every case in DIR/cases.txt
  python3 test_colour.py DIR --out OUT --reader 'CMD {yuv} {width} {height} {matrix} {range} {upsample} {frames} {outdir}'
                                                      run CMD per case first (stdin closed, 300 s timeout), then check
  python3 test_colour.py --self-test                  verify this harness: a good reader passes, >= 12 wrong rules are caught

cases.txt line:  CASE <name> yuv=<file> width=<n> height=<n> frames=<i,j,..> matrix=<bt709|bt601> range=<limited|full>
                 upsample=<nearest|bilinear|bilinear_left> expect=<formula|ffmpeg> [ts=<file>] [vui_mc=<n|->] [vui_full=<0|1|->] [optional=1]
Exit codes: 0 pass, 1 fail (colours outside tolerance), 2 missing / malformed input or PPM.
"""
import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import colour_ref as C  # noqa: E402
import synth  # noqa: E402

CACHE = os.path.join(testfiles.CACHE, "ch9_2")
# tolerance (max abs error, mean abs error, |mean signed error|) per channel, by (expect, upsample)
TOL = {("formula", "nearest"): (2, 0.6, 0.25), ("formula", "bilinear"): (2, 0.6, 0.25), ("formula", "bilinear_left"): (2, 0.6, 0.25),
       ("ffmpeg", "nearest"): (2, 0.6, 0.25), ("ffmpeg", "bilinear"): (4, 1.0, 0.4), ("ffmpeg", "bilinear_left"): (4, 1.0, 0.4)}
MC_FROM_FFPROBE = {"bt709": "1", "smpte170m": "6", "bt470bg": "5", "bt2020nc": "9", "smpte240m": "7", "fcc": "4"}
SW_FLAGS = "{}+accurate_rnd+full_chroma_int"
LOC = {"nearest": "left", "bilinear": "center", "bilinear_left": "left"}


class Fatal(Exception):
    """Input missing / unreadable -> exit 2."""


# ----------------------------------------------------------------------------------------------- cases
def parse_cases(path):
    cases = []
    base = os.path.dirname(os.path.abspath(path))
    try:
        lines = open(path).read().splitlines()
    except OSError as e:
        raise Fatal(f"cannot read {path}: {e}")
    for ln, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] != "CASE" or len(parts) < 3:
            raise Fatal(f"{path}:{ln}: expected 'CASE <name> key=value ...'")
        d = {"name": parts[1], "base": base, "optional": False}
        for kv in parts[2:]:
            k, _, v = kv.partition("=")
            d[k] = v
        d["optional"] = d.get("optional") == "1"
        for k in ("yuv", "width", "height", "frames", "matrix", "range", "upsample", "expect"):
            if k not in d:
                raise Fatal(f"{path}:{ln}: missing {k}=")
        d["width"], d["height"] = int(d["width"]), int(d["height"])
        d["frames"] = [int(t) for t in d["frames"].split(",")]
        d["yuv_path"] = os.path.join(base, d["yuv"])
        cases.append(d)
    if not cases:
        raise Fatal(f"{path}: no cases")
    return cases


def case_line(c):
    s = (f"CASE {c['name']} yuv={c['yuv']} width={c['width']} height={c['height']} frames={','.join(map(str, c['frames']))} "
         f"matrix={c['matrix']} range={c['range']} upsample={c['upsample']} expect={c['expect']}")
    for k in ("ts", "vui_mc", "vui_full"):
        if k in c:
            s += f" {k}={c[k]}"
    return s + (" optional=1" if c.get("optional") else "")


# ----------------------------------------------------------------------------------------------- PPM
def parse_ppm(data):
    """-> (w, h, rgb) or raises ValueError with a precise message."""
    if data[:2] != b"P6":
        raise ValueError(f"magic number is {data[:2]!r}, expected b'P6' (binary PPM)")
    pos, toks = 2, []
    while len(toks) < 3:
        while pos < len(data) and data[pos:pos + 1] in b" \t\r\n":
            pos += 1
        if pos < len(data) and data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos:pos + 1] != b"\n":
                pos += 1
            continue
        s = pos
        while pos < len(data) and data[pos:pos + 1] in b"0123456789":
            pos += 1
        if s == pos:
            raise ValueError("header: expected 'P6', width, height and maxval as decimal numbers separated by whitespace")
        toks.append(int(data[s:pos]))
    if pos >= len(data) or data[pos:pos + 1] not in b" \t\r\n":
        raise ValueError("header: maxval must be followed by exactly one whitespace byte, then the raster")
    w, h, mx = toks
    if mx != 255:
        raise ValueError(f"maxval is {mx}, must be 255 (8-bit samples)")
    raster = data[pos + 1:]
    if len(raster) != w * h * 3:
        raise ValueError(f"raster is {len(raster)} bytes, expected width*height*3 = {w}*{h}*3 = {w * h * 3}"
                         + (" (trailing bytes after the last pixel)" if len(raster) > w * h * 3 else " (truncated)"))
    return w, h, raster


# ----------------------------------------------------------------------------------------------- expected output
def ffmpeg_rgb(c, cache):
    """frames -> rgb bytes, from ffmpeg's swscale (flags chosen so that it is reproducible, see Readme 6)."""
    key = (c["yuv_path"], c["width"], c["height"], c["matrix"], c["range"], c["upsample"], tuple(c["frames"]))
    if key in cache:
        return cache[key]
    w, h = c["width"], c["height"]
    sel = "+".join(f"eq(n,{n})" for n in sorted(set(c["frames"])))
    flags = SW_FLAGS.format("neighbor" if c["upsample"] == "nearest" else "bilinear")
    vf = (f"select='{sel}',scale=in_range={'pc' if c['range'] == 'full' else 'tv'}:out_range=pc:in_color_matrix={c['matrix']}"
          + (f":in_chroma_loc={LOC[c['upsample']]}" if c["upsample"] != "nearest" else "") + f":flags={flags}")
    tmp = tempfile.mkdtemp(prefix="ch92_")
    try:
        out = os.path.join(tmp, "o.rgb")
        p = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-i", c["yuv_path"],
                       "-vf", vf, "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", out], timeout=300)
        if p.returncode != 0 or not os.path.exists(out):
            raise Fatal("ffmpeg reference conversion failed: " + p.stderr.decode()[-300:])
        data = open(out, "rb").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    fs = w * h * 3
    order = sorted(set(c["frames"]))
    res = {n: data[i * fs:(i + 1) * fs] for i, n in enumerate(order)}
    cache[key] = res
    return res


def formula_rgb(c, data, n):
    Y, U, V, _, _ = C.planes(data, c["width"], c["height"], n)
    return C.convert(Y, U, V, c["width"], c["height"], c["matrix"], c["range"] == "full", c["upsample"])


# ----------------------------------------------------------------------------------------------- metrics, diagnosis
def metrics(exp, got):
    """per channel (max, mean, bias, first index of max, first index exceeding nothing yet)."""
    n = len(exp) // 3
    res = []
    for ch in range(3):
        e, g = exp[ch::3], got[ch::3]
        mx, sm, sg, at = 0, 0, 0, 0
        for i in range(n):
            d = g[i] - e[i]
            a = d if d >= 0 else -d
            sm += a
            sg += d
            if a > mx:
                mx, at = a, i
        res.append((mx, sm / n, sg / n, at))
    return res


def diagnose(c, yuvdata, n, got, loc):
    """Try alternative interpretations of the YUV on a 96x16 window around pixel index `loc`; say which one matches."""
    w, h = c["width"], c["height"]
    Y, U, V, _, _ = C.planes(yuvdata, w, h, n)
    ww, hh = min(96, w), min(16, h)
    x0 = max(0, min((loc % w) - ww // 2, w - ww)) & ~1
    y0 = max(0, min((loc // w) - hh // 2, h - hh)) & ~1
    gw = bytearray()
    for y in range(y0, y0 + hh):
        gw += got[(y * w + x0) * 3:(y * w + x0 + ww) * 3]
    gw = bytes(gw)
    base = dict(matrix=c["matrix"], full=c["range"] == "full", upsample=c["upsample"], swap_uv=False, dx=0, dy=0,
                trunc=False, clamp=True, order="rgb")
    other = {"matrix": "bt601" if c["matrix"] == "bt709" else "bt709"}
    singles = [("matrix", other["matrix"]), ("full", not base["full"]), ("swap_uv", True), ("dx", 1), ("dx", -1), ("dy", 1),
               ("dy", -1), ("trunc", True), ("clamp", False), ("order", "bgr")]
    singles += [("upsample", u) for u in ("nearest", "bilinear", "bilinear_left") if u != c["upsample"]]
    cache = {}

    def score(cfg):
        k = tuple(sorted(cfg.items()))
        if k not in cache:
            rgb = C.convert(Y, U, V, w, h, cfg["matrix"], cfg["full"], cfg["upsample"], window=(x0, y0, ww, hh),
                            swap_uv=cfg["swap_uv"], dx=cfg["dx"], dy=cfg["dy"], trunc=cfg["trunc"], clamp=cfg["clamp"],
                            order=cfg["order"])
            m = metrics(rgb, gw)
            cache[k] = (max(t[1] for t in m), max(t[0] for t in m))
        return cache[k]

    def ok(s):
        return s[0] <= 0.6 and s[1] <= 2

    def apply(cfg, ch):
        cfg = dict(cfg)
        cfg[ch[0]] = ch[1]
        return cfg
    best = None
    for ch in singles:
        s = score(apply(base, ch))
        if ok(s) and (best is None or s < best[0]):
            best = (s, [ch])
    if best is None:
        for i, a in enumerate(singles):
            for b in singles[i + 1:]:
                if a[0] == b[0]:
                    continue
                s = score(apply(apply(base, a), b))
                if ok(s) and (best is None or s < best[0]):
                    best = (s, [a, b])
    if best is None:
        return "DIAGNOSIS: no common mistake (matrix, range, Cb/Cr swap, chroma shift, upsampling, rounding, clamping, BGR order) " \
               "reproduces your output near the first bad pixel"
    words = []
    for k, v in best[1]:
        words.append({
            "matrix": lambda: f"matrix: your output looks like {v.upper().replace('BT', 'BT.')} where {c['matrix'].upper().replace('BT', 'BT.')} was expected",
            "full": lambda: f"range: your output looks like {'full' if v else 'limited'}-range conversion where {c['range']} was expected",
            "swap_uv": lambda: "Cb and Cr look swapped (U used as V and V as U)",
            "dx": lambda: f"chroma looks shifted horizontally: you read chroma column x/2{v:+d}",
            "dy": lambda: f"chroma looks shifted by one row: you read chroma row y/2{v:+d}",
            "trunc": lambda: "values look truncated instead of rounded (add 0.5 / the rounding term before the shift)",
            "clamp": lambda: "values look unclamped (wrap-around instead of clamp to 0..255)",
            "order": lambda: "channels look written as B,G,R instead of R,G,B",
            "upsample": lambda: f"chroma upsampling looks like '{v}' where '{c['upsample']}' was expected",
        }[k]())
    return "DIAGNOSIS: " + "; ".join(words) + f"  (window x={x0}..{x0 + ww - 1}, y={y0}..{y0 + hh - 1}, mean err {best[0][0]:.2f})"


# ----------------------------------------------------------------------------------------------- the check
def check_cases(cases, outroot, rep, cache, only=None, log=print):
    """Returns True when an output file was missing or malformed (exit 2)."""
    missing = False
    for c in cases:
        if only and c["name"] not in only:
            continue
        cdir = os.path.join(outroot, c["name"])
        if not os.path.isdir(cdir):
            if c["optional"]:
                log(f"  {c['name']}: optional case, no output directory -> skipped")
                continue
            log(f"  ERROR: {c['name']}: output directory {cdir} is missing")
            missing = True
            continue
        w, h = c["width"], c["height"]
        try:
            yuv = open(c["yuv_path"], "rb").read()
        except OSError as e:
            raise Fatal(f"cannot read {c['yuv_path']}: {e}")
        fs = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
        if len(yuv) % fs or max(c["frames"]) >= len(yuv) // fs:
            raise Fatal(f"{c['yuv']}: size {len(yuv)} is not a whole number of {w}x{h} frames (or frame list out of range)")
        # the matrix/range of a stream-based case must follow from the stream (guards against edited cases.txt)
        if "ts" in c:
            m, r = derive_from_stream(os.path.join(c["base"], c["ts"]), h)
            rep.check((m, r) == (c["matrix"], c["range"]), f"{c['name']}: cases.txt says {c['matrix']}/{c['range']} but the stream's VUI "
                       f"gives {m}/{r}")
        exp_all = ffmpeg_rgb(c, cache) if c["expect"] == "ffmpeg" else None
        tol = TOL[(c["expect"], c["upsample"])]
        case_bad = False
        for n in c["frames"]:
            path = os.path.join(cdir, "frame_%05d.ppm" % n)
            tag = f"{c['name']} frame {n}"
            try:
                data = open(path, "rb").read()
            except OSError:
                log(f"  ERROR: {tag}: file {path} is missing")
                missing = True
                continue
            try:
                pw, ph, got = parse_ppm(data)
            except ValueError as e:
                log(f"  ERROR: {tag}: invalid PPM {path}: {e}")
                missing = True
                continue
            if (pw, ph) != (w, h):
                log(f"  ERROR: {tag}: PPM header says {pw}x{ph}, expected {w}x{h} (the YUV frame size)")
                missing = True
                continue
            exp = exp_all[n] if exp_all else formula_rgb(c, yuv, n)
            ms = metrics(exp, got)
            bad = [(nm, m) for nm, m in zip("RGB", ms) if m[0] > tol[0] or m[1] > tol[1] or abs(m[2]) > tol[2]]
            if not bad:
                rep.check(True, "")
                continue
            worst = max(ms, key=lambda m: m[0])
            idx = worst[3]
            first = next((i for i in range(w * h) if any(abs(got[3 * i + k] - exp[3 * i + k]) > tol[0] for k in range(3))), idx)
            x, y = first % w, first // w
            fy, fu, fv = yuv[n * fs + y * w + x], None, None
            cw = (w + 1) // 2
            fu = yuv[n * fs + w * h + (y // 2) * cw + x // 2]
            fv = yuv[n * fs + w * h + ((h + 1) // 2) * cw + (y // 2) * cw + x // 2]
            txt = ", ".join(f"{nm}: max {m[0]} mean {m[1]:.2f} bias {m[2]:+.2f}" for nm, m in zip("RGB", ms))
            rep.check(False, f"{tag}: outside tolerance (max<={tol[0]}, mean<={tol[1]}, |bias|<={tol[2]}): {txt}; first bad pixel "
                      f"x={x} y={y}: Y={fy} Cb={fu} Cr={fv} expected RGB={tuple(exp[3 * first:3 * first + 3])} got {tuple(got[3 * first:3 * first + 3])}")
            if not case_bad:
                case_bad = True
                loc = first if any(abs(got[3 * first + k] - exp[3 * first + k]) > tol[0] for k in range(3)) else idx
                rep.failures.append("  " + diagnose(c, yuv, n, got, loc))
    return missing


def derive_from_stream(ts, height):
    """(matrix name, range name) as the rule of this chapter derives them from the stream's VUI."""
    info = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")["streams"][0]
    cs, cr = info.get("color_space", "unknown"), info.get("color_range", "unknown")
    mc = int(MC_FROM_FFPROBE[cs]) if cs in MC_FROM_FFPROBE else None
    return C.matrix_for(mc, int(info["height"])), ("full" if cr == "pc" else "limited")


def check_vui(cases, path, rep):
    try:
        lines = [l.split() for l in open(path).read().splitlines() if l.strip() and not l.startswith("#")]
    except OSError as e:
        raise Fatal(f"cannot read {path}: {e}")
    got = {}
    for t in lines:
        if len(t) != 4 or t[0] != "VUI":
            raise Fatal(f"{path}: expected 'VUI <case> mc=<n|-> full=<0|1|->', got {' '.join(t)!r}")
        got[t[1]] = (t[2].partition("=")[2], t[3].partition("=")[2])
    for c in cases:
        if "vui_mc" not in c:
            continue
        if c["name"] not in got:
            rep.check(False, f"VUI: no line for case {c['name']}")
            continue
        mc, fr = got[c["name"]]
        rep.check(mc == c["vui_mc"] or (c["vui_mc"] == "-" and mc == "2"), f"VUI {c['name']}: matrix_coefficients expected {c['vui_mc']}, got {mc}")
        rep.check(fr == c["vui_full"] or (c["vui_full"] == "-" and fr == "0"),
                  f"VUI {c['name']}: video_full_range_flag expected {c['vui_full']}, got {fr}")


# ----------------------------------------------------------------------------------------------- variants
def sh(cmd, timeout=300):
    p = ffref.run(cmd, timeout=timeout)
    if p.returncode != 0:
        raise Fatal(f"command failed: {' '.join(cmd[:6])}...: {p.stderr.decode()[-300:]}")


def make_stream(path, size, extra, seconds, bframes_opts="bframes=3:b-pyramid=normal:keyint=50"):
    sh(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25", "-t", str(seconds), "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-qp", "26", "-profile:v", "high", "-x264-params", f"{bframes_opts}{extra}", "-f", "mpegts", path])


def decode_native(ts, out_yuv):
    """Decode without any pixel-format conversion (yuvj420p stays full-range data)."""
    info = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")["streams"][0]
    if info["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        raise Fatal(f"{ts}: pix_fmt {info['pix_fmt']} is not 4:2:0 8-bit")
    sh(["ffmpeg", "-v", "error", "-y", "-i", ts, "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo", out_yuv])
    return int(info["width"]), int(info["height"])


def make_variants(d):
    os.makedirs(d, exist_ok=True)
    ffref.require_tools()
    lines = ["# Chapter 9.2 cases.  Frames are 0-based indices into the YUV file.  expect=formula: exact formula of the Readme;",
             "# expect=ffmpeg: swscale with neighbor / bilinear + accurate_rnd + full_chroma_int."]
    open(os.path.join(d, "synthetic.yuv"), "wb").write(synth.build())
    allf = ",".join(str(i) for i in range(synth.NFRAMES))
    for m in ("709", "601"):
        for r in ("limited", "full"):
            lines.append(f"CASE syn_{m}_{r} yuv=synthetic.yuv width=96 height=64 frames={allf} matrix=bt{m} range={r} upsample=nearest expect=formula")
    lines.append(f"CASE syn_bilinear_left yuv=synthetic.yuv width=96 height=64 frames={allf} matrix=bt709 range=limited upsample=bilinear_left expect=formula optional=1")
    lines.append(f"CASE syn_bilinear_center yuv=synthetic.yuv width=96 height=64 frames={allf} matrix=bt601 range=full upsample=bilinear expect=formula optional=1")
    real = [  # name, source, size, x264 extras, seconds, frames
        ("sd_untagged", "T9_spatial", None, "", 2, "0,9,30"),
        ("sd_bt709", None, "352x288", ":colormatrix=bt709", 2, "0,12,31"),
        ("sd_bt601_full", None, "352x288", ":colormatrix=smpte170m:fullrange=on", 2, "0,12,31"),
        ("sd_bt470bg", None, "352x288", ":colormatrix=bt470bg", 2, "1,20"),
        ("hd_untagged", None, "1280x720", "", 0.2, "0,4"),
        ("hd_bt709_full", None, "1280x720", ":colormatrix=bt709:fullrange=on", 0.2, "0,4"),
    ]
    for name, src, size, extra, secs, frames in real:
        ts = os.path.join(d, name + ".ts")
        if src:
            shutil.copyfile(testfiles.get(src), ts)
        else:
            make_stream(ts, size, extra, secs)
        w, h = decode_native(ts, os.path.join(d, name + ".yuv"))
        info = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")["streams"][0]
        cs, cr = info.get("color_space", "unknown"), info.get("color_range", "unknown")
        matrix, rng = derive_from_stream(ts, h)
        nfr = os.path.getsize(os.path.join(d, name + ".yuv")) // (w * h * 3 // 2)
        fr = ",".join(f for f in frames.split(",") if int(f) < nfr)
        c = dict(name=name, yuv=name + ".yuv", width=w, height=h, frames=[int(t) for t in fr.split(",")], matrix=matrix, range=rng,
                 upsample="nearest", expect="ffmpeg", ts=name + ".ts", vui_mc=MC_FROM_FFPROBE.get(cs, "-"), vui_full="1" if cr == "pc" else ("0" if cr == "tv" else "-"))
        lines.append(case_line(c))
    c = dict(name="sd_bt709_bilinear", yuv="sd_bt709.yuv", width=352, height=288, frames=[12], matrix="bt709", range="limited",
             upsample="bilinear_left", expect="ffmpeg", ts="sd_bt709.ts", optional=True)
    lines.append(case_line(c))
    open(os.path.join(d, "cases.txt"), "w").write("\n".join(lines) + "\n")
    print(f"wrote {d}/cases.txt ({len(lines) - 2} cases)")
    print("Reader convention: for each case write OUT/<case>/frame_<NNNNN>.ppm, NNNNN = 5-digit 0-based frame index, for every frame in frames=.")


# ----------------------------------------------------------------------------------------------- run
def run_reader(cmd, cases, outroot, only=None):
    for c in cases:
        if only and c["name"] not in only:
            continue
        if c["optional"] and "{upsample}" not in cmd:
            continue
        od = os.path.join(outroot, c["name"])
        os.makedirs(od, exist_ok=True)
        args = cmd.format(yuv=shlex.quote(c["yuv_path"]), width=c["width"], height=c["height"], matrix=c["matrix"], range=c["range"],
                          upsample=c["upsample"], frames=",".join(map(str, c["frames"])), outdir=shlex.quote(od), case=c["name"])
        try:
            p = subprocess.run(args, shell=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise Fatal(f"reader timed out on case {c['name']}")
        if p.returncode != 0:
            print(f"  reader failed on {c['name']} (exit {p.returncode}): {p.stderr.decode()[-200:]}")


def finish(rep, missing, quiet=False):
    for m in rep.failures[:rep.max_shown + 12]:
        if not quiet:
            print(m if m.startswith("  ") else "  FAIL: " + m)
    ok = not rep.failures and not missing
    if not quiet:
        nbad = len([f for f in rep.failures if not f.startswith('  ')])
        print(f"{'PASS' if ok else 'FAIL'}: Chapter 9.2 colour conversion ({rep.checks - nbad}/{rep.checks} checks passed)"
              + ("; some output files are missing or malformed (see ERROR lines), exit 2" if missing else ""))
    return 2 if missing else (1 if rep.failures else 0)


# ----------------------------------------------------------------------------------------------- self-test
def self_test():
    ffref.require_tools()
    tmp = tempfile.mkdtemp(prefix="ch92_self_")
    fails = []
    try:
        os.makedirs(CACHE, exist_ok=True)
        # reference calibration: the formula agrees with swscale (flags above) to within 1
        d = os.path.join(tmp, "cases")
        os.makedirs(d)
        open(os.path.join(d, "synthetic.yuv"), "wb").write(synth.build())
        sdyuv = os.path.join(CACHE, "T9_spatial.yuv")
        if not os.path.exists(sdyuv):
            decode_native(testfiles.get("T9_spatial"), sdyuv)
        shutil.copyfile(sdyuv, os.path.join(d, "sd.yuv"))
        nsd = os.path.getsize(sdyuv) // (352 * 288 * 3 // 2)
        cl = ["CASE syn_709_lim yuv=synthetic.yuv width=96 height=64 frames=0,1,2,3 matrix=bt709 range=limited upsample=nearest expect=formula",
              "CASE syn_601_full yuv=synthetic.yuv width=96 height=64 frames=0,1,2,3 matrix=bt601 range=full upsample=nearest expect=formula",
              "CASE syn_bil yuv=synthetic.yuv width=96 height=64 frames=1,2,3 matrix=bt709 range=limited upsample=bilinear_left expect=formula optional=1",
              f"CASE sd yuv=sd.yuv width=352 height=288 frames=0,{min(9, nsd - 1)} matrix=bt601 range=limited upsample=nearest expect=ffmpeg"]
        open(os.path.join(d, "cases.txt"), "w").write("\n".join(cl) + "\n")
        cases = parse_cases(os.path.join(d, "cases.txt"))
        by = {c["name"]: c for c in cases}
        cache = {}
        yuvs = {c["name"]: open(c["yuv_path"], "rb").read() for c in cases}
        cal = 0
        for nm in ("syn_709_lim", "syn_601_full", "syn_bil"):
            c = dict(by[nm]); c["expect"] = "ffmpeg"
            if nm.startswith("syn"):
                e = ffmpeg_rgb(c, cache)
                for n in [t for t in c["frames"] if t in (1, 2)]:
                    m = metrics(formula_rgb(c, yuvs[nm], n), e[n])
                    cal = max(cal, max(t[0] for t in m))
        print(f"reference calibration: formula vs swscale on synthetic ramp+edge frames: max abs error {cal} (limit 1)")
        if cal > 1:
            fails.append("formula disagrees with swscale")
        # matrix rule
        for mc, hgt, want in ((1, 288, "bt709"), (5, 720, "bt601"), (6, 1080, "bt601"), (2, 576, "bt601"), (2, 577, "bt709"), (None, 720, "bt709"), (2, 1080, "bt709")):
            if C.matrix_for(mc, hgt) != want:
                fails.append(f"matrix_for({mc},{hgt}) != {want}")

        # good reader output = the spoiler fixed-point reader, run through its CLI
        outg = os.path.join(tmp, "good")
        sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
        import colour_reader
        for c in cases:
            colour_reader.main([c["yuv_path"], str(c["width"]), str(c["height"]), c["matrix"], c["range"], c["upsample"],
                                ",".join(map(str, c["frames"])), os.path.join(outg, c["name"])])
        rep = Report("good reader")
        miss = check_cases(cases, outg, rep, cache, log=print)
        rc = finish(rep, miss)
        if rc != 0:
            fails.append("good reader did not pass")

        def good_rgb(c, n):
            return parse_ppm(open(os.path.join(outg, c["name"], "frame_%05d.ppm" % n), "rb").read())[2]

        def ppm(w, h, rgb, magic=b"P6", mx=b"255", sep=b"\n"):
            return magic + b"\n%d %d\n" % (w, h) + mx + sep + rgb

        def convrule(**kw):
            def f(c, n, w, h, rgb):
                Y, U, V, _, _ = C.planes(yuvs[c["name"]], w, h, n)
                args = dict(matrix=c["matrix"], full=c["range"] == "full", upsample=c["upsample"])
                args.update(kw)
                if "matrix_other" in args:
                    args["matrix"] = "bt601" if c["matrix"] == "bt709" else "bt709"; del args["matrix_other"]
                if args.pop("flip_range", False):
                    args["full"] = not args["full"]
                return ppm(w, h, C.convert(Y, U, V, w, h, **args))
            return f
        grey = lambda c, n, w, h, rgb: ppm(w, h, bytes(v for i in range(0, len(rgb), 3) for v in (rgb[i],) * 3))
        rules = [  # (description, targets, function(case, n, w, h, rgb) -> file bytes or None, text expected in output)
            ("wrong matrix (BT.601 used for BT.709)", ["syn_709_lim"], convrule(matrix_other=1), "BT.601 where BT.709"),
            ("wrong matrix (BT.709 used on SD untagged stream)", ["sd"], convrule(matrix="bt709"), "BT.709 where BT.601"),
            ("limited range treated as full", ["syn_709_lim"], convrule(full=True), "full-range"),
            ("full range treated as limited", ["syn_601_full"], convrule(full=False), "limited-range"),
            ("truncation instead of rounding", ["syn_709_lim"], convrule(trunc=True), "truncated"),
            ("no clamp (wraps)", ["syn_709_lim"], convrule(clamp=False), "unclamped"),
            ("Cb and Cr swapped", ["syn_709_lim"], convrule(swap_uv=True), "swapped"),
            ("chroma shifted one sample right", ["syn_709_lim"], convrule(dx=1), "shifted horizontally"),
            ("chroma off by one row", ["syn_709_lim", "sd"], convrule(dy=1), "one row"),
            ("BGR byte order", ["syn_709_lim"], convrule(order="bgr"), "B,G,R"),
            ("nearest used where bilinear expected", ["syn_bil"], convrule(upsample="nearest"), "upsampling"),
            ("luma only (grey image)", ["syn_709_lim"], grey, None),
            ("PPM magic P3", ["syn_709_lim"], lambda c, n, w, h, rgb: ppm(w, h, rgb, magic=b"P3"), "magic"),
            ("PPM maxval 65535", ["syn_709_lim"], lambda c, n, w, h, rgb: ppm(w, h, rgb, mx=b"65535"), "maxval"),
            ("PPM width and height swapped in header", ["syn_709_lim"], lambda c, n, w, h, rgb: ppm(h, w, rgb), "says 64x96"),
            ("PPM raster truncated (last row missing)", ["syn_709_lim"], lambda c, n, w, h, rgb: ppm(w, h, rgb[:-w * 3]), "truncated"),
            ("PPM trailing bytes", ["syn_709_lim"], lambda c, n, w, h, rgb: ppm(w, h, rgb + b"\n"), "trailing"),
            ("PPM no whitespace after maxval", ["syn_709_lim"], lambda c, n, w, h, rgb: ppm(w, h, rgb, sep=b""), "whitespace"),
            ("frame file not written", ["syn_709_lim"], lambda c, n, w, h, rgb: None if n == 2 else ppm(w, h, rgb), "missing"),
        ]
        results = []
        for desc, targets, fn, want in rules:
            outb = os.path.join(tmp, "bad")
            shutil.rmtree(outb, ignore_errors=True)
            for nm in targets:
                c = by[nm]
                os.makedirs(os.path.join(outb, nm))
                for n in c["frames"]:
                    b = fn(c, n, c["width"], c["height"], good_rgb(c, n))
                    if b is not None:
                        open(os.path.join(outb, nm, "frame_%05d.ppm" % n), "wb").write(b)
            rep = Report(desc)
            lines = []
            miss = check_cases(cases, outb, rep, cache, only=targets, log=lines.append)
            rc = finish(rep, miss, quiet=True)
            text = "\n".join(lines + rep.failures)
            caught = rc == (2 if desc.startswith(("PPM", "frame file")) else 1)   # malformed / missing -> 2, wrong colours -> 1
            diag_ok = want is None or want.lower() in text.lower()
            print(f"  [{'caught' if caught else 'NOT CAUGHT (or wrong exit code)'}] {desc} -> exit {rc}" + ("" if diag_ok else f"   (diagnosis text {want!r} not found)"))
            if not caught or not diag_ok:
                fails.append(desc)
                print(text[:600])
        # VUI check
        rep = Report("vui")
        vc = [dict(name="x", vui_mc="1", vui_full="1")]
        p = os.path.join(tmp, "vui.txt")
        open(p, "w").write("VUI x mc=1 full=1\n")
        check_vui(vc, p, rep)
        open(p, "w").write("VUI x mc=6 full=1\n")
        check_vui(vc, p, rep)
        if len(rep.failures) != 1:
            fails.append("vui check")
        print(f"  [{'caught' if len(rep.failures) == 1 else 'NOT CAUGHT'}] wrong VUI matrix_coefficients")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(("SELF-TEST FAIL: " + "; ".join(fails)) if fails else f"SELF-TEST PASS: good reader accepted, {len(rules) + 1} wrong rules caught")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", help="directory with cases.txt (from --make-variants)")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--out", help="directory holding <case>/frame_NNNNN.ppm")
    ap.add_argument("--case", action="append", help="check only this case (repeatable)")
    ap.add_argument("--reader", help="command template run once per case")
    ap.add_argument("--vui", help="file with 'VUI <case> mc=<n|-> full=<0|1|->' lines")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        if a.self_test:
            return self_test()
        if a.make_variants:
            make_variants(a.make_variants)
            return 0
        if not a.dir or not a.out:
            ap.error("need DIR and --out (or --make-variants / --self-test)")
        ffref.require_tools()
        cases = parse_cases(os.path.join(a.dir, "cases.txt"))
        if a.reader:
            run_reader(a.reader, cases, a.out, a.case)
        rep = Report("Chapter 9.2 colour conversion")
        miss = check_cases(cases, a.out, rep, {}, a.case)
        if a.vui:
            check_vui(cases, a.vui, rep)
        return finish(rep, miss)
    except Fatal as e:
        print("error:", e)
        return 2
    except subprocess.TimeoutExpired:
        print("error: a subprocess timed out")
        return 2


if __name__ == "__main__":
    sys.exit(main())
