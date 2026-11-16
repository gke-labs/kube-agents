#!/usr/bin/env python3
"""Build gate for the out-of-process worker toolset resolution (#3937).

Run by ``deploy/docker/Dockerfile`` after the hermes-otel plugin is installed in
``/opt/defaults``, so the profile it resolves carries the plugin set a real
profile is created with. The applier only proves the anchor matched. This
checks, on a copy of that profile in a temporary directory:

- **No plugins in this process:** the patched ``_resolve_worker_cli_toolsets``
  creates no plugin manager, imports no per-home plugin module and starts no
  thread here. This process plays the gateway.
- **Same answer:** it returns exactly what upstream's body returns for the same
  profile, computed in this same process afterwards (which does register the
  plugins, as the gateway used to).
- **Cached:** a second spawn for an unchanged profile starts no child.
- **Never stale:** a change to the profile's ``config.yaml`` that changes its
  toolsets is reflected on the next call, and a change to its plugins directory
  recomputes too.

Usage::

    python3 verify_kanban_worker_toolsets.py [PROFILE_TEMPLATE]   # default /opt/defaults
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, "/opt/hermes")

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


template = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/defaults")
root = Path(tempfile.mkdtemp())
home = root / "profiles" / "cluster-verify-worker-toolsets"
shutil.copytree(template, home, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
os.environ["HERMES_HOME"] = str(root)
os.environ.pop("KUBE_AGENTS_WORKER_TOOLSETS_INPROC", None)

from hermes_cli import plugins  # noqa: E402
from hermes_cli import kanban_db_dispatch as KD  # noqa: E402
from tools import kanban_worker_toolsets as kwt  # noqa: E402

check("the template profile carries the hermes_otel plugin", (home / "plugins" / "hermes_otel").is_dir())

children = []
_real_child = kwt._resolve_in_child


def counted_child(hermes_home):
    children.append(hermes_home)
    return _real_child(hermes_home)


kwt._resolve_in_child = counted_child

managers_before = len(plugins._plugin_managers_by_home)
threads_before = threading.active_count()
home_modules_before = {m for m in sys.modules if "__home_" in m}

patched = KD._resolve_worker_cli_toolsets(str(home))
check("the patched resolver returns a toolset list", isinstance(patched, list) and patched, repr(patched))
check("one child resolved it", len(children) == 1, repr(children))
check("no plugin manager was created in this process",
      len(plugins._plugin_managers_by_home) == managers_before,
      f"{managers_before} -> {len(plugins._plugin_managers_by_home)}")
check("no per-home plugin module was imported in this process",
      {m for m in sys.modules if "__home_" in m} == home_modules_before)
check("no thread was started in this process", threading.active_count() == threads_before,
      f"{threads_before} -> {threading.active_count()}")

again = KD._resolve_worker_cli_toolsets(str(home))
check("an unchanged profile is served from the cache", again == patched and len(children) == 1,
      f"{again!r}, children={len(children)}")

# Upstream's own answer for the same profile, in this process: the reference.
os.environ[kwt.INPROC_ENV] = "1"
upstream = KD._resolve_worker_cli_toolsets(str(home))
del os.environ[kwt.INPROC_ENV]
check("the patched answer equals upstream's", patched == upstream, f"{patched!r} != {upstream!r}")
check("upstream's path is the one that keeps a plugin manager",
      len(plugins._plugin_managers_by_home) > managers_before)

# A config change that changes the answer is picked up on the next call.
config = home / "config.yaml"
original = config.read_text(encoding="utf-8") if config.exists() else ""
narrowed = sorted(patched)[:1]
config.write_text(original + "\nplatform_toolsets:\n  cli: [" + ", ".join(narrowed) + "]\n", encoding="utf-8")
changed = KD._resolve_worker_cli_toolsets(str(home))
check("a changed config.yaml recomputes", len(children) == 2, f"children={len(children)}")
check("the recomputed answer is the new one, not the cached one", changed != patched, repr(changed))
os.environ[kwt.INPROC_ENV] = "1"
upstream_changed = KD._resolve_worker_cli_toolsets(str(home))
del os.environ[kwt.INPROC_ENV]
check("the recomputed answer equals upstream's for the changed config", changed == upstream_changed,
      f"{changed!r} != {upstream_changed!r}")

# A plugin file appearing recomputes too.
(home / "plugins" / "hermes_otel" / "verify-marker.txt").write_text("x", encoding="utf-8")
KD._resolve_worker_cli_toolsets(str(home))
check("a changed plugins directory recomputes", len(children) == 3, f"children={len(children)}")

shutil.rmtree(root, ignore_errors=True)
if FAILURES:
    print(f"verify_kanban_worker_toolsets: {len(FAILURES)} failure(s)", file=sys.stderr)
    sys.exit(1)
print("verify_kanban_worker_toolsets: ok")
