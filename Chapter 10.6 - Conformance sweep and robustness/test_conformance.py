#!/usr/bin/env python3
"""Chapter 10.6 harness: conformance sweep + robustness (fuzz) harness for YOUR decoder.

The decoder is any command line with the placeholders {in} and {out}:

    --decoder "./mydec {in} {out}"

  {in}   an Annex B H.264 byte stream (a .ts/.mp4 input is first remuxed to Annex B; --input-format original passes it unchanged)
  {out}  the file the decoder must write: raw 8-bit planar yuv420p, cropped, all frames, output order, no headers
  exit status 0 on success; on damaged input any status 0..127 is acceptable; a signal, a status >= 128 or a hang is a bug.

Modes (--mode):
  sweep  decode every stream and compare with its expected output (a user-supplied .yuv / .md5, else ffmpeg -threads 1)
  fuzz   deterministic corruption of streams; the decoder must not crash/hang/explode and must be correct again from the
         next IDR that follows the damage
  both   sweep, then fuzz (default)

Streams: --stream S [--expected E]   one stream (E = .yuv or .md5, else ffmpeg is the truth)
         --dir D                     every stream in D with its X.yuv / X.md5 (H.264.1 style pairs; others use ffmpeg)
         --builtin quick|full        the built-in set (x264 + the book's synthetic generators); the default

Exit status: 0 everything passed, 1 at least one check failed, 2 unusable input / usage error.
--self-test checks this harness against a stand-in decoder (ref_decoder.py) and 23 deliberately broken variants.
"""
import argparse
import contextlib
import hashlib
import io
import os
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402
import cs_streams as cs  # noqa: E402

STREAM_EXTS = {".264", ".h264", ".avc", ".26l", ".jsv", ".jvt", ".bit", ".bits", ".bin", ".ts", ".mp4", ".mkv", ".mov", ".m4v"}
ES_EXTS = {".264", ".h264", ".avc", ".26l", ".jsv", ".jvt", ".bit", ".bits", ".bin", ".m4v"}
MIB = 1 << 20


class Bad(Exception):
    """Unusable input: exit status 2."""


def frame_bytes(w, h):
    return w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)


# ------------------------------------------------------------------------------------------------ running the decoder
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


def run_decoder(args, inp, out, timeout, max_out, workdir, mem_mb=0):
    """-> dict(status, rc, secs, out_size, log).  status: ok | error_exit | timeout | crash | explosion."""
    argv = [a.replace("{in}", inp).replace("{out}", out) for a in args]
    if os.path.exists(out):
        os.remove(out)

    def pre():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if max_out:
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_out, max_out))
        if mem_mb:
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * MIB, mem_mb * MIB))

    logp = os.path.join(workdir, "decoder.log")
    t0 = time.time()
    status = None
    with open(logp, "wb") as lf:
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, preexec_fn=pre,
                                 start_new_session=True)
        except OSError as e:
            raise Bad(f"cannot start the decoder {argv[0]!r}: {e}")
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc, status = None, "timeout"
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)               # never leave stragglers behind
            except OSError:
                pass
            p.wait()
    secs = time.time() - t0
    size = os.path.getsize(out) if os.path.exists(out) else -1
    if status is None:
        if rc < 0 and -rc == signal.SIGXFSZ or (max_out and size >= max_out):
            status = "explosion"
        elif rc < 0 or rc >= 128:
            status = "crash"
        elif rc != 0:
            status = "error_exit"
        else:
            status = "ok"
    with open(logp, "rb") as f:
        log = f.read()[-300:].decode(errors="replace").strip()
    return dict(status=status, rc=rc, secs=secs, out_size=size, log=log)


def describe_run(r, timeout):
    s, rc = r["status"], r["rc"]
    if s == "timeout":
        return f"HANG: no exit after {timeout} s"
    if s == "explosion":
        return f"OUTPUT EXPLOSION: the file reached {r['out_size']} bytes"
    if s == "crash":
        return f"CRASH: killed by signal {-rc}" if rc < 0 else f"CRASH: exit status {rc} (>= 128 means a signal)"
    if s == "error_exit":
        return f"exit status {rc}"
    return "ok"


# ------------------------------------------------------------------------------------------------ ground truth
def probe_geometry(path):
    j = ffref.probe_json(path, "-select_streams", "v:0", "-show_entries", "stream=width,height,pix_fmt,profile")
    st = (j.get("streams") or [{}])[0]
    if not st.get("width"):
        raise Bad(f"{path}: ffprobe finds no video stream")
    return int(st["width"]), int(st["height"]), st.get("pix_fmt", "?")


