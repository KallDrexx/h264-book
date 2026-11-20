# Chapter 8.1 - Arithmetic decoding engine and context initialisation

## 1. Goal and the project

Most real-world H.264 files use CABAC (context-adaptive binary arithmetic coding) instead of the CAVLC you built in Phase 4. In this chapter you build the two lowest layers of a CABAC decoder and nothing else:

1. the **context initialisation**: the table of probability states every slice starts from (clause 9.3.1.1);
2. the **arithmetic decoding engine**: the machine that turns bytes into binary decisions (9.3.1.2 and 9.3.3.2).

You also hook the engine to the start of a slice and decode the first macroblock's first few bins. Everything above that (what the bins mean for each syntax element) is Chapters 8.2 to 8.4. Phases 5 to 7 (reconstruction, deblocking, inter prediction) are reused unchanged later; CABAC only replaces entropy decoding.

## 2. Big picture

CABAC reads a bitstream as one long binary fraction. Each syntax element is first turned into a string of **bins** (0/1 decisions, the "binarisation", Chapter 8.2). Each bin is then decoded against a **context**: a small adaptive model that remembers "bin value 0 or 1 has been more likely lately, with this confidence". The decoder keeps an interval (`codIRange`) and a position inside it (`codIOffset`); a likely bin barely shrinks the interval (almost no bits spent), an unlikely one shrinks it a lot. Whenever the interval gets too small it is doubled and a new bit is read in ("renormalisation").

Two reasons this chapter exists on its own: (a) the engine is tiny but unforgiving, a single wrong comparison corrupts every later bin of the slice; (b) the 1024 initial (m, n) pairs are pure data that has to be typed in once and verified. Nothing in CABAC can be tested one syntax element at a time until both are exactly right.

## 3. Concepts in detail

### 3.1 Where the slice data starts

For a slice NAL unit (nal_unit_type 1 or 5), after removing emulation prevention bytes (Phase 2), the bits are: NAL header (1 byte), slice header, then **slice data**. You already parse the slice header (Phase 3, Chapter 7.1 added P/B fields). Four things matter here:

| Field | Where | Use |
|---|---|---|
| `entropy_coding_mode_flag` | PPS | 1 = CABAC for every slice using this PPS |
| `slice_type` (take it modulo 5: 0 P, 1 B, 2 I, 3 SP) | slice header | picks the initialisation table (3.5) |
| `cabac_init_idc` | slice header, `ue(v)`, **only if** entropy_coding_mode_flag = 1 and the slice is not I/SI; it comes after `dec_ref_pic_marking` and before `slice_qp_delta`; range 0..2 | picks one of three P/B tables |
| `SliceQPY` = 26 + `pic_init_qp_minus26` (PPS) + `slice_qp_delta` | PPS + slice header | scales the initial states |

When the PPS selects CABAC, the slice header is followed by `cabac_alignment_one_bit`: 1-bits until the position is a multiple of 8 (counted from the first bit of the NAL header). CABAC data therefore always starts on a **byte boundary**. Your slice-data reader must skip those bits (if the header already ends on a byte boundary there are none).

### 3.2 Context variables

