#!/usr/bin/env python3
"""Chapter 9.3 harness: window and frame presentation, verified headlessly.  Python 3 standard library only (+ ffmpeg / ffprobe).

This harness never opens a window, never reads stdin and runs your player only with a time limit (process group killed after 120 s).

  python3 test_present.py --make-variants DIR       write DIR/cases.txt, YUV files, .ts streams, <case>.script and <case>.expected.log
  python3 test_present.py DIR --out OUT [--case NAME ...]
                                                    check OUT/<case>/log.txt and OUT/<case>/dump/present_NNNNN.{ppm,raw}
  python3 test_present.py DIR --out OUT --reader 'CMD {yuv} {size} ...'
                                                    run CMD once per case first (see Readme 6), then check
  python3 test_present.py --self-test               verify this harness: a good player passes, >= 12 wrong behaviours are caught

Exit codes: 0 pass, 1 wrong behaviour / wrong pixels, 2 missing or malformed input (log, dump files, PPM, cases).
"""
import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref, testfiles  # noqa: E402
from common.report import Report  # noqa: E402
import presenter as P  # noqa: E402
import synth  # noqa: E402
import player  # noqa: E402

TOL = (2, 0.6, 0.25)          # per channel: max abs error, mean abs error, |mean signed error|  (same numbers as Chapter 9.2)
TIMEOUT = 120
SW_FLAGS = "neighbor+accurate_rnd+full_chroma_int"
RB_SWAP = {"bgrx": "rgbx", "rgbx": "bgrx", "bgra": "rgba", "rgba": "bgra", "rgb24": "bgr24", "bgr24": "rgb24"}


class Fatal(Exception):
    """Input missing / malformed -> exit 2."""


# ----------------------------------------------------------------------------------------------- cases
KEYS = ("yuv", "size", "aspect_idc", "sar_ext", "matrix", "range", "window", "format", "align", "origin", "script", "expect")


def parse_cases(path):
    base = os.path.dirname(os.path.abspath(path))
    try:
        lines = open(path).read().splitlines()
    except OSError as e:
        raise Fatal(f"cannot read {path}: {e}")
    cases = []
    for ln, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        t = line.split()
        if t[0] != "CASE" or len(t) < 3:
            raise Fatal(f"{path}:{ln}: expected 'CASE <name> key=value ...'")
        d = {"name": t[1], "base": base}
        for kv in t[2:]:
            k, _, v = kv.partition("=")
            d[k] = v
        for k in KEYS:
            if k not in d:
                raise Fatal(f"{path}:{ln}: missing {k}=")
        try:
            d["w"], d["h"] = (int(x) for x in d["size"].split("x"))
            d["idc"] = int(d["aspect_idc"])
            d["ext"] = tuple(int(x) for x in d["sar_ext"].split(":"))
            d["alignn"] = int(d["align"])
        except ValueError:
            raise Fatal(f"{path}:{ln}: bad number in size / aspect_idc / sar_ext / align")
        if d["format"] not in P.FORMATS or d["origin"] not in ("topdown", "bottomup"):
            raise Fatal(f"{path}:{ln}: bad format or origin")
        for k in ("yuv", "script"):
            d[k + "_path"] = os.path.join(base, d[k])
        cases.append(d)
    if not cases:
        raise Fatal(f"{path}: no cases")
    return cases


def case_line(c):
    s = " ".join(f"{k}={c[k]}" for k in KEYS)
    return f"CASE {c['name']} {s}" + (f" ts={c['ts']}" if "ts" in c else "")


# ----------------------------------------------------------------------------------------------- expected behaviour
def expected(case):
    """-> header dict, presents [(frame, reason, window, script line)], end reason, display size"""
    yuvsize = os.path.getsize(case["yuv_path"])
    w, h = case["w"], case["h"]
    fs = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
    if yuvsize == 0 or yuvsize % fs:
        raise Fatal(f"{case['yuv']}: size {yuvsize} is not a whole number of {w}x{h} frames")
    n = yuvsize // fs
    sar = P.sar_from_idc(case["idc"], case["ext"])
    dw, dh = P.display_size(w, h, sar)
    win0 = (dw, dh) if case["window"] == "auto" else tuple(int(x) for x in case["window"].split("x"))
    try:
        events = P.parse_script(open(case["script_path"]).read())
        presents, end = P.simulate(n, events, win0)
    except (OSError, P.ScriptError) as e:
        raise Fatal(f"{case['script']}: {e}")
    hdr = dict(src=f"{w}x{h}", frames=n, sar=f"{sar[0]}:{sar[1]}", display=f"{dw}x{dh}", format=case["format"], align=case["alignn"],
               origin=case["origin"])
    return hdr, presents, end, (dw, dh), events