def ffmpeg_truth(path, out_yuv):
    """ffmpeg -threads 1 decode: all frames, no dup/drop, cropped, yuv420p.  Documented deviations from the standard: ffmpeg
    conceals missing references and unpaired fields instead of following Annex C, may skip non-IDR leading pictures, and drops
    the pictures before the first recovery point; it converts non-4:2:0 / >8-bit streams to 8-bit 4:2:0."""
    r = ffref.run(["ffmpeg", "-v", "error", "-y", "-threads", "1", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
                   "-f", "rawvideo", "-pix_fmt", "yuv420p", out_yuv], timeout=600)
    if r.returncode != 0 or not os.path.exists(out_yuv):
        raise Bad(f"{path}: ffmpeg cannot decode this stream: {r.stderr.decode(errors='replace')[-200:]}")


def to_es(path, tmp, mode):
    ext = os.path.splitext(path)[1].lower()
    if mode == "original" or ext in ES_EXTS:
        return path
    out = os.path.join(tmp, os.path.splitext(os.path.basename(path))[0] + ".es264")
    ffref.raw_h264(path, out)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise Bad(f"{path}: cannot extract an H.264 elementary stream with ffmpeg")
    return out


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_md5_file(path):
    m = re.search(r"\b[0-9a-fA-F]{32}\b", open(path, errors="replace").read())
    if not m:
        raise Bad(f"{path}: no 32-digit hexadecimal md5 found")
    return m.group(0).lower()


# ------------------------------------------------------------------------------------------------ comparing yuv
def locate(w, h, off):
    """Byte offset inside a frame -> (plane, x, y, mbx, mby) in that plane's own sample units (macroblock = 16 luma / 8 chroma)."""
    ys, cw, ch = w * h, (w + 1) // 2, (h + 1) // 2
    if off < ys:
        return "Y", off % w, off // w, (off % w) // 16, (off // w) // 16
    off -= ys
    pl = "Cb"
    if off >= cw * ch:
        off -= cw * ch
        pl = "Cr"
    return pl, off % cw, off // cw, (off % cw) // 8, (off // cw) // 8


def frame_diff(e, a, w, h):
    """Two frames (bytes) -> None if equal else dict(plane, x, y, mbx, mby, exp, got, n, planes, mbs)."""
    if e == a:
        return None
    first = next(i for i in range(len(e)) if e[i] != a[i])
    pl, x, y, mx, my = locate(w, h, first)
    ys, cw, ch = w * h, (w + 1) // 2, (h + 1) // 2
    bad_planes, mbs = set(), set()
    n = 0
    for i in range(len(e)):
        if e[i] != a[i]:
            n += 1
            p = locate(w, h, i)
            bad_planes.add(p[0])
            mbs.add((p[3], p[4]))                              # chroma 8x8 block (x,y) is the same macroblock as luma 16x16 (x,y)
    return dict(plane=pl, x=x, y=y, mbx=mx, mby=my, exp=e[first], got=a[first], n=n, planes=sorted(bad_planes), mbs=len(mbs),
                swapped=(e[ys:ys + cw * ch] == a[ys + cw * ch:] and e[ys + cw * ch:] == a[ys:ys + cw * ch] and e[:ys] == a[:ys]))


def compare_yuv_files(exp_path, act_path, w, h, ptypes=None):
    """-> (ok, short text, extra lines, info dict).  Frame-by-frame, the first difference is located exactly."""
    fs = frame_bytes(w, h)
    es, as_ = os.path.getsize(exp_path), os.path.getsize(act_path)
    nfe, nfa = es // fs, as_ // fs
    info = dict(nfe=nfe, nfa=nfa, first=None, bad_frames=0)
    lines = []
    first = None
    bad = 0
    with open(exp_path, "rb") as fe, open(act_path, "rb") as fa:
        for i in range(min(nfe, nfa)):
            e, a = fe.read(fs), fa.read(fs)
            if e != a:
                bad += 1
                if first is None:
                    first = (i, frame_diff(e, a, w, h))
    info.update(bad_frames=bad)
    parts = []
    if first:
        i, d = first
        info["first"] = (i, d)
        pt = f" ({ptypes[i]})" if ptypes and i < len(ptypes) else ""
        parts.append(f"frame {i}{pt} plane {d['plane']} MB({d['mbx']},{d['mby']}) x={d['x']} y={d['y']} expected {d['exp']} got {d['got']}")
        lines.append(f"{bad} of {min(nfe, nfa)} compared frames differ; in frame {i}: {d['n']} sample(s) in {d['mbs']} macroblock-position(s), planes {'+'.join(d['planes'])}")
        with open(exp_path, "rb") as fe, open(act_path, "rb") as fa:
            fa.seek(i * fs)
            actual_i = fa.read(fs)
            for j in (i - 1, i + 1):
                if 0 <= j < nfe:
                    fe.seek(j * fs)
                    if fe.read(fs) == actual_i:
                        lines.append(f"output frame {i} is identical to expected frame {j}: frames dropped, duplicated or out of order")
        if d["swapped"]:
            lines.append("Cb and Cr are swapped in this frame")
        elif d["planes"] == ["Y"]:
            lines.append("only luma differs (chroma is right)")
        elif "Y" not in d["planes"]:
            lines.append("only chroma differs (luma is right)")
    if nfa != nfe or as_ % fs:
        parts.append(f"size: expected {nfe} frames ({es} bytes), got {nfa} frames" + (f" + {as_ % fs} stray bytes ({as_} bytes)" if as_ % fs else f" ({as_} bytes)"))
        W16, H16 = (w + 15) // 16 * 16, (h + 15) // 16 * 16
        if (W16, H16) != (w, h) and as_ == nfe * frame_bytes(W16, H16):
            lines.insert(0, f"the output is exactly {nfe} frames of {W16}x{H16}: it looks UNCROPPED; apply the SPS cropping window "
                            f"to get {w}x{h} (Ch 3.1, 9.1)")
        elif not first:
            lines.append("all compared frames are identical; the output is too short, too long or has a partial last frame")
    return (not parts), "; ".join(parts), lines, info


