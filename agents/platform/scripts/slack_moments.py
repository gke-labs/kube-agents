"""Slack layout for the two moments that get a message of their own.

Pure functions only, like ``slack_presenter``, whose layout this reuses.
``gateway/slack_ux_moments.py`` decides when to post them.

* A pull request the work opened (:func:`pr_opened`), or a GitLab merge
  request: a bold headline naming it, the worker's own line below it as evidence, and "Open PR"
  ("Open MR" for a merge request; the primary button) and "Files changed" url buttons, beside a green side bar
  (:data:`PR_SIDE_BAR`). :func:`proposal_ref` reads one from the url the forge
  answered with when the work opened it, which no wording decides, and
  :func:`evidence_line` finds the worker's line naming it. As the fallback for a
  PR opened any other way, :func:`opened_pr` finds it in a progress note or a report, and
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
  question is text only and a typed reply is the answer. Beside it is a yellow
  side bar (:data:`NEEDS_YOU_SIDE_BAR`), which stays once it is settled while
  anything is left below the headline.

A finished answer that ends by asking which of its options to take gets the same
option rules from :func:`answer_choices`, for ``gateway/slack_ux_answer.py`` under today
and ``chat_message.answer_message`` under next.

Both lay out as one block list; ``slack_presenter.with_side_bar`` splits it
into the headline and the attachment that carries the bar when it is posted.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Sequence

import slack_presenter as _presenter

#: A pull request url on GitHub over https: owner, repo and number, with any
#: trailing path (``/files``, ``#discussion``) left out of the match. Any other
#: host is left to the ordinary line: the headline vouches for the url in the
#: bot's voice and the evidence line hides its host, so a note echoing someone
#: else's link must not become a button. github.com, and the install's GitHub
#: Enterprise Server host when it has one (``_pr_url``), and no other host.
PR_URL_TEMPLATE = r"https://(?:{hosts})/([^\s/<>|]+)/([^\s/<>|]+)/pull/(\d+)"
PR_URL = re.compile(PR_URL_TEMPLATE.format(hosts=r"github\.com"))
#: A GitLab merge request url over https: host, the project (the last segment
#: of its path, groups and subgroups before it) and number, as groups 1 to 3,
#: so the project and number sit where :data:`PR_URL` has repo and number. A
#: port is allowed and left out of the host; a tail (``/diffs``, ``#note_1``)
#: is left out of the match. Only a host in :data:`GITLAB_HOST` or the
#: caller's own hosts is announced, for :data:`PR_URL`'s reason.
#: The path that marks a merge request url; a text without it holds none.
MR_MARKER = "/-/merge_requests/"
MR_URL = re.compile(r"https://([^\s/<>|:@]+)(?::\d+)?/(?:[^\s/<>|]+/)+([^\s/<>|]+)" + re.escape(MR_MARKER) + r"(\d+)")
#: ``repo_ref.GITLAB_CANONICAL_HOST``, spelt out: this module imports nothing but
#: ``slack_presenter``, and the image-build check stages no ``repo_ref`` beside it.
GITLAB_HOST = "gitlab.com"


_PR_URLS: dict[str, re.Pattern] = {}


def _pr_url(github_host: str) -> re.Pattern:
    """:data:`PR_URL` for an install whose GitHub host is ``github_host``."""
    host = github_host.strip().lower()
    if not host or host == "github.com":
        return PR_URL
    if host not in _PR_URLS:
        _PR_URLS[host] = re.compile(PR_URL_TEMPLATE.format(hosts=r"github\.com|" + re.escape(host)))
    return _PR_URLS[host]


#: What must sit right before the url for a line to announce the PR as ours:
#: the verb, then optionally "a"/"the", "new", "PR"/"pull request" with its
#: number, a colon, dash or "(", and the opening of a ``[label](`` or ``<`` link.
#: A label's colon may follow the verb ("Opened: <url>"), its closing bold or
#: code may sit before the url ("**Opened PR:** <url>"), and so may bold or code
#: around the url itself ("Opened PR **<url>**"). "draft" may join "new".
#: What may sit between the verb and "PR": "a"/"the", then "new" and "draft".
PR_ARTICLE = r"(?:(?:an?|the)\s+)?(?:(?:new|draft)\s+){0,2}"
OPENED_VERB = r"\b(?:opened|created|raised|filed|submitted)"
#: A label's colon or markup after the verb, then the space before what follows it.
VERB_LABEL = r"(?:[*_`]*:)?[*_`]*\s+"
#: "PR"/"pull request" with its "#412", and "MR"/"merge request" with its "!12".
PR_NAMED = r"(?:PR|pull\s+request)(?:\s*#\d+)?"
MR_NAMED = r"(?:MR|merge\s+request)(?:\s*!\d+)?"
#: A colon, dash or "(" after that name, with any markup round it.
NAME_LABEL = r"\s*[:(—–-]?[*_`]*\s*"
#: Markup and the opening of a ``[label](`` or ``<`` link, right before the url.
URL_OPENING = r"[*_`]*(?:\[[^\]]*\]\(|<)?$"
OPENED_BEFORE_URL = re.compile(
    OPENED_VERB + VERB_LABEL + PR_ARTICLE + "(?:" + PR_NAMED + NAME_LABEL + ")?" + URL_OPENING,
    re.IGNORECASE,
)
#: The same for a merge request.
MR_OPENED_BEFORE_URL = re.compile(
    OPENED_VERB + VERB_LABEL + PR_ARTICLE + "(?:" + MR_NAMED + NAME_LABEL + ")?" + URL_OPENING,
    re.IGNORECASE,
)
#: The verb and "PR" with more words before the url, in the same sentence:
#: "Opened PR #412 in acme/x: <url>", "Opened a PR against main: <url>".
OPENED_PR_THEN = re.compile(
    OPENED_VERB + r"[*_`]*\s+" + PR_ARTICLE
    + r"(?:PR|pull\s+request)\b(?:\s*#(\d+))?",
    re.IGNORECASE,
)
#: The same for a merge request: "Opened MR !12 in platform/infra: <url>".
MR_OPENED_THEN = re.compile(
    OPENED_VERB + r"[*_`]*\s+" + PR_ARTICLE
    + r"(?:MR|merge\s+request)\b(?:\s*!(\d+))?",
    re.IGNORECASE,
)
#: A sentence end between "PR" and the url; a colon does not end it here.
GAP_BREAK = re.compile(r"[.!?](?=[*_`]*\s+[A-Z])|;(?=[*_`]*\s)")
#: Those words end in a colon or dash that labels the url, so a url the
#: sentence only cites ("Opened PR #412, which reverts <url>") is not the one.
GAP_LABEL = re.compile(r"[:—–-][*_`]*\s*[*_`]*(?:\[[^\]]*\]\(|<)?$")
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
    "done", "update", "next", "result", "ready", "green", "complete", "completed", "finished", "passed",
})
#: A label naming the PR with no subject: "PR opened: <url>".
PR_LABEL = (["pr"], ["pull", "request"])
MR_LABEL = (["mr"], ["merge", "request"])
#: How each kind of url is read as opened: the pattern right before it, the
#: one with more words between, and the label that may stand for the subject.
PR_RULES = (OPENED_BEFORE_URL, OPENED_PR_THEN, PR_LABEL)
MR_RULES = (MR_OPENED_BEFORE_URL, MR_OPENED_THEN, MR_LABEL)
#: Someone else as the subject after the worker's first step: "Checked with Bob
#: and he then opened", "Confirmed with Alice, who then opened". Not "it", the
#: object of "Fixed it and opened".
OTHER_SUBJECT = frozenset({"he", "she", "they", "who", "which"})
#: The worker's own earlier steps. A list rather than "-ed", which would take
#: "Ahmed then opened" and "Fred reviewed and opened" as ours.
OUR_VERB = frozenset({
    "added", "addressed", "adjusted", "analysed", "analyzed", "applied", "audited", "began", "bumped", "built", "cherry-picked", "cordoned",
    "changed", "checked", "cleaned", "closed", "committed", "compared", "confirmed", "corrected",
    "created", "debugged", "decreased", "deleted", "deployed", "diagnosed", "did", "disabled", "documented",
    "drafted", "drained", "dropped", "edited", "enabled", "fetched", "filed", "fixed", "following", "found", "generated", "got",
    "identified", "implemented", "increased", "inspected", "installed", "investigated", "investigating", "kept",
    "looked", "lowered", "made", "merged", "migrated", "modified", "moved", "opened", "patched",
    "pinned", "prepared", "proposed", "provisioned", "pulled", "pushed", "put", "raised", "ran", "re-ran", "read", "rebased",
    "rebuilt", "reconciled", "recreated", "redeployed", "reduced", "refactored", "refreshed", "regenerated", "released", "removed", "renamed", "replaced",
    "reproduced", "reran", "resized", "resolved", "restarted", "restored", "retried", "reverted", "reviewed", "rewrote", "rolled", "rotated",
    "scaled", "sent", "set", "shipped", "split", "submitted", "superseded", "synced", "tagged", "tested", "took", "traced", "triggered",
    "tuned", "uncordoned", "updated", "updating", "upgraded", "validated", "verified", "working", "wrote",
})
#: Steps a bot or a person gets credit for with "by": "Triggered by Renovate" is
#: Renovate's, while "Fixed by hand" and "Scaled by 2x" are ours.
PASSIVE_BY = "by"
CREDITED_VERB = frozenset({
    "created", "dropped", "filed", "generated", "merged", "raised", "retried", "reviewed", "rotated",
    "submitted", "synced", "tagged", "triggered",
})
#: The worker's line under the headline, clipped: a note can be one long
#: paragraph, and a Slack context element holds at most 3,000 characters.
EVIDENCE_MAX = 300
#: Markup a button or headline cannot show, stripped with its pair only, so a
#: glob (``app=web-*``) or a dunder name (``__init__``) keeps its characters:
#: ``code``, ``**bold**``, a paired ``*emphasis*`` and ``_emphasis_``.
PAIRED_MARKUP = re.compile(
    r"`([^`]+)`|\*\*([^*]+)\*\*|(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])"
    r"|(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])"
)

PR_HEADLINE = "I opened PR #{number} in {repo}. It's yours to review."
PR_REF = "PR #{number}"
#: The url in the worker's line, with a "PR" or "PR #412:" already in front of
#: it, so "Opened PR <url>" reads "Opened PR #412" rather than "Opened PR PR
#: #412". A Slack ``<url|label>``, markdown ``[label](url)`` or ``(url)``
#: around it goes too, and so does a tail after the number (``/files``).
PR_URL_TAIL = r"(?:[/?#][^\s<>|()\[\]]*)?"
#: ``(?!\d)`` ends the url at its number, so PR 9's url does not match inside PR 91's.
URL_NUMBER_END = r"(?!\d)"
PR_REF_URL = (
    r"(?:<{url}" + URL_NUMBER_END + r"{tail}(?:\|[^>]*)?>|\[[^\]]*\]\({url}" + URL_NUMBER_END
    + r"{tail}\)|{url}" + URL_NUMBER_END + r"{tail})"
)
PR_REF_SPAN = (
    r"(?:\b{lead}\s*[:—–-]?(?P<mark>[*_`]*)\s*)?(?P<open>[*_`]*)"
    r"(?:\(\s*{ref}\s*\)|{ref})(?P<close>[*_`]*)"
)
PR_LEAD = r"PR(?:\s+#{number})?"
OPEN_PR = "Open PR ↗"
PR_SIDE_BAR = _presenter.SIDE_BAR_GREEN
FILES_CHANGED = "Files changed ↗"
FILES_PATH = "/files"
MR_HEADLINE = "I opened merge request !{number} in {repo}. It's yours to review."
OPEN_MR = "Open MR ↗"
MR_REF = "MR !{number}"
#: Unlike "PR", "merge request" is folded too: "Opened merge request !12: <url>" reads
#: "Opened MR !12" rather than repeating the number.
MR_LEAD = r"(?:MR|merge\s+request)(?:\s+!{number})?"
DIFFS_PATH = "/diffs"
#: "a" before a folded "MR !12", which reads "an MR !12": "I opened a merge request: <url>".
A_BEFORE_MR = re.compile(r"\b([Aa])(?=\s+[*_`]*MR !)")
#: Headline, reference, the reference's lead before the url, the primary
#: button's label and the files path.
PR_LAYOUT = (PR_HEADLINE, PR_REF, PR_LEAD, OPEN_PR, FILES_PATH)
MR_LAYOUT = (MR_HEADLINE, MR_REF, MR_LEAD, OPEN_MR, DIFFS_PATH)
PR_ACTION_PREFIX = "kage_pr"
#: A line naming a PR or merge request by its number alone ("PR #99", "MR !12"), as
#: :func:`evidence_line` finds the worker's line about one it opened.
PR_NUMBER_REF = r"(?<![\w#!])#{number}\b"
MR_NUMBER_REF = r"(?<![\w#!])!{number}\b"

#: An option line in a block reason: a bullet, a numbered item or a lettered one ("a)", "B.").
OPTION_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[A-Za-z][.)])\s+(.+?)\s*$")
#: How the line right before the options must end for them to be choices; a
#: list after "I found:" is evidence, not answers.
QUESTION_END = "?"
#: A yes/no question asking leave to go on ("Shall I proceed?", "OK to continue?",
#: "Sound good?", "Does the fix look right?"): a list after it is the plan, not
#: answers, unless the question also offers a choice ("Shall I proceed with A or
#: B?"). "How would you like to proceed?" asks for one of the options, so it is not one.
PROCEED_QUESTION = re.compile(
    r"^(?:(?:shall|should|can|may)\s+(?:i|we)|(?:is\s+it\s+)?ok(?:ay)?\s+to)\b.*?\b(?:proceed|continue|go\s+ahead)\b"
    r"|^do\s+you\s+approve\b"
    # The informal asks only as the whole sentence: "Looks good, what next?" asks something else.
    r"|^(?:(?:does\s+(?:this|that|it|the)|do\s+(?:these|they|those|the))(?:\s+[\w-]+){0,2}\s+)?(?:sounds?|looks?)\s+(?:good|ok(?:ay)?|right|fine)"
    r"(?:\s+to\s+you)?\s*\?$"
    r"|^(?:ok(?:ay)?|agreed|any\s+objections)\s*\?$",
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
#: Bold in a context line: ``**text**`` between word boundaries with no ``*`` inside,
#: which mrkdwn would read as a marker. ``__x__`` stays text, so a ``__init__.py`` in a
#: path or url keeps its characters.
DETAIL_BOLD = re.compile(r"(?<![\w*])\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*(?![\w*])")
NEEDS_YOU_ACTION_PREFIX = "kage_needs"
WAITING = "⏸ waiting on you"
NEEDS_YOU_SIDE_BAR = _presenter.SIDE_BAR_YELLOW

#: A finished answer's options (:func:`answer_choices`): a list item, its indent, marker and text;
#: the bold lead a label is taken from; the "(Recommended)" that makes an option the primary one
#: and leaves its label; and an "Option 2:" numbering, which a button does not need.
ITEM_LINE = re.compile(r"^( {0,3})([-*+•]|\d+[.)]|[A-Za-z][.)])\s+(\S.*?)\s*$")
BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*|^__(.+?)__")
RECOMMENDED = re.compile(r"\s*\(\s*recommended\s*\)", re.IGNORECASE)
OPTION_NUMBER = re.compile(r"^option\s+(?:\d+|[a-z])\b\s*[:.)\-–—]?\s*", re.IGNORECASE)
#: A yes/no question's opening word ("Want me to scale it?", "Should I apply it?"): under a finished
#: answer's list it offers the list as the plan, not as options, unless it also says "which" or "or".
YES_NO_OPENER = re.compile(
    r"^(?:shall|should|can|could|may|might|will|would|do|does|did|is|are|am|was|were|want|have|has|ok|okay"
    r"|sounds?|looks?)\b",
    re.IGNORECASE,
)
#: Words that ask to choose among a list, for a closing question with no "which" or "or":
#: "How would you like to proceed?", "Do you have a preference?" is a yes/no opener and not one.
CHOICE_HINT = re.compile(r"\b(?:prefer|preference|options?|choose|pick)\b|^how\s+would\s+you\s+like\b", re.IGNORECASE)
#: How many item lines :func:`_list_start` reads up from an answer's end before it gives up, nested
#: items included: well past five options with sub-bullets, and it keeps the scan linear.
LIST_SCAN_MAX = 50
#: What may sit between a bold lead and the rest of its line ("**Fix it**: …", "**Fix it** — …").
LEAD_JOINER = " :-–—"
#: An option shown under the answer: its marker, its label in bold, and the first sentence
#: of what the answer said about it.
SHOWN_OPTION = "{marker} **{label}**"
SHOWN_SUMMARY = "{line} — {summary}"
#: A line that is not prose: a quote or a table row, beside the presenter's headings, items and fences.
NOT_PROSE = re.compile(r"^\s*[>|]")


def opened_pr(
    text: str, gitlab_hosts: Collection[str] = frozenset(), github_host: str | None = None
) -> tuple[str, str, str, str] | None:
    """``(url, repo, number, line)`` for the first PR ``text`` says was opened, else None."""
    return next(opened_prs(text, gitlab_hosts, github_host), None)


def opened_prs(
    text: str, gitlab_hosts: Collection[str] = frozenset(), github_host: str | None = None
) -> Iterator[tuple[str, str, str, str]]:
    """``(url, repo, number, line)`` for each PR ``text`` says was opened, in order.

    A GitLab merge request counts too, on :data:`GITLAB_HOST` or one of
    ``gitlab_hosts``, the self-managed hosts the caller vouches for; its
    ``repo`` is the project. :func:`pr_opened` tells the two apart by the url.
    ``github_host`` is the install's GitHub host, which the caller reads; None
    is github.com. This module reads nothing but its arguments.
    """
    hosts = {GITLAB_HOST, *(host.lower() for host in gitlab_hosts)}
    pr_url = _pr_url(github_host or "github.com")
    for line in str(text or "").splitlines():
        for match, (before_url, then, labels) in _proposals(line, hosts, pr_url):
            verb = before_url.search(line[: match.start()]) or _opened_pr_then(
                line[: match.start()], match.group(3), then
            )
            if not verb:
                continue
            before = line[: verb.start()]
            if NEGATED_VERB.search(before) or not _ours(before, labels):
                continue
            yield match.group(0), match.group(2), match.group(3), line.strip()


def proposal_ref(
    url: str, gitlab_hosts: Collection[str] = frozenset(), github_host: str | None = None
) -> tuple[str, str, str] | None:
    """``(url, repo, number)`` for the PR or merge request at ``url``, else None.

    For a url a forge answered with when the work opened it, so no wording is
    read: a PR on github.com or ``github_host``, or a merge request on
    :data:`GITLAB_HOST` or one of ``gitlab_hosts``, the hosts :func:`opened_prs`
    would announce, with any tail left out as it leaves it out.
    """
    text = str(url or "").strip()
    match = _pr_url(github_host or "github.com").match(text)
    if match is None:
        match = MR_URL.match(text)
        hosts = {GITLAB_HOST, *(host.lower() for host in gitlab_hosts)}
        if match is None or match.group(1).lower() not in hosts:
            return None
    return match.group(0), match.group(2), match.group(3)


def evidence_line(text: str, url: str, number: str) -> str:
    """The first line of ``text`` naming the PR at ``url``, by its url or its number, else ""."""
    ref = re.compile((MR_NUMBER_REF if MR_URL.match(url) else PR_NUMBER_REF).format(number=re.escape(number)))
    named = re.compile(re.escape(url) + URL_NUMBER_END)
    for line in str(text or "").splitlines():
        if named.search(line) or ref.search(line):
            return line.strip()
    return ""


def _proposals(
    line: str, gitlab_hosts: Collection[str], pr_url: re.Pattern = PR_URL
) -> list[tuple[re.Match, tuple]]:
    """Each PR url (on the hosts ``pr_url`` matches) and merge request url on
    ``gitlab_hosts`` in ``line``, in order, with its rules."""
    found = [(match, PR_RULES) for match in pr_url.finditer(line)]
    found += [(match, MR_RULES) for match in MR_URL.finditer(line) if match.group(1).lower() in gitlab_hosts]
    return sorted(found, key=lambda item: item[0].start())


def _opened_pr_then(before: str, number: str, then: re.Pattern) -> re.Match | None:
    """The last "opened PR" in ``before`` with no sentence end between it and the
    url, a label just before the url, and no other PR number named."""
    verbs = list(then.finditer(before))
    if not verbs:
        return None
    verb = verbs[-1]
    if GAP_BREAK.search(before, verb.end()) or not GAP_LABEL.search(before, verb.end()):
        return None
    return verb if verb.group(1) in (None, number) else None


def _a_name(clause: str) -> bool:
    """Whether ``clause`` only names someone ("Dependabot", "Renovate (bot)"): no
    step or outcome of ours ("Done", "Checked it", "Tests passed", "Following up")."""
    words = _trimmed([w.lower() for w in CLAUSE_WORD.findall(clause)])
    return bool(words) and (_passive(words) or not any(
        w in OUR_LEAD or w in OUR_VERB or w in OUTCOME_WORD for w in words
    ))


def _passive(words: list[str]) -> bool:
    """Whether ``words`` credit a step to someone: "Triggered by Renovate", not "Fixed by hand"."""
    return any(word in CREDITED_VERB and nxt == PASSIVE_BY for word, nxt in zip(words, words[1:]))


def _trimmed(words: list[str]) -> list[str]:
    start, end = 0, len(words)
    while start < end and (words[start] in FILLER or words[start] == "then"):
        start += 1
    while end > start and words[end - 1] in FILLER:
        end -= 1
    return words[start:end]


def _ours(before: str, labels: tuple) -> bool:
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
    if not words or words in labels or words[-1] in OUR_LEAD:
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
        and not _passive(words)
    )


def _escape(text: str) -> str:
    for raw, escaped in _presenter.MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _link(match: re.Match) -> str:
    label, url = match.group(1), match.group(2)
    if not _presenter._safe_link_url(url):
        return _escape(match.group(0))
    return f"<{_presenter._link_url(url)}|{_escape(label)}>"


def _bolded(text: str) -> str:
    """``text`` escaped, each :data:`DETAIL_BOLD` run as mrkdwn's single-star bold."""
    return DETAIL_BOLD.sub(r"*\1*", _escape(text))