def log_lines(case, hdr, presents, end, disp):
    out = ["HEADER src={src} frames={frames} sar={sar} display={display} format={format} align={align} origin={origin}".format(**hdr)]
    for i, (fr, reason, (ww, wh), _) in enumerate(presents):
        r = P.fit_rect(ww, wh, *disp)
        out.append(f"PRESENT {i} frame={fr} reason={reason} window={ww}x{wh} rect={r[0]},{r[1]},{r[2]},{r[3]} format={case['format']} "
                   f"stride={P.stride_for(ww, case['format'], case['alignn'])} origin={case['origin']}")
    out.append(f"END presents={len(presents)} reason={end}")
    return out


# ----------------------------------------------------------------------------------------------- log parsing
RE_H = re.compile(r"HEADER src=(\d+x\d+) frames=(\d+) sar=(\d+:\d+) display=(\d+x\d+) format=(\w+) align=(\d+) origin=(topdown|bottomup)")
RE_P = re.compile(r"PRESENT (\d+) frame=(\d+) reason=(play|step|redraw) window=(\d+)x(\d+) rect=(\d+),(\d+),(\d+),(\d+) "
                  r"format=(\w+) stride=(\d+) origin=(topdown|bottomup)")
RE_E = re.compile(r"END presents=(\d+) reason=(quit|eos|script_end)")


def parse_log(path):
    try:
        text = open(path, encoding="ascii").read()
    except (OSError, UnicodeDecodeError) as e:
        raise Fatal(f"cannot read log {path}: {e}")
    lines = [l.rstrip("\r") for l in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise Fatal(f"{path}: empty log")
    m = RE_H.fullmatch(lines[0])
    if not m:
        raise Fatal(f"{path}:1: not a HEADER line (expected 'HEADER src=WxH frames=N sar=N:D display=WxH format=F align=A origin=O'): {lines[0]!r}")
    hdr = dict(zip(("src", "frames", "sar", "display", "format", "align", "origin"), m.groups()))
    hdr["frames"], hdr["align"] = int(hdr["frames"]), int(hdr["align"])
    presents = []
    for ln, line in enumerate(lines[1:-1], 2):
        m = RE_P.fullmatch(line)
        if not m:
            raise Fatal(f"{path}:{ln}: not a PRESENT line (expected 'PRESENT <idx> frame=<n> reason=<play|step|redraw> window=WxH "
                        f"rect=x,y,w,h format=<fmt> stride=<bytes> origin=<o>'): {line!r}")
        g = m.groups()
        presents.append(dict(idx=int(g[0]), frame=int(g[1]), reason=g[2], window=(int(g[3]), int(g[4])), rect=tuple(int(v) for v in g[5:9]),
                             format=g[9], stride=int(g[10]), origin=g[11]))
    m = RE_E.fullmatch(lines[-1])
    if not m:
        raise Fatal(f"{path}:{len(lines)}: last line must be 'END presents=<n> reason=<quit|eos|script_end>', got {lines[-1]!r}")
    return hdr, presents, (int(m.group(1)), m.group(2))


# ----------------------------------------------------------------------------------------------- PPM
def parse_ppm(data):
    if data[:2] != b"P6":
        raise ValueError(f"magic number is {data[:2]!r}, expected b'P6'")
    pos, toks = 2, []
    while len(toks) < 3:
        while pos < len(data) and data[pos:pos + 1] in (b" ", b"\t", b"\r", b"\n"):
            pos += 1
        s = pos
        while pos < len(data) and data[pos:pos + 1].isdigit():
            pos += 1
        if s == pos:
            raise ValueError("header: expected width, height and maxval as decimal numbers")
        toks.append(int(data[s:pos]))
    if pos >= len(data) or data[pos:pos + 1] not in (b" ", b"\t", b"\r", b"\n"):
        raise ValueError("header: maxval must be followed by one whitespace byte")
    w, h, mx = toks
    if mx != 255:
        raise ValueError(f"maxval is {mx}, must be 255")
    raster = data[pos + 1:]
    if len(raster) != w * h * 3:
        raise ValueError(f"raster is {len(raster)} bytes, expected {w}*{h}*3 = {w * h * 3}")
    return w, h, raster


# ----------------------------------------------------------------------------------------------- metrics
def metrics(exp, got):
    n = max(1, len(exp) // 3)
    res = []
    for c in range(3):
        d = [g - e for e, g in zip(exp[c::3], got[c::3])]
        res.append((max(map(abs, d), default=0), sum(map(abs, d)) / n, sum(d) / n))
    return res


def within(ms):
    return all(m[0] <= TOL[0] and m[1] <= TOL[1] and abs(m[2]) <= TOL[2] for m in ms)


def crop(rgb, ww, rect):
    x, y, w, h = rect
    return b"".join(rgb[((y + r) * ww + x) * 3:((y + r) * ww + x + w) * 3] for r in range(h))


def first_nonblack(rgb, ww, wh, rect):
    x0, y0, rw, rh = rect
    for y in range(wh):
        for x in range(ww):
            if not (x0 <= x < x0 + rw and y0 <= y < y0 + rh):
                p = rgb[(y * ww + x) * 3:(y * ww + x) * 3 + 3]
                if p != b"\0\0\0":
                    return x, y, tuple(p)
    return None


# ----------------------------------------------------------------------------------------------- ffmpeg expected pictures
def ffmpeg_native(case, frames):
    """Native-size RGB24 of the given frames from ffmpeg's scaler (neighbor chroma, accurate rounding), as in Chapter 9.2."""
    w, h = case["w"], case["h"]
    order = sorted(set(frames))
    sel = "+".join(f"eq(n,{n})" for n in order)
    vf = (f"select='{sel}',scale=in_range={'pc' if case['range'] == 'full' else 'tv'}:out_range=pc:"
          f"in_color_matrix={case['matrix']}:flags={SW_FLAGS}")
    tmp = tempfile.mkdtemp(prefix="ch93_ff_")
    try:
        out = os.path.join(tmp, "o.rgb")
        p = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-i", case["yuv_path"],
                       "-vf", vf, "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", out], timeout=300)
        if p.returncode != 0 or not os.path.exists(out):
            raise Fatal("ffmpeg reference conversion failed: " + p.stderr.decode()[-300:])
        data = open(out, "rb").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    fs = w * h * 3
    if len(data) != fs * len(order):
        raise Fatal("ffmpeg returned an unexpected number of frames")
    return {n: data[i * fs:(i + 1) * fs] for i, n in enumerate(order)}