# ------------------------------------------------------------------------------------------------ feature guess
def trace_features(path, tmp):
    txt = os.path.join(tmp, "trace.txt")
    try:
        ffref.trace_headers(path, txt, timeout=120)
    except Exception:
        return {}
    feats = {}
    rx = re.compile(r"\]\s+\d+\s+([A-Za-z_0-9]+)\s+[01]*\s*=\s*(-?\d+)\s*$")
    for line in open(txt, errors="replace"):
        m = rx.search(line)
        if m:
            feats.setdefault(m.group(1), []).append(int(m.group(2)))
    return feats


def feature_tags(f):
    def has(name, val=1):
        return val in f.get(name, [])
    t = []
    prof = f.get("profile_idc", [0])[0]
    t.append({66: "Baseline", 77: "Main", 88: "Extended", 100: "High"}.get(prof, f"profile_idc {prof}"))
    t.append("CABAC" if has("entropy_coding_mode_flag") else "CAVLC")
    if has("field_pic_flag"):
        t.append("PAFF fields")
    if has("mb_adaptive_frame_field_flag") and has("frame_mbs_only_flag", 0):
        t.append("MBAFF")
    st = {v % 5 for v in f.get("slice_type", [])}
    t.append("slices " + "".join(c for v, c in ((2, "I"), (0, "P"), (1, "B"), (3, "SP"), (4, "SI")) if v in st))
    fm, nsl = len([v for v in f.get("first_mb_in_slice", []) if v == 0]), len(f.get("first_mb_in_slice", []))
    if fm and nsl > fm:
        t.append(f"multi-slice ({nsl} slices / {fm} pictures)")
    if has("weighted_pred_flag"):
        t.append("explicit weighted P")
    if 1 in f.get("weighted_bipred_idc", []):
        t.append("explicit weighted B")
    if 2 in f.get("weighted_bipred_idc", []):
        t.append("implicit weighted B")
    if has("transform_8x8_mode_flag"):
        t.append("8x8 transform")
    if has("seq_scaling_matrix_present_flag") or has("pic_scaling_matrix_present_flag"):
        t.append("scaling lists")
    if 1 in f.get("slice_type", []) or 6 in f.get("slice_type", []):
        t.append("direct " + ("spatial" if has("direct_spatial_mv_pred_flag") else "temporal") if "direct_spatial_mv_pred_flag" in f else "B")
    if has("adaptive_ref_pic_marking_mode_flag"):
        t.append("MMCO")
    if has("ref_pic_list_modification_flag_l0") or has("ref_pic_list_modification_flag_l1"):
        t.append("list reordering")
    if has("constrained_intra_pred_flag"):
        t.append("constrained intra")
    if any(v in (1, 2) for v in f.get("disable_deblocking_filter_idc", [])):
        t.append("deblock idc 1/2")
    if any(v for v in f.get("slice_alpha_c0_offset_div2", [])):
        t.append("deblock offsets")
    if f.get("pic_order_cnt_type") and f["pic_order_cnt_type"][0]:
        t.append(f"poc type {f['pic_order_cnt_type'][0]}")
    return t


def guess_area(tags, ptype, planes, whole, lines=()):
    tg = " ".join(tags)
    g = []
    if any("UNCROPPED" in ln for ln in lines):
        return ["cropping window not applied on output (Ch 3.1, 9.1)"]
    if any("swapped" in ln for ln in lines):
        return ["Cb and Cr written in the wrong order (YUV writer, Ch 5.1 / 9.2), or the chroma planes mixed up in reconstruction"]
    if any("identical to expected frame" in ln for ln in lines):
        return ["output order / bumping (Ch 9.1), frame_num gaps, or a frame dropped or repeated by the writer (Ch 5.1)"]
    if ptype == "I":
        g.append("intra prediction / residual reconstruction (Ch 5.2-5.4)")
        if "8x8 transform" in tg:
            g.append("8x8 transform and intra 8x8 (Ch 8.5)")
        if "scaling lists" in tg:
            g.append("scaling lists / flat-16 fallback rules (Ch 8.6)")
        if "CABAC" in tg:
            g.append("CABAC residual / mb_type contexts (Ch 8.2-8.3)")
        if "constrained intra" in tg:
            g.append("constrained_intra_pred availability")
    elif ptype in ("P", "B"):
        g.append("motion vector prediction / interpolation (Ch 7.3-7.4)")
        g.append("reference list construction and marking (Ch 7.2, 7.6)")
        if "weighted" in tg:
            g.append("weighted prediction (Ch 7.7, 7.9)")
        if ptype == "B":
            g.append("direct modes / bi-prediction (Ch 7.8-7.9)")
        if "CABAC" in tg:
            g.append("CABAC inter syntax (Ch 8.4)")
    if not whole:
        g.append("deblocking filter at the affected edges (Ch 6.x, 7.5)")
    if "MBAFF" in tg:
        g.append("MBAFF neighbour derivation / field-MB reconstruction (Ch 10.3-10.4)")
    if "PAFF fields" in tg:
        g.append("field pictures, field reference lists (Ch 10.1-10.2)")
    if "Y" not in planes and planes:
        g.insert(0, "chroma-only: chroma QP offset, chroma prediction / MC / deblocking")
    return g[:4]


