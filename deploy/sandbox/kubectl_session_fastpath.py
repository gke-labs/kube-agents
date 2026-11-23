#!/usr/bin/env python3
"""Print, per cost tier, the smallest memory limit at which the session admission refuses nothing.

On a session pod (KUBECTL_ADMIT_ON_BUSY=run) the kubectl admission's only refusal is a read
larger than one read may ever take: cost() above the guard's watermark less SHELL_FLOOR_BYTES.
On a pod whose limit is at least the figure printed here for its tier, and not on gVisor, no
argv on that tier can be refused, so deploy/sandbox/session-kubectl.sh runs kubectl directly
for every command. a2a/Dockerfile.worker runs this at build time:

    python3 -I kubectl_session_fastpath.py <kubectl-admit> <shell-memory-guard> <kubectl-costs.json>

One line per tier, `<tier> <min_limit_bytes>`, tiers sorted. The bound is the admission's own
max_cost() and the watermark the guard's own high_watermark(), both loaded from the given paths.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys


def load(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def min_limit(high_watermark, floor: int, bound: int) -> int:
    """The smallest limit L with high_watermark(L) - floor >= bound (the watermark never falls as L grows)."""
    lo, hi = 0, 1
    while high_watermark(hi) - floor < bound:
        lo, hi = hi, hi * 2
    while hi - lo > 1:  # high_watermark(lo) falls short (or lo is 0), high_watermark(hi) suffices
        mid = (lo + hi) // 2
        if high_watermark(mid) - floor >= bound:
            hi = mid
        else:
            lo = mid
    return hi


def lines(admit, guard, table: dict) -> list[str]:
    return [f"{tier} {min_limit(guard.high_watermark, admit.SHELL_FLOOR_BYTES, admit.max_cost(table, tier))}"
            for tier in sorted(table.get("tiers", {}))]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: kubectl_session_fastpath.py <kubectl-admit> <shell-memory-guard> <kubectl-costs.json>\n")
        return 2
    admit = load("kubectl_admit", argv[0])
    guard = load("shell_memory_guard", argv[1])
    with open(argv[2], encoding="utf-8") as f:
        table = json.load(f)
    sys.stdout.write("".join(f"{line}\n" for line in lines(admit, guard, table)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
