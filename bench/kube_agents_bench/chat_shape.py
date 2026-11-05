# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The shape of the reply a chat user reads, measured on every run.

A case's own checks grade what its answer says; this measures how the answer
reads, the same way on every case, so a model or prompt change that brings
back preambles and walls of text shows up as a trend rather than in one case's
judged score. Reported, never scored: nothing here reaches a verdict.

The answer is the last card result delivered into the thread when the ask was
delegated, and the whole reply otherwise. The checks are structural on
purpose: an opening line that is a heading, ends on a colon, or starts with
one of a short list of lead-in phrases, and an apology word anywhere outside
quoted code. A sentence that merely sounds hedged is the judge's to notice.
"""

from __future__ import annotations

import re
from typing import Any

#: The sidecar's keys, which bench-gate's ``rep N shape:`` line repeats.
OPENS_ON_VERDICT = "opens_on_verdict"
APOLOGY = "apology"
BOT_MESSAGES = "bot_messages"
WORDS_BEFORE_ANSWER = "words_before_answer"
ANSWER_WORDS = "answer_words"
KEYS = (OPENS_ON_VERDICT, APOLOGY, BOT_MESSAGES, WORDS_BEFORE_ANSWER, ANSWER_WORDS)
_FLAGS = (OPENS_ON_VERDICT, APOLOGY)

# The headers harness.py opens each delivered card result, paused card's
# question and artifact with in the final message (_RESULT_SECTION,
# _QUESTION_SECTION, _append_artifacts). A result or a question is a message
# the thread received; an artifact is a file read back from the pod, which no
# one was sent.
_DELIVERED_HEADER = re.compile(
    r"^(?:Result of delegated task \S+|Question from delegated task \S+"
    r"|Artifact [^\n]* produced by delegated task \S+):\n",
    re.MULTILINE,
)
_RESULT_HEADER_PREFIX = "Result of delegated task "
_ARTIFACT_HEADER_PREFIX = "Artifact "
_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
# Markup and symbols ahead of a line's first word: emphasis, a quote mark, a
# list bullet, an emoji. Stripped before the lead-in phrases are matched.
_LEADING_MARKUP = re.compile(r"^[\W_]+")
# Phrases that open a reply without answering it. Matched at the start of the
# first sentence only, so "I'll" deep in an answer is not a preamble.
_LEAD_IN = re.compile(
    r"^(?:let me|let's|let us|i'll|i will|i'm going to|i am going to|sure|okay|ok|"
    r"certainly|of course|absolutely|great question|good question|got it|alright|"
    r"here's|here is|here are|happy to|thanks|thank you|checking|looking into)\b",
    re.IGNORECASE,
)
_APOLOGY = re.compile(r"\b(?:sorry|apologi[sz]e[sd]?|apologies)\b", re.IGNORECASE)
# The typographic apostrophe a model writes as often as the ASCII one; folded
# before the lead-in match, as verifiers.py's _MARKDOWN_NOISE folds it.
_CURLY_APOSTROPHE = "\u2019"
# The end of the first sentence on a line: a stop, any emphasis or quote
# closing over it ("**Sure!**"), and the whitespace after.
_FIRST_SENTENCE = re.compile(r"^(.+?[.!?:])[*_\"'\u201d)]*(?:\s+|$)")
_WORD = re.compile(r"\S+")
_FENCE = re.compile(r"```.*?```", re.DOTALL)
# Inline code: a quoted log line or HTTP body, not the agent's own words.
_INLINE_CODE = re.compile(r"`[^`\n]*`")
# Leading units read for a preamble before the answer is taken to have started.
_MAX_PREAMBLE_UNITS = 3


def _sections(final_message: str) -> tuple[str, list[tuple[str, str]]]:
    """The reply ahead of any delivered section, then each section's header and body."""
    headers = list(_DELIVERED_HEADER.finditer(final_message))
    opening = final_message[: headers[0].start()] if headers else final_message
    sections = [
        (h.group(0), final_message[h.end() : headers[i + 1].start() if i + 1 < len(headers) else len(final_message)])
        for i, h in enumerate(headers)
    ]
    return opening, sections


def answer_text(final_message: str) -> str:
    """The answer in ``final_message``: its last delivered card result, or all of it."""
    opening, sections = _sections(final_message)
    results = [body for header, body in sections if header.startswith(_RESULT_HEADER_PREFIX)]
    return (results[-1] if results else final_message).strip()


def _posted(final_message: str) -> list[str]:
    """The text of each message the thread received: the reply, then each result and question."""
    opening, sections = _sections(final_message)
    posted = [body for header, body in sections if not header.startswith(_ARTIFACT_HEADER_PREFIX)]
    return [text for text in [opening, *posted] if text.strip()]


def _quoted_out(text: str) -> str:
    """``text`` without its fenced blocks and inline code, which quote what
    something else said."""
    return _INLINE_CODE.sub("", _FENCE.sub("", text))


def bot_messages(final_message: str) -> int:
    """How many messages the thread received: the reply itself, then one per result or question."""
    return len(_posted(final_message))


def _words(text: str) -> int:
    return len(_WORD.findall(text))


def _preamble_words(answer: str) -> tuple[bool, int]:
    """Whether the answer's first unit is a preamble, and the words before its answer.

    A unit is a line, or the first sentence of a line that opens on a lead-in
    phrase. A heading, a line ending on a colon, or a lead-in sentence is a
    preamble; the first unit that is none of these is where the answer starts.
    """
    lines = [line for line in _FENCE.sub("", answer).splitlines() if line.strip()]
    first_is_preamble = False
    words = 0
    units = 0
    while lines and units < _MAX_PREAMBLE_UNITS:
        line = lines[0].strip()
        bare = _LEADING_MARKUP.sub("", line).replace(_CURLY_APOSTROPHE, "'")
        if _ATX_HEADING.match(line) or line.rstrip("*_ ").endswith(":"):
            spent, rest = line, ""
        elif _LEAD_IN.match(bare):
            sentence = _FIRST_SENTENCE.match(line)
            spent, rest = (sentence.group(1), line[sentence.end() :]) if sentence else (line, "")
        else:
            break
        if units == 0:
            first_is_preamble = True
        words += _words(spent)
        units += 1
        lines = ([rest] if rest.strip() else []) + lines[1:]
    return first_is_preamble, words


def measure(final_message: str, posts: int | None = None) -> dict[str, Any] | None:
    """The shape of the reply in ``final_message``, or None when nothing was delivered.

    ``posts`` is the number of messages the transport saw posted for this
    ask, when it counts them (the inject transport does); without it the
    count is read from the final message's delivered sections.
    """
    answer = answer_text(final_message)
    if not answer:
        return None
    first_is_preamble, before = _preamble_words(answer)
    return {
        OPENS_ON_VERDICT: not first_is_preamble,
        APOLOGY: any(_APOLOGY.search(_quoted_out(text)) for text in _posted(final_message)),
        BOT_MESSAGES: posts if posts is not None else bot_messages(final_message),
        WORDS_BEFORE_ANSWER: before,
        ANSWER_WORDS: _words(answer),
    }


def parse(data: Any) -> dict[str, bool | int] | None:
    """The sidecar's measurements that hold the type they should, or None for none.

    A flag must be a bool and a count a non-negative int; anything else, and
    any unknown key, is dropped rather than reported, as a timing that is not
    a number is.
    """
    if not isinstance(data, dict):
        return None
    shape: dict[str, bool | int] = {}
    for key in KEYS:
        value = data.get(key)
        if key in _FLAGS:
            if isinstance(value, bool):
                shape[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            shape[key] = value
    return shape or None
