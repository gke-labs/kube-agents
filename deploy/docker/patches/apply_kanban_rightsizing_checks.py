"""Wire tools/kanban_rightsizing_checks.py into the kanban_complete gate.

``tools/kanban_rightsizing_checks.py`` puts its rule first in
``tools.kanban_verified_claims.RULES`` when it is imported, so the edit here is
that import and the call that adds its ``rightsizing`` parameter to
``KANBAN_COMPLETE_SCHEMA``, placed right after the verified-claims patch's
preamble in ``tools/kanban_tools.py``. Run after ``apply_kanban_verified_claims.py``. Usage::

    python3 apply_kanban_rightsizing_checks.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

KANBAN_RELATIVE = "tools/kanban_tools.py"

#: Text only a patched file holds.
MARKER = "# kube-agents patch: see tools/kanban_rightsizing_checks.py"

#: The last line of the verified-claims preamble; the import goes after it.
ANCHOR = "_apply_verified_claims_schema(KANBAN_COMPLETE_SCHEMA)\n"

REPLACEMENT = (
    f"{ANCHOR}"
    f"{MARKER}\n"
    "from tools import kanban_rightsizing_checks as _kanban_rightsizing_checks  # noqa: E402 - puts its rule first in RULES\n"
    "_kanban_rightsizing_checks.apply_schema(KANBAN_COMPLETE_SCHEMA)\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    kanban = patchlib.Patch(root, KANBAN_RELATIVE, prefix="kanban_rightsizing_checks")
    kanban.refuse_if_patched(MARKER)
    kanban.substitute(ANCHOR, REPLACEMENT, label="verified-claims preamble")
    kanban.commit("1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
