"""Helper process: print the motion vectors libavcodec exports (AVMotionVector side data), via ctypes, no headers needed.

usage: python3 common/ffmv_helper.py in.264 out.txt   (used by Chapters 7.3, 7.8, 7.9, 10.4)
Writes one line per exported vector:  <frame> <source> <w> <h> <dst_x> <dst_y> <mvx> <mvy> <scale>
(frame = 0-based index in OUTPUT order; for streams without B slices output order = decoding order.)
Run as a subprocess with a timeout (a crash here must not take the harness down).  Only plain C-ABI calls are used and the
only struct layouts relied on are AVFrameSideData {int type; uint8_t *data; size_t size} and AVMotionVector (40 bytes), both
stable for many years.  Exit code 3 = libavcodec shared library not found / unusable."""
import ctypes, ctypes.util, glob, struct, sys


def load_libs():
    libs = {}
    for name in ("avutil", "avcodec"):
        cands = [ctypes.util.find_library(name)] if ctypes.util.find_library(name) else []
        cands += sorted(glob.glob(f"/usr/lib/*/lib{name}.so.*") + glob.glob(f"/usr/lib/lib{name}.so.*")
                        + glob.glob(f"/usr/local/lib/lib{name}.so.*"), reverse=True)
        for c in cands:
            try:
                libs[name] = ctypes.CDLL(c, mode=ctypes.RTLD_GLOBAL)
                break
            except OSError:
                continue
        else:
            sys.exit(3)
    return libs["avutil"], libs["avcodec"]


def main():
    inp, outp = sys.argv[1], sys.argv[2]
    try:
        U, C = load_libs()
    except SystemExit:
        raise
    vp, ci, i64 = ctypes.c_void_p, ctypes.c_int, ctypes.c_int64
    U.av_malloc.restype = vp; U.av_malloc.argtypes = [ctypes.c_size_t]
    U.av_opt_set.argtypes = [vp, ctypes.c_char_p, ctypes.c_char_p, ci]
    U.av_frame_alloc.restype = vp
    U.av_frame_get_side_data.restype = vp; U.av_frame_get_side_data.argtypes = [vp, ci]
    U.av_log_set_level.argtypes = [ci]
    U.av_frame_unref.argtypes = [vp]
    C.avcodec_find_decoder.restype = vp; C.avcodec_find_decoder.argtypes = [ci]
    C.avcodec_alloc_context3.restype = vp; C.avcodec_alloc_context3.argtypes = [vp]
    C.avcodec_open2.argtypes = [vp, vp, vp]
    C.av_parser_init.restype = vp; C.av_parser_init.argtypes = [ci]
    C.av_parser_parse2.argtypes = [vp, vp, ctypes.POINTER(vp), ctypes.POINTER(ci), vp, ci, i64, i64, i64]
    C.av_packet_alloc.restype = vp
    C.av_packet_from_data.argtypes = [vp, vp, ci]
    C.av_packet_unref.argtypes = [vp]
    C.avcodec_send_packet.argtypes = [vp, vp]
    C.avcodec_receive_frame.argtypes = [vp, vp]
    U.av_log_set_level(-8)
    codec = C.avcodec_find_decoder(27)                   # AV_CODEC_ID_H264
    ctx = C.avcodec_alloc_context3(codec)
    if not codec or not ctx:
        sys.exit(3)
    U.av_opt_set(ctx, b"flags2", b"+export_mvs", 1)
    U.av_opt_set(ctx, b"threads", b"1", 1)
    if C.avcodec_open2(ctx, codec, None) < 0:
        sys.exit(3)
    parser = C.av_parser_init(27)
    pkt = C.av_packet_alloc()
    frame = U.av_frame_alloc()
    data = open(inp, "rb").read()
    out = open(outp, "w")
    state = {"n": 0}
    NOPTS = -0x8000000000000000

    def drain():
        while C.avcodec_receive_frame(ctx, frame) == 0:
            sd = U.av_frame_get_side_data(frame, 8)      # AV_FRAME_DATA_MOTION_VECTORS
            if sd:
                typ, ptr, size = struct.unpack("iQQ", ctypes.string_at(sd, 24)[:4] + b"\0\0\0\0" + ctypes.string_at(sd + 8, 16))
                raw = ctypes.string_at(ptr, size)
                for o in range(0, size - 39, 40):
                    src, w, h, sx, sy, dx, dy = struct.unpack_from("<iBBhhhh", raw, o)
                    mx, my = struct.unpack_from("<ii", raw, o + 24)
                    (sc,) = struct.unpack_from("<H", raw, o + 32)
                    out.write(f"{state['n']} {src} {w} {h} {dx} {dy} {mx} {my} {sc}\n")
            state["n"] += 1
            U.av_frame_unref(frame) if hasattr(U, "av_frame_unref") else None

    def feed(buf):
        if buf:
            mem = U.av_malloc(len(buf) + 64)
            ctypes.memmove(mem, buf, len(buf)); ctypes.memset(mem + len(buf), 0, 64)
            C.av_packet_from_data(pkt, mem, len(buf))
            C.avcodec_send_packet(ctx, pkt)
            C.av_packet_unref(pkt)
        else:
            C.avcodec_send_packet(ctx, None)
        drain()

    pos = 0
    bufp = ctypes.c_void_p(); bsz = ctypes.c_int(0)
    padded = data + b"\0" * 64
    while pos < len(data):
        n = min(65536, len(data) - pos)
        chunk = ctypes.create_string_buffer(padded[pos:pos + n + 64], n + 64)
        used = 0
        while used < n:
            r = C.av_parser_parse2(parser, ctx, ctypes.byref(bufp), ctypes.byref(bsz),
                                   ctypes.addressof(chunk) + used, n - used, NOPTS, NOPTS, 0)
            if r < 0:
                sys.exit(3)
            used += r
            if bsz.value:
                feed(ctypes.string_at(bufp, bsz.value))
        pos += n
    C.av_parser_parse2(parser, ctx, ctypes.byref(bufp), ctypes.byref(bsz), None, 0, NOPTS, NOPTS, 0)
    if bsz.value:
        feed(ctypes.string_at(bufp, bsz.value))
    feed(None)
    out.close()


if __name__ == "__main__":
    main()
