#!/usr/bin/env python3
"""Chapter 10.7 harness: how fast is your decoder, and is its output still byte-identical?

    python3 test_performance.py --decoder "./mydec {in} {out}" [--clip hd|hd_slices|sd|tiny|all] [options]
    python3 test_performance.py --self-test

For every selected clip (generated once with x264 into .cache/perf/) the harness runs your decoder `--warmup` + `--runs`
times as a command, each run in its own process group under a hard timeout, and measures wall time, CPU time and peak
memory.  EVERY run's output is compared (MD5) with `ffmpeg -threads 1`; a mismatch is located (frame, plane, macroblock,
sample).  The speed is the BEST wall time of the timed runs.  Exit status: 0 correct and at or above `--target-fps`
(and within `--max-rss-mb`); 1 wrong output, crash, hang, too slow or too much memory; 2 unusable input.
"""
import argparse
import contextlib
import hashlib
import io
import json
import math
import mmap
import os
import resource
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
from common.report import Report  # noqa: E402

MIB = 1 << 20
CACHE = os.path.join(ROOT, ".cache", "perf")
SELF = os.path.abspath(__file__)

# name: (width, height, frames, x264 bit rate, extra x264 params, description)
CLIPS = {
    "tiny": (320, 180, 30, "1M", "", "320x180 development clip"),
    "sd": (640, 360, 60, "3M", "", "640x360 development clip"),
    "hd": (1920, 1080, 90, "12M", "", "1080p30 High, CABAC, 2 B-frames, 3 refs, 8x8 transform, weighted P, 1 slice/picture"),
    "hd_slices": (1920, 1080, 90, "12M", ":slices=4", "1080p30 High, CABAC, 2 B-frames, 3 refs, 8x8 transform, 4 slices/picture"),
}
TIERS = [  # (minimum fps as a multiple of the target, name)
    (4.0, "fast: 4x real time or more"),
    (2.0, "real time with headroom: 2x"),
    (1.0, "REAL TIME: at or above the target"),
    (0.5, "almost: half real time or better"),
    (0.25, "slow: a quarter of real time or better"),
    (0.0, "correct but slow"),
]


class Bad(Exception):
    """Unusable input: exit status 2."""


def tier_of(fps, target):
    r = fps / target if target > 0 else 99.0
    for k, (lo, name) in enumerate(TIERS):
        if r >= lo:
            return len(TIERS) - 1 - k, name
    return 0, TIERS[-1][1]


def frame_bytes(w, h):
    return w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)


# ------------------------------------------------------------------------------------------------ tools and clips
def run_tool(argv, timeout=300):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise Bad(f"{argv[0]} not found on PATH (install ffmpeg with libx264)")
    except subprocess.TimeoutExpired:
        raise Bad(f"{argv[0]} did not finish within {timeout} s")


def gen_clip(name, frames=None):
    w, h, nf, br, extra, _ = CLIPS[name]
    nf = frames or nf
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{name}_{nf}f.264")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    tmp = path + ".tmp"
    xp = f"cabac=1:8x8dct=1:ref=3:bframes=2:keyint=30:min-keyint=30:scenecut=0:threads=4:weightp=2{extra}"
    r = run_tool(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                  f"testsrc2=size={w}x{h}:rate=30,noise=alls=8:allf=t:all_seed=7,format=yuv420p", "-frames:v", str(nf),
                  "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-b:v", br, "-x264-params", xp,
                  "-f", "h264", tmp])
    if r.returncode != 0 or not os.path.exists(tmp):
        raise Bad("cannot generate clip " + name + ": " + r.stderr.decode(errors="replace")[-300:])
    os.replace(tmp, path)
    return path


def probe_geometry(path):
    r = run_tool(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                  "-of", "csv=p=0", path], 60)
    try:
        w, h = r.stdout.decode().strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        raise Bad(f"{path}: ffprobe finds no decodable H.264 video stream")


def file_md5(path):
    m = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(4 * MIB)
            if not b:
                return m.hexdigest()
            m.update(b)


