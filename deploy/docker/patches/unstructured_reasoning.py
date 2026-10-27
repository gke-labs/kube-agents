"""The rule for an assistant reply that is the model's own reasoning (#3206, #3773).

Installed into the image as ``hermes_cli/unstructured_reasoning.py``. The two
statements ``apply_unstructured_reasoning_replay.py`` inserts into Hermes, one
in each place the main loop builds a model call's messages, call
``replace_unstructured_reasoning`` on every outgoing message. This file is
the one place the rule is written down on the Python side. The Hermes bridge's copy
of the same rule is ``opensWithUnstructuredReasoning`` in
``a2a/hermes-bridge/reasoning_leak.go``; the two must agree.

Gemini through LiteLLM sometimes writes its reasoning as plain assistant
content instead of a thought part, so ``reasoning_content`` is empty and
Hermes's tag-based think scrubber passes it through. Three shapes have been
seen: a first line ``thought``, a first line ``thought:``, and a first line
``thought:`` with the reasoning following on the same line
(``thought:The user asked...``). The rule: the content's first non-empty line,
with its surrounding whitespace dropped and compared case-insensitively, is
exactly ``thought`` or starts with ``thought:``. Prose that only starts with the
letters ("Thought about it, ...", "thoughts") does not match, and neither does
"thought" anywhere past the first non-empty line. Lines are split on ``\\n``
only, as the Go side splits them.

A matching reply is not trimmed: nothing reliably marks where the reasoning
ends and an answer begins. Its content goes to the model as PLACEHOLDER, and
every other field, ``tool_calls`` included, is left alone so each tool call
still pairs with its result.
"""

from __future__ import annotations

from typing import Any

# A bare marker, on purpose: it removes a bad example from the history without
# adding wording the model reads. A sentence saying why the reply was omitted
# would itself be guidance about reasoning (.agents/rules/eval_driven_development.md,
# "When it applies").
PLACEHOLDER = "[omitted]"

# The token that opens a reasoning reply, and the separator that may follow it
# on the same line.
REASONING_WORD = "thought"
REASONING_SEPARATOR = ":"

ASSISTANT_ROLE = "assistant"


# Go's strings.TrimSpace trims unicode.IsSpace; Python's str.strip also trims
# the four information separators U+001C..U+001F. Leaving those in keeps the
# two sides of the rule in agreement on every input.
_NOT_TRIMMED_BY_GO = frozenset("\x1c\x1d\x1e\x1f")


def _trim(line: str) -> str:
    """``line`` with surrounding whitespace dropped as Go's ``strings.TrimSpace`` drops it."""
    start, end = 0, len(line)
    while start < end and line[start].isspace() and line[start] not in _NOT_TRIMMED_BY_GO:
        start += 1
    while end > start and line[end - 1].isspace() and line[end - 1] not in _NOT_TRIMMED_BY_GO:
        end -= 1
    return line[start:end]


def opens_with_unstructured_reasoning(content: Any) -> bool:
    """True when ``content``'s first non-empty line is ``thought`` or starts with ``thought:``, in any case."""
    if not isinstance(content, str):
        return False
    for line in content.split("\n"):
        line = _trim(line)
        if not line:
            continue
        lowered = line.lower()
        return lowered == REASONING_WORD or lowered.startswith(REASONING_WORD + REASONING_SEPARATOR)
    return False


def replace_unstructured_reasoning(api_msg: dict) -> None:
    """Swap an assistant reasoning reply's content for PLACEHOLDER, in place; leave everything else."""
    if api_msg.get("role") == ASSISTANT_ROLE and opens_with_unstructured_reasoning(api_msg.get("content")):
        api_msg["content"] = PLACEHOLDER
