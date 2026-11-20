#!/usr/bin/env python3
"""Stand-in for the READER's decoder (Chapter 10.7): a wrapper around `ffmpeg -threads 1` that follows the decoder
interface, with switches that make it artificially slow or wrong, so the harness itself can be tested.

    python3 ref_decoder.py [--pace-fps F] [--first-run-extra S --state FILE] [--mutant NAME] IN.264 OUT.yuv

  --pace-fps F          never finish faster than frames/F seconds (an artificial slowdown; the wall time is a floor, so
                        it can only get slower on a busy machine, never faster)
  --first-run-extra S   with --state FILE: the first call (FILE absent) sleeps S more seconds (a cold start)
  --mutant NAME         one deliberate bug, see --list-mutants
"""
import os
import signal
import subprocess
import sys
import time

MUTANTS = {
    "pixel_luma": "one luma sample off by one in frame 3",
    "pixel_cr_last": "last Cr sample of the last frame wrong",
    "swap_uv": "Cb and Cr planes swapped",
    "gray_chroma": "chroma planes all 128",
    "drop_last_frame": "last frame missing",
    "dup_frame": "frame 2 written twice",
    "swap_frames": "frames 1 and 2 in the wrong order",
    "partial_frame": "file ends in the middle of the last frame",
    "extra_bytes": "7 extra bytes at the end",
    "uncropped": "writes the coded 16-aligned height instead of the cropped one (pads with zero rows)",
    "empty_output": "exit 0 but an empty file",
    "no_output_file": "exit 0 and no file at all",
    "exit_nonzero": "correct output but exit status 3",
    "exit_139": "correct output but exit status 139",
    "crash": "correct output, then SIGSEGV",
    "hang": "endless loop (and a child process that sleeps for ever)",
    "explode": "writes output without end",
    "memhog": "holds 700 MB while decoding",
    "flaky_output": "correct the first time, one wrong sample on every later call (needs --state)",
}


def geometry(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", path], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    w, h = r.stdout.decode().strip().split(",")[:2]
    return int(w), int(h)


def decode(inp, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "1", "-i", inp, "-map", "0:v:0", "-fps_mode", "passthrough",
                    "-f", "rawvideo", "-pix_fmt", "yuv420p", out], stdin=subprocess.DEVNULL, check=True, timeout=300)


def main(argv):
    pace = extra = 0.0
    state = mutant = None
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--list-mutants":
            for k, v in MUTANTS.items():
                print(f"{k:16s} {v}")
            return 0
        if a in ("--pace-fps", "--first-run-extra", "--state", "--mutant"):
            v = argv[i + 1]
            i += 2
            if a == "--pace-fps":
                pace = float(v)
            elif a == "--first-run-extra":
                extra = float(v)
            elif a == "--state":
                state = v
            else:
                mutant = v
                if v not in MUTANTS:
                    sys.exit(f"unknown mutant {v}")
        else:
            pos.append(a)
            i += 1
    inp, out = pos
    t0 = time.perf_counter()
    first = False
    call_no = 0
    if state:
        if os.path.exists(state):
            call_no = int(open(state).read() or 0)
        first = call_no == 0
        with open(state, "w") as f:
            f.write(str(call_no + 1))
    if mutant == "hang":
        subprocess.Popen(["sleep", "31337"], stdin=subprocess.DEVNULL)
        while True:
            time.sleep(1)
    if mutant == "explode":
        with open(out, "wb") as f:
            z = bytes(1 << 20)
            while True:
                f.write(z)
    if mutant == "no_output_file":
        return 0
    hog = None
    if mutant == "memhog":
        hog = bytearray(b"\x01") * (700 << 20)        # really touches the pages
    decode(inp, out)
    w, h = geometry(inp)
    fb = w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
    if mutant:
        with open(out, "rb") as f:
            d = bytearray(f.read())
        n = len(d) // fb
        ysz = w * h
        csz = ((w + 1) // 2) * ((h + 1) // 2)
        rc = 0
        if mutant == "pixel_luma":
            d[min(3, n - 1) * fb + ysz // 2 + 5] ^= 1
        elif mutant == "pixel_cr_last":
            d[-1] ^= 0x10
        elif mutant == "swap_uv":
            for k in range(n):
                o = k * fb + ysz
                d[o:o + 2 * csz] = d[o + csz:o + 2 * csz] + d[o:o + csz]
        elif mutant == "gray_chroma":
            for k in range(n):
                o = k * fb + ysz
                d[o:o + 2 * csz] = b"\x80" * (2 * csz)
        elif mutant == "drop_last_frame":
            del d[-fb:]
        elif mutant == "dup_frame":
            d[2 * fb:2 * fb] = d[2 * fb:3 * fb]
        elif mutant == "swap_frames":
            a, b = d[fb:2 * fb], d[2 * fb:3 * fb]
            d[fb:3 * fb] = b + a
        elif mutant == "partial_frame":
            del d[-fb // 2:]
        elif mutant == "extra_bytes":
            d += b"\x00" * 7
        elif mutant == "uncropped":
            h16 = (h + 15) // 16 * 16
            nd = bytearray()
            for k in range(n):
                fr = d[k * fb:(k + 1) * fb]
                cw, pad = (w + 1) // 2, (h16 - h) // 2
                nd += (fr[:ysz] + bytes(w * (h16 - h)) + fr[ysz:ysz + csz] + bytes(cw * pad)
                       + fr[ysz + csz:] + bytes(cw * pad))
            d = nd
        elif mutant == "empty_output":
            d = bytearray()
        elif mutant == "exit_nonzero":
            rc = 3
        elif mutant == "exit_139":
            rc = 139
        elif mutant == "flaky_output" and not first:
            d[fb + 100] ^= 1
        with open(out, "wb") as f:
            f.write(d)
        if mutant == "crash":
            os.kill(os.getpid(), signal.SIGSEGV)
        if rc:
            return rc
    if hog is not None:
        del hog
    if first and extra:
        time.sleep(extra)
    if pace:
        nframes = os.path.getsize(out) // fb
        left = nframes / pace - (time.perf_counter() - t0)
        if left > 0:
            time.sleep(left)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
