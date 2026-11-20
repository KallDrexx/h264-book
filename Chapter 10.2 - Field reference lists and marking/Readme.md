# Chapter 10.2 - Field reference lists and marking

> **SPOILER WARNING - do not open `spoiler_reference/` until your own decoder passes.** `spoiler_reference/fdpb.py` (field reference lists and marking), `fpdec.py` (the whole P / B field decoder), `fbs.py` (field deblocking decisions) and `direct_ref.py` / `dmvpred_ref.py` (direct prediction with fields) are complete solutions of this chapter; `fpgen.py` is the stream generator, and the other files are copies of earlier chapters' solutions. `test_fieldrefs.py` is plumbing and safe to read.

## 1. Goal and the project

Chapter 10.1 decoded intra-only field pictures. Real interlaced streams (broadcast 1080i, the PAFF conformance streams) code most pictures as **P and B fields** that predict from *other fields*. In this chapter you teach your decoder to do that:

* build the **reference lists of a field** (alternating parity, 8.2.4.2.5), also with `ref_pic_list_modification`;
* keep the **reference marking per field**, with the sliding window and all six MMCO commands working on fields;
* predict from fields (which lines, the chroma vector offset between parities), and use **field POCs** for B lists, temporal direct and implicit weights;
* deblock P / B field pictures.

You write: (A) a text dump with the **final reference lists of every P/B slice** and the **marking state after every picture**, and (B) the decoded frames as raw yuv. `test_fieldrefs.py` checks both.

Everything here builds on Chapters 7.2 / 7.6 / 7.8 / 7.9 (frame lists, marking, B lists, direct, implicit weights), 9.1 (bumping) and 10.1 (fields, pairing, POC). Scope: CAVLC, 4:2:0, 8-bit; frame and field pictures freely mixed (PAFF); MBAFF is Chapter 10.3 / 10.4.

## 2. Big picture

The only new idea is that a **reference picture can be a field**. A frame store of the DPB holds a frame, a complementary field pair or a lone field, and **each of its two fields has its own marking**: unused, short-term or long-term. So the DPB is now a set of *fields*, grouped in frames:

```
store 3:  top field = short-term     bottom field = short-term      (an ordinary reference frame)
store 4:  top field = short-term     bottom field = not yet decoded (first field of the picture being decoded)
store 2:  top field = unused         bottom field = long-term       (after an MMCO on one field)
```

A **frame** picture (P or B) can only use stores whose *both* fields carry the same marking (a "reference frame"); a **field** picture uses single fields. Consequences you will implement:

1. the P/B list of a field is built from *frames* (ordered as before) and then **split into fields** by taking parities alternately, starting with the parity of the current field;
2. the list of a **second field** contains the **first field of its own frame** (it is already a reference);
3. numbers (`FrameNumWrap`, `PicNum`, `LongTermPicNum`) double and carry a parity bit, so that commands can name single fields;
4. the sliding window counts **frames**, not fields, and does not run for a second field;
5. B lists, temporal direct and implicit weights use the POC of **fields**.

x264 cannot help (it writes no PAFF), so the streams are **synthetic** (section 5.4); ffmpeg (`-threads 1`) decoding them is the pixel truth, and the harness's reference decoder is the truth for the dump.

## 3. Concepts in detail

### 3.1 State per field

For every frame store keep: `frame_num`, `LongTermFrameIdx` (shared by both fields), per parity `decoded?`, its POC, and `mark` in {none, short, long}. Notation below: a store is shown as `3` and its fields as `3T` (top) and `3B` (bottom); in the dump a store's marks are written `SS`, `S.`, `-L`, ... (top mark, bottom mark; `S` short, `L` long, `-` decoded but unused, `.` not decoded).

A store *has a reference field* when at least one field is marked. The model of Chapter 7.6 is the special case where both fields always change together.

### 3.2 Numbers (8.2.4.1)

`FrameNumWrap(store) = frame_num - MaxFrameNum` if `frame_num` is greater than the current picture's `frame_num`, else `frame_num` (a store whose fields are short-term). For **frames** `PicNum = FrameNumWrap`, `LongTermPicNum = LongTermFrameIdx`, `CurrPicNum = frame_num`, `MaxPicNum = MaxFrameNum`. For **fields**:

| quantity | value |
|---|---|
| `CurrPicNum` | `2 * frame_num + 1` |
| `MaxPicNum` | `2 * MaxFrameNum` |
| `PicNum` of a short-term field | `2 * FrameNumWrap + 1` if it has the **same parity** as the current field, `2 * FrameNumWrap` for the opposite parity |
| `LongTermPicNum` of a long-term field | `2 * LongTermFrameIdx + 1` same parity, `2 * LongTermFrameIdx` opposite parity |

*Example* (`MaxFrameNum` 16): current picture is a bottom field with `frame_num` 2, so `CurrPicNum` = 5. The top field of the store with `frame_num` 1 has `PicNum` 2, its bottom field 3. A store with `frame_num` 14 has `FrameNumWrap` -2, so its top field is -4 and its bottom field -3 (negative numbers are normal; they are why commands wrap modulo `MaxPicNum`).

The first field of the *current* frame (when decoding the second field) has `FrameNumWrap = frame_num`, the highest of all.

### 3.3 Initial list of a P field (8.2.4.2.2 and 8.2.4.2.5)

1. `refFrameList0ShortTerm`: every store with **at least one field marked short-term** (this includes the first field of the current frame, and stores where the other field is unused or long-term), ordered by `FrameNumWrap` **descending**.
2. `refFrameListLongTerm`: every store with at least one long-term field, ordered by `LongTermFrameIdx` **ascending**.
3. Each frame list is turned into a field list by **alternating parity** (8.2.4.2.5): take fields alternately, starting with a field of the **same parity as the current field**. Go down the frame list for the next frame whose field of the wanted parity is *marked as the kind this list is about* (a missing, undecoded or unmarked field is skipped, not replaced). When one parity runs out, append the remaining fields of the other parity in frame-list order.
4. `RefPicList0 = (short-term fields) followed by (long-term fields)`.

*Example.* Two reference frames: store 0 (`frame_num` 0, both fields short-term) and store 1 (`frame_num` 1, both short-term). Current: first field (top) of `frame_num` 2.

```
frame list (FrameNumWrap descending): 1, 0
top fields   : 1T 0T      bottom fields : 1B 0B
alternate, top first                    : 1T 1B 0T 0B      (PicNum 3 2 1 0)
```
If the current field is a **bottom** first field the same frames give `1B 1T 0B 0T`. Now the **second field** (bottom) of this frame (store 2 holds the decoded top field, `frame_num` 2): the frame list is `2, 1, 0`; bottom fields available: `1B 0B` (2B is the current picture); top fields `2T 1T 0T`:

```
alternate, bottom first : 1B 2T 0B 1T      then the bottom fields are used up: append 0T
RefPicList0             : 1B 2T 0B 1T 0T       (PicNum 3 4 1 2 0)
```
The first field of the same frame (`2T`) therefore sits at index 1. A **P field without any other reference** (an IDR top field followed by a P bottom field) has the list `0T` only.

**P frames** in a DPB that holds fields: only stores with **both** fields marked short-term (long-term: both long-term) are frames of the list; a store with one marked field is simply absent from frame lists (8.2.4.2.1). Order and numbering are those of Chapter 7.2/7.6.

### 3.4 Initial lists of a B field (8.2.4.2.4)

`refFrameList0ShortTerm` and `refFrameList1ShortTerm` are built from the stores with a short-term field, ordered by the **POC of the store**:

* **POC of a store** = the smaller POC of its fields that are **marked as used for reference**: with both fields marked, the POC of the frame / pair; with one field marked (a lone field, the first field of the current frame, or a pair after an MMCO unmarked one field) the POC of that field. For the second field of a frame the first field is therefore ordered by *its own* POC. (ffmpeg and JM take the minimum over all *decoded* fields, ignoring marking; the two only differ for a pair with one unmarked field, which the suite never puts in a B field list, see 3.11.)
* `refFrameList0ShortTerm`: stores with POC **less than or equal to** the POC of the current field, descending; then those with greater POC, ascending.
* `refFrameList1ShortTerm`: greater POC ascending; then less-or-equal, descending.
* `refFrameListLongTerm`: by `LongTermFrameIdx` ascending (the same for both lists).

