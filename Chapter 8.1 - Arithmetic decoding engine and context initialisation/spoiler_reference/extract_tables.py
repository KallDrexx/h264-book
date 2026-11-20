#!/usr/bin/env python3
"""Extract the CABAC constant tables from two independent code bases and cross-check them.

  libavcodec (ffmpeg's H.264 decoder)   cabac_context_init_I / _PB, ff_h264_lps_range, ff_h264_mlps_state
  libx264    (the encoder that made the test streams)   x264_cabac_context_init_I / _PB, x264_cabac_range_lps

Neither library exports these tables as symbols, so they are located by their known leading bytes in the shared
object's data, sliced out with the layout of each library, converted to the standard's layout, and compared entry by
entry.  `python3 extract_tables.py --check` re-verifies tables_data.py against the installed libraries (exit 0 = all
equal, 2 = libraries not found / layout not recognised); `python3 extract_tables.py --write` regenerates tables_data.py.

The standard's own layout is the target: (m, n) per ctxIdx 0..1023 for I slices and for cabac_init_idc 0, 1, 2 (Tables
9-12 .. 9-33), rangeTabLPS[pStateIdx][qCodIRangeIdx] (Table 9-44), transIdxLPS[pStateIdx] (Table 9-45).
transIdxMPS is min(p + 1, 62) for p < 63 and 63 for p = 63 (it is a formula, not a table, and is cross-checked against
ffmpeg's combined state table as well).
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "tables_data.py")
# transIdxLPS typed from the standard (Table 9-45), then compared with both libraries' tables.
TRANS_LPS_TYPED = [0, 0, 1, 2, 2, 4, 4, 5, 6, 7, 8, 9, 9, 11, 11, 12, 13, 13, 15, 15, 16, 16, 18, 18, 19, 19, 21, 21, 22, 22,
                   23, 24, 24, 25, 26, 26, 27, 27, 28, 29, 29, 30, 30, 30, 31, 32, 32, 33, 33, 33, 34, 34, 35, 35, 35, 36,
                   36, 36, 37, 37, 37, 38, 38, 63]
HEAD = [20, -15, 2, 54, 3, 74, 20, -15, 2, 54, 3, 74, -28, 127, -23, 104, -6, 53, -1, 54, 7, 51]   # ctxIdx 0..10, all slice types


def find_lib(prefix):
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib", "/usr/local/lib", "/usr/lib/aarch64-linux-gnu"):
        hits = sorted(glob.glob(os.path.join(d, prefix + ".so*")))
        if hits:
            return hits[0]
    return None


def pairs(blob, off):
    b = blob[off:off + 2048]
    return [(((b[2 * i] + 128) & 255) - 128, ((b[2 * i + 1] + 128) & 255) - 128) for i in range(1024)]


def contexts(blob):
    """The four 1024-entry (m, n) tables stored back to back after the first hit of the ctxIdx 0..10 pattern."""
    pat = bytes(x & 255 for x in HEAD)
    first = blob.find(pat)
    if first < 0 or blob.find(pat, first + 1) != first + 2048:
        raise ValueError("context table pattern not found")
    return [pairs(blob, first + 2048 * k) for k in range(4)]


def classify(tabs):
    """-> (I, [idc0, idc1, idc2]).  The I table is the one without values for ctxIdx 11..59 (all (0, 0));
    idc order is recognised by ctxIdx 11 (Table 9-13): (23, 33), (22, 25), (29, 16)."""
    i_t = [t for t in tabs if all(t[c] == (0, 0) for c in range(11, 60))]
    if len(i_t) != 1:
        raise ValueError("cannot identify the I table")
    pb = {t[11]: t for t in tabs if t is not i_t[0]}
    return i_t[0], [pb[(23, 33)], pb[(22, 25)], pb[(29, 16)]]


def from_x264(blob):
    I, PB = classify(contexts(blob))
    # x264_cabac_range_lps[64][4] is stored with the state numbering reversed (row 0 = pStateIdx 63)
    key = bytes([128, 176, 208, 240])
    i = blob.find(bytes([128, 158, 187, 216, 128, 167, 197, 227, 128, 176, 208, 240]))
    if i < 0:
        raise ValueError("x264 rangeTabLPS not found")
    end = i + 12
    st = [list(blob[end - 256 + 4 * j:end - 256 + 4 * j + 4]) for j in range(64)]
    lps = st[::-1]
    assert lps[0] == list(key)
    return I, PB, lps


def from_ffmpeg(blob):
    I, PB = classify(contexts(blob))
    p = bytes([128, 128, 128, 128, 123, 123])        # ff_h264_lps_range: 4 groups of 128 = 64 states x (mps 0/1) duplicated
    i = blob.find(p)
    if i < 0:
        raise ValueError("ffmpeg lps_range not found")
    base = i - 2
    lps = [[blob[base + 128 * q + 2 * s] for q in range(4)] for s in range(64)]
    for q in range(4):
        for s in range(64):
            assert blob[base + 128 * q + 2 * s] == blob[base + 128 * q + 2 * s + 1]
    # ff_h264_mlps_state: [128 + s] = next state after MPS, [127 - s] = after LPS, s = 2 * pStateIdx + valMPS
    nm = bytes(s + 2 if s < 124 else s for s in range(128))
    j = blob.find(nm)
    if j < 0:
        raise ValueError("ffmpeg mlps_state not found")
    tl = []
    for p_ in range(64):
        v = blob[j - 1 - (2 * p_)]            # LPS successor of (p_, mps 0) -> 2 * p' + (mps ^ (p_ == 0))
        tl.append(v >> 1)
        assert (v & 1) == (1 if p_ == 0 else 0)
    return I, PB, lps, tl


def build():
    av = find_lib("libavcodec")
    x2 = find_lib("libx264")
    if not av or not x2:
        raise ValueError("libavcodec / libx264 shared objects not found")
    a = from_ffmpeg(open(av, "rb").read())
    x = from_x264(open(x2, "rb").read())
    problems = []
    if a[0] != x[0]:
        problems.append("I table differs between libavcodec and libx264")
    if a[1] != x[1]:
        problems.append("P/B tables differ between libavcodec and libx264")
    if a[2] != x[2]:
        problems.append("rangeTabLPS differs between libavcodec and libx264")
    if a[3] != TRANS_LPS_TYPED:
        problems.append("transIdxLPS differs between libavcodec and the typed table")
    return a, problems


def render(a):
    I, PB, lps, tl = a
    out = ['"""GENERATED by extract_tables.py (cross-checked: libavcodec == libx264 for every entry).  Do not edit."""\n']

    def table(name, t):
        out.append(f"{name} = [\n")
        for k in range(0, 1024, 8):
            out.append("    " + " ".join(f"({m},{n})," for m, n in t[k:k + 8]) + "\n")
        out.append("]\n")
    table("CTX_I", I)
    for k in range(3):
        table(f"CTX_PB{k}", PB[k])
    out.append("CTX_PB = [CTX_PB0, CTX_PB1, CTX_PB2]\n")
    out.append("RANGE_LPS = [\n" + "".join(f"    {r},\n" for r in lps) + "]\n")
    out.append(f"TRANS_LPS = {tl}\n")
    return "".join(out)


if __name__ == "__main__":
    try:
        a, problems = build()
    except Exception as e:      # libraries missing or layout not recognised
        print("extract_tables: cannot extract:", e)
        sys.exit(2)
    if problems:
        print("extract_tables: MISMATCH:", "; ".join(problems))
        sys.exit(1)
    if "--write" in sys.argv:
        open(OUT, "w").write(render(a))
        print("wrote", OUT)
    else:
        sys.path.insert(0, HERE)
        import tables_data as T
        same = (T.CTX_I == a[0] and T.CTX_PB == a[1] and T.RANGE_LPS == a[2] and T.TRANS_LPS == a[3])
        print("extract_tables: tables_data.py", "matches" if same else "DIFFERS from", "the installed libraries "
              "(4 x 1024 (m,n) pairs, 64 x 4 rangeTabLPS, 64 transIdxLPS; libavcodec == libx264)")
        sys.exit(0 if same else 1)
