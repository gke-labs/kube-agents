#!/usr/bin/env python3
"""Build-time gate for the kanban-dispatch-failures patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_kanban_dispatch_failures.py``, from ``/opt/hermes`` so the
gateway package imports. Two things are checked:

1. The inserted statements. ``gateway/kanban_watchers_dispatcher.py`` carries
   the two recording calls, and every name each reads is bound where it runs
   (``patchlib.unbound``): the anchors pin a ``return`` line and a log line,
   not ``slug`` or ``exc``, so an upstream rename of one of those would pass
   the applier and raise ``NameError`` on every tick.
2. The helper itself. ``gateway.kanban_dispatch_failures`` imports from the
   built tree, and ``record_tick`` neither raises nor records when the plugins
   directory holds no ``chat_metrics``, and records the board and the
   exception's class (``""`` for a good tick) on a registry stand-in when one
   is reachable.

Usage::

    cd /opt/hermes && python3 /tmp/message-surface/verify_kanban_dispatch_failures.py [HERMES_ROOT]
"""

from __future__ import annotations

import ast
import importlib
import sys
import tempfile
from pathlib import Path

import patchlib

MARKER = "kube-agents patch: kanban-dispatch-failures"
ALIAS = "_kage_dispatch_failures"
DEFAULT_HERMES_ROOT = "/opt/hermes"
DISPATCHER = "gateway/kanban_watchers_dispatcher.py"
# The good tick's call and the failed tick's call.
EXPECTED_INSERTS = 2
HELPER_MODULE = "gateway.kanban_dispatch_failures"
LOOKUP_MODULE = "gateway.chat_turn_metrics"
PLUGIN_PACKAGE = "chat_metrics"
PLUGIN_MODULE = "metrics"
PLUGIN_PACKAGE_INIT = "__init__.py"
# A registry stand-in with the one method the helper calls, recording what it saw.
REGISTRY_STAND_IN = (
    "class _R:\n"
    "    def __init__(self):\n"
    "        self.seen = []\n"
    "    def record_dispatch_tick(self, board, error_class):\n"
    "        self.seen.append((board, error_class))\n"
    "REGISTRY = _R()\n"
)
BOARD = "default"
FAILURE_CLASS = "OperationalError"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"kanban-dispatch-failures verify: {detail}")


def _reads_alias(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == ALIAS for n in ast.walk(node))


def check_inserts(root: Path) -> None:
    source = (root / DISPATCHER).read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    inserts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and not isinstance(node, (ast.Import, ast.ImportFrom))
        and MARKER in lines[node.lineno - 1]
        and _reads_alias(node)
    ]
    if len(inserts) != EXPECTED_INSERTS:
        raise _fail(f"{DISPATCHER} has {len(inserts)} inserted statements, expected {EXPECTED_INSERTS}")
    for node in inserts:
        unbound = patchlib.unbound(tree, node)
        if unbound:
            raise _fail(
                f"{DISPATCHER}:{node.lineno} reads {', '.join(unbound)}, which nothing binds there; "
                "an upstream rename the anchor did not cover"
            )
    print("kanban-dispatch-failures verify: every inserted statement reads only bound names")


def _reset(lookup, plugins_dir: str) -> None:
    lookup.DEFAULT_PLUGINS_DIR = plugins_dir
    lookup._registry = None
    lookup._lookup_failed = False


def check_helper(root: Path) -> None:
    sys.path.insert(0, str(root))
    helper = importlib.import_module(HELPER_MODULE)
    lookup = importlib.import_module(LOOKUP_MODULE)
    with tempfile.TemporaryDirectory() as empty:
        _reset(lookup, empty)
        helper.record_tick(BOARD, OSError())
        if lookup._registry is not None:
            raise _fail("record_tick found a registry in an empty plugins directory")
    with tempfile.TemporaryDirectory() as plugins:
        pkg = Path(plugins) / PLUGIN_PACKAGE
        pkg.mkdir()
        (pkg / PLUGIN_PACKAGE_INIT).write_text("", encoding="utf-8")
        (pkg / f"{PLUGIN_MODULE}.py").write_text(REGISTRY_STAND_IN, encoding="utf-8")
        sys.modules.pop(PLUGIN_PACKAGE, None)
        sys.modules.pop(f"{PLUGIN_PACKAGE}.{PLUGIN_MODULE}", None)
        _reset(lookup, plugins)
        failure = type(FAILURE_CLASS, (Exception,), {})()
        helper.record_tick(BOARD, failure)
        helper.record_tick(BOARD)
        seen = getattr(lookup._registry, "seen", None)
        want = [(BOARD, FAILURE_CLASS), (BOARD, "")]
        if seen != want:
            raise _fail(f"record_tick recorded {seen!r}, expected {want!r}")
    print("kanban-dispatch-failures verify: the helper records on a reachable registry and nothing without one")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT)
    check_inserts(root)
    check_helper(root)


if __name__ == "__main__":
    main()
