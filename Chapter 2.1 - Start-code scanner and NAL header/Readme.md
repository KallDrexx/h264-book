# Chapter 2.1 - Start-code scanner and NAL header

## 1. Goal and the project

Phase 1 left you with an Annex B elementary stream file, `out.264`: the concatenated PES payloads of the video PID, byte-identical to `ffmpeg -i in.ts -map 0:v:0 -c:v copy -f h264 out.264`. From here on the transport stream is behind you and everything works on that one file.

In this chapter you build the first layer of the decoder proper. You scan the byte stream, cut it into NAL units, and parse each NAL unit's one-byte header. The program prints one line per NAL unit. The harness checks every line against a reference scanner, and cross-checks the sequence of NAL types against the NAL list that ffmpeg's `trace_headers` filter prints.

## 2. Big picture

H.264 separates two jobs. The video coding layer (VCL) describes pictures: slices of macroblocks. The network abstraction layer (NAL) wraps everything, VCL data and the parameter sets and metadata, into self-contained packets called NAL units. A NAL unit is a one-byte header followed by a payload. The payload is the useful data; for now treat it as opaque bytes.

The NAL units that make up one coded picture (its slices plus any parameter sets and metadata sent with it) form an **access unit**.

A NAL unit does not say how long it is. The standard defines several ways to carry NAL units; the one used in MPEG-TS files, `.264` files and most broadcast is **Annex B**, the *byte stream format*. In it, every NAL unit is preceded by a **start code**, a recognisable byte pattern. To split the stream you search for start codes. Each NAL unit runs from just after one start code to just before the next.

```
00 00 00 01 09 F0 | 00 00 00 01 67 42 C0 0D ... | 00 00 01 06 ... | 00 00 01 65 88 ...
 start code  AUD     start code   SPS               start code SEI    start code IDR slice
```

The chapter's whole job is: find the start codes, work out where each NAL unit begins and ends, read the header byte. Everything later (parameter sets, slice headers, macroblocks) starts from the NAL units you produce here.

## 3. Concepts in detail

### 3.1 Annex B byte stream (spec Annex B, 7.3.1)

The byte stream is a sequence of `byte_stream_nal_unit` structures:

| Part | Size | Meaning |
|---|---|---|
| `leading_zero_8bits` | 0 or more bytes of `00` | only at the very start of the stream |
| `zero_byte` | one byte `00` | present before the start code when the NAL unit is an SPS, PPS, or the first NAL unit of an access unit; allowed elsewhere |
| `start_code_prefix_one_3bytes` | `00 00 01` | marks the start of a NAL unit |
| `nal_unit` | `NumBytesInNALunit` bytes | header byte, then payload |
| `trailing_zero_8bits` | 0 or more bytes of `00` | after the NAL unit, before the next `zero_byte` or start code |

So a start code is either **3 bytes** (`00 00 01`) or **4 bytes** (`00 00 00 01`, a `zero_byte` plus the 3-byte prefix). The only real start code is `00 00 01`; the 4-byte form is the same thing with one extra zero in front. In ffmpeg/x264 output you typically see the 4-byte form before the first NAL unit of each access unit and before parameter sets, and the 3-byte form elsewhere (in the example in section 5, the SEI and the IDR slice use 3 bytes). A decoder must accept both everywhere.

### 3.2 Zero bytes belong to nobody

Any run of `00` bytes can sit before a start code. Rules to apply:

- To find the start code, look for `00 00 01`. Look at the byte just before it: if it exists and is `00`, the start code is 4 bytes, otherwise 3.
- Any other zeros between the end of one NAL unit and the next start code are `trailing_zero_8bits` (or `leading_zero_8bits` at the start of the file). They belong to no NAL unit: do not count them in the size of the previous one.
- At the end of the file, trailing zeros are likewise not part of the last NAL unit.

Stripping zeros this way never loses data, because the last byte of a NAL unit is never `00` (7.4.1). The payload ends with `rbsp_trailing_bits` (a 1 bit, then zero bits to the byte boundary), so its last byte is non-zero.

With this rule, `00 00 00 00 01` (an extra trailing zero before a 4-byte start code) needs no special case: the start code is the last four bytes, and the first `00` is a trailing zero.

### 3.3 Why a start-code search cannot be fooled

The payload is arbitrary compressed data and could contain `00 00 01` by chance. The standard forbids that by construction: when the encoder writes a NAL unit, whenever the payload would contain `00 00 00`, `00 00 01`, `00 00 02` or `00 00 03`, it inserts a byte `03` after the first two zeros (`00 00 03 01` instead of `00 00 01`). This is **emulation prevention**: no byte-aligned `00 00 00`, `00 00 01` or `00 00 02` ever appears inside a NAL unit, so every `00 00 01` in the file is a real start code. The decoder removes those `03` bytes later (Chapter 2.2). In this chapter you do not touch them: you search the raw bytes, and you report the size of the NAL unit as it appears in the file, `03` bytes included.

