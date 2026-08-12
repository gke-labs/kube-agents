#!/usr/bin/env python3
"""What counts as addressing the agent on a pull request, and what it has answered.

The layer between `forge.py` (forge mechanism — the five API calls) and its two
consumers (the `pr_comments` sweep in `github_scan_gate.py`, and the
`pr-conversation` worker skill). Nothing here talks to a network. Everything
here is policy that stays the same whichever forge the comment came from, which
is why it is not in `forge.py`.

Three questions, and why each is answered the way it is
-------------------------------------------------------
**Was the agent addressed?** Only by an explicit `/agent …` line or an
`@<login>` mention. Review threads are mostly humans talking to each other, and
a watcher that woke the model for "looks good to me" would spend a turn on every
comment in the repository. Explicitness is also what makes the trigger auditable
after the fact: there is a line you can point at.

**Was it addressed, or merely discussed?** Fenced code blocks and inline code
spans come out first. The single likeliest thing to appear in a review comment
about this feature is the command itself — "you can type `/agent …` here" — and
firing on that would make documenting the feature impossible. The fence parser
follows CommonMark rather than the obvious non-greedy regex, for the reason
`strip_fenced_blocks` sets out.

**Has it already answered?** By its own marker in its own comment, and nothing
else. There is no state file, no database, no label: a request is unanswered
when no comment written by the agent on that pull request carries
`<!-- agent-answered:<node-id> -->`. Counting only *self-authored* markers is
load-bearing — a marker scan that trusted any comment would let anyone suppress
a request by pasting the string, which is the same trap
`docs/designs/fleet-audit-issue-ledger.md` §3.1 records for the issue ledger.

The human's comment is never edited and never consumed. Whatever the agent
learns about a conversation, it learns by re-reading it.

On the two copied helpers
-------------------------
`strip_fenced_blocks` and `strip_inline_code` are copies of
`fleet-audit/scripts/audit_report.py`'s, not references to them: a shared module
must not import from a skill, which may not be installed and is not on the path
at import time. Two copies can drift, so `test_pr_triggers.py` runs both against
one corpus and fails when they disagree. Delete the copies and the test together
when `audit_report.py` migrates onto this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from forge import normalise_login

#: The command form. A leading `/` mirrors `/remediate` on the audit ledger and
#: `/review` on this repository's own pull requests, so a reviewer who has seen
#: either already knows the shape.
TRIGGER_COMMAND = "/agent"

#: Line-anchored, so a request has to be the thing the line is for. `(.*?)`
#: captures the request itself. Indentation is allowed because a reviewer
#: replying inside a list writes the command indented and means it.
SLASH_RE = re.compile(r"^[ \t]*/agent\b[ \t]*(.*?)[ \t]*$", re.M)

#: An inline code span, removed before the mention search so prose *about* the
#: trigger is not mistaken for a use of it.
INLINE_CODE_RE = re.compile(r"(`+)[^\n]*?\1")

#: A fence opens on three or more backticks or tildes indented at most three
#: spaces. The indentation bound is CommonMark's and is load-bearing — see
#: `strip_fenced_blocks`.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

#: Markers the agent appends to its own comments. Read from raw API bodies,
#: never from rendered HTML: a forge that displays `<!-- -->` visibly would
#: still round-trip it through the API unchanged, which is what keeps the scheme
#: working somewhere GitHub's comment renderer does not.
ANSWERED_MARKER = "agent-answered"
REFUSED_MARKER = "agent-refused"

#: Deliberately permissive about the id: GraphQL node ids are base64-ish and the
#: alphabet is not documented as stable. Over-matching here costs nothing — the
#: id is only ever compared for equality against one the forge just gave us.
MARKER_RE = re.compile(
    r"<!--\s*agent-(answered|refused)\s*:\s*([A-Za-z0-9_=+/\-]+)\s*-->"
)

#: How much of a request is carried into a card title. The body is not truncated.
MAX_REQUEST_CHARS = 500


@dataclass(frozen=True)
class Trigger:
    """One comment that addressed the agent.

    `kind` distinguishes a typed command from a bare mention because the two
    deserve different replies: a command carries a request, a mention often
    carries only "look at this" and the worker has to read the surrounding
    conversation to find out what is wanted.
    """

    node_id: str
    author: str
    #: "slash" | "mention"
    kind: str
    #: The text after `/agent`, empty for a bare mention or a bare command.
    request: str = ""

    @property
    def summary(self) -> str:
        return (self.request or "(no request text)")[:MAX_REQUEST_CHARS]


def normalise_newlines(text: str) -> str:
    """CRLF and CR to LF, so the line-anchored regex sees real lines."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def strip_fenced_blocks(text: str) -> str:
    """Drop fenced code blocks, so a trigger quoted to discuss it never fires.

    A non-greedy ```…``` regex is the obvious implementation and it is wrong in
    the direction that matters. Given three fences it pairs the first with the
    second and leaves the third dangling, so text between fence 2 and fence 3 —
    text that is *inside* a code block to every Markdown renderer, and to the
    human who wrote it — survives stripping and its trigger fires.

    So: CommonMark's actual rule. A fence opens on a run of three or more
    backticks or tildes, indented at most three spaces; it closes on a run of
    the same character, at least as long, indented at most three spaces, with
    nothing else on the line. An unterminated fence runs to the end.

    The indentation bound is the half that is easy to drop and expensive to
    lose. Strip each line first and `    ``` ` — four spaces, which CommonMark
    and GitHub both render as literal text inside the enclosing block — reads as
    a closer, the block ends four lines early, and the trigger the author put
    inside it to talk *about* fires as a command.
    """
    if not text:
        return ""
    out: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        if fence_char:
            closer = line.rstrip()
            if (
                len(closer) - len(closer.lstrip(" ")) <= 3
                and set(closer.lstrip(" ")) == {fence_char}
                and len(closer.lstrip(" ")) >= fence_len
            ):
                fence_char = ""
                fence_len = 0
            continue
        match = FENCE_OPEN_RE.match(line)
        if match:
            fence_char = match.group(1)[0]
            fence_len = len(match.group(1))
            continue
        out.append(line)
    return "\n".join(out)


