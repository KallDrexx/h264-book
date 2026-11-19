# Chapter 1.2 - PSI tables: PAT and PMT

## 1. Goal and the project

Chapter 1.1 gave you every packet of a `.ts` file, labelled with its PID. But which PID carries the video? The file itself answers that question, in two small tables that are repeated throughout the stream. In this chapter you write a program that reassembles those tables from packet payloads, checks them with a CRC-32, and reads off the program number, the PMT PID and the video PID. Chapter 1.3 will use the video PID to pull out the video data.

## 2. Big picture

A transport stream has no file header. A receiver can tune in at any moment, so the "directory" of the stream is sent again and again, about ten times a second. This directory is made of **PSI tables** (Program Specific Information), carried in the same 188-byte packets as everything else. Two tables matter to us:

- The **PAT** (Program Association Table) lives on the fixed PID `0x0000`. It lists the programs in the stream and, for each, the PID of its PMT.
- The **PMT** (Program Map Table) lives on the PID the PAT named. It lists the elementary streams (video, audio, ...) of one program, each with a PID and a `stream_type` that says what kind of stream it is. `stream_type 0x1B` means H.264.

So the lookup chain is: PID 0 -> PAT -> PMT PID -> PMT -> video PID. A **program** is one service, such as one TV channel; its **program number** is a 16-bit label. A typical file has one program.

Tables are not sent as raw packets. They are sent as **sections**: self-describing byte blocks with a length and a checksum, laid into packet payloads. A section can be small (the PAT is about 16 bytes) or span several packets (a PMT with many streams and descriptors), and one packet can hold the end of one section and the start of the next. Reassembling sections correctly is the main work of this chapter. The CRC at the end of each section lets you prove you got it right: if the CRC passes, every byte arrived and was put together in the right order.

Specs: ITU-T H.222.0 / ISO 13818-1, section 2.4.4 (PSI: 2.4.4.1 pointer_field and sections, 2.4.4.3 PAT, 2.4.4.8 PMT, Annex A for the CRC and the `stream_type` table, Table 2-34).

## 3. Concepts in detail

### 3.1 Recap from chapter 1.1 (what you rely on)

- A packet is 188 bytes: 4-byte header, optional adaptation field, then payload. The header holds the **PID** (13 bits: `((byte1 & 0x1F) << 8) | byte2`), the **PUSI** flag (`payload_unit_start_indicator`, bit 6 of byte 1) and the 2-bit `adaptation_field_control` (AFC, bits 5-4 of byte 3).
- If AFC is 1 there is no adaptation field: the payload is bytes 4 to 187. If AFC is 3, byte 4 is `adaptation_field_length` and the payload starts at byte `5 + length`. If AFC is 0 or 2 there is no payload.
- **Payload extraction** is that rule: the bytes after the header and adaptation field. Everything in this chapter works on payloads.
- For PSI, **PUSI = 1 means "a section starts in this packet"** (or, at least, that the packet begins with a pointer_field, see 3.2). PUSI = 0 means the payload is pure continuation of the section in progress.

Your chapter 1.1 code (packet walking, header parsing, adaptation skipping) is the foundation. You may drop resync handling here: the harness inputs for this chapter are clean 188-byte files.

### 3.2 Sections and pointer_field

A section starts with a **table_id** byte that says which table it is. The ones you need:

| table_id | Meaning |
|---|---|
| `0x00` | PAT (only on PID 0) |
| `0x02` | PMT |
| `0x42` | SDT (service names, on PID `0x0011`; ignored here) |

Some other `table_id`s exist; ignore them. `0xFF` is never a real table_id: it is the stuffing byte.

Where does a section begin inside a packet payload? A section can start in the middle of a payload, because the previous section's last bytes may come first. The payload of a packet with PUSI = 1 therefore starts with one extra byte, the **pointer_field**: the number of bytes between the pointer_field byte itself and the first byte of the new section. Concretely, for a PUSI = 1 payload `P`:

- `P[0]` is the pointer_field, call it `n`.
- `P[1 .. n]` (n bytes) are the final bytes of the previous section, if there was one still incomplete.
- `P[1 + n ..]` is where the next section starts (its table_id is `P[1+n]`).

Most of the time `n = 0`: the section starts immediately after the pointer_field. A non-zero `n` is legal and happens in the wild when a section is split across packets and the next one is packed right behind it. Payloads with PUSI = 0 have **no** pointer_field: all bytes are continuation.

After the end of the last section in a packet, the rest of the payload is filled with `0xFF` bytes. Because `0xFF` is never a table_id, seeing `0xFF` where a table_id should be means "no more sections in this packet".

### 3.3 Section layout (long form)

PAT and PMT use the "long" section form. All values are big-endian, bits are listed most significant first.