A context has two numbers: `pStateIdx` (0..62 for normal use; the engine's tables also have an entry 63, reserved for the terminate bin and never reached by a normal context) and `valMPS` (the "most probable symbol", 0 or 1). Larger `pStateIdx` means a more skewed context: state 0 is about 50/50, state 62 is about 98 percent. Contexts are numbered by `ctxIdx` 0..1023; each syntax element owns a range (3.5). The context of one bin is `ctxIdx` = `ctxIdxOffset` + `ctxIdxInc`: the offset is the start of the element's range, the increment is chosen per bin, often from the neighbouring macroblocks (9.3.3.1).

### 3.3 The engine

State: `codIRange` (9 bits, always 256..510 between bins) and `codIOffset` (the 9 most recent bits, always less than `codIRange`).

**Initialisation (9.3.1.2).** `codIRange` = 510; `codIOffset` = the next 9 bits of the stream, most significant bit first.

**DecodeDecision(context)**, one bin (9.3.3.2.1):

1. `qCodIRangeIdx` = bits 7..6 of `codIRange`, i.e. `(codIRange >> 6) & 3` (0..3).
2. `codIRangeLPS` = `rangeTabLPS[pStateIdx][qCodIRangeIdx]` (Table 9-44: 64 rows by 4 columns).
3. `codIRange` = `codIRange` - `codIRangeLPS`.
4. If `codIOffset >= codIRange`: the **less probable** symbol occurred. `binVal` = 1 - `valMPS`; `codIOffset` -= `codIRange`; `codIRange` = `codIRangeLPS`; if `pStateIdx` was 0, `valMPS` = 1 - `valMPS`; `pStateIdx` = `transIdxLPS[pStateIdx]` (Table 9-45).
5. Otherwise: `binVal` = `valMPS`; `pStateIdx` = `transIdxMPS[pStateIdx]` = `min(pStateIdx + 1, 62)`.
6. **RenormD**: while `codIRange < 256`: `codIRange` doubles, `codIOffset` = `(codIOffset << 1) | next bit`.

**DecodeBypass** (9.3.3.2.3), a bin with fixed 50/50 probability, no context, no range change: `codIOffset` = `(codIOffset << 1) | next bit`; if `codIOffset >= codIRange`, `binVal` = 1 and `codIOffset` -= `codIRange`, else `binVal` = 0. (Used for coefficient signs and escape suffixes, Chapter 8.3.)

**DecodeTerminate** (9.3.3.2.2), a bin that is almost always 0 and marks "end of slice" or "this macroblock is I_PCM": `codIRange` -= 2. If `codIOffset >= codIRange`: `binVal` = 1, **no renormalisation**, decoding of this arithmetic code is over. Otherwise `binVal` = 0 and RenormD runs. There is no context variable (the standard reserves ctxIdx 276 for it).

**Bit position after a terminating 1.** Count the bits the engine has taken from the stream (9 at initialisation, plus one per shifted-in bit). With this counting, after `binVal` = 1 the last bit taken is the final 1 written by the encoder's flush. At the end of a slice that bit *is* the `rbsp_stop_one_bit`, so the slice ends exactly there (the rest of the byte is zero padding). For `mb_type` I_PCM the next syntax elements are `pcm_alignment_zero_bit` (zeros up to the next byte boundary), then the raw PCM samples, and after the samples the engine is **initialised again** (range 510, read 9 bits) at the following bit. Context states are *not* reset by this.

### 3.4 Table 9-44 and 9-45 anchors

You will copy these tables from the standard (or any trustworthy copy); check them with these anchor values. `rangeTabLPS`: row 0 = 128, 176, 208, 240; row 1 = 128, 167, 197, 227; row 12 = 77, 94, 111, 128; row 62 = 6, 7, 8, 9; row 63 = 2, 2, 2, 2. `transIdxLPS` (64 entries): 0, 0, 1, 2, 2, 4, 4, 5, 6, 7, ... ends 37, 37, 37, 38, 38, 63. Note `transIdxMPS` is a formula, not a table.

### 3.5 Context initialisation (9.3.1.1)

At the start of every slice **all** contexts are initialised from a pair (m, n) from Tables 9-12 to 9-33. `Clip3(lo, hi, x)` clamps x to lo..hi. There are four complete tables of 1024 (m, n) pairs: one for I slices (and SI), and one for each `cabac_init_idc` 0, 1, 2 used by P, SP and B slices. For one context:

1. `preCtxState` = `Clip3(1, 126, ((m * Clip3(0, 51, SliceQPY)) >> 4) + n)`.
2. If `preCtxState <= 63`: `pStateIdx` = 63 - `preCtxState`, `valMPS` = 0.
3. Otherwise: `pStateIdx` = `preCtxState` - 64, `valMPS` = 1.

`>>` is an arithmetic shift, which rounds towards minus infinity for negative products (-728 >> 4 is -46, not -45). `Clip3(0, 51, SliceQPY)` matters only when `SliceQPY` is negative, which happens only with bit depths above 8 (outside this book's scope; the harness still tests it).

Worked examples at SliceQPY = 26:

| ctxIdx | table | (m, n) | (m * 26) >> 4 | preCtxState | result |
|---|---|---|---|---|---|
| 3 (mb_type, I slice) | I | (20, -15) | 32 | 17 | pStateIdx 46, valMPS 0 |
| 6 (mb_type, I slice) | I | (-28, 127) | -46 | 81 | pStateIdx 17, valMPS 1 |
| 11 (mb_skip_flag, P) | idc 0 | (23, 33) | 37 | 70 | pStateIdx 6, valMPS 1 |
| 11 | idc 1 | (22, 25) | 35 | 60 | pStateIdx 3, valMPS 0 |

At QP 0 the pair (20, -15) gives preCtxState 0, clipped to 1: pStateIdx 62, valMPS 0.

**Which contexts exist.** ctxIdx 276 has no (m, n) (it is the terminate bin). ctxIdx 11..59 (P/B-only syntax elements) have no value in the I table. All other ctxIdx 0..1023 are defined in all four tables. ctxIdx 460..1023 are only used by 4:4:4 streams (outside this book's scope), but they belong to the same tables, so type them too. Orientation for 8.2 to 8.4 (4:2:0, Table 9-34):

| ctxIdx | syntax element |
|---|---|
| 0-10 | mb_type (SI prefix 0-2, I 3-10) |
| 11-13, 14-20, 21-23 | P: mb_skip_flag, mb_type (prefix 14-17, intra suffix 17-20), sub_mb_type |
| 24-26, 27-35, 36-39 | B: mb_skip_flag, mb_type (prefix 27-35, intra suffix 32-35), sub_mb_type |
| 40-46, 47-53, 54-59 | mvd (x, y), ref_idx |
| 60-63, 64-67, 68-69 | mb_qp_delta, intra_chroma_pred_mode, prev/rem intra pred mode |
| 70-72, 73-84, 85-104 | mb_field_decoding_flag, coded_block_pattern, coded_block_flag |
| 105-275 | significant / last / coeff_abs_level (frame blocks), 276 = terminate |
| 277-398, 399-401 | significant / last (field blocks), transform_size_8x8_flag |
| 402-459 | 8x8 residual contexts |

**Data tip.** The tables are four blocks of 1024 pairs; copy them as data from the standard rather than computing anything, and **count**: 1024 pairs per table, ctxIdx 0..10 identical in all four tables. First rows for orientation: ctxIdx 0..10 are (20,-15) (2,54) (3,74) (20,-15) (2,54) (3,74) (-28,127) (-23,104) (-6,53) (-1,54) (7,51); ctxIdx 11 is (23,33), (22,25), (29,16) for idc 0, 1, 2; ctxIdx 1023 is (-30,127) in the I table and (-23,126), (-31,127), (-30,127) for idc 0, 1, 2.

### 3.6 Worked engine example

Data bytes `F4 80 5A 00`, one context with pStateIdx 46, valMPS 0 (ctxIdx 3 at QP 26 above).

* Init: range 510, offset = first 9 bits `111101001` = 489.
* Decision 1: qIdx = (510 >> 6) & 3 = 3; rangeTabLPS[46][3] = 22; range = 488. Offset 489 >= 488, so LPS: bin = 1, offset = 1, range = 22. valMPS stays 0 (state was not 0); pStateIdx = transIdxLPS[46] = 32. Renorm: 22 -> 44 -> 88 -> 176 -> 352, four shifts, reading bits `0000`: offset = 16. Bits read so far: 13.
* Decision 2 (same context, now state 32): qIdx = (352 >> 6) & 3 = 1; rangeTabLPS[32][1] = 33; range = 319; offset 16 < 319: MPS, bin = 0; pStateIdx = 33. No renorm.
* Bypass: offset = (16 << 1) | next bit (0) = 32; 32 < 319, bin = 0. Bits read: 14.
* Terminate: range = 317; offset 32 < 317, bin = 0, no renorm (317 >= 256).

## 4. The project: exactly what to build

1. **Context initialisation**: from (slice type, `cabac_init_idc`, `SliceQPY`) produce the 1024 context variables (skip the undefined ones).
2. **Engine**: initialise at a given byte position; DecodeDecision (with both tables), RenormD, DecodeBypass, DecodeTerminate, and the "restart after I_PCM" step.
3. **Slice start**: for each CABAC slice, find the first byte of slice data (3.1), initialise contexts and engine, then decode the **first macroblock's** leading bins (5.3). The first macroblock of a slice has no available neighbours (they are in other slices), so every `ctxIdxInc` that depends on neighbours is 0; no neighbour logic is needed yet.
4. **Verify** (the Summary's check): the first macroblock's `mb_type` equals what the real decoder sees, and `end_of_slice_flag` after a skipped or I_PCM first macroblock is correct. The harness checks this against ffmpeg's own macroblock map and against synthetic slices whose content is known by construction.

First-macroblock bins you need (clause 9.3.2.5, Table 9-36, ctxIdxInc of 9.3.3.1.2; all offsets with neighbour term 0):

| Slice | Bins | ctxIdx |
|---|---|---|
| I, `mb_type` | b0 | 3. b0 = 0 gives I_NxN (mb_type 0) |
| | b1 | terminate bin. 1 gives I_PCM (mb_type 25) |
| | b2 = luma flag (coded_block_pattern luma is 15), b3 = chroma not zero | 6, 7 |
| | if b3 = 1: b4 (chroma is 2, else 1), then b5, b6 (prediction mode, two bits) | 8, then 9, 10 |
| | if b3 = 0: b4, b5 (prediction mode, two bits) | 9, 10 |
| P, `mb_skip_flag` | one bin | 11 |
| P, `mb_type` prefix | b0 (1 = intra macroblock, the I mb_type follows as a suffix) | 14 |
| P, intra suffix | like the I rows, with the shifted contexts | b0 17; b1 terminate; b2 18; b3 19; b4 19 if b3 = 1 else 20; b5, b6 20 |
| B, `mb_skip_flag` | one bin | 24 |
| B, `mb_type` | b0 (0 = B_Direct_16x16) | 27 |

For an Intra16x16 macroblock the value is `mb_type` = 1 + prediction mode (0..3, the two mode bins, first one most significant) + 4 * chroma (0, 1, 2) + 12 * luma flag. Value 0 is I_NxN and 25 is I_PCM. In a P slice an intra `mbt` is reported as the I value (the standard numbers it 5 higher). Macroblocks other than I_PCM are not read beyond these bins in this chapter.

After a skipped first macroblock, the next element is `end_of_slice_flag`, a terminate bin. After I_PCM, skip the alignment bits and the sample bytes (384 bytes for 8-bit 4:2:0, in general `(256 * BitDepthY + 2 * MbWidthC * MbHeightC * BitDepthC) / 8`), restart the engine, then read `end_of_slice_flag`.

## 5. Harness input and output formats

Everything is plain text, one record per line, decimal numbers, `#` starts a comment (in your output files too), blank lines are ignored. Write all files yourself with your own program; the harness never calls your code. `python3 test_cabac_init.py --make-variants DIR` writes the input files `ctx_cases.txt`, `engine_vectors.txt`, synthetic streams `syn_pcm_skip_*.264` (each with a `.json` file of expected results that the harness reads; keep it next to the `.264`) and seven x264 CABAC streams `x264_*.ts` (different QPs, B pictures, 4 and 8 slices per picture) into `DIR`.

### 5.1 Context dump (`--ctx`)

Input `ctx_cases.txt`, one case per line: `slice_type cabac_init_idc SliceQPY`, e.g.

```
I - 26
P 1 40
B 2 -6
```

`slice_type` is `I`, `P` or `B` (SP counts as P); `-` stands for the idc of an I slice; QP runs from -12 to 51 (negative QPs exercise the clipping). 96 cases. Your output, per case in order:

```
CASE 0 I - -12
0 62 0
1 9 0
...
```

A `CASE <k> <type> <idc> <qp>` line (only `k`, the 0-based line number of the case, is used), then one line per context `ctxIdx pStateIdx valMPS`, ctxIdx ascending, **all of 0..1023 except 276** (for I cases also except 11..59). Extra lines for ctxIdx the standard leaves undefined for that case are ignored. Only the contexts the standard defines are compared.

### 5.2 Engine vectors (`--engine`)

Input `engine_vectors.txt`, a list of vectors:

```
VECTOR states_0          # name
INIT P 1 40              # optional: initialise all contexts as in 5.1 (type, idc, qp)
CTX 4 17 1               # optional, repeated: context ctxIdx 4 starts at pStateIdx 17, valMPS 1 (overrides INIT)
DATA 8bd398 ...          # arithmetic code bytes in hex, one or more lines, concatenated; byte 0 = first byte
D 4                      # DecodeDecision with context 4
B                        # DecodeBypass
T                        # DecodeTerminate
P 384                    # after a terminate bin of 1: alignment bits, skip 384 bytes, restart the engine
END
```

The ops (`D`, `B`, `T`, `P`) must be executed in order against one engine started at bit 0 of DATA with the stated contexts (bits beyond DATA read as 0). The vectors cover every (pStateIdx, valMPS), runs that walk the state machine to both ends, long bypass runs, terminate bins, I_PCM-style restarts, and contexts initialised from slice parameters. Your output per vector:

```
VECTOR states_0
D 4 1                    # D <ctxIdx> <bin>
B 0                      # B <bin>
T 0 19                   # T <bin> <bits taken from DATA so far, incl. the 9 of the initialisation>
P 389                    # P <byte offset in DATA where the restarted arithmetic code begins>
STATE 4 31 1             # after all ops, for each ctxIdx used by a D op, ascending: STATE <ctxIdx> <pStateIdx> <valMPS>
END
```

Notes: for a `T` with bin 1 the bit count is the position after the terminating bin as described in 3.3 (no renormalisation). The `P n` offset is `(bits taken rounded up to a multiple of 8) / 8 + n`. The last op of every vector is a `T` with bin 1.

### 5.3 Stream output (`input.ts` / `.264` plus your output file)

Input: any H.264 stream (`.ts` or Annex B `.264`) with CABAC slices. MBAFF streams are refused (the first macroblock of a pair has extra syntax); CAVLC slices in the stream are ignored. Output, **one line per CABAC slice NAL unit, in stream order**, and no other lines (comments aside). Fields come in exactly this order:

```
SLICE <index> <type> idc=<0|1|2|-> qp=<SliceQPY> off=<codIOffset after init> <fields>
```

* `index`: 0-based ordinal of the slice NAL unit among all slice NAL units (nal_unit_type 1 or 5) of the stream.
* `type`: `I`, `P` or `B`; `idc` is `-` for I slices.
* `off`: `codIOffset` right after initialisation (the first 9 bits of slice data).
* I slice: `mbt=<0..25>`, plus `eos=<0|1>` (the end_of_slice_flag) if `mbt=25`.
* P slice: `skip=<0|1>`; if 1, `eos=<bin>`. If `skip=0`: `b0=<bin>`, and if `b0=1`: `mbt=<0..25>` (the intra suffix as an I value), plus `eos=<bin>` if `mbt=25`.
* B slice: `skip=<0|1>`; if 1, `eos=<bin>`; if 0, `b0=<bin>`.

Examples: `SLICE 0 I idc=- qp=26 off=310 mbt=0`, `SLICE 3 P idc=1 qp=30 off=445 skip=1 eos=0`, `SLICE 7 B idc=2 qp=12 off=88 skip=0 b0=1`, `SLICE 9 P idc=2 qp=5 off=201 skip=0 b0=1 mbt=25 eos=1`.

## 6. Running the harness

```
python3 test_cabac_init.py --ctx ctx_cases.txt my_ctx.txt
python3 test_cabac_init.py --engine engine_vectors.txt my_engine.txt
python3 test_cabac_init.py syn_pcm_skip_1.264 my_stream.txt       # or any CABAC .ts
```

The three can be combined in one call. `--print-expected ctx|engine|stream INPUT OUT` writes the expected output so you can `diff` it. Exit 0 = pass, 1 = your output differs (first ~10 differences are listed with case / vector / slice, field, expected, got), 2 = unusable input or output.

What it compares against: there is no JM here. An independent reference engine and a separately written arithmetic **encoder** produce the vectors and the synthetic streams. The constant tables were cut out of both libavcodec and libx264 and compared entry by entry, and the harness cross-checks its stream expectations with ffmpeg (first macroblock class of every slice from ffmpeg's macroblock map; the synthetic streams are decoded by ffmpeg and their I_PCM samples must match). `--self-test` verifies the harness, including 28 deliberately wrong decoder rules that must each be rejected.

**Spoiler warning:** `spoiler_reference/` holds the harness's complete reference decoder, encoder and tables. Don't open it before your own implementation passes; copy the tables from the standard yourself.

The x264 streams all use `cabac_init_idc` 0 (x264 does not choose another). The synthetic streams are the only ones with idc 1 and 2, and are built from valid slices (one macroblock per slice, I_PCM, P_Skip and B_Skip), so both ffmpeg and your decoder can handle them. The x264 file `T10_full` from the common test files works too (`python3 -m common.testfiles T10_full`).

## 7. Reading failures

* **`off` differs in every slice**: you start at the wrong byte. Remember `cabac_alignment_one_bit` and that the offset is counted in the unescaped NAL (header byte included). Or your 9-bit initial read is off by a bit.
* **`off` right, `mbt`/`skip` wrong**: the context for that bin (check the ctxIdx in section 4), or a context from the wrong table (I versus idc 0), or the initialisation.
* **Context dump wrong only for negative QP**: you are missing `Clip3(0, 51, ...)`. **Wrong for many contexts at one QP**: check the shift of a negative product, and the `<= 63` boundary.
* **Engine vector fails at the first `D` line**: tables 9-44 or the initial range/offset. **Bins right but `STATE` wrong**: the state transition (flip of `valMPS` only at state 0 on an LPS; MPS saturates at 62). **A long vector goes wrong only late**: renormalisation (loop, not a single shift) or one wrong table cell: the vectors visit every state.
* **`T` bit count wrong**: for a terminating 1 the engine does not renormalise; for a 0 it does.
* **After `P` everything fails**: alignment to the byte boundary, the sample size, or you reset the context states when restarting (you must not).

## 8. What's next

Chapter 8.2 builds on this engine: the binarisation and `ctxIdxInc` rules of the macroblock-level syntax elements: `mb_type` for I slices with real neighbours, `mb_skip_flag`, `mb_qp_delta`, the intra prediction modes, `intra_chroma_pred_mode`, `coded_block_pattern` (prefix and suffix with neighbour-based increments) and `end_of_slice_flag`, so that the (element, value) sequence of whole slices can be decoded. Residuals follow in 8.3.
