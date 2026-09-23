#!/usr/bin/env python3
"""Build-time behaviour gate for the Slack reactions-scope patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_reactions_scope.py``. The applier proves the
list was located and the file still parses; this proves the manifest the shipped
CLI actually *prints* carries the scope.

A grep for the inserted line would not establish that. ``_build_full_manifest``
appends to ``bot_scopes`` on two of its three ``messaging_experience`` branches
and sorts the list before emitting it, so the element being present in the
source is a claim about the source, not about the JSON an operator pastes into
Slack. This calls the real function once per branch and reads the scope out of
the manifest it returns.

Every failure this gate exists to catch is silent in production. A missing
``reactions:write`` does not raise, does not log above ``debug``, and does not
interrupt the turn — the user just never sees the 👀, which is indistinguishable
from the agent being slow. The build is the last place it can be caught loudly.

The adapter is checked too, in the other direction: if upstream ever drops the
reaction calls from the two lifecycle hooks, this patch would be granting a
write scope nothing uses, and that should fail rather than pass quietly.

``test_slack_reactions_scope.py`` covers the applier against a fixture on the
host and cannot cover any of this — the edit lives inside Hermes' own module,
and the unit suite never sees the tree that ships.

The module is loaded by path because ``/opt/hermes`` is the tree under test
rather than whatever ``hermes_cli`` happens to resolve to on ``sys.path``. That
is all it buys: ``_build_full_manifest`` imports ``hermes_cli.commands`` in its
body, so calling it imports the package anyway.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

MANIFEST = "hermes_cli/slack_cli.py"
ADAPTER = "plugins/platforms/slack/adapter.py"

WRITE_SCOPE = "reactions:write"

#: Every messaging experience ``_build_full_manifest`` can emit. ``bot_scopes``
#: is appended to on the ``assistant`` and ``agent`` branches, so each is a
#: separate chance for the emitted list to differ from the source literal.
EXPERIENCES = ("assistant", "agent", "none")

#: The lifecycle hooks that spend the scope: 👀 on pickup, ✅/❌ on the outcome.
#: Each must still call one of the reaction helpers on ``self``. The check is on
#: the hooks' bodies rather than on a substring of the file because the helpers
#: outlive their callers: at v2026.9.14 ``_add_reaction``/``_remove_reaction``
#: are defined and dead, thin wrappers over ``_react(..., remove=...)`` that the
#: hooks call directly. A substring match on either spelling would keep passing
#: after upstream deleted the hooks and left the helpers behind — which is the
#: realistic way this feature dies, and exactly the case this check claims to
#: catch.
REACTING_HOOKS = ("on_processing_start", "on_processing_complete")

#: The methods that wrap ``reactions.add``/``reactions.remove``, in both the
#: v2026.8.19 spelling (``_add_reaction``/``_remove_reaction``) and the
#: v2026.9.14 one (``_react``).
REACTION_HELPERS = frozenset({"_react", "_add_reaction", "_remove_reaction"})


def _fail(detail: str) -> "SystemExit":
    return SystemExit(f"slack_reactions_scope verify: {detail}")


def _load(root: Path):
    path = root / MANIFEST
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_cli_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(root: Path = Path("/opt/hermes")) -> None:
    module = _load(root)

    for experience in EXPERIENCES:
        manifest = module._build_full_manifest(
            "Hermes", "reactions-scope verify", messaging_experience=experience
        )
        scopes = manifest["oauth_config"]["scopes"]["bot"]
        count = scopes.count(WRITE_SCOPE)
        if count != 1:
            raise _fail(
                f"messaging_experience={experience!r} emits bot scopes {scopes!r}, "
                f"carrying {WRITE_SCOPE!r} {count} time(s) rather than once"
            )

    adapter_path = root / ADAPTER
    if not adapter_path.is_file():
        raise _fail(f"{adapter_path} does not exist")
    hooks = {
        node.name: node
        for node in ast.walk(ast.parse(adapter_path.read_text()))
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name in REACTING_HOOKS
    }
    called: dict[str, set[str]] = {}
    for hook in REACTING_HOOKS:
        node = hooks.get(hook)
        called[hook] = set() if node is None else {
            call.func.attr
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr in REACTION_HELPERS
        }
    silent = [hook for hook in REACTING_HOOKS if not called[hook]]
    if silent:
        raise _fail(
            f"{ADAPTER} no longer reacts from {', '.join(silent)} (no call to "
            f"{', '.join(sorted(REACTION_HELPERS))} on self), so {WRITE_SCOPE} "
            "is being granted for nothing — drop this patch instead of "
            "widening the app's permissions"
        )

    print(
        f"slack_reactions_scope verify: {WRITE_SCOPE} present in all "
        f"{len(EXPERIENCES)} emitted manifests; adapter still reacts from "
        + ", ".join(f"{hook} via {'/'.join(sorted(called[hook]))}" for hook in REACTING_HOOKS)
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
