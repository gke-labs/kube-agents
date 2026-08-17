"""The readiness probe of ../agent-process-supervisor.md 3.4.

Reads the supervisor's status file and exits 0 for Ready, 1 for NotReady.
Fails on staleness as well as on content: a supervisor whose loop has wedged
stops rewriting the file, and that must not read as healthy.

    probe.py <status-file> [stale-after-seconds]
"""

import json
import sys
import time

DEFAULT_STALE_AFTER = 3.0  # 3 x the poll interval


def main(argv):
    if len(argv) < 2:
        print("usage: probe.py <status-file> [stale-after-seconds]", file=sys.stderr)
        return 2
    path = argv[1]
    stale_after = float(argv[2]) if len(argv) > 2 else DEFAULT_STALE_AFTER

    try:
        with open(path) as fh:
            status = json.load(fh)
    except Exception as exc:  # missing, truncated, mid-rename
        print(f"probe: unreadable status ({exc})")
        return 1

    age = time.time() - status["updated_at"]
    if age > stale_after:
        print(f"probe: STALE by {age:.1f}s -- the supervisor loop is wedged")
        return 1

    flag = "degraded" if status.get("degraded") else "healthy"
    print(f"probe: role={status['role']} ready={status['ready']} {flag} (age {age:.1f}s)")
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
