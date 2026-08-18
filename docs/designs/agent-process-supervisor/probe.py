"""The readiness and liveness probes of ../agent-process-supervisor.md 3.4.

Reads the supervisor's one-line `ready` file and exits 0 for pass, 1 for fail.
Both probes fail on staleness: a supervisor whose loop has wedged stops
rewriting the file, and that must not read as healthy.

They differ in exactly one thing, and it is the point of there being two:

  readiness  staleness AND the ready flag
  liveness   staleness ONLY

A liveness probe that also failed on the flag would restart the container
whenever the required process was down -- which is 3.3's decision to make, with
backoff and a cap in front of it, not a probe's.

The shipped probes are four lines of POSIX sh over this same file; 3.4 says why
spawning an interpreter nine times a minute under gVisor is the wrong shape.
This is the same logic in Python so the experiments can drive it.

    probe.py <ready-file> [stale-after-seconds] [--liveness]
"""

import sys
import time

# design: 30 s. Not a multiple of the poll interval -- 3.4 sizes it off the
# slowest LEGITIMATE iteration, which 3.5 bounds at ~9 s once every lease call
# carries a timeout. Scaled down here, where POLL is 1 s and there is no API call.
DEFAULT_STALE_AFTER = 3.0


def main(argv):
    argv = list(argv)
    liveness = "--liveness" in argv
    if liveness:
        argv.remove("--liveness")
    if len(argv) < 2:
        print("usage: probe.py <ready-file> [stale-after-seconds] [--liveness]", file=sys.stderr)
        return 2
    path = argv[1]
    stale_after = float(argv[2]) if len(argv) > 2 else DEFAULT_STALE_AFTER
    kind = "liveness" if liveness else "readiness"

    try:
        with open(path) as fh:
            ts_s, ready_s = fh.read().split()
        ts, ready = int(ts_s), ready_s == "1"
    except Exception as exc:  # missing, truncated, mid-rename
        print(f"probe[{kind}]: unreadable ({exc})")
        return 1

    age = time.time() - ts
    if age > stale_after:
        print(f"probe[{kind}]: STALE by {age:.1f}s -- the supervisor loop is wedged")
        return 1

    if liveness:
        print(f"probe[liveness]: loop alive (age {age:.1f}s); ready={ready} ignored")
        return 0

    print(f"probe[readiness]: ready={ready} (age {age:.1f}s)")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
