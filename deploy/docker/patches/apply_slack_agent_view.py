#!/usr/bin/env python3
"""Suggested prompts and a named agent view for Slack when ``KAGE_SLACK_UX`` is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``apply_slack_reactions_scope.py``, which edits the same manifest builder.

What upstream already does
--------------------------
``hermes slack manifest`` prints the only description of the Slack app an
installer writes. Its ``messaging_experience`` has three branches: ``assistant``
(the default), ``agent`` (``--agent-view``) and ``none`` (``--no-assistant``).
The ``agent`` branch adds ``features.agent_view``, ``assistant:write`` and the
``app_context_changed``/``app_home_opened`` events. The adapter answers
``app_home_opened`` on the Messages tab (and ``assistant_thread_started`` on
assistant view) with ``assistant.threads.setSuggestedPrompts``, reading the
prompts from ``platforms.slack.extra.suggested_prompts``. Nothing in this
repository sets that key, so no install shows a prompt.

What this changes, with the flag on
-----------------------------------
* The ``agent`` branch describes the app by its ``--name`` instead of upstream's
  fixed "Chat with Hermes in Slack Messages.".
* The adapter falls back to :data:`SUGGESTED_PROMPTS` when
  ``suggested_prompts`` is unset. A configured value still wins.

Each file gains the same five-line flag check, ``_kage_slack_ux_on()``.

``agent_session_stopped`` is not subscribed. That subscription is what makes
Slack offer Stop, and no handler here acts on the event yet, so it would offer a
Stop that stops nothing.

With the flag off each edit is a branch that is not taken: the manifest and the
prompts are exactly upstream's. With it on, a manifest without ``--agent-view``
is upstream's too.

The messaging experience is never chosen here. Upstream calls ``--agent-view``
irreversible once a manifest carrying it is applied, so the default stays
``assistant`` and agent view is only ever the explicit flag.

The scopes the Slack UX work spends (``reactions:write``, ``users:read``,
``files:write``) are not added here: upstream's list carries the last two and
``apply_slack_reactions_scope.py`` adds the first. ``verify_slack_agent_view.py``
asserts all three in the emitted manifest.

Usage::

    python3 apply_slack_agent_view.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

MANIFEST = "hermes_cli/slack_cli.py"
ADAPTER = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_agent_view"

FLAG_ENV = "KAGE_SLACK_UX"
#: Neither file imports anything of ours, so the check carries its own copy of
#: ``slack_presenter.FLAG_ON_VALUES``; a host test holds the two equal once
#: slack_presenter.py is in agents/platform/scripts or agents/chat/scripts, and
#: fails rather than skips if it is there but will not import.
FLAG_ON_VALUES = ("1", "true", "yes", "on")

#: Mock 01's three asks, without the dev fleet's cluster name: a static prompt
#: cannot know which clusters an install has. Slack sends the ``message`` as
#: the user when the prompt is tapped.
SUGGESTED_PROMPTS = (
    "is anything unhealthy in my clusters right now?",
    "what's on the board?",
    "which clusters are behind their release channel?",
)

#: Upstream's cap on ``bot_name`` wherever the manifest prints it.
NAME_MAX = 35

#: The helper both files gain. Its name is the guard against a second run.
FLAG_HELPER = "_kage_slack_ux_on"
BUILD_MARKER = f"def {FLAG_HELPER}("

#: The adapter's constant; the build greps for it.
PROMPTS_NAME = "_KAGE_SUGGESTED_PROMPTS"
ADAPTER_MARKER = f"{PROMPTS_NAME} = "

HELPER = f'''

# kube-agents patch: slack_agent_view (deploy/docker/patches/apply_slack_agent_view.py).
def {FLAG_HELPER}() -> bool:
    import os as _os

    return _os.environ.get({FLAG_ENV!r}, "").strip().lower() in {FLAG_ON_VALUES!r}
'''

AGENT_DESCRIPTION_ANCHOR = '''\
        features["agent_view"] = {"agent_description": "Chat with Hermes in Slack Messages."}
'''
AGENT_DESCRIPTION = f'''\
{AGENT_DESCRIPTION_ANCHOR}\
        if {FLAG_HELPER}():
            features["agent_view"]["agent_description"] = f"Chat with {{bot_name[:{NAME_MAX}]}} in Slack Messages."
'''

PROMPTS_ANCHOR = '''\
        raw = self.config.extra.get("suggested_prompts")
'''
PROMPTS = f'''\
{PROMPTS_ANCHOR}\
        if raw is None and {FLAG_HELPER}():
            raw = {PROMPTS_NAME}
'''
PROMPTS_CONSTANT = (
    "\n"
    + ADAPTER_MARKER
    + repr([{"title": prompt, "message": prompt} for prompt in SUGGESTED_PROMPTS])
    + "\n"
)


def apply(root: Path) -> None:
    """Patch both files under ``root``, or raise SystemExit with the reason."""
    manifest = patchlib.Patch(root, MANIFEST, prefix=PREFIX)
    manifest.refuse_if_patched(BUILD_MARKER)
    manifest.find_def("slack_manifest_command", label="manifest command")
    manifest.find_def("_build_full_manifest", label="manifest builder")
    manifest.substitute(
        AGENT_DESCRIPTION_ANCHOR, AGENT_DESCRIPTION, label="agent view description"
    )
    manifest.append(HELPER)

    adapter = patchlib.Patch(root, ADAPTER, prefix=PREFIX)
    adapter.refuse_if_patched(BUILD_MARKER)
    adapter.substitute(PROMPTS_ANCHOR, PROMPTS, label="suggested prompts config read")
    adapter.append(HELPER + PROMPTS_CONSTANT)

    manifest.commit(f"a named agent view when {FLAG_ENV} is on")
    adapter.commit(f"default suggested prompts when {FLAG_ENV} is on")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
