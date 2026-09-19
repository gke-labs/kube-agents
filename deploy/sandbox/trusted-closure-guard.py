#!/usr/bin/env python3
"""Prove the root-owned copies of the forwarded scripts are closed under import.

Run once, at image build time, over `/opt/vcs/libexec/platform`. The agent pod
crons ssh in as `hermes` and run the scripts named below; `hermes` must never
execute anything an agent-owned directory can supply, which is the rule stated
at the top of the COPY block in the Dockerfile.

`-P` and a `PYTHONPATH` of the trusted directory alone are not enough to show
that, and the comment that said they were is what this file replaces. Both
`resolver.py` and `vcs_client.py` append `/opt/defaults/scripts` and
`/opt/data/scripts` to `sys.path` at import time -- the first of those is
populated at build time and `chown agent:agent` at runtime -- so a module that
loads after them can resolve from an agent-owned directory and an import-only
check still passes. What proves the closure is where each module actually came
from, so that is what is checked: import the entry points, then read `__file__`
off everything that got loaded.

A failure here means a module in the closure was not added to the COPY list.
`test_sandbox_delivery.py` catches the same gap at review time by AST walk; this
catches the case the walk cannot see, an import that only resolves at runtime.
"""

import importlib
import os
import sys
import sysconfig

# Where the Dockerfile stages the root-owned copies. Overridable only so the
# guard can be exercised against a staging directory outside a container --
# nothing in the image sets it, and a build that did would be changing the
# directory it is asserting about, which the `chown`/`find` half of the same
# RUN would then fail on.
TRUSTED = os.environ.get("TRUSTED_CLOSURE_DIR", "/opt/vcs/libexec/platform")

# Each forwarded script and the constant naming the path its caller forwards to.
# The constant is checked as well as the closure: staging a root-owned copy that
# nothing points at buys nothing.
FORWARDED = {"forge": "SANDBOX_FORGE", "resolver": "SANDBOX_RESOLVER"}

# The interpreter's own files. Everything else a trusted process imports has to
# come out of the root-owned directory.
ALLOWED = tuple(
    sorted(
        {
            os.path.realpath(TRUSTED) + "/",
            TRUSTED + "/",
            sysconfig.get_paths()["stdlib"] + "/",
            sysconfig.get_paths()["purelib"] + "/",
            sysconfig.get_paths()["platlib"] + "/",
        }
    )
)


def main() -> int:
    failures = []
    for name, const in FORWARDED.items():
        try:
            module = importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 -- any failure is a build failure
            failures.append(f"{name} does not import from the trusted copy: {error!r}")
            continue
        want = f"{TRUSTED}/{name}.py"
        got = getattr(module, const, None)
        if got != want:
            failures.append(f"{name}.{const} is {got!r}, not {want!r}")

    for module in sorted(sys.modules.values(), key=lambda m: getattr(m, "__name__", "")):
        origin = getattr(module, "__file__", None)
        # This file is the one module that is legitimately outside: it is copied
        # in beside the directory it checks and deleted in the same layer.
        if module.__name__ == "__main__" or not origin or origin.startswith(ALLOWED):
            continue
        failures.append(f"{module.__name__} resolved from {origin}, outside {TRUSTED}")

    if failures:
        print("the trusted copy is not closed under import:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
