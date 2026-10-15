#!/usr/bin/env python3
"""Build gate for the cgroup memory-sample patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_cgroup_memory_sample.py``. The applier proves its anchor matched once;
this proves the patched ``sample_memory`` and the dispatcher's classifier agree
on what a container under a limit is doing. The cgroup is a fixture directory,
so the result does not depend on the build machine's own cgroup.

- Light use of an 8 GiB limit classifies ``ok``, whatever the node reports.
- 94% of the limit held classifies ``elevated``, and 98% ``critical``: the
  guard still stops spawning when the container itself is near its limit.
- Reclaimable page cache (``inactive_file``) does not count as held.
- No limit (``memory.max`` reads ``max``) or no cgroup leaves the node's
  /proc figures, as upstream ships them.

Usage::

    cd /opt/hermes && python3 verify_cgroup_memory_sample.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from gateway import lifecycle_ledger
from gateway.memory_status import classify_pressure

GIB = 1024**3
LIMIT = 8 * GIB

# (case, memory.max, memory.current, inactive_file, expected level)
CASES = (
    ("light use", str(LIMIT), int(0.7 * GIB), int(0.1 * GIB), "ok"),
    ("94% held", str(LIMIT), int(0.94 * LIMIT), 0, "elevated"),
    ("98% held", str(LIMIT), int(0.98 * LIMIT), 0, "critical"),
    ("98% current, mostly reclaimable", str(LIMIT), int(0.98 * LIMIT), int(0.5 * LIMIT), "ok"),
)


def _write(cgroup: Path, limit: str, current: int, inactive: int) -> None:
    (cgroup / "memory.max").write_text(limit + "\n")
    (cgroup / "memory.current").write_text(f"{current}\n")
    (cgroup / "memory.stat").write_text(f"anon 1\nfile 2\ninactive_file {inactive}\nactive_file 3\n")


def main() -> int:
    failures = []
    node = lifecycle_ledger._proc_fields("/proc/meminfo", {"MemTotal": "t", "MemAvailable": "a"})
    with tempfile.TemporaryDirectory() as tmp:
        cgroup = Path(tmp)
        lifecycle_ledger._KUBE_CGROUP_DIR = str(cgroup)
        for name, limit, current, inactive, want in CASES:
            _write(cgroup, limit, current, inactive)
            sample = lifecycle_ledger.sample_memory()
            got = classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
            if sample.get("mem_total_kib") != LIMIT // 1024 or got != want:
                failures.append(f"{name}: total={sample.get('mem_total_kib')} level={got}, wanted {want}")
        _write(cgroup, "max", GIB, 0)
        unlimited = lifecycle_ledger.sample_memory()
        if node and unlimited.get("mem_total_kib") != node.get("t"):
            failures.append(f"memory.max=max: total={unlimited.get('mem_total_kib')}, wanted the node's {node.get('t')}")
        lifecycle_ledger._KUBE_CGROUP_DIR = str(cgroup / "absent")
        absent = lifecycle_ledger.sample_memory()
        if node and absent.get("mem_total_kib") != node.get("t"):
            failures.append(f"no cgroup: total={absent.get('mem_total_kib')}, wanted the node's {node.get('t')}")
    if failures:
        print("cgroup-memory-sample verify FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"cgroup-memory-sample verify: {len(CASES)} limit cases classified, unlimited and absent cgroups fall back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
