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
reaction calls, this patch would be granting a write scope nothing uses, and
that should fail rather than pass quietly.

``test_slack_reactions_scope.py`` covers the applier against a fixture on the
host and cannot cover any of this — the edit lives inside Hermes' own module,
and the unit suite never sees the tree that ships.

The module is loaded by path because ``/opt/hermes`` is the tree under test
rather than whatever ``hermes_cli`` happens to resolve to on ``sys.path``. That
is all it buys: ``_build_full_manifest`` imports ``hermes_cli.commands`` in its
body, so calling it imports the package anyway.
"""

from __future__ import annotations

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

#: The reaction *call sites* the scope is being granted for — the ``self.``
#: prefix is load-bearing. ``_add_reaction``/``_remove_reaction`` are the private
#: helpers that wrap ``reactions.add``/``reactions.remove``; matching their
#: ``async def`` lines, or the ``.reactions_add(`` inside them, would keep
#: passing after upstream deleted the two lifecycle hooks and left the helpers
#: defined but dead — which is the realistic way this feature dies, and exactly
#: the case this check claims to catch.
WRITE_CALLS = ("self._add_reaction(", "self._remove_reaction(")


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
    adapter = adapter_path.read_text()
    names = {call: call.rstrip("(").removeprefix("self.") for call in WRITE_CALLS}
    unused = [names[call] for call in WRITE_CALLS if call not in adapter]
    if unused:
        raise _fail(
            f"{ADAPTER} defines but no longer calls {', '.join(unused)}, so "
            f"{WRITE_SCOPE} is being granted for nothing — drop this patch "
            "instead of widening the app's permissions"
        )

    print(
        f"slack_reactions_scope verify: {WRITE_SCOPE} present in all "
        f"{len(EXPERIENCES)} emitted manifests; adapter still calls "
        f"{', '.join(names.values())}"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
