"""Synthetic I_PCM-only H.264 streams for the Chapter 5.1 harness (bit writer: common/bitwriter.py).

Every picture is an IDR picture of I slices whose macroblocks are all I_PCM (mb_type 25), deblocking
off, so the decoded picture IS the raw samples: ffmpeg's output must equal the samples (cropped).
The SPS can carry frame cropping.  A variant is fully described by its parameters (a "manifest"
json next to the stream), the samples are pseudo-random from the seed, so the expected cropped
frames can be rebuilt at any time with expected_frames().
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common.bitwriter import W, nal  # noqa: E402

# name -> (width_mbs, height_mbs, (crop_left, crop_right, crop_top, crop_bottom) in 2-pixel units, frames, slices)
VARIANTS = {
    "pcm_16x16":      (1, 1, (0, 0, 0, 0), 4, 1),
    "pcm_176x144":    (11, 9, (0, 0, 0, 0), 3, 1),
    "pcm_200x120":    (13, 8, (0, 4, 0, 4), 3, 2),       # 208x128 coded, 200x120 shown
    "pcm_198x118":    (13, 8, (0, 5, 0, 5), 3, 3),       # odd chroma size 99x59
    "pcm_100x60_lt":  (7, 4, (2, 4, 2, 0), 3, 2),        # crops on the left and top too
    "pcm_1920x1080":  (120, 68, (0, 0, 0, 4), 2, 4),     # 1920x1088 coded
}


def sps_nal(p):
    w = W()
    w.raw(8, 66)
    w.raw(8, 0)
    w.raw(8, 40)                           # level 4.0
    w.ue(0)
    w.ue(0)                                # log2_max_frame_num_minus4
    w.ue(2)                                # pic_order_cnt_type 2
    w.ue(1)
    w.raw(1, 0)
    w.ue(p["width_mbs"] - 1)
    w.ue(p["height_mbs"] - 1)
    w.raw(1, 1)                            # frame_mbs_only
    w.raw(1, 1)
    crop = p["crop"]
    if any(crop):
        w.raw(1, 1)
        for c in crop:                     # left, right, top, bottom
            w.ue(c)
    else:
        w.raw(1, 0)
    w.raw(1, 0)                            # no VUI
    return nal(0x67, w.rbsp())


def pps_nal():
    w = W()
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)
    w.raw(1, 0)
    w.ue(0)
    w.ue(0)
    w.ue(0)
    w.raw(1, 0)
    w.raw(2, 0)
    w.se(0)
    w.se(0)
    w.se(0)
    w.raw(1, 1)
    w.raw(1, 0)
    w.raw(1, 0)
    return nal(0x68, w.rbsp())


def slice_nal(first, mbs, idr_id):
    """One IDR I slice holding the given I_PCM macroblocks (each 384 bytes: 256 Y, 64 Cb, 64 Cr)."""
    w = W()
    w.ue(first)
    w.ue(7)                                # slice_type I (all slices of the picture)
    w.ue(0)
    w.raw(4, 0)                            # frame_num
    w.ue(idr_id)
    w.raw(1, 0)
    w.raw(1, 0)
    w.se(0)
    w.ue(1)                                # deblocking off
    body = bytearray()
    tail = list(w.bits)
    while len(tail) >= 8:                  # flush whole bytes of the header
        body.append(int("".join(map(str, tail[:8])), 2))
        tail = tail[8:]
    for samples in mbs:
        v = 26                             # ue(25) = 000011010
        tail += [0, 0, 0, 0] + [int(c) for c in format(v, "05b")]
        tail += [0] * (-len(tail) % 8)     # pcm_alignment_zero_bit
        for i in range(0, len(tail), 8):
            body.append(int("".join(map(str, tail[i:i + 8])), 2))
        tail = []
        body += samples
    tail += [1]                            # rbsp_stop_one_bit
    tail += [0] * (-len(tail) % 8)
    for i in range(0, len(tail), 8):
        body.append(int("".join(map(str, tail[i:i + 8])), 2))
    return nal(0x65, bytes(body))


def picture_samples(p, idx):
    rng = random.Random(p["seed"] * 1000 + idx)
    n = p["width_mbs"] * p["height_mbs"]
    return [bytes(rng.randrange(1, 256) for _ in range(384)) for _ in range(n)]


def make_stream(p):
    """p: dict(width_mbs, height_mbs, crop, frames, slices, seed) -> Annex B bytes."""
    data = bytearray()
    n = p["width_mbs"] * p["height_mbs"]
    for idx in range(p["frames"]):
        mbs = picture_samples(p, idx)
        data += sps_nal(p) + pps_nal()
        k = p["slices"]
        for s in range(k):
            a0, a1 = s * n // k, (s + 1) * n // k
            data += slice_nal(a0, mbs[a0:a1], idx % 2)
    return bytes(data)


def expected_frames(p):
    """The cropped yuv420p frames (bytes) that any correct decoder outputs for make_stream(p)."""
    wm, hm = p["width_mbs"], p["height_mbs"]
    W_, H_ = wm * 16, hm * 16
    l, r, t, b = (2 * c for c in p["crop"])
    out = []
    for idx in range(p["frames"]):
        mbs = picture_samples(p, idx)
        y = bytearray(W_ * H_)
        cb = bytearray(W_ * H_ // 4)
        cr = bytearray(W_ * H_ // 4)
        for a, s in enumerate(mbs):
            mx, my = a % wm, a // wm
            for row in range(16):
                o = (my * 16 + row) * W_ + mx * 16
                y[o:o + 16] = s[row * 16:row * 16 + 16]
            for row in range(8):
                o = (my * 8 + row) * (W_ // 2) + mx * 8
                cb[o:o + 8] = s[256 + row * 8:256 + row * 8 + 8]
                cr[o:o + 8] = s[320 + row * 8:320 + row * 8 + 8]
        ow, oh = W_ - l - r, H_ - t - b
        f = bytearray()
        for row in range(t, t + oh):
            f += y[row * W_ + l:row * W_ + l + ow]
        cx0, cy0 = l // 2, t // 2
        cw, ch = (ow + 1) // 2, (oh + 1) // 2
        for plane in (cb, cr):
            for row in range(cy0, cy0 + ch):
                f += plane[row * (W_ // 2) + cx0:row * (W_ // 2) + cx0 + cw]
        out.append(bytes(f))
    return out


def params(name, seed=5):
    wm, hm, crop, frames, slices = VARIANTS[name]
    return dict(name=name, width_mbs=wm, height_mbs=hm, crop=list(crop), frames=frames, slices=slices, seed=seed,
                width=wm * 16 - 2 * (crop[0] + crop[1]), height=hm * 16 - 2 * (crop[2] + crop[3]))


def manifest_path(ts):
    return os.path.splitext(ts)[0] + ".manifest.json"


def write_variant(dirpath, name, seed=5):
    """Write <name>.264, <name>.ts (muxed with ffmpeg) and <name>.manifest.json.  -> path of the .ts"""
    from common import ffref
    p = params(name, seed)
    h264 = os.path.join(dirpath, name + ".264")
    ts = os.path.join(dirpath, name + ".ts")
    with open(h264, "wb") as f:
        f.write(make_stream(p))
    r = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-framerate", "25", "-i", h264,
                   "-c:v", "copy", "-f", "mpegts", ts], timeout=120)
    if r.returncode != 0 or not os.path.exists(ts):
        raise SystemExit("ffmpeg could not mux " + h264 + ": " + r.stderr.decode()[-300:])
    with open(manifest_path(ts), "w") as f:
        json.dump(p, f)
    return ts
