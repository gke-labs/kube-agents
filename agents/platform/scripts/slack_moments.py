"""Slack layout for the two moments that get a message of their own.

Pure functions only, like ``slack_presenter``, whose layout this reuses.
``gateway/slack_ux_moments.py`` decides when to post them.

* A pull request the work opened (:func:`pr_opened`): a bold headline naming
  the PR, the worker's own line below it as evidence, and "Open PR" and
  "Files changed" url buttons. :func:`opened_pr` finds it in a progress note or a report, and
  only where the line says it opened that PR, the verb in front of the url
  with the worker as its subject by the one rule in :func:`_ours`, so a note
  citing someone else's PR ("… pull/300, opened by bob", "Dependabot opened
  …", "`dependabot[bot]` opened …") is not announced as ours.
* A question the work is waiting on (:func:`needs_you`): the worker's question
  in bold (plain, when bold would cost it a ``*`` or a ``__name__``), the rest
  of its reason below it, a choice button for each option
  the reason ends with, and a "waiting on you" line. A click is the clicker's
  answer in the thread (``gateway/slack_ux_clicks.py``), the same path as
  typing it. The options are buttons only when there are two to five, each
  fits on a button, they end the reason, and the line right before them is a
  question that is not a yes/no ask to go on ("Shall I proceed?", whose list is
  the plan); otherwise, and with no thread for a click to answer in, the
  question is text only and a typed reply is the answer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import slack_presenter as _presenter

#: A pull request url on GitHub over https: owner, repo and number, with any
#: trailing path (``/files``, ``#discussion``) left out of the match. Any other
#: host is left to the ordinary line: the headline vouches for the url in the
#: bot's voice and the evidence line hides its host, so a note echoing someone
#: else's link must not become a button. GitHub alone, as in
#: ``repo_ref.GITHUB_HOSTS``, which names no enterprise host.
PR_URL = re.compile(r"https://github\.com/([^\s/<>|]+)/([^\s/<>|]+)/pull/(\d+)")
#: What must sit right before the url for a line to announce the PR as ours:
#: the verb, then optionally "a"/"the", "new", "PR"/"pull request" with its
#: number, a colon, dash or "(", and the opening of a ``[label](`` or ``<`` link.
#: A label's colon may follow the verb ("Opened: <url>"), its closing bold or
#: code may sit before the url ("**Opened PR:** <url>"), and so may bold or code
#: around the url itself ("Opened PR **<url>**"). "draft" may join "new".
#: What may sit between the verb and "PR": "a"/"the", then "new" and "draft".
PR_ARTICLE = r"(?:(?:an?|the)\s+)?(?:(?:new|draft)\s+){0,2}"
OPENED_BEFORE_URL = re.compile(
    r"\b(?:opened|created|raised|filed|submitted)(?:[*_`]*:)?[*_`]*\s+" + PR_ARTICLE
    + r"(?:(?:PR|pull\s+request)(?:\s*#\d+)?\s*[:(—–-]?[*_`]*\s*)?[*_`]*(?:\[[^\]]*\]\(|<)?$",
    re.IGNORECASE,
)
#: The verb and "PR" with more words before the url, in the same sentence:
#: "Opened PR #412 in acme/x: <url>", "Opened a PR against main: <url>".
OPENED_PR_THEN = re.compile(
    r"\b(?:opened|created|raised|filed|submitted)[*_`]*\s+" + PR_ARTICLE + r"(?:PR|pull\s+request)\b",
    re.IGNORECASE,
)
#: A sentence end between "PR" and the url; a colon does not end it here.
GAP_BREAK = re.compile(r"[.!?](?=[*_`]*\s+[A-Z])|;(?=[*_`]*\s)")
#: A negation just before the verb: "not opened", "haven't yet opened".
NEGATED_VERB = re.compile(r"(?:\bnot|\bnever|n't)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
#: Where a sentence ends: ".", "!" or "?" before a space and a capital, or
#: ";"/":" before a space, with closing bold or code allowed between. A dot or
#: colon inside a token (``values.yaml``, ``1.4.2``, ``main.py:42``, a url) is
#: not one.
SENTENCE_BREAK = re.compile(r"[.!?](?=[*_`]*\s+[A-Z])|[;:](?=[*_`]*\s)")
#: A word of the sentence, a mention's "@" and a hyphenated verb's "-" kept;
#: a bullet, markup or an emoji is no word.
CLAUSE_WORD = re.compile(r"[\w@'’-]*\w[\w@'’-]*")
#: Words dropped from either end of the sentence before the rule reads it.
FILLER = frozenset({
    "have", "just", "also", "now", "already", "successfully", "finally", "quickly", "eventually",
    "subsequently", "immediately", "promptly", "manually", "automatically", "separately",
    "additionally", "accordingly", "initially", "lastly",
})
#: Joins the verb to the worker's earlier step: "Fixed it and opened".
JOIN = frozenset({"and", "then"})
OUR_LEAD = frozenset({"i", "we", "i've", "we've", "i’ve", "we’ve"})
#: An outcome a label may name before "opened": "Done: opened", "Tests passed, opened".
OUTCOME_WORD = frozenset({
    "done", "update", "next", "result", "ready", "green", "complete", "completed", "finished",
})
#: A label naming the PR with no subject: "PR opened: <url>".
PR_LABEL = (["pr"], ["pull", "request"])
#: Someone else as the subject after the worker's first step: "Checked with Bob
#: and he then opened", "Confirmed with Alice, who then opened". Not "it", the
#: object of "Fixed it and opened".
OTHER_SUBJECT = frozenset({"he", "she", "they", "who", "which"})
#: The worker's own earlier steps. A list rather than "-ed", which would take
#: "Ahmed then opened" and "Fred reviewed and opened" as ours.
OUR_VERB = frozenset({
    "added", "adjusted", "analysed", "analyzed", "applied", "audited", "began", "bumped", "built",
    "changed", "checked", "cleaned", "closed", "committed", "compared", "confirmed", "corrected",
    "created", "debugged", "decreased", "deployed", "diagnosed", "did", "disabled", "documented",
    "drafted", "edited", "enabled", "fetched", "filed", "fixed", "found", "generated", "got",
    "identified", "implemented", "increased", "inspected", "installed", "investigated", "kept",
    "looked", "lowered", "made", "merged", "migrated", "modified", "moved", "opened", "patched",
    "pinned", "prepared", "pulled", "pushed", "put", "raised", "ran", "re-ran", "read", "rebased",
    "rebuilt", "reduced", "refactored", "regenerated", "removed", "renamed", "replaced",
    "reproduced", "reran", "restarted", "restored", "reverted", "reviewed", "rewrote", "rolled",
    "scaled", "sent", "set", "split", "submitted", "superseded", "tested", "took", "traced",
    "tuned", "updated", "upgraded", "validated", "verified", "wrote",
})
#: The worker's line under the headline, clipped: a note can be one long
#: paragraph, and a Slack context element holds at most 3,000 characters.
EVIDENCE_MAX = 300
#: Markup a button cannot show, stripped from an option with its pair only, so
#: a glob (``app=web-*``) or a dunder name keeps its characters.
OPTION_MARKUP = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*")
#: The same for the question's headline, plus a paired ``*emphasis*``.
HEADLINE_MARKUP = re.compile(
    r"`([^`]+)`|\*\*([^*]+)\*\*|(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])"
)

PR_HEADLINE = "I opened PR #{number} in {repo}. It's yours to review."
PR_REF = "PR #{number}"
#: The url in the worker's line, with a "PR" or "PR #412:" already in front of
#: it, so "Opened PR <url>" reads "Opened PR #412" rather than "Opened PR PR
#: #412". A Slack ``<url|label>``, markdown ``[label](url)`` or ``(url)``
#: around it goes too, and so does a tail after the number (``/files``).
PR_URL_TAIL = r"(?:[/?#][^\s<>|()\[\]]*)?"
PR_REF_URL = r"(?:<{url}{tail}(?:\|[^>]*)?>|\[[^\]]*\]\({url}{tail}\)|{url}{tail})"
PR_REF_SPAN = (
    r"(?:\bPR(?:\s+#{number})?\s*[:—–-]?(?P<mark>[*_`]*)\s*)?(?P<open>[*_`]*)"
    r"(?:\(\s*{ref}\s*\)|{ref})(?P<close>[*_`]*)"
)
OPEN_PR = "Open PR ↗"
FILES_CHANGED = "Files changed ↗"
FILES_PATH = "/files"
PR_ACTION_PREFIX = "kage_pr"

#: An option line in a block reason: a bullet, a numbered item or a lettered one ("a)", "B.").
OPTION_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[A-Za-z][.)])\s+(.+?)\s*$")
#: How the line right before the options must end for them to be choices; a
#: list after "I found:" is evidence, not answers.
QUESTION_END = "?"
#: A yes/no question asking leave to go on ("Shall I proceed?", "OK to continue?"):
#: a list after it is the plan, not answers, unless the question also offers a
#: choice ("Shall I proceed with A or B?"). "How would you like to proceed?" asks
#: for one of the options, so it is not one.
PROCEED_QUESTION = re.compile(
    r"^(?:(?:shall|should|can|may)\s+(?:i|we)|(?:is\s+it\s+)?ok(?:ay)?\s+to)\b.*?\b(?:proceed|continue|go\s+ahead)\b"
    r"|^do\s+you\s+approve\b",
    re.IGNORECASE,
)
#: Where the question's last sentence starts: "Here is the fix. Shall I proceed?"
SENTENCE_START = re.compile(r"(?<=[.!:;])\s+")
CHOICE_WORD = re.compile(r"\b(?:which|or)\b", re.IGNORECASE)
#: Buttons only for two to five options that end the reason, right after a
#: question, each short enough that Slack shows all of it; otherwise the
#: options stay in the text.
OPTIONS_MIN = 2
OPTIONS_MAX = _presenter.BUTTONS_PER_ROW
#: The reason below the question, clipped; ``kanban_block`` puts no bound on it.
DETAIL_MAX = 2000
NEEDS_YOU_ACTION_PREFIX = "kage_needs"
WAITING = "⏸ waiting on you"


def opened_pr(text: str) -> tuple[str, str, str, str] | None:
    """``(url, repo, number, line)`` for the first PR ``text`` says was opened, else None."""
    for line in str(text or "").splitlines():
        for match in PR_URL.finditer(line):
            verb = OPENED_BEFORE_URL.search(line[: match.start()]) or _opened_pr_then(line[: match.start()])
            if not verb:
                continue
            before = line[: verb.start()]
            if NEGATED_VERB.search(before) or not _ours(before):
                continue
            return match.group(0), match.group(2), match.group(3), line.strip()
    return None


def _opened_pr_then(before: str) -> re.Match | None:
    """The last "opened PR" in ``before`` with no sentence end between it and the url."""
    verbs = list(OPENED_PR_THEN.finditer(before))
    if not verbs or GAP_BREAK.search(before, verbs[-1].end()):
        return None
    return verbs[-1]


def _a_name(clause: str) -> bool:
    """Whether ``clause`` only names someone ("Dependabot", "Renovate (bot)"): no
    step or outcome of ours ("Done", "Checked it", "Tests passed", "Following up")."""
    words = _trimmed([w.lower() for w in CLAUSE_WORD.findall(clause)])
    return bool(words) and not any(
        w in OUR_LEAD or w in OUR_VERB or w in OUTCOME_WORD or w.endswith(("ed", "ing")) for w in words
    )


def _trimmed(words: list[str]) -> list[str]:
    start, end = 0, len(words)
    while start < end and (words[start] in FILLER or words[start] == "then"):
        start += 1
    while end > start and words[end - 1] in FILLER:
        end -= 1
    return words[start:end]


def _ours(before: str) -> bool:
    """Whether the verb after ``before`` is the worker's own.

    One rule, read on the sentence the verb is in with :data:`FILLER` words
    trimmed from both ends: it is ours when nothing is left ("- Opened",
    "Done: opened", "Successfully opened") unless the sentence before a colon
    only names someone ("Dependabot: opened"), or only a label ("PR opened"),
    when a comma ends it (the verb opens a clause) and its first clause does
    not only name someone ("Renovate, as usual, opened"), when it ends in "I"/"we" ("I
    have just opened", "Tests pass, so I opened"), or when it ends in
    "and"/"then", starts with "I"/"we" or one of :data:`OUR_VERB` ("Bumped
    values.yaml and opened") and names no :data:`OTHER_SUBJECT` after that.
    Anything else names someone else: "Dependabot opened", "`dependabot[bot]`
    opened", "Ahmed then opened", "bob reviewed and opened", "Checked with
    Bob and he then opened".
    """
    clauses = SENTENCE_BREAK.split(before)
    sentence = clauses[-1]
    # "Dependabot: opened" is a label naming who did it; "Done: opened" is ours.
    if len(clauses) > 1 and not _trimmed([w.lower() for w in CLAUSE_WORD.findall(sentence)]) and _a_name(clauses[-2]):
        return False
    if sentence.rstrip().endswith(","):
        # "Renovate, as usual, opened" names who did it; "Following up <url>, opened" does not.
        return not _a_name(sentence.split(",")[0])
    words = _trimmed([w.lower() for w in CLAUSE_WORD.findall(sentence)])
    if not words or words in PR_LABEL or words[-1] in OUR_LEAD:
        return True
    if words[-1] not in JOIN:
        return False
    while words and words[-1] in JOIN:
        words.pop()
    words = _trimmed(words)
    return (
        bool(words)
        and (words[0] in OUR_LEAD or words[0] in OUR_VERB)
        and not OTHER_SUBJECT.intersection(words[1:])
    )


def _escape(text: str) -> str:
    for raw, escaped in _presenter.MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _with_subline(blocks: list[dict], subline: str) -> list[dict]:
    """``blocks`` with ``subline`` as a context line under the headline, escaped so
    the worker's words cannot mention anyone."""
    if not subline:
        return blocks
    context = {"type": "context", "elements": [{"type": "mrkdwn", "text": _escape(subline)}]}
    return [*blocks[:1], context, *blocks[1:]]


def _text(headline_text: str, subline: str) -> str:
    return "\n".join(part for part in (headline_text, _escape(subline)) if part)


def _ref_markup(match: re.Match) -> str:
    """The markup to keep after the url's reference: a label's closing bold ("**Opened PR:**
    <url>") stays; bold or code wrapped round the url ("**<url>**") goes with it."""
    before, close = (match.group("mark") or "") + (match.group("open") or ""), match.group("close") or ""
    return before[: -len(close)] if close and before.endswith(close) else before + close


def pr_opened(url: str, repo: str, number: str, line: str) -> tuple[list[dict], str]:
    """Blocks and fallback text for an opened PR; ``line`` is the worker's, the url shortened."""
    headline = PR_HEADLINE.format(number=number, repo=repo)
    ref = PR_REF_URL.format(url=re.escape(url), tail=PR_URL_TAIL)
    span = re.compile(PR_REF_SPAN.format(number=number, ref=ref), re.IGNORECASE)
    shortened = span.sub(lambda m: PR_REF.format(number=number) + _ref_markup(m), line)
    evidence = _presenter._clip(shortened.strip(), EVIDENCE_MAX)
    links = [(OPEN_PR, url), (FILES_CHANGED, url + FILES_PATH)]
    blocks = _presenter.blocks_answer(headline, links=links, action_id_prefix=PR_ACTION_PREFIX)
    first, *rest = _presenter.fallback_text(headline, links=links).split("\n")
    return _with_subline(blocks, evidence), "\n".join([_text(first, evidence), *rest])


def _unmarked(option: str) -> str:
    return OPTION_MARKUP.sub(lambda m: m.group(1) or m.group(2), option).strip()


def _headline_text(headline: str) -> str:
    """``headline`` as plain text, stripped of paired markup only, so a glob
    (``app=web-*``) or a dunder name keeps its characters; the presenter's
    ``_plain`` drops every ``*`` and collapses ``__name__``."""
    text = _presenter.HEADING.sub("", headline.strip())
    text = _presenter.LIST_MARKER.sub("", text)
    text = _presenter.MD_LINK.sub(r"\1", text)
    return HEADLINE_MARKUP.sub(lambda m: m.group(1) or m.group(2) or m.group(3), text).strip()


def _trailing_options(lines: Sequence[str]) -> tuple[int, list[str]]:
    """Where the option lines that end ``lines`` start, and their text, when a question
    comes right before them; ``(len(lines), [])`` otherwise."""
    start = len(lines)
    while start > 1 and (not lines[start - 1].strip() or OPTION_LINE.match(lines[start - 1])):
        start -= 1
    # A button is plain text: `code` and **bold** would show their markup.
    options = [_unmarked(m.group(1)) for m in (OPTION_LINE.match(line) for line in lines[start:]) if m]
    # Read as the headline shows it: "**Which cluster?**" still ends in "?".
    question = _headline_text(lines[start - 1])
    if not options or not question.endswith(QUESTION_END):
        return len(lines), []
    asked = SENTENCE_START.split(question)[-1]
    if PROCEED_QUESTION.search(asked) and not CHOICE_WORD.search(asked):
        return len(lines), []
    return start, options


def _live_markup(text: str) -> bool:
    """Whether Slack mrkdwn would read markup in ``text``: a backtick, or a marker the
    presenter's fallback counts as live (``*``/``~`` at a word's edge, a paired ``_``).
    ``node_pool`` and ``2*3`` are text, as they are in the fallback."""
    return "`" in text or bool(_presenter.FALLBACK_LIVE_MARKER.search(text) or _presenter.MD_ITALIC.search(text))


def _question(reason: str, buttons: bool) -> tuple[str, list[str], list[str]]:
    """The reason's first line, the lines after it, and its options when they can be buttons."""
    lines = str(reason or "").strip().splitlines()
    # A line of markup alone (a bare "```") has no text to head the question.
    while lines and (
        not any(ch.isalnum() for ch in _presenter._plain(lines[0])) or _presenter.FENCE.match(lines[0])
    ):
        lines.pop(0)
    if not lines:
        return "", [], []
    start, options = _trailing_options(lines)
    usable = buttons and OPTIONS_MIN <= len(options) <= OPTIONS_MAX and all(
        len(option) <= _presenter.BUTTON_TEXT_MAX for option in options
    )
    if not usable:
        start, options = len(lines), []
    first = lines[0].strip()
    # A clipped headline keeps its whole line below it too.
    below = 0 if len(first) > _presenter.HEADLINE_MAX else 1
    return first, lines[below:start], options


def _detail(lines: Sequence[str]) -> str:
    text = "\n".join(lines).strip()
    if len(text) > DETAIL_MAX:
        text = text[: DETAIL_MAX - len(_presenter.ELLIPSIS)].rstrip() + _presenter.ELLIPSIS
    return text


def needs_you(reason: str, buttons: bool = True) -> tuple[list[dict], str] | None:
    """Blocks and fallback text for a question the work waits on, or None for an empty reason.

    ``buttons`` False keeps the options in the text, as when there are too many.
    """
    headline, rest, options = _question(reason, buttons)
    if not headline:
        return None
    detail = _detail(rest)
    blocks = _with_subline(
        _presenter.blocks_answer(headline, choices=options, action_id_prefix=NEEDS_YOU_ACTION_PREFIX),
        detail,
    )
    blocks.append({
        "type": "context",
        "block_id": _presenter.WAITING_BLOCK_ID,
        "elements": [{"type": "mrkdwn", "text": WAITING}],
    })
    first, *more = _presenter.fallback_text(_presenter._plain(headline), choices=options).split("\n")
    title = _headline_text(headline)
    if title != _presenter._plain(headline) or _live_markup(title):
        # Slack mrkdwn has no escape for "*", so a headline the presenter would
        # change goes out as plain text, unbolded but with its characters.
        title = _presenter._clip(title, _presenter.HEADLINE_MAX)
        blocks[0] = {"type": "section", "text": {"type": "plain_text", "text": title, "emoji": True}}
        first = _escape(title)
    return blocks, "\n".join([_text(first, detail), *more])


def needs_you_settled(blocks: Sequence[dict]) -> list[dict]:
    """A posted question's ``blocks`` once its card has moved on: no choice buttons, no waiting line."""
    out: list[dict] = []
    for block in blocks or ():
        if not isinstance(block, dict) or block.get("block_id") == _presenter.WAITING_BLOCK_ID:
            continue
        if block.get("type") == "actions":
            kept = [
                e for e in block.get("elements") or ()
                if not _presenter.CHOICE_ACTION_ID_PATTERN.search(str((e or {}).get("action_id") or ""))
            ]
            if not kept:
                continue
            block = {**block, "elements": kept}
        out.append(block)
    return out
