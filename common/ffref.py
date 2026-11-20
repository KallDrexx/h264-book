"""Thin, timeout-protected wrappers around ffmpeg/ffprobe for chapter harnesses."""
import shutil
import subprocess

DEFAULT_TIMEOUT = 120


def require_tools():
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t) is None:
            raise SystemExit(f"error: {t} not found on PATH (install ffmpeg with libx264)")


def run(args, timeout=DEFAULT_TIMEOUT, capture=True):
    """Run a command with stdin closed and a timeout. Returns CompletedProcess (bytes output)."""
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=capture,
                          timeout=timeout, check=False)


def probe_packets(ts, entries="pts,dts,size,flags"):
    """Rows of video-stream packets: list of dicts keyed by entry name (strings)."""
    names = entries.split(",")
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
             "-show_entries", f"packet={entries}", "-of", "csv=p=0", ts])
    rows = []
    for line in p.stdout.decode().splitlines():
        parts = line.strip().split(",")
        if len(parts) >= len(names):
            rows.append(dict(zip(names, parts)))
    return rows


def probe_frames(ts, entries="pict_type,pts"):
    names = entries.split(",")
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
             "-show_entries", f"frame={entries}", "-of", "csv=p=0", ts])
    rows = []
    for line in p.stdout.decode().splitlines():
        parts = line.strip().split(",")
        if len(parts) >= len(names):
            rows.append(dict(zip(names, parts)))
    return rows


def probe_json(ts, *extra):
    """ffprobe -of json with extra args (e.g. '-show_programs'); returns parsed dict."""
    import json
    p = run(["ffprobe", "-v", "error", "-of", "json", *extra, ts])
    return json.loads(p.stdout.decode() or "{}")


def trace_headers(ts, out_txt, timeout=300):
    """Write ffmpeg's trace_headers log (NAL/SPS/PPS/slice header fields) to out_txt."""
    with open(out_txt, "wb") as f:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-i", ts, "-c:v", "copy",
                            "-bsf:v", "trace_headers", "-f", "null", "-"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=f, timeout=timeout, check=False)
    return p.returncode


def raw_h264(ts, out_264):
    """Reference Annex B elementary stream (what a correct demuxer should produce)."""
    run(["ffmpeg", "-v", "error", "-y", "-i", ts, "-map", "0:v:0", "-c:v", "copy",
         "-f", "h264", out_264], timeout=300)


def reference_yuv(ts, out_yuv):
    """Bit-exact decoded reference: all frames, no frame dup/drop, 8-bit 4:2:0.

    The output is CROPPED per the SPS (frame_cropping_*), planar, unpadded, in output (display) order;
    see common/yuvref.py and common/yuvcmp.py."""
    run(["ffmpeg", "-v", "error", "-y", "-i", ts, "-map", "0:v:0", "-fps_mode", "passthrough",
         "-f", "rawvideo", "-pix_fmt", "yuv420p", out_yuv], timeout=600)
