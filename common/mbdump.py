"""Shared pieces of the Phase 4 harnesses (Chapters 4.1 - 4.4): the macroblock-dump parser, the ffmpeg
macroblock-class cross-check of the reference, and the decode/PCM-pixel check of synthetic streams.

Dump grammar: Chapter 4.1's Readme, section 5 (later chapters add keys to the MB line).
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from common import cavlc_ref as ref, ffref  # noqa: E402

KEYS_SLICE = ["type", "first_mb", "pic_size_in_mbs", "header_end_bit"]
KEYS_PCM = ["align_bits", "sum_y", "sum_cb", "sum_cr"]
KEYS_END = ["mbs", "status"]
STATUSES = ("complete", "stopped", "skipped", "error")


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ================================================================ reader output parsing
def parse_kv(tokens, lineno, line):
    d = {}
    for t in tokens:
        if "=" not in t:
            die(f"malformed reader output, line {lineno}: expected key=value, found {t!r}: {line!r}")
        k, v = t.split("=", 1)
        d[k] = v
    return d


def parse_reader(path):
    """-> (slices, hist).  slices: ordered list of dict(index, slice=kv, mbs=[(addr, kv)], pcm={addr: kv}, end=kv)."""
    try:
        text = open(path, "r").read()
    except OSError as e:
        die(f"cannot read reader output {path}: {e}")
    slices, cur, hist, seen_hist = [], None, None, False
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        kind = tok[0]
        if seen_hist:
            die(f"malformed reader output, line {no}: nothing may follow the HIST line")

        def need_int(s, what):
            if not re.fullmatch(r"\d+", s):
                die(f"malformed reader output, line {no}: {what} must be a non-negative integer, found {s!r}: {line!r}")
            return int(s)
        if kind == "SLICE":
            if len(tok) < 2:
                die(f"malformed reader output, line {no}: SLICE needs an index: {line!r}")
            cur = dict(index=need_int(tok[1], "slice index"), slice=parse_kv(tok[2:], no, line),
                       mbs=[], pcm={}, end=None, line=no)
            slices.append(cur)
        elif kind in ("MB", "PCM", "END"):
            if cur is None or cur["end"] is not None:
                die(f"malformed reader output, line {no}: {kind} line outside a SLICE ... END block: {line!r}")
            if len(tok) < 2 or need_int(tok[1], "slice index") != cur["index"]:
                die(f"malformed reader output, line {no}: slice index missing or not {cur['index']} inside the "
                    f"block of slice {cur['index']}: {line!r}")
            if kind == "END":
                cur["end"] = parse_kv(tok[2:], no, line)
            else:
                if len(tok) < 3:
                    die(f"malformed reader output, line {no}: {kind} needs a macroblock address: {line!r}")
                addr = need_int(tok[2], "macroblock address")
                kv = parse_kv(tok[3:], no, line)
                if kind == "MB":
                    cur["mbs"].append((addr, kv))
                else:
                    cur["pcm"][addr] = kv
        elif kind == "HIST":
            hist = {}
            for t in tok[1:]:
                m = re.fullmatch(r"(\d+):(\d+)", t)
                if not m:
                    die(f"malformed reader output, line {no}: HIST entries look like 3:17, found {t!r}")
                hist[int(m.group(1))] = int(m.group(2))
            seen_hist = True
        else:
            die(f"malformed reader output, line {no}: unknown line type {kind!r} (allowed: SLICE MB PCM END HIST, "
                f"or # comments)")
    if cur is not None and cur["end"] is None:
        die(f"malformed reader output: the block of slice {cur['index']} has no END line")
    if not seen_hist:
        die("malformed reader output: no HIST line at the end")
    return slices, hist


def hist_diff(a, b):
    """Only the mb_type values whose counts differ, e.g. '7: 3 vs 2, 30: 0 vs 1'."""
    keys = sorted(k for k in set(a) | set(b) if a.get(k, 0) != b.get(k, 0))
    return ", ".join(f"{k}: {a.get(k, 0)} vs {b.get(k, 0)}" for k in keys[:10]) + (" ..." if len(keys) > 10 else "")


# ================================================================ checking the reference against ffmpeg
def pic_type_letter(info):
    return {0: "P", 1: "B", 2: "I"}.get(info.slice_type, "?")


def match_pictures(infos, grid, what):
    """Pair decode-order pictures with ffmpeg's output-order maps.  -> (usable picture set or None, note)."""
    npics = infos[-1].picture + 1
    first_type = {}
    for i in infos:
        first_type.setdefault(i.picture, pic_type_letter(i))
    types_dec = "".join(first_type[p] for p in range(npics))
    types_out = "".join(g["type"] for g in grid)
    if types_dec == types_out:
        return set(range(npics)), ""
    if types_dec[:1] == "I" and types_out[:1] == "I":
        return {0}, " (pictures are reordered: only the first picture could be matched safely)"
    return None, f"{what} skipped: could not match ffmpeg's pictures to slices"