# ------------------------------------------------------------------------------------------------ sweep
def collect_sources(opts):
    """-> list of dict(name, path, exp_kind, exp_path, tags)."""
    out = []
    if opts.stream:
        if not os.path.isfile(opts.stream):
            raise Bad(f"--stream {opts.stream}: no such file")
        ek, ep = "ffmpeg", None
        if opts.expected:
            if not os.path.isfile(opts.expected):
                raise Bad(f"--expected {opts.expected}: no such file")
            ek, ep = ("md5" if opts.expected.lower().endswith(".md5") else "yuv"), opts.expected
        out.append(dict(name=os.path.splitext(os.path.basename(opts.stream))[0], path=opts.stream, exp_kind=ek, exp_path=ep, tags=""))
    elif opts.expected:
        raise Bad("--expected needs --stream")
    if opts.dir:
        if not os.path.isdir(opts.dir):
            raise Bad(f"--dir {opts.dir}: not a directory")
        names = sorted(os.listdir(opts.dir))
        for n in names:
            stem, ext = os.path.splitext(n)
            if ext.lower() not in STREAM_EXTS or not os.path.isfile(os.path.join(opts.dir, n)):
                continue
            ek, ep = "ffmpeg", None
            for cand, kind in ((stem + ".yuv", "yuv"), (stem + ".YUV", "yuv"), (stem + "_dec.yuv", "yuv"), (stem + ".md5", "md5"), (stem + ".MD5", "md5")):
                if cand in names:
                    ek, ep = kind, os.path.join(opts.dir, cand)
                    break
            out.append(dict(name=stem, path=os.path.join(opts.dir, n), exp_kind=ek, exp_path=ep, tags=""))
        if not out:
            raise Bad(f"--dir {opts.dir}: no stream files ({', '.join(sorted(STREAM_EXTS))}) found")
    if not out:
        out = [dict(name=c["name"], path=None, case=c, exp_kind="ffmpeg", exp_path=None, tags=c["tags"])
               for c in cs.builtin_cases(opts.builtin)]
    if opts.only:
        keys = [k for k in opts.only.split(",") if k]
        out = [s for s in out if any(k in s["name"] for k in keys)]
        if not out:
            raise Bad(f"--only {opts.only}: nothing matches (--list shows the built-in names)")
    return out


def materialize(src, tmp, opts):
    """Make sure the source has a file; returns the Annex B path (or the original container when --input-format original)."""
    if src.get("path") is None:
        try:
            src["path"] = cs.make_stream(src["case"])
        except Exception as e:
            raise Bad(f"cannot generate built-in stream {src['name']}: {e}")
    return to_es(src["path"], tmp, opts.input_format)


