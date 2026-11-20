"""SPOILER: the presentation plan of Chapter 10.5 - which fields of which decoded frame are shown, in which order, at which tick.

scan(trace_path)     parse ffmpeg's trace_headers log of a stream into coded pictures (decoding order) with their field_pic_flag,
                     bottom_field_flag, picture order counts (computed here, 8.2.1) and the pic_struct of the pic_timing SEI in front
                     of them (D.2.2 / Table D-1), then group them into frames and put the frames in output order.
plan(frames)         -> (frame_lines, out_lines) = the expected presentation log:
                     FRAME k coded=<F|TB|BT> ps=<0..8|-> seq=<string of T/B/F>      and      OUT n frame=k field=<T|B|F> tick=t

The rules (Readme section 3):
  * field pair (two field pictures): SEI ignored; the field with the smaller POC first ("TB" if TopFieldOrderCnt < BottomFieldOrderCnt,
    "BT" if greater); equal POCs: the field decoded first.
  * frame picture with a pic_timing SEI (VUI pic_struct_present_flag = 1): seq from Table D-1:
        0 F   1 T   2 B   3 TB   4 BT   5 TBT   6 BTB   7 FF   8 FFF
  * frame picture without one: frame_mbs_only_flag = 1 -> "F"; otherwise "TB" if TopFieldOrderCnt <= BottomFieldOrderCnt else "BT".
  * tick: starts at 0; an output of one field advances it by 1, an output of a whole frame ("F") by 2.
  * output order of the frames: ascending PicOrderCnt (= min of the two field POCs) inside each IDR / memory_management_control_operation 5 period.
MUTANT (harness self-test) switches break exactly one rule; see MUTANTS.
"""
import re

MUTANT = None

MUTANTS = {
    "ignore_ps": "pic_timing SEI ignored: frames always use the POC rule",
    "always_tff": "top field always first (frames and field pairs)",
    "always_bff": "bottom field always first (frames and field pairs)",
    "swap_ps34": "pic_struct 3 and 4 swapped",
    "ps5_no_repeat": "pic_struct 5 shows TB (no repeated field)",
    "ps6_no_repeat": "pic_struct 6 shows BT (no repeated field)",
    "ps5_wrong_order": "pic_struct 5 shows TBB instead of TBT",
    "ps7_once": "pic_struct 7 (frame doubling) shows the frame once",
    "ps8_twice": "pic_struct 8 (frame tripling) shows the frame twice",
    "ps1_as_frame": "pic_struct 1 and 2 (single field) shown as the whole frame",
    "ps2_as_top": "pic_struct 2 (bottom field only) shown as the top field",
    "pair_always_tb": "a field pair is always shown top field first",
    "tie_bottom_first": "equal top / bottom POC: bottom field first",
    "interlaced_as_frame": "no SEI, frame_mbs_only_flag = 0: frame shown whole instead of as two fields",
    "progressive_as_fields": "no SEI, frame_mbs_only_flag = 1: frame shown as two fields",
    "tick_field_2": "a field output advances the tick by 2",
    "tick_frame_1": "a whole-frame output advances the tick by 1",
    "tick_per_frame": "the tick restarts at 0 for every frame",
    "decode_order": "frames output in decoding order instead of POC order",
    "ps_prev_picture": "the SEI is attached to the previous picture instead of the following one",
    "pair_decode_order": "a field pair is shown in decoding order even when the field POCs say otherwise",
}

PS_SEQ = {0: "F", 1: "T", 2: "B", 3: "TB", 4: "BT", 5: "TBT", 6: "BTB", 7: "FF", 8: "FFF"}

_PREFIX = re.compile(r"^\[trace_headers @ [^\]]*\] ?")
_FIELD = re.compile(r"^(\d+)\s+(\S+)\s+([01]*)\s+=\s+(-?\d+)\s*$")
_TITLES = ("Sequence Parameter Set", "Picture Parameter Set", "Slice Header", "Picture Timing", "Supplemental Enhancement Information",
           "Access Unit Delimiter")


class Unsupported(Exception):
    pass


def parse_trace(path):
    """-> list of (title, [(name, value)]) for the blocks we need."""
    blocks, cur = [], None
    with open(path, "r", errors="replace") as f:
        for raw in f:
            if raw.startswith("[") and not raw.startswith("[trace_headers"):
                continue
            line = _PREFIX.sub("", raw.rstrip("\n"))
            m = _FIELD.match(line)
            if m:
                if cur is not None:
                    cur[1].append((m.group(2), int(m.group(4))))
                continue
            cur = None
            if line in _TITLES:
                cur = (line, [])
                blocks.append(cur)
                # a "Picture Timing" block continues the SEI block's elements: keep them in their own block
    return blocks


def _first(items):
    d = {}
    for n, v in items:
        d.setdefault(n, v)
    return d