def check_reference_vs_ffmpeg(path, infos, results, td):
    """Macroblock class (I4x4 / I16x16 / PCM) of every macroblock the reference printed vs ffmpeg's
    `-debug mb_type` map.  -> (list of problems, summary text)."""
    npics = infos[-1].picture + 1 if infos else 0
    w0 = infos[0].pic_width_in_mbs
    same_size = all(i.pic_width_in_mbs == w0 and i.pic_size_in_mbs == infos[0].pic_size_in_mbs for i in infos)
    grid = ref.ffmpeg_mb_grid(path, td, dims=(w0, infos[0].pic_size_in_mbs // w0) if same_size else None,
                              count=npics)
    if grid is None:
        return [], "ffmpeg cross-check skipped: ffmpeg's log was garbled on every try"
    if len(grid) < npics:
        return [], f"ffmpeg cross-check skipped: ffmpeg showed {len(grid)} pictures, stream has {npics}"
    grid = grid[-npics:]
    usable, note = match_pictures(infos, grid, "ffmpeg cross-check")
    if usable is None:
        return [], note
    problems, n = [], 0
    for r in results:
        p = r.info.picture
        if p not in usable:
            continue
        rows = grid[p]["rows"]
        w = r.info.pic_width_in_mbs
        for rec in r.mbs:
            y, x = divmod(rec.addr, w)
            if y >= len(rows) or x >= len(rows[y]):
                problems.append(f"slice {r.info.index} addr {rec.addr}: ffmpeg's macroblock map has no such macroblock")
                continue
            ch = rows[y][x]
            cls = ref.CLASS_OF_CHAR.get(ch, f"inter or other class {ch!r}")
            mine = rec.f["name"]
            mine_cls = "I_16x16" if mine.startswith("I_16x16") else mine
            n += 1
            if cls != mine_cls:
                problems.append(f"slice {r.info.index} addr {rec.addr}: reference says {mine}, ffmpeg says {cls}")
    return problems, f"ffmpeg cross-check: {n} macroblock classes in {len(usable)} picture(s) agree{note}"


# ================================================================ synthetic streams
def manifest_path(path):
    base = path[:-4] if path.lower().endswith(".264") else path
    return base + ".manifest.json"


def decode_synth(path, manifest, td):
    """Decode a synthetic stream with ffmpeg.  -> (problems, list of frames as bytes).  Any ffmpeg error
    message, or a wrong frame count, is a problem."""
    yuv = os.path.join(td, "synth.yuv")
    p = ffref.run(["ffmpeg", "-v", "error", "-y", "-f", "h264", "-i", path, "-fps_mode", "passthrough",
                   "-f", "rawvideo", "-pix_fmt", "yuv420p", yuv], timeout=120)
    err = p.stderr.decode(errors="replace").strip()
    if err or p.returncode != 0:
        return ["ffmpeg reports errors decoding the synthetic stream: " + err[:300]], []
    w, h = manifest["width_mbs"] * 16, manifest["height_mbs"] * 16
    fs = w * h * 3 // 2
    data = open(yuv, "rb").read()
    n = len(manifest["pictures"])
    if len(data) != fs * n:
        return [f"ffmpeg produced {len(data) // fs} frames, expected {n}"], []
    return [], [data[i * fs:(i + 1) * fs] for i in range(n)]


def mb_pixels(frame, width_mbs, height_mbs, addr):
    """The 384 decoded samples of macroblock addr (16x16 Y, 8x8 Cb, 8x8 Cr) in I_PCM sample order."""
    w, h = width_mbs * 16, height_mbs * 16
    my, mx = divmod(addr, width_mbs)
    got = bytearray()
    for yy in range(16):
        o = (my * 16 + yy) * w + mx * 16
        got += frame[o:o + 16]
    for plane in range(2):
        base = w * h + plane * (w // 2) * (h // 2)
        for yy in range(8):
            o = base + (my * 8 + yy) * (w // 2) + mx * 8
            got += frame[o:o + 8]
    return bytes(got)


def pcm_pixel_problems(manifest, frames):
    """Every I_PCM macroblock of the manifest must decode to exactly its raw samples."""
    problems = []
    for pi, pic in enumerate(manifest["pictures"]):
        for s in pic:
            for e in s["mbs"]:
                if e["kind"] == "pcm" and \
                        mb_pixels(frames[pi], manifest["width_mbs"], manifest["height_mbs"], e["addr"]) != \
                        bytes.fromhex(e["samples"]):
                    problems.append(f"picture {pi} addr {e['addr']}: ffmpeg's decoded pixels differ from the I_PCM samples")
    return problems
