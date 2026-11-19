"""Reference CAVLC residual parsing for I macroblocks (Phase 4 harness truth, parsing LEVEL 3 of cavlc_ref.py).

Stdlib only.  Written for the book's harnesses and kept independent of every chapter's expected reader:

  * SliceResidual keeps the per-slice neighbour bookkeeping: for every macroblock decoded so far in the slice
    the total_coeff of each of its 4x4 blocks (luma: 16, Cb: 4, Cr: 4; I_PCM counts 16 everywhere).
    Availability is "this macroblock was already decoded in THIS slice" (no FMO/ASO), found by looking the
    neighbour address up in that set, not by arithmetic on first_mb.
  * nC (9.2.1) -> coeff_token table (Table 9-5) -> TotalCoeff / TrailingOnes, then the rest of
    residual_block_cavlc (9.2.2 levels, 9.2.3 total_zeros / run_before, Tables 9-7 .. 9-10) so that the NEXT block
    and macroblock start at the right bit.  The full residual is needed here because no other tool reports
    block boundaries; its correctness is tied to a hard external check: a whole slice must end exactly on the
    rbsp_stop_one_bit, and ffmpeg must decode the stream (Chapter 4.3's harness).
  * Every parsed block is kept in rec.blocks (kind, idx, start/end bit, nC, TotalCoeff, TrailingOnes and the
    coefficient levels in scan order) so that Chapter 4.4 can use the same data.

MUTANT: a switch used only by the harness self-tests to run a deliberately wrong parser (a classic bug of the
neighbour / nC rules) and prove the harness notices.  None means the correct rules.
"""
from . import cavlc_tables as T

MUTANT = None

# luma4x4BlkIdx -> (x, y) in units of 4x4 blocks (6.4.3) and back
BLK_XY = [(((i >> 2) & 1) * 2 + (i & 1), ((i >> 3) & 1) * 2 + ((i >> 1) & 1)) for i in range(16)]
XY_BLK = {xy: i for i, xy in enumerate(BLK_XY)}

_DEC = {}


def _dec(table):
    key = id(table)
    if key not in _DEC:
        _DEC[key] = ({c: k for k, c in table.items()}, max(len(c) for c in table.values()))
    return _DEC[key]


def read_vlc(br, table, what):
    """Bit-by-bit prefix code reader (slow and obvious, on purpose)."""
    inv, maxlen = _dec(table)
    code = ""
    for _ in range(maxlen):
        code += str(br.u(1))
        if code in inv:
            return inv[code]
    raise _error(f"{what}: no code word matches {code}")


def _error(msg):
    from .cavlc_ref import ParseError
    return ParseError(msg)


class SliceResidual:
    def __init__(self, width_mbs):
        self.w = width_mbs
        self.tcl = {}          # addr -> list of 16 luma total_coeff, by luma4x4BlkIdx
        self.tcc = {}          # addr -> [Cb list of 4, Cr list of 4], by chroma4x4BlkIdx

    # ------------------------------------------------------------ neighbouring macroblocks (6.4.9 / 6.4.12)
    def mb_a(self, addr):
        return addr - 1 if addr % self.w > 0 and addr - 1 in self.tcl else None

    def mb_b(self, addr):
        return addr - self.w if addr >= self.w and addr - self.w in self.tcl else None

    # ------------------------------------------------------------ nC (9.2.1)
    def _nc(self, na, nb):
        """na / nb: total_coeff of the neighbour block, or None when the neighbour is not available."""
        if MUTANT == "unavail_as_zero":
            na, nb = (0 if na is None else na), (0 if nb is None else nb)
        if MUTANT == "ignore_b":
            nb = None
        if na is not None and nb is not None:
            return (na + nb) >> 1 if MUTANT == "nc_floor" else (na + nb + 1) >> 1
        if na is not None:
            return na
        if nb is not None:
            return nb
        return 0

    def nc_luma(self, addr, blk):
        """nC for luma4x4BlkIdx blk (also Intra16x16 DC with blk = 0 and the Intra16x16 AC blocks)."""
        bx, by = BLK_XY[blk]
        cur = self.tcl[addr]
        if bx > 0:
            na = cur[XY_BLK[(bx - 1, by)]]
        else:
            m = self.mb_a(addr)
            na = None if m is None else self.tcl[m][XY_BLK[(3, by)]]
        if by > 0:
            nb = cur[XY_BLK[(bx, by - 1)]]
        else:
            m = self.mb_b(addr)
            nb = None if m is None else self.tcl[m][XY_BLK[(bx, 3)]]
        return self._nc(na, nb)

    def nc_chroma_ac(self, addr, plane, blk):
        """nC for chroma4x4BlkIdx blk (0..3, raster order in the 2x2 arrangement) of Cb (plane 0) or Cr (1)."""
        bx, by = blk & 1, blk >> 1
        cur = self.tcc[addr][plane]
        if bx > 0:
            na = cur[by * 2 + bx - 1]
        else:
            m = self.mb_a(addr)
            na = None if m is None else self.tcc[m][plane][by * 2 + 1]
        if by > 0:
            nb = cur[(by - 1) * 2 + bx]
        else:
            m = self.mb_b(addr)
            nb = None if m is None else self.tcc[m][plane][2 + bx]
        return self._nc(na, nb)

    # ------------------------------------------------------------ macroblocks
    def pcm(self, addr):
        v = 0 if MUTANT == "pcm0" else 16
        self.tcl[addr] = [v] * 16
        self.tcc[addr] = [[v] * 4, [v] * 4]

    def macroblock(self, br, rec, kind, cbp):
        """residual() of one non-PCM intra macroblock.  Fills rec.f nc / tc / tcl / tccb / tccr and rec.blocks."""
        addr = rec.addr
        luma, chroma = cbp & 15, cbp >> 4
        self.tcl[addr] = [0] * 16
        self.tcc[addr] = [[0] * 4, [0] * 4]
        rec.blocks = []

        def block(kind_, idx, nc, maxnum, store):
            start = br.pos
            tc, t1, coeffs = residual_block(br, nc, maxnum)
            rec.blocks.append(dict(kind=kind_, idx=idx, start=start, end=br.pos, nc=nc, tc=tc, t1=t1, coeffs=coeffs))
            if store is not None:
                store(tc)

        def luma_store(blk):
            def f(tc):
                self.tcl[addr][blk] = tc
            return f

        if kind == "i16":
            def dc_store(tc):
                if MUTANT == "dc_counted":
                    self.tcl[addr][0] = tc
            block("i16dc", 0, self.nc_luma(addr, 0), 16, dc_store)
        if kind == "i16" and luma:
            for blk in range(16):
                block("i16ac", blk, self.nc_luma(addr, blk), 15, luma_store(blk))
        elif kind != "i16":
            for q in range(4):
                if luma >> q & 1:
                    for k in range(4):
                        blk = q * 4 + k
                        block("luma", blk, self.nc_luma(addr, blk), 16, luma_store(blk))
        if chroma:
            for plane, name in enumerate(("cbdc", "crdc")):
                block(name, 0, 0 if MUTANT == "chroma_dc_nc" else -1, 4, None)
        if chroma == 2:
            for plane, name in enumerate(("cbac", "crac")):
                for blk in range(4):
                    def st(tc, plane=plane, blk=blk):
                        self.tcc[addr][plane][blk] = tc
                    block(name, blk, self.nc_chroma_ac(addr, plane, blk), 15, st)
        f = rec.f
        f["nc"] = ",".join(str(b["nc"]) for b in rec.blocks) or "-"
        f["tc"] = ",".join(f"{b['tc']}/{b['t1']}" for b in rec.blocks) or "-"
        self.stored(rec)

    def stored(self, rec):
        a = rec.addr
        rec.f["tcl"] = ",".join(map(str, self.tcl[a]))
        rec.f["tccb"] = ",".join(map(str, self.tcc[a][0]))
        rec.f["tccr"] = ",".join(map(str, self.tcc[a][1]))