class _Poc:
    """8.2.1 for frames and fields."""

    def __init__(self):
        self.prev_msb = self.prev_lsb = 0
        self.prev_off = self.prev_fn = 0

    def picture(self, sps, pps, h, idr, ref_idc, field, bottom, mmco5):
        t = sps["pic_order_cnt_type"]
        fn = h["frame_num"]
        max_fn = 1 << (sps["log2_max_frame_num_minus4"] + 4)
        top = bot = None
        msb = lsb = off = None
        if t == 0:
            max_lsb = 1 << (sps["log2_max_pic_order_cnt_lsb_minus4"] + 4)
            pm, pl = (0, 0) if idr else (self.prev_msb, self.prev_lsb)
            lsb = h["pic_order_cnt_lsb"]
            if lsb < pl and pl - lsb >= max_lsb // 2:
                msb = pm + max_lsb
            elif lsb > pl and lsb - pl > max_lsb // 2:
                msb = pm - max_lsb
            else:
                msb = pm
            if not field:
                top = msb + lsb
                bot = top + h.get("delta_pic_order_cnt_bottom", 0)
            elif bottom:
                bot = msb + lsb
            else:
                top = msb + lsb
        else:
            off = 0 if idr else (self.prev_off + max_fn if self.prev_fn > fn else self.prev_off)
            if t == 2:
                tmp = 0 if idr else (2 * (off + fn) - (1 if ref_idc == 0 else 0))
                if not field or not bottom:
                    top = tmp
                if not field or bottom:
                    bot = tmp
            else:
                n = sps["num_ref_frames_in_pic_order_cnt_cycle"]
                offs = [sps[f"offset_for_ref_frame[{i}]"] for i in range(n)]
                absfn = off + fn if n else 0
                if ref_idc == 0 and absfn > 0:
                    absfn -= 1
                exp = 0
                if absfn > 0:
                    cyc, inc = divmod(absfn - 1, n)
                    exp = cyc * sum(offs) + sum(offs[:inc + 1])
                if ref_idc == 0:
                    exp += sps["offset_for_non_ref_pic"]
                d0, d1 = h.get("delta_pic_order_cnt[0]", 0), h.get("delta_pic_order_cnt[1]", 0)
                if not field:
                    top = exp + d0
                    bot = top + sps["offset_for_top_to_bottom_field"] + d1
                elif bottom:
                    bot = exp + sps["offset_for_top_to_bottom_field"] + d0
                else:
                    top = exp + d0
        # state for the next picture
        raw = (top, bot)
        if mmco5:
            base = min(x for x in raw if x is not None)
            top = None if top is None else top - base
            bot = None if bot is None else bot - base
        if t == 0:
            if ref_idc:
                if mmco5:
                    self.prev_msb, self.prev_lsb = 0, (0 if (field and bottom) else (top if top is not None else 0))
                else:
                    self.prev_msb, self.prev_lsb = msb, lsb
        else:
            self.prev_off = 0 if mmco5 else off
            self.prev_fn = 0 if mmco5 else fn
        return top, bot


def scan(trace_path, blocks=None):
    """-> (pictures, sps) in decoding order.  picture: dict(field, bottom, idr, mmco5, frame_num, top, bot, ps, ref_idc)."""
    sps_t, pps_t, pics = {}, {}, []
    pending_ps = None
    poc = _Poc()
    last_first_mb = None
    cur_sps = None
    for title, items in (blocks if blocks is not None else parse_trace(trace_path)):
        d = _first(items)
        if title == "Sequence Parameter Set":
            sps_t[d["seq_parameter_set_id"]] = dict(d, **{n: v for n, v in items if n.startswith("offset_for_ref_frame[")})
        elif title == "Picture Parameter Set":
            pps_t[d["pic_parameter_set_id"]] = d
        elif title == "Picture Timing":
            if "pic_struct" in d:
                pending_ps = d["pic_struct"]
        elif title == "Slice Header" and d.get("nal_unit_type") in (1, 5):
            pps = pps_t[d["pic_parameter_set_id"]]
            sps = sps_t[pps["seq_parameter_set_id"]]
            cur_sps = sps
            fm = d["first_mb_in_slice"]
            if last_first_mb is not None and fm > last_first_mb:
                last_first_mb = fm
                continue                              # another slice of the same picture
            last_first_mb = fm
            idr = d["nal_unit_type"] == 5
            field = d.get("field_pic_flag", 0)
            bottom = d.get("bottom_field_flag", 0)
            mmco5 = any(n == "memory_management_control_operation" and v == 5 for n, v in items)
            top, bot = poc.picture(sps, pps, d, idr, d["nal_ref_idc"], field, bottom, mmco5)
            ps = pending_ps if sps.get("pic_struct_present_flag", 0) and pending_ps in PS_SEQ else None   # 9..15: reserved, as absent
            pending_ps = None
            pics.append(dict(field=field, bottom=bottom, idr=idr, mmco5=mmco5, frame_num=d["frame_num"], top=top, bot=bot, ps=ps,
                             ref_idc=d["nal_ref_idc"], fmo=sps.get("frame_mbs_only_flag", 1)))
    if not pics:
        raise Unsupported("no slices found in the trace")
    if MUTANT == "ps_prev_picture":
        shifted = [None] + [p["ps"] for p in pics[:-1]]
        for p, s in zip(pics, shifted):
            p["ps"] = s
    return pics, cur_sps


