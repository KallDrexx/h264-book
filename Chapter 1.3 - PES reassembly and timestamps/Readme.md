# Chapter 1.3 - PES reassembly and timestamps

## 1. Goal and the project

Chapter 1.1 labelled every packet of a `.ts` file, and chapter 1.2 told you which PID carries the video. In this chapter you collect the video PID's payloads into whole **PES packets**, parse each PES header, and extract three things per packet: its **PTS**, its **DTS** (both timestamps on a 90 kHz clock) and the size of the video data it carries. You write a program that prints one line per PES packet. The harness checks your lines against a second implementation and against `ffprobe -show_packets`.

Chapter 1.4 then concatenates the PES payloads into the raw `.264` file. The payload sizes you verify here are what makes that step trivially correct.

## 2. Big picture

H.264 video is not put into TS packets directly. The encoder's output, the **elementary stream (ES)**, is first cut into **access units** (one coded picture each, mostly). Each access unit is wrapped in a **PES packet** (Packetized Elementary Stream, H.222.0 sections 2.4.3.6 and 2.4.3.7): a header followed by the access unit's bytes. Then the PES packet is chopped into pieces of up to 184 bytes, and each piece goes into one TS packet of the video PID.

```
PES packet:  [ PES header | access unit bytes ............................ ]
TS packets:  [TS hdr|first piece] [TS hdr|piece] [TS hdr|piece] ... [TS hdr|last piece]
              PUSI = 1             PUSI = 0       PUSI = 0
```

The reason for the PES layer is the header: it carries the timestamps. Video pictures are not stored in the order they are shown (B-frames are decoded after the frames they refer to), so each picture needs two times:

- **DTS** (decoding time stamp): when to decode the picture.
- **PTS** (presentation time stamp): when to show it.

Without B-frames, decode order equals display order, so PTS equals DTS, and the stream usually carries only the PTS. Both are counted in ticks of a **90 kHz clock** (90000 ticks per second). The ES bytes inside a PES packet are exactly the bytes of the H.264 Annex B stream, so concatenating the payloads rebuilds the stream (chapter 1.4).

## 3. Concepts in detail

### 3.1 What you rely on from chapters 1.1 and 1.2

- A TS packet is 188 bytes: a 4-byte header, an optional **adaptation field**, then the payload. The adaptation field exists when `adaptation_field_control` (AFC, bits 5-4 of byte 3) is 2 or 3. Byte 4 is its length (not counting itself), so the payload starts at byte `5 + length` for AFC 3, at byte 4 for AFC 1, and does not exist for AFC 2. Encoders use adaptation fields to carry the PCR and to pad the last packet of a PES (stuffing), so you must skip them correctly: a wrong payload start corrupts the sizes.
- The **PID** is 13 bits: `((byte1 & 0x1F) << 8) | byte2`. The **video PID** is the elementary_PID of the stream with `stream_type 0x1B`, which you found in the PMT in chapter 1.2. (For a first test you may hard-code the PID that `ffprobe` reports; the harness checks the PID you print against `ffprobe`.)
- **PUSI** (`payload_unit_start_indicator`, bit 6 of byte 1). For PSI it meant "a section starts here". For PES it means "the first byte of this payload is the first byte of a new PES packet". A PES packet therefore begins exactly at the start of the payload of a PUSI = 1 packet (no pointer_field here, that exists only for PSI), and every following packet of the same PID with PUSI = 0 continues it.

### 3.2 Reassembly

For the video PID, keep a buffer. For each packet of that PID that has a payload (AFC 1 or 3):

1. **PUSI = 1:** the previous PES packet (if any) is complete. Finish it (parse it, emit a line). Start a new buffer with this payload.
2. **PUSI = 0:** if a buffer exists, append the payload. If none exists (the file starts in the middle of a PES packet), ignore the payload.

At the end of the file, finish the last buffer. Packets of other PIDs are skipped entirely. Packets with no payload (AFC 2, adaptation only) contribute nothing, and in particular do not end the PES.

Why PUSI is the boundary and not PES_packet_length: see 3.4. For video the length is usually 0, so the next PUSI is the only end marker you have.

Do not parse the header until the whole PES packet is collected. Real headers are 14 or 19 bytes and fit in the first TS packet, but a header can be up to 264 bytes, and collecting first is simpler and removes a class of bugs.

### 3.3 The PES header

A PES packet for video starts like this (all bytes are big-endian, numbering from the first byte of the PES packet):