def ffmpeg_decode(inp, out):
    r = run_tool(["ffmpeg", "-v", "error", "-y", "-threads", "1", "-i", inp, "-map", "0:v:0", "-fps_mode", "passthrough",
                  "-f", "rawvideo", "-pix_fmt", "yuv420p", out])
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def truth_for(path, workdir):
    """-> dict(w, h, size, md5, frames); cached in .cache/perf/truth/ keyed by path, size and mtime."""
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]
    cp = os.path.join(CACHE, "truth", key + ".json")
    try:
        with open(cp) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    w, h = probe_geometry(path)
    tmp = os.path.join(workdir, "truth.yuv")
    if not ffmpeg_decode(path, tmp):
        raise Bad(f"{path}: ffmpeg cannot decode this stream (no truth to compare with)")
    size = os.path.getsize(tmp)
    fb = frame_bytes(w, h)
    if size % fb:
        raise Bad(f"{path}: ffmpeg output is not a whole number of {w}x{h} frames")
    t = dict(w=w, h=h, size=size, md5=file_md5(tmp), frames=size // fb)
    os.remove(tmp)
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    with open(cp, "w") as f:
        json.dump(t, f)
    return t


# ------------------------------------------------------------------------------------------------ running a decoder
def parse_template(t):
    if not t:
        raise Bad("--decoder is required (e.g. --decoder \"./mydec {in} {out}\")")
    try:
        args = shlex.split(t)
    except ValueError as e:
        raise Bad(f"--decoder: {e}")
    if not any("{in}" in a for a in args) or not any("{out}" in a for a in args):
        raise Bad("--decoder must contain both {in} and {out}")
    return args


def run_once(args, inp, out, timeout, max_out, workdir):
    """-> dict(status, rc, wall, cpu, rss_mb, out_size, log).
    status: ok | error_exit | timeout | crash | explosion.  The decoder runs in its own process group, which is killed
    at the timeout and again after it exits, so nothing is left behind."""
    argv = [a.replace("{in}", inp).replace("{out}", out) for a in args]
    if os.path.exists(out):
        os.remove(out)

    def pre():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_out, max_out))

    logp = os.path.join(workdir, "decoder.log")
    timed_out = []
    with open(logp, "wb") as lf:
        t0 = time.perf_counter()
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, preexec_fn=pre,
                                 start_new_session=True)
        except OSError as e:
            raise Bad(f"cannot start the decoder {argv[0]!r}: {e}")

        def kill():
            timed_out.append(1)
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGKILL)

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            _, wstatus, ru = os.wait4(p.pid, 0)             # per-child resource usage, including peak memory
        finally:
            timer.cancel()
            wall = time.perf_counter() - t0
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGKILL)            # stragglers
        rc = os.waitstatus_to_exitcode(wstatus)
        p.returncode = rc
    size = os.path.getsize(out) if os.path.exists(out) else -1
    if rc == -signal.SIGXFSZ or size >= max_out:          # checked first: a writer stuck at the size limit may also time out
        status = "explosion"
    elif timed_out:
        status = "timeout"
    elif rc < 0 or rc >= 128:
        status = "crash"
    elif rc != 0:
        status = "error_exit"
    else:
        status = "ok"
    rss = ru.ru_maxrss / (MIB if sys.platform == "darwin" else 1024)
    with open(logp, "rb") as f:
        log = f.read()[-300:].decode(errors="replace").strip()
    return dict(status=status, rc=rc, wall=wall, cpu=ru.ru_utime + ru.ru_stime, rss_mb=rss, out_size=size, log=log)


def describe_status(r, timeout):
    s, rc = r["status"], r["rc"]
    if s == "timeout":
        return f"HANG: no exit after {timeout} s (process group killed)"
    if s == "explosion":
        return f"OUTPUT EXPLOSION: the output file reached {r['out_size']} bytes (more than 4x the expected size)"
    if s == "crash":
        return f"CRASH: killed by signal {-rc}" if rc < 0 else f"CRASH: exit status {rc} (>= 128 means a signal)"
    if s == "error_exit":
        return f"non-zero exit status {rc}"
    return "ok"


