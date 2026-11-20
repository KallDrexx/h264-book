"""SPOILER: synthetic-stream generator for Chapter 9.1.

x264 streams with unusual output-order settings (encode), SPS bitstream_restriction rewriting (set_restriction), and
no_output_of_prior_pics_flag flipping (set_noout; a one-bit in-place edit, the slice data is untouched).  Everything is
written to a cache directory, never into the chapter folder.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import frontend  # noqa: E402

# name -> (video size, seconds-of-25fps frames, profile, x264-params)
X264_CASES = {
    "ip":       ("352x288", 50, "main", "bframes=0:keyint=25"),
    "b1":       ("352x288", 50, "main", "bframes=1:b-pyramid=none:keyint=30"),
    "pyr3":     ("352x288", 60, "high", "bframes=3:b-pyramid=normal:keyint=40:ref=4"),
    "pyr7":     ("352x288", 60, "high", "bframes=7:b-adapt=0:b-pyramid=normal:keyint=48:ref=5"),
    "manyidr":  ("352x288", 60, "high", "bframes=3:b-pyramid=normal:keyint=7:min-keyint=7:scenecut=0"),
    "opengop":  ("352x288", 60, "high", "bframes=3:b-pyramid=normal:keyint=20:open-gop=1:scenecut=0"),
    "nopyr":    ("352x288", 50, "main", "bframes=3:b-pyramid=none:keyint=30:ref=2"),
    "intra":    ("352x288", 20, "main", "keyint=1"),
    # 640x360: coded 640x368 with SPS cropping (8 rows), the "broadcast-like" file: High profile, long GOP, pyramid
    "broadcast": ("640x360", 75, "high", "bframes=3:b-pyramid=normal:keyint=30:open-gop=1:ref=4:scenecut=0"),
}


def encode(name, outdir, force=False):
    """-> path of a .ts file for X264_CASES[name] (cached)."""
    size, frames, prof, params = X264_CASES[name]
    out = os.path.join(outdir, f"{name}.ts")
    if force or not os.path.exists(out):
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25", "-frames:v", str(frames),
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", "28", "-profile:v", prof, "-x264-params", params,
               "-f", "mpegts", out + ".tmp"]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        if r.returncode:
            raise SystemExit("ffmpeg failed: " + r.stderr.decode()[-500:])
        os.replace(out + ".tmp", out)
    return out


def annexb_nals(data):
    return [data[a:b] for a, b in frontend.split_nals(data)]


def join_nals(nals):
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def set_restriction(data, num_reorder, mdfb):
    """Rewrite every SPS: bitstream_restriction with (num_reorder, mdfb), or none (None, None)."""
    out = []
    for n in annexb_nals(data):
        if n[0] & 31 == 7:
            n = frontend.rewrite_sps_restriction(n, num_reorder, mdfb)
        out.append(n)
    return join_nals(out)


def set_noout(data, which=None):
    """Set no_output_of_prior_pics_flag = 1 in the IDR slices.  which = None (all IDR pictures) or a set of IDR ordinals
    (0 = first IDR).  The flag is one bit in the slice header, so the NAL unit length does not change; emulation
    prevention bytes are re-derived."""
    out, idr_no, last_idr_key = [], -1, None
    spss, ppss = {}, {}
    for n in annexb_nals(data):
        t = n[0] & 31
        if t == 7:
            s = frontend.parse_sps(frontend.unescape(n[1:]))
            spss[s["id"]] = s
        elif t == 8:
            p = frontend.parse_pps(frontend.unescape(n[1:]))
            ppss[p["id"]] = p
        elif t == 5:
            rb = bytearray(frontend.unescape(n[1:]))
            h = frontend.parse_slice_header(bytes(rb), 5, (n[0] >> 5) & 3, spss, ppss)
            if h["first_mb"] == 0:
                idr_no += 1
            if which is None or idr_no in which:
                pos = h["noout_bit"]
                rb[pos >> 3] |= 0x80 >> (pos & 7)
                n = n[:1] + frontend.escape(bytes(rb))
        out.append(n)
    return join_nals(out)