class Truth:
    """Expected native pictures (cached) and expected presented images of one case."""

    def __init__(self, case, frames):
        self.case, self.native = case, {}
        w, h = case["w"], case["h"]
        if case["expect"] == "ffmpeg":
            self.native = ffmpeg_native(case, frames)
        else:
            data = open(case["yuv_path"], "rb").read()
            fs = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
            for n in set(frames):
                self.native[n] = P.convert_nearest(data, n * fs, w, h, case["matrix"], case["range"] == "full")

    def image(self, frame, window, disp, rect=None, native=None):
        c = self.case
        rect = rect or P.fit_rect(window[0], window[1], *disp)
        return P.render(native or self.native[frame], c["w"], c["h"], window[0], window[1], rect), rect


# ----------------------------------------------------------------------------------------------- check one case
def ffprobe_sar(ts):
    info = ffref.probe_json(ts, "-show_streams", "-select_streams", "v:0")["streams"][0]
    return info.get("sample_aspect_ratio", "N/A"), info.get("display_aspect_ratio", "N/A")


def check_case(case, outroot, rep, log):
    """Returns True when output files were missing or malformed (exit 2)."""
    name = case["name"]
    cdir = os.path.join(outroot, name)
    logp = os.path.join(cdir, "log.txt")
    ddir = os.path.join(cdir, "dump")
    hdr_e, pres_e, end_e, disp, events = expected(case)
    # aspect ratio claims against ffprobe (stream cases)
    if "ts" in case:
        sar_p, dar_p = ffprobe_sar(os.path.join(case["base"], case["ts"]))
        sw, sh = P.sar_from_idc(case["idc"], case["ext"])
        try:
            pw, ph = (int(x) for x in sar_p.split(":"))
            rep.check(pw * sh == ph * sw, f"{name}: Table E-1 / extended SAR of cases.txt gives {sw}:{sh} but ffprobe sample_aspect_ratio is {sar_p}")
            dn, dd = (int(x) for x in dar_p.split(":"))
            rep.check(abs(disp[0] - disp[1] * dn / dd) <= 0.51,
                      f"{name}: display width {disp[0]} x height {disp[1]} does not match ffprobe display_aspect_ratio {dar_p}")
        except ValueError:
            log(f"  note: {name}: ffprobe reports sample_aspect_ratio={sar_p} display_aspect_ratio={dar_p}; aspect check skipped")
    if not os.path.isfile(logp):
        log(f"  ERROR: {name}: log file {logp} is missing")
        return True
    try:
        hdr, pres, end = parse_log(logp)
    except Fatal as e:
        log(f"  ERROR: {name}: {e}")
        return True
    # header and END
    for k in ("src", "frames", "sar", "display", "format", "align", "origin"):
        rep.check(hdr[k] == hdr_e[k], f"{name} HEADER {k}: expected {hdr_e[k]}, got {hdr[k]}"
                  + (" (Table E-1 / display size = width*SAR_w/SAR_h rounded)" if k in ("sar", "display") else ""))
    rep.check(end == (len(pres_e), end_e), f"{name} END: expected presents={len(pres_e)} reason={end_e}, got presents={end[0]} reason={end[1]}")
    # present lines
    lines_e = log_lines(case, hdr_e, pres_e, end_e, disp)[1:-1]
    script_text = {ln: f"after {k} {nm}" + (f" {a[0]}x{a[1]}" if a else "") for ln, k, nm, a in events}
    rep.check(len(pres) == len(pres_e), f"{name}: expected {len(pres_e)} PRESENT lines, got {len(pres)}")
    bad_lines = 0
    for i, (e, ex) in enumerate(zip(lines_e, pres_e)):
        if i >= len(pres):
            break
        g = pres[i]
        want = dict(idx=i, frame=ex[0], reason=ex[1], window=ex[2], rect=P.fit_rect(ex[2][0], ex[2][1], *disp),
                    format=case["format"], stride=P.stride_for(ex[2][0], case["format"], case["alignn"]), origin=case["origin"])
        diffs = [f"{k} expected {want[k]}, got {g[k]}" for k in want if want[k] != g[k]]
        if diffs and want["rect"] != g["rect"]:
            diffs.append(rect_hint(case, ex[2], disp, g["rect"]))
        if diffs:
            bad_lines += 1
            why = f" [script line {ex[3]}: '{script_text[ex[3]]}']" if ex[3] in script_text else " [free play]"
            rep.check(False, f"{name} PRESENT {i}: " + "; ".join(diffs) + why)
        else:
            rep.check(True, "")
    seq_wrong = bad_lines > 0 or len(pres) != len(pres_e) or hdr["display"] != hdr_e["display"]
    # sequence invariants on what the player actually logged (clearer messages for the classic mistakes)
    prev_new = -1
    for g in pres:
        if g["reason"] in ("play", "step"):
            if g["frame"] != prev_new + 1:
                rep.check(False, f"{name} PRESENT {g['idx']}: new frame {g['frame']} after frame {prev_new}: frames must be consecutive "
                          f"({'skipped' if g['frame'] > prev_new + 1 else 'repeated'} frame(s))")
                break
            prev_new = g["frame"]
        elif g["frame"] != prev_new:
            rep.check(False, f"{name} PRESENT {g['idx']}: a redraw must repeat the last shown frame ({prev_new}), got {g['frame']}")
            break
    # images (only meaningful when the logged sequence is right: dump files are matched to the expected present index)
    if seq_wrong:
        log(f"  note: {name}: image checks skipped because the presentation log is wrong (fix the log lines above first)")
        return False
    truth = Truth(case, [p[0] for p in pres_e])
    missing, case_bad = False, False
    for i, (fr, reason, (ww, wh), _ln) in enumerate(pres_e):
        tag = f"{name} present {i} (frame {fr}, {reason}, window {ww}x{wh})"
        base = os.path.join(ddir, "present_%05d" % i)
        try:
            ppm = open(base + ".ppm", "rb").read()
        except OSError:
            log(f"  ERROR: {tag}: file {base}.ppm is missing")
            missing = True
            continue
        try:
            raw = open(base + ".raw", "rb").read()
        except OSError:
            log(f"  ERROR: {tag}: file {base}.raw is missing")
            missing = True
            continue
        try:
            pw, ph, got = parse_ppm(ppm)
        except ValueError as e:
            log(f"  ERROR: {tag}: invalid PPM: {e}")
            missing = True
            continue
        if (pw, ph) != (ww, wh):
            log(f"  ERROR: {tag}: PPM is {pw}x{ph}, the window was {ww}x{wh}: the dump must be the window-sized presented picture")
            missing = True
            continue
        fmt, align = case["format"], case["alignn"]
        stride = P.stride_for(ww, fmt, align)
        bpp = P.FORMATS[fmt][0]
        ok_here = True
        # raw buffer
        if len(raw) != stride * wh:
            hint = " (that is width*bytes_per_pixel*height: rows without padding)" if len(raw) == ww * bpp * wh else ""
            rep.check(False, f"{tag}: .raw is {len(raw)} bytes, expected stride*height = {stride}*{wh} = {stride * wh} "
                      f"(stride = width*{bpp} rounded up to a multiple of {align}){hint}")
            ok_here = False
        else:
            un, alpha = P.unpack(raw, ww, wh, fmt, stride, case["origin"])
            if un != got:
                k = next(j for j in range(len(un)) if un[j] != got[j]) // 3
                msg = f"{tag}: the .raw buffer ({fmt}, stride {stride}, {case['origin']}) decodes to different pixels than the .ppm; first at x={k % ww} y={k // ww}"
                other = "bottomup" if case["origin"] == "topdown" else "topdown"
                if P.unpack(raw, ww, wh, fmt, stride, other)[0] == got:
                    msg += f". DIAGNOSIS: the raw rows are stored {other} (opposite of the {case['origin']} you declared)"
                elif P.unpack(raw, ww, wh, RB_SWAP[fmt], stride, case["origin"])[0] == got:
                    msg += f". DIAGNOSIS: red and blue are swapped: the memory order of '{fmt}' is {P.FORMATS[fmt][1].upper()}"
                rep.check(False, msg)
                ok_here = False
            if alpha is not None and alpha != b"\xff" * len(alpha):
                rep.check(False, f"{tag}: alpha byte of {fmt} must be 255 in every pixel (first bad value {next(a for a in alpha if a != 255)})")
                ok_here = False
        # picture
        exp, rect = truth.image(fr, (ww, wh), disp)
        nb = first_nonblack(got, ww, wh, rect)
        if nb:
            rep.check(False, f"{tag}: letterbox / pillarbox area must be black (0,0,0) outside rect {rect}; first non-black pixel x={nb[0]} y={nb[1]} = {nb[2]}")
            ok_here = False
        ms = metrics(crop(exp, ww, rect), crop(got, ww, rect))
        if not within(ms):
            txt = ", ".join(f"{c}: max {m[0]} mean {m[1]:.2f} bias {m[2]:+.2f}" for c, m in zip("RGB", ms))
            msg = f"{tag}: picture inside rect {rect} outside tolerance (max<={TOL[0]}, mean<={TOL[1]}, |bias|<={TOL[2]}): {txt}"
            if not case_bad:
                msg += ". " + diagnose(case, truth, fr, (ww, wh), disp, got, exp)
            rep.check(False, msg)
            ok_here = False
        if ok_here:
            rep.check(True, "")
        else:
            case_bad = True
    return missing