def sweep_one(src, args, opts, tmp):
    """-> dict(name, ok, frames, detail, lines)."""
    work = tempfile.mkdtemp(prefix="sw_", dir=tmp)
    es = materialize(src, work, opts)
    ref_in = es                                              # truth is taken from exactly what the decoder reads
    w, h, pix = probe_geometry(ref_in)
    fs = frame_bytes(w, h)
    truth = os.path.join(work, "truth.yuv")
    exp_kind, exp_path = src["exp_kind"], src["exp_path"]
    if exp_kind == "yuv":
        truth = exp_path
        if os.path.getsize(truth) % fs:
            raise Bad(f"{src['name']}: expected yuv is {os.path.getsize(truth)} bytes, not a multiple of the {w}x{h} frame size {fs} "
                      f"(ffmpeg reports the CROPPED size {w}x{h}; is the expected file uncropped or not 8-bit 4:2:0?)")
    else:
        ffmpeg_truth(ref_in, truth)
    nfe = os.path.getsize(truth) // fs
    out = os.path.join(work, "dec.yuv")
    max_out = max(16 * os.path.getsize(truth), 16 * MIB)
    r = run_decoder(args, es, out, opts.timeout, max_out, work, opts.mem_mb)
    row = dict(name=src["name"], ok=False, frames=f"?/{nfe}", detail="", lines=[], truth=exp_kind, secs=r["secs"])
    if r["status"] in ("timeout", "crash", "explosion"):
        row["detail"] = describe_run(r, opts.timeout)
    elif r["out_size"] < 0:
        row["detail"] = "NO OUTPUT FILE" + (f" (decoder exit status {r['rc']})" if r["rc"] else "")
    else:
        nfa = r["out_size"] // fs
        row["frames"] = f"{nfa}/{nfe}"
        ptypes = None
        if exp_kind == "md5" and md5_of(out) == read_md5_file(exp_path):
            ok, det, lines = True, "", []
            info = {}
        elif exp_kind == "md5":
            ok, det, lines, info = False, f"md5 differs (expected {read_md5_file(exp_path)}, got {md5_of(out)})", [], {}
            tp = os.path.join(work, "truth.yuv")
            ffmpeg_truth(ref_in, tp)
            if md5_of(tp) == read_md5_file(exp_path):
                _, d2, lines, info = compare_yuv_files(tp, out, w, h)
                lines.insert(0, "located against ffmpeg's decode, which matches the md5: " + d2)
            else:
                lines.append("ffmpeg's decode does not match this md5 either; give --expected X.yuv to locate the difference")
        else:
            try:
                ptypes = [x["pict_type"] for x in ffref.probe_frames(ref_in, "pict_type")]
            except Exception:
                ptypes = None
            ok, det, lines, info = compare_yuv_files(truth, out, w, h, ptypes)
        if ok and r["status"] == "error_exit":
            ok, det = False, f"output is correct but the decoder exit status is {r['rc']} (must be 0 on a valid stream)"
        row.update(ok=ok, detail=det, lines=lines)
        if not ok and info.get("first"):
            i, d = info["first"]
            tags = feature_tags(trace_features(es, work))
            pt = ptypes[i] if ptypes and i < len(ptypes) else "?"
            whole = d["n"] > fs // 4
            row["lines"].append("stream: " + ", ".join(tags))
            row["lines"].append("guess (heuristic, most likely first): " + "; ".join(guess_area(tags, pt, d["planes"], whole, row["lines"])))
        elif not ok and exp_kind != "md5" and "size" in det:
            tags = feature_tags(trace_features(es, work))
            row["lines"].append("stream: " + ", ".join(tags))
            row["lines"].append("guess: frame count/order wrong: output ordering and bumping (Ch 9.1), frame_num gaps, unpaired fields (Ch 10.1), or a crash/early exit")
    row["ok"] = row["ok"] and r["status"] == "ok"
    if not row["ok"] and not row["detail"]:
        row["detail"] = describe_run(r, opts.timeout)
    if r["status"] == "ok" or not row["ok"]:
        if r["log"] and not row["ok"]:
            row["lines"].append("decoder said: " + r["log"].replace("\n", " | ")[-200:])
    return row


def run_sweep(sources, args, opts, tmp, rep):
    print(f"conformance sweep: {len(sources)} stream(s); decoder: {opts.decoder}")
    print(f"  {'#':>3}  {'stream':<34} {'frames':>9}  {'truth':<6} {'result':<6} detail")
    rows = []
    for k, src in enumerate(sources, 1):
        row = sweep_one(src, args, opts, tmp)
        rows.append(row)
        print(f"  {k:>3}  {row['name'][:34]:<34} {row['frames']:>9}  {row['truth']:<6} {'PASS' if row['ok'] else 'FAIL':<6} {row['detail'][:160]}", flush=True)
        for ln in row["lines"] if not row["ok"] else []:
            print(f"        {ln}")
        rep.check(row["ok"], f"{row['name']}: {row['detail']}")
        if not row["ok"] and opts.fail_fast:
            break
    npass = sum(r["ok"] for r in rows)
    print(f"  sweep: {npass} of {len(rows)} stream(s) passed")
    return rows


# ------------------------------------------------------------------------------------------------ fuzz
def key_frames(path):
    rows = ffref.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=key_frame",
                      "-of", "csv=p=0", path], timeout=120).stdout.decode().splitlines()
    return [i for i, ln in enumerate(r for r in rows if r[:1] in "01" and r.strip()) if ln.split(",")[0] == "1"], len(
        [r for r in rows if r[:1] in "01" and r.strip()])