### 3.4 The NAL unit header (7.3.1.2, 7.4.1)

The first byte after the start code is the NAL unit header (for the types in this book it is exactly one byte).

| Bits (MSB first) | Field | Meaning |
|---|---|---|
| 1 | `forbidden_zero_bit` | must be 0. A 1 means a damaged or non-conforming stream |
| 2 | `nal_ref_idc` | 0 means "not used for reference"; 1 to 3 means "used for reference" (the exact non-zero value is an importance hint). Always non-zero for SPS, PPS and IDR slices |
| 5 | `nal_unit_type` | what the payload is, see below |

So `type = byte & 0x1F`, `ref_idc = (byte >> 5) & 3`, `forbidden_zero_bit = byte >> 7`. Example: `67` is `0 11 00111`: forbidden 0, ref_idc 3, type 7 (SPS). `65` is `0 11 00101`: IDR slice. `41` is `0 10 00001`: a reference non-IDR slice. `09` is `0 00 01001`: an access unit delimiter.

### 3.5 nal_unit_type (Table 7-1)

| Type | Name | Handling in this book |
|---|---|---|
| 1 | Coded slice of a non-IDR picture | decoded (Phase 3 onward) |
| 5 | Coded slice of an IDR picture | decoded |
| 6 | Supplemental enhancement information (SEI) | parsed lightly or skipped; x264 puts a "user data unregistered" message with its settings here |
| 7 | Sequence parameter set (SPS) | parsed (3.1) |
| 8 | Picture parameter set (PPS) | parsed (3.2) |
| 9 | Access unit delimiter (AUD) | recognised; marks the start of a picture's NAL units |
| 10 | End of sequence | recognised, then ignored |
| 11 | End of stream | recognised, then ignored |
| 12 | Filler data | ignored |
| 2, 3, 4 | Slice data partitions (Extended profile) | not supported; ignored |
| 13 to 23 | SPS extension, auxiliary picture, SVC/MVC/3D extensions, reserved | ignored |
| 0, 24 to 31 | unspecified | ignored |

"Ignored" means: your scanner still lists the NAL unit with its true type; later stages choose to skip it. Do not drop it here, and do not fail on unknown types. The scanner's job is to be complete and boring.

## 4. The project: exactly what to build

Write a program that reads `out.264` (from Chapter 1.4, or any Annex B `.264` file) and writes `nals.txt` (format in section 5).

1. Read the whole file into memory.
2. Find every start code `00 00 01`. Resume searching after the three bytes you just matched.
3. For each one, decide the start code length (3 or 4) from the byte before it, and record the offset of the first byte of the start code (so for a 4-byte start code, the offset of its leading `00`).
4. The NAL unit starts at the first byte after the prefix and ends just before the next start code's `00 00 01` (or at the end of the file). Remove trailing `00` bytes from the end. Its length is the size.
5. If the size is 0 (two start codes in a row, or a start code at the very end of the file), skip it; it is not a NAL unit.
6. Read the header byte and split it into the three fields.
7. Print the lines.

Bytes before the first start code, which are all zeros in a legal stream, are not part of any NAL unit and are not reported.

Verify (Summary step 2.1): the count of each `nal_unit_type`, in order, matches the NAL list in `trace_headers` output. The harness does this for you.

You will use these NAL units in every later chapter, so keep them (offset, size and header fields) in memory rather than printing and discarding them.

## 5. Harness input format

One text file, UTF-8 or ASCII, one record per line, fields separated by single spaces, **all numbers decimal**.

```
NAL_COUNT <n>
NAL <index> <offset> <sc_len> <size> <forbidden_zero_bit> <nal_ref_idc> <nal_unit_type>
...
```

| Field | Meaning |
|---|---|
| `NAL_COUNT n` | first line; number of NAL lines that follow |
| `index` | 0, 1, 2, ... in file order |
| `offset` | byte offset in `out.264` of the first byte of the start code (the leading `00` of a 4-byte start code, or the first byte of a 3-byte one) |
| `sc_len` | 3 or 4 |
| `size` | number of bytes of the NAL unit: the header byte plus the payload, as stored in the file (emulation prevention bytes included), excluding the start code and excluding trailing zero bytes |
| `forbidden_zero_bit` | 0 or 1 |
| `nal_ref_idc` | 0 to 3 |
| `nal_unit_type` | 0 to 31 |

