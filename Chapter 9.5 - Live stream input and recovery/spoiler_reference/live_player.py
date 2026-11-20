#!/usr/bin/env python3
"""SPOILER: reference player for Chapter 9.5.  Headless, stdlib only, no GUI.

  live_player.py (--stdin | --udp PORT) --frame-log FILE --truth REF.json [--idle-timeout S] [--max-seconds N] [--bug NAME]

HONEST SCOPE: this player does NOT decode pixels.  Everything around the decoder is real: incremental TS input with sync
recovery, PAT / PMT (CRC-checked, version and PID changes), continuity-counter / duplicate / error-indicator rules, PES
reassembly, NAL / access-unit splitting, SPS / PPS activation at IDR, recovery point SEI, POC and bumping order, resize
events, concealment and resync.  Only 'decode this picture' is simulated: the md5 of a real frame is looked up in the
side table made from ffmpeg's framemd5 of the clean source, and 'the slice data decoded without an error' is simulated by
comparing a hash of the received slice NAL units with the hash of the original ones (--truth REF.json).
Your own player decodes for real and needs neither table.
"""
import argparse
import os
import select
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h264lite as H
import tsmux

BUGS = {
    "start_nonidr": "starts decoding at the first slice it sees (no IDR / recovery point wait): P pictures without references are shown",
    "no_flush_sps": "does not output the pictures still waiting in the DPB when an IDR activates a different SPS",
    "wrong_size": "keeps reporting the first picture size after a resolution switch",
    "show_leading_b": "outputs the leading B pictures of an open-GOP recovery point that reference unavailable pictures",
    "no_conceal": "after an error it shows nothing (no repeated frame) until the next IDR",
    "no_idr_wait": "after an error it keeps 'decoding' P pictures (garbage) instead of waiting for the next IDR",
    "conceal_forever": "never leaves the concealment state: not even an IDR resumes real output",
    "conceal_past_idr": "resumes only at the second IDR after an error (recovery slower than one GOP)",
    "crash_trunc_nal": "crashes (exception) on a damaged / truncated slice NAL unit",
    "no_dup_rule": "duplicate TS packets (same continuity counter) are treated as data, then as a CC error",
    "no_sync_scan": "gives up after the first loss of TS sync instead of scanning for the next sync byte",
    "sync_no_confirm": "accepts any 0x47 byte as a sync byte (no check of the next packet)",
    "ignore_pmt_change": "keeps the first video PID forever, ignoring a new PMT",
    "start_before_tables": "guesses the video PID from a PES start code before any PAT / PMT was seen",
    "pmt_version_reset": "treats every PMT version change as a new stream: flushes and waits for an IDR again",
    "no_resize": "never reports a picture size change (no resize notification)",
    "conceal_wrong_pixels": "concealed frames are black instead of a repeat of the last frame",
    "no_flush_eof": "drops the pictures still waiting in the DPB at end of input",
    "ignore_tei": "ignores the transport_error_indicator bit",
}


