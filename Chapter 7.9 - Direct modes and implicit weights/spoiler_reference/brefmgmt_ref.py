"""SPOILER: reference model of Chapter 7.6 (full reference picture management, frame pictures only), extended for Chapter 7.8 with
the picture order count of every frame and the B-slice lists (8.2.4.2.3: init_list_b / build_lists_b, the list-1 swap rule).

A complete solution of the chapter: the DPB with short-term and long-term frames, PicNum / LongTermPicNum, the initial P list
(8.2.4.2.1), ref_pic_list_modification (8.2.4.3), the marking processes (8.2.5.1 IDR, 8.2.5.3 sliding window, 8.2.5.4 MMCO 1-6) and
the frame_num gap process (8.2.5.2).  Do not read it before your own implementation passes.

Everything is driven by slice-header values.  `Dpb` is used in two ways: by the harness to compute the expected log of a stream,
and by the synthetic-stream generator (refmgmtgen.py) to choose commands that are valid for the state the decoder will be in.

Slots of a list: (label, 'S', PicNum) | (label, 'L', LongTermPicNum) | None ("no reference picture"); `label` is the decoding-order
index of the picture (an int) or 'g<k>' for the k-th "non-existing" frame inserted by the gap process.

`mutant` switches ONE deliberately wrong rule on (MUTANTS) so that the harness can show that it detects each of them.
"""
import copy

MUTANTS = {
    # --- Chapter 7.8 (B lists)
    "b_l0_no_poc_order": "list 0 of a B slice is sorted by frame_num (PicNum) like a P list instead of by POC",
    "b_l0_future_first": "list 0 puts the pictures with POC greater than the current one BEFORE the earlier ones",
    "b_l0_past_ascending": "list 0 sorts the earlier pictures by ASCENDING POC (oldest first)",
    "b_l1_same_as_l0": "list 1 is built exactly like list 0",
    "b_l1_future_desc": "list 1 sorts the later pictures by DEscending POC",
    "b_l1_past_ascending": "list 1 sorts the earlier pictures by ASCENDING POC",
    "b_no_swap": "the 'list 1 equals list 0: swap the first two entries' rule is missing",
    "b_swap_after_trunc": "the swap rule is tested on the lists AFTER they were cut to num_ref_idx_active",
    "b_swap_always": "list 1 entries 0 and 1 are swapped whenever list 1 has two or more entries",
    "b_lt_first": "long-term frames are put before the short-term ones in B lists",
    "b_l1_mod_uses_l0": "ref_pic_list_modification of list 1 is ignored (list 1 stays as initialised)",

    "mod_no_wrap": "picNumLXNoWrap is not wrapped by MaxPicNum (idc 0 below 0 / idc 1 at or above MaxPicNum)",
    "mod_diff_off1": "abs_diff_pic_num = abs_diff_pic_num_minus1 (the + 1 is forgotten)",
    "mod_pred_fixed": "picNumLXPred is not updated: every idc 0/1 command starts again from CurrPicNum",
    "mod_dup_kept": "the list insertion does not remove the later duplicate of the inserted picture",
    "mod_no_trunc": "the list is not cut back to num_ref_idx_active after the modification (one extra entry stays)",
    "mod_lt_as_pic": "idc 2 looks the picture up by short-term PicNum instead of LongTermPicNum",
    "init_lt_first": "initial P list puts long-term frames before the short-term ones",
    "init_lt_desc": "initial P list sorts long-term frames by DEscending LongTermPicNum",
    "mmco1_off1": "MMCO 1: picNumX = CurrPicNum - difference_of_pic_nums_minus1 (the + 1 is forgotten)",
    "mmco3_no_free": "MMCO 3 does not free the long-term frame that already holds the index",
    "mmco4_no_trunc": "MMCO 4 sets MaxLongTermFrameIdx but does not unmark the long-term frames above it",
    "mmco4_off1": "MMCO 4: MaxLongTermFrameIdx = max_long_term_frame_idx_plus1 (the - 1 is forgotten)",
    "mmco5_keep_fn": "MMCO 5: the picture keeps its own frame_num afterwards (and PrevRefFrameNum is not reset)",
    "mmco5_keep_lt": "MMCO 5 does not reset MaxLongTermFrameIdx",
    "mmco6_no_free": "MMCO 6 does not free the long-term frame that already holds the index",
    "sw_short_only": "sliding window counts only short-term frames (numShortTerm == Max(max_num_ref_frames, 1))",
    "sw_with_mmco": "the sliding window also runs when adaptive_ref_pic_marking_mode_flag = 1",
    "gap_ignored": "frame_num gaps are not handled (no non-existing frames inserted)",
    "gap_no_slide": "non-existing frames are inserted without running the sliding window",
    "idr_lt_ignored": "long_term_reference_flag of an IDR picture is ignored",
    "idr_lt_max": "IDR with long_term_reference_flag = 1 leaves MaxLongTermFrameIdx at 'none'",
}


