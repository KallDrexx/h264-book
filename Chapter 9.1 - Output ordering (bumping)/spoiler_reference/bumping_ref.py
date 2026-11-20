"""SPOILER: reference implementation of the output-order DPB of C.4 ("bumping", C.4.4, C.4.5.1 - C.4.5.3), for frame pictures.

run(pics) -> (events, final_out)
    pics: list of dicts (keys id, poc, idr, mmco5, ref, noout, unref, profile, level, cs3, wmbs, hmbs, mdfb) in decoding order,
          as produced by frontend.load_pictures() or parse_trace().
    events: one (id, [ids output while this picture was processed, in output order], fullness_after) per picture.
    final_out: the ids output by the flush at the end of the stream.

Rules implemented (the SPEC rule set; ffmpeg deviates, see the Readme):
  * DPB size = max_dec_frame_buffering if the SPS has one, else 0 for intra-only profiles (profile_idc 44, 86, 100, 110, 122,
    244 with constraint_set3_flag = 1), else Min(MaxDpbMbs / (PicWidthInMbs * FrameHeightInMbs), 16).
    A size of 0 means pictures never wait: every picture is output as soon as it is decoded.
  * IDR picture / picture with memory_management_control_operation 5: all frames become unused for reference; with
    no_output_of_prior_pics_flag = 1 (IDR only) every frame buffer is emptied WITHOUT output, otherwise the buffers are emptied by
    bumping repeatedly (all waiting pictures are output in POC order).  The picture is then stored; an MMCO 5 picture has POC 0.
  * any other picture: its marking is applied (pic.unref lists the frames that stop being references), frame buffers that are
    neither needed for output nor used for reference are emptied; a reference picture waits for an empty buffer (bumping
    repeatedly); a non-reference picture is output immediately, not stored, if there is no empty buffer and its POC is lower
    than that of every waiting picture, else bumping repeatedly, then stored.
  * bumping: output the waiting picture with the smallest POC; its buffer is emptied if it is no longer a reference.
  * end of stream: bumping repeatedly until the DPB is empty.

MUTANT: the harness sets this to one of MUTANTS (a dict name -> description) to build deliberately wrong readers.
"""
MUTANT = None
LENIENT = False        # True: a stream that overflows its DPB size is handled by growing the DPB (see make_room)

MAX_DPB_MBS = {9: 396, 10: 396, 11: 900, 12: 2376, 13: 2376, 20: 2376, 21: 4752, 22: 8100, 30: 8100, 31: 18000, 32: 20480,
               40: 32768, 41: 32768, 42: 34816, 50: 110400, 51: 184320, 52: 184320, 60: 696320, 61: 696320, 62: 696320}

MUTANTS = {
    "dpb_vui_ignored": "DPB size always from the level, even when max_dec_frame_buffering is present",
    "dpb_minus1": "DPB size one frame too small",
    "dpb_plus1": "DPB size one frame too large",
    "dpb_no_cap16": "level-derived DPB size not capped at 16 frames",
    "level_1b_wrong": "level_idc 11 with constraint_set3_flag (level 1b) treated as level 1.1",
    "bump_max_poc": "bumping outputs the LARGEST POC",
    "bump_fifo": "bumping outputs the oldest picture in decoding order instead of the smallest POC",
    "nonref_always_stored": "a non-reference picture is never output immediately (always waits for a bump)",
    "nonref_immediate_any_poc": "a non-reference picture is output immediately when the DPB is full, whatever its POC",
    "idr_no_flush": "IDR picture does not flush the waiting pictures (they stay and compete by POC)",
    "idr_ignores_noout": "no_output_of_prior_pics_flag ignored (prior pictures are always output)",
    "mmco5_no_flush": "MMCO 5 does not flush the waiting pictures",
    "mmco5_discards": "MMCO 5 discards the waiting pictures like no_output_of_prior_pics_flag = 1",
    "mmco5_poc_not_rebased": "the POC of an MMCO 5 picture is kept instead of becoming 0",
    "no_end_flush": "pictures still waiting at the end of the stream are never output",
    "end_flush_decode_order": "the end-of-stream flush outputs in decoding order instead of POC order",
    "flush_one": "IDR / MMCO 5 flush outputs only one picture",
    "bump_frees_ref": "a bumped picture's buffer is emptied even though it is still a reference",
    "store_then_bump": "the picture is stored first and bumping happens afterwards (buffer count may exceed the size)",
    "dead_frames_linger": "frames that are unused for reference and already output are not emptied until the next bump",
    "poc_compare_abs": "POCs compared by absolute value (wrong for negative POC)",
    "size0_stores": "a DPB size of 0 is treated as 1 (the picture waits)",
    "nonref_immediate_when_free": "a lowest-POC non-reference picture is output immediately even when a frame buffer is empty (JM)",
    "intra_profile_level_size": "intra-only profile (constraint_set3_flag) without bitstream_restriction gets the level size, not 0",
}

