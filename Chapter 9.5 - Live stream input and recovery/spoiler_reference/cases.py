"""SPOILER: damage generators for Chapter 9.5.  Builds the clean sources with x264, then every damaged stream plus its truth:

  * <case>.ts      the damaged stream (what your player reads)
  * <case>.json    the harness's expectation: one entry per access unit whose PES start survives the damage, in decoding order:
                   {"pts", "cls": real | conceal | may, "md5", "w", "h", "rank"}   (cls rules: see the Readme, section 3.6)
  * <case>.ref.json  side tables used ONLY by the reference player (it does not decode pixels): per-PTS frame md5 (from
                   ffmpeg -f framemd5 of the clean source) and per-PTS slice hash (stands in for 'the slice data decoded without error')

Built on the PES reader / muxer in tsmux.py (a copy of Chapter 9.4's rewriter ideas).  Python 3 standard library only (+ ffmpeg).
"""
import json
import os
import random
import subprocess

import h264lite as H
import tsmux

SOURCES = {   # name: (size, seconds, x264 params)
    "A": ("64x48", 2.4, "keyint=15:min-keyint=15:scenecut=0:bframes=2"),
    "O": ("64x48", 2.4, "keyint=15:min-keyint=15:scenecut=0:bframes=2:open-gop=1"),
    "W": ("96x64", 1.2, "keyint=15:min-keyint=15:scenecut=0:bframes=2"),
    "S": ("64x48", 1.2, "keyint=15:min-keyint=15:scenecut=0:bframes=0:ref=2"),
}


def _run(args, timeout=120):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)


def build_sources(work):
    """-> {name: {"aus": [AU dict], "w", "h"}}  (encodes with x264, decodes once with ffmpeg for the per-frame md5)."""
    out = {}
    for name, (size, secs, x264) in SOURCES.items():
        ts, fm = os.path.join(work, f"src_{name}.ts"), os.path.join(work, f"src_{name}.md5")
        r = _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={secs},noise=alls=20:allf=t",
                  "-pix_fmt", "yuv420p", "-c:v", "libx264", "-profile:v", "main", "-qp", "28", "-x264-params", x264,
                  "-f", "mpegts", ts], 240)
        if r.returncode or not os.path.exists(ts):
            raise RuntimeError("ffmpeg could not encode source " + name + ": " + r.stderr.decode(errors="replace")[-300:])
        r = _run(["ffmpeg", "-v", "error", "-y", "-i", ts, "-fps_mode", "passthrough", "-f", "framemd5", fm], 240)
        if r.returncode:
            raise RuntimeError("ffmpeg framemd5 failed: " + r.stderr.decode(errors="replace")[-300:])
        md5s = [ln.split(",")[-1].strip() for ln in open(fm) if ln.strip() and not ln.startswith("#")]
        pes = tsmux.read_pes(ts)
        if len(md5s) != len(pes):
            raise RuntimeError(f"source {name}: {len(md5s)} decoded frames but {len(pes)} PES")
        order = sorted(range(len(pes)), key=lambda i: pes[i]["pts"])
        w, h = map(int, size.split("x"))
        aus = []
        for i, p in enumerate(pes):
            nals = [n for _o, n in H.split_nals(p["payload"])]
            aus.append({"pts": p["pts"], "dts": p["dts"], "payload": p["payload"], "md5": md5s[order.index(i)], "w": w, "h": h,
                        "idr": any((n[0] & 31) == 5 for n in nals), "sha": H.slice_hash(nals), "src_pts": p["pts"]})
        out[name] = {"aus": aus, "w": w, "h": h}
    return out


# ---------------------------------------------------------------------------------------------- composing
def take(src, a, b, off, seg, sps_id=None):
    res = []
    for x in src["aus"][a:b]:
        y = dict(x)
        y["pts"] = (x["pts"] + off) % tsmux.MOD
        y["dts"] = None if x["dts"] is None else (x["dts"] + off) % tsmux.MOD
        y["seg"] = seg
        if sps_id is not None:
            y["payload"] = retag(x["payload"], sps_id)
        res.append(y)
    return res


