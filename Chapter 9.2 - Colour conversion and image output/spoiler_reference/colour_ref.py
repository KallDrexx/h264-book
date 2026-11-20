"""Reference YCbCr 4:2:0 -> RGB conversion (spoiler: this is the answer to the chapter).

Pure Python 3 standard library.  Exact definition (all arithmetic in IEEE double, rounding = floor(x + 0.5)):

    matrix BT.709: Kr = 0.2126, Kb = 0.0722      BT.601: Kr = 0.299, Kb = 0.114      Kg = 1 - Kr - Kb
    limited range: Yn = (Y - 16) * 255 / 219,  Cn = (C - 128) * 255 / 224
    full range   : Yn = Y,                     Cn = C - 128
    R = Yn + 2 (1 - Kr) Cr_n
    B = Yn + 2 (1 - Kb) Cb_n
    G = (Yn - Kr R - Kb B) / Kg
    each channel: clamp(floor(v + 0.5), 0, 255)

Chroma upsampling: "nearest" uses chroma sample (x // 2, y // 2).  "bilinear" places chroma sample (cx, cy) at
luma position (2cx + 0.5, 2cy + 0.5) ("centre" siting) and interpolates linearly in both directions (weights
1/4 and 3/4), clamping at the picture edges.  "bilinear_left" is the H.264 default siting (chroma_sample_loc_type 0):
horizontally co-sited (2cx), vertically centred.
"""
MATRICES = {  # name -> (Kr, Kb); matrix_coefficients value -> name below
    "bt709": (0.2126, 0.0722), "bt601": (0.299, 0.114), "fcc": (0.30, 0.11),
    "smpte240m": (0.212, 0.087), "bt2020": (0.2627, 0.0593),
}
MC_NAMES = {1: "bt709", 4: "fcc", 5: "bt601", 6: "bt601", 7: "smpte240m", 9: "bt2020", 10: "bt2020"}


def matrix_for(mc, height):
    """matrix_coefficients (None / 2 = unspecified, 0 = identity, 3 = reserved) + coded height -> matrix name.

    The rule used by this book: unspecified -> BT.601 when height <= 576, else BT.709."""
    if mc in MC_NAMES:
        return MC_NAMES[mc]
    return "bt601" if height <= 576 else "bt709"


def make_luts(matrix, full, trunc=False):
    kr, kb = MATRICES[matrix]
    ys = 1.0 if full else 255.0 / 219
    cs = 1.0 if full else 255.0 / 224
    yo = 0 if full else 16
    ly = [(y - yo) * ys for y in range(256)]
    lc = [(c - 128) * cs for c in range(256)]
    return kr, kb, ly, lc


def planes(data, w, h, frame):
    cw, ch = (w + 1) // 2, (h + 1) // 2
    fs = w * h + 2 * cw * ch
    base = frame * fs
    return (data[base:base + w * h], data[base + w * h:base + w * h + cw * ch],
            data[base + w * h + cw * ch:base + fs], cw, ch)


def _chroma_at(pl, cw, ch, x, y, mode, dx, dy):
    """Chroma value (float) for luma position (x, y)."""
    if mode == "nearest":
        cx = min(max(x // 2 + dx, 0), cw - 1)
        cy = min(max(y // 2 + dy, 0), ch - 1)
        return pl[cy * cw + cx]
    # bilinear: u = chroma coordinate of luma pixel x
    ux = (x - 0.5) / 2 if mode == "bilinear" else x / 2.0
    uy = (y - 0.5) / 2
    ux += dx
    uy += dy
    x0 = int(ux // 1)
    y0 = int(uy // 1)
    fx, fy = ux - x0, uy - y0
    def g(cx, cy):
        return pl[min(max(cy, 0), ch - 1) * cw + min(max(cx, 0), cw - 1)]
    top = g(x0, y0) * (1 - fx) + g(x0 + 1, y0) * fx
    bot = g(x0, y0 + 1) * (1 - fx) + g(x0 + 1, y0 + 1) * fx
    return top * (1 - fy) + bot * fy


def convert(Y, U, V, w, h, matrix="bt709", full=False, upsample="nearest", window=None,
            swap_uv=False, dx=0, dy=0, trunc=False, clamp=True, order="rgb", yscale_bug=False):
    """Convert one frame (plane bytes) -> bytes of packed RGB for `window` = (x0, y0, ww, hh) (default: whole frame).
    The keyword extras (swap_uv, dx, dy, trunc, clamp, order) exist so the harness can build deliberately wrong
    interpretations for diagnosis."""
    cw, ch = (w + 1) // 2, (h + 1) // 2
    x0, y0, ww, hh = window or (0, 0, w, h)
    kr, kb, ly, lc = make_luts(matrix, full)
    kg = 1 - kr - kb
    if swap_uv:
        U, V = V, U
    out = bytearray()
    cr_r, cb_b = 2 * (1 - kr), 2 * (1 - kb)
    nearest = upsample == "nearest"
    for y in range(y0, y0 + hh):
        for x in range(x0, x0 + ww):
            yn = ly[Y[y * w + x]]
            if nearest:
                cx = min(max(x // 2 + dx, 0), cw - 1)
                cy = min(max(y // 2 + dy, 0), ch - 1)
                cbv, crv = lc[U[cy * cw + cx]], lc[V[cy * cw + cx]]
            else:
                s = 1.0 if full else 255.0 / 224
                cbv = (_chroma_at(U, cw, ch, x, y, upsample, dx, dy) - 128) * s
                crv = (_chroma_at(V, cw, ch, x, y, upsample, dx, dy) - 128) * s
            r = yn + cr_r * crv
            b = yn + cb_b * cbv
            g = (yn - kr * r - kb * b) / kg
            px = []
            for t in (r, g, b):
                v = int(t // 1) if trunc else int((t + 0.5) // 1)
                if clamp:
                    v = 0 if v < 0 else 255 if v > 255 else v
                else:
                    v &= 255
                px.append(v)
            if order == "bgr":
                px.reverse()
            out += bytes(px)
    return bytes(out)


def write_ppm(path, w, h, rgb):
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        f.write(rgb)
