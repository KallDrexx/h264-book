"""CAVLC code tables of H.264 (clause 9.2) as bit strings.  Stdlib only, data only.

Table 9-5 (coeff_token), Tables 9-7 / 9-8 (total_zeros 4x4), 9-9 (total_zeros chroma DC 2x2), 9-10 (run_before).
Shared by the Phase 4 reference parser (cavlc_resid.py) and the synthetic stream generators.

PROVENANCE.  Typed as bit strings (the Summary's tip: typos here are the most common Phase 4 bug).  The entries were
transcribed from an independent implementation (the tables inside libavcodec) and then checked three ways:
  * every table is prefix-free and, where the standard says so, complete (check_tables(), run by the harnesses);
  * check_against_libavcodec() compares them with the tables found in the installed libavcodec (best effort);
  * ffmpeg decodes synthetic streams that use EVERY entry (Chapter 4.3 harness): a wrong entry desynchronises it.
"""

# Table 9-5, column 0 <= nC < 2: rows TotalCoeff, columns TrailingOnes 0..3
_CT0 = """
 0  1                 -                 -                 -
 1  000101            01                -                 -
 2  00000111          000100            001               -
 3  000000111         00000110          0000101           00011
 4  0000000111        000000110         00000101          000011
 5  00000000111       0000000110        000000101         0000100
 6  0000000001111     00000000110       0000000101        00000100
 7  0000000001011     0000000001110     00000000101       000000100
 8  0000000001000     0000000001010     0000000001101     0000000100
 9  00000000001111    00000000001110    0000000001001     00000000100
10  00000000001011    00000000001010    00000000001101    0000000001100
11  000000000001111   000000000001110   00000000001001    00000000001100
12  000000000001011   000000000001010   000000000001101   00000000001000
13  0000000000001111  000000000000001   000000000001001   000000000001100
14  0000000000001011  0000000000001110  0000000000001101  000000000001000
15  0000000000000111  0000000000001010  0000000000001001  0000000000001100
16  0000000000000100  0000000000000110  0000000000000101  0000000000001000
"""

# Table 9-5, column 2 <= nC < 4: rows TotalCoeff, columns TrailingOnes 0..3
_CT1 = """
 0  11                -                 -                 -
 1  001011            10                -                 -
 2  000111            00111             011               -
 3  0000111           001010            001001            0101
 4  00000111          000110            000101            0100
 5  00000100          0000110           0000101           00110
 6  000000111         00000110          00000101          001000
 7  00000001111       000000110         000000101         000100
 8  00000001011       00000001110       00000001101       0000100
 9  000000001111      00000001010       00000001001       000000100
10  000000001011      000000001110      000000001101      00000001100
11  000000001000      000000001010      000000001001      00000001000
12  0000000001111     0000000001110     0000000001101     000000001100
13  0000000001011     0000000001010     0000000001001     0000000001100
14  0000000000111     00000000001011    0000000000110     0000000001000
15  00000000001001    00000000001000    00000000001010    0000000000001
16  00000000000111    00000000000110    00000000000101    00000000000100
"""

# Table 9-5, column 4 <= nC < 8: rows TotalCoeff, columns TrailingOnes 0..3
_CT2 = """
 0  1111              -                 -                 -
 1  001111            1110              -                 -
 2  001011            01111             1101              -
 3  001000            01100             01110             1100
 4  0001111           01010             01011             1011
 5  0001011           01000             01001             1010
 6  0001001           001110            001101            1001
 7  0001000           001010            001001            1000
 8  00001111          0001110           0001101           01101
 9  00001011          00001110          0001010           001100
10  000001111         00001010          00001101          0001100
11  000001011         000001110         00001001          00001100
12  000001000         000001010         000001101         00001000
13  0000001101        000000111         000001001         000001100
14  0000001001        0000001100        0000001011        0000001010
15  0000000101        0000001000        0000000111        0000000110
16  0000000001        0000000100        0000000011        0000000010
"""

