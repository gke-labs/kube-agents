"""Never use the model's reasoning as the reply.

The chart's gateway asks a Gemini model for its thoughts
(``thinking: {"type": "enabled"}`` in the LiteLLM config, see
``kube-agents.litellmConfig`` in ``charts/kube-agents/templates/_helpers.tpl``),
so they come back as ``reasoning_content`` instead of not at all. Hermes keeps
that channel out of the reply on an ordinary turn, but three places in
v2026.9.14 turn it into reply text when the reply itself is empty, and before
the gateway asked for the thoughts none of the three ever saw Gemini reasoning:

1. ``agent/turn_final_response.py``, the reasoning-only clean stop: a response
   with ``finish_reason == "stop"``, no tool calls and no text has its whole
   reasoning promoted to the reply, which is then persisted as ordinary
   content and replayed to the model on every later call. Upstream wrote it for
   a vLLM parser that files the answer as reasoning; on Gemini the reasoning is
   a thought summary, not the answer.
2. ``agent/turn_empty_response.py``, ``_terminal_empty``: once the empty-reply
   retries are spent, the reply is a notice plus the first 500 characters of
   the reasoning.
3. ``agent/turn_truncation.py``, the ``content_filter`` refusal: a refusal with
   no text takes the reasoning as "Model's explanation", in the reply and in
   the turn's ``error``, which the Hermes bridge puts in the task's failed
   reason on the bus.

Each becomes the path a model without a reasoning channel takes: (1) leaves
the reply empty, so the empty-reply ladder runs (a prefill continuation, then
retries); (2) returns the notice without the excerpt; (3) reports a refusal
with no explanation. The reasoning still reaches the pod log where upstream
logs it, and the session store, as before; it reaches no reply. Showing the
model's thinking to a person is a later, deliberate feature, not a fallback.

The gateway's own display of reasoning goes too. ``/reasoning show`` (or
``on``, typed, picked, or as ``/model X --reasoning show``) lets any user turn
display on for a whole platform, and ``gateway/run_turn.py`` then prepends the
last reasoning block to every reply there. The patch makes
``_apply_reasoning_selection`` refuse the show toggle with a fixed reply
(``hide``/``off`` and the effort levels still work), and makes the prepend
never fire, so a ``show_reasoning: true`` already persisted in config.yaml is
inert too.

Model-agnostic on purpose: the rule is about the channel, and the same three
fallbacks would hand a Claude model's thinking to a Slack thread. Two costs
follow. A backend whose reasoning parser files the whole answer as reasoning
(upstream names vLLM nemotron_v3 past ~500K prompt tokens, an
``openai_compatible`` install) loses the rescue in (1): its clean stop runs
the ladder and ends on the notice. And a Gemini turn that ends in reasoning
alone, which before the gateway asked for thoughts looked truly empty and
went straight to the retries, now first takes up to two prefill
continuations (``agent/turn_empty_response.py``), each a full model call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

MARKER = "kube-agents patch: reasoning_not_reply"

FINAL_RESPONSE_RELATIVE = "agent/turn_final_response.py"
EMPTY_RESPONSE_RELATIVE = "agent/turn_empty_response.py"
TRUNCATION_RELATIVE = "agent/turn_truncation.py"
SLASH_RELATIVE = "gateway/slash_commands_model.py"
RUN_TURN_RELATIVE = "gateway/run_turn.py"

CLEAN_STOP_ANCHOR = "        _promoted = agent._extract_reasoning(assistant_message)\n"
CLEAN_STOP_REPLACEMENT = f"        _promoted = None  # {MARKER}: reasoning is not the reply\n"

TERMINAL_ANCHOR = (
    '        + ". Its last reasoning, which may contain the answer:\\n\\n" + reasoning_preview\n'
)
TERMINAL_REPLACEMENT = f'        + "."  # {MARKER}: no reasoning excerpt\n'

REFUSAL_ANCHOR = (
    "    if not _refusal_text:\n"
    "        _refusal_text = (agent._extract_reasoning(_refusal_result) or \"\").strip()\n"
)
REFUSAL_REPLACEMENT = f"    # {MARKER}: the reasoning is not the refusal's explanation\n"

DISPLAY_REFUSED = "Reasoning display is turned off on this install: the model's reasoning is never posted in chat."
DISPLAY_TOGGLE_ANCHOR = (
    "        show = _REASONING_DISPLAY_TOGGLES.get(value)\n"
    "        if show is not None:  # per-platform display toggle\n"
)
DISPLAY_TOGGLE_REPLACEMENT = (
    "        show = _REASONING_DISPLAY_TOGGLES.get(value)\n"
    f"        if show:  # {MARKER}: display stays off\n"
    f"            return {DISPLAY_REFUSED!r}\n"
    "        if show is not None:  # per-platform display toggle\n"
)

PREPEND_ANCHOR = '        last_reasoning = agent_result.get("last_reasoning")\n'
PREPEND_REPLACEMENT = f"        last_reasoning = None  # {MARKER}: never prepended to a reply\n"


def apply(root: Path) -> None:
    prefix = "reasoning-not-reply"
    clean_stop = patchlib.Patch(root, FINAL_RESPONSE_RELATIVE, prefix=prefix)
    terminal = patchlib.Patch(root, EMPTY_RESPONSE_RELATIVE, prefix=prefix)
    refusal = patchlib.Patch(root, TRUNCATION_RELATIVE, prefix=prefix)
    display_toggle = patchlib.Patch(root, SLASH_RELATIVE, prefix=prefix)
    prepend = patchlib.Patch(root, RUN_TURN_RELATIVE, prefix=prefix)
    patches = (clean_stop, terminal, refusal, display_toggle, prepend)
    for patch in patches:
        patch.refuse_if_patched(MARKER)
    clean_stop.substitute(CLEAN_STOP_ANCHOR, CLEAN_STOP_REPLACEMENT, label="reasoning-only clean stop")
    terminal.substitute(TERMINAL_ANCHOR, TERMINAL_REPLACEMENT, label="terminal empty excerpt")
    refusal.substitute(REFUSAL_ANCHOR, REFUSAL_REPLACEMENT, label="refusal explanation")
    display_toggle.substitute(DISPLAY_TOGGLE_ANCHOR, DISPLAY_TOGGLE_REPLACEMENT, label="/reasoning show")
    prepend.substitute(PREPEND_ANCHOR, PREPEND_REPLACEMENT, label="reasoning prepend")
    # Every anchor matched before anything is written, so a moved anchor in one
    # file leaves every file untouched.
    clean_stop.commit("a reasoning-only clean stop is no longer promoted to the reply")
    terminal.commit("the terminal empty reply carries no reasoning excerpt")
    refusal.commit("a refusal with no text no longer explains itself with the reasoning")
    display_toggle.commit("/reasoning show is refused")
    prepend.commit("the gateway never prepends the reasoning to a reply")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