def retag(payload, sps_id):
    out = b""
    for _o, n in H.split_nals(payload):
        t = n[0] & 31
        if t == 7:
            n = H.rewrite_sps_id(n, sps_id)
        elif t == 8:
            n = H.rewrite_pps_sps_id(n, sps_id)
        out += b"\x00\x00\x00\x01" + n
    return out


def cont(prev, src, a, b, seg, sps_id=None, gap=3600):
    """src[a:b] placed right after the last (largest PTS) access unit of prev."""
    last = max(x["pts"] for x in prev)
    first = min(x["src_pts"] for x in src["aus"][a:b])
    return take(src, a, b, last + gap - first, seg, sps_id)


def survivors(pk, n):
    s = [False] * n
    for p in pk:
        if p["au"] is not None and p["pos"] == 0:
            s[p["au"]] = True
    return s


def next_idr(aus, k):
    return next((i for i in range(k + 1, len(aus)) if aus[i]["idr"]), len(aus))


def make_expect(aus, surv, first_ok=0, zones=(), skip=()):
    ent = []
    for i, a in enumerate(aus):
        if i < first_ok or i in skip or not surv[i]:
            continue
        cls = "real"
        for z0, z1, c in zones:
            if z0 <= i < z1:
                cls = c
        ent.append({"i": i, "pts": a["pts"], "cls": cls, "md5": a["md5"], "w": a["w"], "h": a["h"], "key": (a["seg"], a["src_pts"])})
    real = sorted((e for e in ent if e["cls"] == "real"), key=lambda e: e["key"])
    for r, e in enumerate(real):
        e["rank"] = r
    for e in ent:
        e.setdefault("rank", None)
        e.pop("key")
    return ent


def finish(name, aus, pk, exp, note="", udp=False):
    data = b"".join(p["b"] for p in pk)
    pts = [a["pts"] for a in aus]
    assert len(set(pts)) == len(pts), f"{name}: duplicate PTS values"
    truth = {"name": name, "expect": exp, "note": note, "udp": udp}
    ref = {"md5": {str(a["pts"]): a["md5"] for a in aus}, "sha": {str(a["pts"]): a["sha"] for a in aus}}
    return data, truth, ref


def idx_of(pk, au, pos):
    return next(i for i, p in enumerate(pk) if p["au"] == au and p["pos"] == pos)


def au_range(pk, au, a, b):
    r = [i for i, p in enumerate(pk) if p["au"] == au and a <= p["pos"] <= b]
    assert r, f"AU {au} has no packet at positions {a}..{b}"
    return r


def drop(pk, idxs):
    s = set(idxs)
    return [p for i, p in enumerate(pk) if i not in s]


def cut_front_at_au(pk, au):
    c = idx_of(pk, au, 0)
    while c > 0 and pk[c - 1]["au"] is None:
        c -= 1
    return pk[c:]


def raw(b):
    return {"b": b, "au": None, "pos": 0, "n": 1, "pid": None}


def noise_bytes(rng, n, with_sync=False):
    out = bytearray(rng.choice([x for x in range(1, 256) if x != 0x47]) for _ in range(n))
    if with_sync:
        for i in range(7, n, 61):
            out[i] = 0x47                          # lone 0x47 bytes: none is confirmed by a second 0x47 188 bytes later
    return bytes(out)


# ---------------------------------------------------------------------------------------------- the cases
CASES = {}


def case(fn):
    CASES[fn.__name__] = fn
    return fn


def _a(S, **kw):
    aus = take(S["A"], 0, 60, 0, 0)
    return aus, tsmux.mux(aus, **kw)


@case
def clean(S):
    aus, pk = _a(S)
    return finish("clean", aus, pk, make_expect(aus, survivors(pk, 60)), "undamaged stream, B-frames, 4 IDR periods", udp=True)