# Table 9-5, column 8 <= nC (6-bit fixed length): rows TotalCoeff, columns TrailingOnes 0..3
_CT3 = """
 0  000011            -                 -                 -
 1  000000            000001            -                 -
 2  000100            000101            000110            -
 3  001000            001001            001010            001011
 4  001100            001101            001110            001111
 5  010000            010001            010010            010011
 6  010100            010101            010110            010111
 7  011000            011001            011010            011011
 8  011100            011101            011110            011111
 9  100000            100001            100010            100011
10  100100            100101            100110            100111
11  101000            101001            101010            101011
12  101100            101101            101110            101111
13  110000            110001            110010            110011
14  110100            110101            110110            110111
15  111000            111001            111010            111011
16  111100            111101            111110            111111
"""

# Table 9-5, column nC = -1 (chroma DC 2x2): rows TotalCoeff, columns TrailingOnes 0..3
_CTDC = """
 0  01                -                 -                 -
 1  000111            1                 -                 -
 2  000100            000110            001               -
 3  000011            0000011           0000010           000101
 4  000010            00000011          00000010          0000000
"""

# Tables 9-7 / 9-8: total_zeros for 4x4 blocks: rows tzVlcIndex = TotalCoeff 1..15, columns total_zeros 0..15
_TZ = """
 1  1                 011               010               0011              0010              00011             00010             000011            000010            0000011           0000010           00000011          00000010          000000011         000000010         000000001
 2  111               110               101               100               011               0101              0100              0011              0010              00011             00010             000011            000010            000001            000000            -
 3  0101              111               110               101               0100              0011              100               011               0010              00011             00010             000001            00001             000000            -                 -
 4  00011             111               0101              0100              110               101               100               0011              011               0010              00010             00001             00000             -                 -                 -
 5  0101              0100              0011              111               110               101               100               011               0010              00001             0001              00000             -                 -                 -                 -
 6  000001            00001             111               110               101               100               011               010               0001              001               000000            -                 -                 -                 -                 -
 7  000001            00001             101               100               011               11                010               0001              001               000000            -                 -                 -                 -                 -                 -
 8  000001            0001              00001             011               11                10                010               001               000000            -                 -                 -                 -                 -                 -                 -
 9  000001            000000            0001              11                10                001               01                00001             -                 -                 -                 -                 -                 -                 -                 -
10  00001             00000             001               11                10                01                0001              -                 -                 -                 -                 -                 -                 -                 -                 -
11  0000              0001              001               010               1                 011               -                 -                 -                 -                 -                 -                 -                 -                 -                 -
12  0000              0001              01                1                 001               -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
13  000               001               1                 01                -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
14  00                01                1                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
15  0                 1                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
"""

# Table 9-9: total_zeros for chroma DC 2x2: rows tzVlcIndex = TotalCoeff 1..3, columns total_zeros 0..3
_TZDC = """
 1  1                 01                001               000
 2  1                 01                00                -
 3  1                 0                 -                 -
"""

# Table 9-10: run_before: rows zerosLeft 1..6 and 7 (meaning >6), columns run_before 0..14
_RB = """
 1  1                 0                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
 2  1                 01                00                -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
 3  11                10                01                00                -                 -                 -                 -                 -                 -                 -                 -                 -                 -                 -
 4  11                10                01                001               000               -                 -                 -                 -                 -                 -                 -                 -                 -                 -
 5  11                10                011               010               001               000               -                 -                 -                 -                 -                 -                 -                 -                 -
 6  11                000               001               011               010               101               100               -                 -                 -                 -                 -                 -                 -                 -
 7  111               110               101               100               011               010               001               0001              00001             000001            0000001           00000001          000000001         0000000001        00000000001
"""
# ---------------------------------------------------------------------------------------------------- parsing
def _rows(text):
    out = {}
    for line in text.strip().splitlines():
        f = line.split()
        out[int(f[0])] = [None if x == "-" else x for x in f[1:]]
    return out


