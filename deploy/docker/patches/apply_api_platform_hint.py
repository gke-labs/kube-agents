"""Tell the model what renders an API-server turn: the kube-agents chat gateway.

One anchored substitution in ``agent/prompt_builder.py``'s ``PLATFORM_HINTS``.
Upstream's ``api_server`` hint assumes an unknown renderer and forbids
Markdown ("assume plain text. No markdown formatting (no asterisks, bullets,
headers, code fences)"). Under the A2A stack every chat turn reaches Hermes
through that platform: the hermes-bridge calls ``/v1/chat/completions`` for
Slack, Google Chat and Microsoft Teams alike, and the gateway renders the
answer for the backend (Block Kit, Chat cards, Adaptive Cards: headings,
bullets, code and pipe tables all land). The upstream hint therefore
contradicts the agent's own formatting rules (``SOUL.md``: write standard
Markdown, tabular data in one pipe table), and the model follows the hint
often enough to answer a table request with ``Label: value | Label: value``
lines and to say it is "configured to only send plain text" (the Teams live
test of #3056, 2026-11-17).

The replacement keeps upstream's file and media delivery sentences, which
describe the API server's own behaviour, and replaces only the rendering
claim. The marker is the gateway's name in the new text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PROMPT_BUILDER_RELATIVE = "agent/prompt_builder.py"

MARKER = "kube-agents chat gateway"

RENDERING_ANCHOR = (
    '        "You\'re responding through an API server. The rendering layer is unknown — assume plain text. No markdown "\n'
    '        "formatting (no asterisks, bullets, headers, code fences). Treat this like a conversation, not a document. "\n'
    '        "Keep responses brief and natural. '
)

RENDERING_REPLACEMENT = (
    f'        "You\'re responding through the {MARKER}, which renders your answer for the chat platform the "\n'
    '        "user is on (Slack, Google Chat or Microsoft Teams). Write standard Markdown: **bold**, `code`, fenced code "\n'
    '        "blocks, ## headings, - bullets, [links](url) and | pipe tables all render; put tabular data in one pipe "\n'
    '        "table with a header row. Treat this like a conversation, not a document. "\n'
    '        "Keep responses brief and natural. '
)


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, PROMPT_BUILDER_RELATIVE, prefix="api-platform-hint")
    patch.refuse_if_patched(MARKER)
    patch.substitute(RENDERING_ANCHOR, RENDERING_REPLACEMENT, label="api_server rendering hint")
    patch.commit("the api_server platform hint names the chat gateway as the renderer and asks for Markdown")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