def _subline_mrkdwn(subline: str, links: bool = True) -> str:
    """``subline`` escaped with its bold as mrkdwn bold outside code spans; with
    ``links``, each Markdown link there as a mrkdwn link."""
    held, spans = _presenter._hold_code(subline.replace("\x00", ""))
    parts, last = [], 0
    for match in _presenter.MD_LINK.finditer(held) if links else ():
        parts += [_bolded(held[last : match.start()]), _link(match)]
        last = match.end()
    text = "".join(parts) + _bolded(held[last:])
    return _presenter.CODE_PLACEHOLDER.sub(lambda m: _escape(spans[int(m.group(1))][0]), text)


def _with_subline(blocks: list[dict], subline: str, links: bool = False) -> list[dict]:
    """``blocks`` with ``subline`` as a context line under the headline, escaped so
    the worker's words cannot mention anyone; with ``links``, a Markdown link stays
    a link. An opened PR's line keeps its links as text, for :data:`PR_URL`'s reason."""
    if not subline:
        return blocks
    text = _subline_mrkdwn(subline, links)
    context = {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
    return [*blocks[:1], context, *blocks[1:]]


def _text(headline_text: str, subline: str) -> str:
    return "\n".join(part for part in (headline_text, _escape(subline)) if part)


def _ref_markup(match: re.Match) -> str:
    """The markup to keep after the url's reference: a label's closing bold ("**Opened PR:**
    <url>") stays; bold or code wrapped round the url ("**<url>**") goes with it."""
    before, close = (match.group("mark") or "") + (match.group("open") or ""), match.group("close") or ""
    return before[: -len(close)] if close and before.endswith(close) else before + close


def pr_opened(url: str, repo: str, number: str, line: str) -> tuple[list[dict], str]:
    """Blocks and fallback text for an opened PR or merge request; ``line`` is the
    worker's, the url shortened."""
    merge_request = MR_URL.match(url) is not None
    headline_format, ref_format, lead, open_label, files_path = MR_LAYOUT if merge_request else PR_LAYOUT
    headline = headline_format.format(number=number, repo=repo)
    ref = PR_REF_URL.format(url=re.escape(url), tail=PR_URL_TAIL)
    span = re.compile(PR_REF_SPAN.format(lead=lead.format(number=number), ref=ref), re.IGNORECASE)
    shortened = span.sub(lambda m: ref_format.format(number=number) + _ref_markup(m), line)
    if merge_request:
        shortened = A_BEFORE_MR.sub(r"\1n", shortened)
    evidence = _presenter._clip(shortened.strip(), EVIDENCE_MAX)
    links = [(open_label, url), (FILES_CHANGED, url + files_path)]
    blocks = _presenter.blocks_answer(headline, links=links, action_id_prefix=PR_ACTION_PREFIX, primary_link=True)
    first, *rest = _presenter.fallback_text(headline, links=links).split("\n")
    return _with_subline(blocks, evidence), "\n".join([_text(first, evidence), *rest])


def _unmarked(text: str) -> str:
    return PAIRED_MARKUP.sub(lambda m: next(g for g in m.groups() if g is not None), text).strip()


def _headline_text(headline: str) -> str:
    """``headline`` as plain text, stripped of paired markup only, so a glob
    (``app=web-*``) or a dunder name keeps its characters; the presenter's
    ``_plain`` drops every ``*`` and collapses ``__name__``."""
    text = _presenter.HEADING.sub("", headline.strip())
    text = _presenter.LIST_MARKER.sub("", text)
    text = _presenter.MD_LINK.sub(r"\1", text)
    return _unmarked(text)


def _trailing_options(lines: Sequence[str]) -> tuple[int, list[str]]:
    """Where the option lines that end ``lines`` start, and their text, when a question
    comes right before them; ``(len(lines), [])`` otherwise."""
    start = len(lines)
    while start > 1 and (not lines[start - 1].strip() or OPTION_LINE.match(lines[start - 1])):
        start -= 1
    # A button is plain text: `code` and **bold** would show their markup.
    # A linked option shows, and answers with, its label, as the headline does.
    options = [_unmarked(_presenter.MD_LINK.sub(r"\1", m.group(1))) for m in (OPTION_LINE.match(line) for line in lines[start:]) if m]
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


def _first_text_line(lines: Sequence[str], fences: bool) -> int | None:
    """The index of the first line with text, outside a fence when ``fences``."""
    fence = None
    for index, line in enumerate(lines):
        opened = _presenter.next_fence(line, fence) if fences else None
        # A line inside a fence, an opener or a closer is no headline; a line
        # that opens with a code span ("```x``` is down. Which?") is text.
        stray = not fences and _presenter._opens_fence(line)
        if fence is None and opened is None and not stray and any(
            ch.isalnum() for ch in _presenter._plain(line)
        ):
            return index
        fence = opened
    return None


def _question(reason: str, buttons: bool) -> tuple[str, list[str], list[str]]:
    """The reason's first line, the lines after it, and its options when they can be buttons."""
    lines = str(reason or "").strip().splitlines()
    # The headline is the first line of text outside a fence; a line of markup
    # alone (a bare "```") has none, and a fenced block before it stays in the
    # detail. A fence that never closes is a stray opener, not code.
    head = _first_text_line(lines, fences=True)
    before = lines[:head] if head is not None and any(_presenter.FENCE.match(line) for line in lines[:head]) else []
    if head is None:
        head = _first_text_line(lines, fences=False)
    if head is None:
        return "", [], []
    lines = lines[head:]
    start, options = _trailing_options(lines)
    usable = buttons and OPTIONS_MIN <= len(options) <= OPTIONS_MAX and all(
        len(option) <= _presenter.BUTTON_TEXT_MAX for option in options
    )
    if not usable:
        start, options = len(lines), []
    first = lines[0].strip()
    # A clipped headline keeps its whole line below it too, measured as shown, and so
    # does one with a link: the headline shows only the label, the line below the url.
    below = 0 if len(_headline_text(first)) > _presenter.HEADLINE_MAX or _presenter.MD_LINK.search(first) else 1
    # Linked options likewise stay in the detail: their buttons show only the label.
    end = len(lines) if any(_presenter.MD_LINK.search(line) for line in lines[start:]) else start
    return first, [*before, *lines[below:end]], options


def _detail(lines: Sequence[str]) -> str:
    """``lines`` as one text, clipped so that it is at most :data:`DETAIL_MAX` once
    escaped: Slack counts ``&amp;``, ``&lt;`` and ``&gt;`` against the limit."""
    text = "\n".join(lines).strip()
    if len(_escape(text)) <= DETAIL_MAX:
        return text
    budget, end = DETAIL_MAX - len(_presenter.ELLIPSIS), 0
    for ch in text:
        budget -= len(_escape(ch))
        if budget < 0:
            break
        end += 1
    return text[:end].rstrip() + _presenter.ELLIPSIS


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
        links=True,
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


def needs_you_settled(blocks: Sequence[dict], note: str = "") -> list[dict]:
    """A posted question's ``blocks`` once its card has moved on: no choice buttons, no waiting line,
    and ``note``, the line naming who answered, as a context line last when there is one."""
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
    if note:
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return out


# --- a finished answer's options --------------------------------------------


def _not_prose(line: str) -> bool:
    """Whether ``line`` is a heading, a list item, a fence, a quote or a table row."""
    return bool(
        _presenter.HEADING.match(line) or _presenter.LIST_MARKER.match(line) or ITEM_LINE.match(line)
        or _presenter._opens_fence(line) or NOT_PROSE.match(line)
    )


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _marker_kind(marker: str) -> tuple[bool, bool]:
    """A numbered, lettered or bulleted marker; one list keeps to one kind."""
    return marker[0].isdigit(), marker[0].isalpha()


def _items(lines: Sequence[str]) -> list[tuple[str, str, list[str]]] | None:
    """``lines`` as one list's items, each ``(marker, text, continuation lines)``, or None when they are not one.

    The first line is an item. Every later line is blank, an item of the same kind at the same
    indent, or indented deeper, which continues the item above it (a nested list, a fence).
    """
    first = ITEM_LINE.match(lines[0]) if lines else None
    if not first:
        return None
    indent, kind = len(first.group(1)), _marker_kind(first.group(2))
    items: list[tuple[str, str, list[str]]] = []
    for line in lines:
        item = ITEM_LINE.match(line)
        if item and len(item.group(1)) == indent and _marker_kind(item.group(2)) == kind:
            items.append((item.group(2), item.group(3), []))
        elif not line.strip() or _indent(line) > indent:
            items[-1][2].append(line)
        else:
            return None
    return items


def _list_start(lines: Sequence[str]) -> int | None:
    """Where the longest list that ends ``lines`` starts, or None when they do not end in one or
    it runs past :data:`LIST_SCAN_MAX` item lines, far more than an offer of options holds."""
    found, seen = None, 0
    for start in range(len(lines) - 1, -1, -1):
        line = lines[start]
        if ITEM_LINE.match(line):
            seen += 1
            if seen > LIST_SCAN_MAX:
                return None
            if _items(lines[start:]) is not None:
                found = start
        elif line.strip() and not _indent(line):
            break
    return found


def _asks_choice(question: str) -> bool:
    """Whether ``question`` asks for one of the options: it ends in a question mark, and its
    last sentence says "which" or "or", or asks to choose (:data:`CHOICE_HINT`) without being
    a yes/no ask to go on (:func:`_trailing_options`) or any other yes/no question
    (:data:`YES_NO_OPENER`). "Anything else?" under a list of findings asks for no choice."""
    text = _presenter._plain(question)
    if not text.endswith(QUESTION_END):
        return False
    asked = SENTENCE_START.split(text)[-1]
    if CHOICE_WORD.search(asked):
        return True
    return not (PROCEED_QUESTION.search(asked) or YES_NO_OPENER.match(asked)) and bool(CHOICE_HINT.search(asked))


def _option(marker: str, text: str, continuation: Sequence[str]) -> tuple[str, bool, str, bool] | None:
    """``(label, primary, shown line, whole)`` for one option, or None when it cannot be a button.

    The label is the item's bold lead, else its whole first line, which must then fit a
    button; "(Recommended)" makes it primary and an "Option 2:" numbering is dropped. The
    shown line is the label in bold and the first sentence the item says after it, or empty
    when it says nothing more than its label. ``whole`` is whether the label and the shown
    line carry all the item says.
    """
    lead = BOLD_LEAD.match(text)
    said = text[lead.end():] if lead else ""
    # "**Fix it** (Recommended): …" marks the option outside its bold as well.
    marked = RECOMMENDED.match(said)
    said = said[marked.end():].lstrip(LEAD_JOINER) if marked else said.lstrip(LEAD_JOINER)
    label = _presenter._plain(lead.group(1) or lead.group(2) if lead else text)
    primary = bool(marked or RECOMMENDED.search(label))
    label = " ".join(OPTION_NUMBER.sub("", RECOMMENDED.sub("", label)).split()).rstrip(":").strip()
    if not label or (not lead and len(label) > _presenter.BUTTON_TEXT_MAX):
        return None
    clipped = _presenter._clip(label, _presenter.BUTTON_TEXT_MAX)
    whole_label, label = clipped == label, clipped
    if not said:
        prose = []
        for line in continuation:
            if not line.strip() or _not_prose(line.strip()):
                break
            prose.append(line.strip())
        said = " ".join(prose)
    held, spans = _presenter._hold_code(said)
    summary = _presenter._restore_code(_presenter._first_sentence(held)[0], spans).strip()
    shown = SHOWN_SUMMARY.format(line=SHOWN_OPTION.format(marker=marker, label=label), summary=summary)
    whole = whole_label and not any(line.strip() for line in continuation) and summary == said.strip()
    return label, primary, shown if summary else "", whole


def answer_choices(rest: str, buttons: bool = True) -> tuple[str, str, str, list[tuple[str, bool]]] | None:
    """A finished answer's options as choices: ``(shown, folded, question, options)``, or None.

    ``rest`` is the answer after its headline, as markdown. It offers choices when it ends with
    a question over two to five options, as a needs-you reason does (:func:`_question`): the
    options end it right under the question, or the question is its last paragraph right
    under them. The question asks for one (:func:`_asks_choice`), and every option makes a
    label (:func:`_option`), none twice. ``options`` are ``(label, primary)``, the first
    recommended one primary. ``shown`` is a prose paragraph that opens ``rest``, then each
    option's shown line when every option has one; ``folded`` is everything else before the options, then the options
    as written when any says more than its first line, for a fold; ``question`` is the question.
    With ``buttons`` False, as outside a thread, nothing carries the labels, so ``shown`` holds
    the options as written and ``folded`` only what came before them.
    """
    lines = (rest or "").strip("\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    para = len(lines)
    while para and lines[para - 1].strip():
        para -= 1
    closing = lines[para:]
    question = " ".join(line.strip() for line in closing)
    if closing and not any(_not_prose(line) for line in closing) and _asks_choice(question):
        before = lines[:para]
        while before and not before[-1].strip():
            before.pop()
        start = _list_start(before)
        region, head = (before[start:], before[:start]) if start is not None else ([], [])
    else:
        start = _list_start(lines)
        asked = (start or 0) - 1
        while asked >= 0 and not lines[asked].strip():
            asked -= 1
        if start is None or asked < 0 or _not_prose(lines[asked]) or not _asks_choice(lines[asked]):
            return None
        question, region, head = lines[asked].strip(), lines[start:], lines[:asked]
    items = _items(region)
    if not items or not OPTIONS_MIN <= len(items) <= OPTIONS_MAX:
        return None
    options = [_option(*item) for item in items]
    if any(option is None for option in options):
        return None
    labels = [label.casefold() for label, _primary, _shown, _whole in options]
    if len(set(labels)) != len(labels):
        return None
    first = next((i for i, (_label, primary, _shown, _whole) in enumerate(options) if primary), None)
    head = list(head)
    while head and not head[-1].strip():
        head.pop()
    lead_end = next((i for i, line in enumerate(head) if not line.strip()), len(head))
    lead = head[:lead_end] if head and not _not_prose(head[0]) else []
    folded = head[len(lead):]
    if not buttons:
        shown = "\n\n".join(part for part in ("\n".join(lead).strip(), "\n".join(region).strip()) if part)
        return shown, "\n".join(folded).strip(), question, [
            (label, i == first) for i, (label, _primary, _shown, _whole) in enumerate(options)
        ]
    # The options' lines show only when each says more than its button does.
    lines = [line for _label, _primary, line, _whole in options]
    shown_all = all(lines)
    # The options fold as written whenever what is in sight does not carry all they say.
    if any(not whole or (line and not shown_all) for _label, _primary, line, whole in options):
        folded = [*folded, "", *region]
    shown = "\n\n".join(part for part in ("\n".join(lead).strip(), "\n".join(lines) if shown_all else "") if part)
    return shown, "\n".join(folded).strip(), question, [
        (label, i == first) for i, (label, _primary, _shown, _whole) in enumerate(options)
    ]