def _flat(text, ncols):
    """-> dict (row, col) -> code string"""
    d = {}
    for r, cells in _rows(text).items():
        for c, code in enumerate(cells):
            if code is not None:
                d[(r, c)] = code
    return d


# COEFF_TOKEN[table] : dict (TotalCoeff, TrailingOnes) -> bit string.  table 0..3 = nC ranges 0-1, 2-3, 4-7, 8+;
# COEFF_TOKEN_DC = chroma DC (nC = -1).
COEFF_TOKEN = [_flat(t, 4) for t in (_CT0, _CT1, _CT2, _CT3)]
COEFF_TOKEN_DC = _flat(_CTDC, 4)
TOTAL_ZEROS = {tc: {z: c for (r, z), c in _flat(_TZ, 16).items() if r == tc} for tc in range(1, 16)}
TOTAL_ZEROS_DC = {tc: {z: c for (r, z), c in _flat(_TZDC, 4).items() if r == tc} for tc in range(1, 4)}
RUN_BEFORE = {zl: {z: c for (r, z), c in _flat(_RB, 15).items() if r == zl} for zl in range(1, 8)}


def coeff_token_table(nc):
    """nC -> (table dict).  0..1 / 2..3 / 4..7 / >= 8 / -1 (Table 9-5, 9.2.1)."""
    if nc == -1:
        return COEFF_TOKEN_DC
    if nc < 2:
        return COEFF_TOKEN[0]
    return COEFF_TOKEN[1] if nc < 4 else COEFF_TOKEN[2] if nc < 8 else COEFF_TOKEN[3]


def _decoder(table):
    """bit-string table (key -> code) -> dict code -> key, with max length (for a bit-by-bit reader)."""
    return {c: k for k, c in table.items()}


def check_tables():
    """-> list of problems.  Prefix-freeness of every table, Kraft sums (complete codes have sum 1), the
    entry counts, and the structural facts of Table 9-5."""
    problems = []

    def prefix_free(name, table):
        codes = sorted(table.values())
        if len(set(codes)) != len(codes):
            problems.append(f"{name}: duplicate code")
        for a in codes:
            for b in codes:
                if a != b and b.startswith(a):
                    problems.append(f"{name}: {a} is a prefix of {b}")

    def kraft(table):
        return sum(2.0 ** -len(c) for c in table.values())
    expect_entries = [62, 62, 62, 62]            # TotalCoeff 0..16 x TrailingOnes 0..min(3, TotalCoeff): 1+2+3+4*14 = 62
    for i, t in enumerate(COEFF_TOKEN):
        prefix_free(f"coeff_token table {i}", t)
        if len(t) != expect_entries[i]:
            problems.append(f"coeff_token table {i}: {len(t)} entries, expected 62")
        for tc in range(17):
            for t1 in range(min(3, tc) + 1):
                if (tc, t1) not in t:
                    problems.append(f"coeff_token table {i}: missing TotalCoeff {tc} TrailingOnes {t1}")
    prefix_free("coeff_token chroma DC", COEFF_TOKEN_DC)
    if len(COEFF_TOKEN_DC) != 14:
        problems.append(f"chroma DC coeff_token: {len(COEFF_TOKEN_DC)} entries, expected 14")
    if any(len(c) != 6 for c in COEFF_TOKEN[3].values()) or len(set(COEFF_TOKEN[3].values())) != 62:
        problems.append("coeff_token table 3 must be 62 distinct 6-bit codes")
    # Kraft sums of the variable-length tables.  The standard's Table 9-5 leaves a few codes unused (sum < 1);
    # the values below are what the transcribed tables give and act as a regression check on typos.
    for i, want in enumerate([0.999969482421875, 0.9998779296875, 0.9990234375]):
        if abs(kraft(COEFF_TOKEN[i]) - want) > 1e-12:
            problems.append(f"coeff_token table {i}: Kraft sum {kraft(COEFF_TOKEN[i])}, expected {want}")
    if abs(kraft(COEFF_TOKEN_DC) - 1.0) > 1e-12:
        problems.append(f"chroma DC coeff_token Kraft sum {kraft(COEFF_TOKEN_DC)}")
    for tc, t in TOTAL_ZEROS.items():
        prefix_free(f"total_zeros tzVlcIndex {tc}", t)
        if len(t) != 17 - tc:
            problems.append(f"total_zeros tzVlcIndex {tc}: {len(t)} entries, expected {17 - tc}")
        if abs(kraft(t) - 1.0) > 1e-12 and tc != 1:     # tzVlcIndex 1 leaves one code unused
            problems.append(f"total_zeros tzVlcIndex {tc}: Kraft sum {kraft(t)} (complete code expected)")
    for tc, t in TOTAL_ZEROS_DC.items():
        prefix_free(f"chroma DC total_zeros {tc}", t)
        if len(t) != 5 - tc or abs(kraft(t) - 1.0) > 1e-12:
            problems.append(f"chroma DC total_zeros {tc}: entries {len(t)} / Kraft {kraft(t)}")
    for zl, t in RUN_BEFORE.items():
        prefix_free(f"run_before zerosLeft {zl}", t)
        if len(t) != (zl + 1 if zl < 7 else 15):
            problems.append(f"run_before zerosLeft {zl}: {len(t)} entries")
        if zl < 7 and abs(kraft(t) - 1.0) > 1e-12:
            problems.append(f"run_before zerosLeft {zl}: Kraft sum {kraft(t)}")
    return problems


