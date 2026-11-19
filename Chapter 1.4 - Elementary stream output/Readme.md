# Chapter 1.4 - Elementary stream output

## 1. Goal and the project

This is the last step of the demuxing phase. You already walk the TS packets (1.1), know the video PID (1.2) and collect each video PES packet with its timestamps (1.3). Now you write the result to disk: **`out.264`**, the concatenated PES payloads, which is a plain H.264 file. You also write the PTS/DTS list from chapter 1.3, unchanged, to keep it for Phase 9 (display timing).

The harness compares `out.264` byte for byte with what ffmpeg extracts from the same file. If they are identical, your demuxer is correct, and everything after this chapter can ignore transport streams.

## 2. Big picture

An H.264 encoder produces one byte stream. TS and PES only deliver it: the stream is cut into access units, wrapped in PES packets, cut into 184-byte pieces and interleaved with audio and tables. Demuxing is the exact inverse. If you remove every wrapper and keep only the pieces of the video stream, in order, you get back the encoder's output **bit for bit**. Nothing is added, converted or repaired.

```
TS packets of video PID --(drop TS headers, adaptation fields)--> PES packets
PES packets --(drop PES headers)--> access unit bytes --(concatenate)--> out.264
```

The file format of `out.264` is called **Annex B byte stream** (Annex B of the H.264 spec): NAL units, each preceded by a start code `00 00 01` or `00 00 00 01`. Phase 2 parses it. You do not need to understand it yet; you only need to know that the bytes are opaque and must be preserved exactly.

## 3. Concepts in detail

### 3.1 What to write

For each PES packet from chapter 1.3, in file order, append its payload: the bytes from offset `9 + PES_header_data_length` to the end of the PES packet (3.4 in chapter 1.3: to the next PUSI when `PES_packet_length` is 0, otherwise `6 + PES_packet_length` bytes in total). Nothing else goes into the file: no TS header, no adaptation field, no PES header, no separator, no padding.

Consequences worth knowing:

- Open the output file in **binary mode** (no newline conversion; the bytes `0a` and `0d` occur in video).
- Do not skip PES packets without a PTS. Their data still belongs to the stream.
- Do not parse the payload. You never look for start codes, SPS or NAL units in this chapter.
- Packets of other PIDs (audio, tables) never appear in the output.
- A PES that was only partly present at the beginning of the file (before the first PUSI) is discarded, as in 1.3.

### 3.2 Access unit delimiters (AUD)

An **access unit delimiter** (AUD) is a tiny NAL unit (`00 00 00 01 09 xx`, NAL type 9) that marks the start of an access unit. ffmpeg's TS muxer inserts one in front of every access unit that lacks it, so in a TS written by ffmpeg each PES payload begins with an AUD (usually `00 00 00 01 09 f0`). Streams from other tools may have none.

You copy AUDs like any other bytes. The reference `ref.264` (`ffmpeg -i in.ts -map 0:v:0 -c:v copy -f h264 ref.264`) is a pure pass-through as well: it adds no AUD and removes none, with or without B-frames, and also for a TS whose video has no AUDs at all. So the comparison is simply byte for byte. If your file differs from the reference only in AUDs, the bug is in your reader (for example a PES start was cut), not in the reference.

### 3.3 The PTS/DTS list

Keep the exact output of chapter 1.3: `VIDEO_PID`, `PES_COUNT`, one `PES` line per PES packet. The harness checks it against `ffprobe -show_packets`, and also checks that the `payload_size` values add up to the size of `out.264`. That sum check catches a list and a file written from different data. Phase 9 will use the list: PES line `i` belongs to access unit `i`, and its PTS and DTS give the display and decode times. The PES line index is the join key between this list and the access units you will find in `out.264`.

## 4. The project: exactly what to build

Extend your chapter 1.3 program (or write a wrapper around it) so that it takes `in.ts`, an output path for the stream and an output path for the list, and:

1. Does everything chapter 1.3 does.
2. Appends every PES payload, in order, to the stream file (binary mode).
3. Writes the list file in the chapter 1.3 format, exactly.

Verify (Summary step 1.4):

- `cmp out.264 ref.264` reports no difference, where `ref.264` comes from `ffmpeg -i in.ts -map 0:v:0 -c:v copy -f h264 ref.264`. The harness does this for you, with better diagnostics than `cmp`.
- Optional, by hand: `ffplay out.264` should play the video (it may play at the wrong speed, since a raw `.264` file has no timestamps; add `-framerate 25` or similar). If it shows garbage or nothing, check where the first byte differs. The harness never starts ffplay; this is your own manual check.

## 5. Harness input format

Two files written by your program, plus the input `.ts`.

- **`out.264`**: raw bytes, the concatenated PES payloads, nothing else. No header, no trailer.
- **`pes_list.txt`**: exactly the chapter 1.3 format (UTF-8, ASCII, space-separated lines; `#` lines and blank lines ignored):

```
VIDEO_PID <pid>
PES_COUNT <n>
PES <index> <stream_id> <pes_packet_length> <pts_dts_flags> <pts> <dts> <payload_size>
```

`pid` is four lowercase hex digits, `stream_id` two, the rest decimal, `pts`/`dts` are 90 kHz ticks or `-` when absent. Example:

```
VIDEO_PID 0100
PES_COUNT 50
PES 0 e0 0 3 133200 126000 8075
PES 1 e0 0 3 144000 129600 3515
```

See chapter 1.3 section 5 for the full rules (0-based indexes without gaps, `dts` is `-` for flags 2, and so on). This chapter's harness is a little more lenient than 1.3's: all eight fields must be present with the right syntax, but it does not compare the values of `stream_id` and `pes_packet_length`.

## 6. Running the harness