def frames_of(pics):
    """Group the coded pictures into frames (decoding order), then sort into output order. -> list of frame dicts."""
    stores, period, open_store = [], 0, None
    for p in pics:
        if p["idr"] or (p["mmco5"] and MUTANT != "mmco5_no_period"):
            period += 1
        if p["field"]:
            if (open_store is not None and open_store["kind"] == "P" and len(open_store["fields"]) == 1
                    and open_store["fields"][0]["bottom"] != p["bottom"] and open_store["fields"][0]["frame_num"] == p["frame_num"]
                    and not p["idr"]):
                open_store["fields"].append(p)
                for k in ("top", "bot"):
                    if p[k] is not None:
                        open_store[k] = p[k]
                open_store = None
                continue
            s = dict(kind="P", fields=[p], top=p["top"], bot=p["bot"], period=period, ps=None, fmo=p["fmo"])
            stores.append(s)
            open_store = s
        else:
            open_store = None
            stores.append(dict(kind="F", fields=[p], top=p["top"], bot=p["bot"], period=period, ps=p["ps"], fmo=p["fmo"]))
    for s in stores:
        if s["kind"] == "P" and len(s["fields"]) != 2:
            raise Unsupported("the stream contains a field picture without its partner field (ffmpeg does not output lone fields)")
    if MUTANT == "decode_order":
        return stores
    return sorted(stores, key=lambda s: (s["period"], min(s["top"], s["bot"])))


def sequence(s):
    """The display sequence of one frame: a string of T / B / F."""
    m = MUTANT
    if s["kind"] == "P":
        first = "TB" if not s["fields"][0]["bottom"] else "BT"
        if s["top"] == s["bot"] or m == "pair_decode_order":
            seq = first
        else:
            seq = "TB" if s["top"] < s["bot"] else "BT"
        if m == "pair_always_tb":
            seq = "TB"
    else:
        ps = s["ps"]
        if m == "ignore_ps":
            ps = None
        if ps is not None and ps in PS_SEQ:
            seq = PS_SEQ[ps]
            if m == "swap_ps34" and ps in (3, 4):
                seq = PS_SEQ[7 - ps]
            elif m == "ps5_no_repeat" and ps == 5:
                seq = "TB"
            elif m == "ps6_no_repeat" and ps == 6:
                seq = "BT"
            elif m == "ps5_wrong_order" and ps == 5:
                seq = "TBB"
            elif m == "ps7_once" and ps == 7:
                seq = "F"
            elif m == "ps8_twice" and ps == 8:
                seq = "FF"
            elif m == "ps1_as_frame" and ps in (1, 2):
                seq = "F"
            elif m == "ps2_as_top" and ps == 2:
                seq = "T"
        elif s["fmo"]:
            seq = "TB" if m == "progressive_as_fields" else "F"
        else:
            if m == "interlaced_as_frame":
                seq = "F"
            elif m == "tie_bottom_first":
                seq = "TB" if s["top"] < s["bot"] else "BT"
            else:
                seq = "TB" if s["top"] <= s["bot"] else "BT"
    if m == "always_tff" and seq in ("TB", "BT"):
        seq = "TB"
    if m == "always_bff" and seq in ("TB", "BT"):
        seq = "BT"
    return seq


def plan(stores):
    """-> (frame_lines, out_lines, outputs) ; outputs = [(frame index, field char, tick)]."""
    flines, olines, outs = [], [], []
    tick, n = 0, 0
    for k, s in enumerate(stores):
        seq = sequence(s)
        coded = "F" if s["kind"] == "F" else ("TB" if not s["fields"][0]["bottom"] else "BT")      # decoding order of the two fields
        ps = "-" if s["ps"] is None or s["kind"] == "P" else str(s["ps"])
        flines.append(f"FRAME {k} coded={coded} ps={ps} seq={seq}")
        if MUTANT == "tick_per_frame":
            tick = 0
        for ch in seq:
            olines.append(f"OUT {n} frame={k} field={ch} tick={tick}")
            outs.append((k, ch, tick))
            if ch == "F":
                tick += 1 if MUTANT == "tick_frame_1" else 2
            else:
                tick += 2 if MUTANT == "tick_field_2" else 1
            n += 1
    return flines, olines, outs
