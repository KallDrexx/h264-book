"""SPOILER: the slice-header facts Chapter 7.8's reference needs that cavlc_ref's "first values" dictionary cannot hold because the
elements repeat: ref_pic_list_modification commands of BOTH lists (7.3.3.1), pred_weight_table for list 0 and list 1 (7.3.3.2) and
dec_ref_pic_marking (7.3.3.3); plus the POC-related and list-size values.  Source: the ordered element list of ffmpeg's trace_headers
(<workdir>/ref_trace.txt, written by cavlc_ref.load_stream).

load(trace_path) -> one dict per slice NAL unit (types 1 and 5, stream order):
    slice_type (mod 5), nal_ref_idc, idr, frame_num, hdr (first values), active0, active1 (after the override rule; 0 if unused),
    mods0, mods1  [(modification_of_pic_nums_idc, value)] (the closing idc 3 is not included),
    mmco None | [(op, {argument: value})],  lt_flag,
    wt  None | dict(denom_y, denom_c, flags0, entries0, flags1, entries1): entries have the defaults filled in (weight 2^denom,
        offset 0 when a flag is 0): dict(wy, oy, wcb, ocb, wcr, ocr);  flags = [(luma_flag, chroma_flag)].
        Present for P slices with weighted_pred_flag = 1 and B slices with weighted_bipred_idc = 1.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from common import cavlc_ref  # noqa: E402

MMCO_ARGS = {1: ("difference_of_pic_nums_minus1",), 2: ("long_term_pic_num",),
             3: ("difference_of_pic_nums_minus1", "long_term_frame_idx"), 4: ("max_long_term_frame_idx_plus1",),
             5: (), 6: ("long_term_frame_idx",)}


def parse_mods(names, which):
    idx = next((i for i, (n, _) in enumerate(names) if n == f"ref_pic_list_modification_flag_l{which}"), None)
    mods = []
    if idx is not None and names[idx][1] == 1:
        i = idx + 1
        while i < len(names) and names[i][0] == "modification_of_pic_nums_idc":
            idc = names[i][1]
            i += 1
            if idc == 3:
                break
            mods.append((idc, names[i][1]))
            i += 1
    return mods


def parse_mmco(names):
    idx = next((i for i, (n, _) in enumerate(names) if n == "adaptive_ref_pic_marking_mode_flag"), None)
    if idx is None or names[idx][1] != 1:
        return None
    out, i = [], idx + 1
    while i < len(names) and names[i][0] == "memory_management_control_operation":
        op = names[i][1]
        i += 1
        if op == 0:
            break
        a = {}
        for k in MMCO_ARGS[op]:
            a[k] = names[i][1]
            i += 1
        out.append((op, a))
    return out


def parse_weights(names, active0, active1):
    byname = {n: v for n, v in names}
    if "luma_log2_weight_denom" not in byname:
        return None
    dy = byname["luma_log2_weight_denom"]
    dc = byname.get("chroma_log2_weight_denom", 0)
    out = dict(denom_y=dy, denom_c=dc)
    for lst, active in ((0, active0), (1, active1)):
        entries, flags = [], []
        for i in range(active):
            lf = byname.get(f"luma_weight_l{lst}_flag[{i}]", 0)
            cf = byname.get(f"chroma_weight_l{lst}_flag[{i}]", 0)
            e = dict(wy=1 << dy, oy=0, wcb=1 << dc, ocb=0, wcr=1 << dc, ocr=0)
            if lf:
                e["wy"], e["oy"] = byname[f"luma_weight_l{lst}[{i}]"], byname[f"luma_offset_l{lst}[{i}]"]
            if cf:
                e["wcb"], e["ocb"] = byname[f"chroma_weight_l{lst}[{i}][0]"], byname[f"chroma_offset_l{lst}[{i}][0]"]
                e["wcr"], e["ocr"] = byname[f"chroma_weight_l{lst}[{i}][1]"], byname[f"chroma_offset_l{lst}[{i}][1]"]
            entries.append(e)
            flags.append((lf, cf))
        out[f"entries{lst}"], out[f"flags{lst}"] = entries, flags
    return out


def load(trace_path):
    out, sps_t, pps_t = [], {}, {}
    for title, extradata, items in cavlc_ref._parse_trace(trace_path):
        d = cavlc_ref._first(items)
        if title == "Sequence Parameter Set":
            sps_t[d["seq_parameter_set_id"]] = d
        elif title == "Picture Parameter Set":
            pps_t[d["pic_parameter_set_id"]] = d
        elif not extradata and d.get("nal_unit_type") in (1, 5):
            names = [(n, v) for _, n, _, v in items]
            pps = pps_t[d["pic_parameter_set_id"]]
            st = d["slice_type"] % 5
            ov = d.get("num_ref_idx_active_override_flag", 0)
            a0 = a1 = 0
            if st in (0, 1):
                a0 = d["num_ref_idx_l0_active_minus1"] + 1 if ov else pps["num_ref_idx_l0_default_active_minus1"] + 1
            if st == 1:
                a1 = d["num_ref_idx_l1_active_minus1"] + 1 if ov else pps["num_ref_idx_l1_default_active_minus1"] + 1
            wt = None
            if (st == 0 and pps.get("weighted_pred_flag", 0)) or (st == 1 and pps.get("weighted_bipred_idc", 0) == 1):
                wt = parse_weights(names, a0, a1)
            out.append(dict(slice_type=st, nal_ref_idc=d["nal_ref_idc"], idr=d["nal_unit_type"] == 5, frame_num=d["frame_num"],
                            hdr=d, active0=a0, active1=a1,
                            mods0=parse_mods(names, 0) if st in (0, 1) else [], mods1=parse_mods(names, 1) if st == 1 else [],
                            mmco=parse_mmco(names), lt_flag=d.get("long_term_reference_flag", 0), wt=wt,
                            bipred_idc=pps.get("weighted_bipred_idc", 0) if st == 1 else 0))
    return out