class Player:
    HOLD = 4                                    # reorder depth of the simulated DPB: output the smallest POC when more are held

    def __init__(self, truth, bug=None):
        self.truth, self.bug = truth, bug
        self.lines, self.buf = [], bytearray()
        self.dead = False
        # TS / PSI
        self.pmt_pid = self.vpid = self.pmt_ver = None
        self.last_cc, self.last_pkt, self.dup_seen = {}, {}, {}
        self.psi = {}
        self.cur = None                         # video PES being assembled: dict(buf, damaged)
        # ES / AU
        self.es, self.es_base, self.spans = bytearray(), 0, []
        self.au = None
        self.sps_t, self.pps_t = {}, {}
        # decoder state
        self.synced = self.zone = self.garb = False
        self.zone_idr = 0
        self.active, self.first_size = None, None
        self.poc = H.Poc()
        self.start_poc, self.rec_left, self.rec_fn = None, 0, 0
        self.held = []
        self.out_size, self.last_md5, self.n = None, None, 0

    # ------------------------------------------------------------------------------------------------ TS layer
    def feed(self, data):
        self.buf += data
        self.pump(False)

    def pump(self, final):
        b, n, p = self.buf, len(self.buf), 0
        while not self.dead:
            avail = n - p
            if avail <= 0 or (not final and avail < 3 * 188 + 1):
                break
            if b[p] == 0x47 and (p + 188 >= n or b[p + 188] == 0x47 or (final and p + 188 > n - 1)) and avail >= 188:
                self.packet(bytes(b[p:p + 188]))
                p += 188
                continue
            if self.bug == "no_sync_scan":
                self.dead = True
                break
            q = p + 1
            while q < n:
                if b[q] == 0x47 and (self.bug == "sync_no_confirm" or all(q + k * 188 >= n or b[q + k * 188] == 0x47 for k in (1, 2))):
                    if self.bug == "sync_no_confirm" or final or q + 376 < n:
                        break
                    q = None                    # cannot confirm yet: wait for more data
                    break
                if not final and q + 376 >= n:
                    q = None
                    break
                q += 1
            if q is None:
                break
            if q >= n:                          # final: nothing more that looks like a packet
                p = n
                break
            if b[p] == 0x47 and q - p >= 188 and avail >= 188:
                self.packet(bytes(b[p:p + 188]))        # a complete packet followed by garbage
            p = q                                       # (q - p < 188: that packet was short: lost)
        del self.buf[:p]

    def packet(self, pk):
        pid, pusi, afc, cc = ((pk[1] & 0x1F) << 8) | pk[2], bool(pk[1] & 0x40), (pk[3] >> 4) & 3, pk[3] & 15
        if pid == 0x1FFF:
            return
        known = pid == 0 or pid == self.pmt_pid or pid == self.vpid
        if pk[1] & 0x80 and self.bug != "ignore_tei":
            self.last_cc[pid], self.last_pkt[pid] = cc, pk
            if pid == self.vpid:
                self.damage()
            self.psi.pop(pid, None)
            return
        if not (afc & 1):
            return
        pay = 5 + pk[4] if afc & 2 else 4
        if pay > 188:
            return
        body = pk[pay:]
        prev = self.last_cc.get(pid)
        if prev is not None:
            if cc == prev and pk == self.last_pkt.get(pid) and not self.dup_seen.get(pid) and self.bug != "no_dup_rule":
                self.dup_seen[pid] = True
                return                          # duplicate packet: drop exactly one copy
            if cc != (prev + 1) & 15:
                if pid == self.vpid:
                    self.damage()
                self.psi.pop(pid, None)
        self.dup_seen[pid] = False
        self.last_cc[pid], self.last_pkt[pid] = cc, pk
        if pid == 0 or pid == self.pmt_pid:
            self.psi_feed(pid, pusi, body)
        elif pid == self.vpid:
            self.video(pusi, body)
        elif self.vpid is None and self.bug == "start_before_tables" and pusi and body[:3] == b"\x00\x00\x01" and 0xE0 <= body[3] <= 0xEF:
            self.vpid = pid
            self.video(pusi, body)

    def damage(self):
        if self.cur is not None:
            self.cur["damaged"] = True

    # ------------------------------------------------------------------------------------------------ PSI
    def psi_feed(self, pid, pusi, body):
        if pusi:
            if not body:
                return
            ptr = body[0]
            if pid in self.psi and ptr:
                self.psi[pid] += body[1:1 + ptr]
                self.psi_sections(pid)
            self.psi[pid] = bytearray(body[1 + ptr:])
        elif pid in self.psi:
            self.psi[pid] += body
        else:
            return
        self.psi_sections(pid)

    def psi_sections(self, pid):
        buf = self.psi.get(pid)
        while buf and len(buf) >= 3 and buf[0] != 0xFF:
            ln = 3 + (((buf[1] & 0xF) << 8) | buf[2])
            if len(buf) < ln:
                return
            sec = bytes(buf[:ln])
            del buf[:ln]
            if tsmux.crc32_mpeg(sec) == 0 and sec[5] & 1:
                self.section(sec)
        if buf and buf[0] == 0xFF:
            buf.clear()

    def section(self, s):
        tid, ver = s[0], (s[5] >> 1) & 31
        if tid == 0:
            for i in range(8, len(s) - 4, 4):
                if (s[i] << 8 | s[i + 1]) != 0:
                    self.pmt_pid = ((s[i + 2] & 0x1F) << 8) | s[i + 3]
                    break
        elif tid == 2:
            pil = ((s[10] & 0xF) << 8) | s[11]
            i, vpid = 12 + pil, None
            while i + 5 <= len(s) - 4:
                if s[i] == 0x1B and vpid is None:
                    vpid = ((s[i + 1] & 0x1F) << 8) | s[i + 2]
                i += 5 + (((s[i + 3] & 0xF) << 8) | s[i + 4])
            ver_changed = self.pmt_ver is not None and ver != self.pmt_ver
            self.pmt_ver = ver
            if vpid is not None and vpid != self.vpid:
                if self.vpid is not None and self.bug == "ignore_pmt_change":
                    return
                if self.vpid is not None:
                    self.close_pes()            # the old stream's last PES is complete: hand it on
                self.vpid = vpid
                self.cur = None
            elif ver_changed and self.bug == "pmt_version_reset":
                self.flush()
                self.synced = False

    # ------------------------------------------------------------------------------------------------ PES
    def video(self, pusi, body):
        if pusi:
            self.close_pes()
            self.cur = {"buf": bytearray(), "damaged": False}
        if self.cur is not None:
            self.cur["buf"] += body

    def close_pes(self):
        c, self.cur = self.cur, None
        if c is None:
            return
        b, dmg, pts = bytes(c["buf"]), c["damaged"], None
        if len(b) < 9 or b[:3] != b"\x00\x00\x01" or len(b) < 9 + b[8]:
            return
        fl = b[7] >> 6
        if fl & 2 and b[8] >= 5:
            v = b[9:14]
            pts = (((v[0] >> 1) & 7) << 30) | (v[1] << 22) | ((v[2] >> 1) << 15) | (v[3] << 7) | (v[4] >> 1)
        self.spans.append((self.es_base + len(self.es), pts, dmg))
        self.es += b[9 + b[8]:]
        self.scan(False)

    # ------------------------------------------------------------------------------------------------ ES / access units
    def span_at(self, off, end):
        """-> (PTS of the PES that holds byte `off`, True if any PES overlapping [off, end) was damaged)."""
        pts, dmg, top = None, False, self.es_base + len(self.es)
        for k, (s, p, d) in enumerate(self.spans):
            e = self.spans[k + 1][0] if k + 1 < len(self.spans) else top
            if s <= off < e or (s <= off and k + 1 == len(self.spans)):
                pts = p
            if s < end and e > off and d:
                dmg = True
        return pts, dmg

    def scan(self, final):
        es = bytes(self.es)
        nals = H.split_nals(es)
        done = nals if final else nals[:-1]       # a NAL is complete once another start code follows it
        for off, nal in done:
            pts, dmg = self.span_at(self.es_base + off, self.es_base + off + len(nal))
            self.nal(nal, pts, dmg)
        if final:
            cut = len(es)
        elif nals:
            cut = nals[-1][0] - 3                 # keep the unfinished NAL (and its start code)
        else:
            cut = max(0, len(es) - 3)
        self.es_base_old = self.es_base
        del self.es[:cut]
        self.es_base += cut
        sp = self.spans
        self.spans = [x for i, x in enumerate(sp) if i + 1 >= len(sp) or sp[i + 1][0] > self.es_base]

    def nal(self, nal, pts, dmg):
        if nal[0] & 0x80:
            return                              # forbidden_zero_bit: not a NAL unit
        t = nal[0] & 31
        new = False
        if t in (9, 7, 8, 6):
            new = self.au is None or self.au["vcl"]
        elif t in (1, 5):
            try:
                fm = H.Bits(H.unescape(nal[1:])).ue()
            except ValueError:
                fm = None
            new = self.au is None or (self.au["vcl"] and fm == 0)
        else:
            return                              # unknown / unsupported NAL types are ignored
        if new:
            self.finish_au()
            self.au = {"pts": pts, "nals": [], "vcl": False, "bad": False, "rec": None}
        au = self.au
        au["nals"].append(nal)
        if t == 7 and not dmg:
            try:
                sp = H.parse_sps(nal)
                self.sps_t[sp["id"]] = sp
            except ValueError:
                pass
        elif t == 8 and not dmg:
            try:
                pp = H.parse_pps(nal)
                self.pps_t[pp["id"]] = pp
            except ValueError:
                pass
        elif t == 6 and not dmg:
            r = H.recovery_point(nal)
            if r is not None:
                au["rec"] = r
        elif t in (1, 5):
            au["vcl"] = True
            if dmg:
                au["bad"] = True

    def finish_au(self):
        au, self.au = self.au, None
        if au is None or not au["vcl"]:
            return
        info, bad = None, au["bad"]
        if not bad:
            try:
                for n in au["nals"]:
                    if (n[0] & 31) in (1, 5):
                        s = H.parse_slice(n, self.pps_t, self.sps_t)
                        info = info or s
            except ValueError:
                bad = True
        pts = au["pts"]
        if not bad and (pts is None or self.truth["sha"].get(str(pts)) != H.slice_hash(au["nals"])):
            bad = True                          # stands in for: the slice data did not decode cleanly
        if bad and self.bug == "crash_trunc_nal":
            raise IndexError("slice NAL unit is shorter than its header says")
        self.decode(au, info, bad)

    # ------------------------------------------------------------------------------------------------ the decoder (simulated)
    def decode(self, au, info, bad):
        if not self.synced:
            if self.bug == "start_nonidr" and info is not None:
                self.synced = True
                self.begin(info)
                self.garb = not info["idr"]
            elif bad or info is None:
                return
            elif info["idr"]:
                self.begin(info)
            elif au["rec"] is not None and info["type"] in (2, 4):
                self.begin(info, rec=au["rec"])
            else:
                return                          # nothing to decode from yet
        elif bad or info is None:
            self.error(au)
            return
        elif self.zone:
            if info["idr"] and (self.bug != "conceal_past_idr" or self.zone_idr >= 1) and self.bug != "conceal_forever":
                self.zone = False
                self.on_idr(info)
            else:
                self.zone_idr += 1 if info["idr"] else 0
                self.conceal(au)
                return
        elif info["idr"]:
            self.on_idr(info)
        self.present(au, info)

    def begin(self, info, rec=None):
        self.synced = True
        self.zone = False
        self.on_idr(info)
        if rec is not None:
            self.poc.start_at(info)
            self.start_poc = self.poc.compute(info)
            self.poc.start_at(info)
            self.rec_left, self.rec_fn = rec, info["frame_num"]

    def on_idr(self, info):
        new = info["sps"]
        if self.active is not None and new["raw"] != self.active["raw"]:
            if self.bug == "no_flush_sps":
                self.held = []
            self.flush()                        # new SPS content: nothing from the old sequence may remain
        else:
            self.flush()
        self.active = new
        if self.first_size is None:
            self.first_size = (new["w"], new["h"])
        self.poc.reset()
        self.start_poc, self.rec_left, self.garb, self.zone_idr = None, 0, False, 0

    def present(self, au, info):
        sps = self.active
        poc = self.poc.compute(info)
        if self.rec_left and not info["idr"]:
            if (info["frame_num"] - self.rec_fn) % (1 << sps["log2_fn"]) < self.rec_left:
                return                          # still inside the recovery period: decoded but not shown
            self.rec_left = 0
        if self.start_poc is not None and poc < self.start_poc and self.bug != "show_leading_b":
            return                              # leading picture of a recovery point: its references do not exist
        size = self.first_size if self.bug == "wrong_size" else (sps["w"], sps["h"])
        md5 = "1" * 32 if self.garb else self.truth["md5"].get(str(au["pts"]), "0" * 32)
        self.held.append((poc, au["pts"], size, md5))
        if len(self.held) > self.HOLD:
            self.held.sort(key=lambda x: x[0])
            self.emit(*self.held.pop(0)[1:], "real")

    def flush(self):
        self.held.sort(key=lambda x: x[0])
        for poc, pts, size, md5 in self.held:
            self.emit(pts, size, md5, "real")
        self.held = []

    def error(self, au):
        self.flush()
        self.zone = True
        if self.bug == "no_idr_wait":
            self.zone, self.garb = False, True
        self.conceal(au)

    def conceal(self, au):
        if self.bug == "no_conceal" or self.last_md5 is None:
            return
        md5 = "0" * 32 if self.bug == "conceal_wrong_pixels" else self.last_md5
        self.emit(au["pts"], self.out_size, md5, "concealed")

    def emit(self, pts, size, md5, kind):
        if size != self.out_size and self.bug != "no_resize":
            self.lines.append(f"resize {size[0]} {size[1]}")
        self.out_size = size
        self.lines.append(f"frame {self.n} {'-' if pts is None else pts} {size[0]} {size[1]} {kind} {md5}")
        self.n += 1
        if kind == "real" or self.bug != "conceal_wrong_pixels":
            self.last_md5 = md5

    def finish(self):
        self.pump(True)
        self.close_pes()
        self.scan(True)
        self.finish_au()
        if self.bug == "no_flush_eof":
            self.held = []
        self.flush()