| Bits | Field | Meaning |
|---|---|---|
| 8 | table_id | See 3.2. |
| 1 | section_syntax_indicator | 1 for PAT and PMT. |
| 1 | (zero / private bit) | 0 here. |
| 2 | reserved | Ignore. |
| 12 | section_length | Number of bytes that follow this field, **including the 4-byte CRC**. Max 1021 for PSI. |
| 16 | table_id_extension | PAT: transport_stream_id. PMT: program_number. |
| 2 | reserved | Ignore. |
| 5 | version_number | Increments when the table content changes. |
| 1 | current_next_indicator | 1 = applies now. (ffmpeg always writes 1.) |
| 8 | section_number | 0 unless the table is split over several sections. |
| 8 | last_section_number | Same. |
| ... | table-specific data | PAT loop or PMT body, see below. |
| 32 | CRC_32 | Checksum of everything before it, see 3.4. |

So the **total size of a section** is `3 + section_length` bytes (table_id plus the two bytes holding section_length, plus what follows). The fixed part before the table-specific data is 8 bytes (bytes 0 to 7 of the section). Your reassembly needs only this: read table_id, read section_length from bytes 1-2 (mask the top nibble: `((b1 & 0x0F) << 8) | b2`), then collect `3 + section_length` bytes.

**PAT body** (between byte 8 and the CRC): a list of 4-byte entries.

| Bits | Field |
|---|---|
| 16 | program_number |
| 3 | reserved |
| 13 | PID |

If `program_number` is 0, the PID is the **network PID** (not a program; skip the entry). Otherwise the PID is that program's **PMT PID**. The body is `section_length - 9` bytes long (section_length minus the 5 fixed bytes after it and the 4-byte CRC), so it holds `(section_length - 9) / 4` entries.

**PMT body:**

| Bits | Field |
|---|---|
| 3 | reserved |
| 13 | PCR_PID (PID carrying the clock reference of this program) |
| 4 | reserved |
| 12 | program_info_length (bytes of program descriptors that follow) |
| ... | `program_info_length` bytes of descriptors (skip them) |

then a loop that runs until 4 bytes (the CRC) remain:

| Bits | Field |
|---|---|
| 8 | stream_type |
| 3 | reserved |
| 13 | elementary_PID |
| 4 | reserved |
| 12 | ES_info_length |
| ... | `ES_info_length` bytes of descriptors (skip them) |

Descriptors are tagged blobs (language of an audio track and the like); you only need their lengths to skip them. The first byte of the body is section byte 8, so `program_info_length` is found at section bytes 10-11, and the stream loop begins at section byte `12 + program_info_length`. Common `stream_type`s: `0x1B` H.264/AVC video, `0x02` MPEG-2 video, `0x24` H.265, `0x0F` AAC audio (ADTS), `0x03`/`0x04` MPEG audio, `0x81` AC-3 (user-private assignment, common in ATSC).

### 3.4 CRC-32/MPEG-2

The last four bytes of every section are a CRC (cyclic redundancy check): a checksum that detects damaged or missing bytes. The variant is **CRC-32/MPEG-2** (H.222.0 Annex A):

- Polynomial `0x04C11DB7`.
- Register starts as `0xFFFFFFFF`.
- Bytes are processed most significant bit first (**not reflected**, unlike the zlib/PNG CRC-32).
- No final XOR (the register is not inverted at the end).

Bitwise algorithm, per input byte: XOR the byte into the top 8 bits of the 32-bit register, then do 8 times: if the top bit is 1, shift left by one and XOR with the polynomial; else just shift left by one. (A 256-entry table speeds this up; the bitwise form is fast enough here.)

The encoder computes the CRC over the section bytes from table_id up to, not including, the CRC field, and appends it big-endian. The receiver can do something simpler: run the same CRC over the **entire** section including the 4 CRC bytes. For an undamaged section the result is exactly **0**. That is your check: `crc_ok = (CRC over all 3 + section_length bytes == 0)`.

A reference check value: this CRC over the ASCII bytes `123456789` is `0x0376E6E7`. Use it as a unit test before touching any file.

### 3.5 Reassembling sections from packets

Keep, for each PID you care about, a **section buffer** (bytes of the section in progress, starting at its table_id) that may be empty. For each packet of that PID that has a payload:

1. **PUSI = 1:** let `n = payload[0]`.
   - If the buffer is not empty, append `payload[1 .. n]` to it and see whether the section is now complete (below). Whatever is in the buffer afterwards is discarded: a new section is starting, so a section that is still short is incomplete (damaged), and it is dropped.
   - Start a fresh buffer with `payload[1 + n ..]`.
