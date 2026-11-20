"""Reference presenter (spoiler: this is the answer to the chapter).  Pure Python 3 standard library, no GUI, no window.

Everything a window-system front end must decide *before* it calls the OS is here:
  Table E-1 sample aspect ratio -> display size -> fit into the window (letterbox) -> nearest scaling ->
  pixel format / stride / origin packing -> pause / step / resize state machine -> the presentation log.
The functions take an optional `bug` string.  It exists only so the harness can build deliberately wrong players
for its self-test; the correct behaviour is bug=None.
"""
import re

SAR_TABLE = {1: (1, 1), 2: (12, 11), 3: (10, 11), 4: (16, 11), 5: (40, 33), 6: (24, 11), 7: (20, 11), 8: (32, 11),
             9: (80, 33), 10: (18, 11), 11: (15, 11), 12: (64, 33), 13: (160, 99), 14: (4, 3), 15: (3, 2), 16: (2, 1)}
FORMATS = {  # name -> (bytes per pixel, memory order of colour bytes, 4th byte: 'x' unused, 'a' alpha, None)
    "bgrx": (4, "bgr", "x"), "bgra": (4, "bgr", "a"), "rgbx": (4, "rgb", "x"), "rgba": (4, "rgb", "a"),
    "rgb24": (3, "rgb", None), "bgr24": (3, "bgr", None)}
EVENTS = ("pause", "resume", "toggle", "step", "resize", "expose", "quit")
MATRICES = {"bt709": (0.2126, 0.0722), "bt601": (0.299, 0.114)}


# ------------------------------------------------------------------------------------------ aspect ratio
def sar_from_idc(idc, ext=(0, 0), bug=None):
    """Table E-1.  0, 17..254 (reserved) and an extended SAR with a zero field mean 'unspecified' -> 1:1."""
    if idc == 255:
        if bug == "ignore_ext_sar":
            return (1, 1)
        return tuple(ext) if ext[0] > 0 and ext[1] > 0 else (1, 1)
    if bug == "table_error" and idc == 14:
        return (3, 4)
    return SAR_TABLE.get(idc, (1, 1))


def display_size(w, h, sar, bug=None):
    """Width scaled by SAR_w / SAR_h, rounded to nearest; height unchanged."""
    sw, sh = sar
    if bug == "inverted_sar":
        sw, sh = sh, sw
    return (2 * w * sw + sh) // (2 * sh), h