```
python3 test_es.py <input.ts> <out.264> <pes_list.txt>
python3 test_es.py --ignore-aud <input.ts> <out.264> <pes_list.txt>
python3 test_es.py --make-variants <input.ts> <dir>     # writes <dir>/noaud.ts and <dir>/padhdr.ts
python3 test_es.py --self-test                          # checks the harness itself
```

`--ignore-aud` accepts an `out.264` that differs from the reference only in access unit delimiters (3.2). Use it only if you strip AUDs on purpose; a normal demuxer never needs it.

Run it from this chapter's folder. It needs Python 3 and `ffmpeg`/`ffprobe` on `PATH`. Build inputs from the repository root with `python3 -m common.testfiles default T5 T9_spatial`.

What it compares:

1. `out.264` against `ref.264`, made with `ffmpeg -i in.ts -map 0:v:0 -c:v copy -f h264 ref.264` (inside a temporary directory). The result must be identical.
2. Your PES list against `ffprobe -show_packets` (pts, dts or your pts when `-`, and size, modulo 2^33), and the sum of your `payload_size` values against the size of `out.264`.

The two variants stress cases the plain files do not:

- `noaud.ts`: the video has no access unit delimiters (the reference then has none either, which confirms that the comparison does not expect them).
- `padhdr.ts`: the PES headers carry 7 bytes of stuffing after the timestamps (a larger `PES_header_data_length`), all PES packets have a real `PES_packet_length`, and the TS packetisation is different (adaptation-field stuffing in the last packet of each PES). A reader that assumes a 14- or 19-byte header fails here.

Run your reader on the variant file, then the harness on that same file.

Exit codes: 0 = pass, 1 = fail, 2 = a file is missing or the list is malformed.

## 7. Reading failures and debugging tips

On a difference the harness prints the two sizes, the first differing offset, a hex window of both files (16 bytes before the difference, then 16 bytes from it), the NAL unit around it, which reference PES packet (index as in your list) the offset falls in and how far into its payload, and an analysis of how the files re-synchronise (bytes inserted into or missing from yours). Use the **size difference** and the **inserted or missing bytes** to find the bug.

| What you see | Likely cause |
|---|---|
| Yours is shorter by about 184 bytes (or 183, 182...), and the missing bytes are in the middle of the stream | A dropped TS packet. Typical: skipping packets whose PUSI is 0 when no buffer exists yet, mishandling a packet with AFC 3 (adaptation field plus payload), treating a transport_error or continuity-counter jump as a reason to skip. |
| Yours is longer by about 184 bytes, the inserted bytes duplicate the previous data | A packet processed twice (for example appended once on arrival and again when the PES is flushed). |
| The inserted bytes start with `00 00 01 e0` | The PES header was copied. The payload starts at byte `9 + PES_header_data_length`, and the header is not part of the stream. The inserted count is the header size (14 or 19 with timestamps). |
| The inserted bytes start with `47` | A TS header was copied into the data. |
| The inserted bytes are `ff ff ...` or `00 00 ...` | The adaptation field (stuffing) was not skipped: payload starts at `5 + adaptation_field_length` when AFC is 3. Stuffing in the PES header is copied when the header length is wrong. |
| A few bytes (up to about 40) are missing at the start of a payload, again and again | Payload start too late: the header length was counted twice, or the adaptation field length was over-counted. |
| Only the last bytes differ, or the file is shorter at the end | The last PES was never flushed at the end of the file (the harness says your file ends exactly after a PES), or its last packets are lost. |
| The difference is at byte 0 to 3 of a PES payload | Wrong payload start offset; the inserted or missing count tells you by how much (5 or 10 bytes inserted: the PTS/DTS bytes were copied). |
| Differences only at `00 00 00 01 09 f0` | An access unit delimiter was damaged. See 3.2. |
| Files differ in the middle and never re-synchronise | Wrong PID in part of the file (an audio packet mixed in), or a text-mode write. |
| Many bytes differ, starting at offset 0 | Wrong PID chosen, or the payload offset is wrong from the first packet. |
| `out.264` is identical, but the list check fails | The error is in chapter 1.3 logic: fix timestamps or sizes there. |
| Everything matches but the sum check fails | The list was written from different data than the file (for example before trimming a PES to its `PES_packet_length`). |

To look at bytes yourself: `cmp -l out.264 ref.264 | head` lists the first differing offsets (1-based), and `xxd -s <offset> -l 64 out.264` shows the area.

## 8. Phase 1 recap

Phase 1 turned a transport stream into an elementary stream:

- 1.1 split the file into 188-byte packets and read each header (PID, PUSI, adaptation field, continuity counter).
- 1.2 reassembled and CRC-checked the PAT and PMT sections to learn which PID is the H.264 video.
- 1.3 reassembled that PID's payloads into PES packets, and decoded PTS and DTS.
- 1.4 stripped the PES headers and wrote the payloads to `out.264`, keeping the PTS/DTS list.

You now have two artefacts that every later phase relies on: `out.264`, an Annex B H.264 stream identical to ffmpeg's, and `pes_list.txt`, the timing of each access unit. Neither contains a single decoded pixel yet, and nothing below this point depends on MPEG-TS.

**What Phase 2 does with `out.264`:** it scans the file for start codes (`00 00 01`), splits it into NAL units, parses each 1-byte NAL header (type, `nal_ref_idc`), removes emulation prevention bytes to get RBSPs, and builds the bit reader (`u(n)`, `ue(v)`, `se(v)`). The first check there is that your NAL list matches ffmpeg's `trace_headers` log for the same file, which only works because `out.264` is exact.

## 9. What's next

Phase 2 (chapter 2.1) splits `out.264` into NAL units.