INTRA_PROFILES = (44, 86, 100, 110, 122, 244)     # with constraint_set3_flag = 1: intra-only profiles (E.2.1 inference)


class DpbOverflow(Exception):
    pass


def level_mbs(pic):
    lv = pic["level"]
    if lv == 11 and pic["cs3"] and pic["profile"] in (66, 77, 88) and MUTANT != "level_1b_wrong":
        return 396
    if lv not in MAX_DPB_MBS:
        raise ValueError(f"unknown level_idc {lv}")
    return MAX_DPB_MBS[lv]


def dpb_size(pic):
    if pic["mdfb"] is not None and MUTANT != "dpb_vui_ignored":
        n = pic["mdfb"]
    elif pic["profile"] in INTRA_PROFILES and pic["cs3"] and MUTANT != "intra_profile_level_size":
        n = 0                                       # E.2.1: max_dec_frame_buffering inferred 0 for intra profiles
    else:
        n = level_mbs(pic) // (pic["wmbs"] * pic["hmbs"])
        if MUTANT != "dpb_no_cap16":
            n = min(n, 16)
    if MUTANT == "dpb_minus1":
        n = max(n - 1, 0)
    if MUTANT == "dpb_plus1":
        n += 1
    if MUTANT == "size0_stores":
        n = max(n, 1)
    return n


def _key(f):
    return abs(f["poc"]) if MUTANT == "poc_compare_abs" else f["poc"]