@case
def start_mid_pes(S):
    aus, pk = _a(S)
    pk = pk[idx_of(pk, 20, 3):]          # the next periodic PAT/PMT (before AU 25) lets the player find the video PID
    return finish("start_mid_pes", aus, pk, make_expect(aus, survivors(pk, 60), first_ok=30),
                  "first packet is the 4th packet of AU 20 (no PUSI); first IDR is AU 30", udp=True)


def with_repeated_headers(aus):
    """Copy the SPS and PPS of the first IDR into every access unit (as many broadcast encoders do), right after the AUD."""
    hdr = [n for _o, n in H.split_nals(aus[0]["payload"]) if (n[0] & 31) in (7, 8)]
    res = []
    for a in aus:
        nals = [n for _o, n in H.split_nals(a["payload"])]
        if not any((n[0] & 31) == 7 for n in nals):
            k = 1 if nals and (nals[0][0] & 31) == 9 else 0
            nals = nals[:k] + hdr + nals[k:]
        res.append(dict(a, payload=b"".join(b"\x00\x00\x00\x01" + n for n in nals)))
    return res


@case
def start_at_p(S):
    aus = with_repeated_headers(take(S["A"], 0, 60, 0, 0))
    pk = cut_front_at_au(tsmux.mux(aus), 22)
    return finish("start_at_p", aus, pk, make_expect(aus, survivors(pk, 60), first_ok=30),
                  "stream starts cleanly at AU 22 (a non-IDR P/B picture); SPS and PPS are repeated in every AU, so the P "
                  "picture is parsable, but decoding may begin only at the IDR of AU 30")


@case
def start_open_gop(S):
    aus = take(S["O"], 0, 60, 0, 0)
    pk = cut_front_at_au(tsmux.mux(aus, force_psi={28}), 28)
    skip = {i for i in range(29, 34) if aus[i]["pts"] < aus[28]["pts"]}
    return finish("start_open_gop", aus, pk, make_expect(aus, survivors(pk, 60), first_ok=28, skip=skip),
                  "open GOP: starts at a non-IDR I picture with a recovery point SEI (recovery_frame_cnt 0); the leading B pictures "
                  "that follow it in decoding order but precede it in output order must not be shown", udp=False)


@case
def no_tables_start(S):
    aus, pk = _a(S, psi_from=8)
    return finish("no_tables_start", aus, pk, make_expect(aus, survivors(pk, 60), first_ok=15),
                  "no PAT/PMT before AU 8; the IDR of AU 0 must not be shown (no PMT yet), decoding starts at the IDR of AU 15")


@case
def pmt_version(S):
    aus, pk = _a(S, psi_every=3, pmt_version=lambda i: 0 if i < 22 else 1, extra_es=lambda i: () if i < 22 else (0x102,))
    return finish("pmt_version", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "PMT/PAT version 0 -> 1 at AU 22 (a data PID is added, the video PID is unchanged): playback must continue untouched")


@case
def pid_change(S):
    aus, pk = _a(S, psi_every=5, vpid=lambda i: 0x100 if i < 30 else 0x110, pmt_version=lambda i: 0 if i < 30 else 1,
                 force_psi={30})
    return finish("pid_change", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "the video PID changes from 0x100 to 0x110 at the IDR of AU 30 (new PMT version): every picture is still shown")


@case
def dup_packets(S):
    aus, pk = _a(S)
    out = []
    pick = {idx_of(pk, 7, 0), idx_of(pk, 7, 5), idx_of(pk, 20, 0), idx_of(pk, 20, 2), idx_of(pk, 33, 3),
            idx_of(pk, 33, pk[idx_of(pk, 33, 0)]["n"] - 1), idx_of(pk, 50, 4)}
    pick |= {i for i, p in enumerate(pk) if p["pid"] == 0 and p["au"] is None and i % 7 == 0}
    for i, p in enumerate(pk):
        out.append(p)
        if i in pick:
            out.append(dict(p))
    return finish("dup_packets", aus, out, make_expect(aus, survivors(out, 60)),
                  "seven video packets (first, middle and last of PESs) and some PAT packets are sent twice (same CC): drop the copy")


