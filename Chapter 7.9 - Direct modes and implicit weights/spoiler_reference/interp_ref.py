"""SPOILER: reference fractional-sample interpolation (8.4.2.2.1 luma, 8.4.2.2.2 chroma) for Chapter 7.4.
Do not read before your own decoder passes.

Planes are lists of rows (plane[y][x]).  luma_block / chroma_block return a block (list of rows) of the prediction for one
prediction block; the reference picture is read through coordinate clamping (8-228 / 8-229): xInt = Clip3(0, W - 1, x).

MUTANT switches on ONE deliberately wrong rule (used by the harness self-test); None = the correct rules.
"""
MUTANT = None

TAPS = (1, -5, 20, 20, -5, 1)


def clip1(v):
    return 0 if v < 0 else 255 if v > 255 else v


def _window(plane, W, H, x0, y0, ww, wh):
    """ww x wh samples with top-left (x0, y0), coordinates clamped into the picture."""
    hi_x, hi_y = W - 1, H - 1
    if MUTANT == "clamp_off_by_one":
        hi_x, hi_y = W - 2, H - 2
    out = []
    for yy in range(y0, y0 + wh):
        if MUTANT == "no_clamp":
            row = plane[yy] if 0 <= yy < H else None
            out.append([(row[xx] if row is not None and 0 <= xx < W else 0) for xx in range(x0, x0 + ww)])
            continue
        row = plane[0 if yy < 0 else hi_y if yy > hi_y else yy]
        out.append([row[0 if xx < 0 else hi_x if xx > hi_x else xx] for xx in range(x0, x0 + ww)])
    return out


def _filt(a, i, step):
    t = TAPS
    if MUTANT == "taps":
        t = (2, -6, 20, 20, -6, 2)
    elif MUTANT == "taps_sign":
        t = (1, -5, 20, 20, -5, -1)
    return (t[0] * a(i - 2 * step) + t[1] * a(i - step) + t[2] * a(i) + t[3] * a(i + step)
            + t[4] * a(i + 2 * step) + t[5] * a(i + 3 * step))


