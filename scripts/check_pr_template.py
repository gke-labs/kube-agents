#!/usr/bin/env python3
"""Keep every section of the pull-request template one an author can answer.

Two readers hold a pull request's description to the template: the review
bot, ``gke-labs/kube-agents-bot``, opens a thread for any template heading
the body lacks, and the contributor robot, ``gke-labs/kube-agents-robot``,
refuses to post a body that lacks one. The bot has no notion of an optional
section -- it walks every heading and asks for an answer -- and the robot's
one exception is a comment that says "otherwise delete this section", a
phrase this check forbids. So a template section whose comment tells the
author to take it out asks for exactly what the bot reports. The section that
applies to some pull requests only says instead what the rest write there,
the way **Bug Fix: Preventing Recurrence** does: ``Otherwise write "Not a bug
fix."``

That is how this file came to exist. kube-agents#3987 added **Roster
Admission** with "Otherwise delete this section", every pull request that
followed the instruction drew a description thread from the review bot, and
the robot grew its reader-side exception (kube-agents-robot#215) before the
template itself was put right (#4144). The template is reviewed here, where
neither reader's code is in view, so this check is the one that fails the
pull request that re-introduces the shape, in the review where it can be
fixed.

This is a tripwire for the shapes seen and the ones next to them, not a
grammar of English: the rule itself lives in ``.agents/rules/pre_pr_review.md``
and a reviewer still reads the comment. What the check reads, per section:

- **Removal.** A comment that tells the author to delete, remove, omit, drop,
  skip, erase, leave out or take out the section or heading (``REMOVAL_RE``):
  active ("delete this section", "delete any sections that do not apply"),
  passive ("this section is removed when"), bare before its condition
  ("remove if not applicable"), or a clause of its own after "otherwise" or
  "if not" ("otherwise delete it"). The section may be named, quoted or
  plural. A removal negated right before its verb ("do not delete this
  section", "rather than delete", "take care not to delete") is not one; a
  "not" elsewhere in the sentence ("if this PR does not fix a bug, delete
  this section") is the condition, and the removal stands. The pronoun form
  is read as the section: "otherwise skip it" about a screenshot is a
  finding, and the author rewords it.
- **Condition.** The first sentence of each comment, with a leading label such
  as "Note:" or "Optional:" kept, is read for a signal at its start that the
  section applies to some pull requests only (``CONDITION_RE``): "optional",
  "only when", "only if", "only for", "unless", "required when", "applies
  if", "only applies when", "applies only to", "fill in when", "not
  required", with "this section" or "complete this section" allowed in
  front. A section with one also says ``Otherwise write "..."``
  (``ANSWER_RE``; "if not", "say", "answer", "state" and a colon count, and
  the quotes may be curly or backticks), with the quoted answer on one line so a search for the phrase
  finds the file that states it. Deeper sentences are not read, because the
  template's own prose says "a complete answer only when ..." there; a signal
  later in the sentence is not read either ("name the optional values"); and
  a bare "If this PR ..." opener is not a signal, because the same shape
  introduces an ordinary instruction ("If this PR closes an issue, link it").
  A conditional section opens with the condition, which is the convention
  the rule file states, and a reviewer reads the rest.
- **Shape.** A section's comment markers balance, which is how a heading-
  shaped line inside a comment shows, since the parse splits the section on
  it first; and no comment holds a fence-shaped line, which the marker count
  cannot see, because both readers open a fence there (they are not
  CommonMark parsers, and neither is this) and a fence left open swallows
  every later section.

The text before the first heading is read for removal and shape too, since a
whole-file "delete the sections that do not apply" would sit there, and not
for a condition, since it is no section.

The parser is the readers' shape, copied from ``app/pr_body.py`` in
kube-agents-bot and ``runner/kube_agents_robot/pr_body.py`` in
kube-agents-robot: ``##`` through ``######`` outside a fence is a section, a
fence closes on the marker that opened it, and a comment is what ``<!-- -->``
encloses. Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_pr_template.py [path]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Where GitHub reads the template from, and the one file this checks.
TEMPLATE = Path(".github/PULL_REQUEST_TEMPLATE.md")

# The readers' parser, character for character: kube-agents-bot
# `app/pr_body.py` (origin/main 7179b5e) and kube-agents-robot
# `runner/kube_agents_robot/pr_body.py` (main 2103d4b). `## Heading` through
# `###### Heading`; the H1 is the template's own title, not a section anyone
# fills in. If either reader moves to a real Markdown parser, this moves too.
HEADING_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
COMMENT_OPEN = "<!--"
COMMENT_CLOSE = "-->"

# The quotes a stock answer may sit in, and that may precede a negated verb.
QUOTE_CHARS = "\"'“”‘’`"

# An instruction to take the section out of the body.
_VERB = r"(?:delete|remove|omit|drop|skip|erase|leave\s+out|take\s+out)"
_PASSIVE = r"(?:deleted|removed|omitted|dropped|skipped|erased|left\s+out)"
_DETERMINER = r"(?:this|the|that|those|these|any|all|every|each|both)"
_FILLER = r"(?:(?!(?:in|under|of|from|within|this|the|that|a|an)\b)[^\s.;]+\s+){0,4}"
_OBJECT = rf"{_DETERMINER}\s+{_FILLER}(?:section|heading)s?\b(?![{QUOTE_CHARS}]s)"
_CONDITION_OPENER = r"(?:if|when|unless)"
_OTHERWISE = r"(?:otherwise|if\s+not|when\s+not)"
_SOFTENER = r"(?:just\s+|simply\s+|feel\s+free\s+to\s+|you\s+(?:may|can)\s+|it\s+can\s+be\s+)?"
_CLAUSE_END = r"\s*(?:[.;,!]|$)"
REMOVAL_RE = re.compile(
    rf"\b{_VERB}\s+{_OBJECT}"
    rf"|\b{_VERB}(?=\s+{_CONDITION_OPENER}\b|{_CLAUSE_END})"
    rf"|\b(?:leave|take)\s+{_OBJECT}\s+out\b"
    rf"|\b{_OBJECT}\s+(?:is|are|will\s+be|can\s+be|may\s+be|should\s+be|must\s+be)\s+{_PASSIVE}\b"
    rf"|\b{_OTHERWISE},?\s+{_SOFTENER}(?:{_VERB}|{_PASSIVE})(?:\s+(?:it|them)\b|{_CLAUSE_END})"
    rf"|\b{_OTHERWISE},?\s+(?:leave|take)\s+(?:it|them)\s+out{_CLAUSE_END}",
    re.IGNORECASE,
)
# A removal is negated by the words right before its verb: "do not delete",
# "never remove", "rather than delete", "no reason to delete", with one
# parenthetical ("do not, under any circumstances, delete") and an opening
# quote allowed between. A "not" elsewhere in the sentence ("if this PR does
# not fix a bug, delete this section") is the condition, not a negation.
_MODAL = r"(?:do|does|did|should|must|may|can|will|would|need)"
NEGATION_BEFORE_RE = re.compile(
    rf"(?:\b{_MODAL}\s+not|\b(?:don|doesn|didn|shouldn|mustn|can|won|wouldn|needn)'t"
    r"|\bnever|\brather\s+than|\binstead\s+of|\bno\s+(?:reason|need)\s+to"
    r"|\bunder\s+no\s+circumstances|\bnot\s+to)"
    rf"(?:,[^,]*,)?\s*(?:just\s+|simply\s+)?[{QUOTE_CHARS}]?\s*$",
    re.IGNORECASE,
)

# A signal, in a comment's first sentence, that the section applies to some
# pull requests only. Kept short on purpose; the docstring says what is not
# read and why.
_SIGNAL = (
    r"(?:optional\b"
    r"|only\s+(?:when|if|for|applies|fill)\b"
    r"|applies\s+only\s+to\b"
    r"|unless\b"
    r"|(?:applies|required|needed)\s+(?:only\s+)?(?:when|if)\b"
    r"|not\s+(?:required|needed)\b"
    r"|fill(?:ed)?\s+(?:this\s+)?in\s+(?:only\s+)?(?:when|if)\b)"
)
_SUBJECT = r"(?:(?:complete|fill\s+in|answer|use)\s+)?(?:this\s+section\s+)?(?:is\s+)?(?:only\s+)?"
CONDITION_RE = re.compile(rf"^(?:(?:[\w'/.-]+\s*){{1,3}}:\s*)?{_SUBJECT}{_SIGNAL}", re.IGNORECASE)
# A comment's first sentence: leading list markers dropped, a short label
# ending in a colon kept with the sentence after it.
LEAD_RE = re.compile(r"^[\s\-*+>]+")
LABEL_RE = re.compile(r"^(?:[\w'/.-]+\s*){1,3}:\s*")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")

# The stock answer a conditional section names for every other pull request,
# in straight or curly quotes. The phrase inside may not contain a line break
# (ANSWER_RE); one found only across a break is the split case (SPLIT_ANSWER_RE).
_QUOTE = f"[{QUOTE_CHARS}]"
_NOT_QUOTE = f"[^{QUOTE_CHARS}"
_WRITE = rf"\b{_OTHERWISE},?\s+(?:write|say|answer|state):?\s+(?:\w+\s+){{0,2}}"
ANSWER_RE = re.compile(rf"{_WRITE}{_QUOTE}{_NOT_QUOTE}\n]+{_QUOTE}", re.IGNORECASE)
SPLIT_ANSWER_RE = re.compile(rf"{_WRITE}{_QUOTE}{_NOT_QUOTE}]*\n{_NOT_QUOTE}]*{_QUOTE}", re.IGNORECASE)

# The heading a problem is filed under when the file has no section at all,
# and the one for the text before the first heading, where a whole-file
# instruction ("delete the sections that do not apply") would sit.
NO_SECTIONS_HEADING = "(whole file)"
PREAMBLE_HEADING = "(before the first heading)"
BOM = "\ufeff"

# One message per rule; the tests import these rather than pin the prose.
NO_SECTIONS_MESSAGE = "the template has no section heading at all"
REMOVAL_MESSAGE = "its comment tells the author to remove the section"
NO_ANSWER_MESSAGE = (
    'it applies to some pull requests only and names no answer for the rest (Otherwise write "...")'
)
SPLIT_ANSWER_MESSAGE = "its stock answer is split across a line break"
UNBALANCED_MESSAGE = (
    "a comment opens or closes without its pair, as one with a heading-shaped line inside it does,"
    " which splits the section for the readers"
)
LINE_IN_COMMENT_MESSAGE = "a comment holds a fence-shaped line, which opens a fence for the readers"
OPEN_FENCE_MESSAGE = "a fence is still open at the end of the file, so every later heading is hidden"

REMEDY = (
    "A pull-request description is checked against every heading of this template by\n"
    "the review bot (gke-labs/kube-agents-bot) and the contributor robot\n"
    "(gke-labs/kube-agents-robot). The bot has no optional section, and the robot's one\n"
    "exception is the phrase this check forbids: a heading the body lacks is a thread on\n"
    "the pull request, and a comment that says to delete the section asks for exactly\n"
    "that. A section that applies to some pull requests only opens with the condition\n"
    "and says what the rest write there, in quotes on one line, the way Bug Fix:\n"
    'Preventing Recurrence does: Required when ... Otherwise write "Not a bug fix."'
)


def _parse(markdown: str) -> tuple[list[tuple[str, str]], bool]:
    """The preamble, the sections, and whether a fence was still open at the end.

    The preamble is the first entry, under ``PREAMBLE_HEADING``; ``sections()``
    drops it, because it is not a heading the readers hold a body to.
    """
    found: list[tuple[str, list[str]]] = [(PREAMBLE_HEADING, [])]
    fence: str | None = None
    for line in markdown.lstrip(BOM).splitlines():
        marker = FENCE_RE.match(line)
        if marker and fence is None:
            fence = marker.group(1)
        elif marker and marker.group(1) == fence:
            fence = None
        heading = None if fence else HEADING_RE.match(line)
        if heading:
            found.append((heading.group(2), []))
        else:
            found[-1][1].append(line)
    return [(heading, "\n".join(lines)) for heading, lines in found], fence is not None


def sections(markdown: str) -> list[tuple[str, str]]:
    """``(heading, content)`` for every section, in file order."""
    return _parse(markdown)[0][1:]


def comments(content: str) -> list[str]:
    return HTML_COMMENT_RE.findall(content)


def first_sentence(comment: str) -> str:
    """The opening sentence, with a leading label such as ``Note:`` kept on it."""
    text = LEAD_RE.sub("", comment)
    label = LABEL_RE.match(text)
    rest = text[label.end() :] if label else text
    sentence = SENTENCE_END_RE.split(rest, maxsplit=1)[0]
    return (label.group(0) if label else "") + sentence


def removal(comment: str) -> bool:
    """Whether the comment tells the author to take the section out."""
    return any(
        not NEGATION_BEFORE_RE.search(comment[: match.start()])
        for match in REMOVAL_RE.finditer(comment)
    )


def _shape_problems(content: str) -> list[str]:
    found = []
    if content.count(COMMENT_OPEN) != content.count(COMMENT_CLOSE):
        found.append(UNBALANCED_MESSAGE)
    for comment in comments(content):
        if any(FENCE_RE.match(line) for line in comment.splitlines()):
            found.append(LINE_IN_COMMENT_MESSAGE)
            break
    return found


def problems(markdown: str) -> list[tuple[str, str]]:
    """``(heading, message)`` for every rule a section breaks, in file order."""
    parsed, open_fence = _parse(markdown)
    if len(parsed) == 1:
        message = OPEN_FENCE_MESSAGE if open_fence else NO_SECTIONS_MESSAGE
        return [(NO_SECTIONS_HEADING, message)]
    found = []
    for heading, content in parsed:
        shape = _shape_problems(content)
        if shape:
            # A misparsed section's comments are not the author's: a fence
            # left open in one pulls the next section's text into this one.
            found.extend((heading, message) for message in shape)
            continue
        section_comments = comments(content)
        if not section_comments:
            continue
        joined = "\n".join(section_comments)
        if removal(joined):
            # One defect, one line: a comment that says to delete the
            # section names no answer either, and the remedy is the same.
            found.append((heading, REMOVAL_MESSAGE))
        elif heading == PREAMBLE_HEADING:
            continue
        elif any(CONDITION_RE.search(first_sentence(c)) for c in section_comments):
            if SPLIT_ANSWER_RE.search(joined) and not ANSWER_RE.search(joined):
                found.append((heading, SPLIT_ANSWER_MESSAGE))
            elif not ANSWER_RE.search(joined):
                found.append((heading, NO_ANSWER_MESSAGE))
    if open_fence:
        found.append((parsed[-1][0], OPEN_FENCE_MESSAGE))
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else REPO / TEMPLATE
    if not path.is_file():
        print(f"MISSING  {path} -- the pull-request template is gone")
        return 1
    markdown = path.read_text(encoding="utf-8")
    found = problems(markdown)
    if found:
        lines = "\n".join(f"  section {heading!r}: {message}" for heading, message in found)
        print(
            f"FAIL: {path} has a section the readers cannot hold a body to:\n"
            f"{lines}\n\n{REMEDY}"
        )
        return 1
    print(f"PR template: {len(sections(markdown))} sections, every one answerable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
