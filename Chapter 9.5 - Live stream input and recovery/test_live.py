#!/usr/bin/env python3
"""Chapter 9.5 harness: live stream input and recovery.  Python 3 standard library only (+ ffmpeg / ffprobe to build the streams).

The harness never reads stdin and never opens a window.  It runs YOUR player under a hard time limit (whole process group
killed) with a damaged TS fed on stdin (from a file, or through a pipe it writes itself) or, optionally, as UDP datagrams
that it sends to 127.0.0.1 itself.  Your player writes a frame log; the harness checks that log.

  python3 test_live.py --make-cases DIR            write DIR/<case>.ts (damaged stream), <case>.json (expectation), <case>.ref.json, cases.txt
  python3 test_live.py DIR --out OUT --reader 'CMD' [--pipe] [--udp] [--case NAME ...] [--timeout S] [--yuv]
        for each case runs   CMD --stdin --frame-log OUT/<case>/frame.log   < DIR/<case>.ts     (--pipe: stdin is a pipe)
        --udp additionally runs the cases marked udp as   CMD --udp PORT --idle-timeout 1 --max-seconds S --frame-log ...
        then checks every log.   --yuv appends --yuv-out OUT/<case>/out.yuv and checks it against the md5 values.
  python3 test_live.py DIR --out OUT               only check logs you made yourself (OUT/<case>/frame.log)
  python3 test_live.py --self-test                 verify the harness: the reference player passes, >= 12 wrong behaviours are caught

Exit codes: 0 all checks pass, 1 wrong behaviour (including a crash, a non-zero exit status or a hang), 2 missing / malformed input
(cases, frame log, command).  Frame-log grammar and the checks are in the Readme (sections 5 and 3.6).
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "spoiler_reference"))
from common import ffref  # noqa: E402
from common.report import Report  # noqa: E402
import cases as G  # noqa: E402
import live_player  # noqa: E402

MD5 = re.compile(r"^[0-9a-f]{32}$")


class Fatal(Exception):
    """missing / malformed input -> exit 2"""


# ----------------------------------------------------------------------------------------------- frame log
def parse_log(path):
    """-> list of ('resize', w, h) / ('frame', idx, pts|None, w, h, kind, md5, lineno); raises Fatal on a malformed line."""
    if not os.path.exists(path):
        raise Fatal(f"frame log {path} does not exist")
    ev = []
    for no, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
        t = line.split()
        if not t or t[0].startswith("#"):
            continue
        try:
            if t[0] == "resize" and len(t) == 3:
                ev.append(("resize", int(t[1]), int(t[2])))
            elif t[0] == "frame" and len(t) == 7:
                if t[5] not in ("real", "concealed") or not MD5.match(t[6]):
                    raise ValueError
                ev.append(("frame", int(t[1]), None if t[2] == "-" else int(t[2]), int(t[3]), int(t[4]), t[5], t[6], no))
            else:
                raise ValueError
        except ValueError:
            raise Fatal(f"{path}:{no}: malformed line {line.strip()[:80]!r} (expected 'resize W H' or "
                        f"'frame IDX PTS W H real|concealed MD5')") from None
    return ev


def check_log(rep, name, truth, ev):
    exp = {e["pts"]: e for e in truth["expect"]}
    seen, prev, last_rank = set(), None, -1
    cur_resize, hints = None, []
    for k, e in enumerate(ev):
        if e[0] == "resize":
            cur_resize = (e[1], e[2])
            if k + 1 >= len(ev) or ev[k + 1][0] != "frame" or (ev[k + 1][3], ev[k + 1][4]) != cur_resize:
                rep.check(False, f"{name}: 'resize {e[1]} {e[2]}' must be directly followed by a frame of that size")
            continue
        _, idx, pts, w, h, kind, md5, no = e
        nth = sum(1 for x in ev[:k] if x[0] == "frame")
        rep.check(idx == nth, f"{name}: line {no}: frame index {idx}, expected {nth} (indices count 0, 1, 2, ...)")
        size = (w, h)
        need_resize = prev is None or size != prev["size"]
        has_resize = k > 0 and ev[k - 1][0] == "resize"
        rep.check(need_resize == has_resize, f"{name}: line {no}: " + (
            f"size {w}x{h} differs from the previous frame but there is no 'resize {w} {h}' line before it" if need_resize
            else "'resize' line although the picture size did not change"))
        if kind == "concealed":
            rep.check(prev is not None, f"{name}: line {no}: a concealed frame before any real frame (nothing to repeat)")
            if prev is not None:
                rep.check(md5 == prev["md5"] and size == prev["size"],
                          f"{name}: line {no} (PTS {pts}): a concealed frame must repeat the previous output frame "
                          f"(expected md5 {prev['md5'][:8]}.. {prev['size'][0]}x{prev['size'][1]}, got {md5[:8]}.. {w}x{h})")
        prev = {"md5": md5, "size": size}
        if pts is None:
            rep.check(False, f"{name}: line {no}: frame without PTS ('-'): every presented picture carries its access unit's PES PTS")
            continue
        en = exp.get(pts)
        if en is None:
            rep.check(False, f"{name}: line {no}: PTS {pts} must not be presented ({kind}): its access unit is before the first valid "
                             f"start point, a discarded leading picture, or was never received")
            continue
        rep.check(pts not in seen, f"{name}: line {no}: PTS {pts} presented twice")
        seen.add(pts)
        if en["cls"] == "real":
            rep.check(kind == "real", f"{name}: PTS {pts}: expected a real (decoded) frame, got {kind} (recovery slower than the next IDR, or an error was reported where there was none)")
            if kind == "real":
                rep.check((w, h) == (en["w"], en["h"]), f"{name}: PTS {pts}: size {w}x{h}, expected {en['w']}x{en['h']}")
                rep.check(md5 == en["md5"], f"{name}: PTS {pts}: frame md5 {md5[:8]}.. differs from ffmpeg's {en['md5'][:8]}.. "
                                            f"(a frame after a clean IDR must be bit exact)")
                rep.check(en["rank"] > last_rank, f"{name}: PTS {pts}: output order is wrong (picture number {en['rank']} after {last_rank})")
                last_rank = max(last_rank, en["rank"])
        elif en["cls"] == "conceal":
            rep.check(kind == "concealed", f"{name}: PTS {pts}: this picture is damaged or follows a damaged one before the next IDR: "
                                           f"expected a concealed frame, got {kind}")
    for cls, what in (("real", "real (decoded) frames"), ("conceal", "concealed frames")):
        want = [en for en in truth["expect"] if en["cls"] == cls]
        miss = [en for en in want if en["pts"] not in seen]
        if cls == "real":
            miss.sort(key=lambda en: en["rank"])
        rep.check(not miss, f"{name}: {len(miss)} of {len(want)} expected {what} are missing from the log, PTS " +
                  ", ".join(str(en["pts"]) for en in miss[:6]) + (" ..." if len(miss) > 6 else "") +
                  (f" (the first missing one is output picture #{miss[0]['rank']})" if miss and cls == "real" else ""))
    n_real = sum(1 for x in ev if x[0] == "frame" and x[5] == "real")
    n_conc = sum(1 for x in ev if x[0] == "frame" and x[5] == "concealed")
    return f"{name}: {n_real} real + {n_conc} concealed frames"


def check_yuv(rep, name, ev, yuv_path):
    if not os.path.exists(yuv_path):
        rep.check(False, f"{name}: --yuv given but {yuv_path} was not written")
        return
    import hashlib
    data, pos = open(yuv_path, "rb").read(), 0
    for e in ev:
        if e[0] != "frame":
            continue
        n = e[3] * e[4] * 3 // 2
        rep.check(hashlib.md5(data[pos:pos + n]).hexdigest() == e[6], f"{name}: frame {e[1]}: the yuv bytes do not match the md5 in the log")
        pos += n
    rep.check(pos == len(data), f"{name}: yuv file is {len(data)} bytes, the frame log describes {pos}")


# ----------------------------------------------------------------------------------------------- running a reader (hang-safe)
def _kill(pr):
    try:
        os.killpg(pr.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        pr.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_reader(cmd, name, ts, out_dir, mode, timeout, extra=(), yuv=False, udp_data=None):
    """mode: 'file' | 'pipe' | 'udp'.  -> dict(rc, timed_out, log, why)"""
    os.makedirs(out_dir, exist_ok=True)
    log = os.path.join(out_dir, "frame.log" if mode != "udp" else "frame_udp.log")
    if os.path.exists(log):
        os.unlink(log)
    args = shlex.split(cmd) + ["--frame-log", log] + list(extra)
    if yuv:
        args += ["--yuv-out", os.path.join(out_dir, "out.yuv")]
    res = {"rc": None, "timed_out": False, "log": log, "why": ""}
    port_probe = None
    with open(os.path.join(out_dir, "reader_output.txt" if mode != "udp" else "reader_output_udp.txt"), "wb") as fo:
        try:
            if mode == "udp":
                port_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                port_probe.bind(("127.0.0.1", 0))
                port = port_probe.getsockname()[1]
                port_probe.close()
                port_probe = None
                args += ["--udp", str(port), "--idle-timeout", "1", "--max-seconds", str(max(5, timeout - 5))]
                pr = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.STDOUT, start_new_session=True)
            elif mode == "pipe":
                pr = subprocess.Popen(args + ["--stdin"], stdin=subprocess.PIPE, stdout=fo, stderr=subprocess.STDOUT, start_new_session=True)
            else:
                with open(ts, "rb") as fin:
                    pr = subprocess.Popen(args + ["--stdin"], stdin=fin, stdout=fo, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            if port_probe:
                port_probe.close()
            raise Fatal(f"cannot start the reader command: {e}") from None
        t0 = time.monotonic()
        helper = None
        try:
            if mode == "pipe":
                data = open(ts, "rb").read()

                def feed():
                    try:
                        for i in range(0, len(data), 1000):          # deliberately not a multiple of 188
                            pr.stdin.write(data[i:i + 1000])
                        pr.stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
                helper = threading.Thread(target=feed, daemon=True)
                helper.start()
            elif mode == "udp":
                while not os.path.exists(log):                      # ready signal: the reader created its log after binding
                    if pr.poll() is not None or time.monotonic() - t0 > 10:
                        res["timed_out"] = True
                        res["why"] = (f"in --udp mode your player {'exited (status %s)' % pr.returncode if pr.returncode is not None else 'ran 10 s'} "
                                      f"without creating {log}; it must bind 127.0.0.1:{port}, then create the frame log, then read "
                                      f"(see {fo.name})")
                        return res
                    time.sleep(0.02)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                def send():
                    try:
                        for n, i in enumerate(range(0, len(udp_data), 1316)):      # 7 x 188 bytes per datagram
                            sock.sendto(udp_data[i:i + 1316], ("127.0.0.1", port))
                            if n % 8 == 7:
                                time.sleep(0.001)
                    finally:
                        sock.close()
                helper = threading.Thread(target=send, daemon=True)
                helper.start()
            res["rc"] = pr.wait(timeout=max(1, timeout - (time.monotonic() - t0)))
        except subprocess.TimeoutExpired:
            res["timed_out"] = True
            res["why"] = f"your player did not finish within {timeout} s (hang?) and was killed (see {fo.name})"
        finally:
            _kill(pr)
            if helper:
                helper.join(timeout=5)
            if mode == "pipe" and pr.stdin and not pr.stdin.closed:
                try:
                    pr.stdin.close()
                except OSError:
                    pass
    return res


def udp_possible():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------------------------- driver
def load_cases(d, only=None):
    f = os.path.join(d, "cases.txt")
    if not os.path.exists(f):
        raise Fatal(f"{f} not found: run  python3 test_live.py --make-cases {d}")
    names = open(f).read().split()
    if only:
        bad = set(only) - set(names)
        if bad:
            raise Fatal("unknown case(s): " + ", ".join(sorted(bad)))
        names = [n for n in names if n in only]
    return names


def main_check(a):
    names = load_cases(a.dir, a.case)
    rep = Report("Chapter 9.5 live input and recovery")
    infos = []
    runs = []
    for n in names:
        truth = json.load(open(os.path.join(a.dir, n + ".json")))
        ts = os.path.join(a.dir, n + ".ts")
        out = os.path.join(a.out, n)
        modes = []
        if a.reader:
            modes = ["pipe" if a.pipe else "file"] + (["udp"] if a.udp and truth["udp"] else [])
        else:
            modes = ["file"]
        for m in modes:
            extra = ["--truth", os.path.join(a.dir, n + ".ref.json")] if a.ref_truth else []
            if a.reader:
                r = run_reader(a.reader, n, ts, out, m, a.timeout, extra, a.yuv, open(ts, "rb").read() if m == "udp" else None)
            else:
                r = {"rc": 0, "timed_out": False, "log": os.path.join(out, "frame.log"), "why": ""}
            label = n + ("[udp]" if m == "udp" else "")
            if r["timed_out"]:
                rep.check(False, f"{label}: {r['why']}")
                continue
            if r["rc"] != 0:
                rep.check(False, f"{label}: your player exited with status {r['rc']} (crash on damaged input?); see {out}/reader_output.txt")
                continue
            ev = parse_log(r["log"])
            before = len(rep.failures)
            info = check_log(rep, label, truth, ev)
            if a.yuv and a.reader:
                check_yuv(rep, label, [e for e in ev], os.path.join(out, "out.yuv"))
            n_bad = len(rep.failures) - before
            infos.append(f"{label:24s} {'ok  ' if not n_bad else 'FAIL'}  {info.split(': ', 1)[1]}"
                         + (f"  ({n_bad} problem(s))" if n_bad else ""))
    print("per case:")
    for i in infos:
        print("  " + i)
    rep.finish()


def make_cases(d):
    os.makedirs(d, exist_ok=True)
    ffref.require_tools()
    work = tempfile.mkdtemp(prefix="t95_")
    try:
        return G.make_all(work, d)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------------------------------- self-test
def _check_inproc(d, n, bug=None, chunk=4096):
    truth = json.load(open(os.path.join(d, n + ".json")))
    ref = json.load(open(os.path.join(d, n + ".ref.json")))
    data = open(os.path.join(d, n + ".ts"), "rb").read()
    rep = Report("x")
    try:
        lines = live_player.run_bytes(data, ref, bug, chunk)
    except Exception as e:  # noqa: BLE001 - a crashing player is a failure
        return [f"{n}: player crashed: {type(e).__name__}: {e}"]
    tmp = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    try:
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        check_log(rep, n, truth, parse_log(tmp.name))
    finally:
        os.unlink(tmp.name)
    return rep.failures


def self_test():
    ffref.require_tools()
    tmp = tempfile.mkdtemp(prefix="t95st_")
    py = shlex.quote(sys.executable)
    ref = shlex.quote(os.path.join(HERE, "spoiler_reference", "live_player.py"))
    try:
        d = os.path.join(tmp, "cases")
        names = make_cases(d)
        print(f"self-test: {len(names)} cases generated")
        # 1. truth sanity: ffmpeg decodes the generated (muxed) clean streams to exactly the expected md5 values
        for n in ("clean", "junk_nals", "sps_new_id", "start_open_gop"):
            r = ffref.run(["ffmpeg", "-v", "error", "-y", "-i", os.path.join(d, n + ".ts"), "-map", "0:v:0", "-fps_mode", "passthrough",
                           "-f", "framemd5", os.path.join(tmp, "x.md5")], timeout=120)
            got = sorted(ln.split(",")[-1].strip() for ln in open(os.path.join(tmp, "x.md5")) if ln[0] != "#")
            truth = json.load(open(os.path.join(d, n + ".json")))
            want = sorted(e["md5"] for e in truth["expect"] if e["cls"] == "real")
            if got != want:
                print(f"SELF-TEST FAILED: ffmpeg's decode of {n}.ts differs from the stored truth ({len(got)} vs {len(want)} frames)")
                return 1
        print("  truth: ffmpeg decodes clean / junk_nals / sps_new_id / start_open_gop to the expected md5 values")
        # 2. the reference player passes every case, with two different input chunkings
        bad = []
        for n in names:
            for chunk in (4096, 997):
                bad += _check_inproc(d, n, None, chunk)
        if bad:
            for m in bad[:10]:
                print("  FAIL:", m)
            print("SELF-TEST FAILED: the reference player does not pass")
            return 1
        print(f"  reference player (in process, 2 chunkings): PASS on {len(names)} cases")
        # 3. the real run paths: stdin from a file, stdin through a pipe, UDP
        for mode, sel in (("file", ["clean", "drop_mid", "garbage_prefix"]), ("pipe", ["start_mid_pes", "res_up"]), ("udp", ["clean", "restart_back"])):
            if mode == "udp" and not udp_possible():
                print("  udp: SKIPPED (cannot bind a loopback UDP socket here)")
                continue
            for n in sel:
                truth = json.load(open(os.path.join(d, n + ".json")))
                ts = os.path.join(d, n + ".ts")
                r = run_reader(f"{py} {ref}", n, ts, os.path.join(tmp, "out", n), mode, 60, ["--truth", os.path.join(d, n + ".ref.json")],
                               False, open(ts, "rb").read())
                rr = Report("run")
                if r["timed_out"] or r["rc"] != 0:
                    print(f"SELF-TEST FAILED: reference run {mode}/{n}: rc={r['rc']} timeout={r['timed_out']}")
                    return 1
                check_log(rr, n, truth, parse_log(r["log"]))
                if rr.failures:
                    print(f"SELF-TEST FAILED: reference run {mode}/{n}: {rr.failures[:3]}")
                    return 1
            print(f"  reference player as a subprocess, input mode {mode}: PASS ({', '.join(sel)})")
        # 4. wrong behaviours
        missed = []
        for bug, text in live_player.BUGS.items():
            caught = None
            for n in names:
                f = _check_inproc(d, n, bug)
                if f:
                    caught = f"{n}: {f[0][:100]}"
                    break
            print(f"  {'caught' if caught else 'MISSED'}: {bug:20s} {text[:62]:62s} -> {caught or ''}")
            if not caught:
                missed.append(bug)
        # 5. harness robustness: a hanging and a crashing reader, a malformed log
        t0 = time.monotonic()
        r = run_reader(f"{py} -c 'import time; time.sleep(60)'", "x", os.path.join(d, "clean.ts"), os.path.join(tmp, "hang"), "file", 3)
        if not r["timed_out"] or time.monotonic() - t0 > 15:
            print("MISSED: hanging reader was not killed in time")
            missed.append("hang")
        else:
            print(f"  caught: hanging reader killed after {time.monotonic() - t0:.1f} s (process group)")
        r = run_reader(f"{py} -c 'import sys; sys.exit(3)'", "x", os.path.join(d, "clean.ts"), os.path.join(tmp, "crash"), "file", 10)
        if r["rc"] != 3:
            missed.append("crash-exit")
        else:
            print("  caught: non-zero exit status is reported")
        bad_log = os.path.join(tmp, "bad.log")
        open(bad_log, "w").write("frame 0 100 64 48 maybe abc\n")
        try:
            parse_log(bad_log)
            missed.append("malformed-log")
        except Fatal:
            print("  caught: malformed frame log -> exit 2")
        if missed:
            print("SELF-TEST FAILED: not caught:", ", ".join(missed))
            return 1
        print(f"SELF-TEST PASSED: reference passes {len(names)} cases, {len(live_player.BUGS)} wrong behaviours caught")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", help="directory made by --make-cases")
    ap.add_argument("--make-cases", metavar="DIR")
    ap.add_argument("--out", help="directory for <case>/frame.log")
    ap.add_argument("--case", action="append")
    ap.add_argument("--reader", help="command prefix that starts your player, e.g. 'python3 myplayer.py'")
    ap.add_argument("--pipe", action="store_true", help="feed stdin through a pipe the harness writes (default: stdin is the file)")
    ap.add_argument("--udp", action="store_true", help="also run the cases marked udp over loopback UDP")
    ap.add_argument("--yuv", action="store_true", help="pass --yuv-out and check the yuv bytes against the log's md5 values")
    ap.add_argument("--timeout", type=int, default=120, help="seconds per run before the player is killed (default 120)")
    ap.add_argument("--ref-truth", action="store_true", help="append --truth <case>.ref.json (only for the reference player)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    try:
        if a.self_test:
            sys.exit(self_test())
        if a.make_cases:
            print("wrote", len(make_cases(a.make_cases)), "cases to", a.make_cases)
            return
        if not a.dir or not a.out:
            ap.error("need DIR and --out (or --make-cases / --self-test)")
        main_check(a)
    except Fatal as e:
        print("ERROR:", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
