"""Measure memory pressure against the container's own cgroup, not the node.

One anchored edit in ``gateway/lifecycle_ledger.py``. ``sample_memory`` reads
``MemTotal`` and ``MemAvailable`` from ``/proc/meminfo``, which inside a
container is the node's. The kanban dispatcher's memory guard
(``hermes_cli/kanban_db_dispatch.py``, ``_memory_pressure_level``) classifies
that sample with ``gateway.memory_status.classify_pressure``: under 15% of
MemTotal available is "elevated" and limits the tick to one new worker, under
5% is "critical" and spawns none. On a GKE Autopilot node packed with other
pods, the node sits under 15% available while the agent's container uses a few
percent of its own limit, so fan-out ran one worker per tick on every lane whose
node happened to be full (gke-labs#3237).

The edit adds the container's cgroup v2 figures to the sample when a limit is
set: ``memory.max`` as the total, and ``memory.max`` minus what the cgroup holds
that reclaim cannot free as the available. Reclaimable is the file cache
(``active_file`` and ``inactive_file``) plus ``slab_reclaimable`` from
``memory.stat``, the same things ``MemAvailable`` counts as available, so
upstream's 15%/5% thresholds mean what they were set to mean, against the
container's limit instead of the node. Swap used comes from
``memory.swap.current`` when the cgroup has it. Without a limit (``memory.max``
reads ``max``) or without a readable cgroup, the sample stays the node's, as
upstream ships it.

What changes, deliberately: the guard now protects the container's own limit,
which is what kills the agent. Pressure on the node as a whole is left to the
kubelet's eviction, which acts on it already. The dashboard's memory banner and
the lifecycle ledger's OOM heuristics read the same sample, so they now describe
the container too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import patchlib

LEDGER_RELATIVE = "gateway/lifecycle_ledger.py"

# Where the image installs Hermes, when no root is given.
DEFAULT_HERMES_ROOT = "/opt/hermes"

# The def whose return gains the cgroup figures. Located by AST.
SAMPLE_DEF = "sample_memory"

# The statement the inserted line goes in front of, without its indentation.
RETURN_ANCHOR = "return sample"

MARKER = "kube-agents patch: cgroup-memory-sample"

# Called from sample_memory, defined by the trailer below.
HELPER = "_kube_cgroup_memory"

INSERTED_LINE = f"sample.update({HELPER}())  # {MARKER}"

# Appended to the module. Its names start with _kube_ so they cannot collide
# with upstream's. _KUBE_CGROUP_DIR is a module global so the verifier and the
# tests can point it at a fixture directory.
TRAILER = f'''

# {MARKER}
# The container's cgroup v2 files. Overridable for tests.
_KUBE_CGROUP_DIR = "/sys/fs/cgroup"
# memory.max's value when no limit is set.
_KUBE_CGROUP_UNLIMITED = "max"
# The memory.stat keys for memory reclaim can free before an OOM kill: the file
# cache on both LRU lists and reclaimable slab. Not "file", which counts shmem.
_KUBE_CGROUP_RECLAIMABLE_KEYS = ("active_file", "inactive_file", "slab_reclaimable")
_KUBE_BYTES_PER_KIB = 1024


def {HELPER}() -> Dict[str, int]:
    """The container's own memory (KiB) under a cgroup v2 limit: total, available, swap used.

    ``{{}}`` when ``memory.max`` is ``max`` or any file is unreadable, so the
    caller keeps the node's /proc figures. Available is the limit less what the
    cgroup holds that reclaim cannot free: ``memory.current`` minus the file
    cache and reclaimable slab in ``memory.stat``.
    """
    try:
        with open(os.path.join(_KUBE_CGROUP_DIR, "memory.max"), encoding="utf-8") as fh:
            raw_limit = fh.read().strip()
        if raw_limit == _KUBE_CGROUP_UNLIMITED:
            return {{}}
        limit = int(raw_limit)
        with open(os.path.join(_KUBE_CGROUP_DIR, "memory.current"), encoding="utf-8") as fh:
            current = int(fh.read().strip())
        reclaimable = 0
        with open(os.path.join(_KUBE_CGROUP_DIR, "memory.stat"), encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.partition(" ")
                if key in _KUBE_CGROUP_RECLAIMABLE_KEYS:
                    reclaimable += int(value)
    except (OSError, ValueError):
        return {{}}
    if limit <= 0:
        return {{}}
    held = max(0, current - reclaimable)
    figures = {{
        "mem_total_kib": limit // _KUBE_BYTES_PER_KIB,
        "mem_available_kib": max(0, limit - held) // _KUBE_BYTES_PER_KIB,
    }}
    try:
        with open(os.path.join(_KUBE_CGROUP_DIR, "memory.swap.current"), encoding="utf-8") as fh:
            figures["swap_used_kib"] = int(fh.read().strip()) // _KUBE_BYTES_PER_KIB
    except (OSError, ValueError):
        pass
    return figures
'''


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, LEDGER_RELATIVE, prefix="cgroup-memory-sample")
    patch.refuse_if_patched(MARKER)
    definition = patch.find_def(SAMPLE_DEF, label="memory sample")
    indent = definition.body_indent
    body = patch.source[definition.start : definition.end] + "\n"
    # Bounded by line breaks, so "return samples" or "return sample_x" can't
    # match. The closing break is a lookahead so two returns in a row, which
    # share it, are both counted.
    anchor = "\n" + indent + RETURN_ANCHOR
    matches = list(re.finditer(re.escape(anchor) + r"(?=\n)", body))
    found = len(matches)
    if found != 1:
        raise SystemExit(
            f"cgroup-memory-sample patch: {LEDGER_RELATIVE}: expected 1 "
            f"'{RETURN_ANCHOR}' in {SAMPLE_DEF}(), found {found}. {patch.note}"
        )
    # Just past the line break, where the return statement's line begins.
    offset = definition.start + matches[0].start() + 1
    patch.splice(offset, offset, indent + INSERTED_LINE + "\n")
    patch.append(TRAILER)
    patch.commit("sample_memory reads the container's cgroup limit when one is set")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
