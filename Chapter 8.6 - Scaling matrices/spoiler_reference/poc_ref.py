"""SPOILER: picture order count of frame pictures (8.2.1), all three pic_order_cnt_type values, for Chapter 7.8.

PocState.next(hdr, sps, nal_ref_idc, idr) -> (top, bottom, poc) for the next picture in decoding order; PicOrderCnt(frame) = min(top,
bottom).  After the picture's marking process the caller reports PocState.finish(mmco5, top, bottom, frame_num) so the prev* variables
follow 8.2.1 (after a picture with memory_management_control_operation 5 its POC is rebased: tempPicOrderCnt is subtracted and
frame_num is treated as 0).  hdr is the dict of the slice header's first values (ffmpeg trace_headers element names), sps likewise.

MUTANT (harness self-test): "poc_no_msb" ignores the MSB (the lsb alone is the POC: wrong after the first wrap).
"""
MUTANT = None


class PocState:
    def __init__(self):
        self.prev_msb = 0
        self.prev_lsb = 0
        self.prev_fn_offset = 0
        self.prev_fn = 0
        self._cur = None

    def next(self, hdr, sps, nal_ref_idc, idr):
        t = sps["pic_order_cnt_type"]
        fn = hdr["frame_num"]
        max_fn = 1 << (sps["log2_max_frame_num_minus4"] + 4)
        if t == 0:
            max_lsb = 1 << (sps["log2_max_pic_order_cnt_lsb_minus4"] + 4)
            pm, pl = (0, 0) if idr else (self.prev_msb, self.prev_lsb)
            lsb = hdr["pic_order_cnt_lsb"]
            if lsb < pl and pl - lsb >= max_lsb // 2:
                msb = pm + max_lsb
            elif lsb > pl and lsb - pl > max_lsb // 2:
                msb = pm - max_lsb
            else:
                msb = pm
            if MUTANT == "poc_no_msb":
                msb = 0
            top = msb + lsb
            bottom = top + hdr.get("delta_pic_order_cnt_bottom", 0)
            self._cur = (msb, lsb, None)
        else:
            off = 0 if idr else (self.prev_fn_offset + max_fn if self.prev_fn > fn else self.prev_fn_offset)
            if t == 2:
                temp = 0 if idr else (2 * (off + fn) - 1 if nal_ref_idc == 0 else 2 * (off + fn))
                top = bottom = temp
            else:
                n = sps["num_ref_frames_in_pic_order_cnt_cycle"]
                offs = [sps[f"offset_for_ref_frame[{i}]"] for i in range(n)]
                absfn = off + fn if n else 0
                if nal_ref_idc == 0 and absfn > 0:
                    absfn -= 1
                exp = 0
                if absfn > 0:
                    cyc, inc = divmod(absfn - 1, n)
                    exp = cyc * sum(offs) + sum(offs[:inc + 1])
                if nal_ref_idc == 0:
                    exp += sps["offset_for_non_ref_pic"]
                top = exp + hdr.get("delta_pic_order_cnt[0]", 0)
                bottom = top + sps["offset_for_top_to_bottom_field"] + hdr.get("delta_pic_order_cnt[1]", 0)
            self._cur = (None, None, off)
        return top, bottom, min(top, bottom)

    def finish(self, mmco5, top, bottom, frame_num, nal_ref_idc):
        msb, lsb, off = self._cur
        if mmco5:
            top = top - min(top, bottom)
        if msb is not None:                      # type 0: prev* are those of the previous REFERENCE picture
            if nal_ref_idc:
                self.prev_msb, self.prev_lsb = (0, top) if mmco5 else (msb, lsb)
        else:
            self.prev_fn_offset = 0 if mmco5 else off
            self.prev_fn = 0 if mmco5 else frame_num