Relationship you can check yourself: the NAL unit's bytes are `[offset + sc_len, offset + sc_len + size)`, and the byte at `offset + sc_len` has your three header fields in it.

Example, the start of the stream from the `T1` test file (`00 00 00 01 09 f0 00 00 00 01 67 42 c0 0d dc 16 ...`):

```
NAL_COUNT <total>
NAL 0 0 4 2 0 0 9
NAL 1 6 4 22 0 3 7
NAL 2 32 4 4 0 3 8
NAL 3 40 3 543 0 0 6
NAL 4 586 3 7402 0 3 5
NAL 5 7991 4 2 0 0 9
```

Read it: NAL 0 is an AUD (type 9), 4-byte start code at offset 0, 2 bytes (`09 f0`). NAL 1 starts at offset 6, SPS, ref_idc 3. NAL 3 is an SEI with a 3-byte start code; NAL 4 is the IDR slice.

Empty lines are ignored. Any other malformed line makes the harness exit 2.

## 6. Running the harness

```
python3 test_nal.py <input> <nals.txt>
```

`<input>` is either a `.ts` file or a raw `.264` file. Given a `.ts`, the harness extracts the elementary stream itself with ffmpeg and checks your offsets against that, so run your program on the `.264` from the same `.ts` (your Chapter 1.4 output, which is byte-identical to ffmpeg's). Given a `.264`, it checks against that file directly. It checks:

1. Every NAL line (offset, sc_len, size, header fields) against a reference scanner built into the harness, and `NAL_COUNT` against the line count.
2. The sequence of `(forbidden_zero_bit, nal_ref_idc, nal_unit_type)` against the NAL list in ffmpeg's `trace_headers` log (`ffmpeg -i in.ts -c:v copy -bsf:v trace_headers -f null -`; tested with ffmpeg 8.0.1). This is the Summary's "Verify" check. ffmpeg only lists NAL types it understands (1, 5, 6, 7, 8, 9, 10, 11, 12, 13, 19), so only those entries of your list are compared.

On success it prints the per-type counts and how many start codes were 3 or 4 bytes. Exit code 0 pass, 1 fail, 2 missing or malformed output.

`python3 test_nal.py --self-test` checks the harness itself: it builds correct outputs, expects PASS, then corrupts them and expects FAIL.

Stress variants: `python3 test_nal.py --make-variants <input.ts> <dir>` writes two raw files to run your scanner on:

- `all3.264`: the same NAL units with only 3-byte start codes.
- `padded.264`: random 3/4-byte start codes, leading zeros at the start of the file, trailing zeros after some NAL units and at the end of the file, plus extra filler (type 12), end-of-sequence (type 10) and unspecified (type 30) NAL units.

Run your scanner on each, then `python3 test_nal.py <variant.264> <your output>`. Test on at least `T1`, a B-frame file such as `T9_spatial`, and both variants.

## 7. Reading failures

The harness prints the NAL index, the field, expected and actual values, and a hex window near the problem.

- **Count too low, or `offset` off by one on 4-byte starts.** You reported the offset of the `01` or of the second zero. The offset is the first byte of the whole start code, including the extra `00`.
- **`sc_len` wrong after padding.** You decided 4 bytes by "the previous 3 bytes are zero" instead of "the one byte before `00 00 01` is zero", or you counted all zeros in a run. Cap at 4.
- **`size` too big by a few bytes.** Trailing zeros (`trailing_zero_8bits`, or the `zero_byte` of the next start code) were left in the NAL unit. Strip them. If you only miss the `zero_byte`, you will pass a plain stream and fail on `padded.264`.
- **Missing NAL at the very end, or one extra.** Off-by-one at end of file: the last NAL unit runs to the end of the file, minus trailing zeros.
- **A false NAL in the middle.** You started searching inside the previous start code. Resume after the matched `00 00 01`. Payload bytes cannot cause this (section 3.3), so it is a scanner bug, or your `.264` is not the same as ffmpeg's (check your Chapter 1.4 output).
- **Every offset after some point is wrong.** You missed or added a NAL unit there; the harness names the first offset where the lists diverge.
- **Wrong `nal_ref_idc` or type.** Check shifts and masks: type is the low 5 bits, ref_idc the 2 bits above, forbidden bit the top bit.
- **Slow on large files.** Search with a fast byte search (memchr-style on the `01` or `00` byte) rather than testing three bytes at every offset through a high-level iterator.

## 8. What's next

Chapter 2.2 removes the emulation prevention bytes (`00 00 03`) to turn each NAL unit payload into an RBSP, and builds the bit reader (`u(n)`, `ue(v)`, `se(v)`) you use for all header parsing.