def luma_block(plane, W, H, x, y, w, h, mvx, mvy):
    """Predicted w x h luma block whose top-left sample is (x, y) of the current picture, vector in quarter samples."""
    xi, yi = x + (mvx >> 2), y + (mvy >> 2)
    xf, yf = mvx & 3, mvy & 3
    if MUTANT == "mv_int_round":
        xi, yi = x + ((mvx + 2) >> 2), y + ((mvy + 2) >> 2)
    win = _window(plane, W, H, xi - 2, yi - 2, w + 5, h + 5)       # win[r][c] = sample (xi - 2 + c, yi - 2 + r)
    G = lambda c, r: win[r + 2][c + 2]                              # noqa: E731  sample (xi + c, yi + r)
    if xf == 0 and yf == 0:
        return [[G(c, r) for c in range(w)] for r in range(h)]

    # b1[r][c]: horizontal intermediate between G(c, r) and G(c + 1, r) for window rows r = -2 .. h + 2
    b1 = {}
    if xf:
        for r in range(-2, h + 3):
            row = win[r + 2]
            b1[r] = [row[c] - 5 * row[c + 1] + 20 * row[c + 2] + 20 * row[c + 3] - 5 * row[c + 4] + row[c + 5]
                     if MUTANT is None else _filt(lambda k: row[k + 2], c, 1) for c in range(w)]
    # h1[r][c]: vertical intermediate between G(c, r) and G(c, r + 1) for columns c = 0 .. w
    h1 = {}
    if yf:
        for c in range(0, w + 1):
            col = [win[r][c + 2] for r in range(h + 5)]
            h1[c] = [_filt(lambda k: col[k + 2], r, 1) for r in range(h)]

    rb = 15 if MUTANT == "b_round" else 16

    def Bv(c, r):                                                    # b at (c, r): 5-tap-rounded horizontal half sample
        return clip1((b1[r][c] + rb) >> 5)

    def Hv(c, r):                                                    # h at (c, r)
        return clip1((h1[c][r] + rb) >> 5)

    def J(c, r):
        if MUTANT == "j_from_rounded":
            v = _filt(lambda k: Bv(c, k), r, 1)
            return clip1((v + 16) >> 5)
        if MUTANT == "j_from_clipped_b1":
            v = _filt(lambda k: max(-4000, min(4000, b1[k][c])), r, 1)
            return clip1((v + 512) >> 10)
        v = _filt(lambda k: b1[k][c], r, 1)
        if MUTANT == "j_round":
            return clip1((v + 256) >> 10)
        if MUTANT == "j_shift":
            return clip1((v + 16) >> 5)
        return clip1((v + 512) >> 10)

    def avg(p, q):
        return (p + q) >> 1 if MUTANT == "avg_round_down" else (p + q + 1) >> 1

    out = []
    for r in range(h):
        line = []
        for c in range(w):
            if (xf, yf) == (0, 1):
                v = avg(G(c, r), Hv(c, r))                           # d
            elif (xf, yf) == (0, 2):
                v = Hv(c, r)                                         # h
            elif (xf, yf) == (0, 3):
                v = avg(G(c, r + 1), Hv(c, r))                       # n
            elif (xf, yf) == (1, 0):
                v = avg(G(c, r), Bv(c, r))                           # a
            elif (xf, yf) == (1, 1):
                v = avg(Bv(c, r), Hv(c, r))                          # e
            elif (xf, yf) == (1, 2):
                v = avg(Hv(c, r), J(c, r))                           # i
            elif (xf, yf) == (1, 3):
                v = avg(Hv(c, r), Bv(c, r + 1))                      # p  (h, s)
            elif (xf, yf) == (2, 0):
                v = Bv(c, r)                                         # b
            elif (xf, yf) == (2, 1):
                v = avg(Bv(c, r), J(c, r))                           # f
            elif (xf, yf) == (2, 2):
                v = J(c, r)                                          # j
            elif (xf, yf) == (2, 3):
                v = avg(J(c, r), Bv(c, r + 1))                       # q  (j, s)
            elif (xf, yf) == (3, 0):
                v = avg(G(c + 1, r), Bv(c, r))                       # c
            elif (xf, yf) == (3, 1):
                if MUTANT == "diag_pair":
                    v = avg(Bv(c, r), Hv(c, r))                      # wrong: g uses (b, h) like e
                else:
                    v = avg(Bv(c, r), Hv(c + 1, r))                  # g  (b, m)
            elif (xf, yf) == (3, 2):
                v = avg(J(c, r), Hv(c + 1, r))                       # k  (j, m)
            else:
                if MUTANT == "diag_pair_r":
                    v = avg(Hv(c, r), Bv(c, r + 1))                  # wrong: r uses (h, s) like p
                else:
                    v = avg(Hv(c + 1, r), Bv(c, r + 1))              # r  (m, s)
            line.append(v)
        out.append(line)
    return out


def chroma_block(plane, W, H, x, y, w, h, mvx, mvy):
    """Predicted w x h chroma block, (x, y) = top-left chroma sample (W, H = chroma plane size).  4:2:0: the vector of the
    luma block is used unchanged, in units of 1/8 chroma sample."""
    if MUTANT == "chroma_mv_units":
        xi, yi, xf, yf = x + (mvx >> 2), y + (mvy >> 2), (mvx & 3) << 1, (mvy & 3) << 1
    else:
        xi, yi, xf, yf = x + (mvx >> 3), y + (mvy >> 3), mvx & 7, mvy & 7
    win = _window(plane, W, H, xi, yi, w + 1, h + 1)
    if MUTANT == "chroma_swap_frac":
        xf, yf = yf, xf
    wa, wb, wc, wd = (8 - xf) * (8 - yf), xf * (8 - yf), (8 - xf) * yf, xf * yf
    if MUTANT == "chroma_swap_bc":
        wb, wc = wc, wb
    rnd = 0 if MUTANT == "chroma_round" else 32
    out = []
    for r in range(h):
        a, b = win[r], win[r + 1]
        out.append([(wa * a[c] + wb * a[c + 1] + wc * b[c] + wd * b[c + 1] + rnd) >> 6 for c in range(w)])
    return out
