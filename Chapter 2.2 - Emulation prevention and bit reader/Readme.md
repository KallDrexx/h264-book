# Chapter 2.2 - Emulation prevention and bit reader

## 1. Goal and the project

Chapter 2.1 gave you every NAL unit of the stream: where it is, how big it is, and its header byte. The payload after the header is still the raw bytes as stored in the file. In this chapter you do two things.

1. Convert each NAL unit's payload to its **RBSP** by removing the emulation prevention bytes.
2. Write the **bit reader** you will use for every header in Phase 3: `u(n)`, `ue(v)`, `se(v)`, `byte_aligned()`, `more_rbsp_data()` and the trailing-bits check.

The harness checks two outputs, separately. Part (a) is a report with one line per NAL unit, compared against the harness's own conversion of a real stream. Part (b) is a set of bit-reader unit tests: the harness writes them, your program runs them, and the harness grades the results.

This chapter ends Phase 2. After it, you can cut any H.264 file into NAL units, get clean payload bytes, and read bits and Exp-Golomb numbers out of them.

## 2. Big picture

Chapter 2.1 explained that the byte stream finds NAL units by searching for `00 00 01`. For that to be safe, a NAL unit's bytes must never contain `00 00 01` (or `00 00 00`, which would be confused with zero padding) by accident. The encoder guarantees that by inserting an extra byte `03` wherever such a pattern would otherwise appear. That byte is the **emulation prevention byte**. It carries no information, so the decoder throws it away.

What is left after dropping those bytes is the **RBSP**, the raw byte sequence payload. This is what the syntax tables in the standard describe. Every syntax table in sections 7.3.2 and 7.3.3 (SPS, PPS, slice header, ...) reads from the RBSP, never from the NAL bytes.

```
NAL unit bytes:   67 | 42 C0 0D 00 00 03 01 ... 80
                  ^header ^---------payload----------^
drop the 03:           42 C0 0D 00 00 01 ... 80   <- the RBSP (header byte not included)
```

The RBSP is a stream of **bits**, not bytes. Fields are 1 bit, 3 bits, 13 bits wide. Many are numbers in a variable-length code called **Exp-Golomb**, where small numbers use few bits. The bit reader turns "a pile of bytes" into "the next 5 bits as a number", "the next Exp-Golomb number" and so on. It is the foundation under all header parsing.

## 3. Concepts in detail

### 3.1 Emulation prevention (7.3.1, 7.4.1, 7.4.1.1)

**Encoder side.** After building the RBSP, the encoder scans it. Whenever two consecutive zero bytes are followed by a byte with a value from `00` to `03`, it inserts a `03` between the zeros and that byte:

| RBSP bytes | NAL bytes |
|---|---|
| `00 00 00` | `00 00 03 00` |
| `00 00 01` | `00 00 03 01` |
| `00 00 02` | `00 00 03 02` |
| `00 00 03` | `00 00 03 03` |

After inserting, the count of consecutive zeros starts over (the `03` is not a zero). So `00 00 00 00` becomes `00 00 03 00 00`, and six zero bytes become `00 00 03 00 00 03 00 00`.

There is one more case. If the RBSP ends in a `00` byte, the encoder appends a final `03`. This only happens when the RBSP ends in `cabac_zero_word`s (zero padding that some CABAC slices carry after their data, section 3.3), so the NAL unit then ends in `... 00 00 03`. This also explains why a NAL unit never ends in `00` (Chapter 2.1).

Consequences for a conforming stream: inside a NAL unit, `00 00 03` is always followed by `00`, `01`, `02`, `03`, or the end of the NAL unit. A `00 00 03` followed by anything bigger is not allowed.

**Decoder side.** The decoder rule (the `nal_unit()` syntax in 7.3.1) is simpler than the encoder's:

1. Take the NAL unit's payload (the bytes after the header byte).
2. Keep a count of consecutive `00` bytes just copied.
3. For each byte: if the count is at least 2 and the byte is `03`, drop it and reset the count to 0. Otherwise copy it; the count goes up by one if the byte was `00`, and resets to 0 if not.

You do not need to look at the byte after the `03`: in a conforming stream it is one of `00`-`03` or the end, and dropping the `03` is right in every case. Two things to get right:

- **Reset the count after a drop.** `00 00 03 00 00 03` becomes `00 00 00 00` (two drops), and `00 00 03 03` becomes `00 00 03` (the second `03` is data, not a second prevention byte).
- **A single zero does not trigger.** `00 03` stays `00 03`. A `03` after one zero byte, or after non-zero bytes, is data.

The header byte is not part of the RBSP and not part of the zero count. (Some NAL types, 14, 20 and 21, have extra header bytes; this book ignores those types.)

Always cut the stream into NAL units first (Chapter 2.1 works on the escaped bytes), and only then unescape each NAL unit as a whole.

### 3.2 RBSP, SODB and trailing bits (7.3.2.11, 7.4.1.1)

Terms, from the inside out:

- **SODB**, string of data bits: the actual syntax elements, as a bit string of any length (not necessarily a multiple of 8).
- **RBSP**: the SODB followed by `rbsp_trailing_bits()`, which pad it to a whole number of bytes. If the SODB is empty (end-of-sequence and end-of-stream NAL units, types 10 and 11), the RBSP is empty too: no trailing bits.
- **NAL unit**: header byte plus the RBSP with emulation prevention bytes inserted.

`rbsp_trailing_bits()` is one `1` bit (`rbsp_stop_one_bit`) followed by as many `0` bits as needed to reach a byte boundary (`rbsp_alignment_zero_bit`). So the last non-zero byte of the RBSP holds the stop bit as its lowest set bit, and everything after it is zeros. Examples for a SODB that ends on a byte boundary, after 3 bits, and after 7 bits:

```
SODB ends aligned:       ... xxxxxxxx | 10000000
SODB ends after 3 bits:  ... xxx10000
SODB ends after 7 bits:  ... xxxxxxx1
```

That is how a decoder finds the end of the real data: the last `1` bit in the RBSP is the stop bit, and the bits before it are the SODB. This matters because several structures (the PPS, some SEI messages, CAVLC slice data) have optional or repeating content that continues "while there is more data", and the only way to know is to look for the stop bit.

### 3.3 Zero bytes after the trailing bits

The RBSP of a slice that uses CABAC entropy coding may end in `cabac_zero_word`s: pairs of bytes `00 00` appended after `rbsp_slice_trailing_bits()`. The encoder adds them to stay within a CABAC limit on how many coded symbols a slice may hold relative to its size in bytes (7.4.2.10). In the NAL unit they appear as `00 00 03 00 00 03 ...`; after unescaping they are trailing zero bytes of the RBSP. They are part of the RBSP, so your RBSP length includes them, but they come after the stop bit and are never data. Rule: the stop bit is the **last** `1` bit of the RBSP, not the first byte that looks like `0x80`.

### 3.4 Reading bits (7.2, 9.1)

The reader keeps a position counted in bits from the start of the RBSP. Bits are consumed most significant bit first: the first bit read is the top bit of byte 0.

**u(n)**: read `n` bits as an unsigned number, first bit most significant. In this book `n` goes up to 32 (`u(32)` appears in SEI and in timing info of the VUI). If you use a 32-bit integer for the result, be careful with `n = 32`; most languages want a 64-bit accumulator.

**Exp-Golomb, the idea.** Many syntax elements are small numbers most of the time (mostly 0, 1, 2) with a long tail. Exp-Golomb gives short codes to small numbers. A code has two parts:

1. A run of `z` zero bits ended by a `1` bit. The `z` tells you how many bits of "value" follow.
2. `z` more bits (the suffix).