def check_against_libavcodec():
    """Best effort: find the tables of the installed libavcodec (they live as plain byte arrays; the anchors
    are the known first rows) and compare.  -> (problems, text).  Silent skip when the layout is not found."""
    import glob
    import subprocess
    try:
        out = subprocess.run(["ldconfig", "-p"], stdin=subprocess.DEVNULL, capture_output=True, timeout=20).stdout.decode()
        paths = sorted({l.split("=>")[1].strip() for l in out.splitlines() if "libavcodec.so" in l and "=>" in l})
    except Exception:
        paths = []
    paths += glob.glob("/usr/lib/x86_64-linux-gnu/libavcodec.so.*") + glob.glob("/usr/lib/libavcodec.so.*")
    for p in paths:
        try:
            d = open(p, "rb").read()
        except OSError:
            continue
        a = d.find(bytes([1, 0, 0, 0, 6, 2, 0, 0, 8, 6, 3, 0, 9, 8, 7, 5, 10, 9, 8, 6]))        # coeff_token_len[0]
        b = d.find(bytes([1, 0, 0, 0, 5, 1, 0, 0, 7, 4, 1, 0]))                                 # coeff_token_bits[0]
        if a < 0 or b < 0:
            continue
        problems, n = [], 0
        for t in range(4):
            for tc in range(17):
                for t1 in range(4):
                    ln, bits = d[a + t * 68 + tc * 4 + t1], d[b + t * 68 + tc * 4 + t1]
                    mine = COEFF_TOKEN[t].get((tc, t1))
                    theirs = format(bits, "0%db" % ln) if ln else None
                    n += 1
                    if mine != theirs:
                        problems.append(f"coeff_token table {t} TotalCoeff {tc} TrailingOnes {t1}: ours {mine}, libavcodec {theirs}")
        cl = d.find(bytes([2, 0, 0, 0, 6, 1, 0, 0, 6, 6, 3, 0, 6, 7, 7, 6, 6, 8, 8, 7]))
        if cl >= 32:
            for tc in range(5):
                for t1 in range(4):
                    ln, bits = d[cl + tc * 4 + t1], d[cl - 32 + tc * 4 + t1]
                    mine = COEFF_TOKEN_DC.get((tc, t1))
                    theirs = format(bits, "0%db" % ln) if ln else None
                    n += 1
                    if mine != theirs:
                        problems.append(f"chroma DC coeff_token TotalCoeff {tc} TrailingOnes {t1}: ours {mine}, libavcodec {theirs}")
        return problems, f"Table 9-5 compared with {p}: {n} entries"
    return [], "libavcodec tables not located: comparison skipped"
