#!/usr/bin/env python3
"""Build gate for the typed-evidence recorder patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_evidence_tools.py``. The applier proves only that its locator
matched once, and the Dockerfile's ``grep`` proves only that the registration
line is in the file. Neither proves the two tools reach a worker's registry
behind the worker-only gate — and ``capacity-obtainability/SKILL.md`` tells a
worker to fall back to prose when the tools are absent, so a registration that
silently misses degrades to the pre-#804 behaviour instead of failing the
build. This drives the live registry the way ``verify_kanban_worker_tools.py``
does: import the patched module, then ask the registry what it holds.

Usage::

    cd /opt/hermes && python3 verify_kanban_evidence_tools.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from tools.kanban_evidence_tools import (  # noqa: E402
    ATTACH_ARTIFACT_SCHEMA,
    RECORD_EVIDENCE_SCHEMA,
)
from tools.kanban_worker_tools import check_kanban_worker_mode  # noqa: E402

#: The two tools and the schema each must have been registered with.
EXPECTED_SCHEMAS = {
    "record_evidence": RECORD_EVIDENCE_SCHEMA,
    "attach_artifact": ATTACH_ARTIFACT_SCHEMA,
}
TOOLSET = "kanban"

print("live registry:")

# Importing the patched module runs upstream's registration loop and the
# block the applier inserted above it.
import tools.kanban_tools  # noqa: E402,F401
from tools.registry import registry  # noqa: E402

for tool, schema in EXPECTED_SCHEMAS.items():
    entry = registry.get_entry(tool)
    check(f"{tool} is registered", entry is not None, "missing from the registry")
    if entry is None:
        continue
    check(
        f"{tool} gates with the worker-only gate",
        entry.check_fn is check_kanban_worker_mode,
        f"check_fn={entry.check_fn!r}",
    )
    if hasattr(entry, "toolset"):
        check(
            f"{tool} sits in the {TOOLSET} toolset",
            entry.toolset == TOOLSET,
            f"toolset={entry.toolset!r}",
        )
    if hasattr(entry, "schema"):
        check(
            f"{tool} carries its own schema",
            entry.schema is schema or (entry.schema or {}).get("name") == tool,
            f"schema name={(entry.schema or {}).get('name')!r}",
        )

if FAILURES:
    print(f"verify_kanban_evidence_tools: {len(FAILURES)} check(s) failed:")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("verify_kanban_evidence_tools: all checks passed")