def fuzz_base(src, args, opts, tmp, rep):
    work = tempfile.mkdtemp(prefix="fz_", dir=tmp)
    materialize(src, work, opts)
    es = to_es(src["path"], work, "es")                      # corruptions work on Annex B, whatever --input-format says
    es_for_decoder = es
    w, h, _ = probe_geometry(es)
    fs = frame_bytes(w, h)
    truth = os.path.join(work, "truth.yuv")
    ffmpeg_truth(es, truth)
    tsize = os.path.getsize(truth)
    nf = tsize // fs
    kfs, nframes_probe = key_frames(es)
    data = open(es, "rb").read()
    segs = cs.scan(data)
    idrs = cs.idr_aus(segs)
    mapping_ok = len(kfs) == len(idrs) and nframes_probe == nf
    print(f"fuzz base {src['name']}: {nf} frames, {len(segs)} NAL units, {len(idrs)} IDR picture(s)"
          + ("" if mapping_ok else "; IDR/key-frame mapping unreliable: the recovery check is skipped"), flush=True)
    clean_out = os.path.join(work, "clean.yuv")
    r = run_decoder(args, es_for_decoder, clean_out, opts.timeout, max(16 * tsize, 16 * MIB), work, opts.mem_mb)
    if r["status"] != "ok" or r["out_size"] != tsize or open(clean_out, "rb").read() != open(truth, "rb").read():
        msg = f"{src['name']} (clean): the undamaged stream is not decoded correctly ({describe_run(r, opts.timeout)}; run the sweep first); fuzz skipped"
        print("  FAIL", msg)
        if r["log"]:
            print("       decoder said: " + r["log"].replace("\n", " | ")[-200:])
        rep.check(False, msg)
        return []
    max_out = 4 * tsize + 4 * MIB
    results = []
    kinds = opts.kinds.split(",") if opts.kinds else cs.KINDS
    for kind in kinds:
        passed = 0
        total = 0
        for seed in range(opts.seed_base, opts.seed_base + opts.seeds):
            total += 1
            new, dmg = cs.corrupt(data, kind, seed, src["name"])
            cp = os.path.join(work, f"c_{kind}_{seed}.264")
            with open(cp, "wb") as f:
                f.write(new)
            outp = os.path.join(work, "c_out.yuv")
            run = run_decoder(args, cp, outp, opts.timeout, max_out, work, opts.mem_mb)
            verdict, notes = judge(run, outp, truth, fs, w, h, nf, kfs if mapping_ok else None, segs, dmg, opts)
            label = f"{src['name']} {kind}#{seed}"
            if verdict == "ok":
                passed += 1
                if opts.verbose:
                    print(f"    ok   {label}: {dmg.desc}; {notes}")
            else:
                print(f"    FAIL {label}: {verdict}")
                print(f"         damage: {dmg.desc} (touches AU {sorted(dmg.touched)})")
                print(f"         {notes}")
                print(f"         repro: --only {src['name']} --kinds {kind} --seed-base {seed} --seeds 1   (output of {run['secs']:.1f} s run)")
                if run["log"]:
                    print("         decoder said: " + run["log"].replace("\n", " | ")[-200:])
                if opts.keep:
                    os.makedirs(opts.keep, exist_ok=True)
                    shutil.copy(cp, os.path.join(opts.keep, f"{src['name']}.{kind}.{seed}.264"))
                rep.check(False, f"{label}: {verdict}")
            results.append((src["name"], kind, seed, verdict == "ok"))
            rep.check(verdict == "ok", "") if verdict == "ok" else None
            if verdict != "ok" and opts.fail_fast:
                print(f"  fuzz {kind}: {passed}/{total} (stopped: --fail-fast)")
                return results
        print(f"  fuzz {kind:<10} {passed}/{total} passed", flush=True)
    return results


def judge(run, outp, truth, fs, w, h, nf, kfs, segs, dmg, opts):
    st = run["status"]
    if st in ("timeout", "crash", "explosion"):
        return describe_run(run, opts.timeout), "rule: never crash, hang or flood the disk, whatever the input"
    size = run["out_size"] if run["out_size"] > 0 else 0
    nfa = size // fs
    if nfa > 2 * nf + 8:
        return f"OUTPUT EXPLOSION: {nfa} frames from a stream whose clean decode has {nf}", "rule: no output explosion"
    hit_ps = dmg.new_ps or any(s.type in (7, 8) and s.au in dmg.touched for s in segs)   # a damaged SPS may change the frame size
    if size % fs and run["rc"] == 0 and not hit_ps:
        return f"PARTIAL FRAME: exit status 0 but the file ends {size % fs} bytes into frame {nfa}", "rule: write whole frames"
    r = cs.recovery_ordinal(segs, dmg, opts.guard)
    if r is None or kfs is None:
        why = "damage reaches the end / no undamaged parameter sets + IDR follow" if r is None else "mapping unreliable"
        return "ok", f"no recovery required ({why}); {nfa} frames out, exit {run['rc']}"
    tail = nf - kfs[r]
    need = f"must be correct again from IDR #{r} on: the last {tail} frame(s)"
    if nfa < tail:
        return (f"NO RECOVERY: only {nfa} frame(s) written, {tail} needed", need)
    with open(truth, "rb") as ft, open(outp, "rb") as fo:
        ft.seek(kfs[r] * fs)
        fo.seek((nfa - tail) * fs)
        for j in range(tail):
            e, a = ft.read(fs), fo.read(fs)
            if e != a:
                d = frame_diff(e, a, w, h)
                return (f"WRONG AFTER RECOVERY: tail frame {j} of {tail} (output frame {nfa - tail + j}, expected frame {kfs[r] + j}) differs: "
                        f"plane {d['plane']} MB({d['mbx']},{d['mby']}) expected {d['exp']} got {d['got']}", need)
    return "ok", f"recovered at IDR #{r} ({tail} frames correct); {nfa} frames out, exit {run['rc']}"


