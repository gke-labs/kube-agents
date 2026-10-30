"""Resolve a kanban worker's CLI toolsets outside the gateway process (#3937).

Installed as ``/opt/hermes/tools/kanban_worker_toolsets.py`` and wired into
``hermes_cli/kanban_db_dispatch.py`` by ``apply_kanban_worker_toolsets.py``.

Why
---
Before every spawn the kanban dispatcher, which runs inside ``hermes gateway
run``, pins the worker's toolsets: ``_resolve_worker_cli_toolsets`` switches
``HERMES_HOME`` to the worker's profile and calls ``_get_platform_tools``. That
call reaches ``get_plugin_manager()``, which builds a ``PluginManager`` for the
profile's home, keeps it in the module-level ``_plugin_managers_by_home`` for the
life of the process, and runs the profile's plugin discovery. Every directory
plugin is imported again under a per-home module name, and its ``register()``
runs in the gateway. The cluster profiles' ``hermes_otel`` plugin starts its
OpenTelemetry providers there, with a span processor thread and a metric reader
thread each, and registers ``atexit`` flushes that keep them.

Nothing evicts the manager, so the gateway keeps about 2.9 MiB and two threads
for every distinct profile it has dispatched to since it started. Deleting the
profile frees none of it. Measured on a dev install: 100 new profiles took the
gateway from 320 to 612 MiB and from 37 to 235 threads, and deleting them left
it at 623 MiB. At 2,500 clusters this is about 7 GiB, reached by onboarding
alone. Evicting the manager afterwards wouldn't help: the plugin has no
unregister, and its threads and ``atexit`` handlers outlive the manager.

What this does
--------------
The resolution runs upstream's own code in a short-lived child process, with
the same environment as the gateway, and the gateway keeps only the result, a
short list of toolset names, per profile home. So no profile's plugins are
ever imported or registered in the gateway.

When the cached result is used, and when it is recomputed
---------------------------------------------------------
The cache is in memory only, so a new gateway process (an image upgrade, any
change to its environment, a restart) always starts empty. Within one process
an entry is reused only while the profile's inputs are unchanged, read as a
stat signature (``mtime_ns`` and size) of:

- the profile's ``config.yaml`` and ``.env``;
- every file under the profile's ``plugins/`` directory, which covers the
  plugin set and its code;
- every file in the managed scope (``HERMES_MANAGED_DIR``, else ``/etc/hermes``),
  which overrides each profile's config.

Any difference, including a file appearing or disappearing, recomputes. A
child that fails, times out or prints something unreadable yields ``None``:
no pin, which is upstream's own answer when resolution fails, and the worker
resolves its toolsets itself at startup. Only a list is cached: upstream also
answers ``None`` when it swallows an error, so a ``None`` is asked again next
time. A cached list is never served for a signature it wasn't computed for.

Cost: one child process (about a second of CPU) the first time a profile is
dispatched to in a gateway's life, and again after its inputs change.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

# Set in the child so the patched ``_resolve_worker_cli_toolsets`` runs
# upstream's body there instead of starting another child.
INPROC_ENV = "KUBE_AGENTS_WORKER_TOOLSETS_INPROC"

# A child that takes longer than this gives no pin, never a stale one.
CHILD_TIMEOUT_SECONDS = 60.0

_CHILD_PROGRAM = (
    "import json, sys\n"
    "from hermes_cli.kanban_db_dispatch import _resolve_worker_cli_toolsets\n"
    "json.dump({'toolsets': _resolve_worker_cli_toolsets(sys.argv[1])}, sys.stdout)\n"
)

_cache: dict[str, tuple[tuple, list[str]]] = {}
_lock = threading.Lock()


def _stat(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _tree(root: Optional[Path]) -> tuple:
    """(relative path, mtime_ns, size) for every file under ``root``, sorted."""
    if root is None or not root.is_dir():
        return ()
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            path = Path(dirpath) / name
            stat = _stat(path)
            if stat is not None:
                entries.append((str(path.relative_to(root)), *stat))
    return tuple(entries)


def _managed_dir() -> Optional[Path]:
    try:
        from hermes_cli.managed_scope import get_managed_dir
        return get_managed_dir()
    except Exception:
        return None


def signature(hermes_home: str) -> tuple:
    """Everything a profile's resolved toolsets depend on that can change in a running gateway."""
    home = Path(hermes_home)
    return (
        ("config", _stat(home / "config.yaml")),
        ("env", _stat(home / ".env")),
        ("plugins", _tree(home / "plugins")),
        ("managed", _tree(_managed_dir())),
    )


def _resolve_in_child(hermes_home: str) -> tuple[bool, Optional[list[str]]]:
    """(ok, toolsets) from upstream's resolver run in a child process."""
    env = dict(os.environ)
    env[INPROC_ENV] = "1"
    try:
        done = subprocess.run(
            [sys.executable, "-c", _CHILD_PROGRAM, hermes_home],
            env=env, capture_output=True, text=True, timeout=CHILD_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    if done.returncode != 0:
        return False, None
    try:
        # The program's JSON is the last line: anything a plugin printed on the way comes first.
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, None
    if not isinstance(answer, dict) or "toolsets" not in answer:
        return False, None
    toolsets = answer["toolsets"]
    if toolsets is None:
        return True, None
    if not isinstance(toolsets, list) or not all(isinstance(t, str) for t in toolsets):
        return False, None
    return True, toolsets


def resolve_worker_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """The worker's toolset pin for ``hermes_home``, resolved out of process and cached by signature."""
    if not hermes_home:
        return None
    key = signature(hermes_home)
    with _lock:
        cached = _cache.get(hermes_home)
    if cached is not None and cached[0] == key:
        return list(cached[1])
    ok, toolsets = _resolve_in_child(hermes_home)
    if not ok or toolsets is None:
        return None
    with _lock:
        _cache[hermes_home] = (key, list(toolsets))
    return toolsets


def in_child() -> bool:
    return os.environ.get(INPROC_ENV) == "1"

