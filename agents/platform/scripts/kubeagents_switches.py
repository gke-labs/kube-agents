#!/usr/bin/env python3
"""The install's earned-autonomy switches, each "off" unless an operator turned it on.

An operator sets `KUBEAGENTS_<NAME>` in the PlatformAgent's spec.deployment.env,
and the operator delivers it, normalised to a value the switch knows, else "off",
to one operator-owned file per pod:

- the agent pod: the managed .env, a read-only ConfigMap mount that pins every
  switch on every render (platformagent_manifests.go, renderManagedEnvForPlugins);
- the shell sandbox: the root-owned file deploy/sandbox/entrypoint.sh writes
  under /run/kube-agents-sandbox (shell_sandbox_manifests.go, autonomySwitchEnv).

Both are read before the variable, which a session can export and the agent can
set with a line in the PVC .env that Hermes applies over the container env. The
managed .env comes first, and where it exists it is the only answer: a key it
lacks (an operator older than the switches) reads as "off", because every other
source in the agent pod is one the agent can write. Not every agent-pod process
has the managed values in its environment either: session_kv_server starts
before Hermes loads any .env.

The variable is the answer only where neither file exists, as in a checkout or
the credential broker, which no feature should ask.
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
MANAGED_DOTENV_PATH = Path("/etc/hermes/.env")


def _file(name: str) -> Path:
    return SWITCH_DIR / name.lower().replace("_", "-")


def _managed(name: str) -> str | None:
    """The managed .env's value for `KUBEAGENTS_<name>`, "" when it lacks the key, None without the file."""
    try:
        lines = MANAGED_DOTENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    prefix = f"KUBEAGENTS_{name}="
    values = [line[len(prefix) :] for line in lines if line.startswith(prefix)]
    return values[-1] if values else ""


def switch(name: str) -> str:
    """The lowercase value of `KUBEAGENTS_<name>`: the managed .env, else the sandbox's file, else the variable."""
    known = VALUES.get(name)
    if known is None:
        return OFF
    value = _managed(name)
    if value is None:
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
