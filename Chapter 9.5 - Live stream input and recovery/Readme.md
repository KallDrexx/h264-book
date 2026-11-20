# Chapter 9.5 - Live stream input and recovery

## 1. Goal and the project

Until now your player read a clean `.ts` file from start to end. A live stream is joined in the middle, loses packets, restarts and changes picture size. In this chapter your player learns to read **stdin or a UDP socket**, to **start where decoding is possible**, to **recover from damage**, and to **follow an SPS change**. It must never crash and never show garbage.

The project is a headless player mode that writes a **frame log**: one line per presented frame (PTS, size, real or concealed, md5 of the picture). The harness feeds it 34 damaged streams and checks the logs.

## 2. Big picture

* **Live means the start is arbitrary.** You may join after any packet. Until you have the tables (PAT/PMT, Chapter 1.2) and a picture that decodes on its own, you show nothing.
* **Damage is normal.** UDP loses and duplicates packets, encoders restart. Each layer needs a rule for bad input that hands one clean signal upwards: "this access unit is unusable".
* **One cure: the next IDR.** After an error, P and B pictures reference damaged pictures and cannot be trusted. Until the next IDR you **conceal**: repeat the last good frame, so the picture freezes instead of breaking up.
* **A new SPS is a new video.** The sequence parameters can change at an IDR; the DPB, frame buffers and window must then be rebuilt.

## 3. Concepts in detail

### 3.1 Input and TS sync

Read stdin or UDP **incrementally**; never assume that what you read is aligned to 188 bytes (UDP datagrams usually hold 7 packets = 1316 bytes, but nothing guarantees it, and a pipe delivers arbitrary pieces). Keep a byte buffer.

* **Sync rule.** A packet starts where byte 0 is `0x47` and the byte 188 bytes later is also `0x47`. When that fails, you have lost sync: scan forward for a byte `0x47` that is confirmed by `0x47` at +188 **and** +376 (one 0x47 inside garbage or payload proves nothing).
* **Do not lose good packets.** The packet before a lost-sync region counts only if its 188 bytes are complete: if the next confirmed sync byte is at least 188 bytes away, the earlier packet was whole (garbage came *between* packets, keep it); if it is closer, that packet was cut short, so drop it. Garbage bytes between packets must cost you nothing.
* A final partial packet at end of input is dropped. At end of input (EOF on stdin, or no datagram for the idle timeout) process what you have and **flush** (section 3.5).
* The input may be any bytes at all (empty, text, random). That is not an error: exit status 0, no frames.

### 3.2 Packet-level rules (H.222.0 2.4.3)

For each packet (PID 0x1FFF = null: ignore):

| Condition | Rule |
| --- | --- |
| `transport_error_indicator` = 1 | The packet is unusable: treat it as lost. Take its CC as the new "previous" value, so the next packet does not report a second loss. |
| `continuity_counter` (CC) | Only packets that carry payload (adaptation_field_control 01 or 11) increment it; skip the check for adaptation-only packets. Expected = previous + 1 mod 16; any other value means packets were lost on that PID. |
| `discontinuity_indicator` = 1 (first flag bit of the adaptation field) | The sender announces a restart: accept any CC on this packet, no loss. (A sender that is killed and restarted usually does *not* set it; then you see an ordinary CC jump.) |
| Same CC as the previous packet on the PID, payload present, **identical bytes** | A **duplicate** (allowed once by H.222.0): drop the copy, never count it as a loss. A third identical copy is a CC error. |
| Lost packets on the video PID | The PES being assembled is **damaged**. Do the CC check *before* acting on `payload_unit_start_indicator`: a gap seen on the packet that starts a new PES damages the old PES, not the new one. |
| Lost packets on PAT/PMT | Drop the half-built section. |

A damaged PES is not thrown away silently: its access unit still has to be reported, as concealed (section 3.5).

### 3.3 Tables, PIDs and PES

* **Wait for the tables.** Before the first valid PAT and PMT you do not know the video PID: ignore all video packets, even if one looks like a PES start. Check the CRC-32 of every section and ignore bad ones. Only sections with `current_next_indicator` = 1 count.
* **PMT version change** with the same video PID changes nothing for decoding: do not flush, do not wait for an IDR.
* **PID change** (a new PMT names another video PID): the last PES of the old PID is complete, so hand it on; then follow the new PID. Do not reset the decoder (the new stream usually starts with an IDR anyway).
* **First PES.** Discard video packets until the first one with `payload_unit_start_indicator` = 1. The PES that ends at the end of input may be incomplete: the slice decoder will notice (section 3.5).
* A PES whose start was lost never produces an access unit and never appears in the log.