def rect_hint(case, win, disp, got_rect):
    ww, wh = win
    sw, sh = P.sar_from_idc(case["idc"], case["ext"])
    inv = P.display_size(case["w"], case["h"], (sh, sw))
    r = P.fit_rect(ww, wh, *disp)
    if got_rect == (0, 0, ww, wh):
        return "DIAGNOSIS: the picture is stretched over the whole window (aspect ratio ignored)"
    if got_rect == P.fit_rect(ww, wh, *inv):
        return "DIAGNOSIS: SAR applied inverted (display width = width*SAR_h/SAR_w)"
    if got_rect == (0, 0, r[2], r[3]):
        return "DIAGNOSIS: the picture is top-left aligned, not centred"
    if got_rect == P.fit_rect(ww, wh, case["w"], case["h"]):
        return "DIAGNOSIS: the sample aspect ratio is ignored (rect fitted with the coded width:height)"
    return "DIAGNOSIS: rect is not the centred largest-fit rectangle of the display aspect"


def diagnose(case, truth, fr, win, disp, got, exp):
    ww, wh = win
    cands = []
    rows = [got[y * ww * 3:(y + 1) * ww * 3] for y in range(wh)]
    cands.append(("picture is upside down (rows reversed)", b"".join(rows[::-1])))
    sw = bytearray(got)
    sw[0::3], sw[2::3] = got[2::3], got[0::3]
    cands.append(("red and blue are swapped", bytes(sw)))
    for d in (-1, 1):
        if fr + d in truth.native:
            cands.append((f"this looks like frame {fr + d}", None, truth.image(fr + d, win, disp)[0]))
    cands.append(("aspect ratio ignored: picture stretched over the whole window", None, truth.image(fr, win, disp, (0, 0, ww, wh))[0]))
    sw_, sh_ = P.sar_from_idc(case["idc"], case["ext"])
    inv = P.display_size(case["w"], case["h"], (sh_, sw_))
    if inv != disp:
        cands.append(("SAR applied inverted (width*SAR_h/SAR_w)", None, truth.image(fr, win, inv)[0]))
    x, y, rw, rh = P.fit_rect(ww, wh, *disp)
    if (x, y) != (0, 0):
        cands.append(("picture is not centred (top-left aligned)", None, truth.image(fr, win, disp, (0, 0, rw, rh))[0]))
    for c in cands:
        a, b = c[1], (c[2] if len(c) > 2 else exp)
        if c[1] is None:
            a = got
        else:
            b = exp
        if len(a) == len(b) and within(metrics(b, a)):
            return "DIAGNOSIS: " + c[0]
    return "DIAGNOSIS: no common mistake (flip, R/B swap, neighbouring frame, stretch, inverted SAR, off-centre) reproduces your picture"