def strip_inline_code(text: str) -> str:
    """Drop inline code spans, so quoting the trigger is not using it."""
    return INLINE_CODE_RE.sub(" ", text or "")


def mention_re(login: str) -> re.Pattern:
    """`@<login>` as a whole handle, with GitHub's optional `[bot]` suffix.

    Both spellings have to match. GitHub renders an App mention as
    `@kube-agents-bot` in most places and `@kube-agents-bot[bot]` in others, and
    a reviewer copying the author name off the pull request header gets whichever
    one that view happens to show. Case-insensitive, because forge logins are.

    The leading lookbehind excludes `@` as well as word characters, so an email
    address does not read as a mention of its own local part.
    """
    escaped = re.escape(login)
    return re.compile(
        rf"(?<![A-Za-z0-9_@.\-])@{escaped}(?:\[bot\])?(?![A-Za-z0-9_\-])",
        re.IGNORECASE,
    )


def find_trigger(body: str, self_login: str, node_id: str, author: str):
    """The trigger in one comment, or None.

    A command wins over a mention when both are present: the reviewer typed a
    request, and the request is the more specific thing to act on.
    """
    text = strip_fenced_blocks(normalise_newlines(body))

    matches = SLASH_RE.findall(text)
    if matches:
        # First non-empty request; several `/agent` lines in one comment are one
        # request with elaboration, not several requests.
        request = next((m.strip().strip("`") for m in matches if m.strip()), "")
        return Trigger(node_id=node_id, author=author, kind="slash", request=request)

    if self_login and mention_re(self_login).search(strip_inline_code(text)):
        return Trigger(node_id=node_id, author=author, kind="mention", request="")

    return None


def marker(node_id: str, kind: str = ANSWERED_MARKER) -> str:
    """The HTML comment that records having handled `node_id`."""
    return f"<!-- {kind}:{node_id} -->"


def handled_node_ids(comments, self_login: str) -> set[str]:
    """Node ids already answered or refused, per the agent's own comments.

    `comments` is any iterable of `forge.Comment`. Only comments whose author
    normalises to `self_login` are read: see the module docstring for why that
    restriction is the whole security of the scheme.

    Bodies are scanned raw rather than fence-stripped. The agent writes these
    markers itself and does not fence them; stripping first would only create a
    way for its own quoting to erase its own record.
    """
    wanted = normalise_login(self_login)
    handled: set[str] = set()
    for comment in comments:
        if normalise_login(comment.author) != wanted:
            continue
        for _kind, node_id in MARKER_RE.findall(comment.body or ""):
            handled.add(node_id)
    return handled