The number is `2^z - 1 + (the suffix read as an unsigned number)`. With `z = 0` the code is just `1` (value 0). Each time `z` goes up by one, the code is two bits longer and covers twice as many values.

**ue(v)**, unsigned Exp-Golomb (9.1): count the leading zero bits until the first `1` bit (consume that bit as well), call the count `z`, read `z` bits as `suffix`, result `codeNum = 2^z - 1 + suffix`. Table 9-2:

| Bit string | codeNum |
|---|---|
| `1` | 0 |
| `010` | 1 |
| `011` | 2 |
| `00100` | 3 |
| `00101` | 4 |
| `00110` | 5 |
| `00111` | 6 |
| `0001000` | 7 |
| `0001001` | 8 |
| `0001010` | 9 |
| ... | ... |
| `000010000` | 15 |

Worked example: bits `00101`: two leading zeros, so `z = 2`; the `1` ends the prefix; the suffix is the next 2 bits `01` = 1; codeNum = 2^2 - 1 + 1 = 4. Matches the table.

Limits: no H.264 syntax element needs a value above 2^32 - 2, so `z` is at most 31 and a code is at most 63 bits long. A `z` of 32 or more means a corrupt stream: report an error. Use a 64-bit variable for the value in languages with a 32-bit default integer: 2^32 - 2 does not fit a signed 32-bit integer.

**se(v)**, signed Exp-Golomb: read `codeNum` exactly like `ue(v)`, then map it to a signed number: odd codeNums are positive, even ones are zero or negative.

| codeNum | se value |
|---|---|
| 0 | 0 |
| 1 | +1 |
| 2 | -1 |
| 3 | +2 |
| 4 | -2 |
| 5 | +3 |
| ... | ... |

Formula: if `codeNum` is odd the value is `(codeNum + 1) / 2`, otherwise `-(codeNum / 2)` (integer division). The extreme codeNum 2^32 - 2 gives -(2^31 - 1).

**byte_aligned()**: true when the bit position is a multiple of 8.

**more_rbsp_data()** (7.2): true if there is more data in the RBSP before the `rbsp_trailing_bits()`, false otherwise. Implement it as: find the position of the stop bit (the last `1` bit in the whole RBSP, ignoring any trailing zero bytes); result is true exactly when the current position is **before** that bit. If the next bit to read is the stop bit, it is false. If the RBSP contains no `1` bit at all, or is empty, it is false. Compute the stop-bit position once when you create the reader, not on every call. (A reader that scans backwards from the end of the buffer on each call is correct but slow for CAVLC slice data.)

**The trailing-bits check**: after the last syntax element of an RBSP, `rbsp_trailing_bits()` must follow. Check it by reading one bit that must be `1`, then reading zero bits until the position is byte-aligned, which must all be `0`. This book also requires that every remaining byte after that is `00` (cabac_zero_words, or nothing). If any of that fails, the stream is broken or your parser lost its place. A parser that ends every RBSP with this check catches most header-parsing mistakes immediately.

## 4. The project: exactly what to build

Build, in your language of choice, on top of Chapter 2.1:

1. **NAL to RBSP**: a function that takes a NAL unit's bytes (header included) and returns the RBSP (bytes after the header byte, emulation prevention bytes removed) together with the number of bytes removed.
2. **Bit reader** over a byte array with: `u(n)` for `n` from 1 to 32, `ue()`, `se()`, `byte_aligned()`, `more_rbsp_data()`, a way to skip bits, and the trailing-bits check. Reading beyond the end of the data, or an Exp-Golomb code with 32 or more leading zeros, must be reported as an error (an exception, an error flag, whatever fits your language), not a crash and not silent garbage.
3. **Part (a) program**: read `out.264`, split into NAL units (your 2.1 code), convert each to its RBSP, compute the values in section 5.1 and write the report.
4. **Part (b) program**: read the harness's vector file, run the scripted operations on your bit reader, and write the results file (section 5.2).