### 3.4 Where decoding may start

Decoding may begin only at a **start point**, and only once the SPS and PPS it refers to have arrived. Every access unit before the first start point is dropped silently (no frame lines), even if its slices parse: a P or B picture without its references is garbage. There are two kinds of start point:

1. **An IDR picture** (nal\_unit\_type 5).
2. **A recovery point**: a non-IDR access unit that carries a recovery point SEI and whose slices are all I slices (slice\_type 2 or 7). Open-GOP encoders (x264 `open-gop=1`) code every GOP after the first this way, so for such a stream it is the only start point you will ever get.

**Finding the SEI.** An SEI NAL unit (type 6, syntax 7.3.2.3.1, after removing emulation prevention bytes) holds one or more messages followed by the trailing byte 0x80. Each message is: payloadType (add 255 for every 0xFF byte, then add the next byte), payloadSize (coded the same way), then payloadSize bytes of payload. A recovery point is payloadType 6 (Annex D, "recovery point SEI message"):

| Field | Coding | Use here |
| --- | --- | --- |
| `recovery_frame_cnt` | ue(v) | N. Decode from the start point, but show nothing until frame\_num has advanced by N (mod MaxFrameNum). The harness streams use N = 0: the I picture itself is shown. |
| `exact_match_flag` | u(1) | 1: the output from the recovery point on is identical to decoding from an earlier IDR; 0: only approximately. Informational: start either way. |
| `broken_link_flag` | u(1) | 1: the stream was edited (spliced) here, so pictures decoded *across* this point from an earlier start may be broken. Informational for a joiner, which starts here anyway. |
| `changing_slice_group_idc` | u(2) | FMO only (out of scope): ignore. |

**Leading pictures.** After a recovery point start, the B pictures that follow the I picture in decoding order but come before it in output order (smaller POC) are *leading pictures*: they also reference pictures from before the join, which you never had. Discard them (no frame lines). To compare POCs, compute the recovery picture's POC as if it were an IDR (PicOrderCntMsb = 0, 8.2.1.1) and continue normally from there; every later picture with a smaller POC is a leading picture. Leading pictures of *later* recovery points are decodable (their references exist) and are shown normally.

### 3.5 Errors, concealment, resync and SPS changes

**What counts as an error.** (a) A damaged PES (section 3.2; a packet lost to a sync loss is a lost packet too); (b) a slice NAL unit your decoder rejects (truncated, garbage, an unparsable header, an unknown parameter set id, a missing reference picture).

**What an error does:**

1. **Flush** the DPB: every picture already decoded correctly is output now, in POC order (bumping, Chapter 9.1). Nothing decoded earlier is lost.
2. Enter the **concealment state**. For the failed access unit and every following one up to the next undamaged IDR, write one `concealed` frame line: the access unit's own PTS, and the **previous output frame repeated unchanged** (same size, same md5). Do not decode these access units, even if they look healthy: their references are damaged. If nothing has been output yet, there is nothing to repeat: write no line.
3. **Resync at the next undamaged IDR**: leave the concealment state and decode normally; from there on the output is bit exact again. If no IDR comes, you conceal until the end. "Recovery within one GOP" means exactly this.

Concealed lines appear in **decoding order** (nothing is reordered, they are not decoded). NAL unit types you do not handle (filler 12, SPS extension 13, auxiliary slice 19, reserved, unspecified 0 and 24..31, and any NAL unit with `forbidden_zero_bit` = 1) are ignored completely; they never end an access unit and never cause an error.

**Flush at the end** (EOF, idle timeout): output everything still held.

