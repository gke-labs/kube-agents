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

The second thing checked is `sys.path` itself, after everything has loaded.
The trusted copies drop `/opt/defaults/scripts` and `/opt/data/scripts` from
the path when they find themselves under the trusted directory; a module that
put either back -- under any spelling -- would leave a uid-1000-writable
directory for a *deferred* import to resolve from, which the `__file__` walk
above cannot see because that import has not happened yet.

A failure here means a module in the closure was not added to the COPY list,
or one of them reaches back out to an agent-owned directory.
`test_sandbox_delivery.py` checks both at review time, by loading the staged
modules as the trusted copy on a tree nobody has to build; this is the same
check where the files really are.
"""

import importlib
import os
import sys
import sysconfig

# Where the Dockerfile stages the root-owned copies, and not overridable. An
# earlier shape read a `TRUSTED_CLOSURE_DIR` out of the environment so the
# guard could be pointed at a staging directory outside a container; it could
# never have passed there. The trusted copies drop `/opt/defaults/scripts` and
# `/opt/data/scripts` from `sys.path` only when they find themselves under this
# literal path, so under any other directory they leave both on it and the
# `AGENT_WRITABLE` check below fails on modules that are behaving correctly.
# `test_sandbox_delivery.py` is the check that runs outside a container, and it
# loads the staged modules itself rather than through this file.
TRUSTED = IN_THE_IMAGE = "/opt/vcs/libexec/platform"

# The directories the entrypoint chowns to `agent`. A trusted process must not
# carry either on its import path, even behind site-packages.
AGENT_WRITABLE = ("/opt/data", "/opt/defaults")

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
        want = f"{IN_THE_IMAGE}/{name}.py"
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
    for entry in sys.path:
        if entry.startswith(AGENT_WRITABLE):
            failures.append(f"{entry} is on sys.path after the trusted copies loaded")

    if failures:
        print("the trusted copy is not closed under import:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