class StreamError(Exception):
    """The stream violates a rule of the standard (non-conforming) or uses something this model does not cover."""


class Frame:
    def __init__(self, label, frame_num, seq, nonexist=False):
        self.label, self.frame_num, self.seq, self.nonexist = label, frame_num, seq, nonexist
        self.lt = None                       # LongTermFrameIdx when marked long-term, else None
        self.poc = 0                         # PicOrderCnt of the frame (Chapter 7.8)


class Dpb:
    def __init__(self, max_num_ref_frames, log2_max_frame_num, gaps_allowed=False, mutant=None):
        self.max_refs = max(max_num_ref_frames, 1)
        self.max_fn = 1 << log2_max_frame_num
        self.gaps_allowed = gaps_allowed
        self.mu = mutant
        self.lenient = mutant is not None    # a wrong rule may break the stream's own consistency; keep going so the harness can compare
        self.frames = []                     # reference frames (short- and long-term), in no particular order
        self.max_lt = None                   # MaxLongTermFrameIdx; None = "no long-term frame indices"
        self.prev_ref_fn = 0                 # PrevRefFrameNum
        self.seq = 0
        self.ngap = 0
        self.gap_states = []                 # state() after each non-existing frame inserted by the last check_frame_num

    def clone(self):
        return copy.deepcopy(self)

    # ---------------------------------------------------------------- numbers (8.2.4.1)
    def short(self):
        return [f for f in self.frames if f.lt is None]

    def long(self):
        return [f for f in self.frames if f.lt is not None]

    def wrap(self, f, cur_fn):
        return f.frame_num - self.max_fn if f.frame_num > cur_fn else f.frame_num      # FrameNumWrap (= PicNum for frames)

    def newest_first(self):
        return sorted(self.short(), key=lambda f: -f.seq)

    # ---------------------------------------------------------------- lists (8.2.4.2.1, 8.2.4.3)
    def init_list(self, cur_fn):
        sh = sorted(self.short(), key=lambda f: -self.wrap(f, cur_fn))
        lg = sorted(self.long(), key=lambda f: f.lt, reverse=(self.mu == "init_lt_desc"))
        order = lg + sh if self.mu == "init_lt_first" else sh + lg
        return [(f.label, 'S', self.wrap(f, cur_fn)) if f.lt is None else (f.label, 'L', f.lt) for f in order]

    def init_lists_b(self, cur_poc):
        """8.2.4.2.3: the initial RefPicList0 / RefPicList1 of a B slice (frames), BEFORE truncation to num_ref_idx_active.
        Non-existing frames (gap process) are not used by B slices when pic_order_cnt_type is 0 (taken as always true here)."""
        mu = self.mu
        sh = [f for f in self.short() if not f.nonexist]
        lg = sorted(self.long(), key=lambda f: f.lt)
        before = [f for f in sh if f.poc < cur_poc]
        after = [f for f in sh if f.poc > cur_poc]
        d_before = sorted(before, key=lambda f: f.poc, reverse=True)       # closest earlier picture first
        a_before = sorted(before, key=lambda f: f.poc)
        a_after = sorted(after, key=lambda f: f.poc)                        # closest later picture first
        d_after = sorted(after, key=lambda f: f.poc, reverse=True)
        l0 = (a_before if mu == "b_l0_past_ascending" else d_before) + a_after
        if mu == "b_l0_future_first":
            l0 = a_after + d_before
        l1 = a_after + (a_before if mu == "b_l1_past_ascending" else d_before)
        if mu == "b_l1_future_desc":
            l1 = d_after + d_before
        if mu == "b_l1_same_as_l0":
            l1 = list(l0)
        if mu == "b_l0_no_poc_order":
            l0 = sorted(sh, key=lambda f: -f.frame_num)
        l0 = l0 + lg if mu != "b_lt_first" else lg + l0
        l1 = l1 + lg if mu != "b_lt_first" else lg + l1
        return l0, l1

    def _slot(self, f, cur_fn):
        return (f.label, 'S', self.wrap(f, cur_fn)) if f.lt is None else (f.label, 'L', f.lt)

    def build_lists_b(self, cur_fn, cur_poc, active0, active1, mods0=(), mods1=()):
        """-> (RefPicList0, RefPicList1) of a B slice: initial lists, swap rule, truncation / padding, then the modifications."""
        mu = self.mu
        i0, i1 = self.init_lists_b(cur_poc)
        sl0 = [self._slot(f, cur_fn) for f in i0]
        sl1 = [self._slot(f, cur_fn) for f in i1]
        if mu != "b_swap_after_trunc":
            if mu == "b_swap_always":
                swap = len(sl1) > 1
            else:
                swap = len(sl1) > 1 and sl1 == sl0
            if swap and mu != "b_no_swap":
                sl1[0], sl1[1] = sl1[1], sl1[0]
        sl0c, sl1c = sl0[:active0], sl1[:active1]
        if mu == "b_swap_after_trunc" and len(sl1c) > 1 and sl1c == sl0c:
            sl1c[0], sl1c[1] = sl1c[1], sl1c[0]
        l0 = self.build_list(cur_fn, active0, mods0, init=sl0c)
        l1 = self.build_list(cur_fn, active1, [] if mu == "b_l1_mod_uses_l0" else mods1, init=sl1c)
        return l0, l1

    def build_list(self, cur_fn, active, mods=(), init=None):
        """RefPicList0 of a P slice: initial list, cut/padded to `active`, then the modification commands
        mods = [(modification_of_pic_nums_idc, value)], value = abs_diff_pic_num_minus1 (idc 0, 1) or long_term_pic_num (idc 2);
        idc 3 may be included or left out."""
        lst = (self.init_list(cur_fn) if init is None else list(init))[:active]
        lst += [None] * (active - len(lst))
        if not mods:
            return lst
        mu = self.mu
        lst = lst + [None]                                   # temporarily active + 1 entries
        pred, ref_idx, curr = cur_fn, 0, cur_fn
        maxp = self.max_fn
        for idc, val in mods:
            if idc == 3:
                break
            if ref_idx >= active:
                raise StreamError("more ref_pic_list_modification commands than num_ref_idx_active")
            if idc in (0, 1):
                d = val if mu == "mod_diff_off1" else val + 1
                base = curr if mu == "mod_pred_fixed" else pred
                if idc == 0:
                    nw = base - d
                    if nw < 0 and mu != "mod_no_wrap":
                        nw += maxp
                else:
                    nw = base + d
                    if nw >= maxp and mu != "mod_no_wrap":
                        nw -= maxp
                pred = nw
                num = nw - maxp if nw > curr else nw
                kind = 'S'
            elif idc == 2:
                num, kind = val, ('S' if mu == "mod_lt_as_pic" else 'L')
            else:
                raise StreamError(f"modification_of_pic_nums_idc {idc}")
            tgt = next((s for s in lst if s is not None and s[1] == kind and s[2] == num), None)
            if tgt is None:
                # the target is not in the (cut) initial list: take it from the DPB
                fr = next((f for f in self.frames if (f.lt is None and kind == 'S' and self.wrap(f, cur_fn) == num)
                           or (f.lt is not None and kind == 'L' and f.lt == num)), None)
                if fr is None and self.lenient:
                    fr = Frame("?", 0, 0)
                if fr is None:
                    raise StreamError(f"modification refers to {'PicNum' if kind == 'S' else 'LongTermPicNum'} {num}, "
                                      f"which is not a reference frame")
                tgt = (fr.label, kind, num)
            # insertion: shift up, put at ref_idx, then remove the later duplicate
            for c in range(active, ref_idx, -1):
                lst[c] = lst[c - 1]
            lst[ref_idx] = tgt
            ref_idx += 1
            n = ref_idx
            for c in range(ref_idx, active + 1):
                dup = lst[c] is not None and lst[c][0] == tgt[0] and lst[c][1] == tgt[1]
                if mu == "mod_dup_kept":
                    dup = False
                if not dup:
                    lst[n] = lst[c]
                    n += 1
        return lst[:active + 1] if mu == "mod_no_trunc" else lst[:active]

    # ---------------------------------------------------------------- frame_num gaps (8.2.5.2)
    def check_frame_num(self, frame_num, idr):
        """Called at the start of every picture (before its lists).  Returns the labels of the non-existing frames inserted."""
        if idr or frame_num == self.prev_ref_fn or frame_num == (self.prev_ref_fn + 1) % self.max_fn:
            return []
        if not self.gaps_allowed:
            raise StreamError(f"frame_num {frame_num} after PrevRefFrameNum {self.prev_ref_fn}: a gap, but "
                              f"gaps_in_frame_num_value_allowed_flag is 0 (a loss: error concealment, Phase 9.5)")
        if self.mu == "gap_ignored":
            self.prev_ref_fn = (frame_num - 1) % self.max_fn
            return []
        out = []
        self.gap_states = []
        unused = (self.prev_ref_fn + 1) % self.max_fn
        while unused != frame_num:
            f = Frame(f"g{self.ngap}", unused, self.seq, nonexist=True)
            self.ngap += 1
            self.seq += 1
            if self.mu != "gap_no_slide":
                self._sliding_window(unused)
            self.frames.append(f)
            out.append(f.label)
            self.gap_states.append(self.post_state())
            self.prev_ref_fn = unused
            unused = (unused + 1) % self.max_fn
        return out

    # ---------------------------------------------------------------- marking (8.2.5)
    def _sliding_window(self, cur_fn):
        n_sh, n_lg = len(self.short()), len(self.long())
        full = n_sh == self.max_refs if self.mu == "sw_short_only" else n_sh + n_lg == self.max_refs
        if full:
            if n_sh == 0 and self.lenient:
                return
            if n_sh == 0:
                raise StreamError("sliding window with no short-term frame to remove (non-conforming)")
            v = min(self.short(), key=lambda f: self.wrap(f, cur_fn))
            self.frames.remove(v)

    def mmco_step(self, op, a, cur_fn, cur=None):
        """One memory_management_control_operation.  a = dict of its arguments.  cur = the current Frame (for MMCO 6)."""
        mu = self.mu
        if op == 1:
            px = cur_fn - (a["difference_of_pic_nums_minus1"] + (0 if mu == "mmco1_off1" else 1))
            v = next((f for f in self.short() if self.wrap(f, cur_fn) == px), None)
            if v is None and self.lenient:
                return
            if v is None:
                raise StreamError(f"MMCO 1: no short-term frame with PicNum {px}")
            self.frames.remove(v)
        elif op == 2:
            v = next((f for f in self.long() if f.lt == a["long_term_pic_num"]), None)
            if v is None and self.lenient:
                return
            if v is None:
                raise StreamError(f"MMCO 2: no long-term frame with LongTermPicNum {a['long_term_pic_num']}")
            self.frames.remove(v)
        elif op == 3:
            px = cur_fn - (a["difference_of_pic_nums_minus1"] + 1)
            v = next((f for f in self.short() if self.wrap(f, cur_fn) == px), None)
            if v is None and self.lenient:
                return
            if v is None:
                raise StreamError(f"MMCO 3: no short-term frame with PicNum {px}")
            idx = a["long_term_frame_idx"]
            self._need_idx(idx, 3)
            if mu != "mmco3_no_free":
                for f in self.long():
                    if f.lt == idx:
                        self.frames.remove(f)
            v.lt = idx
        elif op == 4:
            p1 = a["max_long_term_frame_idx_plus1"]
            if mu != "mmco4_no_trunc":
                lim = p1 if mu == "mmco4_off1" else p1 - 1
                for f in self.long():
                    if f.lt > lim:
                        self.frames.remove(f)
            self.max_lt = None if p1 == 0 else (p1 if mu == "mmco4_off1" else p1 - 1)
        elif op == 5:
            self.frames.clear()
            if mu != "mmco5_keep_lt":
                self.max_lt = None
        elif op == 6:
            idx = a["long_term_frame_idx"]
            self._need_idx(idx, 6)
            if mu != "mmco6_no_free":
                for f in self.long():
                    if f.lt == idx:
                        self.frames.remove(f)
            cur.lt = idx
        else:
            raise StreamError(f"memory_management_control_operation {op}")

    def _need_idx(self, idx, op):
        if (self.max_lt is None or idx > self.max_lt) and not self.lenient:
            raise StreamError(f"MMCO {op}: long_term_frame_idx {idx} > MaxLongTermFrameIdx "
                              f"({'none' if self.max_lt is None else self.max_lt})")

    def finish_picture(self, label, frame_num, idr, ref_idc, long_term_reference_flag=0, mmcos=None, poc=0):
        """Marking process of a decoded picture (called once per picture, after all its slices).
        mmcos: None (adaptive_ref_pic_marking_mode_flag = 0) or [(op, args)] (without the terminating 0).
        Returns the number of MMCO-5 pictures handled (0/1) for the caller's information."""
        if ref_idc == 0:
            return 0
        mu = self.mu
        cur = Frame(label, frame_num, self.seq)
        cur.poc = poc
        self.seq += 1
        mmco5 = False
        if idr:
            self.frames.clear()
            if long_term_reference_flag and mu != "idr_lt_ignored":
                cur.lt = 0
                self.max_lt = None if mu == "idr_lt_max" else 0
            else:
                self.max_lt = None
        elif mmcos is None:
            self._sliding_window(frame_num)
        else:
            if mu == "sw_with_mmco":
                self._sliding_window(frame_num)
            for op, a in mmcos:
                self.mmco_step(op, a, frame_num, cur)
                mmco5 |= op == 5
        if mmco5:
            if mu != "mmco5_keep_fn":
                cur.frame_num = 0                 # 7.4.3: the picture is treated as having had frame_num 0
            cur.poc = 0                           # 8.2.1: tempPicOrderCnt is subtracted, the picture's POC becomes 0
        if self.lenient:
            self.frames.append(cur)
            self.prev_ref_fn = 0 if (mmco5 and mu != "mmco5_keep_fn") else frame_num
            return int(mmco5)
        if cur.lt is not None and (self.max_lt is None or cur.lt > self.max_lt):
            raise StreamError(f"picture {label}: its LongTermFrameIdx {cur.lt} exceeds MaxLongTermFrameIdx after the MMCO operations")
        if cur.lt is not None and any(f.lt == cur.lt for f in self.frames):
            raise StreamError(f"picture {label}: LongTermFrameIdx {cur.lt} is held by two frames (the current picture was given an "
                              f"index that MMCO 3 then assigned to another frame)")
        self.frames.append(cur)
        if len(self.frames) > self.max_refs:
            raise StreamError(f"{len(self.frames)} reference frames after picture {label}: more than "
                              f"Max(max_num_ref_frames, 1) = {self.max_refs} (non-conforming stream)")
        self.prev_ref_fn = 0 if (mmco5 and mu != "mmco5_keep_fn") else frame_num
        return int(mmco5)

    def state(self):
        """(short list newest first [(label, frame_num)], long list by LongTermFrameIdx [(label, idx)], MaxLongTermFrameIdx)"""
        return ([(f.label, f.frame_num) for f in self.newest_first()],
                [(f.label, f.lt) for f in sorted(self.long(), key=lambda f: f.lt)], self.max_lt)


    def post_state_poc(self):
        """Like post_state() with the POC: ([(frame_num, poc)] short-term newest first, [(LongTermFrameIdx, frame_num, poc)])."""
        return ([(f.frame_num, f.poc) for f in self.newest_first()],
                [(f.lt, f.frame_num, f.poc) for f in sorted(self.long(), key=lambda f: f.lt)])

    def post_state(self):
        """What ffmpeg's `-debug mmco` prints after a marking process: ([frame_num of the short-term frames, newest first],
        [(LongTermFrameIdx, frame_num)] ascending)."""
        return ([f.frame_num for f in self.newest_first()], [(f.lt, f.frame_num) for f in sorted(self.long(), key=lambda f: f.lt)])