**SPS changes.** Keep SPSs and PPSs by id, but the **active** SPS changes only when an IDR picture is activated (a new SPS id through the PPS, or the same id with different content: compare the bytes). At that IDR: (1) flush and output everything of the old sequence (the spec lets a decoder infer no\_output\_of\_prior\_pics\_flag = 1 when the size changes, C.4.4; this book outputs them, and the harness expects them), (2) activate the new SPS, (3) rebuild the DPB and frame buffers (size from 9.1's rules), (4) if the cropped size changed, notify the window with a `resize` line (and resize it in your GUI) **before** the first new frame. A repeated SPS with identical content does nothing; a size notification is sent **exactly when the size changes** (and before the very first frame).

### 3.6 What the harness expects of each stream

The generator knows which access units (AUs) are untouched. For every AU whose PES start survives the damage it records a class:

| Class | Meaning | Check |
| --- | --- | --- |
| `real` | Decodable exactly (before the damage, or after a clean IDR) | A `real` line with this PTS, correct size, **md5 equal to ffmpeg's frame for that PTS**; real lines in output order. |
| `conceal` | Surely damaged, or after a surely damaged AU and before the next IDR | A `concealed` line with this PTS. |
| `may` | Corrupted payload (CC intact): a decoder might or might not notice | Absent, concealed or real is accepted; md5 not checked. |
| (none) | Before the start point, a discarded leading picture, or its PES start was lost | **No line** with this PTS. |

Also checked: every concealed line repeats the previous line's md5 and size; indices count 0, 1, 2, ...; the `resize` rule above; exit status 0; the run ends before the timeout. Because each stream has several IDRs, a player that resumes late fails on the `real` entries it skipped.

## 4. The project: exactly what to build

A command `PLAYER` (any language) with the contract of section 5.1. Build, in this order:

1. **Input** (3.1, 3.2): stdin and UDP readers feeding one incremental TS parser. Cases: `clean`, `garbage_*`, `dup_packets`.
2. **Tables and PES** (3.3): `no_tables_start`, `pmt_version`, `pid_change`, `start_mid_pes`.
3. **Start points** (3.4): `start_at_p`, `start_open_gop`.
4. **Errors** (3.5): `drop_*`, `tei`, `short_packet`, `trunc_*`, `corrupt_*`, `garbage_nal`, `junk_nals`, `all_garbage`, `empty_input`.
5. **SPS changes** (3.5): `res_*`, `sps_*`, `restart_*`.
6. The UDP runs and the manual live check (section 7).

Your player must exit by itself at end of input, never wait on a socket without a timeout, and never crash on any byte sequence: bounds-check every parse.

Verify: the harness (damaged files, pipe and UDP runs), then the manual `ffmpeg -re` workflow of section 7: join mid-stream, kill and restart the sender; the picture recovers within one GOP and the player never crashes.

## 5. Exact formats

### 5.1 Command-line contract

```
PLAYER --stdin               --frame-log FILE [--yuv-out FILE]
PLAYER --udp PORT [--idle-timeout S] [--max-seconds N]  --frame-log FILE [--yuv-out FILE]
```

* `--stdin`: read the TS from standard input until EOF. **The player reads stdin; the harness never does.** The harness starts it with stdin connected to the damaged `.ts` file, or (`--pipe`) to a pipe it fills itself in 1000-byte writes (so reads are not 188-aligned).
* `--udp PORT`: bind a UDP socket to `127.0.0.1:PORT`, **then create (truncate) the frame log** (the harness waits for that file before it sends, so no datagram is lost), and read datagrams. Exit status 0 when no datagram has arrived for `--idle-timeout` seconds (default 2; counted after the first datagram), or after `--max-seconds` (default 60). Datagrams hold any number of bytes; do not assume 1316.
* Options may come in any order (the harness puts `--frame-log` first).
* `--frame-log FILE`: written by the player (create it at start, complete it at exit). Required.
* `--yuv-out FILE` (optional, checked only with the harness's `--yuv`): the presented frames, cropped yuv420p, concatenated; concealed frames repeat the previous frame's bytes. The md5 in the log must equal the md5 of that frame's bytes.
* The player writes no windows and needs no GUI for the harness. Exit status: 0 whenever it reached end of input, **including garbage input**; anything else (crash, hang) fails.

### 5.2 Frame-log grammar

```
log     = { line LF }
line    = comment | resize | frame | empty
comment = "#" text
resize  = "resize" SP W SP H
frame   = "frame" SP IDX SP PTS SP W SP H SP KIND SP MD5
```

| Field | Meaning |
| --- | --- |
| `IDX` | 0, 1, 2, ... counting `frame` lines |
| `PTS` | decimal 90 kHz value of the PES header of the access unit, as received (no unwrapping, no rebasing; a jump or wrap-around is kept as is) |
| `W H` | size of the output frame (after cropping) |
| `KIND` | `real` (decoded) or `concealed` (repeat of the previous output frame) |
| `MD5` | 32 lowercase hex digits, md5 of the cropped yuv420p bytes of that frame (Y, then U, then V, no padding) |

`resize W H` is written immediately before the first frame and before every frame whose size differs from the previous frame's, with that frame's size; it is not written otherwise. Lines appear in presentation (output) order. Example (`drop_mid`: three packets lost in AU 20; the picture freezes on frame 19 until the IDR):

```
frame 19 208800 64 48 real      7b4580f74874e2f1d30c5f713d400ecc
frame 20 201600 64 48 concealed 7b4580f74874e2f1d30c5f713d400ecc
frame 21 205200 64 48 concealed 7b4580f74874e2f1d30c5f713d400ecc
...
frame 29 234000 64 48 concealed 7b4580f74874e2f1d30c5f713d400ecc
frame 30 241200 64 48 real      bf7205d4f8b7291adb82a03a2e3fb2fe
```

And a resolution switch (`res_up`): `... frame 29 237600 64 48 real dd0e...` then `resize 96 64` then `frame 30 241200 96 64 real 9474...`.

### 5.3 The streams

`python3 test_live.py --make-cases DIR` writes, per case, `DIR/<case>.ts` (what your player reads), `<case>.json` (the expectation) and `<case>.ref.json` (used only by the reference player). Sources are x264 streams, 64x48 or 96x64, 25 fps, B-frames, one IDR every 15 AUs, muxed by the generator itself so every damage is exactly known. AU numbers are in decoding order.

| Case | Damage and expectation |
| --- | --- |
| `clean` | none (udp) |
| `start_mid_pes`, `start_at_p` | join in the middle of a PES / at a P picture (`start_at_p` repeats SPS and PPS in every AU, so that P picture parses): first real frame is the IDR of AU 30 |
| `start_open_gop` | join at an open-GOP I picture with recovery SEI: leading B pictures dropped |
| `no_tables_start`, `pmt_version`, `pid_change` | no PAT/PMT for 8 AUs; PMT version bump; video PID 0x100 to 0x110 |
| `dup_packets` | duplicate video and PAT packets |
| `drop_mid`, `drop_one`, `drop_last_packet`, `drop_cross`, `drop_idr`, `drop_last_gop`, `drop_two` | lost packets in P, I and last-GOP pictures, across a PES start, twice |
| `corrupt_p`, `corrupt_idr`, `tei` | corrupted bytes (`may`), transport error indicator |
| `trunc_pkts`, `trunc_bytes` | stream ends mid-PES / mid-packet |
| `restart_back`, `restart_fwd` | sender killed mid-AU and restarted: new IDR, CC reset, PTS jumps back / forward 60 s |
| `res_up`, `res_down_up`, `sps_same_size`, `sps_new_id` | SPS changes at an IDR: new size, same id; new size and back; same size, new content; new SPS id |
| `garbage_prefix`, `garbage_between`, `short_packet` | sync loss: garbage before / between packets, a 128-byte packet |
| `trunc_nal`, `garbage_nal`, `junk_nals` | truncated slice, random slice, unknown / filler NAL units |
| `all_garbage`, `empty_input` | no video at all: exit 0 and an empty log |

## 6. Running the harness

```
python3 test_live.py --make-cases cases
python3 test_live.py cases --out out --reader 'python3 myplayer.py'              # stdin from the file
python3 test_live.py cases --out out --reader 'python3 myplayer.py' --pipe --udp # also pipe stdin and 5 UDP runs
python3 test_live.py cases --out out --case drop_mid                             # check out/drop_mid/frame.log you made by hand
python3 test_live.py --self-test
```

By hand: `PLAYER --stdin --frame-log out/drop_mid/frame.log < cases/drop_mid.ts`. For `--udp` runs the harness picks a free loopback port, starts `PLAYER --udp PORT --idle-timeout 1 --max-seconds S --frame-log ...`, waits for the log file to appear, sends the stream as 1316-byte datagrams, and the player ends by idle timeout. Every run has a hard timeout (`--timeout`, default 120 s) after which the player's whole process group is killed and the case fails. Loopback UDP rarely drops datagrams; if a UDP case fails alone, re-run it before debugging.

Exit status: 0 pass, 1 failure (wrong frames, crash, non-zero exit, hang), 2 unusable input (no log, malformed log line, unknown case).

**What is the truth, honestly.** The expected md5 values come from **ffmpeg's decoding of the clean sources** (`-f framemd5`), assigned to each access unit by PTS; frames after a clean IDR are bit exact, so this is real pixel truth. Frames inside a damage zone are *not* compared with ffmpeg (its concealment is not bit exact truth); only the rules of section 3.6 apply. In `may` zones (corrupted bytes, intact CCs) nobody can say whether a decoder *should* notice, so there the harness only checks that you do not crash and that you recover at the next IDR. The bundled reference player (`spoiler_reference/live_player.py`) does **not decode pixels**: everything around the decoder is real code (sync, PSI, CC rules, PES, NAL/AU splitting, SPS activation, POC and bumping, concealment) but "decode a picture" is a lookup of ffmpeg's md5, and "the slice decoded cleanly" is a comparison with the hash of the original slice bytes. `--self-test` shows it passes all 34 cases and that 19 deliberately wrong variants (decoding before an IDR, no flush at an SPS change, showing leading B pictures, no concealment, resuming late, no duplicate rule, no sync scan, ...) each fail at least one case. The `spoiler_reference` folder contains the answers; try the chapter first.

## 7. Manual checklist: the real live workflow

With your decoder, window (9.3) and timing (9.4) wired in, check by eye:

1. Sender: `ffmpeg -re -i in.ts -c copy -f mpegts "udp://127.0.0.1:1234?pkt_size=1316"`. Player: `PLAYER --udp 1234 ...`. Start the player first: video appears at the first IDR and plays at the right speed.
2. **Join mid-stream:** start the player 10 s after the sender. No garbage frames; the first picture appears at the next IDR, at most one GOP later.
3. **Kill and restart the sender** (Ctrl-C, run it again): the picture freezes, then recovers within one GOP of the new stream. PTS restarts from the beginning of the file: the clock re-anchors (9.4). The player must not crash and must not exit.
4. **Loss:** `sudo tc qdisc add dev lo root netem loss 2%` (remove with `sudo tc qdisc del dev lo root`): the picture freezes briefly at each loss and recovers at each IDR; no crash over several minutes.
5. **Resolution switch:** `cat 480p.ts 720p.ts | ffmpeg -re -f mpegts -i - -c copy -f mpegts udp://...` (or a sender you write): the window resizes at the switch, no stale frames from the old size.
6. Idle: stop the sender; with a window open the player stays alive on the frozen last frame (only the headless `--udp` mode exits on idle timeout) and exits cleanly when the window is closed.
7. Robustness: `head -c 50000000 /dev/urandom | PLAYER --stdin ...` (it ends at EOF), and a real `.ts` with bytes overwritten by `dd`: no crash, no memory growth.

## 8. Reading failures

The harness first prints one line per case (`ok` or `FAIL`, with your real and concealed frame counts), then the first 10 problems. Read the case table (5.3) entry of the first failing case, then:

* **"must not be presented"**: you started too early (a P picture before an IDR, a leading B picture, video before the PMT), or you presented an AU whose PES start was lost.
* **"are missing"** for `real` frames: you resynced too late, lost a packet you should have kept (sync scan, duplicate rule), or discarded something at a PMT change. A missing `conceal` frame: you did not report a damaged AU.
* **"expected a concealed frame, got real"**: you decoded something after an error, before the IDR, or ignored the transport error flag.
* **md5 differs on a `real` frame**: a decoder bug after an IDR (use Phases 5 to 8's tools), or you did not flush / reset POC at the IDR, or output order is wrong at a flush.
* **"resize"**: sent too late, too often, or never.
* **Crash or timeout**: the case name shows which damage; run the player by hand on that file and look at `out/<case>/reader_output.txt`.

## 9. What's next: end of Phase 9 and a look at Phase 10

**Phase 9 recap.** You now have a complete progressive player. Output order uses the real bumping process (9.1), pictures become RGB (9.2) in a native window (9.3), shown at the right time from the PES PTS (9.4), from a file, a pipe or a live UDP stream, surviving loss, restarts and resolution switches (9.5). Together with Phases 1 to 8 that is the whole path from bytes on the wire to a moving picture, for Constrained Baseline, Main and High profile, progressive 4:2:0 video.

**Phase 10 preview: interlaced video, conformance and speed.** Much broadcast TV is 1080i. Phase 10 adds **field pictures** (each field coded as its own picture) and **MBAFF** (frame/field chosen per macroblock pair), with their reference lists, neighbour derivation and deblocking, then **deinterlacing for display**, a **conformance sweep** with fuzzed streams (this chapter's robustness habits, at scale), and **performance** work. Your recovery logic stays as it is.