# ----------------------------------------------------------------------------------------------- running a reader
def run_cmd(cmd, timeout=TIMEOUT):
    """Run through the shell, stdin closed, whole process group killed on timeout."""
    p = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise Fatal(f"player timed out after {timeout} s: {cmd[:120]}")
    return p.returncode, err.decode(errors="replace")


def run_reader(template, cases, outroot, only=None):
    for c in cases:
        if only and c["name"] not in only:
            continue
        cdir = os.path.join(outroot, c["name"])
        shutil.rmtree(cdir, ignore_errors=True)
        os.makedirs(os.path.join(cdir, "dump"))
        q = shlex.quote
        cmd = template.format(yuv=q(c["yuv_path"]), size=c["size"], width=c["w"], height=c["h"], aspect_idc=c["idc"], sar_ext=c["sar_ext"],
                              matrix=c["matrix"], range=c["range"], window=c["window"], format=c["format"], align=c["align"],
                              origin=c["origin"], script=q(c["script_path"]), log=q(os.path.join(cdir, "log.txt")),
                              dumpdir=q(os.path.join(cdir, "dump")), ts=q(os.path.join(c["base"], c.get("ts", "none"))), case=c["name"])
        rc, err = run_cmd(cmd)
        if rc != 0:
            print(f"  player failed on {c['name']} (exit {rc}): {err[-300:]}")


