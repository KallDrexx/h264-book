"""SPOILER: the slice-header tables that Chapter 7.7 needs and that cavlc_ref's "first values" dictionary cannot hold because the
elements repeat: ref_pic_list_modification commands (7.3.3.1), pred_weight_table (7.3.3.2) and dec_ref_pic_marking (7.3.3.3).

Source: the ordered element list of ffmpeg's trace_headers (the file cavlc_ref.load_stream writes as <workdir>/ref_trace.txt).
Everything is read in syntax order, by element name, exactly like the syntax tables prescribe.

load(trace_path) -> list with one dict per slice NAL unit (types 1 and 5, in stream order):
    mods        [(modification_of_pic_nums_idc, value), ...]  list 0 only (the final idc 3 is not included)
    mmco        None | [(op, {argument: value})]              dec_ref_pic_marking of this slice (None: sliding window / IDR)
    lt_flag     long_term_reference_flag (IDR) else 0
    wt          None | dict(denom_y, denom_c, flags=[(luma_flag, chroma_flag)], entries=[dict(wy, oy, wcb, ocb, wcr, ocr)])
                entries are given for every ref_idx 0..active-1 with the DEFAULTS filled in when a flag is 0
                (weight 2^denom, offset 0).  None when the slice has no pred_weight_table.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from common import cavlc_ref  # noqa: E402

MMCO_ARGS = {1: ("difference_of_pic_nums_minus1",), 2: ("long_term_pic_num",),
             3: ("difference_of_pic_nums_minus1", "long_term_frame_idx"), 4: ("max_long_term_frame_idx_plus1",),
             5: (), 6: ("long_term_frame_idx",)}


def parse_mods(names):
    idx = next((i for i, (n, _) in enumerate(names) if n == "ref_pic_list_modification_flag_l0"), None)
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


def parse_weights(names, active):
    """pred_weight_table of a P slice with `active` entries (4:2:0: the chroma elements are present)."""
    vals = {}
    for n, v in names:
        vals.setdefault(n, [])
        vals[n].append(v)
    if "luma_log2_weight_denom" not in vals:
        return None
    dy = vals["luma_log2_weight_denom"][0]
    dc = vals["chroma_log2_weight_denom"][0] if "chroma_log2_weight_denom" in vals else 0
    byname = {n: v for n, v in names}
    entries, flags = [], []
    for i in range(active):
        lf = byname.get(f"luma_weight_l0_flag[{i}]", 0)
        cf = byname.get(f"chroma_weight_l0_flag[{i}]", 0)
        e = dict(wy=1 << dy, oy=0, wcb=1 << dc, ocb=0, wcr=1 << dc, ocr=0)
        if lf:
            e["wy"], e["oy"] = byname[f"luma_weight_l0[{i}]"], byname[f"luma_offset_l0[{i}]"]
        if cf:
            e["wcb"], e["ocb"] = byname[f"chroma_weight_l0[{i}][0]"], byname[f"chroma_offset_l0[{i}][0]"]
            e["wcr"], e["ocr"] = byname[f"chroma_weight_l0[{i}][1]"], byname[f"chroma_offset_l0[{i}][1]"]
        entries.append(e)
        flags.append((lf, cf))
    return dict(denom_y=dy, denom_c=dc, flags=flags, entries=entries)


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
            active = 0
            if st == 0:
                active = (d["num_ref_idx_l0_active_minus1"] + 1 if d.get("num_ref_idx_active_override_flag", 0)
                          else pps["num_ref_idx_l0_default_active_minus1"] + 1)
            wt = parse_weights(names, active) if st == 0 and pps.get("weighted_pred_flag", 0) else None
            out.append(dict(mods=parse_mods(names) if st == 0 else [], mmco=parse_mmco(names),
                            lt_flag=d.get("long_term_reference_flag", 0), wt=wt, active=active, slice_type=st))
    return out
