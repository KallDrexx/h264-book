#!/usr/bin/env python3
"""Chapter 5.1 harness: frame buffers and YUV writer.

usage: test_yuv.py <in.ts> <reader_output.yuv> [--structure-only | --pixels]
       test_yuv.py --make-variants DIR       write the synthetic I_PCM test streams into DIR
       test_yuv.py --self-test               verify this harness itself

Checks (all against ffmpeg, which is never part of your reader):
  structure  the .yuv size is frames x (W*H + 2*((W+1)//2)*((H+1)//2)) for ffmpeg's (cropped) W x H,
             and the frame count equals ffprobe's frame count.
  pixels     your frames are byte-identical to ffmpeg's reference YUV; on a mismatch the first
             differing frame / plane / macroblock / pixel is shown.
Pixels are compared when a <name>.manifest.json sits next to the .ts (every --make-variants stream
has one) or when you pass --pixels; any other file (T1, T2, T4 ...) gets the structure checks only,
because a 5.1 reader cannot predict or transform yet (non-I_PCM macroblocks are written as gray 128).
Exit code: 0 pass, 1 fail, 2 missing or unusable reader output.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import ffref, yuvcmp, yuvref  # noqa: E402
from common.report import Report  # noqa: E402
import pcmgen  # noqa: E402


def die(msg):
    print("error:", msg)
    sys.exit(2)


def run_checks(ts, yuv, mode, title):
    """mode: 'auto', 'pixels' or 'structure'.  -> Report (not finished)."""
    rep = Report(title)
    if not os.path.isfile(yuv):
        die(f"reader output {yuv!r} not found (did your program write it?)")
    if os.path.getsize(yuv) == 0:
        die(f"reader output {yuv!r} is empty")
    info = yuvref.stream_info(ts)
    w, h, n = info["width"], info["height"], info["frames"]
    if not (w and h and n):
        die(f"ffmpeg/ffprobe could not decode any frames from {ts}")
    fs = yuvref.frame_bytes(w, h)
    size = os.path.getsize(yuv)
    man = pcmgen.manifest_path(ts)
    has_man = os.path.isfile(man)
    p = json.load(open(man)) if has_man else None
    rep.check(size == n * fs, size_message(size, n, w, h, p))
    if size % fs:
        print("note: pixels not compared, because the file is not a whole number of frames; fix the size first")
        return rep
    do_pixels = mode == "pixels" or (mode == "auto" and has_man)
    if not do_pixels:
        print(f"note: structure checks only ({'--structure-only' if mode == 'structure' else 'no manifest next to the stream; pass --pixels to compare pixels'})")
        return rep
    tmp = tempfile.mkdtemp(prefix="yuv51_")
    try:
        ref = os.path.join(tmp, "ref.yuv")
        yuvref.reference_yuv(ts, ref)
        if has_man:
            rep.check((w, h) == (p["width"], p["height"]),
                      f"harness sanity: ffmpeg reports {w}x{h}, the stream was built as {p['width']}x{p['height']}")
            truth = b"".join(pcmgen.expected_frames(p))
            rep.check(open(ref, "rb").read() == truth, "harness sanity: ffmpeg's decode differs from the I_PCM samples")
        origin = (2 * p["crop"][0], 2 * p["crop"][2]) if has_man else (0, 0)   # left/top crop, for MB numbers
        r = yuvcmp.compare_yuv(ref, yuv, w, h, quiet=True, origin=origin)
        rep.check(r["ok"], "pixels differ from ffmpeg's reference (expected = ffmpeg, actual = yours)\n    "
                  + r["text"].replace("\n", "\n    "))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rep


def size_message(size, n, w, h, p):
    """The failure text for a wrong file size, with a guess at the cause."""
    fs = yuvref.frame_bytes(w, h)
    msg = (f"file size: expected {n * fs} bytes = {n} frames x {fs} bytes ({w}x{h} after cropping, yuv420p), "
           f"got {size} bytes")
    if size % fs:
        msg += f" = {size // fs} whole frames + {size % fs} bytes"
    else:
        msg += f" = {size // fs} frames (ffprobe counts {n})"
    # coded (macroblock-aligned) size: exact for the synthetic streams, a guess otherwise
    cw, ch = (16 * p["width_mbs"], 16 * p["height_mbs"]) if p else ((w + 15) // 16 * 16, (h + 15) // 16 * 16)
    guesses = [((cw, ch) != (w, h) and size == n * yuvref.frame_bytes(cw, ch),
                f"that is exactly {n} uncropped {cw}x{ch} frames: crop to {w}x{h} when writing (3.4)"),
               ((w % 2 or h % 2) and size == n * (w * h + 2 * (w // 2) * (h // 2)),
                f"that is {n} frames with chroma {w // 2}x{h // 2}: round the chroma size up to {(w + 1) // 2}x{(h + 1) // 2} (3.1)"),
               (size == n * w * h, "that is luma only: write the Cb and Cr planes after Y (3.1)"),
               (size % fs == 0 and size // fs != n, "one frame per decoded picture, none dropped or repeated (3.6)")]
    for hit, text in guesses:
        if hit:
            return msg + "\n    " + text
    return msg

def make_variants(d):
    os.makedirs(d, exist_ok=True)
    for name in pcmgen.VARIANTS:
        ts = pcmgen.write_variant(d, name)
        p = pcmgen.params(name)
        print(f"{ts}  {p['width']}x{p['height']}  {p['frames']} frames")
    print("run your reader on each .ts, then: python3 test_yuv.py <that.ts> <your output.yuv>")


def self_test():
    ffref.require_tools()
    td = tempfile.mkdtemp(prefix="yuv51_self_")
    ok = True

    def expect(label, rep, want_pass):
        nonlocal ok
        good = (not rep.failures) == want_pass
        ok &= good
        print(f"  {'ok  ' if good else 'BAD '} {label}: {'passes' if not rep.failures else 'fails'}"
              f"{'' if good else '  <-- unexpected'}")
        if rep.failures and not want_pass:
            print("       e.g.", rep.failures[0].replace("\n", "\n       ")[:400])

    try:
        for name in ("pcm_200x120", "pcm_198x118", "pcm_100x60_lt"):
            ts = pcmgen.write_variant(td, name)
            p = pcmgen.params(name)
            frames = pcmgen.expected_frames(p)
            good = os.path.join(td, name + ".good.yuv")
            open(good, "wb").write(b"".join(frames))
            expect(f"{name} correct output", run_checks(ts, good, "auto", name), True)
            if name == "pcm_200x120":
                bad = bytearray(b"".join(frames))
                fs = len(frames[0])
                pos = fs + p["width"] * p["height"] + 7          # frame 1, Cb plane
                bad[pos] ^= 0x10
                open(os.path.join(td, "bad1.yuv"), "wb").write(bad)
                expect("one wrong Cb sample", run_checks(ts, os.path.join(td, "bad1.yuv"), "auto", name), False)
                wh, cs = p["width"] * p["height"], len(frames[0]) - p["width"] * p["height"]
                swapped = b"".join(f[:wh] + f[wh + cs // 2:] + f[wh:wh + cs // 2] for f in frames)
                open(os.path.join(td, "bad4.yuv"), "wb").write(swapped)
                expect("Cb and Cr swapped", run_checks(ts, os.path.join(td, "bad4.yuv"), "auto", name), False)
                open(os.path.join(td, "bad2.yuv"), "wb").write(bytes(bad[:-5]))
                expect("truncated file", run_checks(ts, os.path.join(td, "bad2.yuv"), "auto", name), False)
                # uncropped output (16-aligned frames), as a reader that forgot cropping would write
                wm, hm = p["width_mbs"] * 16, p["height_mbs"] * 16
                full = b"\x80" * (yuvref.frame_bytes(wm, hm) * p["frames"])
                open(os.path.join(td, "bad3.yuv"), "wb").write(full)
                expect("uncropped output", run_checks(ts, os.path.join(td, "bad3.yuv"), "auto", name), False)
        # structure-only mode on a real encoder file: gray frames of the right size pass
        from common import testfiles
        t1 = testfiles.get("T1")
        info = yuvref.stream_info(t1)
        gray = os.path.join(td, "gray.yuv")
        open(gray, "wb").write(b"\x80" * (yuvref.frame_bytes(info["width"], info["height"]) * info["frames"]))
        expect("T1 gray frames, structure only", run_checks(t1, gray, "auto", "T1"), True)
        open(gray, "ab").write(b"\x80" * 10)
        expect("T1 with stray bytes", run_checks(t1, gray, "auto", "T1"), False)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("SELF-TEST " + ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ts", nargs="?")
    ap.add_argument("yuv", nargs="?")
    ap.add_argument("--structure-only", action="store_true")
    ap.add_argument("--pixels", action="store_true")
    ap.add_argument("--make-variants", metavar="DIR")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    ffref.require_tools()
    if a.make_variants:
        make_variants(a.make_variants)
        return
    if not (a.ts and a.yuv):
        ap.error("need <in.ts> <reader_output.yuv>")
    if not os.path.isfile(a.ts):
        die(f"input {a.ts!r} not found")
    rep = run_checks(a.ts, a.yuv, "structure" if a.structure_only else "pixels" if a.pixels else "auto",
                     "5.1 YUV writer: " + os.path.basename(a.ts))
    rep.finish()


if __name__ == "__main__":
    main()