# ------------------------------------------------------------------------------------------------ main
def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--decoder", help='command template with {in} and {out}, e.g. "./mydec {in} {out}"')
    p.add_argument("--stream", help="one stream (.264/.h264/.ts/.mp4 ...)")
    p.add_argument("--expected", help="expected output for --stream: .yuv (raw yuv420p) or .md5; default: ffmpeg -threads 1")
    p.add_argument("--dir", help="directory of stream + X.yuv / X.md5 pairs")
    p.add_argument("--builtin", choices=["quick", "full"], default="quick", help="built-in set when no --stream/--dir (default quick)")
    p.add_argument("--mode", choices=["sweep", "fuzz", "both"], default="both")
    p.add_argument("--only", help="comma-separated substrings of stream names to keep")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds per decoder run (default 60)")
    p.add_argument("--seeds", type=int, default=3, help="corruptions per kind and stream (default 3)")
    p.add_argument("--seed-base", type=int, default=1, help="first seed (default 1)")
    p.add_argument("--kinds", help="comma-separated corruption kinds: " + ",".join(cs.KINDS))
    p.add_argument("--input-format", choices=["es", "original"], default="es", help="give the decoder Annex B (default) or the original file")
    p.add_argument("--guard", type=int, default=1, help="access units after the last damaged one that may still be wrong (default 1)")
    p.add_argument("--mem-mb", type=int, default=0, help="address-space limit for the decoder (0 = none)")
    p.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    p.add_argument("--keep", help="directory to keep failing corrupted streams in")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--list", action="store_true", help="list the built-in streams and exit")
    p.add_argument("--self-test", action="store_true")
    return p


def main(argv):
    opts = build_parser().parse_args(argv)
    try:
        if opts.list:
            for c in cs.builtin_cases(opts.builtin):
                print(f"{c['name']:<36} {c['tags']}")
            return 0
        if opts.self_test:
            return self_test()
        ffref.require_tools()
        if opts.seeds < 1 or opts.timeout <= 0:
            raise Bad("--seeds must be >= 1 and --timeout > 0")
        if opts.kinds:
            bad = [k for k in opts.kinds.split(",") if k not in cs.KINDS]
            if bad:
                raise Bad(f"unknown corruption kind(s) {bad}; known: {','.join(cs.KINDS)}")
        args = parse_template(opts.decoder)
        sources = collect_sources(opts)
        tmp = tempfile.mkdtemp(prefix="conf106_")
        try:
            rep = Report("Chapter 10.6 conformance/robustness")
            if opts.mode in ("sweep", "both"):
                run_sweep(sources, args, opts, tmp, rep)
            if opts.mode in ("fuzz", "both"):
                if opts.mode == "both" and rep.failures and opts.fail_fast:
                    pass
                else:
                    fsrc = sources
                    for src in fsrc:
                        before = len(rep.failures)
                        fuzz_base(src, args, opts, tmp, rep)
                        if opts.fail_fast and len(rep.failures) > before:
                            break
            try:
                rep.finish()
            except SystemExit as e:
                return e.code
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Bad as e:
        print("error:", e)
        return 2


