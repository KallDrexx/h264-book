# Chapter 1.1 - TS packet reader

## 1. Goal and the project

You are building an H.264 decoder from scratch, and the first thing it needs is bytes of video. Broadcast and streaming video usually arrives as an MPEG transport stream (a `.ts` file). In this chapter you write the lowest layer: a program that cuts a `.ts` file into its fixed-size packets, decodes each packet's 4-byte header, and prints a summary. Later chapters build on it: 1.2 finds out which stream is the video, 1.3 reassembles the video data and its timestamps, and 1.4 writes out the raw H.264 stream.

## 2. Big picture

A transport stream (TS) carries several streams at once: typically one video, one or more audio, and small "table" streams that describe the rest. To make this work over unreliable links (satellite, cable, UDP), the data is chopped into **packets of exactly 188 bytes**. Any receiver can tune in mid-stream: it only has to find the next packet boundary and start reading.

Every packet begins with a header that says which stream it belongs to. That stream label is the **PID** (packet identifier). Packets of different streams are interleaved. To recover one stream, you pick out all packets with its PID and concatenate their payloads. This chapter does the first half: labelling every packet. Nothing in this chapter looks inside the payloads.

The spec is ITU-T H.222.0 / ISO 13818-1 (MPEG-2 Systems), section 2.4.3.

## 3. Concepts in detail

### 3.1 Packet layout

Each packet is 188 bytes: a 4-byte header, then an optional adaptation field, then payload (together always filling the remainder).

Header (bits, most significant first; all multi-byte values are big-endian):

| Bits | Field | Meaning |
|---|---|---|
| 8 | sync_byte | Always `0x47`. Marks a packet start. |
| 1 | transport_error_indicator (TEI) | 1 = the demodulator knew this packet was corrupt. Normally 0. |
| 1 | payload_unit_start_indicator (PUSI) | 1 = a new higher-level unit (a table, or a chunk of video or audio) starts in this payload. Chapters 1.2 and 1.3 use it. |
| 1 | transport_priority | Rarely used. Ignore. |
| 13 | PID | Stream label, 0 to 8191. |
| 2 | transport_scrambling_control | 0 = clear. Ignore here (encrypted streams are out of scope). |
| 2 | adaptation_field_control (AFC) | See table below. |
| 4 | continuity_counter (CC) | 4-bit counter, see 3.3. |

So byte 1 holds TEI (bit 7), PUSI (bit 6), priority (bit 5) and the top 5 PID bits; byte 2 holds the low 8 PID bits; byte 3 holds scrambling (bits 7-6), AFC (bits 5-4) and CC (bits 3-0).

adaptation_field_control:

| Value | Meaning |
|---|---|
| 0 | Reserved. Treat the packet as having no usable content. |
| 1 | Payload only. |
| 2 | Adaptation field only, no payload. |
| 3 | Adaptation field, then payload. |

Reserved PIDs worth knowing: `0x0000` always carries the PAT, the table that lists the programs in the stream (chapter 1.2). `0x1FFF` marks a **null packet**: pure padding with no data.

### 3.2 The adaptation field

If AFC is 2 or 3, byte 4 is `adaptation_field_length`: the number of bytes that follow it, *not counting itself*. The adaptation field occupies byte 4 through byte `4 + length`. The payload (if AFC is 3) starts right after, at byte `5 + length`. For AFC 2 the length is 183, filling the whole packet. A length of 0 is legal: the adaptation field is then just the length byte, which encoders use to pad a payload by one byte.

When the length is above 0, the next byte is a flags byte:

| Bit | Flag |
|---|---|
| 7 | discontinuity_indicator |
| 6 | random_access_indicator |
| 5 | elementary_stream_priority_indicator |
| 4 | PCR_flag |
| 3 | OPCR_flag |
| 2 | splicing_point_flag |
| 1 | transport_private_data_flag |
| 0 | adaptation_field_extension_flag |

If PCR_flag is set, the 6 bytes after the flags hold the **program clock reference** (PCR): the sender's clock reading, which real-time players use to stay in step. A file-based decoder needs it only for sanity checks, so decoding it is optional here, but it is a good test of your bit handling. The 48 bits are:

| Bits | Field |
|---|---|
| 33 | base, in 90 kHz ticks |
| 6 | reserved (skip) |
| 9 | extension, in 27 MHz ticks |

The PCR in 27 MHz ticks is `base * 300 + extension`.

You do not need the other flags. Just trust `adaptation_field_length` to find where the payload begins. Be defensive: if the length would run past the end of the packet, the packet is malformed; do not read outside it.