def finish(rep, missing, quiet=False):
    if not quiet:
        for m in rep.failures[:rep.max_shown + 5]:
            print("  FAIL:", m)
        if len(rep.failures) > rep.max_shown + 5:
            print(f"  ... and {len(rep.failures) - rep.max_shown - 5} more failure(s)")
    ok = not rep.failures and not missing
    if not quiet:
        print(f"{'PASS' if ok else 'FAIL'}: Chapter 9.3 presentation ({rep.checks - len(rep.failures)}/{rep.checks} checks passed)"
              + ("; some output files are missing or malformed (see ERROR lines), exit 2" if missing else ""))
    return 2 if missing else (1 if rep.failures else 0)


def check_all(cases, outroot, only=None, quiet=False, collect=None):
    rep = Report("Chapter 9.3 presentation")
    lines = []
    log = (lambda s: lines.append(s)) if quiet else print
    missing = False
    for c in cases:
        if only and c["name"] not in only:
            continue
        if not os.path.isdir(os.path.join(outroot, c["name"])):
            log(f"  ERROR: {c['name']}: output directory {os.path.join(outroot, c['name'])} is missing")
            missing = True
            continue
        missing |= check_case(c, outroot, rep, log)
    rc = finish(rep, missing, quiet)
    if collect is not None:
        collect.append("\n".join(lines + rep.failures))
    return rc


# ----------------------------------------------------------------------------------------------- variants
def sh(cmd, timeout=300):
    p = ffref.run(cmd, timeout=timeout)
    if p.returncode != 0:
        raise Fatal(f"command failed: {' '.join(cmd[:6])}...: {p.stderr.decode()[-300:]}")


def make_stream(path, size, sar):
    sh(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25", "-t", "2", "-pix_fmt", "yuv420p",
        "-vf", "setsar=" + sar.replace(":", "/"), "-c:v", "libx264", "-qp", "26", "-profile:v", "high",
        "-x264-params", "bframes=3:b-pyramid=normal:keyint=50", "-f", "mpegts", path])


def variants(with_streams=True):
    """-> list of (case dict without paths, script text)."""
    S = []

    def add(name, script, yuv="syn.yuv", size="64x48", idc=1, ext="1:1", window="auto", fmt="bgrx", align=4, origin="topdown",
            matrix="bt601", rng="limited", expect="formula", ts=None):
        d = dict(name=name, yuv=yuv, size=size, aspect_idc=idc, sar_ext=ext, matrix=matrix, range=rng, window=window, format=fmt, align=align,
                 origin=origin, script=name + ".script", expect=expect)
        if ts:
            d["ts"] = ts
        S.append((d, script))
    add("play_all", "# no events: play every frame\n")
    add("pause_step", "after 2 pause\nafter 2 step\nafter 3 step\nafter 4 resume\nafter 6 pause\nafter 6 step\nafter 7 quit\n", window="128x96")
    add("step_from_start", "after 0 pause\nafter 0 step\nafter 1 step\nafter 2 step\nafter 3 resume\nafter 5 step   # ignored: playing\n"
        "after 6 pause\nafter 6 toggle\nafter 7 toggle\nafter 7 step\nafter 8 step   # end of stream: ignored\n", idc=2)
    add("resize_redraw", "after 0 expose     # nothing shown yet: nothing to redraw\nafter 3 pause\nafter 3 resize 201x75\nafter 3 expose\n"
        "after 3 step\nafter 4 resize 64x200\nafter 4 resume\nafter 6 resize 90x90   # while playing: redraw frame 5, then go on\nafter 7 expose\n", window="100x80")
    add("fmt_rgb24_odd", "after 2 pause\nafter 2 step\nafter 3 quit\n", window="99x61", fmt="rgb24")
    add("fmt_bgra_bottomup", "after 4 quit\n", window="83x61", fmt="bgra", align=16, origin="bottomup")
    add("fmt_rgbx_a16", "after 3 quit\n", window="50x50", fmt="rgbx", align=16)
    add("fmt_bgr24_bottomup", "after 3 quit\n", window="77x40", fmt="bgr24", origin="bottomup")
    add("sar_16_11", "after 3 quit\n", idc=4, window="200x100")
    add("sar_ext_7_5", "after 3 quit\n", idc=255, ext="7:5")
    add("sar_narrow_10_11", "after 3 quit\n", idc=3, window="120x120")
    for idc in list(range(0, 17)) + [17, 255]:
        add(f"sar_idc_{idc}", "", yuv="tiny.yuv", size="16x16", idc=idc, ext="11:7" if idc == 255 else "1:1")
    add("sar_idc_255_zero", "", yuv="tiny.yuv", size="16x16", idc=255, ext="0:0")
    if with_streams:
        add("st_sar_16_11", "after 1 pause\nafter 1 step\nafter 2 step\nafter 3 resume\nafter 6 quit\n", yuv="st1.yuv", size="176x144", idc=4,
            ext="1:1", expect="ffmpeg", ts="st1.ts")
        add("st_sar_ext_7_5", "after 2 pause\nafter 2 resize 300x200\nafter 2 step\nafter 3 quit\n", yuv="st2.yuv", size="176x144", idc=255,
            ext="7:5", window="auto", expect="ffmpeg", ts="st2.ts")
    return S


