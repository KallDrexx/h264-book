"""SPOILER: Chapter 10.2 reference model of the decoded picture buffer for FRAME and FIELD pictures: reference list initialisation
(8.2.4.2.1 P frames, 8.2.4.2.2 P fields, 8.2.4.2.3 B frames, 8.2.4.2.4 B fields, 8.2.4.2.5 alternating parity), list modification
(8.2.4.3, picNum of fields), and the marking processes (8.2.5.1 IDR, 8.2.5.3 sliding window, 8.2.5.4 MMCO 1-6) with a marking state
PER FIELD.  Do not read before your own implementation passes.

A frame store (FS) is a frame, a complementary field pair or a non-paired field.  Per parity 'T' / 'B' it knows whether the field
is decoded (has), its POC (poc) and its marking (mark: None = unused for reference, 'S' short-term, 'L' long-term).  FrameNum and
LongTermFrameIdx belong to the store (both fields of a frame share them).

List slots: (store id, parity, 'S'|'L', PicNum | LongTermPicNum) with parity 'T' / 'B' for fields and 'F' for frames; None = "no
reference picture".  The dump token of a slot is  <store id><parity>, e.g. 3T, 0B, 2F.

`mutant` switches ONE deliberately wrong rule on (MUTANTS) so that the harness can show that it detects each of them.
"""
import copy

MUTANTS = {
    "p_alt_start_opposite": "P / B field lists: the alternation of 8.2.4.2.5 starts with the OPPOSITE parity",
    "p_no_current_first_field": "second field of a frame: the first field of the same frame is not a reference candidate",
    "p_no_fnw_wrap": "FrameNumWrap is frame_num (no subtraction of MaxFrameNum for frame_num above the current one)",
    "p_need_both_fields": "field lists: a frame is a candidate only when BOTH its fields are marked (a single marked field is lost)",
    "p_missing_not_skipped": "alternation: a field that is not marked is not skipped, it takes the place in the list as 'no reference picture'",
    "p_lt_desc": "long-term fields of the lists are ordered by DEscending LongTermFrameIdx",
    "p_no_alt_append": "alternation: when one parity runs out the remaining fields of the other parity are dropped",
    "b_poc_max": "B lists: the POC of a frame store with two decoded fields is the larger of the two field POCs",
    "b_no_swap": "B lists: the 'list 1 equals list 0, swap the first two entries' rule is missing",
    "b_strict_lt": "B field lists: entries with POC equal to the current field's POC go to the 'later' part",
    "b_cur_poc_frame": "B field lists: the current field is ordered against the POC of the frame (min of both field POCs of its frame)",
    "b_l1_no_alt_start": "B field list 1 is alternated starting with the opposite parity",
    "mod_parity_swapped": "picNum of a field: 2 * FrameNumWrap + 1 for the OPPOSITE parity, 2 * FrameNumWrap for the same parity",
    "mod_currpicnum_frame": "CurrPicNum = frame_num (the field rule 2 * frame_num + 1 is missing)",
    "mod_maxpicnum_frame": "MaxPicNum = MaxFrameNum (the field rule 2 * MaxFrameNum is missing)",
    "mod_lt_parity_swapped": "LongTermPicNum of a field: 2 * idx for the same parity, 2 * idx + 1 for the opposite parity",
    "mod_lt_as_frame": "LongTermPicNum of a field is LongTermFrameIdx (the frame rule)",
    "sw_count_fields": "sliding window counts FIELDS marked as reference instead of frames / pairs / unpaired fields",
    "sw_second_field_slides": "the second field of a reference frame runs the sliding window again",
    "sw_short_only": "sliding window counts only short-term frames",
    "mmco1_both_fields": "MMCO 1 on a field unmarks both fields of its frame",
    "mmco3_frees_same_frame": "MMCO 3: the long-term index held by the OTHER FIELD OF THE SAME FRAME frees that field",
    "mmco6_frees_first_field": "MMCO 6 on a second field frees the first field of the same frame when it holds the same index",
    "mmco2_both_fields": "MMCO 2 on a field unmarks both fields of its frame",
    "mmco4_no_trunc": "MMCO 4 does not unmark the long-term fields above the new maximum",
}


