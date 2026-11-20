"""Reference-YUV helpers for Phase 5+ (the YUV counterpart of ffref.py).

CROPPING.  ffmpeg's decoded output is CROPPED according to the SPS (frame_cropping offsets), so
ffprobe's width/height and the frames of `reference_yuv` are the cropped size, not 16 x macroblocks.
The readers of Phase 5+ must write the same: the cropped frame, planar yuv420p, no padding.  For the
intra-only files of Phase 5 (T1, T2, T4) decode order = output order, so the reader writes each
frame as soon as it is complete; output reordering only arrives in Phase 9.
"""
import os

from . import ffref


def frame_bytes(width, height):
    """Size of one 8-bit 4:2:0 frame; chroma planes are (w+1)//2 x (h+1)//2."""
    return width * height + 2 * ((width + 1) // 2) * ((height + 1) // 2)


def cropped_size(width_mbs, height_mbs_in_map_units, frame_mbs_only, crop_left, crop_right, crop_top, crop_bottom):
    """7.4.2.1.1 for 4:2:0 (CropUnitX = 2, CropUnitY = 2 * (2 - frame_mbs_only_flag)).
    -> (width, height, x0, y0): the output size and the position of its top-left sample in the
    macroblock-aligned decoded frame."""
    fh = (2 - frame_mbs_only) * height_mbs_in_map_units
    cux, cuy = 2, 2 * (2 - frame_mbs_only)
    return (16 * width_mbs - cux * (crop_left + crop_right), 16 * fh - cuy * (crop_top + crop_bottom),
            cux * crop_left, cuy * crop_top)


def stream_info(ts):
    """-> dict(width, height, frames) of the first video stream as ffmpeg outputs it (cropped size,
    frame count from a full decode)."""
    j = ffref.probe_json(ts, "-select_streams", "v:0", "-show_entries", "stream=width,height")
    st = (j.get("streams") or [{}])[0]
    return dict(width=int(st.get("width", 0)), height=int(st.get("height", 0)), frames=len(ffref.probe_frames(ts, "pts")))


def reference_yuv(ts, out_yuv):
    """ffref.reference_yuv plus a check that something was written; returns the byte size."""
    ffref.reference_yuv(ts, out_yuv)
    if not os.path.exists(out_yuv):
        raise SystemExit(f"error: ffmpeg could not decode {ts}")
    return os.path.getsize(out_yuv)