Each of the three frame lists is split into fields by alternating parity **starting with the current field's parity** (3.3). Then `RefPicList0 = short0 fields + long fields`, `RefPicList1 = short1 fields + long fields`. Finally **if `RefPicList1` has more than one entry and is identical to `RefPicList0`, swap its first two entries** (the comparison is on the full lists before any truncation).

*Example.* Stores 0 (POC top 0 / bottom 1) and 1 (POC 8 / 9), current: top field, POC 4 (a non-reference B field):
```
list0 frames: 0 (0 <= 4), then 1      -> fields, top first : 0T 0B 1T 1B
list1 frames: 1 (8 > 4), then 0       -> fields            : 1T 1B 0T 0B
```
For a bottom field with POC 5 the fields start with B: `0B 0T 1B 1T` / `1B 1T 0B 0T`. If **both** stores lay before the current field (a B field with only past references, as in "tail" streams), the two frame lists are equal, so the field lists are equal and `RefPicList1` becomes `[second entry, first entry, ...]`.

**B frames** use frame POCs (minimum of both fields) and only frames with both fields marked, as in Chapter 7.8.

### 3.5 num_ref_idx_active

`num_ref_idx_l0/l1_default_active_minus1` of the PPS apply **unchanged** to field pictures (a PPS default of 2 means 2 entries for a field as for a frame); the slice header may override with `num_ref_idx_active_override_flag` up to **32** entries for fields (16 for frames). The initial list is cut or padded ("no reference picture") to that length *before* modification. A list longer than the number of available fields is normal for fields (a frame list of 4 frames has up to 8 fields); entries that are "no reference picture" must simply never be used. `ref_idx` is coded `te(v)`: absent when the list has 1 entry, 1 bit (inverted) with 2 entries, else `ue(v)`; the range is the number of entries minus one, so a field can use `ref_idx` up to 31.

### 3.6 List modification for fields (8.2.4.3)

Same loop as Chapter 7.6 with the field numbers of 3.2:

* start with `picNumLXPred = CurrPicNum` (`2*frame_num+1`);
* `idc 0`: `picNumLXNoWrap = pred - (abs_diff_pic_num_minus1 + 1)`, plus `MaxPicNum` if negative; `idc 1`: `pred + (abs_diff_pic_num_minus1 + 1)`, minus `MaxPicNum` if it reaches `MaxPicNum`. `pred = NoWrap`. `picNumLX = NoWrap - MaxPicNum` if `NoWrap > CurrPicNum`, else `NoWrap`. The wanted picture is the **short-term field with `PicNum == picNumLX`**;
* `idc 2`: `long_term_pic_num` names the **long-term field with that `LongTermPicNum`**;
* insert the picture at `refIdxLX`, shift the rest up, and remove the *later* entry that is the same **field** (compare field identity: store *and* parity, short/long kind). The other field of the same frame is a different picture and stays.

*Example.* Stores 0 (`frame_num` 0) and 1 (`frame_num` 1), current: bottom first field, `frame_num` 2 (`CurrPicNum` 5, `MaxPicNum` 32). Fields: `0T` PicNum 0, `0B` 1, `1T` 2, `1B` 3. Initial list `1B 1T 0B 0T`. The command `idc 0, abs_diff_pic_num_minus1 = 4` asks for `picNum 5 - 5 = 0`, i.e. `0T`; result `0T 1B 1T 0B`.

### 3.7 Marking (8.2.5)

**Sliding window (8.2.5.3).** Only for a reference picture without MMCO commands:

* if the current picture is the **second field** of a frame and the **first field is marked short-term**, nothing is removed: the second field is simply marked short-term too;
* otherwise, when `numShortTerm + numLongTerm == Max(max_num_ref_frames, 1)`, remove the short-term **store** with the smallest `FrameNumWrap`: **all** its short-term fields become unused. `numShortTerm` / `numLongTerm` count **frames, complementary pairs and unpaired fields** having any field marked short / long (the current frame's first field counts when you decode its second field).

**Marking the current picture.** After the commands the current frame / field is marked short-term, unless `mmco 6` made it long-term. The second field of a frame joins the store of its first field. After marking, at most `Max(max_num_ref_frames, 1)` stores may have a marked field.

**MMCO (8.2.5.4), per field** (`CurrPicNum`, `PicNum`, `LongTermPicNum` from 3.2; for frame pictures the frame numbers and both fields):

| op | on a field picture | notes |
|---|---|---|
| 1 | `picNumX = CurrPicNum - (difference_of_pic_nums_minus1 + 1)`: that **one** short-term field becomes unused | the other field of the frame keeps its marking; the frame count does not change until both are gone |
| 2 | `long_term_pic_num`: that one long-term field becomes unused | |
| 3 | the short-term field `picNumX` becomes long-term with `long_term_frame_idx` | if that index is held by another frame (or another frame's field) that holder is freed; **if it is held by the other field of the same frame, nothing is freed** (both fields share the index). The index must not exceed `MaxLongTermFrameIdx` |
| 4 | `max_long_term_frame_idx_plus1`: long-term fields with `LongTermFrameIdx` above the new maximum become unused; `MaxLongTermFrameIdx` = value - 1 ("no index" for 0) | |
| 5 | all fields unused, `MaxLongTermFrameIdx` "no index"; the picture then counts as `frame_num` 0 and its POC is rebased (frame: minimum of both fields becomes 0; top field: its POC becomes 0; bottom field likewise) | a new output period starts, as with IDR |
| 6 | the **current field** becomes long-term with the index; a holder of the index in *another* frame is freed, **the first field of the same frame is not** | when the first field is long-term the second field must also use `mmco 6` with the same index |

Frame pictures use the same commands on frames (`PicNum` = `FrameNumWrap`) and change both fields; only stores whose two fields have the same marking can be addressed that way.

**Unpaired fields.** A lone reference field occupies a store (one marked field, the other `.`); it counts as one frame in the sliding window and is a candidate for field lists but never for frame lists. (The suite has no lone fields; ffmpeg does not output them, see Chapter 10.1.)

### 3.8 Sample prediction from fields (8.4.2.1, 8.4.2.2)

* The reference picture of a field slice is **one field**: line `y` of the field is line `2*y + parity` of the stored frame (top parity 0). Treat it as a picture of `PicHeightInMbs*16` lines; **clamp coordinates to the field**, not the frame.
* Motion vectors are in **quarter samples of the field** (vertical units are field lines); interpolation (7.4) runs inside the field. Macroblock addresses, mv prediction and neighbours work in the field exactly as in a frame (10.1).
* **Chroma vertical offset (Table 8-9).** When the reference field has a different parity from the current field, the chroma vector is `mvCLX[1] = mvLX[1] + offset` with offset **-2** if the reference is a **bottom** field and the current field is a **top** field, **+2** if the reference is a **top** field and the current field is a **bottom** field (chroma vectors are in 1/8 chroma sample units, so 2 is a quarter chroma sample). Same parity: 0. It is the geometric correction for the half-line shift between the two fields. Luma is not corrected.

### 3.9 Temporal and spatial direct in field pictures (8.4.1.2)

Everything of Chapter 7.9 holds with *field* data:

* The **co-located picture** of a field is the **field `RefPicList1[0]`** itself when that field was coded as a field (`mbAddrCol = CurrMbAddr`, same macroblock address, same 4x4 block; the vertical vector is used unscaled). `direct_8x8_inference_flag` is 1 because `frame_mbs_only_flag` is 0, so each 8x8 quadrant (`x8`, `y8` = 0 or 1) reads one **corner** 4x4 block, column `3*x8`, row `3*y8`.
* `refIdxL0 = (refIdxCol < 0) ? 0 : MapColToList0(refIdxCol)`: the **field** (store *and* parity) that the co-located block referenced is looked up in the current `RefPicList0`; the lowest index wins. `refIdxL1 = 0`.
* `DistScaleFactor` uses the **POCs of fields**: `tb = POC(current field) - POC(pic0)`, `td = POC(pic1) - POC(pic0)` where `pic0` = `RefPicList0[refIdxL0]`, `pic1` = `RefPicList1[0]`, each a field with its own POC. Then the usual `tx`, `DistScaleFactor`, `mvL0 = (DSF * mvCol + 128) >> 8`, `mvL1 = mvL0 - mvCol`; long-term `pic0` or `td = 0` gives `mvL0 = mvCol`, `mvL1 = 0`.
* `colZeroFlag` (spatial) needs `RefPicList1[0]` short-term, `refIdxCol = 0` and both components of `mvCol` in -1..1 (units of the field).
* **Implicit weights** (`weighted_bipred_idc` 2) for a field: `currPicOrField`, `pic0`, `pic1` are the current field and the two **fields** the vectors refer to; `DistScaleFactor >> 2` gives `w1`, `w0 = 64 - w1`, with the fallback to 32/32 for `DiffPicOrderCnt(pic1, pic0) == 0`, a long-term reference, or `w1` outside -64..128. A *frame* picture uses the frame POCs (minimum of the two fields).

**Mixed coding (8.4.1.2.1).** In a PAFF stream `RefPicList1[0]` may have been coded the other way. Then 8.4.1.2.1 (Tables 8-6 and 8-8) picks a different co-located macroblock and 4x4 row and sets `vertMvScale`. With `(mx, my)` the current macroblock's column and row (in the current picture):

| current | `RefPicList1[0]` coded as | co-located picture | co-located MB (column `mx`, row) | 4x4 block (column `3*x8`, row) | `vertMvScale` |
|---|---|---|---|---|---|
| field | field | the field itself | `my` | `3*y8` | One_To_One |
| field | frame | the **frame** containing it | frame row `2*my + y8` (`mbAddrCol1`) | `2*y8` (`yM = (2*yCol) % 16`) | Frm_To_Fld |
| frame | fields (a pair) | the field of the pair **closer in POC** | field row `my / 2` (`mbAddrCol4`) | `2*(my % 2) + y8` (`yM = 8*(my % 2) + 4*y8`) | Fld_To_Frm |
| frame | frame | the frame | `my` | `3*y8` | One_To_One |

"Closer in POC": the top field if `|POC(top) - POC(cur)| < |POC(bottom) - POC(cur)|`, otherwise (ties too) the bottom field; `POC(cur)` is the current frame's POC (minimum of its two fields). This is exactly Chapter 10.4's MBAFF table (3.4) with whole pictures in place of macroblock pairs.

*Temporal direct* then converts the co-located data before the usual steps: `Frm_To_Fld`: `mvCol.y = mvCol.y / 2` (division **toward zero**, so -3 gives -1) and `refIdxL0` refers to the **field of the frame `refPicCol` with the current picture's parity** (`refPicCol` = the frame the co-located block referenced); `Fld_To_Frm`: `mvCol.y = mvCol.y * 2` and `refIdxL0` refers to the **frame containing the field `refPicCol`**. In both cases `refIdxL0` is the lowest index of the current `RefPicList0` that holds that picture, `refIdxL1 = 0`, and the POCs for `DistScaleFactor` are those of the current picture's kind (fields for a field picture, frames for a frame picture). *Spatial direct* uses `refIdxCol` and `mvCol` **as stored** (no scaling) for `colZeroFlag`. The case `d_mixed` exercises all four rows of the table with both direct modes.

### 3.10 Deblocking of P and B field pictures (8.7.2.1)

Chapter 10.1's rules plus the inter rules: bS 4 on intra **vertical** macroblock edges, **3 on intra horizontal macroblock edges** of field pictures, 3 on internal intra edges. For the motion test (bS 1) in a field picture the **vertical** component limit is **2** quarter field samples (the standard states "4 in units of quarter *frame* samples", which is 2 in field units; JM uses `mvlimit = 2` for field pictures, ffmpeg `mvy_limit = 2` for interlaced macroblocks); the horizontal limit stays 4. "Different reference pictures" compares **fields**: the top and the bottom field of one frame are different reference pictures. Frame pictures of a PAFF stream use the frame rules.

### 3.11 Where ffmpeg 8.0 differs from the standard (observed while building the suite)

The standard is the truth for your decoder; these corner cases are avoided in the ffmpeg-verified cases:

1. **Frame lists with a one-field frame.** A frame P/B picture while some store has only *one* marked field: the suite avoids it (ffmpeg's frame lists differ there, the standard excludes such stores).
2. **`mmco 6` on a second field when the first field is long-term with the same index** (also the IDR long-term flag on a field pair): ffmpeg keeps only the second field as a reference. The case `mmco6_fields` contains these; its truth is the reference decoder.
3. **Direct prediction whose co-located field is the first field of the current frame** (a second B field with only past references): ffmpeg decodes it differently from the model; unresolved, the suite avoids it. If you implement it as written in 3.9 you cannot verify it here.
4. **POC of a half-unmarked pair in B field lists** (3.4): ffmpeg (and JM) order it by the minimum over both decoded fields, the standard by its marked field only. The suite avoids the case, so both pass.
5. `-threads` > 1 makes ffmpeg's result differ for second fields that use the first field (race); the harness always uses `-threads 1`. Do the same when you compare by hand.

## 4. The project: exactly what to build

Starting from your Phase 7 / Phase 9 / Chapter 10.1 decoder:

1. **Slice header** for P/B in fields: `field_pic_flag`, `bottom_field_flag`, `num_ref_idx_active` up to 32, list modification with the field numbers, `dec_ref_pic_marking` (MMCO).
2. **DPB with per-field marking** (3.1) and the **sliding window / MMCO** of 3.7, including the second-field rules.
3. **Initial lists** of P fields, B fields (3.3, 3.4), P/B frames in a field-capable DPB; truncation, modification (3.6).
4. **Prediction** from fields (3.8): field reference planes, clamping, chroma offset; direct and implicit weights with field POCs (3.9). Reuse the motion vector prediction, interpolation and residual code of Phase 7 unchanged.
5. **Deblocking** with the field rules (3.10).
6. **Output**: pairing, POC, bumping and frame interleaving from 10.1.
7. Write the **dump** (5.1) and the **yuv** (5.2).

Verify: all suite cases pass (`--check`); then the PAFF conformance streams with P and B fields and a real 1080i recording decode byte-identical to their reference yuv (not available offline; use `--stream` for an ffmpeg comparison, remembering the deviations of 3.11).

## 5. Harness input format

### 5.1 The dump: `<case>.dump`

Text, one item per line; three line kinds, each `PIC n`, `DPB n` and `LIST n slice=k` exactly once. All numbers decimal; fields `key=value` in any order; extra keys are ignored; blank lines and `#` lines are ignored.

```
PIC  <n> struct=<F|T|B> frame_num=<fn> idr=<0|1> ref=<0|1> store=<s> second=<0|1> top=<..|-> bot=<..|-> poc=<PicOrderCnt>
LIST <n> slice=<k> type=<P|B> l0=<tok,tok,...> l1=<tok,...|->
DPB  <n> maxlt=<MaxLongTermFrameIdx|-> stores=<store>:<marks>:<frame_num>:<LongTermFrameIdx|->,<store>:...   (or stores=-)
```

* `n` is the **picture number** (every coded frame or field, decoding order, from 0); `k` the slice index inside the picture (0, 1, ...). `PIC` lines are Chapter 10.1's (here `top`/`bot` are the field POCs, `-` for the absent parity; `store` numbers frame stores from 0 in creation order; `second=1` for the second field of a frame).
* `LIST`: one line per **P or B slice**, the **final** lists (after initialisation, truncation to `num_ref_idx_active`, and modification), exactly `num_ref_idx_active` entries each. A token is `<store><parity>` with parity `T`, `B` (field) or `F` (frame picture): `3T`, `0B`, `2F`; `-` is "no reference picture". `l1=-` for P slices (it may be omitted).
* `DPB`: after the **marking** of picture `n` (also for non-reference pictures, where it repeats the previous state): `maxlt` the `MaxLongTermFrameIdx` (`-` = "no long-term frame indices"); then one comma-separated item per store that still has a marked field, ordered by store number: `<store>:<marks>:<frame_num>:<idx>` where `marks` is two characters, top then bottom: `S` short-term, `L` long-term, `-` decoded but unused, `.` not decoded; `frame_num` is the store's `frame_num` (0 after an MMCO 5 picture); `idx` its `LongTermFrameIdx` (only if a field is long-term). A DPB without reference fields: `stores=-`.

Example (case `p_fields`, start):
```
PIC 0 struct=T frame_num=0 idr=1 ref=1 store=0 second=0 top=0 bot=- poc=0
DPB 0 maxlt=- stores=0:S.:0:-
PIC 1 struct=B frame_num=0 idr=0 ref=1 store=0 second=1 top=- bot=1 poc=1
LIST 1 slice=0 type=P l0=0T,-,-,-,-,-,-,-,-,-,- l1=-
DPB 1 maxlt=- stores=0:SS:0:-
PIC 2 struct=T frame_num=1 idr=0 ref=1 store=1 second=0 top=8 bot=- poc=8
LIST 2 slice=0 type=P l0=0T,0B,-,-,-,-,- l1=-
```
`PicOrderCnt`, `store` and `second` come from Chapter 10.1's rules. The `PIC` POCs are the values decoded from the slice header, *before* the rebasing of an MMCO 5 picture (3.7); later lists use the rebased values. The truth files for every case are written by `--truth DIR`.

### 5.2 The pixels: `<case>.yuv`

All **output frames** in output order (ascending POC inside each IDR / MMCO-5 period), 80x96, planar yuv420p 8-bit, no cropping, no padding (11520 bytes per frame).

### 5.3 What is compared

Dump: every `PIC`, `LIST`, `DPB` line of the truth must exist in yours with identical values. Pixels: ffmpeg's decode (`-threads 1`), except the case `mmco6_fields` whose truth is the reference decoder (3.11).

### 5.4 The suite (CAVLC, 80x96 pictures, 5x6 macroblocks; a field is 5x3)

Every stream: Main profile, `frame_mbs_only_flag = 0`, `direct_8x8_inference_flag = 1`, POC type 0, `max_num_ref_frames` 1-5, `log2_max_frame_num` 4 (so `frame_num` wraps), PPS defaults of 1/2/4 and 1/1/3 active entries (random override), `weighted_bipred_idc` 0 or 2, random slice QP, 1-2 slices per picture, and **no residual** (`coded_block_pattern` 0): the macroblock types are P/B inter types, P_Skip, B_Skip, B_Direct, I_PCM and Intra16x16 DC. Pictures are frames, top-first or bottom-first field pairs, I/P/B, reference or non-reference.

| case | what it exercises |
|---|---|
| `p_fields`, `p_mixed` | P field lists (alternation, first field of the current frame), field pairs only / mixed with frames |
| `p_mod`, `p_wrap` | list modification with field picNums, `frame_num` wrap (40 units) |
| `slide`, `slide1` | sliding window with second fields, `max_num_ref_frames` 2 and 1 |
| `b_fields`, `b_mixed`, `b_tail`, `b_mod` | B field lists, POC order, list-1 swap (only past references), modification |
| `d_spatial`, `d_temporal` | spatial / temporal direct with field co-located blocks |
| `d_mixed` | direct with a co-located picture coded the other way (field picture / frame-coded `RefPicList1[0]` and frame picture / field pair): `Frm_To_Fld`, `Fld_To_Frm` (3.9) |
| `w_implicit`, `w_temporal` | implicit weights with field POCs, large POC steps; with temporal direct |
| `b_spread`, `d_spread`, `b_equal` | the two fields of a frame with POCs far apart or **equal** (frames interleave in POC): POC of an entry = minimum, `<=` for list 0, current POC = the field's own; implicit weights / temporal direct on top |
| `lt_fields`, `lt_pairs`, `lt_b`, `lt_direct` | MMCO 1-4 on fields, long-term fields in P/B lists, `long_term_pic_num` commands |
| `mmco_mixed` | marking commands on frames and fields mixed, IDR |
| `mmco5_frames` | MMCO 5 in a PAFF stream (frames) |
| `mmco6_fields` | MMCO 6 and the IDR long-term flag on fields (truth: reference decoder) |
| `dbk_fields`, `dbk_mixed` | deblocking of P/B fields with small vertical vector differences, bS 3 |

## 6. Running the harness

```
python3 test_fieldrefs.py --make DIR                           # DIR/<case>.264
# run YOUR program on every DIR/<case>.264: write OUTDIR/<case>.dump and OUTDIR/<case>.yuv
python3 test_fieldrefs.py --check DIR OUTDIR [--only a,b] [--no-dump] [--no-yuv]
python3 test_fieldrefs.py --suite DIR --reader './mydec {stream} {dump} {yuv}'
python3 test_fieldrefs.py --truth DIR                          # DIR/<case>.expected.dump / .expected.yuv to diff yourself
python3 test_fieldrefs.py --stream any.264 --yuv mine.yuv      # pixels of any stream against ffmpeg
python3 test_fieldrefs.py --self-test                          # about 7 minutes (CPU bound, pure Python)
```

* Exit 0 pass, 1 fail, 2 missing / malformed input. Streams are determined by fixed seeds; `--check` regenerates them and refuses a stale `DIR/<case>.264`. The first run builds the streams (a minute, cached in `.cache/ch10_2`).
* Dump failures print case, picture, slice, key, expected and actual, and for lists the first differing index. Pixel failures name the output frame, the pictures that made it, the first differing plane / macroblock / sample and which field line it is on.
* `--self-test` checks the harness: the reference decoder equals ffmpeg frame for frame on every case whose truth is ffmpeg; its own output passes; 8 corruptions of a reader output (list entries, marking, missing lines, samples, frame order and count) fail; and **every one of 42 wrong rules** of the reference decoder (parity alternation start, missing first field of the current frame, `FrameNumWrap`, `PicNum` / `LongTermPicNum` parity, `CurrPicNum` / `MaxPicNum`, sliding window counting fields or running for second fields, MMCO 1/2/3/4/6 field rules, B-list POC choices, `<=` vs `<`, the list-1 swap, chroma vector offset, co-located field choice, the frame / field co-located conversions (row, vertical scaling, field choice), `MapColToList0` by frame, implicit-weight POC choice, bS 3 / vector limit / field-reference identity in deblocking, output order, ...) changes the dump or the pictures of at least one case; the output names the case and channel for each.

## 7. Reading failures

* **`LIST` wrong on the first field of the second picture of a frame**: the first field of the current frame must be in the list (it is already marked); in P lists it has the highest `FrameNumWrap`.
* **Lists right for frames, wrong for fields**: alternation must start with the *current* parity and skip, not replace, a field that is missing or unmarked.
* **Only B field lists wrong**: POC of the entry (minimum over decoded fields; the current frame's first field uses its own), `<=` for list 0, and the swap rule on the full lists.
* **Modification commands pick the wrong field**: `CurrPicNum = 2*frame_num+1`, `MaxPicNum = 2*MaxFrameNum`, parity bit of `PicNum` (same parity = +1), `LongTermPicNum` likewise.
* **`DPB` wrong after a second field**: no sliding window for it; MMCO 1 and 2 touch one field.
* **A store disappears too early / too late**: the sliding window counts frames; a frame with one unused field still counts.
* **Pixels wrong only where the chroma of a P field looks shifted vertically by a fraction**: the +-2 chroma offset between parities.
* **Temporal-direct macroblocks wrong**: co-located field = `RefPicList1[0]`, `MapColToList0` identifies fields, POCs are field POCs.
* **Horizontal block edges of B/P fields too sharp / not sharp enough**: bS 3 on intra horizontal edges, vertical vector limit 2.

## 8. What's next

Chapter 10.3 starts **MBAFF**: frame pictures in which every macroblock *pair* is coded either as two frame macroblocks or as a top and a bottom field macroblock (`mb_field_decoding_flag`, inferred when both macroblocks of a pair are skipped). The new work is parsing and neighbour derivation (6.4.12.2): which macroblock is the left / above neighbour of a frame or field macroblock in a pair, which feeds CAVLC `nC`, CABAC contexts, intra prediction and vector prediction. The field reference lists and the field reconstruction of this chapter are reused in 10.4, where field macroblocks of an MBAFF picture predict from the fields of reference frames.