2. **PUSI = 0:** if the buffer is not empty, append the whole payload. If the buffer is empty (you tuned in mid-section), ignore the packet.
3. After each append, **extract complete sections** from the buffer, repeatedly:
   - If the buffer is empty: stop.
   - If its first byte is `0xFF`: it is stuffing; empty the buffer and stop.
   - If it holds fewer than 3 bytes: stop (need more data to learn section_length).
   - Let `total = 3 + section_length`. If the buffer holds at least `total` bytes: the first `total` bytes are one complete section; emit it, remove those bytes from the buffer, and repeat (another section may follow immediately). Otherwise stop and wait for the next packet.
4. Packets with AFC 0 or 2 have no payload and are skipped; they do not affect the buffer.

Notes: several sections can sit in one packet (small PMTs for several programs on one PID is the usual case); one section can span many; a section starting in packet N with PUSI = 1 can finish in packet N+1 after the pointer_field of N+1's own new section, if the pointer_field there is non-zero. Do not rely on packet counts, always use `section_length`.

Which PIDs to reassemble: PID `0x0000` always, plus every PMT PID found in the PAT, **once you have a valid PAT** (3.6). Packets on a PMT PID that appear before the first valid PAT are ignored. All other PIDs (video, audio, SDT, ...) are not touched in this chapter.

### 3.6 Using the sections

PSI repeats, so you see the same PAT dozens of times and the same PMT again and again. Make the following rules, which the harness follows too, so that results are identical for everyone:

- Every completed section from a tracked PID is **listed** in your output with its `crc_ok` flag (section 5), in the order the sections complete.
- A section with `crc_ok = 0` is **discarded**: it must not be used for anything else. This is the point of the CRC: damaged data is never trusted.
- The **first** CRC-valid PAT section (table_id `0x00` on PID 0) defines the programs: one program per entry whose program_number is non-zero, with its PMT PID. Later PATs are listed but ignored. (A PAT split over several sections via section_number is rare and not handled.)
- For each program, the **first** CRC-valid PMT section (table_id `0x02`, on that program's PMT PID, with table_id_extension equal to the program number) defines its streams. Later PMTs are listed but ignored.
- The **video PID** is the elementary_PID of the first stream with `stream_type 0x1B`, searching programs in ascending program number and each PMT's streams in their order. If there is none, there is no video PID.

Real-world note for later: a PMT's `version_number` changes when streams change mid-stream, and decoders must then re-read it. We ignore that: first valid table wins.

## 4. The project: exactly what to build

Write a command-line program (any language) that takes a `.ts` file path and an output path and:

1. Walks the packets (chapter 1.1 code), extracting each packet's PID, PUSI and payload.
2. Reassembles sections on PID 0 per 3.5, honouring pointer_field, and parses the PAT (3.3, 3.6) to find the PMT PID(s).
3. Reassembles sections on each PMT PID and parses the PMT; find the stream with `stream_type 0x1B`.
4. Implements CRC-32/MPEG-2 (3.4), unit-tested with `123456789 -> 0x0376E6E7`, and checks **every** section you reassemble.
5. Writes the output file described in section 5.

Verify by eye first:

- Every `SECTION` line has `crc_ok` 1 on a clean file. This also proves your reassembly: one wrong byte anywhere gives CRC 0.
- Your `PROGRAM`, PMT PID and `VIDEO_PID` equal what `ffprobe -show_programs in.ts` prints (`program_id`, `pmt_pid`, and the stream `id`, which ffprobe prints in hex, e.g. `0x100`).
- A clean file never exercises CRC failure or multi-packet sections. The harness makes two variants for you (section 6): one with a deliberately damaged CRC, one with large PMTs split over packets with a non-zero pointer_field. Run your reader on both.

## 5. Harness input format

The harness reads one UTF-8 text file written by your program. Lines are ASCII, space-separated, with `\n` or `\r\n` endings. Blank lines and lines starting with `#` are ignored. There are four line types. The harness does not care about the order of lines in the file (the `index` field carries the SECTION order), but writing all SECTION lines first, as in the example, is conventional.

```
SECTION <index> <pid> <table_id> <section_length> <crc_ok>
PROGRAM <program_number> <pmt_pid>
STREAM <program_number> <elementary_pid> <stream_type>
VIDEO_PID <pid or none>
```

**SECTION**: one line for every complete section assembled on a tracked PID (PID `0000` and every PMT PID from the first valid PAT), in completion order, including repeats and including sections whose CRC failed.

- `index`: 0-based position in that order (0, 1, 2, ...). The harness matches by index, so a missing or extra section shifts everything after it, which makes the failure easy to spot.
- `pid`: exactly four lowercase hex digits, no `0x` (e.g. `0000`, `1000`). The PID the section arrived on.
- `table_id`: exactly two lowercase hex digits (`00`, `02`).
- `section_length`: decimal, the 12-bit field value (so total bytes = value + 3).
- `crc_ok`: `1` if the CRC check passed, `0` if not.

"Completion order" means the order in which the last byte of each section is received (so a section whose tail arrives in a later packet is listed at that later point). A section that was started but never completed (cut off by a new PUSI, or by the end of the file) is not listed.

**PROGRAM**: one line per program in the first valid PAT, excluding program_number 0. `program_number` is decimal, `pmt_pid` is four lowercase hex digits. Order is free.

**STREAM**: one line per elementary stream in the first valid PMT of each program. `program_number` decimal, `elementary_pid` four lowercase hex digits, `stream_type` two lowercase hex digits (`1b`). Order is free.

**VIDEO_PID**: exactly one line: four lowercase hex digits, or the word `none`.

Example (a clean ffmpeg file with a video and an audio stream, abridged):

```
SECTION 0 0000 00 13 1
SECTION 1 1000 02 23 1
SECTION 2 0000 00 13 1
SECTION 3 1000 02 23 1
# ... one PAT/PMT pair roughly every 100 ms
PROGRAM 1 1000
STREAM 1 0100 1b
STREAM 1 0101 0f
VIDEO_PID 0100
```

Here `section_length` 13 is the PAT: 5 bytes of header after the length field, one 4-byte entry, 4 bytes CRC.

## 6. Running the harness

```
python3 test_psi.py <input.ts> <your_output.txt>
python3 test_psi.py --make-variants <input.ts> <dir>     # writes <dir>/crcbad.ts and <dir>/split.ts
python3 test_psi.py --self-test                          # checks the harness itself
```

For each variant, run your reader on the variant file and then the harness on that same file, e.g. `python3 test_psi.py <dir>/split.ts split_out.txt`.

Run it from this chapter's folder; it needs only Python 3 plus `ffmpeg`/`ffprobe` on `PATH`. Build test inputs from the repository root with `python3 -m common.testfiles T1 T12 default` (files appear in `.cache/`). Any ffmpeg-produced 188-byte MPEG-TS works, including audio+video.

The harness re-does the whole job itself (section reassembly per 3.5, CRC per 3.4, table use per 3.6) and compares: every SECTION line (pid, table_id, section_length, crc_ok), the PROGRAM and STREAM sets and VIDEO_PID. It then cross-checks program number, PMT PID, elementary PIDs and the H.264 PID independently against `ffprobe -show_programs`.

The two variants:

- `crcbad.ts`: one bit flipped in the CRC of the second PAT and one bit in the body of the second PMT. Your output must show exactly those two sections with `crc_ok 0` (and everything else 1), and your PROGRAM/STREAM/VIDEO_PID results must be unchanged because the damaged tables are ignored.
- `split.ts`: every PMT is padded with descriptors to about 400 bytes so each spans three packets, and the last 6 bytes of each PMT section (including its CRC) are delivered at the start of the *next* PMT's first packet, behind a pointer_field of 6. The final section's tail follows in its own packet. All CRCs must pass, so this tests spanning, pointer_field and PUSI = 0 continuation together. Note that ffmpeg's own demuxer also reads this file correctly.

Exit code 0 = PASS, 1 = FAIL (the first 10 mismatches with index, field, expected and actual), 2 = output file missing or malformed (the offending line is named).

## 7. Reading failures and debugging tips

- `number of SECTION lines` too small, and CRC fine on the ones you have: you probably stop at the first section in a packet or drop a section starting after a pointer_field.
- All `crc_ok` are 0: check the CRC itself. Non-reflected, init `0xFFFFFFFF`, no final XOR. Do the `123456789` unit test. Also check you CRC `3 + section_length` bytes, not `section_length`.
- Only the PAT is CRC-bad: you may be including the pointer_field byte in the section, or reading from the wrong offset. The section starts at `payload[1 + pointer_field]`.
- `split.ts` fails but the clean file passes: you do not handle PUSI = 0 continuation, or you ignore the non-zero pointer_field's head bytes (`payload[1 .. n]` belong to the *previous* section), or you forget that the buffer must survive across packets.
- `section_length` differs from the harness: you included the pointer_field, or masked only 8 bits of the length. It is a 12-bit field.
- Wrong VIDEO_PID but right PROGRAM: you are probably taking the PCR_PID (bytes 8-9 of the PMT body) or an off-by-one in the descriptor-skipping arithmetic. Print the PMT bytes and parse by hand: `PCR_PID` at section bytes 8-9, `program_info_length` at 10-11, the first stream at `12 + program_info_length`.
- To inspect a PAT by hand, dump the second packet of an ffmpeg file (the first is the SDT, as chapter 1.1 showed): `xxd -g 1 -s 188 -l 32 f.ts`. Expect `47 40 00 1x 00 00 b0 0d ...`: packet header, pointer_field 0, table_id 0, then `b0 0d` = syntax bit, reserved bits and section_length 13.

## 8. What's next

Chapter 1.3 takes the video PID you just found, collects its packet payloads from one PUSI to the next, and decodes the PES header to get each frame's PTS and DTS.