Verify (Summary step 2.2): unit-test Exp-Golomb against Table 9-2 and the se mapping (the harness vectors include them); smoke test: the first three bytes of the first SPS RBSP are `profile_idc` (66, 77 or 100, for the profiles in this book), the constraint flags byte, and `level_idc` (for example 40 for level 4.0), both matching what `ffprobe -show_streams` reports. The harness does that comparison, plus the one against ffmpeg's `trace_headers`.

Keep the RBSPs in memory or be able to recompute them cheaply. Phase 3 runs your bit reader on them.

## 5. Harness input formats

All numbers are decimal unless stated. Fields are separated by single spaces. Empty lines are ignored.

### 5.1 Part (a): NAL report

```
NAL_COUNT <n>
RBSP <index> <nal_unit_type> <rbsp_len> <ep_removed> <stop_bit_pos> <hash>
...                                              (n RBSP lines, in stream order)
SPS_BYTES <b0> <b1> <b2>
```

| Field | Meaning |
|---|---|
| `index` | 0, 1, 2, ... the NAL's index in the file, the same numbering as Chapter 2.1 |
| `nal_unit_type` | low 5 bits of the header byte |
| `rbsp_len` | number of bytes in the RBSP: NAL size minus 1 (header) minus `ep_removed`. Trailing zero bytes of the RBSP (cabac_zero_words) are counted |
| `ep_removed` | number of emulation prevention bytes dropped |
| `stop_bit_pos` | bit offset of the rbsp_stop_one_bit, counted from the most significant bit of RBSP byte 0 (which is offset 0). That is the last `1` bit in the RBSP. `-1` if the RBSP has no `1` bit or is empty |
| `hash` | FNV-1a 32-bit hash of the RBSP bytes, as exactly 8 lowercase hexadecimal digits (the only hex field) |
| `b0 b1 b2` (`SPS_BYTES`) | the first three RBSP bytes of the first NAL unit of type 7, each as a decimal 0-255. One line, last in the file |

**FNV-1a 32**, defined exactly: start with `h = 2166136261` (`0x811C9DC5`). For each RBSP byte `x` in order: `h = h XOR x`, then `h = (h * 16777619) mod 2^32` (`16777619 = 0x01000193`). The result is `h`. The hash of an empty RBSP is `811c9dc5`. Known value: the hash of the single byte `61` is `e40c292c`. Use 64-bit arithmetic for the multiplication in languages without wrap-around 32-bit unsigned integers.

All NAL units are listed, whatever their type. The `RBSP` line for a NAL unit consisting of only a header byte has `rbsp_len` 0, `stop_bit_pos` -1 and hash `811c9dc5`.

Example, the start and end of the report for `T1` (the elementary stream is the same on every machine, so your values should match exactly):

```
NAL_COUNT 201
RBSP 0 9 1 0 3 750b7a6f
RBSP 1 7 20 1 154 a7af30a1
...
SPS_BYTES 66 192 13
```

The AUD `09 f0` has RBSP `f0`: length 1, stop bit at offset 3 (`f0` is `1111 0000`, the last 1 bit is bit 3).

### 5.2 Part (b): vector file and results file

The vector file is generated by the harness (`--write-vectors`) and is deterministic. Your program reads it and writes the results file. The harness recomputes the expected results from the vector file you give it, so you can add your own vectors by hand.

**Vector file grammar.** The file is a list of records: multi-line `VECTOR` blocks and one-line `ESCAPE` records. Lines starting with `#` and empty lines are ignored.

```
VECTOR <name>
HEX <hex digits, no spaces, even count, or "-" for zero bytes of data>
<op>
<op>
...
END
ESCAPE <name> <hex digits of a whole NAL unit, header byte included>
```