def _drop_case(S, name, k, a, b, note, extra=()):
    aus, pk = _a(S)
    idx = au_range(pk, k, a, b)
    for ek, ea, eb in extra:
        idx += au_range(pk, ek, ea, eb)
    pk = drop(pk, idx)
    zones = [(k, next_idr(aus, k), "conceal")] + [(ek, next_idr(aus, ek), "conceal") for ek, _a_, _b in extra]
    return finish(name, aus, pk, make_expect(aus, survivors(pk, 60), zones=zones), note, udp=name == "drop_mid")


@case
def drop_mid(S):
    return _drop_case(S, "drop_mid", 20, 2, 4, "three packets (positions 2..4) lost in the middle of AU 20: AUs 20..29 are concealed, the IDR of AU 30 recovers")


@case
def drop_one(S):
    return _drop_case(S, "drop_one", 8, 4, 4, "a single packet lost in AU 8 (an early GOP): concealed until the IDR of AU 15")


@case
def drop_last_packet(S):
    aus, pk = _a(S)
    n = pk[idx_of(pk, 25, 0)]["n"]
    return _drop_case(S, "drop_last_packet", 25, n - 1, n - 1, "the last packet of AU 25's PES is lost (the next PES start shows the CC gap)")


@case
def drop_cross(S):
    aus, pk = _a(S)
    n = pk[idx_of(pk, 33, 0)]["n"]
    pk2 = drop(pk, au_range(pk, 33, n // 2, n) + au_range(pk, 34, 0, 1))
    return finish("drop_cross", aus, pk2, make_expect(aus, survivors(pk2, 60), zones=[(33, 45, "conceal")]),
                  "loss from the middle of AU 33 into the first packets of AU 34 (its PUSI is lost: AU 34 never appears)")


@case
def drop_idr(S):
    return _drop_case(S, "drop_idr", 30, 2, 3, "two packets lost inside the IDR of AU 30: nothing can be decoded until the IDR of AU 45")


@case
def drop_last_gop(S):
    return _drop_case(S, "drop_last_gop", 52, 3, 4, "loss in the last GOP: no later IDR, so everything up to the end is concealed")


@case
def drop_two(S):
    aus, pk = _a(S)
    pk2 = drop(pk, au_range(pk, 5, 2, 3) + au_range(pk, 33, 2, 2))
    return finish("drop_two", aus, pk2, make_expect(aus, survivors(pk2, 60), zones=[(5, 15, "conceal"), (33, 45, "conceal")]),
                  "two separate losses (AU 5 and AU 33): two concealment zones, each ended by its next IDR")


def _mod_case(S, name, k, fn, note, zone="may"):
    aus, pk = _a(S)
    pk = fn(pk)
    return finish(name, aus, pk, make_expect(aus, survivors(pk, 60), zones=[(k, next_idr(aus, k), zone)]), note)


def _corrupt(k, pos):
    def f(pk):
        i = idx_of(pk, k, pos)
        b = bytearray(pk[i]["b"])
        for o in range(60, 70):
            b[o] ^= 0x5A
            assert b[o] != 0
        pk = list(pk)
        pk[i] = dict(pk[i], b=bytes(b))
        return pk
    return f


@case
def corrupt_p(S):
    return _mod_case(S, "corrupt_p", 38, _corrupt(38, 4), "ten payload bytes of a picture (AU 38) are damaged, CCs intact: AUs 38..44 may be concealed or garbage")


@case
def corrupt_idr(S):
    return _mod_case(S, "corrupt_idr", 15, _corrupt(15, 3), "ten payload bytes inside the IDR of AU 15 are damaged: AUs 15..29 are not checked, the IDR of AU 30 recovers")


@case
def tei(S):
    def f(pk):
        i = idx_of(pk, 40, 3)
        pk = list(pk)
        pk[i] = dict(pk[i], b=bytes([pk[i]["b"][0], pk[i]["b"][1] | 0x80]) + pk[i]["b"][2:])
        return pk
    return _mod_case(S, "tei", 40, f, "transport_error_indicator set on a mid-PES packet of AU 40 (payload intact): treat the packet as lost", zone="conceal")


@case
def trunc_pkts(S):
    aus, pk = _a(S)
    pk = pk[:idx_of(pk, 57, 3) + 1]
    return finish("trunc_pkts", aus, pk, make_expect(aus, survivors(pk, 60), zones=[(57, 58, "may")]),
                  "the stream ends after the 4th packet of AU 57: AUs 0..56 are shown (flush at end of input); AU 57 is incomplete")


@case
def trunc_bytes(S):
    aus, pk = _a(S)
    i = idx_of(pk, 57, 3)
    pk = pk[:i] + [raw(pk[i]["b"][:100])]
    pk[-1]["au"] = None
    exp = make_expect(aus, survivors(pk, 60), zones=[(57, 58, "may")])
    return finish("trunc_bytes", aus, pk, exp, "the file ends in the middle of a packet (100 bytes of 188), inside AU 57")


def _restart(S, name, off2, note):
    big = 90000 * 3600 * 5
    aus = take(S["A"], 0, 21, big, 0) + take(S["A"], 0, 60, off2, 1)
    pk = tsmux.mux(aus, force_psi={21}, reset_cc={21})
    pk = drop(pk, [i for i, p in enumerate(pk) if p["au"] == 20 and p["pos"] >= 4])
    return finish(name, aus, pk, make_expect(aus, survivors(pk, len(aus)), zones=[(20, 21, "may")]), note, udp=name == "restart_back")


@case
def restart_back(S):
    return _restart(S, "restart_back", 0, "sender killed in AU 20 and restarted from the start of the file: PTS jumps backwards, CC counters restart, new IDR")


@case
def restart_fwd(S):
    return _restart(S, "restart_fwd", 90000 * 3600 * 5 + 90000 * 60, "same restart, but the PTS jumps forward by one minute")


@case
def res_up(S):
    a = take(S["A"], 0, 30, 0, 0)
    w = cont(a, S["W"], 0, 30, 1)
    aus = a + w
    pk = tsmux.mux(aus)
    return finish("res_up", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "64x48 for 30 AUs, then an IDR with a new SPS (96x64, same SPS id 0): flush, resize notification, new DPB", udp=True)


@case
def res_down_up(S):
    w1 = take(S["W"], 0, 30, 0, 0)
    a = cont(w1, S["A"], 0, 30, 1)
    w2 = cont(a, S["W"], 0, 30, 2)
    aus = w1 + a + w2
    pk = tsmux.mux(aus)
    return finish("res_down_up", aus, pk, make_expect(aus, survivors(pk, 90)), "96x64, then 64x48, then 96x64 again (three activations)")


@case
def sps_same_size(S):
    a = take(S["A"], 0, 30, 0, 0)
    s = cont(a, S["S"], 0, 30, 1)
    aus = a + s
    pk = tsmux.mux(aus)
    return finish("sps_same_size", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "same SPS id and picture size, different SPS content (reference frames, B-frames off): activate at the IDR, no resize notification")


@case
def sps_new_id(S):
    a = take(S["A"], 0, 30, 0, 0)
    s = cont(a, S["S"], 0, 30, 1, sps_id=1)
    aus = a + s
    pk = tsmux.mux(aus)
    return finish("sps_new_id", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "the second half uses SPS id 1 (the PPS now points at it): the active SPS changes at the IDR, same size")


@case
def garbage_prefix(S):
    aus, pk = _a(S)
    rng = random.Random(5)
    pk = [raw(noise_bytes(rng, 301, True))] + pk
    return finish("garbage_prefix", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "301 bytes of garbage (with some lone 0x47 bytes) before the first packet: find the real sync, lose nothing", udp=False)


@case
def garbage_between(S):
    aus, pk = _a(S)
    rng = random.Random(6)
    for at, n in ((200, 37), (150, 187), (60, 100)):
        pk = pk[:at] + [raw(noise_bytes(rng, n))] + pk[at:]
    return finish("garbage_between", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "37, 187 and 100 garbage bytes between two packets (no packet is damaged): keep every real packet")


@case
def short_packet(S):
    aus, pk = _a(S)
    i = idx_of(pk, 26, 3)
    pk = pk[:i] + [raw(pk[i]["b"][:128])] + pk[i + 1:]
    return finish("short_packet", aus, pk, make_expect(aus, survivors(pk, 60), zones=[(26, 30, "conceal")]),
                  "one packet of AU 26 is only 128 bytes long (the next packet follows 60 bytes early): that packet is lost, the rest is intact")


def _payload_case(S, name, k, fn, note, zone="may"):
    aus, pk = _a(S)
    aus[k] = dict(aus[k], payload=fn(aus[k]["payload"]))
    pk = tsmux.mux(aus)
    return finish(name, aus, pk, make_expect(aus, survivors(pk, 60), zones=[(k, next_idr(aus, k), zone)]), note)


@case
def trunc_nal(S):
    return _payload_case(S, "trunc_nal", 22, lambda b: b[:len(b) * 55 // 100], "the slice NAL of AU 22 is cut off at 55 % (a clean TS carrying a truncated NAL)")


@case
def garbage_nal(S):
    def f(b):
        rng = random.Random(9)
        return b[:24] + noise_bytes(rng, len(b) - 24)
    return _payload_case(S, "garbage_nal", 22, f, "the slice NAL of AU 22 is replaced by random bytes (start code and NAL header kept)")


@case
def junk_nals(S):
    junk = [bytes([0x0C]) + b"\xff" * 9 + b"\x80", bytes([0x18, 1, 2, 3, 4, 5]), bytes([0x1F, 0x55, 0x66]),
            bytes([0x13, 9, 9, 9, 0x80]), bytes([0x9C, 1, 2, 3])]
    aus, pk = _a(S)
    for i in range(0, 60, 3):
        nals = [n for _o, n in H.split_nals(aus[i]["payload"])]
        j = junk[i % len(junk)]
        nals = nals[:1] + [j] + nals[1:] + [junk[(i + 1) % len(junk)]]
        aus[i] = dict(aus[i], payload=b"".join(b"\x00\x00\x00\x01" + n for n in nals))
    pk = tsmux.mux(aus)
    return finish("junk_nals", aus, pk, make_expect(aus, survivors(pk, 60)),
                  "filler (12), reserved (13, 19, 24, 31) and forbidden_zero_bit NAL units inside every third AU: ignore them all")


@case
def all_garbage(S):
    rng = random.Random(11)
    pk = [raw(noise_bytes(rng, 3000, True))]
    for i in range(60):
        hdr = bytes([0x47, (i * 37) & 0x1F | (0x40 if i % 3 == 0 else 0), (i * 91) & 0xFF, 0x10 | (i & 15)])
        pk.append(raw(hdr + noise_bytes(rng, 184)))
    pk.append(raw(noise_bytes(rng, 500, True)))
    return finish("all_garbage", [], pk, [], "no video at all: random bytes and 188-byte noise packets; exit 0, no frames")


@case
def empty_input(S):
    return finish("empty_input", [], [], [], "zero bytes of input; exit 0, no frames")


def make_all(work, outdir, only=None):
    S = build_sources(work)
    names = []
    for name, fn in CASES.items():
        if only and name not in only:
            continue
        data, truth, ref = fn(S)
        open(os.path.join(outdir, name + ".ts"), "wb").write(data)
        json.dump(truth, open(os.path.join(outdir, name + ".json"), "w"))
        json.dump(ref, open(os.path.join(outdir, name + ".ref.json"), "w"))
        names.append(name)
    open(os.path.join(outdir, "cases.txt"), "w").write("\n".join(names) + "\n")
    return names