# ------------------------------------------------------------------------------------------------ comparing output
def locate(w, h, off):
    fb = frame_bytes(w, h)
    k, r = divmod(off, fb)
    cw, ch = (w + 1) // 2, (h + 1) // 2
    if r < w * h:
        plane, pw, x, y = "Y", w, r % w, r // w
        mb = (x // 16, y // 16)
    else:
        r -= w * h
        plane = "Cb" if r < cw * ch else "Cr"
        r %= cw * ch
        pw, x, y = cw, r % cw, r // cw
        mb = (x // 8, y // 8)
    return k, plane, x, y, mb


def first_diff(a, b, n):
    """First differing byte offset in the first n bytes of two buffers, or -1."""
    step = MIB
    for o in range(0, n, step):
        e = min(n, o + step)
        if a[o:e] != b[o:e]:
            for i in range(o, e):
                if a[i] != b[i]:
                    return i
    return -1


def diagnose(truth_yuv, out_path, w, h):
    """Explain how out_path differs from truth_yuv: list of text lines (first one is the headline)."""
    fb = frame_bytes(w, h)
    ts, os_ = os.path.getsize(truth_yuv), os.path.getsize(out_path)
    lines = []
    if os_ == 0:
        return ["output file is empty"]
    with open(truth_yuv, "rb") as ft, open(out_path, "rb") as fo:
        with mmap.mmap(ft.fileno(), 0, access=mmap.ACCESS_READ) as t, mmap.mmap(fo.fileno(), 0, access=mmap.ACCESS_READ) as o:
            n = min(ts, os_)
            d = first_diff(t, o, n)
            nexp, ngot = ts / fb, os_ / fb
            if d < 0:
                if os_ < ts:
                    lines.append(f"output is a correct prefix but stops early: {ngot:.2f} frame(s) written, {nexp:.0f} expected")
                else:
                    lines.append(f"all {nexp:.0f} frames are right, then {os_ - ts} extra byte(s) follow")
                return lines
            k, plane, x, y, mb = locate(w, h, d)
            lines.append(f"first difference: frame {k} (0-based, output order) plane {plane} MB({mb[0]},{mb[1]}) x={x} y={y} "
                         f"expected {t[d]} got {o[d]}")
            if os_ % fb:
                lines.append(f"file size {os_} is not a whole number of {w}x{h} frames ({ngot:.3f} frames)")
            elif os_ != ts:
                lines.append(f"{ngot:.0f} frame(s) written, {nexp:.0f} expected")
            nf = min(ts, os_) // fb
            bad = [i for i in range(nf) if t[i * fb:(i + 1) * fb] != o[i * fb:(i + 1) * fb]]
            lines.append(f"{len(bad)} of {nf} compared frames differ")
            if k < nf:
                te, to = t[k * fb:(k + 1) * fb], o[k * fb:(k + 1) * fb]
                diffs = [i for i in range(0, len(te)) if te[i] != to[i]] if len(te) < 4 * MIB else []
                ysz, csz = w * h, ((w + 1) // 2) * ((h + 1) // 2)
                if diffs:
                    planes = sorted({"Y" if i < ysz else ("Cb" if i < ysz + csz else "Cr") for i in diffs})
                    lines.append(f"in frame {k}: {len(diffs)} sample(s) differ, planes {','.join(planes)}")
                    if planes == ["Y"]:
                        lines.append("only luma differs (chroma is right)")
                    if "Y" not in planes:
                        lines.append("luma is right; only chroma differs")
                if te[ysz:ysz + csz] == to[ysz + csz:ysz + 2 * csz] and te[ysz + csz:] == to[ysz:ysz + csz]:
                    lines.append("hint: Cb and Cr are swapped")
                for j in range(max(0, k - 3), min(nexp.__trunc__(), k + 4)):
                    if j != k and t[j * fb:(j + 1) * fb] == to:
                        lines.append(f"hint: output frame {k} equals expected frame {j} (dropped, duplicated or reordered frame)")
                        break
            h16 = (h + 15) // 16 * 16
            if h16 != h and os_ == int(nexp) * frame_bytes(w, h16):
                lines.append("hint: the size matches the UNCROPPED (16-aligned) picture; write the SPS cropping window only")
    return lines


# ------------------------------------------------------------------------------------------------ measuring a clip
def fmt_run(tag, r, frames, verdict):
    fps = frames / r["wall"] if r["wall"] > 0 else 0
    ratio = r["cpu"] / r["wall"] if r["wall"] > 0 else 0
    return f"  {tag:7s} {r['wall']:8.3f} {fps:8.1f} {r['cpu']:8.2f} {ratio:8.2f} {r['rss_mb']:8.0f}   {verdict}"


def measure(clip_name, path, args, opts, workdir):
    """Run warm-ups and timed runs of one clip. -> result dict."""
    tr = truth_for(path, workdir)
    w, h, frames, fb = tr["w"], tr["h"], tr["frames"], frame_bytes(tr["w"], tr["h"])
    max_out = 4 * tr["size"] + 16 * MIB
    out = os.path.join(workdir, "out.yuv")
    res = dict(clip=clip_name, w=w, h=h, frames=frames, correct=False, runs=[], fps=0.0, best=None, tier=0,
               tier_name=TIERS[-1][1], rss_mb=0.0, problems=[], ref_fps=None)
    print(f"clip {clip_name}: {w}x{h}, {frames} frames, {os.path.getsize(path) / 1e6:.1f} MB"
          + (f", {CLIPS[clip_name][5]}" if clip_name in CLIPS else ""))
    print(f"  {'run':7s} {'wall s':>8s} {'fps':>8s} {'cpu s':>8s} {'cpu/wall':>8s} {'peak MB':>8s}   output")
    schedule = [("warm-up", True)] * opts.warmup + [(str(i + 1), False) for i in range(opts.runs)]
    walls = []
    for tag, warm in schedule:
        r = run_once(args, path, out, opts.timeout, max_out, workdir)
        res["rss_mb"] = max(res["rss_mb"], r["rss_mb"])
        bad = None
        diag = []
        if r["status"] != "ok":
            bad = describe_status(r, opts.timeout)
            if r["status"] == "error_exit" and r["out_size"] == tr["size"] and file_md5(out) == tr["md5"]:
                bad += " (the output itself is correct)"
        elif r["out_size"] < 0:
            bad = "NO OUTPUT FILE: the decoder exited with status 0 but did not create {out}"
        elif r["out_size"] == tr["size"] and file_md5(out) == tr["md5"]:
            pass
        else:
            bad = "WRONG OUTPUT: not byte-identical to ffmpeg -threads 1 (md5 differs)"
            ref = os.path.join(workdir, "truth.yuv")
            if ffmpeg_decode(path, ref):
                diag = diagnose(ref, out, w, h)
                os.remove(ref)
        print(fmt_run(tag, r, frames, "identical" if bad is None else "FAIL"))
        if bad:
            res["problems"].append(f"{clip_name} run {tag}: {bad}")
            for d in diag:
                res["problems"].append("    " + d)
            if r["log"]:
                res["problems"].append("    decoder said: " + r["log"].replace("\n", " | ")[-300:])
            break
        if not warm:
            walls.append(r["wall"])
            res["runs"].append(r["wall"])
    if os.path.exists(out):
        os.remove(out)
    if res["problems"]:
        return res
    res["correct"] = True
    best = min(walls)
    res["best"], res["fps"] = best, frames / best
    med = statistics.median(walls)
    spread = (max(walls) - min(walls)) / best * 100
    res["tier"], res["tier_name"] = tier_of(res["fps"], opts.target_fps)
    print(f"  best of {len(walls)}: {best:.3f} s = {res['fps']:.1f} fps ({res['fps'] / (opts.target_fps or 30):.2f}x of {opts.target_fps or 30:g} fps); median {med:.3f} s; "
          f"spread {spread:.0f}%")
    if len(walls) > 1 and spread > 25:
        print("  note: the timed runs differ by more than 25%: the machine is noisy; close other programs or raise --runs")
    if opts.ref_timing:
        ra = ["ffmpeg", "-v", "error", "-y", "-threads", "1", "-i", "{in}", "-fps_mode", "passthrough", "-f", "rawvideo",
              "-pix_fmt", "yuv420p", "{out}"]
        rw = []
        for _ in range(max(1, min(opts.runs, 3))):
            rr = run_once(ra, path, out, 300, max_out, workdir)
            if rr["status"] == "ok":
                rw.append(rr["wall"])
        if os.path.exists(out):
            os.remove(out)
        if rw:
            res["ref_fps"] = frames / min(rw)
            print(f"  reference: ffmpeg -threads 1 needs {min(rw):.3f} s = {res['ref_fps']:.1f} fps; your decoder runs at "
                  f"{res['fps'] / res['ref_fps']:.2f}x of that")
    return res


# ------------------------------------------------------------------------------------------------ main
def build_parser():
    ap = argparse.ArgumentParser(description="Decoder speed and byte-identical-output harness (Chapter 10.7).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--decoder", help='command template with {in} (Annex B file) and {out} (yuv file), e.g. "./mydec {in} {out}"')
    ap.add_argument("--clip", default="hd", help="hd (default), hd_slices, sd, tiny, a comma list, or all")
    ap.add_argument("--stream", help="measure on this Annex B/.ts/.mp4 file instead of the generated clips (truth: ffmpeg)")
    ap.add_argument("--frames", type=int, help="frames in the generated clips (default per clip: tiny 30, sd 60, hd 90)")
    ap.add_argument("--runs", type=int, default=3, help="timed runs; the best counts (default 3)")
    ap.add_argument("--warmup", type=int, default=1, help="untimed runs first (still verified; default 1)")
    ap.add_argument("--target-fps", type=float, default=30.0, help="required frame rate; 0 = only report (default 30)")
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per run before the process group is killed")
    ap.add_argument("--max-rss-mb", type=float, default=2048.0, help="fail above this peak memory (default 2048)")
    ap.add_argument("--workdir", help="where output files go (default .cache/perf/work; /dev/shm removes disk effects)")
    ap.add_argument("--json", help="also write the results to this JSON file")
    ap.add_argument("--no-ref", dest="ref_timing", action="store_false", help="skip the ffmpeg reference timing")
    ap.add_argument("--self-test", action="store_true", help="test the harness with stand-in decoders")
    return ap


def check_opts(o):
    for name in ("target_fps", "timeout", "max_rss_mb"):
        if not math.isfinite(getattr(o, name)):
            raise Bad(f"--{name.replace('_', '-')} must be a finite number")
    if o.runs < 1:
        raise Bad("--runs must be at least 1")
    if o.warmup < 0:
        raise Bad("--warmup must be 0 or more")
    if o.target_fps < 0:
        raise Bad("--target-fps must be 0 or more")
    if o.timeout <= 0:
        raise Bad("--timeout must be positive")
    if o.max_rss_mb <= 0:
        raise Bad("--max-rss-mb must be positive")
    if o.frames is not None and o.frames < 2:
        raise Bad("--frames must be at least 2")


def to_annexb(path, tmp):
    """Containers are remuxed to Annex B once, so your decoder only ever sees a byte stream."""
    if os.path.splitext(path)[1].lower() in (".ts", ".mp4", ".mkv", ".mov", ".m4v"):
        out = os.path.join(tmp, "stream.264")
        r = run_tool(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0:v:0", "-c:v", "copy", "-f", "h264", out])
        if r.returncode != 0:
            raise Bad(f"{path}: ffmpeg cannot extract an H.264 stream")
        return out
    return path


def main(argv):
    o = build_parser().parse_args(argv)
    if o.self_test:
        return self_test()
    check_opts(o)
    args = parse_template(o.decoder)
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t) is None:
            raise Bad(f"{t} not found on PATH (install ffmpeg with libx264)")
    os.makedirs(CACHE, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="run-", dir=o.workdir if o.workdir else _mk(os.path.join(CACHE, "work")))
    try:
        jobs = []
        if o.stream:
            if not os.path.isfile(o.stream) or os.path.getsize(o.stream) == 0:
                raise Bad(f"--stream {o.stream}: missing or empty file")
            jobs.append((os.path.basename(o.stream), to_annexb(os.path.abspath(o.stream), workdir)))
        else:
            names = list(CLIPS) if o.clip == "all" else [c.strip() for c in o.clip.split(",") if c.strip()]
            for c in names:
                if c not in CLIPS:
                    raise Bad(f"unknown clip {c!r}; choose from {', '.join(CLIPS)}, all")
            if not names:
                raise Bad("--clip is empty")
            for c in names:
                jobs.append((c, gen_clip(c, o.frames)))
        ncpu = os.cpu_count() or 1
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0
        print(f"performance: decoder {o.decoder!r}; target {o.target_fps:g} fps; {o.warmup} warm-up + {o.runs} timed run(s); "
              f"{ncpu} cpus, load {load:.1f}")
        if load > ncpu * 0.5:
            print("  warning: the machine is busy; timings will be pessimistic and noisy")
        results = [measure(n, p, args, o, workdir) for n, p in jobs]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print()
    print(f"{'clip':10s} {'frames':>6s} {'best fps':>9s} {'x target':>8s} {'peak MB':>8s}  correct  tier")
    rep = Report("performance")
    for r in results:
        if r["correct"]:
            print(f"{r['clip']:10s} {r['frames']:6d} {r['fps']:9.1f} {r['fps'] / (o.target_fps or 30):8.2f} {r['rss_mb']:8.0f}  yes      "
                  f"{r['tier']} {r['tier_name']}")
        else:
            print(f"{r['clip']:10s} {r['frames']:6d} {'-':>9s} {'-':>8s} {r['rss_mb']:8.0f}  NO")
        rep.check(r["correct"], f"clip {r['clip']}: output or process is wrong (details below)")
        if r["correct"]:
            rep.check(o.target_fps <= 0 or r["fps"] >= o.target_fps,
                      f"clip {r['clip']}: {r['fps']:.1f} fps is below the target {o.target_fps:g} fps "
                      f"(tier {r['tier']}: {r['tier_name']})")
            rep.check(r["rss_mb"] <= o.max_rss_mb, f"clip {r['clip']}: peak memory {r['rss_mb']:.0f} MB exceeds --max-rss-mb "
                      f"{o.max_rss_mb:g} (MEMORY)")
    if o.json:
        with open(o.json, "w") as f:
            json.dump(dict(target_fps=o.target_fps, results=results), f, indent=1)
    for r in results:
        for p in r["problems"]:
            print("  " + p)
    rep.max_shown = 100
    rep.finish()


def _mk(p):
    os.makedirs(p, exist_ok=True)
    return p


# ------------------------------------------------------------------------------------------------ self-test
def self_test():
    ref = os.path.join(HERE, "ref_decoder.py")
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t) is None:
            raise Bad(f"{t} not found")
    tmp = tempfile.mkdtemp(prefix="selftest-", dir=_mk(os.path.join(CACHE, "work")))   # on disk: /tmp may be a small RAM disk
    rep = Report("self-test")

    def tmpl(*extra):
        return " ".join([shlex.quote(sys.executable), shlex.quote(ref)] + [shlex.quote(str(e)) for e in extra]
                        + ["{in}", "{out}"])

    def harness(*a, timeout=400):
        p = subprocess.run([sys.executable, SELF, *a], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace")

    base = ["--clip", "tiny", "--runs", "1", "--warmup", "0", "--no-ref", "--target-fps", "1", "--timeout", "30",
            "--workdir", tmp]
    try:
        print("self-test: clips are generated into .cache/perf/ on first use")
        # 1. the correct stand-in passes, on every kind of clip (and the reference timing path works)
        for clip, extra in (("tiny", ["--runs", "2", "--warmup", "1"]), ("sd", ["--no-ref"]), ("hd", ["--frames", "30"]),
                            ("hd_slices", ["--frames", "30"])):
            a = [x for x in base]
            a[1] = clip
            rc, out = harness("--decoder", tmpl(), *a, *extra, "--json", os.path.join(tmp, "r.json"))
            rep.check(rc == 0 and "PASS" in out, f"correct stand-in on {clip}: expected exit 0, got {rc}\n{out[-600:]}")
        with open(os.path.join(tmp, "r.json")) as f:
            js = json.load(f)
        rep.check(js["results"][0]["correct"] and js["results"][0]["fps"] > 0, "json report has no fps")
        rc, out = harness("--decoder", tmpl(), "--clip", "tiny", "--runs", "1", "--warmup", "0", "--target-fps", "1",
                          "--workdir", tmp)
        rep.check(rc == 0 and "reference: ffmpeg -threads 1" in out, f"reference timing line missing (rc {rc})")
        # a container input and a custom stream
        ts = os.path.join(tmp, "c.ts")
        run_tool(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-frames:v", "20",
                  "-c:v", "libx264", "-profile:v", "high", "-bf", "2", "-f", "mpegts", ts])
        rc, out = harness("--decoder", tmpl(), "--stream", ts, *base[2:])
        rep.check(rc == 0, f"--stream with a .ts container: exit {rc}\n{out[-400:]}")

        # 2. artificial slowdowns: ordering of fps and of tiers, with generous margins (pace is a floor on the time)
        fps = {}
        for pace in (4, 12, 60):
            rc, out = harness("--decoder", tmpl("--pace-fps", pace), *base[:2], "--runs", "1", "--warmup", "0", "--no-ref",
                              "--target-fps", "15", "--timeout", "60", "--workdir", tmp, "--json", os.path.join(tmp, "p.json"))
            with open(os.path.join(tmp, "p.json")) as f:
                r = json.load(f)["results"][0]
            fps[pace] = (r["fps"], r["tier"], rc)
        rep.check(fps[4][0] < fps[12][0] < fps[60][0], f"fps not ordered by artificial slowdown: {fps}")
        rep.check(fps[4][2] == 1 and fps[12][2] == 1, f"slow stand-ins must exit 1 (below target 15 fps): {fps}")
        rep.check(fps[60][2] == 0, f"fast stand-in (>= 4x the target margin) must exit 0: {fps}")
        rep.check(fps[4][1] <= 1 and fps[12][1] <= 2 and fps[60][1] >= 3, f"tiers do not follow the speed: {fps}")
        rep.check(fps[4][0] < 4.5 and fps[12][0] < 12.5, f"measured fps above the artificial ceiling: {fps}")
        rc, out = harness("--decoder", tmpl("--pace-fps", 4), *base[:2], "--runs", "1", "--warmup", "0", "--no-ref",
                          "--target-fps", "0", "--workdir", tmp)
        rep.check(rc == 0, f"--target-fps 0 reports only: slow decoder must exit 0, got {rc}")

        # 3. warm-up and best-of-N: the first call is 3 s slower (cold start)
        for name, st_extra, expect in (("warm-up discards the cold start", ["--warmup", "1", "--runs", "2"], 0),
                                       ("best-of-3 hides a cold start", ["--warmup", "0", "--runs", "3"], 0),
                                       ("a single cold run counts", ["--warmup", "0", "--runs", "1"], 1)):
            st = os.path.join(tmp, "state")
            if os.path.exists(st):
                os.remove(st)
            rc, out = harness("--decoder", tmpl("--pace-fps", 100, "--first-run-extra", 3, "--state", st), *base[:2],
                              "--no-ref", "--target-fps", "15", "--timeout", "60", "--workdir", tmp, *st_extra)
            rep.check(rc == expect, f"{name}: expected exit {expect}, got {rc}\n{out[-500:]}")

        # 4. wrong or misbehaving decoders: every one must exit 1 with the right headline
        cases = [  # mutant, extra harness args, text that must appear
            ("pixel_luma", [], "frame 3 (0-based, output order) plane Y"),
            ("pixel_cr_last", [], "plane Cr"),
            ("swap_uv", [], "Cb and Cr are swapped"),
            ("gray_chroma", [], "luma is right; only chroma differs"),
            ("drop_last_frame", [], "stops early"),
            ("dup_frame", [], "WRONG OUTPUT"),
            ("swap_frames", [], "dropped, duplicated or reordered"),
            ("partial_frame", [], "stops early"),
            ("extra_bytes", [], "7 extra byte(s)"),
            ("uncropped", [], "UNCROPPED"),
            ("empty_output", [], "empty"),
            ("no_output_file", [], "NO OUTPUT FILE"),
            ("exit_nonzero", [], "non-zero exit status 3 (the output itself is correct)"),
            ("exit_139", [], "CRASH: exit status 139"),
            ("crash", [], "killed by signal 11"),
            ("hang", ["--timeout", "4"], "HANG"),
            ("explode", [], "OUTPUT EXPLOSION"),
            ("memhog", ["--max-rss-mb", "400"], "MEMORY"),
            ("flaky_output", ["--runs", "3"], "run 2: WRONG OUTPUT"),
        ]
        for m, extra, text in cases:
            st = os.path.join(tmp, "state")
            if os.path.exists(st):
                os.remove(st)
            args_ = [x for x in base]
            for k in range(0, len(extra), 2):          # extra options replace the base values
                if extra[k] in args_:
                    args_[args_.index(extra[k]) + 1] = extra[k + 1]
                else:
                    args_ += extra[k:k + 2]
            rc, out = harness("--decoder", tmpl("--mutant", m, "--state", st), *args_)
            rep.check(rc == 1 and text in out, f"mutant {m}: expected exit 1 and {text!r}; got exit {rc}\n{out[-700:]}")
        rep.check(len(cases) >= 12, "fewer than 12 wrong-behaviour cases")
        rc, out = harness("--decoder", tmpl("--pace-fps", 3), *base[:2], "--runs", "1", "--warmup", "0", "--no-ref",
                          "--target-fps", "15", "--workdir", tmp)
        rep.check(rc == 1 and "below the target" in out, f"too slow stand-in: expected exit 1, got {rc}")
        if shutil.which("pgrep"):
            p = subprocess.run(["pgrep", "-f", "sleep 31337"], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            rep.check(p.returncode != 0, "the hanging decoder's child process survived the process-group kill")
            if p.returncode == 0:
                subprocess.run(["pkill", "-f", "sleep 31337"], stdin=subprocess.DEVNULL, timeout=10)

        # 5. malformed input: exit 2
        empty = os.path.join(tmp, "empty.264")
        open(empty, "wb").close()
        junk = os.path.join(tmp, "junk.264")
        with open(junk, "wb") as f:
            f.write(os.urandom(5000))
        good = tmpl()
        bad_cases = [
            ("no --decoder", []),
            ("no {in}", ["--decoder", "python3 x {out}"]),
            ("no {out}", ["--decoder", "python3 x {in}"]),
            ("unbalanced quote", ["--decoder", "'abc {in} {out}"]),
            ("program does not exist", ["--decoder", "/nonexistent/dec {in} {out}", "--clip", "tiny"]),
            ("--runs 0", ["--decoder", good, "--runs", "0"]),
            ("--runs abc", ["--decoder", good, "--runs", "abc"]),
            ("--target-fps -5", ["--decoder", good, "--target-fps", "-5"]),
            ("--timeout 0", ["--decoder", good, "--timeout", "0"]),
            ("--target-fps nan", ["--decoder", good, "--target-fps", "nan"]),
            ("unknown clip", ["--decoder", good, "--clip", "huge"]),
            ("missing stream", ["--decoder", good, "--stream", os.path.join(tmp, "nope.264")]),
            ("empty stream", ["--decoder", good, "--stream", empty]),
            ("junk stream", ["--decoder", good, "--stream", junk]),
            ("--frames 1", ["--decoder", good, "--frames", "1"]),
            ("unknown option", ["--decoder", good, "--bogus"]),
        ]
        for name, a in bad_cases:
            rc, out = harness(*a, "--workdir", tmp) if "--clip" in a or "--stream" in a else harness(*a)
            rep.check(rc == 2, f"malformed input '{name}': expected exit 2, got {rc}\n{out[-300:]}")
        rep.check(not os.path.exists(os.path.join(tmp, "out.yuv")), "output file left behind")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"self-test: {len(cases)} wrong-behaviour cases, {len(bad_cases)} malformed-input cases")
    rep.finish()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Bad as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
