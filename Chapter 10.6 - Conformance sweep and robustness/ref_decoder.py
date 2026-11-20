#!/usr/bin/env python3
"""Stand-in for the READER's decoder (Chapter 10.6): a thin wrapper around ffmpeg that follows the decoder interface, plus
deliberately broken variants ("mutants") that the harness must catch.

    python3 ref_decoder.py [--mutant NAME] IN.264 OUT.yuv

Interface (the one your own decoder must implement, see the Readme):
  * IN  is an Annex B byte stream, OUT is written as raw 8-bit planar yuv420p, CROPPED, all frames, in output order;
  * exit status 0 normally; a non-zero status below 128 is allowed on bad input; a signal or a hang is a bug.

The wrapper uses `ffmpeg -threads 1` (the same decode the harness uses as ground truth), so the unmutated stand-in must pass
every check.  --list-mutants prints the broken variants and the check that is expected to catch each.
"""
import os
import signal
import subprocess
import sys
import time

# name: (what is wrong, "sweep" or "fuzz" = which harness mode must catch it)
MUTANTS = {
    "pixel_luma": ("one luma sample off by one in frame 3", "sweep"),
    "pixel_cb": ("one Cb sample wrong in frame 1", "sweep"),
    "pixel_cr_last": ("last Cr sample of the last frame wrong", "sweep"),
    "swap_uv": ("Cb and Cr planes swapped", "sweep"),
    "gray_chroma": ("chroma planes all 128", "sweep"),
    "drop_first_frame": ("first frame missing", "sweep"),
    "drop_last_frame": ("last frame missing", "sweep"),
    "dup_frame": ("frame 2 written twice", "sweep"),
    "swap_frames": ("frames 1 and 2 in the wrong order", "sweep"),
    "partial_frame": ("file ends in the middle of the last frame", "sweep"),
    "extra_bytes": ("7 extra bytes at the end", "sweep"),
    "empty_output": ("exit 0 but an empty file", "sweep"),
    "no_output_file": ("exit 0 and no file at all", "sweep"),
    "exit_nonzero": ("correct output but exit status 3", "sweep"),
    "crash_on_error": ("SIGSEGV when the stream is damaged", "fuzz"),
    "exit_139": ("exit status 139 (what a shell reports for SIGSEGV) when damaged", "fuzz"),
    "hang_on_error": ("endless loop when the stream is damaged", "fuzz"),
    "explode_on_error": ("writes output without end when damaged", "fuzz"),
    "stop_at_error": ("gives up after the damage: only half of the frames", "fuzz"),
    "wrong_after_recovery": ("one sample wrong in the last frame after damage", "fuzz"),
    "extra_frame_after_error": ("one extra frame at the end after damage", "fuzz"),
    "partial_on_error": ("exit 0 with a half-written last frame after damage", "fuzz"),
    "no_resync": ("after damage, repeats one stale frame for the second half of the stream", "fuzz"),
}


def geometry(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    try:
        w, h = r.stdout.decode().strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return None


def decode(inp, out):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "1", "-i", inp, "-map", "0:v:0", "-fps_mode", "passthrough",
                        "-f", "rawvideo", "-pix_fmt", "yuv420p", out], stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    return r.returncode, bool(r.stderr.strip())


def main(argv):
    mutant = None
    if argv and argv[0] == "--list-mutants":
        for k, (d, m) in MUTANTS.items():
            print(f"{k:24s} {m:6s} {d}")
        return 0
    if len(argv) >= 2 and argv[0] == "--mutant":
        mutant, argv = argv[1], argv[2:]
        if mutant not in MUTANTS:
            print("unknown mutant", mutant, file=sys.stderr)
            return 64
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 64
    inp, out = argv
    rc, noisy = decode(inp, out)
    if mutant is None:
        return 0 if rc == 0 else 1
    damaged = rc != 0 or noisy
    if mutant == "crash_on_error" and damaged:
        os.kill(os.getpid(), signal.SIGSEGV)
    if mutant == "exit_139" and damaged:
        os._exit(139)
    if mutant == "hang_on_error" and damaged:
        time.sleep(3600)
    if mutant == "explode_on_error" and damaged:
        with open(out, "wb") as f:
            chunk = bytes(1 << 20)
            while True:
                f.write(chunk)
                f.flush()
    if mutant == "no_output_file":
        if os.path.exists(out):
            os.remove(out)
        return 0
    if not os.path.exists(out):
        return rc or 1
    data = open(out, "rb").read()
    wh = geometry(inp)
    fs = (wh[0] * wh[1] + 2 * ((wh[0] + 1) // 2) * ((wh[1] + 1) // 2)) if wh else None
    if mutant in ("exit_nonzero",):
        return 3
    if mutant == "empty_output":
        data = b""
    elif mutant == "partial_frame":
        data = data[:-100]
    elif mutant == "extra_bytes":
        data += b"\x01" * 7
    elif fs and len(data) >= fs * 4:
        n = len(data) // fs
        fr = [bytearray(data[i * fs:(i + 1) * fs]) for i in range(n)]
        W, H = wh
        ys, cs = W * H, ((W + 1) // 2) * ((H + 1) // 2)
        if mutant == "pixel_luma":
            fr[3][17 * W + 40] = (fr[3][17 * W + 40] + 1) & 255
        elif mutant == "pixel_cb":
            fr[1][ys + 5 * ((W + 1) // 2) + 9] ^= 0x10
        elif mutant == "pixel_cr_last":
            fr[-1][-1] ^= 0x04
        elif mutant == "swap_uv":
            fr = [bytearray(f[:ys] + f[ys + cs:] + f[ys:ys + cs]) for f in fr]
        elif mutant == "gray_chroma":
            fr = [bytearray(f[:ys] + bytes([128]) * (2 * cs)) for f in fr]
        elif mutant == "drop_first_frame":
            fr = fr[1:]
        elif mutant == "drop_last_frame":
            fr = fr[:-1]
        elif mutant == "dup_frame":
            fr = fr[:3] + [fr[2]] + fr[3:]
        elif mutant == "swap_frames":
            fr[1], fr[2] = fr[2], fr[1]
        elif damaged and mutant == "stop_at_error":
            fr = fr[: n // 2]
        elif damaged and mutant == "wrong_after_recovery":
            fr[-1][ys // 2 + 3] ^= 0x20
        elif damaged and mutant == "extra_frame_after_error":
            fr.append(fr[-1])
        elif damaged and mutant == "no_resync":
            fr = fr[: n // 2] + [fr[n // 2 - 1]] * (n - n // 2)
        data = b"".join(bytes(f) for f in fr)
        if damaged and mutant == "partial_on_error":
            data = data[:-37]
    elif damaged and mutant == "partial_on_error":
        data = data[:-37]
    with open(out, "wb") as f:
        f.write(data)
    return 0 if mutant != "exit_nonzero" else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