class Pic:
    """idx, idr, ref_idc, frame_num, long_term_reference_flag, mmcos (None | [(op, args)]), slices: [dict(type 0 P/1 B/2 I,
    active, mods)]; the stream-wide values are passed to simulate()."""


def simulate(pics, max_num_ref_frames, log2_max_frame_num, gaps_allowed, mutant=None):
    """-> (slice_rows, dpb_rows, gap_rows, posts).  posts: state() after every marking process in decoding order (each
    non-existing frame inserted by the gap process counts as one, then each reference picture): what ffmpeg's -debug mmco prints.  slice_rows: dict(pic, slice, frame_num, active, entries); dpb_rows: (pic, short, long,
    max_lt) after the marking of every picture; gap_rows: (pic, [labels of non-existing frames inserted before it])."""
    d = Dpb(max_num_ref_frames, log2_max_frame_num, gaps_allowed, mutant)
    slice_rows, dpb_rows, gap_rows, posts = [], [], [], []
    for p in pics:
        gl = d.check_frame_num(p.frame_num, p.idr)
        if gl:
            gap_rows.append((p.idx, gl))
            posts += d.gap_states
        for si, s in enumerate(p.slices):
            if s["type"] == 0:
                slice_rows.append(dict(pic=p.idx, slice=si, frame_num=p.frame_num, active=s["active"],
                                       entries=d.build_list(p.frame_num, s["active"], s["mods"])))
        d.finish_picture(p.idx, p.frame_num, p.idr, p.ref_idc, p.long_term_reference_flag, p.mmcos)
        sh, lg, mx = d.state()
        dpb_rows.append((p.idx, sh, lg, mx))
        if p.ref_idc:
            posts.append(d.post_state())
    return slice_rows, dpb_rows, gap_rows, posts
