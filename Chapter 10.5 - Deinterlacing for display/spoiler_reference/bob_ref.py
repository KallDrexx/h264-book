"""SPOILER: the bob deinterlacer of Chapter 10.5, written straight from the Readme's definition.

bob(frame, w, h, field) -> bytes     frame: one cropped yuv420p frame (Y, then Cb, then Cr; chroma planes are ceil(w/2) x ceil(h/2));
                                     field: "T" (keep the even rows), "B" (keep the odd rows) or "F" (return the frame unchanged).
Rule, applied to EVERY plane separately (rows counted inside that plane):
    a row whose number has the field's parity (T: even, B: odd) is copied;
    every other row y is rebuilt from the nearest kept rows above and below:
        up = y-1 if y-1 >= 0 else y+1          dn = y+1 if y+1 <= rows-1 else y-1       (a single neighbour is used twice)
        out[y][x] = (kept[up][x] + kept[dn][x] + 1) >> 1
    (if the plane has one row only, that row is copied).
MUTANT (harness self-test) switches break exactly one rule; see MUTANTS.
"""
MUTANT = None

MUTANTS = {
    "swap_parity": "T keeps the odd rows and B the even rows",
    "weave": "no interpolation: the source frame is output as it is",
    "replicate_above": "a missing row repeats the kept row above (the bottom-most one repeats upward)",
    "replicate_below": "a missing row repeats the kept row below",
    "floor": "average without the +1 rounding",
    "wrap_edge": "at the picture edge the missing neighbour is taken from the opposite edge (wrap-around)",
    "edge_zero": "a missing row with only one neighbour is filled with 0",
    "edge_keep_source": "a missing row with only one neighbour keeps the source frame's own row",
    "chroma_weave": "chroma planes are left as in the source frame",
    "chroma_parity": "chroma planes use the opposite parity from luma",
    "chroma_replicate": "chroma rows are replicated instead of averaged",
    "frame_bobbed": "an F (whole frame) output is bobbed as a top field",
    "cbcr_swapped": "Cb and Cr planes swapped in the output",
    "smooth_kept": "the kept rows are smoothed too ([1 2 1] / 4 vertical filter)",
    "far_neighbour": "the lower neighbour is taken two rows too far (y+3)",
    "round_half_even": "an odd sum is rounded to the even result instead of up",
}


def planes_of(w, h):
    cw, ch = (w + 1) // 2, (h + 1) // 2
    return [("Y", w, h, 0), ("Cb", cw, ch, w * h), ("Cr", cw, ch, w * h + cw * ch)]


def frame_size(w, h):
    cw, ch = (w + 1) // 2, (h + 1) // 2
    return w * h + 2 * cw * ch


def _avg(a, b):
    m = MUTANT
    if m == "floor":
        return bytes((x + y) >> 1 for x, y in zip(a, b))
    if m == "round_half_even":
        return bytes((lambda q, r: q + (q & 1) if r else q)((x + y) // 2, (x + y) & 1) for x, y in zip(a, b))
    return bytes((x + y + 1) >> 1 for x, y in zip(a, b))


def _bob_plane(rows, parity, luma, src_rows=None):
    n = len(rows)
    m = MUTANT
    if m == "chroma_parity" and not luma:
        parity ^= 1
    out = []
    for y in range(n):
        if y % 2 == parity:
            if m == "smooth_kept":
                a = rows[y - 1] if y > 0 else rows[y]
                b = rows[y + 1] if y + 1 < n else rows[y]
                out.append(bytes((p + 2 * q + r + 2) >> 2 for p, q, r in zip(a, rows[y], b)))
            else:
                out.append(rows[y])
            continue
        up_ok, dn_ok = y - 1 >= 0, y + 1 <= n - 1
        up = y - 1 if up_ok else y + 1
        dn = y + 1 if dn_ok else y - 1
        if n == 1:
            out.append(rows[0])
            continue
        if m == "replicate_above" or (m == "chroma_replicate" and not luma):
            out.append(rows[up if up_ok else dn])
            continue
        if m == "replicate_below":
            out.append(rows[dn if dn_ok else up])
            continue
        if m == "far_neighbour" and dn_ok:
            dn = y + 3 if y + 3 <= n - 1 else dn
        if m == "wrap_edge":
            if not up_ok:
                up = n - 1 if (n - 1) % 2 == parity else n - 2
            if not dn_ok:
                dn = parity if parity < n else 1
        if m == "edge_zero" and not (up_ok and dn_ok):
            out.append(bytes(len(rows[0])))
            continue
        if m == "edge_keep_source" and not (up_ok and dn_ok):
            out.append(src_rows[y])
            continue
        out.append(_avg(rows[up], rows[dn]))
    return out


def bob(frame, w, h, field):
    m = MUTANT
    if field == "F" and m != "frame_bobbed":
        return bytes(frame)
    if field == "F":
        field = "T"
    if m == "weave":
        return bytes(frame)
    parity = 0 if field == "T" else 1
    if m == "swap_parity":
        parity ^= 1
    out = bytearray()
    planes = planes_of(w, h)
    pieces = []
    for name, pw, ph, off in planes:
        rows = [frame[off + y * pw: off + (y + 1) * pw] for y in range(ph)]
        if m == "chroma_weave" and name != "Y":
            res = rows
        else:
            res = _bob_plane(rows, parity, name == "Y", rows)
        pieces.append(res)
    if m == "cbcr_swapped":
        pieces[1], pieces[2] = pieces[2], pieces[1]
    for res in pieces:
        for r in res:
            out += r
    return bytes(out)