### 3.3 Continuity counter

Per PID, the 4-bit CC counts packets that carry a payload: it goes 0, 1, ..., 15, 0, ... A receiver uses it to detect lost packets. Rules (2.4.3.3):

- The CC increments by 1 (mod 16) from one packet of a PID to the next *only if that next packet has a payload* (AFC 1 or 3).
- A packet with AFC 2 (adaptation only) does **not** increment: it carries the same CC as the previous packet of that PID.
- Null packets (PID `0x1FFF`) and AFC 0 packets are not checked.
- The spec also allows one duplicate packet (same CC repeated) and lets a `discontinuity_indicator` excuse a jump. Your reader may ignore both; this chapter's ground truth does too. Clean ffmpeg output has neither.

Define a **continuity error** exactly like this, per PID, remembering `last_cc` (the CC of the previous non-null, non-AFC-0 packet of that PID):

1. First such packet of the PID: no check; just record its CC.
2. If AFC is 1 or 3: error if `CC != (last_cc + 1) mod 16`.
3. If AFC is 2: error if `CC != last_cc`.
4. In all cases set `last_cc = CC` afterwards (so one lost packet causes one error, not a cascade).

### 3.4 Sync and resync

In a clean file every 188th byte is `0x47`. Real captures can start mid-packet, or have junk or dropped bytes. If the byte where a sync byte is expected is not `0x47`, you have lost sync. **Resync** by scanning forward byte by byte for a position `p` where a whole packet fits and the sync bytes of three consecutive packets starting at `p` are all `0x47`: bytes `p`, `p+188` and `p+376` for 188-byte packets. If the file ends before all three exist, check the ones that do. A single `0x47` is not enough proof, because that value also occurs in payload data. Once in sync, accept each packet whose sync byte is `0x47` without further checks. Bytes skipped while searching are counted as skipped and not as packets. A trailing fragment shorter than a packet at the end of the file is also skipped bytes.

### 3.5 Aside: 192-byte `.m2ts`

Blu-ray and AVCHD camcorders store transport streams as `.m2ts` with **192-byte** units: a 4-byte prefix (a 2-bit copy-permission field and a 30-bit arrival timestamp), then the ordinary 188-byte packet. Everything else is the same. A robust reader checks both layouts at startup: sync byte at offset 0 every 188 bytes, or at offset 4 every 192 bytes. For 192-byte units, the resync checks of 3.4 look at the sync byte, 4 bytes into each unit. The harness supports both layouts and can make an `.m2ts` for you (section 6). Some 204-byte variants exist too (188 plus 16 bytes of error-correction data); you can ignore them.

## 4. The project: exactly what to build

Write a command-line program (any language) that takes a `.ts` file path and an output path, and:

1. Reads the file and picks the layout: 188-byte packets if the first five 188-byte units (or as many as exist) have `0x47` at offset 0, else 192 if the first five 192-byte units have it at offset 4. If neither fits (junk at the start), resync as in 3.4, trying 188 first, and use the first layout that finds a sync point.
2. Walks the file packet by packet. On a missing sync byte, resyncs as in 3.4 and counts skipped bytes.
3. For each packet parses the 4-byte header (TEI, PUSI, PID, AFC, CC), and if an adaptation field is present, skips it using its length byte. If the PCR flag is set, optionally decodes the PCR.
4. Keeps, per PID: packet count and continuity-error count (3.3).
5. Writes the output file described in section 5.

Verify (by eye, before running the harness):

- `PACKETS` equals file size / 188 for a clean file (and `SKIPPED_BYTES` is 0).
- Your per-PID histogram has a few packets on PID 0 (the PAT), a few on another table PID, most on the video PID, and no continuity errors in a clean file. If you see errors, you most likely incremented the counter on adaptation-only packets.
- A clean file never exercises resync or continuity errors. Make a damaged copy with the harness (section 6) and check that your reader reports skipped bytes and a continuity error.

## 5. Harness input format

The harness reads one UTF-8 text file written by your program. Lines are ASCII, space-separated, with `\n` (or `\r\n`) endings. Blank lines and lines starting with `#` are ignored. Line order within the file does not matter, but the content must be exactly as follows.

**Header lines (exactly one each, all required):**

```
FILE_SIZE <bytes>
PACKET_SIZE <188 or 192>
PACKETS <number of packets accepted>
SKIPPED_BYTES <bytes not part of any accepted packet>
```