def run(pics):
    dpb, events = [], []                  # frame buffers: dict(id, poc, need, ref)
    grow = [0]

    def bump(out):
        waiting = [f for f in dpb if f["need"]]
        if not waiting:
            raise DpbOverflow("no picture to output but no empty frame buffer")
        if MUTANT == "bump_max_poc":
            f = max(waiting, key=_key)
        elif MUTANT == "bump_fifo":
            f = min(waiting, key=lambda g: g["id"])
        else:
            f = min(waiting, key=_key)
        out.append(f["id"])
        f["need"] = False
        if not f["ref"] or MUTANT == "bump_frees_ref":
            dpb.remove(f)

    def make_room(out, size):
        """Bump repeatedly until a frame buffer is empty.  A strict decoder fails when every buffer holds a reference
        picture (bumping can never free one: the stream does not fit its DPB size); LENIENT grows the DPB by one frame."""
        while len(dpb) >= size + grow[0]:
            if LENIENT and dpb and all(f["ref"] for f in dpb):
                grow[0] += 1
                continue
            bump(out)

    def drop_dead():
        if MUTANT == "dead_frames_linger":
            return
        dpb[:] = [f for f in dpb if f["need"] or f["ref"]]

    for p in pics:
        out = []
        size = dpb_size(p)
        poc = p["poc"]
        flush_event = False
        if p["idr"] or p["mmco5"]:
            grow[0] = 0
            for f in dpb:
                f["ref"] = False
            if p["mmco5"]:
                poc = poc if MUTANT == "mmco5_poc_not_rebased" else 0
            discard = (p["noout"] and p["idr"] and MUTANT != "idr_ignores_noout") or (p["mmco5"] and MUTANT == "mmco5_discards")
            if discard:
                dpb.clear()
            elif (p["idr"] and MUTANT == "idr_no_flush") or (p["mmco5"] and MUTANT == "mmco5_no_flush"):
                drop_dead()
            elif MUTANT == "flush_one":
                if any(f["need"] for f in dpb):
                    bump(out)
                dpb.clear()
            else:
                while any(f["need"] for f in dpb):
                    bump(out)
                dpb.clear()
            flush_event = True
        else:
            for f in dpb:
                if f["id"] in p["unref"]:
                    f["ref"] = False
            drop_dead()
        cur = dict(id=p["id"], poc=poc, need=True, ref=bool(p["ref"]))
        stored = True
        if size == 0:
            out.append(cur["id"])
            stored = False
        elif MUTANT == "store_then_bump":
            dpb.append(cur)
            stored = False
            while len(dpb) > size + grow[0]:
                bump(out)
        elif flush_event:
            pass
        elif p["ref"]:
            make_room(out, size)
        else:
            if MUTANT == "nonref_immediate_when_free" and all(_key(cur) < _key(f) for f in dpb if f["need"]):
                out.append(cur["id"])
                stored = False
            elif len(dpb) >= size + grow[0]:
                lower = all(_key(cur) < _key(f) for f in dpb if f["need"])
                if MUTANT == "nonref_always_stored":
                    lower = False
                if MUTANT == "nonref_immediate_any_poc":
                    lower = True
                if lower:
                    out.append(cur["id"])
                    stored = False
                else:
                    make_room(out, size)
        if stored:
            dpb.append(cur)
        events.append((p["id"], out, len(dpb)))
    final = []
    if MUTANT == "no_end_flush":
        pass
    elif MUTANT == "end_flush_decode_order":
        final = [f["id"] for f in sorted(dpb, key=lambda f: f["id"]) if f["need"]]
    else:
        while any(f["need"] for f in dpb):
            bump(final)
    return events, final


def output_order(pics):
    events, final = run(pics)
    return [i for _, out, _ in events for i in out] + final


# ---- the input trace format (see the Readme) -------------------------------------------------------------------------

TRACE_FIELDS = ("id", "poc", "idr", "mmco5", "ref", "noout", "unref", "profile", "level", "cs3", "wmbs", "hmbs", "mdfb")


def format_trace(pics):
    lines = []
    for p in pics:
        lines.append("PIC id=%d poc=%d idr=%d mmco5=%d ref=%d noout=%d unref=%s profile=%d level=%d cs3=%d wmbs=%d hmbs=%d mdfb=%s" % (
            p["id"], p["poc"], p["idr"], p["mmco5"], p["ref"], p["noout"], ",".join(map(str, p["unref"])) or "-",
            p["profile"], p["level"], p["cs3"], p["wmbs"], p["hmbs"], "-" if p["mdfb"] is None else p["mdfb"]))
    return "\n".join(lines) + "\n"


def parse_trace(text):
    pics = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] != "PIC":
            raise ValueError(f"bad trace line {line!r}")
        kv = dict(t.split("=", 1) for t in parts[1:])
        p = {k: int(kv[k]) for k in ("id", "poc", "idr", "mmco5", "ref", "noout", "profile", "level", "cs3", "wmbs", "hmbs")}
        p["unref"] = [] if kv["unref"] == "-" else [int(x) for x in kv["unref"].split(",")]
        p["mdfb"] = None if kv["mdfb"] == "-" else int(kv["mdfb"])
        pics.append(p)
    return pics


def format_output(events, final):
    lines = ["AFTER %d out=%s full=%d" % (i, ",".join(map(str, out)) or "-", full) for i, out, full in events]
    lines.append("END out=%s" % (",".join(map(str, final)) or "-"))
    return "\n".join(lines) + "\n"
