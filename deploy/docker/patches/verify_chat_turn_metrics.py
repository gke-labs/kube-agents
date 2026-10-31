#!/usr/bin/env python3
"""Build-time gate for the chat-turn-metrics patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_chat_turn_metrics.py``, from ``/opt/hermes`` so the gateway
package imports. Two things are checked:

1. The inserted statements. Each file carries the expected number of inserts
   (``run_turn.py``: the count in the error reply and the guarded count before
   the delivery; ``run_turn_runner.py``: the count at the provider
   authentication failure), and every name an insert reads is bound where it
   runs (``patchlib.unbound``): the anchors pin a comment and a ``return``
   line, not ``source``, ``agent_result`` or ``ctx``, so an upstream rename of
   one of those would pass the applier and raise ``NameError`` on every failed
   turn, inside the handler that builds the user's error reply.
2. The helper itself. ``gateway.chat_turn_metrics`` imports from the built
   tree, and ``record_turn_error`` neither raises nor counts when the plugins
   directory holds no ``chat_metrics`` (the gateway must behave as upstream
   ships it without the plugin), and counts under the right platform label on
   a registry stand-in when one is reachable.

Usage::

    cd /opt/hermes && python3 /tmp/message-surface/verify_chat_turn_metrics.py [HERMES_ROOT]
"""

from __future__ import annotations

import ast
import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import patchlib

MARKER = "kube-agents patch: chat-turn-metrics"
ALIAS = "_kage_chat_turn_metrics"
DEFAULT_HERMES_ROOT = "/opt/hermes"
# Inserted statements per file: the call in the error reply and the guarded
# `if` before the delivery; the call at the provider authentication failure.
EXPECTED_INSERTS = {"gateway/run_turn.py": 2, "gateway/run_turn_runner.py": 1}
# The helper as the patched gateway imports it, and the plugin module it reaches
# for the registry: the package directory under the plugins root and its module.
HELPER_MODULE = "gateway.chat_turn_metrics"
PLUGIN_PACKAGE = "chat_metrics"
PLUGIN_MODULE = "metrics"
PLUGIN_PACKAGE_INIT = "__init__.py"
# A registry stand-in with the one method the helper calls, recording the platforms it saw.
REGISTRY_STAND_IN = (
    "class _R:\n"
    "    def __init__(self):\n"
    "        self.seen = []\n"
    "    def record_turn_error(self, platform):\n"
    "        self.seen.append(platform)\n"
    "REGISTRY = _R()\n"
)
# One source with an enum-shaped platform and one with a bare string, in the order they are recorded.
PLATFORM_ENUM = "slack"
PLATFORM_BARE = "google_chat"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"chat-turn-metrics verify: {detail}")


def _reads_alias(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == ALIAS for n in ast.walk(node))


def _inserts(tree: ast.Module, lines: list[str]) -> list[ast.stmt]:
    """The statements the applier inserted: marked on their first line, and the
    one `if` whose body is the marked count."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        marked = MARKER in lines[node.lineno - 1]
        if marked and _reads_alias(node):
            found.append(node)
    return found


def check_inserts(root: Path) -> None:
    for relative, expected in EXPECTED_INSERTS.items():
        source = (root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        inserts = _inserts(tree, lines)
        if len(inserts) != expected:
            raise _fail(f"{relative} has {len(inserts)} inserted statements, expected {expected}")
        for node in inserts:
            unbound = patchlib.unbound(tree, node)
            if unbound:
                raise _fail(
                    f"{relative}:{node.lineno} reads {', '.join(unbound)}, which nothing binds there; "
                    "an upstream rename the anchor did not cover"
                )
    print("chat-turn-metrics verify: every inserted statement reads only bound names")


def check_helper(root: Path) -> None:
    sys.path.insert(0, str(root))
    helper = importlib.import_module(HELPER_MODULE)
    with tempfile.TemporaryDirectory() as empty:
        helper.DEFAULT_PLUGINS_DIR = empty
        helper._registry = None
        helper._lookup_failed = False
        helper.record_turn_error(SimpleNamespace(platform=SimpleNamespace(value=PLATFORM_ENUM)))
        if helper._registry is not None:
            raise _fail("record_turn_error found a registry in an empty plugins directory")
    # A registry stand-in in a plugins directory of its own: the helper imports
    # chat_metrics.metrics from there and counts under the platform's name.
    with tempfile.TemporaryDirectory() as plugins:
        pkg = Path(plugins) / PLUGIN_PACKAGE
        pkg.mkdir()
        (pkg / PLUGIN_PACKAGE_INIT).write_text("", encoding="utf-8")
        (pkg / f"{PLUGIN_MODULE}.py").write_text(REGISTRY_STAND_IN, encoding="utf-8")
        sys.modules.pop(PLUGIN_PACKAGE, None)
        sys.modules.pop(f"{PLUGIN_PACKAGE}.{PLUGIN_MODULE}", None)
        helper.DEFAULT_PLUGINS_DIR = plugins
        helper._registry = None
        helper._lookup_failed = False
        helper.record_turn_error(SimpleNamespace(platform=SimpleNamespace(value=PLATFORM_ENUM)))
        helper.record_turn_error(SimpleNamespace(platform=PLATFORM_BARE))
        seen = getattr(helper._registry, "seen", None)
        if seen != [PLATFORM_ENUM, PLATFORM_BARE]:
            raise _fail(f"record_turn_error counted {seen!r}, expected {[PLATFORM_ENUM, PLATFORM_BARE]!r}")
    print("chat-turn-metrics verify: the helper counts on a reachable registry and nothing without one")


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT)
    check_inserts(root)
    check_helper(root)


if __name__ == "__main__":
    main()