def fit_rect(ww, wh, dw, dh, bug=None):
    """Largest rectangle of aspect dw:dh inside the window, centred.  Returns (x, y, w, h)."""
    if bug == "no_aspect":
        return (0, 0, ww, wh)
    if ww * dh <= wh * dw:                       # width limited
        rw, rh = ww, (2 * dh * ww + dw) // (2 * dw)
    else:                                        # height limited
        rw, rh = (2 * dw * wh + dh) // (2 * dh), wh
    rw, rh = max(1, min(rw, ww)), max(1, min(rh, wh))
    if bug == "left_aligned":
        return (0, 0, rw, rh)
    return ((ww - rw) // 2, (wh - rh) // 2, rw, rh)


# ------------------------------------------------------------------------------------------ colour (9.2, nearest)
def convert_nearest(data, base, w, h, matrix, full):
    """One yuv420p frame starting at byte `base` -> top-down RGB24 bytes (the formula of Chapter 9.2)."""
    kr, kb = MATRICES[matrix]
    kg = 1 - kr - kb
    ys, cs, yo = (1.0, 1.0, 0) if full else (255 / 219, 255 / 224, 16)
    cw, ch = (w + 1) // 2, (h + 1) // 2
    pu, pv = base + w * h, base + w * h + cw * ch
    ly = [(v - yo) * ys for v in range(256)]
    lc = [(v - 128) * cs for v in range(256)]
    cr_r, cb_b = 2 * (1 - kr), 2 * (1 - kb)
    out = bytearray(w * h * 3)
    o = 0
    for y in range(h):
        for x in range(w):
            c = (y >> 1) * cw + (x >> 1)
            yn, cbv, crv = ly[data[base + y * w + x]], lc[data[pu + c]], lc[data[pv + c]]
            r = yn + cr_r * crv
            b = yn + cb_b * cbv
            g = (yn - kr * r - kb * b) / kg
            for t in (r, g, b):
                v = int(t + 0.5) if t > -0.5 else 0
                out[o] = 255 if v > 255 else v
                o += 1
    return bytes(out)


# ------------------------------------------------------------------------------------------ scaling
def render(native, sw, sh, ww, wh, rect, bug=None):
    """Nearest-neighbour scale of the native RGB24 picture into `rect`; everything else black.  -> window-size RGB24."""
    rx, ry, rw, rh = rect
    fill = bytes([64, 64, 64]) if bug == "bars_grey" else b"\0\0\0"
    if bug == "nn_shift":
        cmap = [i * sw // rw for i in range(rw)]
        rmap = [i * sh // rh for i in range(rh)]
    else:
        cmap = [(2 * i + 1) * sw // (2 * rw) for i in range(rw)]
        rmap = [(2 * i + 1) * sh // (2 * rh) for i in range(rh)]
    left, right = fill * rx, fill * (ww - rx - rw)
    rows, cache = [], {}
    for y in range(wh):
        if y < ry or y >= ry + rh:
            rows.append(fill * ww)
            continue
        sy = rmap[y - ry]
        if sy not in cache:
            src = native[sy * sw * 3:(sy + 1) * sw * 3]
            cache[sy] = left + b"".join([src[3 * sx:3 * sx + 3] for sx in cmap]) + right
        rows.append(cache[sy])
    return b"".join(rows)


# ------------------------------------------------------------------------------------------ buffer packing
def stride_for(width, fmt, align):
    bpp = FORMATS[fmt][0]
    return (width * bpp + align - 1) // align * align


def pack(rgb, ww, wh, fmt, align, origin, bug=None):
    """RGB24 window image -> (bytes handed to the OS, stride written, stride claimed in the log)."""
    bpp, order, fourth = FORMATS[fmt]
    stride = stride_for(ww, fmt, align)
    claimed = stride
    if bug == "stride_unaligned":
        stride = claimed = ww * bpp
    elif bug == "stride_log_only":
        claimed = ww * bpp
    rows = []
    for y in range(wh):
        row = rgb[y * ww * 3:(y + 1) * ww * 3]
        ch = {"r": row[0::3], "g": row[1::3], "b": row[2::3]}
        buf = bytearray(stride)
        names = order if bug != "bgr_swap" else order[::-1]
        for i, c in enumerate(names):
            buf[i:ww * bpp:bpp] = ch[c]
        if fourth == "a":
            buf[3:ww * bpp:bpp] = bytes([0 if bug == "alpha_zero" else 255]) * ww
        elif fourth == "x":
            buf[3:ww * bpp:bpp] = bytes([0x5A]) * ww      # unused byte: any value is legal
        rows.append(bytes(buf))
    flip = (origin == "bottomup") != (bug == "flip")
    if flip:
        rows.reverse()
    return b"".join(rows), stride, claimed


def unpack(raw, ww, wh, fmt, stride, origin):
    """Inverse of pack (what the OS does with the buffer) -> (RGB24, alpha bytes or None)."""
    bpp, order, fourth = FORMATS[fmt]
    rows = [raw[y * stride:y * stride + ww * bpp] for y in range(wh)]
    if origin == "bottomup":
        rows.reverse()
    out, alpha = [], bytearray()
    for r in rows:
        ch = {c: r[i::bpp] for i, c in enumerate(order)}
        buf = bytearray(ww * 3)
        buf[0::3], buf[1::3], buf[2::3] = ch["r"], ch["g"], ch["b"]
        out.append(bytes(buf))
        if fourth == "a":
            alpha += r[3::bpp]
    return b"".join(out), (bytes(alpha) if fourth == "a" else None)


def write_ppm(path, w, h, rgb):
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        f.write(rgb)


# ------------------------------------------------------------------------------------------ script + state machine
class ScriptError(Exception):
    pass


def parse_script(text):
    """-> list of (line number, K, event, argument)."""
    ev = []
    for ln, line in enumerate(text.splitlines(), 1):
        t = line.split("#")[0].split()
        if not t:
            continue
        if len(t) < 3 or t[0] != "after" or not t[1].isdigit():
            raise ScriptError(f"line {ln}: expected 'after <K> <event> [WxH]', got {line!r}")
        name, arg = t[2], (t[3] if len(t) > 3 else None)
        if name not in EVENTS:
            raise ScriptError(f"line {ln}: unknown event {name!r}")
        if name == "resize":
            if arg is None or not re.fullmatch(r"[1-9]\d*x[1-9]\d*", arg):
                raise ScriptError(f"line {ln}: resize needs WxH")
            arg = tuple(int(v) for v in arg.split("x"))
        elif arg is not None:
            raise ScriptError(f"line {ln}: {name} takes no argument")
        ev.append((ln, int(t[1]), name, arg))
    return ev


def simulate(nframes, events, win0, bug=None):
    """The presenter state machine.  Returns ([(frame, reason, (ww, wh), script line or 0)], end_reason).

    K in 'after K event' = number of new frames shown so far (frames 0..K-1 are on screen).  Redraws do not count."""
    out = []
    state = {"next": 0, "paused": False, "last": None, "win": tuple(win0)}

    def present(frame, reason, line):
        out.append((frame, reason, state["win"], line))

    def redraw(line):
        if bug == "no_redraw":
            return
        if bug == "redraw_advances" and state["next"] < nframes:
            present(state["next"], "redraw", line)
            state["last"] = state["next"]
            state["next"] += 1
        elif state["last"] is not None:
            present(state["last"], "redraw", line)

    def show_new(reason, line):
        n = state["next"]
        if bug == "dup_first" and n == 0 and not out:
            present(0, reason, line)
        present(n, reason, line)
        state["last"] = n
        state["next"] = n + 1

    end = None
    for ln, k, name, arg in events:
        while state["next"] < k:
            if state["paused"] or state["next"] >= nframes:
                if bug:        # a deliberately wrong player may drift away from the script: stay lenient
                    break
                raise ScriptError(f"line {ln}: cannot reach 'after {k}': "
                                  + ("the player is paused" if state["paused"] else f"the stream has only {nframes} frames"))
            show_new("play", ln)
        if state["next"] > k and not bug:
            raise ScriptError(f"line {ln}: 'after {k}' but {state['next']} frames are already shown (events must be in order)")
        if name == "pause":
            state["paused"] = state["paused"] or bug != "pause_ignored"
        elif name == "resume":
            state["paused"] = False
        elif name == "toggle":
            if not (bug == "pause_ignored" and not state["paused"]):
                state["paused"] = not state["paused"]
        elif name == "step":
            if state["paused"] or bug == "step_when_playing":
                if state["next"] < nframes:
                    if bug == "step_skips" and state["next"] + 1 < nframes:
                        state["next"] += 1
                    show_new("step", ln)
                elif bug == "step_end_dup" and state["last"] is not None:
                    present(state["last"], "step", ln)
        elif name == "resize":
            state["win"] = arg
            if bug == "redraw_old_size":
                state["win"] = out[-1][2] if out else arg
                redraw(ln)
                state["win"] = arg
            else:
                redraw(ln)
        elif name == "expose":
            redraw(ln)
        elif name == "quit" and bug != "quit_ignored":
            end = "quit"
            break
    if end is None:
        if state["paused"]:
            end = "script_end"
        else:
            while state["next"] < nframes:
                show_new("play", 0)
            end = "eos"
    return out, end