| Byte | Bits | Field | Meaning |
|---|---|---|---|
| 0-2 | 24 | packet_start_code_prefix | Always `00 00 01`. If it is not, this is not a PES packet; skip it. |
| 3 | 8 | stream_id | `0xE0`-`0xEF` = video stream (usually `0xE0`). Audio is `0xC0`-`0xDF`. |
| 4-5 | 16 | PES_packet_length | Number of bytes after this field. 0 means unbounded (3.4). |
| 6 | 2 | marker | `10` |
| 6 | 2 | PES_scrambling_control | 0 = clear |
| 6 | 1+1+1+1 | priority, data_alignment_indicator, copyright, original_or_copy | Ignore. |
| 7 | 2 | PTS_DTS_flags | See 3.5. |
| 7 | 6 | ESCR, ES_rate, DSM_trick_mode, additional_copy_info, CRC, extension flags | Ignore. |
| 8 | 8 | PES_header_data_length | Number of bytes of optional fields that follow, *including* timestamps and any stuffing. |
| 9 ... | PES_header_data_length bytes | PTS (5 bytes), DTS (5 bytes) if present, other optional fields, stuffing | |
| 9 + header_data_length ... | | PES packet data | The ES payload. |

Important: the payload starts at byte `9 + PES_header_data_length`, **not** at a fixed offset. Always use the length byte. Different encoders pad the header differently. The fields after byte 8 that you do not decode (ESCR, extension) are skipped by this rule. A few special stream_ids (padding stream, program stream map and others) have no bytes 6-8 at all, but they never appear on the video PID.

### 3.4 PES_packet_length = 0

The 16-bit PES_packet_length counts the bytes that follow it, so a bounded PES packet is `6 + PES_packet_length` bytes in total. A video access unit can be larger than 65535 bytes (any I-frame at HD), so for video in a TS the spec allows the value **0**, which means "unbounded: the packet ends where the next PES packet starts". ffmpeg's muxer always writes 0 for video. Other encoders write real lengths for small frames.

Your rule:

- Length 0: the PES packet's data is everything up to the next PUSI on this PID (or the end of the file).
- Length N > 0: the PES packet is `6 + N` bytes. If you collected more bytes than that, the extra bytes are not part of the PES packet: drop them. If you collected fewer, use what you have.

The payload size is then (end of the PES packet) minus (`9 + PES_header_data_length`).

### 3.5 PTS_DTS_flags

The 2-bit field in byte 7 (bits 7-6) says which timestamps are in the header:

| Value | Meaning | Timestamps present |
|---|---|---|
| `00` | none | Neither. The picture has no timestamp of its own. This happens for non-first pictures when several pictures share a PES packet, and for some streams. |
| `01` | forbidden | Treat as no timestamps. |
| `10` | PTS only | PTS, 5 bytes at byte 9. DTS is implied equal to PTS. |
| `11` | PTS and DTS | PTS, 5 bytes at byte 9; DTS, 5 bytes at byte 14. |

Real files use 10 (no B-frames, or a picture whose DTS equals its PTS) and 11 (B-frame streams, where PTS differs from DTS). The decode order in the file is the order of the DTS values, which rise steadily; PTS jumps around.

### 3.6 The 33-bit timestamp layout

A PTS or DTS is 33 bits. It is spread over 5 bytes (40 bits) with **marker bits** (each always 1) between the groups so that a start code `00 00 01` can never appear by accident. The layout of the 5 bytes `b0..b4`:

| Byte | Bits (7 to 0) | Content |
|---|---|---|
| b0 | `PPPP` `v v v` `1` | `PPPP` = 4-bit prefix: `0010` for a PTS-only header, `0011` for the PTS and `0001` for the DTS in a PTS+DTS header. Then timestamp bits 32-30 (3 bits), then marker 1. |
| b1 | 8 bits | timestamp bits 29-22 |
| b2 | 7 bits + marker | timestamp bits 21-15 (top 7 bits of b2), then marker 1 |
| b3 | 8 bits | timestamp bits 14-7 |
| b4 | 7 bits + marker | timestamp bits 6-0 (top 7 bits of b4), then marker 1 |

So the value is: `((b0 >> 1) & 7) << 30  |  b1 << 22  |  (b2 >> 1) << 15  |  b3 << 7  |  (b4 >> 1)`.

Check this by hand. The first PES in an ffmpeg test file starts `00 00 01 e0 00 00 80 c0 0a 31 00 09 10 a1 11 00 07 d8 61`:
`stream_id e0`, length `0000` (unbounded), flags byte `80`, PTS_DTS_flags `c0 >> 6 = 3` (PTS and DTS), header data length `0a` = 10. The PTS bytes are `31 00 09 10 a1`: `0x31` is prefix `0011`, bits `000`, marker; `00`; `09 >> 1 = 4`; `0x10`; `0xa1 >> 1 = 0x50`. So PTS = `4 << 15 | 0x10 << 7 | 0x50` = 131072 + 2048 + 80 = **133200** ticks = 1.48 s. The DTS bytes `11 00 07 d8 61` give 126000 the same way. The payload starts at byte `9 + 10 = 19`, with `00 00 00 01 09 f0`, an Annex B start code and an access-unit delimiter NAL, as expected from H.264.