def run_bytes(data, truth, bug=None, chunk=4096):
    """In-process helper for the harness: -> list of frame-log lines."""
    p = Player(truth, bug)
    for i in range(0, len(data), chunk):
        p.feed(data[i:i + chunk])
    p.finish()
    return p.lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--udp", type=int, metavar="PORT")
    ap.add_argument("--frame-log", required=True)
    ap.add_argument("--yuv-out")
    ap.add_argument("--truth", required=True)
    ap.add_argument("--idle-timeout", type=float, default=2.0)
    ap.add_argument("--max-seconds", type=float, default=60.0)
    ap.add_argument("--bug", choices=sorted(BUGS), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.yuv_out:
        sys.exit("the reference player does not decode pixels: --yuv-out is not supported")
    if bool(a.stdin) == (a.udp is not None):
        sys.exit("exactly one of --stdin / --udp PORT is required")
    import json
    p = Player(json.load(open(a.truth)), a.bug)
    deadline = time.monotonic() + a.max_seconds
    if a.stdin:
        open(a.frame_log, "w").close()
        while time.monotonic() < deadline:
            r, _, _ = select.select([0], [], [], 1.0)
            if not r:
                continue
            d = os.read(0, 4096)
            if not d:
                break
            p.feed(d)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
            s.bind(("127.0.0.1", a.udp))
            open(a.frame_log, "w").close()            # ready signal: the socket is open
            last = None
            while time.monotonic() < deadline:
                r, _, _ = select.select([s], [], [], 0.2)
                if r:
                    p.feed(s.recv(65536))
                    last = time.monotonic()
                elif last is not None and time.monotonic() - last > a.idle_timeout:
                    break
        finally:
            s.close()
    p.finish()
    open(a.frame_log, "w").write("# reference player (no pixel decoding)\n" + "\n".join(p.lines) + ("\n" if p.lines else ""))


if __name__ == "__main__":
    main()
