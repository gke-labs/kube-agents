#!/usr/bin/env python3
"""Build gate for the policy refusal exit code 77 runtime hint verification.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` with ``/opt/hermes/.venv/bin/python3``.
Asserts that Hermes Agent's runtime execution annotator (``tools.terminal_hints``)
returns ``None`` for exit code 77 on policy-refused command output, and verifies that
exit code 126 continues to produce the execution/file-permission hint.

Usage::

    cd /opt/hermes && python3 verify_policy_refusal_hint.py
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from tools import terminal_hints
    except ImportError as exc:
        print(f"verify_policy_refusal_hint: cannot import tools.terminal_hints: {exc}", file=sys.stderr)
        return 1

    cmd = "kubectl delete pod mypod"
    refusal_output = "Command blocked for security reasons.\npolicy rule: kubernetes.read-only\n"

    # 1. Exit 77 must return None (no execution / chmod hint attached)
    hint_77 = terminal_hints.annotate_failure(cmd, 77, refusal_output)
    if hint_77 is not None:
        print(
            f"verify_policy_refusal_hint: expected None for exit 77, got: {hint_77!r}",
            file=sys.stderr,
        )
        return 1

    # 2. Exit 126 must produce a hint (verifying annotator works and 126 was indeed annotated)
    hint_126 = terminal_hints.annotate_failure(cmd, 126, refusal_output)
    if not hint_126:
        print(
            "verify_policy_refusal_hint: expected non-empty hint for exit 126, got empty",
            file=sys.stderr,
        )
        return 1

    print("verify_policy_refusal_hint: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