- `<name>` is a token without spaces. Names are unique across the file.
- A `VECTOR` block creates a **fresh** bit reader on the HEX bytes at bit position 0 (the bytes are an RBSP already: no unescaping), then executes its ops in order.
- An `ESCAPE` line is a one-line record for the NAL-to-RBSP conversion.

**Ops**, one per line:

| Op | Meaning | Result line |
|---|---|---|
| `u <n>` | read `n` bits (1 to 32) as unsigned | the value |
| `ue` | read one ue(v) | the value |
| `se` | read one se(v) | the value, with a leading `-` if negative |
| `aligned` | `byte_aligned()` at the current position | `1` or `0` |
| `more_data` | `more_rbsp_data()` at the current position | `1` or `0` |
| `skip <n>` | skip `n` bits (any n >= 0) | `ok` |
| `trailing` | the trailing-bits check of section 3.4 at the current position | `1` if valid, `0` if not |

Every op writes **exactly one** result line, so the result lines of a block line up with its ops. If a `u`, `ue`, `se` or `skip` would read past the end of the data, or `ue`/`se` meets 32 or more leading zero bits, the result line is `ERR`. `aligned`, `more_data` and `trailing` never give `ERR`; `trailing` with no bits left gives `0`.

`aligned` and `more_data` do not move the position. A valid `trailing` consumes the stop bit and the alignment zeros. After an `ERR` or a `trailing` that gives `0`, the position is undefined, so every generated vector ends at such an op (and at any `trailing`). If you write your own vectors, follow the same rule.

**Results file grammar.**

```
VECTOR <name>
<result line, one per op>
END
...                                  (one block per VECTOR)
ESCAPE <name> <rbsp hex, or "-" if empty> <ep_removed>    (one line per ESCAPE record)
```

The harness matches blocks and lines by name, so the order does not matter; writing them in vector-file order is simplest. Each `ESCAPE` result line is compared as a whole string.

For an `ESCAPE` record: strip the header byte, unescape, print the RBSP bytes as lowercase hex without spaces and the number of removed bytes, on one line.

Example, a vector from the generated file (Table 9-2 codes `1 010 011 00100 00101 00110 00111 0001000` packed into bytes, `a6 42 98 e2 00`):

```
VECTOR table_9_2_first_codes
HEX a64298e200
ue
ue
ue
ue
ue
ue
ue
ue
END
ESCAPE esc0 6500000301
```

and the matching results:

```
VECTOR table_9_2_first_codes
0
1
2
3
4
5
6
7
END
ESCAPE esc0 000001 1
```

What the generated file contains: Table 9-2 for codeNum 0 to 31 as ue, the same bit patterns read as se (the mapping 0, +1, -1, +2, -2, ...), se round-trips including +/-(2^31 - 1), ue codes up to 2^32 - 2 (31 leading zeros followed by a 32-bit number), `u(n)` for every n up to 32 across byte boundaries, `aligned` after odd and even bit counts, `more_data` stepped bit by bit through a byte, with trailing zero padding, with cabac_zero_words and with the stop bit in the last bit of the data, `trailing` valid and invalid cases, error cases (reading past the end, 32 leading zeros, a truncated suffix), 24 randomized vectors, and `ESCAPE` cases for every pattern in section 3.1.

## 6. Running the harness

Generate the vectors:

```
python3 test_bits.py --write-vectors vectors.txt
```

Run your programs (your own command lines), then:

```
python3 test_bits.py <in.ts> <nal_report>                          # part (a) only
python3 test_bits.py --vectors vectors.txt <vec_results>            # part (b) only
python3 test_bits.py <in.ts> <nal_report> --vectors vectors.txt <vec_results>   # both
python3 test_bits.py --self-test
```

`<in.ts>` may also be a raw `.264` file. The harness (tested with ffmpeg 8.0.1) checks:

- **Part (a)**: the harness extracts the elementary stream with ffmpeg, cuts it into NAL units with its own scanner, applies its own NAL-to-RBSP conversion, and compares every report line field by field: index, type, `rbsp_len`, `ep_removed`, `stop_bit_pos`, `hash`. It then checks `SPS_BYTES` against the first SPS RBSP, and cross-checks `profile_idc` and `level_idc` against ffmpeg's `trace_headers` log and `ffprobe -show_streams` (profile name mapped to its number, level). On success it prints how many emulation prevention bytes were removed in total.
- **Part (b)**: it parses the vector file, computes the expected results with its reference reader and compares them line by line. A failure names the vector, the op index (counted from 0) and text, the expected and actual line, the vector's data bytes, and the bit position before that op.

Exit codes: 0 pass, 1 fail, 2 missing or malformed output. `--self-test` builds correct outputs from ffmpeg-derived truth (three test files, a variant where every slice has `cabac_zero_words` appended, the full vector set), expects PASS, then corrupts them (hash, stop-bit position, removed count, SPS bytes, a ue result, a missing record, malformed lines) and expects FAIL or exit 2.

Test part (a) on at least `T1`, a B-frame file such as `T9_spatial`, and a CABAC file (`T10_full`, whose slices may end in `cabac_zero_words`). The `.ts` files are produced by `python3 -m common.testfiles`.

## 7. Reading failures and debugging tips

- **`ep_removed` too small or `rbsp_len` too big.** You only drop `03` when it follows `00 00` in the original bytes, but forgot the zero-count reset after a drop: `00 00 03 00 00 03` must lose both `03`s. Or you only check `00 00 03 0x` with a look-ahead and miss the one at the very end of the NAL unit (the cabac_zero_words case).
- **`ep_removed` too big.** You drop `03` after a single zero, or after a dropped `03` you keep the old zero count: `00 00 03 03` loses only the first `03`.
- **`rbsp_len` off by exactly one.** The header byte is not part of the RBSP.
- **Lengths agree, hash differs.** Check the hash first: FNV-1a XORs then multiplies (not the other way round, that is FNV-1) and the multiply wraps at 32 bits. The harness prints the first RBSP bytes. If many NALs fail but short ones pass, it is usually the hash multiply overflowing.
- **`stop_bit_pos` wrong.** You looked for the first `0x80` byte, or the last non-zero byte but then used its high bit rather than its lowest set bit. The position is `8 * byte_index + (7 - index_of_lowest_set_bit_counted_from_LSB)`.
- **A ue result is wrong only for big values.** 32-bit overflow when you compute `2^z - 1 + suffix`, or `u(32)` truncating. Use 64 bits.
- **se sign wrong for codeNum 2, 4, ...** Odd is positive: codeNum 1 is +1, 2 is -1.
- **`more_data` off by one.** The stop bit itself is not data: at the position of the stop bit the answer is 0. In the `more_data_walk` vector it flips from 1 to 0 exactly when the next bit to read is the stop bit.
- **`more_data` wrong with padding.** You looked at the last byte instead of the last byte that is non-zero.
- **`ERR` expected but you returned a number** (or crashed). Check bounds on every read, including each suffix read inside `ue`.

## 8. Phase 2 recap and what's next

Phase 2 took you from the elementary stream to parsed-ready data: Chapter 2.1 split the byte stream into NAL units and read each NAL header; this chapter removed the emulation prevention bytes to give each NAL unit its RBSP, and built the bit reader that reads fixed-width and Exp-Golomb fields and finds the end of the data. From here on the pipeline is: file, NAL units, RBSP, bit reader.

Phase 3 uses it for the first real syntax. Chapter 3.1 parses the sequence parameter set (7.3.2.1.1), 3.2 the picture parameter set, and the following chapters the slice header and picture order count, all from the RBSP with `u(n)`, `ue(v)`, `se(v)` and `more_rbsp_data()`. The verification target changes too: you print the field names and values the way ffmpeg's `trace_headers` does, and compare with a plain `diff`.
