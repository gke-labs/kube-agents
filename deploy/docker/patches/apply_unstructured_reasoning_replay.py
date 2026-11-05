"""Keep an assistant reply that is unstructured model reasoning out of the model's history.

Gemini through LiteLLM sometimes writes its reasoning as plain assistant
content under a first line ``thought`` or ``thought:`` (#3206, #3773), with
``reasoning_content`` empty, so Hermes stores it as an ordinary reply; its think
scrubber (``agent/think_scrubber.py``) only handles tagged reasoning. Once a
session's history holds one such reply, Hermes replays it on every later call
and the model keeps imitating it: long cron audit sessions on one lane held
99-220 such replies each.

This breaks that loop on the wire copy only. Two anchored insertions, one in
each place Hermes v2026.9.14 builds the main loop's messages list for a model
call, run
``hermes_cli.unstructured_reasoning.replace_unstructured_reasoning`` (shipped
beside this file as ``unstructured_reasoning.py``, where the rule is written
down) on every outgoing message:

- ``agent/turn_context.py``'s ``build_api_messages``, which every turn's calls
  go through (``conversation_loop`` -> ``assemble_api_request``: chat
  completions, the API server's routes, cron), in its per-message loop after
  the ``api_content`` replay and before the reasoning-field copy;
- ``agent/chat_completion_helpers.py``'s ``_iteration_summary_api_messages``,
  the summary call a turn makes when it reaches its iteration limit, after its
  own ``api_content`` substitution.

Not covered, as data rather than as an assistant turn: context compression
serialises stored replies into its summary prompt (``context_compressor``,
``micro_compaction``), and a delegated subagent's final reply comes back to the
parent as a ``tool`` row. Both reach a model as text to read, not as its own
earlier turn, so the imitation risk is lower; a compressed session's summary
can still quote the reasoning. Only string content is checked; Hermes stores
assistant content as a string.

Both loops work on a copy of each stored message, so ``messages``, and with it
state.db, is untouched; the placeholder replaces only the content, so
``tool_calls`` still pair with their results. The marker is the trailing
comment on each inserted import.

The Hermes bridge (``a2a/hermes-bridge``, ``reasoning_leak.go``) withholds such
a reply from the user when it is the turn's answer; this patch is what stops
the next call from repeating it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

TURN_CONTEXT_RELATIVE = "agent/turn_context.py"
SUMMARY_RELATIVE = "agent/chat_completion_helpers.py"

MARKER = "kube-agents patch: unstructured_reasoning_replay"

# The function the insertions call, under the name they bind it to.
RULE_MODULE = "hermes_cli.unstructured_reasoning"
RULE_FUNCTION = "replace_unstructured_reasoning"
RULE_ALIAS = "_kube_replace_reasoning"

REASONING_COPY_ANCHOR = (
    "        # Pass reasoning back to the API for ALL assistant messages so multi-turn\n"
    "        # reasoning context is preserved.\n"
    "        agent._copy_reasoning_content_for_api(msg, api_msg)\n"
)

SUMMARY_SUBSTITUTE_ANCHOR = (
    "        substitute_api_content(api_msg)\n"
    "        if needs_sanitize:\n"
)

INSERTION = (
    "        # A reply that is the model's reasoning under a `thought` line (#3206) goes\n"
    "        # out as a placeholder, or the model imitates it on later calls.\n"
    f"        from {RULE_MODULE} import {RULE_FUNCTION} as {RULE_ALIAS}  # {MARKER}\n"
    f"        {RULE_ALIAS}(api_msg)\n"
)


def apply(root: Path) -> None:
    turn = patchlib.Patch(root, TURN_CONTEXT_RELATIVE, prefix="unstructured-reasoning-replay")
    summary = patchlib.Patch(root, SUMMARY_RELATIVE, prefix="unstructured-reasoning-replay")
    turn.refuse_if_patched(MARKER)
    summary.refuse_if_patched(MARKER)
    turn.substitute(REASONING_COPY_ANCHOR, INSERTION + "\n" + REASONING_COPY_ANCHOR,
                    label="build_api_messages reasoning copy")
    summary.substitute(SUMMARY_SUBSTITUTE_ANCHOR,
                       SUMMARY_SUBSTITUTE_ANCHOR.replace("        if needs_sanitize:\n", INSERTION + "        if needs_sanitize:\n"),
                       label="_iteration_summary_api_messages api_content substitution")
    # Both anchors matched before either file is written.
    turn.commit("a reasoning reply replays as a placeholder in every turn's calls")
    summary.commit("a reasoning reply replays as a placeholder in the iteration-limit summary call")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