# ------------------------------------------------------------------------------------------------ self test
def self_test():
    sys.path.insert(0, HERE)
    import ref_decoder
    py = shlex.quote(sys.executable)
    rd = shlex.quote(os.path.join(HERE, "ref_decoder.py"))
    good = f"{py} {rd} {{in}} {{out}}"
    tmp = tempfile.mkdtemp(prefix="conf106_self_")
    fails = []
    t0 = time.time()

    def call(*a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main(list(a))
            except SystemExit as e:                            # argparse usage errors
                code = e.code
        return code, buf.getvalue()

    def expect(label, code, want, text="", must=()):
        ok = code == want and all(m in text for m in must)
        print(f"  {'ok  ' if ok else 'BAD '} {label}: exit {code} (want {want})" + ("" if ok else f"\n{text[-1500:]}"), flush=True)
        if not ok:
            fails.append(label)

    try:
        quick = "cavlc_p_ref4,cabac_b_pyramid,mbaff_cabac,paff_cabac_t8_mixed,mbaff_g_b_cavlc,x264_weighted_p"
        print("1. unmodified stand-in must pass")
        c, t = call("--decoder", good, "--mode", "sweep", "--only", quick, "--timeout", "60")
        expect("sweep, 6 built-in streams", c, 0, t, ("6 of 6",))
        c, t = call("--decoder", good, "--mode", "fuzz", "--only", "cavlc_multislice,cabac_b_pyramid,paff_cavlc_reorder", "--seeds", "2", "--timeout", "30")
        expect("fuzz, 3 bases x 9 kinds x 2 seeds", c, 0, t, ("recovered at",) if False else ())
        # a user supplied pair (yuv, md5), a directory, a .ts input
        sp = cs.make_stream(next(x for x in cs.builtin_cases("quick") if x["name"] == "x264_cabac_b_pyramid"))
        ypath = os.path.join(tmp, "pair.yuv")
        ffmpeg_truth(sp, ypath)
        c, t = call("--decoder", good, "--mode", "sweep", "--stream", sp, "--expected", ypath)
        expect("--stream with --expected X.yuv", c, 0, t, ("truth",))
        mpath = os.path.join(tmp, "pair.md5")
        open(mpath, "w").write(md5_of(ypath) + "  pair.yuv\n")
        c, t = call("--decoder", good, "--mode", "sweep", "--stream", sp, "--expected", mpath)
        expect("--stream with --expected X.md5", c, 0, t)
        d = os.path.join(tmp, "dir")
        os.makedirs(d)
        shutil.copy(sp, os.path.join(d, "CVSE_test_A.264"))
        shutil.copy(ypath, os.path.join(d, "CVSE_test_A.yuv"))
        ts = os.path.join(d, "B_from_mp4.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", "25", "-i", sp, "-c", "copy", "-f", "mp4", ts], stdin=subprocess.DEVNULL, timeout=60)
        c, t = call("--decoder", good, "--mode", "sweep", "--dir", d)
        expect("--dir with a yuv pair and an .mp4 (ffmpeg truth)", c, 0, t, ("2 of 2",))
        print("2. wrong expected files must fail with a located diagnosis")
        bad = bytearray(open(ypath, "rb").read())
        w, h, _ = probe_geometry(sp)
        fs = frame_bytes(w, h)
        bad[5 * fs + w * 40 + 70] ^= 0x40
        open(os.path.join(tmp, "bad.yuv"), "wb").write(bad)
        c, t = call("--decoder", good, "--mode", "sweep", "--stream", sp, "--expected", os.path.join(tmp, "bad.yuv"))
        expect("expected yuv with one wrong sample", c, 1, t, ("frame 5", "plane Y", "MB(4,2)", "x=70 y=40"))
        open(os.path.join(tmp, "bad.md5"), "w").write("0" * 32 + "\n")
        c, t = call("--decoder", good, "--mode", "sweep", "--stream", sp, "--expected", os.path.join(tmp, "bad.md5"))
        expect("wrong md5", c, 1, t, ("md5 differs",))

        print("3. malformed input and usage errors must exit 2")
        expect("no --decoder", call("--mode", "sweep")[0], 2)
        expect("template without {out}", call("--decoder", "foo {in}", "--mode", "sweep")[0], 2)
        expect("decoder program does not exist", call("--decoder", "/nonexistent/dec {in} {out}", "--mode", "sweep", "--only", "cavlc_intra")[0], 2)
        expect("missing --stream file", call("--decoder", good, "--stream", os.path.join(tmp, "nope.264"))[0], 2)
        expect("--expected without --stream", call("--decoder", good, "--expected", ypath)[0], 2)
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        expect("empty --dir", call("--decoder", good, "--dir", empty)[0], 2)
        junk = os.path.join(tmp, "junk.264")
        open(junk, "wb").write(os.urandom(500))
        expect("stream that ffmpeg cannot decode", call("--decoder", good, "--stream", junk, "--mode", "sweep")[0], 2)
        open(os.path.join(tmp, "short.yuv"), "wb").write(b"123")
        expect("expected yuv of impossible size", call("--decoder", good, "--stream", sp, "--expected", os.path.join(tmp, "short.yuv"), "--mode", "sweep")[0], 2)
        expect("--only matches nothing", call("--decoder", good, "--only", "zzzz")[0], 2)
        expect("unknown corruption kind", call("--decoder", good, "--kinds", "melt")[0], 2)
        open(os.path.join(tmp, "nomd5.md5"), "w").write("not a hash")
        expect("md5 file without a hash", call("--decoder", good, "--stream", sp, "--expected", os.path.join(tmp, "nomd5.md5"), "--mode", "sweep")[0], 2)

        print(f"4. broken stand-ins ({len(ref_decoder.MUTANTS)} mutants) must be caught")
        for name, (desc, where) in ref_decoder.MUTANTS.items():
            dec = f"{py} {rd} --mutant {name} {{in}} {{out}}"
            if where == "sweep":
                c, t = call("--decoder", dec, "--mode", "sweep", "--only", "cavlc_p_ref4,cabac_b_pyramid", "--fail-fast", "--timeout", "30")
            else:
                c, t = call("--decoder", dec, "--mode", "fuzz", "--only", "cavlc_multislice,cabac_b_pyramid", "--seeds", "2", "--fail-fast",
                            "--timeout", "4")
            expect(f"mutant {name} ({desc})", c, 1, t)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"self-test took {time.time() - t0:.0f} s")
    if fails:
        print("FAIL: Chapter 10.6 self-test: " + ", ".join(fails))
        return 1
    print("PASS: Chapter 10.6 self-test")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
