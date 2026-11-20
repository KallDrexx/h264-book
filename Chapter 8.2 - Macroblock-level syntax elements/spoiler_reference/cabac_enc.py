"""Independent CABAC ENCODER (H.264 clause 9.3.4.2 flowcharts) used to build the test vectors of Chapter 8.1.

It is written from the encoding flowcharts (PutBit / RenormE / EncodeDecision / EncodeBypass / EncodeTerminate /
EncodeFlush), not from the decoder, and uses only the constant tables.  It is checked by a round trip (encode, then decode with
cabac_ref) and by ffmpeg decoding the synthetic streams built with it.
"""
import tables_data as T


class Encoder:
    def __init__(self):
        self.bits = []          # output bits (list of 0/1)
        self.start()

    def start(self):
        self.low, self.range, self.first, self.out = 0, 510, True, 0

    def put(self, b):
        if self.first:
            self.first = False
        else:
            self.bits.append(b)
        while self.out > 0:
            self.bits.append(1 - b)
            self.out -= 1

    def renorm(self):
        while self.range < 256:
            if self.low < 256:
                self.put(0)
            elif self.low >= 512:
                self.low -= 512
                self.put(1)
            else:
                self.low -= 256
                self.out += 1
            self.range <<= 1
            self.low <<= 1

    def decision(self, st, b):
        """st = [pStateIdx, valMPS] (updated)."""
        q = (self.range >> 6) & 3
        lps = T.RANGE_LPS[st[0]][q]
        self.range -= lps
        if b != st[1]:
            self.low += self.range
            self.range = lps
            if st[0] == 0:
                st[1] = 1 - st[1]
            st[0] = T.TRANS_LPS[st[0]]
        else:
            st[0] = min(st[0] + 1, 62)
        self.renorm()

    def bypass(self, b):
        self.low <<= 1
        if b:
            self.low += self.range
        if self.low >= 1024:
            self.put(1)
            self.low -= 1024
        elif self.low < 512:
            self.put(0)
        else:
            self.low -= 512
            self.out += 1

    def terminate(self, b):
        self.range -= 2
        if b:
            self.low += self.range
            self.flush()
        else:
            self.renorm()

    def flush(self):
        self.range = 2
        self.renorm()
        self.put((self.low >> 9) & 1)
        v = ((self.low >> 7) & 3) | 1
        self.bits += [(v >> 1) & 1, v & 1]

    def align_zero(self):
        while len(self.bits) % 8:
            self.bits.append(0)

    def raw_bytes(self, bs):
        for x in bs:
            self.bits += [(x >> (7 - i)) & 1 for i in range(8)]

    def restart(self):
        self.start()

    def tobytes(self):
        b = list(self.bits)
        while len(b) % 8:
            b.append(0)
        return bytes(int("".join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))