def make_variants(d, with_streams=True):
    os.makedirs(d, exist_ok=True)
    ffref.require_tools() if with_streams else None
    open(os.path.join(d, "syn.yuv"), "wb").write(synth.build(64, 48, 8))
    open(os.path.join(d, "tiny.yuv"), "wb").write(synth.build(16, 16, 3))
    if with_streams:
        for nm, sar in (("st1", "16:11"), ("st2", "7:5")):
            make_stream(os.path.join(d, nm + ".ts"), "176x144", sar)
            sh(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(d, nm + ".ts"), "-map", "0:v:0", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
                "-f", "rawvideo", os.path.join(d, nm + ".yuv")])
    lines = ["# Chapter 9.3 cases.  Frames = 0-based output-order indices into the YUV file.",
             "# expect=formula: Chapter 9.2 formula (nearest chroma); expect=ffmpeg: ffmpeg swscale (neighbor, accurate_rnd, full_chroma_int)."]
    for dct, script in variants(with_streams):
        open(os.path.join(d, dct["script"]), "w").write(script)
        lines.append(case_line(dct))
    open(os.path.join(d, "cases.txt"), "w").write("\n".join(lines) + "\n")
    for c in parse_cases(os.path.join(d, "cases.txt")):
        hdr, pres, end, disp, _ = expected(c)
        open(os.path.join(d, c["name"] + ".expected.log"), "w").write("\n".join(log_lines(c, hdr, pres, end, disp)) + "\n")
    print(f"wrote {d}/cases.txt ({len(lines) - 2} cases), scripts, YUV files and <case>.expected.log")


# ----------------------------------------------------------------------------------------------- self-test
MUTANTS = [  # (description, bug name, target cases, expected exit code, text that must appear in the report)
    ("step skips a frame", "step_skips", ["pause_step"], 1, "consecutive"),
    ("step also works while playing", "step_when_playing", ["step_from_start"], 1, "reason"),
    ("pause does not freeze playback", "pause_ignored", ["pause_step"], 1, "PRESENT"),
    ("log frame numbers 1-based (off by one)", "frames_1based", ["play_all"], 1, "frame expected"),
    ("log says frame n but image is frame n-1", "stale_frame", ["pause_step"], 1, "looks like frame"),
    ("stride not rounded up (3-byte pixels, odd width)", "stride_unaligned", ["fmt_rgb24_odd"], 1, "stride"),
    ("log stride unpadded but buffer padded", "stride_log_only", ["fmt_rgb24_odd"], 1, "stride"),
    ("buffer rows in the opposite order to the declared origin", "flip", ["fmt_bgra_bottomup", "fmt_bgr24_bottomup"], 1, "opposite"),
    ("SAR applied inverted", "inverted_sar", ["sar_16_11"], 1, "inverted"),
    ("channel order BGR written for RGB format", "bgr_swap", ["play_all", "fmt_rgb24_odd"], 1, "swapped"),
    ("aspect ratio ignored (stretch to window)", "no_aspect", ["sar_narrow_10_11"], 1, "stretched"),
    ("resize presents the next frame instead of redrawing", "redraw_advances", ["resize_redraw"], 1, "redraw"),
    ("resize does not redraw", "no_redraw", ["resize_redraw"], 1, "PRESENT lines"),
    ("redraw uses the old window size", "redraw_old_size", ["resize_redraw"], 1, "window"),
    ("quit ignored", "quit_ignored", ["pause_step"], 1, "END"),
    ("first frame presented twice", "dup_first", ["play_all"], 1, "repeated"),
    ("step at end of stream repeats last frame", "step_end_dup", ["step_from_start"], 1, "PRESENT"),
    ("letterbox bars not black", "bars_grey", ["sar_16_11"], 1, "black"),
    ("alpha byte 0 in bgra", "alpha_zero", ["fmt_bgra_bottomup"], 1, "alpha"),
    ("scaling without half-pixel centre (nearest mapping off)", "nn_shift", ["sar_16_11"], 1, "tolerance"),
    ("picture top-left aligned instead of centred", "left_aligned", ["sar_16_11"], 1, "rect"),
    ("extended SAR (idc 255) ignored", "ignore_ext_sar", ["sar_ext_7_5", "sar_idc_255"], 1, "sar"),
    ("Table E-1 entry wrong (idc 14)", "table_error", ["sar_idc_14"], 1, "sar"),
    ("log window reports the source size", "log_frame_size", ["pause_step"], 1, "window"),
    ("a .ppm file is not written", "missing_ppm", ["pause_step"], 2, "missing"),
    ("malformed log line", "bad_log_line", ["pause_step"], 2, "PRESENT"),
]