Because the field has only 33 bits, it wraps from 8589934591 (2^33 - 1) back to 0 after about 26.5 hours. Your reader prints the raw 33-bit value. (A real player must handle the wrap; this chapter does not, and the harness compares modulo 2^33 against ffprobe.) Timestamps in files usually start at a non-zero offset (ffmpeg's muxer starts near 1.4 s, 126000 ticks) so that DTS values, which precede PTS by the reorder delay, never go negative.

You may check the marker bits and prefix nibbles, but a robust reader just ignores them. A wrong marker usually means your offsets are wrong.

### 3.7 Payload size versus ffprobe's size

`ffprobe -show_packets` prints one row per PES packet of the video stream, and its `size` is the payload size from 3.4. ffprobe always prints a DTS: when the header has no DTS (flags 10), it prints DTS = PTS. Your output does not invent one; it prints `-` (section 5), and the harness treats `-` as "equal to the PTS" when comparing with ffprobe.

Units: `ffprobe -of csv` prints the raw pts and dts as integers in the stream's time base. For MPEG-TS this is `1/90000`, so the numbers are already 90 kHz ticks. (`pts_time` is the same value in seconds, which you do not want.)

Files written by ffmpeg put one access unit in each PES packet. A file from another muxer that packs several access units per PES may fail the ffprobe comparison while passing the harness's own parse, which is the definition for this chapter.

## 4. The project: exactly what to build

Write a command-line program (any language) that takes a `.ts` file path and an output path and:

1. Walks the packets (chapter 1.1 code) and finds the video PID (chapter 1.2 code; the PID of the first stream with `stream_type 0x1B`).
2. For the video PID only, reassembles PES packets from one PUSI to the next (3.2).
3. For each one parses the header (3.3): checks the `00 00 01` prefix (skip anything that fails, and skip a PES shorter than its own header), reads stream_id, PES_packet_length, PTS_DTS_flags, PES_header_data_length, decodes the PTS and DTS as in 3.5 and 3.6, and computes the payload size using 3.4.
4. Writes the output file described in section 5.

Verify by eye first:

- The number of PES lines is roughly the number of frames (50 for a 2 s, 25 fps clip). The first PES starts with an `00 00 00 01` start code and an AUD (`09 xx`) in its payload.
- DTS values rise by a constant step in decode order (3600 ticks for 25 fps, 90000 / 25). PTS values go forward and back when the file has B-frames. In a file without B-frames, DTS = PTS (flags 2, `-` in the DTS column).
- Your (PTS, DTS, size) triples equal the rows of
  `ffprobe -v error -select_streams v:0 -show_packets -show_entries packet=pts,dts,size -of csv=p=0 in.ts`
  (the first three columns; ignore trailing columns).
- A normal ffmpeg file only has PES_packet_length 0. The harness makes a variant with bounded lengths and one with huge timestamps (section 6). Run your reader on both.

## 5. Harness input format

The harness reads one UTF-8 text file written by your program. Lines are ASCII, space-separated, with `\n` or `\r\n` endings. Blank lines and lines starting with `#` are ignored. Line order does not matter (the `index` field carries the PES order), but the conventional order is as in the example below. Exactly one `VIDEO_PID` line and exactly one `PES_COUNT` line are required.

```
VIDEO_PID <pid>
PES_COUNT <n>
PES <index> <stream_id> <pes_packet_length> <pts_dts_flags> <pts> <dts> <payload_size>
```

- `VIDEO_PID`: exactly four lowercase hex digits, no `0x` (as in chapters 1.1 and 1.2), e.g. `0100`.
- `PES_COUNT`: decimal, the number of `PES` lines you write.
- One `PES` line for every PES packet on the video PID, in file order, 0-based `index` (0, 1, 2, ...) that must have no gaps. Include every PES packet that has a valid `00 00 01` prefix and a complete header, also ones without timestamps. Do **not** list the piece of a PES packet at the start of the file that began before the first PUSI (it is discarded, 3.2), nor units with a bad prefix.
- `stream_id`: exactly two lowercase hex digits, e.g. `e0`.
- `pes_packet_length`: decimal, the 16-bit field exactly as in the header (0 = unbounded).
- `pts_dts_flags`: one digit, 0-3, the 2-bit field exactly as in the header.
- `pts`, `dts`: decimal 90 kHz ticks (the raw 33-bit value, 0 to 8589934591), or the single character `-` when that timestamp is not present in the header. With flags 3 both are numbers; with flags 2 the PTS is a number and the DTS is `-`; with flags 0 or 1 both are `-`.
- `payload_size`: decimal, the number of ES bytes (3.4 and 3.7).

Example (start of a typical ffmpeg file with B-frames, then the start of a file without):

```
VIDEO_PID 0100
PES_COUNT 50
PES 0 e0 0 3 133200 126000 8075
PES 1 e0 0 3 144000 129600 3515
PES 2 e0 0 3 136800 133200 2542
PES 3 e0 0 3 140400 136800 2047
# ... PES 4 to 49 omitted here
```

```
VIDEO_PID 0100
PES_COUNT 50
PES 0 e0 0 2 126000 - 7993
PES 1 e0 0 2 129600 - 3549
```

In the first example, frame 0 (an I-frame) is shown at tick 133200 but decoded at 126000: the encoder reorders, and later frames are decoded at 3600-tick steps while shown in a different order.

## 6. Running the harness

```
python3 test_pes.py <input.ts> <your_output.txt>
python3 test_pes.py --make-variants <input.ts> <dir>    # writes <dir>/bounded.ts and <dir>/hightime.ts
python3 test_pes.py --self-test                         # checks the harness itself
```

Run it from this chapter's folder; it needs only Python 3 plus `ffmpeg`/`ffprobe` on `PATH`. Build test inputs from the repository root with `python3 -m common.testfiles T1 T5 T9_spatial default` (files appear in `.cache/`). Use at least one file with B-frames (`default`, `T8`, `T9_spatial`: PTS and DTS differ, flags 3) and one without (`T5`: PTS only, flags 2). Any ffmpeg-produced MPEG-TS with one access unit per PES works, including audio+video.

The harness finds the video PID with `ffprobe`, then performs two comparisons:

1. Its own reassembly and parse (sections 3.2 to 3.6) against every field of every `PES` line: stream_id, pes_packet_length, pts_dts_flags, pts, dts, payload_size, and the `VIDEO_PID`, `PES_COUNT` lines.
2. `ffprobe -show_packets` of the video stream (raw 90 kHz ticks) against your lines row for row: pts, dts (your dts, or your pts where it is `-`) and size, modulo 2^33.

The two variants:

- `bounded.ts`: every video PES whose length fits in 16 bits gets its true, non-zero `PES_packet_length` instead of 0. Your output must change only in the `pes_packet_length` column; everything else is the same as for the clean file. This catches readers that print 0 or their buffer length, or that compute the payload end wrongly from a non-zero length.
- `hightime.ts`: every PTS and DTS is increased by `0x180000000` (6442450944), so the top bits of the 33-bit fields are set. A reader that uses 32-bit integers, or mis-places the top 3 bits in byte 0, fails here. Values still fit in 33 bits (there is no wrap).

For each variant, run your reader on the variant file and then the harness on that same file.

Exit code 0 = PASS, 1 = FAIL (the first 10 mismatches with PES index, field, expected and actual, so you can see whether the whole list is shifted, or a single field is wrong), 2 = output file missing or malformed (the offending line is named).

## 7. Reading failures and debugging tips

- `number of PES lines` off by one: you either forgot to emit the last PES at the end of the file, or you emitted a leading fragment (data before the first PUSI). Remember that you finish a PES when the *next* PUSI arrives.
- Every `payload_size` off by the same small amount (for example 10 or 19 too large): you used a fixed header size instead of `9 + PES_header_data_length`, or you forgot that payload size means *ES bytes only*.
- `payload_size` off by a varying small amount: you included TS stuffing, or you start the payload at the wrong byte for AFC 3 packets (it is `5 + adaptation_field_length`, with the length byte counted).
- `pts` too large by a lot, or wrong in the low bits: the bit groups are 3 + 8 + 7 + 8 + 7, and the marker bit is the low bit of b0, b2 and b4. Shifting b2 or b4 right by 1 is what removes the marker. Do the hand example in 3.6.
- `pts` right but `dts` wrong: DTS is at byte 14, not 9, and its prefix is `0001`. Read a DTS only when flags = 3.
- `pts` correct for the clean file but wrong on `hightime.ts`: your integers are 32 bits wide (use 64-bit), or you drop bit 32.
- The ffprobe comparison fails but your own-parse comparison passes (or the reverse): the first mismatch row tells you whether the difference is in pts, dts or size. The input probably packs more than one access unit per PES, or has a damaged first or last packet; the own-parse comparison is the definition for this chapter.
- `pes_packet_length` differs in every row (you print non-zero numbers where the file has 0): you probably print the length of your buffer; print the 16-bit header field.
- To inspect by hand: find the first PUSI packet of the video PID, `xxd -g 1 -s <offset> -l 64 in.ts`, skip the 4-byte header plus adaptation field, and read `00 00 01 e0` followed by the fields in 3.3.

## 8. What's next

Chapter 1.4 writes the concatenated PES payloads to `out.264` and keeps your PTS/DTS list for later: `cmp out.264 ref.264` then proves that your reassembly and payload start offsets are exactly right.