All numbers are decimal. `FILE_SIZE` = `PACKETS * PACKET_SIZE + SKIPPED_BYTES`.

**PID lines (one per PID that occurs, including null packets on 1FFF):**

```
PID <pid> <packet_count> <cc_errors>
```

`<pid>` is **exactly four lowercase hex digits** (no `0x`), e.g. `0000`, `0100`, `1fff`. Counts are decimal and include every packet of that PID (also adaptation-only ones). Order is free, but ascending by PID is conventional.

**PKT lines (one per packet for the first 20 packets of the file; fewer if the file has fewer):**

```
PKT <index> <offset> <tei> <pusi> <pid> <afc> <cc>
```

- `index`: 0-based packet number among accepted packets.
- `offset`: file byte offset where the unit starts (for 192-byte files this is the start of the 4-byte prefix, not the sync byte).
- `tei`, `pusi`: 0 or 1. `pid`: four lowercase hex digits. `afc`: 0 to 3. `cc`: 0 to 15, decimal.

**PCR lines (optional; if you print any, print them for the first 10 packets that have a PCR, in file order):**

```
PCR <index> <pid> <pcr_27mhz>
```

`<pid>` as above; `pcr_27mhz` is `base*300 + extension` in decimal. A packet "has a PCR" when AFC is 2 or 3, `adaptation_field_length >= 7` and flag bit 4 (PCR_flag) is set. If you print no PCR lines, the PCR check is skipped.

Example (start of a small file):

```
FILE_SIZE 427888
PACKET_SIZE 188
PACKETS 2276
SKIPPED_BYTES 0
PID 0000 17 0
PID 0011 4 0
PID 0100 2238 0
PID 1000 17 0
PKT 0 0 0 1 0011 1 0
PKT 1 188 0 1 0000 1 0
PKT 2 376 0 1 1000 1 0
PKT 3 564 0 1 0100 3 0
# ... PKT 4 to 19 omitted here; your file must contain all 20
PCR 3 0100 18900000
PCR 47 0100 19980000
# ... 8 more PCR lines
```

## 6. Running the harness

```
python3 test_ts_reader.py <input.ts> <your_output.txt>
python3 test_ts_reader.py --make-variants <input.ts> <dir>   # writes damaged.ts and as.m2ts
python3 test_ts_reader.py --self-test       # checks the harness itself
```

Run it from this chapter's folder (it needs only Python 3, `ffmpeg` and `ffprobe` on `PATH`). Test inputs: `python3 -m common.testfiles T1 T12 default` from the repository root builds `.ts` files into `.cache/`. Any ffmpeg-produced MPEG-TS file works, including audio+video ones. `--make-variants` turns a clean `.ts` into `damaged.ts` (junk at the start, a dropped packet, junk bytes mid-stream, a cut-off last packet) and `as.m2ts` (192-byte units); run your reader and the harness on both.

The harness parses the file itself, following sections 3.3 and 3.4 exactly, and compares the four header lines, every PID line, the PKT lines, and the PCR lines if present. It also checks that the video PID reported by `ffprobe` is in your table. Exit code 0 means PASS, 1 means FAIL (it lists the first 10 mismatches by line type, index and field), 2 means your output file is missing or malformed (it names the offending line).

## 7. Reading failures and debugging tips

- `PACKETS` too large or small by a few: your resync is wrong, or you count a trailing fragment as a packet.
- Continuity errors on a clean file, on the video PID: you are likely incrementing or comparing on AFC 2 packets, or comparing against the wrong "previous" packet (it must be the previous packet *of the same PID*).
- `PKT` row differs in `pid`: check that you masked the top byte with `0x1F` before shifting. `pusi` is bit 6, not bit 7, of byte 1.
- Everything off on a 192-byte file: you are reading the sync byte at offset 0 rather than 4, or your `offset` points to the sync byte rather than the unit start.
- `PCR` value off: with the 6 PCR bytes numbered 0 to 5, the 33-bit base is bytes 0-3 plus the top bit of byte 4; the 9-bit extension is the lowest bit of byte 4 plus all of byte 5. Bits 6 to 1 of byte 4 are reserved and must be skipped.
- Dump `xxd -l 564 file.ts` and decode the first three headers by hand. In ffmpeg output the first packet is the SDT (PID 0x0011, bytes `47 40 11 10`); the second, `47 40 00 10`, means sync, PUSI=1, PID 0 (the PAT), AFC 1, CC 0.

## 8. What's next

Chapter 1.2 uses the PID 0 packets you can now find to reassemble the PAT and PMT sections and discover which PID carries the video.
