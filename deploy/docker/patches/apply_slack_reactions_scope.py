#!/usr/bin/env python3
"""Grant the generated Slack app the scope its own reaction calls need.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``hermes_cli/slack_cli.py``.

The bug
-------
``_build_full_manifest`` is what ``hermes slack manifest`` prints, and that JSON
is the only description of the Slack app an installer ever writes — the operator
pastes it into Slack and clicks Save, so a scope the manifest omits is a scope
the app does not have. Its ``bot_scopes`` list asks for ``reactions:read`` and
stops there.

The adapter calls the *write* half of the API in four places
(``plugins/platforms/slack/adapter.py``)::

    on_processing_start     -> _add_reaction(channel, ts, "eyes")
    on_processing_complete  -> _remove_reaction(channel, ts, "eyes")
                            -> _add_reaction(channel, ts, "white_check_mark")
                            -> _add_reaction(channel, ts, "x")

``reactions.add`` and ``reactions.remove`` both require ``reactions:write``.
Without it Slack rejects every one of them with ``missing_scope``, so the 👀
that says the agent picked the message up, and the ✅/❌ that says how the turn
ended, never appear.

Why this has gone unnoticed
---------------------------
The failure is swallowed deliberately. ``_add_reaction`` ends::

    except Exception as e:
        # Don't log as error — may fail if already reacted or missing scope
        logger.debug("[Slack] reactions.add failed (%s): %s", emoji, e)
        return False

which folds the one condition that makes the feature permanently dead in with
the benign one, and logs it below the default level. Nothing above ``debug`` is
emitted, neither caller inspects the ``False``, and the turn otherwise succeeds
— the user simply never sees a reaction and has no reason to read it as a
misconfiguration. ``_reactions_enabled()`` reads ``SLACK_REACTIONS`` defaulting
to ``"true"``, so the feature is enabled on every install and functions on none.

The manifest is self-inconsistent here rather than merely incomplete: it already
subscribes to the ``reaction_added`` and ``reaction_removed`` bot events and
asks for ``reactions:read``. The app it describes is built to hear about
reactions and not to make any.

The fix
-------
Add ``reactions:write`` to ``bot_scopes``. ``bot_scopes.sort()`` runs further
down the same function, so this element's position is cosmetic as far as the
emitted manifest goes; it is inserted next to ``reactions:read`` to keep the
source list alphabetical as well.

The list is located with ``find_assign`` rather than pinned by a literal anchor
spelling out its contents. Upstream edits this list — ``reactions:read`` itself
is absent in v2026.7.20 and present from v2026.8.3 — and a contents anchor would
fail the build on any scope change, none of which have anything to do with this
patch. What is
asserted instead is what the patch actually reasons about: that ``bot_scopes``
is still a list literal, and that it still carries ``reactions:read``. That
element is the marker that upstream still intends to support reactions at all,
and if it ever goes, this patch should fail loudly rather than quietly grant a
write scope to an app that no longer reacts.

Upstream: not reported. This directory is the repository's normal route for a
Hermes fix.

Usage::

    python3 apply_slack_reactions_scope.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import patchlib

RELATIVE = "hermes_cli/slack_cli.py"

# Asserted in the built bundle by the Dockerfile, and the guard against a second
# run: the insert sits beside its anchor rather than consuming it, so the
# element count alone cannot tell a fresh tree from an already-patched one.
BUILD_MARKER = '"reactions:write"'

#: The element the new scope is inserted after, at whatever indentation the list
#: is written at. Anchored to the whole line so it cannot match inside a longer
#: scope name, and so the captured indent can be reused verbatim.
READ_SCOPE = re.compile(r'^([ \t]*)"reactions:read",[ \t]*$', re.MULTILINE)

WRITE_SCOPE = "reactions:write"


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="slack_reactions_scope")
    patch.refuse_if_patched(BUILD_MARKER)

    scopes = patch.find_assign("bot_scopes", label="Slack manifest bot scope list")
    # chat:write is asserted alongside reactions:read purely as a shape check —
    # it is the one scope the adapter cannot work at all without, so a list
    # missing it is not the list this patch was derived against.
    scopes.expect_contains("reactions:read", "chat:write")

    text = scopes.value_text
    found = READ_SCOPE.findall(text)
    if len(found) != 1:
        raise SystemExit(
            f"slack_reactions_scope patch: {RELATIVE}: expected 1 "
            f'"reactions:read" element on a line of its own in bot_scopes, '
            f"found {len(found)}. {patchlib.DRIFT_NOTE}"
        )

    patch.splice(
        scopes.value_start,
        scopes.value_end,
        READ_SCOPE.sub(
            lambda m: f'{m.group(0)}\n{m.group(1)}"{WRITE_SCOPE}",', text, count=1
        ),
    )
    patch.commit(f"{WRITE_SCOPE} added to bot_scopes")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
