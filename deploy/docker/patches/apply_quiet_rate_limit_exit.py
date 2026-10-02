"""Make a plain ``hermes chat -Q`` run exit 75 when the turn gave up on a rate limit.

One anchored edit in ``cli.py``'s one-shot exit block. Upstream maps a failed
turn to exit 1, and only a kanban worker (``HERMES_KANBAN_TASK`` set) whose
``failure_reason`` is ``rate_limit`` or ``billing`` exits with
``KANBAN_RATE_LIMIT_EXIT_CODE`` (75, ``EX_TEMPFAIL``) so the dispatcher can
release the card without counting a failure. The Hermes bridge
(``a2a/hermes-bridge``) runs the same CLI with no kanban task, so a turn that
exhausted its 429 retries exited 1 there and was recorded as the persona's
failure (``reason: hermes-exited-nonzero``), which is how a quota storm reds an
eval case (#2036). The bridge names exit 75 ``hermes-rate-limited`` and the
harness classes that as infrastructure; this patch is the other half, making
the exit code say what happened whatever process is listening.

The condition drops the kanban requirement and keeps the rest: a failed turn
whose ``failure_reason`` is ``rate_limit`` or ``billing`` exits 75, every other
failed turn exits 1, and a turn that did not fail exits 0. ``_exit_code`` is
assigned in the same statement upstream uses, so the patched block is
upstream's with one condition shorter; the marker is the trailing comment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

CLI_RELATIVE = "cli.py"

EXIT_ANCHOR = (
    '        if os.environ.get("HERMES_KANBAN_TASK") and result.get("failure_reason") in ("rate_limit", "billing"):\n'
)

EXIT_REPLACEMENT = (
    '        if result.get("failure_reason") in ("rate_limit", "billing"):  # kube-agents patch: quiet_rate_limit_exit\n'
)

MARKER = "kube-agents patch: quiet_rate_limit_exit"


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, CLI_RELATIVE, prefix="quiet-rate-limit-exit")
    patch.refuse_if_patched(MARKER)
    patch.substitute(EXIT_ANCHOR, EXIT_REPLACEMENT, label="rate-limit exit code")
    patch.commit("a plain -Q run exits 75 on a rate-limit failure, as a kanban worker did")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
