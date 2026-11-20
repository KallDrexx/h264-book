"""Generators for the Chapter 8.1 test inputs: context-initialisation cases and arithmetic-decoder vectors."""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cabac_enc  # noqa: E402
import cabac_ref as R  # noqa: E402

BINS = {}      # vector name -> bins the encoder was told to code, in op order (P ops carry no bin)
QPS = [-12, -6, -1, 0, 1, 2, 3, 5, 8, 11, 13, 17, 20, 23, 26, 29, 32, 35, 38, 41, 44, 47, 50, 51]


def ctx_cases_text():
    lines = ["# slice_type cabac_init_idc SliceQPY   (idc '-' for I slices; negative QPs occur with bit depth > 8)"]
    for qp in QPS:
        lines.append(f"I - {qp}")
        for t in "PB":
            for idc in range(3):
                lines.append(f"{t} {idc} {qp}")
    return "\n".join(lines) + "\n"


def _bin_choice(rng, st, mode):
    if mode == "mps":
        return st[1]
    if mode == "lps":
        return 1 - st[1]
    if mode == "lpsheavy":
        return 1 - st[1] if rng.random() < 0.8 else st[1]
    return st[1] if rng.random() < 0.7 else 1 - st[1]


def build(name, rng, nops, init=None, ctxs=None, mix=(0.6, 0.3, 0.1), mode="mixed", pcm=False, final_terms=1,
          pick=None):
    """Encode a random op sequence.  mix = (decision, bypass, terminate-0) probabilities; ctxs = {idx: [p, mps]}."""
    ctxs = {k: list(v) for k, v in (ctxs or {}).items()}
    states = {}
    if init:
        for c, s in enumerate(R.init_contexts(*init)):
            if s is not None:
                states[c] = s
    states.update({k: list(v) for k, v in ctxs.items()})
    keys = pick or sorted(states)
    start = {k: list(v) for k, v in states.items()}
    enc = cabac_enc.Encoder()
    ops, bins = [], []
    for _ in range(nops):
        x = rng.random()
        if x < mix[0] and keys:
            c = rng.choice(keys)
            b = _bin_choice(rng, states[c], mode)
            enc.decision(states[c], b)
            ops.append(f"D {c}")
            bins.append(b)
        elif x < mix[0] + mix[1]:
            b = rng.randint(0, 1) if mode != "ones" else 1
            enc.bypass(b)
            ops.append("B")
            bins.append(b)
        elif x < mix[0] + mix[1] + mix[2]:
            enc.terminate(0)
            ops.append("T")
            bins.append(0)
        elif pcm and rng.random() < 0.5:
            n = rng.choice([0, 1, 3, 384])
            enc.terminate(1)
            enc.align_zero()
            enc.raw_bytes(bytes(rng.randrange(256) for _ in range(n)))
            enc.restart()
            ops.append("T")
            bins.append(1)
            ops.append(f"P {n}")
    for _ in range(final_terms):
        enc.terminate(1)
        ops.append("T")
        bins.append(1)
    BINS[name] = bins
    return _text(name, init, start if not init else {k: start[k] for k in ctxs}, enc.tobytes(), ops)


def _text(name, init, ctxs, data, ops):
    L = [f"VECTOR {name}"]
    if init:
        L.append(f"INIT {init[0]} {'-' if init[0] == 'I' else init[1]} {init[2]}")
    for c in sorted(ctxs):
        L.append(f"CTX {c} {ctxs[c][0]} {ctxs[c][1]}")
    h = data.hex()
    for i in range(0, len(h), 64):
        L.append("DATA " + h[i:i + 64])
    if not data:
        L.append("DATA")
    L += ops
    L.append("END")
    return "\n".join(L) + "\n"


def engine_vectors_text(seed=8101):
    rng = random.Random(seed)
    out = ["# arithmetic decoding engine vectors (grammar: Chapter 8.1 Readme, section 5.2)\n"]
    # 1. every (pStateIdx, valMPS) combination appears as the starting state of some context
    allstates = [(p, m) for p in range(63) for m in (0, 1)]   # pStateIdx 63 is never used by DecodeDecision
    for k in range(8):   # 8 x 16 contexts cover all 126 states (the last two repeat)
        ctxs = {c: list(allstates[(k * 16 + c) % 126]) for c in range(16)}
        out.append(build(f"states_{k}", rng, 700, ctxs=ctxs, mix=(0.9, 0.05, 0.05)))
    # 2. state-machine walks
    out.append(build("mps_run_to_62", rng, 400, ctxs={0: [0, 0]}, mix=(1, 0, 0), mode="mps"))
    out.append(build("lps_walk_down", rng, 120, ctxs={0: [62, 1], 1: [62, 0]}, mix=(1, 0, 0), mode="lps"))
    out.append(build("lps_heavy", rng, 600, ctxs={c: [rng.randint(0, 62), rng.randint(0, 1)] for c in range(6)},
                     mix=(1, 0, 0), mode="lpsheavy"))
    # 3. bypass
    out.append(build("bypass_random", rng, 400, mix=(0, 1, 0)))
    out.append(build("bypass_ones", rng, 80, mix=(0, 1, 0), mode="ones"))
    out.append(build("bypass_after_decisions", rng, 500, ctxs={0: [30, 0], 1: [5, 1]}, mix=(0.5, 0.5, 0)))
    # 4. terminate
    out.append(build("terminate_only", rng, 0, final_terms=1))
    out.append(build("terminate_zeros", rng, 50, mix=(0, 0, 1)))
    out.append(build("terminate_mixed", rng, 400, ctxs={0: [20, 0], 1: [40, 1]}, mix=(0.5, 0.3, 0.2)))
    # 5. I_PCM style segments: terminate bin 1, alignment, raw bytes, engine restart (contexts keep their state)
    for k in range(6):
        out.append(build(f"pcm_{k}", rng, 120 + 30 * k, ctxs={0: [rng.randint(0, 62), 1], 1: [rng.randint(0, 62), 0]},
                         mix=(0.5, 0.2, 0.2), pcm=True))
    # 6. contexts from slice parameters (9.3.1.1) + engine
    n = 0
    for t, idc, qp in [("I", 0, 0), ("I", 0, 26), ("I", 0, 51), ("I", 0, -6), ("P", 0, 26), ("P", 1, 10), ("P", 2, 40), ("B", 0, 3),
                       ("B", 1, 33), ("B", 2, 51), ("P", 2, 0), ("B", 1, -12)]:
        tab = R.init_contexts(t, idc, qp)
        ids = [c for c, s in enumerate(tab) if s is not None]
        pick = rng.sample(ids, 40)
        out.append(build(f"init_{t}{idc}_qp{qp}", rng, 400, init=(t, idc, qp), ctxs={}, mix=(0.8, 0.15, 0.05), pick=pick))
        n += 1
    # 7. long mixed run
    out.append(build("long_mixed", rng, 6000, ctxs={c: [rng.randint(0, 62), rng.randint(0, 1)] for c in range(24)},
                     mix=(0.7, 0.2, 0.1), pcm=True))
    return "".join(out)