# ---------------------------------------------------------------------------------------- residual_block_cavlc
def residual_block(br, nc, maxnum):
    """residual_block_cavlc (7.3.5.3.2, 9.2).  -> (TotalCoeff, TrailingOnes, coefficient list of maxnum levels
    in scan order)."""
    tc, t1 = read_vlc(br, T.coeff_token_table(nc), f"coeff_token (nC={nc})")
    if tc > maxnum:
        raise _error(f"TotalCoeff {tc} larger than maxNumCoeff {maxnum} (nC={nc})")
    coeffs = [0] * maxnum
    if tc == 0:
        return 0, 0, coeffs
    levels = []
    suffix_length = 1 if tc > 10 and t1 < 3 else 0
    for i in range(tc):
        if i < t1:
            levels.append(1 - 2 * br.u(1))
            continue
        prefix = 0
        while br.u(1) == 0:
            prefix += 1
            if prefix > 31:
                raise _error("level_prefix longer than 31")
        code = min(15, prefix) << suffix_length
        if prefix == 14 and suffix_length == 0:
            size = 4
        elif prefix >= 15:
            size = prefix - 3
        else:
            size = suffix_length
        if size:
            code += br.u(size)
        if prefix >= 15 and suffix_length == 0:
            code += 15
        if prefix >= 16:
            code += (1 << (prefix - 3)) - 4096
        if i == t1 and t1 < 3:
            code += 2
        level = (code + 2) >> 1 if code % 2 == 0 else (-code - 1) >> 1
        levels.append(level)
        if suffix_length == 0:
            suffix_length = 1
        if abs(level) > (3 << (suffix_length - 1)) and suffix_length < 6:
            suffix_length += 1
    if tc < maxnum:
        if maxnum == 4:
            total_zeros = read_vlc(br, T.TOTAL_ZEROS_DC[tc], f"total_zeros (chroma DC, TotalCoeff {tc})")
        else:
            total_zeros = read_vlc(br, T.TOTAL_ZEROS[tc], f"total_zeros (TotalCoeff {tc})")
        if total_zeros > maxnum - tc:
            raise _error(f"total_zeros {total_zeros} with TotalCoeff {tc} does not fit maxNumCoeff {maxnum}")
    else:
        total_zeros = 0
    runs, left = [], total_zeros
    for i in range(tc - 1):
        if left > 0:
            r = read_vlc(br, T.RUN_BEFORE[min(left, 7)], f"run_before (zerosLeft {left})")
            if r > left:
                raise _error(f"run_before {r} larger than zerosLeft {left}")
        else:
            r = 0
        runs.append(r)
        left -= r
    runs.append(left)
    pos = -1
    for i in range(tc - 1, -1, -1):             # levels[0] is the highest-frequency coefficient
        pos += runs[i] + 1
        coeffs[pos] = levels[i]
    return tc, t1, coeffs
