#!/usr/bin/env python3
"""The install's earned-autonomy switches, each "off" unless an operator turned it on.

An operator sets `KUBEAGENTS_<NAME>` in the PlatformAgent's spec.deployment.env.
The operator passes it to the agent container through its allowlist, and to the
shell sandbox normalised to a known value, where deploy/sandbox/entrypoint.sh
writes it to a root-owned file (shell_sandbox_manifests.go, autonomySwitchEnv).
The file is read first, as audit_report.py reads the audits' switch: a session
can export a variable in one word, and the file is what the operator's choice
rests on. The agent pod has no such file, so there the container's variable is
the answer.

Any value a switch does not know, unset included, reads as "off".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

OFF = "off"
# Each switch's values besides "off".
VALUES = {
    "POST_MERGE_CHECK": ("silent", "report"),
    "AUTOMERGE": ("practice", "on"),
    "DIAL": ("on",),
}
SWITCH_DIR = Path("/run/kube-agents-sandbox")


def _file(name: str) -> Path:
    return SWITCH_DIR / name.lower().replace("_", "-")


def switch(name: str) -> str:
    """The lowercase value of `KUBEAGENTS_<name>`: the sandbox's root-owned file first, then the variable."""
    known = VALUES.get(name)
    if known is None:
        return OFF
    try:
        value = _file(name).read_text(encoding="utf-8")
    except OSError:
        value = os.environ.get(f"KUBEAGENTS_{name}", "")
    value = value.strip().lower()
    return value if value in known else OFF


def main(argv: list[str]) -> int:
    """Print each switch as NAME=value, for a human checking an install."""
    for name in argv or sorted(VALUES):
        print(f"KUBEAGENTS_{name}={switch(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