def self_test():
    ffref.require_tools()
    tmp = tempfile.mkdtemp(prefix="ch93_self_")
    fails = []
    try:
        d = os.path.join(tmp, "cases")
        make_variants(d)
        cases = parse_cases(os.path.join(d, "cases.txt"))
        names = {c["name"] for c in cases}
        # script semantics sanity (independent of any player): hand-written expectations
        pres, end = P.simulate(8, P.parse_script("after 2 pause\nafter 2 step\nafter 3 step\nafter 4 resume\nafter 6 quit\n"), (64, 48))
        got = [(f, r) for f, r, _, _ in pres]
        want = [(0, "play"), (1, "play"), (2, "step"), (3, "step"), (4, "play"), (5, "play")]
        if got != want or end != "quit":
            fails.append(f"presenter semantics: {got} {end}")
        for idc, sar in ((1, (1, 1)), (4, (16, 11)), (14, (4, 3)), (16, (2, 1)), (255, (7, 5)), (0, (1, 1)), (17, (1, 1))):
            if P.sar_from_idc(idc, (7, 5)) != sar:
                fails.append(f"sar_from_idc({idc})")
        if P.display_size(352, 288, (16, 11)) != (512, 288) or P.stride_for(99, "rgb24", 4) != 300 or P.fit_rect(99, 61, 64, 48) != (9, 0, 81, 61):
            fails.append("worked examples of the Readme")
        # good player through the real command-line contract
        out = os.path.join(tmp, "good")
        tpl = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.join(HERE, 'spoiler_reference', 'player.py'))} {{yuv}} --size {{size}} " \
              "--aspect-idc {aspect_idc} --sar-ext {sar_ext} --matrix {matrix} --range {range} --window {window} --format {format} " \
              "--align {align} --origin {origin} --script {script} --log {log} --dump-presented {dumpdir}"
        run_reader(tpl, cases, out)
        rc = check_all(cases, out)
        if rc != 0:
            fails.append("good player did not pass")
        # calibration: formula vs ffmpeg on a stream case
        c = next(c for c in cases if c["name"] == "st_sar_16_11")
        fr = ffmpeg_native(c, [0, 5])
        data = open(c["yuv_path"], "rb").read()
        fs = c["w"] * c["h"] * 3 // 2
        cal = max(max(m[0] for m in metrics(fr[n], P.convert_nearest(data, n * fs, c["w"], c["h"], c["matrix"], False))) for n in (0, 5))
        print(f"calibration: Chapter 9.2 formula vs ffmpeg swscale on the stream case: max abs error {cal} (limit 2)")
        if cal > 2:
            fails.append("formula disagrees with swscale")
        # wrong behaviours: the player module is run in-process with a bug switch, output checked by the same checker
        results = 0
        for desc, bug, targets, want_rc, want_text in MUTANTS:
            targets = [t for t in targets if t in names]
            outb = os.path.join(tmp, "bad_" + bug)
            for t in targets:
                c = next(c for c in cases if c["name"] == t)
                cd = os.path.join(outb, t)
                os.makedirs(os.path.join(cd, "dump"))
                player.main([c["yuv_path"], "--size", c["size"], "--aspect-idc", str(c["idc"]), "--sar-ext", c["sar_ext"], "--matrix", c["matrix"],
                             "--range", c["range"], "--window", c["window"], "--format", c["format"], "--align", c["align"], "--origin", c["origin"],
                             "--script", c["script_path"], "--log", os.path.join(cd, "log.txt"), "--dump-presented", os.path.join(cd, "dump"),
                             "--bug", bug])
            col = []
            rc = check_all(cases, outb, only=targets, quiet=True, collect=col)
            text = col[0]
            caught = rc == want_rc
            diag_ok = want_text.lower() in text.lower()
            print(f"  [{'caught' if caught else 'NOT CAUGHT (or wrong exit code)'}] {desc} -> exit {rc}"
                  + ("" if diag_ok else f"   (message lacks {want_text!r})"))
            if not caught or not diag_ok:
                fails.append(desc)
                print("     " + text[:500].replace("\n", "\n     "))
            else:
                results += 1
        # the good player must also pass when only the optional parts are absent? (not needed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(("SELF-TEST FAIL: " + "; ".join(fails)) if fails else f"SELF-TEST PASS: good player accepted, {results} wrong behaviours caught")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", help="directory with cases.txt (from --make-variants)")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--out", help="directory holding <case>/log.txt and <case>/dump/")
    ap.add_argument("--case", action="append", help="check only this case (repeatable)")
    ap.add_argument("--reader", help="player command template run once per case (placeholders in Readme 6)")
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
        return check_all(cases, a.out, a.case)
    except Fatal as e:
        print("error:", e)
        return 2
    except subprocess.TimeoutExpired:
        print("error: a subprocess timed out")
        return 2


if __name__ == "__main__":
    sys.exit(main())
