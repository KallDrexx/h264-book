"""Synthetic yuv420p frames whose content identifies the frame number and shows orientation / channel order.

Every frame: horizontal luma ramp with 4-row stripes, vertical Cb ramp, horizontal Cr ramp shifted by 9 per frame, a bright box in
the top-left corner (up/down and left/right errors show), and along the bottom a row of N cells with only cell n lit (frame id)."""


def build(w, h, n):
    cw, ch = w // 2, h // 2
    out = bytearray()
    for f in range(n):
        Y = bytearray(w * h)
        for y in range(h):
            for x in range(w):
                v = 40 + x * 160 // w + (20 if (y // 4) % 2 == 0 else 0)
                if x < w // 4 and y < h // 4:
                    v = 200
                if y >= h - max(2, h // 8):
                    v = 235 if x * n // w == f else 40
                Y[y * w + x] = v
        U = bytearray(cw * ch)
        V = bytearray(cw * ch)
        for y in range(ch):
            for x in range(cw):
                U[y * cw + x] = 64 + y * 128 // ch
                V[y * cw + x] = (192 - x * 128 // cw + 9 * f) % 256
        out += Y + U + V
    return bytes(out)
