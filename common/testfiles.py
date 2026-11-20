"""Generate the Summary's test-file recipes (T1..T12) into a git-ignored cache.

    from common.testfiles import get
    path = get("T2")            # generates .cache/T2.ts on first use
Run `python3 -m common.testfiles` to list recipes, or `... T3 T9` to build some.
"""
import os
import subprocess
import sys

from .ffref import require_tools

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
SRC = "testsrc2=size=352x288:rate=25"

# id -> (extra input filter args, codec options)
RECIPES = {
    "T1": ([], "-profile:v baseline -x264-params keyint=1:partitions=none:no-deblock=1"),
    "T2": ([], "-profile:v baseline -x264-params keyint=1:partitions=i4x4:no-deblock=1"),
    "T3": ([], "-profile:v baseline -x264-params keyint=1:partitions=i4x4"),
    "T3_dm3": ([], "-profile:v baseline -x264-params keyint=1:partitions=i4x4:deblock=-3,-3"),
    "T3_dp3": ([], "-profile:v baseline -x264-params keyint=1:partitions=i4x4:deblock=3,3"),
    "T4": ([], "-profile:v baseline -x264-params keyint=1:partitions=i4x4:slices=4"),
    # (5.4) T4 without deblocking: T2's options plus four slices.  Decodable byte-exactly in Phase 5 (before Phase 6).
    "T4_nodeblock": ([], "-profile:v baseline -x264-params keyint=1:partitions=i4x4:no-deblock=1:slices=4"),
    "T5": ([], "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=none:ref=1:no-deblock=1"),
    "T6": ([], "-profile:v baseline -bf 0 -x264-params keyint=50:partitions=all:ref=4"),
    "T7": (["-vf", "fade=in:0:25"], "-profile:v main -x264-params cabac=0:bframes=0:weightp=2"),
    "T8": ([], "-profile:v main -x264-params cabac=0:bframes=2:b-pyramid=none:direct=none:weightb=0"),
    "T9_spatial": ([], "-profile:v main -x264-params cabac=0:bframes=3:direct=spatial:weightb=1:b-pyramid=normal"),
    "T9_temporal": ([], "-profile:v main -x264-params cabac=0:bframes=3:direct=temporal:weightb=1:b-pyramid=normal"),
    "T10_intra": ([], "-profile:v main -x264-params keyint=1:partitions=i4x4:no-deblock=1:cabac=1"),
    "T10_full": ([], "-profile:v main"),
    "T11_high": ([], "-profile:v high"),
    "T11_cqm": ([], "-profile:v high -x264-params cqm=jvt"),
    "T12": ([], "-profile:v high -x264-params interlaced=1:tff=1"),
    # Convenience: a generic stream with every common feature (I/P/B, CAVLC, 2 s).
    # (5.4) Main-profile intra-only CAVLC without deblocking and with x264's default partitions: a "default-style"
    # input every Phase 5 decoder can reproduce exactly.
    "default_intra": ([], "-profile:v main -x264-params cabac=0:keyint=1:no-deblock=1"),
    "default": ([], "-profile:v main -x264-params cabac=0"),
}


def get(recipe="default", force=False):
    """Return the path of the recipe's .ts file, generating it if needed."""
    if recipe not in RECIPES:
        raise KeyError(f"unknown recipe {recipe!r}; known: {', '.join(RECIPES)}")
    require_tools()
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, recipe + ".ts")
    if force or not os.path.exists(out):
        vf, opts = RECIPES[recipe]
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SRC, "-t", "2",
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", "26", *vf,
               *opts.split(), "-f", "mpegts", out + ".tmp"]
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
        if r.returncode != 0:
            raise SystemExit(f"ffmpeg failed for {recipe}:\n{r.stderr.decode()[-800:]}")
        os.replace(out + ".tmp", out)
    return out


if __name__ == "__main__":
    names = sys.argv[1:] or list(RECIPES)
    for n in names:
        print(n, get(n))