class StreamError(Exception):
    """The stream violates a rule of the standard (non-conforming) or uses something this model does not cover."""


class FS:
    def __init__(self, sid, frame_num, coded):
        self.id, self.frame_num, self.coded = sid, frame_num, coded      # coded: 'F' frame picture, 'P' field pictures
        self.has = {"T": False, "B": False}
        self.poc = {"T": None, "B": None}
        self.mark = {"T": None, "B": None}
        self.lt = None

    def fpoc(self, mu=None):
        v = [self.poc[p] for p in "TB" if self.has[p]]
        return max(v) if mu == "b_poc_max" else min(v)

    def marked(self, kind):
        return [p for p in "TB" if self.mark[p] == kind]


def opposite(p):
    return "B" if p == "T" else "T"


class Dpb:
    def __init__(self, max_num_ref_frames, log2_max_frame_num, mutant=None):
        self.max_refs = max(max_num_ref_frames, 1)
        self.max_fn = 1 << log2_max_frame_num
        self.mu = mutant
        self.lenient = mutant is not None
        self.stores = []                      # frame stores with at least one field marked as reference (plus the pending first field)
        self.max_lt = None                    # MaxLongTermFrameIdx; None = "no long-term frame indices"
        self.prev_ref_fn = 0

    def clone(self):
        return copy.deepcopy(self)

    # ---------------------------------------------------------------- numbers (8.2.4.1)
    def fnw(self, s, cur_fn):
        if self.mu == "p_no_fnw_wrap":
            return s.frame_num
        return s.frame_num - self.max_fn if s.frame_num > cur_fn else s.frame_num

    def curr_pic_num(self, cur):
        fn = cur["frame_num"]
        return fn if (cur["struct"] == "F" or self.mu == "mod_currpicnum_frame") else 2 * fn + 1

    def max_pic_num(self, cur):
        return self.max_fn if (cur["struct"] == "F" or self.mu == "mod_maxpicnum_frame") else 2 * self.max_fn

    def pic_num(self, s, par, cur):
        w = self.fnw(s, cur["frame_num"])
        if cur["struct"] == "F":
            return w
        same = par == cur["struct"]
        if self.mu == "mod_parity_swapped":
            same = not same
        return 2 * w + 1 if same else 2 * w

    def lt_pic_num(self, s, par, cur):
        if cur["struct"] == "F":
            return s.lt
        if self.mu == "mod_lt_as_frame":
            return s.lt
        same = par == cur["struct"]
        if self.mu == "mod_lt_parity_swapped":
            same = not same
        return 2 * s.lt + 1 if same else 2 * s.lt

    def slot(self, s, par, kind, cur):
        num = self.pic_num(s, par, cur) if kind == "S" else self.lt_pic_num(s, par, cur)
        return (s.id, par, kind, num)

    # ---------------------------------------------------------------- candidates
    def candidates(self, cur):
        """All reference pictures a command of this picture can address: [(store, parity, kind)]; parity 'F' for frames."""
        out = []
        for s in self.stores:
            if cur["struct"] == "F":
                for kind in ("S", "L"):
                    if s.has["T"] and s.has["B"] and s.mark["T"] == kind and s.mark["B"] == kind:
                        out.append((s, "F", kind))
            else:
                for p in "TB":
                    if s.mark[p]:
                        out.append((s, p, s.mark[p]))
        return out

    # ---------------------------------------------------------------- 8.2.4.2.5
    def _alternate(self, frames, cur_par, kind, first_opposite=False):
        """frames: ordered list of stores; -> ordered list of (store, parity) fields of marking `kind`."""
        mu = self.mu
        start = opposite(cur_par) if (mu == "p_alt_start_opposite" or first_opposite) else cur_par
        a, b = opposite(start), start
        if mu == "p_missing_not_skipped":
            fa = [(s, start) for s in frames]
            fb = [(s, a) for s in frames]
        else:
            fa = [(s, start) for s in frames if s.mark[start] == kind]
            fb = [(s, a) for s in frames if s.mark[a] == kind]
        out, i, j = [], 0, 0
        while i < len(fa) or j < len(fb):
            if i < len(fa):
                out.append(fa[i])
                i += 1
            if j < len(fb):
                out.append(fb[j])
                j += 1
            if (i >= len(fa)) != (j >= len(fb)) and mu == "p_no_alt_append":
                break
        return out

    def _field_candidates(self, cur, kind):
        out = []
        for s in self.stores:
            if self.mu == "p_no_current_first_field" and s.id == cur.get("store"):
                continue
            if self.mu == "p_need_both_fields":
                if not (s.mark["T"] == kind and s.mark["B"] == kind):
                    continue
            elif not s.marked(kind):
                continue
            out.append(s)
        return out

    # ---------------------------------------------------------------- initial lists
    def init_p(self, cur):
        """-> initial RefPicList0 of a P picture: [(store, parity, kind)]"""
        if cur["struct"] == "F":
            sh = [s for s in self.stores if s.has["T"] and s.has["B"] and s.mark["T"] == "S" and s.mark["B"] == "S"]
            lg = [s for s in self.stores if s.has["T"] and s.has["B"] and s.mark["T"] == "L" and s.mark["B"] == "L"]
            sh.sort(key=lambda s: -self.fnw(s, cur["frame_num"]))
            lg.sort(key=lambda s: s.lt, reverse=(self.mu == "p_lt_desc"))
            return [(s, "F", "S") for s in sh] + [(s, "F", "L") for s in lg]
        sh = self._field_candidates(cur, "S")
        lg = self._field_candidates(cur, "L")
        sh.sort(key=lambda s: -self.fnw(s, cur["frame_num"]))
        lg.sort(key=lambda s: s.lt, reverse=(self.mu == "p_lt_desc"))
        return ([(s, p, "S") for s, p in self._alternate(sh, cur["struct"], "S") if s is not None] +
                [(s, p, "L") for s, p in self._alternate(lg, cur["struct"], "L") if s is not None])

    def init_b(self, cur):
        """-> (initial RefPicList0, RefPicList1) of a B picture, with the swap rule, before truncation."""
        mu = self.mu
        if cur["struct"] == "F":
            sh = [s for s in self.stores if s.has["T"] and s.has["B"] and s.mark["T"] == "S" and s.mark["B"] == "S"]
            lg = [s for s in self.stores if s.has["T"] and s.has["B"] and s.mark["T"] == "L" and s.mark["B"] == "L"]
            cp = cur["poc"]
            before = sorted([s for s in sh if s.fpoc(mu) < cp], key=lambda s: -s.fpoc(mu))
            after = sorted([s for s in sh if s.fpoc(mu) > cp], key=lambda s: s.fpoc(mu))
            lg.sort(key=lambda s: s.lt, reverse=(mu == "p_lt_desc"))
            l0 = [(s, "F", "S") for s in before + after] + [(s, "F", "L") for s in lg]
            l1 = [(s, "F", "S") for s in after + before] + [(s, "F", "L") for s in lg]
        else:
            sh = self._field_candidates(cur, "S")
            lg = self._field_candidates(cur, "L")
            cp = cur["poc"]
            if mu == "b_cur_poc_frame":
                cp = cur["frame_poc"]
            if mu == "b_strict_lt":
                before = sorted([s for s in sh if s.fpoc(mu) < cp], key=lambda s: -s.fpoc(mu))
                after = sorted([s for s in sh if s.fpoc(mu) >= cp], key=lambda s: s.fpoc(mu))
            else:
                before = sorted([s for s in sh if s.fpoc(mu) <= cp], key=lambda s: -s.fpoc(mu))
                after = sorted([s for s in sh if s.fpoc(mu) > cp], key=lambda s: s.fpoc(mu))
            lg.sort(key=lambda s: s.lt, reverse=(mu == "p_lt_desc"))
            par = cur["struct"]
            lt = [(s, p, "L") for s, p in self._alternate(lg, par, "L") if s is not None]
            l0 = [(s, p, "S") for s, p in self._alternate(before + after, par, "S") if s is not None] + lt
            l1 = [(s, p, "S") for s, p in self._alternate(after + before, par, "S", mu == "b_l1_no_alt_start") if s is not None] + lt
        if mu != "b_no_swap" and len(l1) > 1 and [(x[0].id, x[1]) for x in l0] == [(x[0].id, x[1]) for x in l1]:
            l1[0], l1[1] = l1[1], l1[0]
        return l0, l1

    # ---------------------------------------------------------------- modification (8.2.4.3)
    def build_list(self, cur, active, init, mods=()):
        """init: initial list [(store, parity, kind)]; mods: [(modification_of_pic_nums_idc, value)] with value =
        abs_diff_pic_num_minus1 (idc 0, 1) or long_term_pic_num (idc 2).  -> list of `active` slots (None = no reference picture)."""
        lst = [self.slot(s, p, k, cur) for s, p, k in init][:active]
        lst += [None] * (active - len(lst))
        if not mods:
            return lst
        lst = lst + [None]
        curr, maxp = self.curr_pic_num(cur), self.max_pic_num(cur)
        pred, ref_idx = curr, 0
        cands = self.candidates(cur)
        for idc, val in mods:
            if idc == 3:
                break
            if ref_idx >= active:
                raise StreamError("more modification commands than num_ref_idx_active")
            if idc in (0, 1):
                d = val + 1
                if idc == 0:
                    nw = pred - d
                    nw = nw + maxp if nw < 0 else nw
                else:
                    nw = pred + d
                    nw = nw - maxp if nw >= maxp else nw
                pred = nw
                num = nw - maxp if nw > curr else nw
                kind = "S"
            elif idc == 2:
                num, kind = val, "L"
            else:
                raise StreamError(f"modification_of_pic_nums_idc {idc}")
            tgt = None
            for s, p, k in cands:
                if k == kind:
                    n = self.pic_num(s, p, cur) if kind == "S" else self.lt_pic_num(s, p, cur)
                    if n == num:
                        tgt = (s.id, p, kind, num)
                        break
            if tgt is None:
                if self.lenient:
                    tgt = (-1, "F", kind, num)
                else:
                    raise StreamError(f"modification refers to {'PicNum' if kind == 'S' else 'LongTermPicNum'} {num}: no such reference")
            for c in range(active, ref_idx, -1):
                lst[c] = lst[c - 1]
            lst[ref_idx] = tgt
            ref_idx += 1
            n = ref_idx
            for c in range(ref_idx, active + 1):
                e = lst[c]
                if not (e is not None and e[0] == tgt[0] and e[1] == tgt[1] and e[2] == tgt[2]):
                    lst[n] = e
                    n += 1
        return lst[:active]

    def lists(self, cur, is_b, active0, active1, mods0=(), mods1=()):
        """-> (RefPicList0, RefPicList1) slots (list 1 is [] for P)."""
        if not is_b:
            return self.build_list(cur, active0, self.init_p(cur), mods0), []
        i0, i1 = self.init_b(cur)
        return self.build_list(cur, active0, i0, mods0), self.build_list(cur, active1, i1, mods1)

    def slot_pictures(self, slot):
        """-> (store, parity) of a slot (None for none)."""
        if slot is None:
            return None
        s = next((x for x in self.stores if x.id == slot[0]), None)
        return s, slot[1]

    # ---------------------------------------------------------------- marking (8.2.5)
    def _count(self, kind=None):
        n = 0
        for s in self.stores:
            if kind == "S":
                n += bool(s.marked("S"))
            elif kind == "L":
                n += bool(s.marked("L"))
        return n

    def _sliding_window(self, cur_fn):
        n_sh, n_lg = self._count("S"), self._count("L")
        if self.mu == "sw_count_fields":
            full = sum(len(s.marked("S")) + len(s.marked("L")) for s in self.stores) >= self.max_refs
        elif self.mu == "sw_short_only":
            full = n_sh == self.max_refs
        else:
            full = n_sh + n_lg == self.max_refs
        if full:
            shorts = [s for s in self.stores if s.marked("S")]
            if not shorts:
                if self.lenient:
                    return
                raise StreamError("sliding window with no short-term frame to remove (non-conforming)")
            v = min(shorts, key=lambda s: self.fnw(s, cur_fn))
            for p in "TB":
                if v.mark[p] == "S":
                    v.mark[p] = None

    def _free_idx(self, idx, keep_store=None, only_fields=False):
        """The LongTermFrameIdx idx becomes free: stores (other than keep_store) holding it lose their long-term fields."""
        for s in self.stores:
            if s.lt == idx and s.marked("L") and s is not keep_store:
                for p in "TB":
                    if s.mark[p] == "L":
                        s.mark[p] = None

    def _find(self, cur, kind, num):
        for s, p, k in self.candidates(cur):
            if k == kind:
                n = self.pic_num(s, p, cur) if kind == "S" else self.lt_pic_num(s, p, cur)
                if n == num:
                    return s, p
        return None

    def mmco_step(self, op, a, cur, curfs):
        mu = self.mu
        if op in (1, 3):
            px = self.curr_pic_num(cur) - (a["difference_of_pic_nums_minus1"] + 1)
            t = self._find(cur, "S", px)
            if t is None:
                if self.lenient:
                    return
                raise StreamError(f"MMCO {op}: no short-term picture with PicNum {px}")
            s, p = t
            if op == 1:
                for q in ("TB" if (p == "F" or mu == "mmco1_both_fields") else p):
                    if s.mark[q] == "S":
                        s.mark[q] = None
            else:
                idx = a["long_term_frame_idx"]
                self._need_idx(idx, 3)
                keep = None if mu == "mmco3_frees_same_frame" else s
                self._free_idx(idx, keep)
                if p != "F" and s.lt is not None and s.lt != idx and s.marked("L") and not self.lenient:
                    raise StreamError("MMCO 3: the other field of the frame already has a different LongTermFrameIdx")
                for q in ("TB" if p == "F" else p):
                    s.mark[q] = "L"
                s.lt = idx
        elif op == 2:
            t = self._find(cur, "L", a["long_term_pic_num"])
            if t is None:
                if self.lenient:
                    return
                raise StreamError(f"MMCO 2: no long-term picture with LongTermPicNum {a['long_term_pic_num']}")
            s, p = t
            for q in ("TB" if (p == "F" or mu == "mmco2_both_fields") else p):
                if s.mark[q] == "L":
                    s.mark[q] = None
        elif op == 4:
            p1 = a["max_long_term_frame_idx_plus1"]
            if mu != "mmco4_no_trunc":
                for s in self.stores:
                    if s.lt is not None and s.lt > p1 - 1:
                        for q in "TB":
                            if s.mark[q] == "L":
                                s.mark[q] = None
            self.max_lt = None if p1 == 0 else p1 - 1
        elif op == 5:
            for s in self.stores:
                for q in "TB":
                    s.mark[q] = None
            for q in "TB":
                curfs.mark[q] = None
            self.max_lt = None
        elif op == 6:
            idx = a["long_term_frame_idx"]
            self._need_idx(idx, 6)
            keep = None if mu == "mmco6_frees_first_field" else curfs
            self._free_idx(idx, keep)
            curfs.lt = idx
            cur["_lt_cur"] = True
        else:
            raise StreamError(f"memory_management_control_operation {op}")

    def _need_idx(self, idx, op):
        if (self.max_lt is None or idx > self.max_lt) and not self.lenient:
            raise StreamError(f"MMCO {op}: long_term_frame_idx {idx} > MaxLongTermFrameIdx ({self.max_lt})")

    def finish_picture(self, cur, lt_flag=0, mmcos=None):
        """Marking process of a decoded picture.  cur: dict(struct 'F'|'T'|'B', frame_num, idr, ref (bool), second (bool),
        store (id), poc_t, poc_b (None for the absent parity), coded 'F'|'P').  mmcos: None | [(op, args)] (without the 0).
        Returns the (possibly rebased) dict of POCs when MMCO 5 occurred, else None."""
        if not cur["ref"]:
            return None
        mu = self.mu
        structs = "TB" if cur["struct"] == "F" else cur["struct"]
        curfs = next((s for s in self.stores if s.id == cur["store"]), None) if cur["second"] else None
        if curfs is None:
            curfs = FS(cur["store"], cur["frame_num"], cur["coded"])
            new = True
        else:
            new = False
        first_marked_short = (not new) and curfs.marked("S") and len(structs) == 1
        cur = dict(cur)
        mmco5 = False
        if cur["idr"]:
            for s in self.stores:
                for q in "TB":
                    s.mark[q] = None
            self.stores = []
            if lt_flag:
                curfs.lt = 0
                cur["_lt_cur"] = True
                self.max_lt = 0
            else:
                self.max_lt = None
        elif mmcos is None:
            if not (first_marked_short and mu != "sw_second_field_slides"):
                self._sliding_window(cur["frame_num"])
        else:
            for op, a in mmcos:
                self.mmco_step(op, a, cur, curfs)
                mmco5 |= op == 5
        for q in structs:
            curfs.has[q] = True
            curfs.poc[q] = cur["poc_t"] if q == "T" else cur["poc_b"]
            curfs.mark[q] = "L" if cur.get("_lt_cur") else "S"
        if cur.get("_lt_cur") and curfs.lt is not None and self.max_lt is not None and curfs.lt > self.max_lt and not self.lenient:
            raise StreamError("LongTermFrameIdx of the current picture exceeds MaxLongTermFrameIdx")
        if mmco5:
            curfs.frame_num = 0
        if new:
            self.stores.append(curfs)
        # keep stores that still have a marked field; the store of a first field stays (its second field may follow)
        self.stores = [s for s in self.stores if s.marked("S") or s.marked("L") or s is curfs]
        if not self.lenient:
            n = len([s for s in self.stores if s.marked("S") or s.marked("L")])
            if n > self.max_refs:
                raise StreamError(f"{n} reference frames after the picture: more than Max(max_num_ref_frames, 1) = {self.max_refs}")
            seen = {}
            for s in self.stores:
                if s.lt is not None and s.marked("L"):
                    if s.lt in seen:
                        raise StreamError(f"LongTermFrameIdx {s.lt} is held by two frames")
                    seen[s.lt] = s
        self.prev_ref_fn = 0 if mmco5 else cur["frame_num"]
        if mmco5:
            tmp = None
            if cur["struct"] == "F":
                tmp = min(cur["poc_t"], cur["poc_b"])
                new_t, new_b = cur["poc_t"] - tmp, cur["poc_b"] - tmp
            elif cur["struct"] == "T":
                new_t, new_b = 0, None
            else:
                new_t, new_b = None, 0
            for q in structs:
                curfs.poc[q] = new_t if q == "T" else new_b
            return dict(top=new_t, bot=new_b)
        return None

    def state(self):
        """[(store id, 'ST' marks string with S / L / - / . per parity, frame_num, LongTermFrameIdx or None)] sorted by store id,
        for stores with at least one field marked; max_lt."""
        rows = []
        for s in sorted(self.stores, key=lambda s: s.id):
            if not (s.marked("S") or s.marked("L")):
                continue
            ms = "".join((s.mark[p] or "-") if s.has[p] else "." for p in "TB")
            rows.append((s.id, ms, s.frame_num, s.lt if s.marked("L") else None))
        return rows, self.max_lt


def token(slot):
    return "-" if slot is None else f"{slot[0]}{slot[1]}"
