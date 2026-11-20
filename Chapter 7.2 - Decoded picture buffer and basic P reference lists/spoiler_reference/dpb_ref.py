"""SPOILER: reference model of Chapter 7.2 (frame_num / FrameNumWrap / PicNum, sliding-window marking, default P list).

This is a complete solution of the chapter.  The harness (test_dpb.py) runs it on slice-header values and compares your log with
what it produces.  Do not read it before your own implementation passes.

simulate(pics, mutant=None) takes the pictures in decoding order (objects with the attributes listed in Pic) and returns
  slice_rows: one dict per P slice (pic, slice, frame_num, active, entries) where entries is a list of (pic_index, PicNum)
              or None ("no reference picture"), and
  dpb_rows:   one (pic_index, [(pic_index, frame_num), ...]) per picture, the short-term reference set AFTER the marking
              process of that picture, newest decoded picture first.
`mutant` switches ONE deliberately wrong rule on, so that the harness can show it detects each of them (see MUTANTS).
Only what 7.2 teaches is modelled: frames, short-term references, sliding window.  (7.6 adds MMCO, long-term, gaps, reordering.)
"""

MUTANTS = {
    "no_wrap": "FrameNumWrap = FrameNum (no subtraction of MaxFrameNum for frame numbers above the current one)",
    "maxfn_wrong": "MaxFrameNum computed as 2^log2_max_frame_num_minus4 (the +4 forgotten)",
    "victim_largest": "sliding window removes the reference with the LARGEST FrameNumWrap",
    "victim_raw": "sliding window picks the smallest raw FrameNum instead of the smallest FrameNumWrap",
    "window_late": "sliding window fires one picture too late (only when numShortTerm > max_num_ref_frames)",
    "ascending": "default P list sorted by ascending PicNum",
    "idr_keeps": "an IDR picture does not mark the earlier references as unused",
    "idr_not_stored": "an IDR picture itself does not enter the reference set",
    "nonref_stored": "pictures with nal_ref_idc = 0 are stored as references (and run the sliding window)",
    "trunc_plus1": "list truncated to num_ref_idx_active + 1 entries",
    "trunc_minus1": "list truncated to num_ref_idx_active - 1 entries",
    "pad_repeat": "a list shorter than num_ref_idx_active is padded by repeating its last entry",
    "mark_before": "sliding window applied BEFORE the lists of the current picture are built",
    "cur_in_list": "current picture marked (stored) before its own lists are built",
}


class Pic:
    """idx, idr (bool), ref_idc, frame_num, log2_max_frame_num, max_num_ref_frames, slices: list of dicts with
    'type' (0 P, 1 B, 2 I) and 'active' (num_ref_idx_l0_active, P slices only)."""


def simulate(pics, mutant=None):
    mu = mutant
    refs = []                                            # [(pic_index, frame_num)] short-term references
    slice_rows, dpb_rows = [], []
    for p in pics:
        max_fn = 1 << (p.log2_max_frame_num - 4 if mu == "maxfn_wrong" else p.log2_max_frame_num)
        cur = p.frame_num
        max_refs = max(p.max_num_ref_frames, 1)
        is_ref = p.ref_idc != 0 or mu == "nonref_stored"

        def wrap(fn):                                    # 8.2.4.1: FrameNumWrap
            if mu == "no_wrap":
                return fn
            return fn - max_fn if fn > cur else fn

        def sliding_window():                            # 8.2.5.3
            limit = max_refs + 1 if mu == "window_late" else max_refs
            if len(refs) >= limit:
                if mu == "victim_largest":
                    v = max(refs, key=lambda r: wrap(r[1]))
                elif mu == "victim_raw":
                    v = min(refs, key=lambda r: r[1])
                else:
                    v = min(refs, key=lambda r: wrap(r[1]))
                refs.remove(v)

        if p.idr and mu != "idr_keeps":
            refs.clear()                                 # 8.2.5.1: all references unused
        if mu == "mark_before" and is_ref and not p.idr:
            sliding_window()
        if mu == "cur_in_list" and is_ref:
            if not p.idr:
                sliding_window()
            refs.append((p.idx, cur))
        for si, s in enumerate(p.slices):
            if s["type"] != 0:
                continue
            order = sorted(refs, key=lambda r: wrap(r[1]), reverse=(mu != "ascending"))   # 8.2.4.2.1
            lst = [(r[0], wrap(r[1])) for r in order]
            n = s["active"]
            if mu == "trunc_plus1":
                n += 1
            elif mu == "trunc_minus1":
                n = max(n - 1, 0)
            lst = lst[:n]
            while len(lst) < n:
                lst.append(lst[-1] if (mu == "pad_repeat" and lst) else None)
            slice_rows.append(dict(pic=p.idx, slice=si, frame_num=cur, active=s["active"], entries=lst))
        if is_ref:                                       # marking, after the picture is decoded
            if p.idr:
                if mu not in ("idr_not_stored", "cur_in_list"):
                    refs.append((p.idx, cur))
            elif mu not in ("mark_before", "cur_in_list"):
                sliding_window()
                refs.append((p.idx, cur))
            elif mu == "mark_before":
                refs.append((p.idx, cur))
        dpb_rows.append((p.idx, sorted(refs, key=lambda r: -r[0])))
    return slice_rows, dpb_rows
