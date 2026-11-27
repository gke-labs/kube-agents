"""The token shapes the platform scripts refuse to publish and redact before logging.

One list, read by two callers in two pods: `feedback_broker.validate` refuses a
report that carries any of them (a submission becomes a public issue), and
`credential_proxy.redact_credentials` blanks them out of a line the broker is
about to log (everything the broker writes leaves the cluster). Both used to
keep a list of their own, and the two had already diverged in grammar for the
same shape; this module is the one copy. It is seeded from the chart's
`AuditRedactor` (`charts/kube-agents/files/redactor.py`), which covers the
Hermes side and cannot be imported from here.

The sandbox image stages this file beside `feedback_broker.py`
(`deploy/sandbox/Dockerfile`), and the agent and broker images copy the whole
scripts tree, so each caller imports the same module wherever it runs.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# The header value has two forms. The redactor masks any twelve letters after
# "bearer" or "basic", which costs a blanked word in a log line. The refusing
# gate requires a digit or a =, + or / in the value and sixteen characters,
# because "the basic troubleshooting steps" and "Bearer authentication returns
# 401" are the vocabulary of an auth bug report, and a refusal costs a person
# their report.
HEADER_VALUE_REDACT = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9_.=+/-]{12,}")
HEADER_VALUE_REFUSE = re.compile(r"(?i)\b(?:bearer|basic)\s+(?=[A-Za-z0-9_.=+/-]*[0-9=+/])[A-Za-z0-9_.=+/-]{16,}")

# (what the shape is, the pattern). The name is what a refusal says; it never
# echoes the match. Patterns apply to raw text: a caller that lowercases first
# loses the case-sensitive shapes (AKIA, the prefixed sk- tokens), so none does.
CREDENTIAL_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key material", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # No leading word boundary on the GitHub shapes, as in the chart's list and
    # the proxy's before it: a token glued to the text before it, as a log line
    # truncated mid-word leaves it, is still a token.
    ("a GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("a fine-grained GitHub token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_.-]{20,}")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[A-Z2-7]{16}\b")),
    # The provider keys this install holds itself (LiteLLM's ANTHROPIC_API_KEY
    # and OPENAI_API_KEY): the bare sk- form and the prefixed one.
    ("a provider API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    (
        "a provider API key",
        re.compile(r"(?<![\w-])sk-(?:ant|proj|svcacct|admin)-(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,}"),
    ),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an authorization header value", HEADER_VALUE_REDACT),
    # The proxy's grammar (8+ per segment) rather than the chart's (10+): the
    # looser one matches everything the stricter one does.
    ("a signed JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "a password in a URL",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s:/?#@\[\]\"'<>{}\\|^`,;]*:[^\s/?#\[\]\"'<>{}\\|^`,;]+@"),
    ),
)


# What the gate refuses: the redactor's list with the header value in its
# narrow form.
REFUSAL_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, HEADER_VALUE_REFUSE if pattern is HEADER_VALUE_REDACT else pattern)
    for name, pattern in CREDENTIAL_SHAPES
)


def find_credential_shape(text: str) -> str | None:
    """The name of the first shape text carries that a public filing must refuse, or None."""
    for name, pattern in REFUSAL_SHAPES:
        if pattern.search(text):
            return name
    return None


# Tool output is redacted with the shapes above in forms of their own
# (`redact_tool_output_counted`), because what a command prints is often JSON
# text (`kubectl -o json`) and what a log line holds is not:
#
# - The leading boundary also accepts a preceding JSON escape. `\n` before
#   `ya29.` leaves the escape's `n`, a word character, in front of the token,
#   where `\b` does not hold. The plain boundary stays an alternative, so
#   whatever the log form matches (a token right after any backslash, as in
#   a Windows path) the tool-output form matches too, with one exception: the
#   plain boundary never opens a match on the letter of a JSON escape (the `n`
#   of `\n`, the `u` of `\u0041`). A match opened there would take the letter
#   and leave its backslash alone in front of REDACTED, and JSON text with a
#   lone backslash no longer parses. The match opens after the escape instead.
# - A password in a URL may hold JSON escapes in its user and its password:
#   Go's encoder writes `&`, `<` and `>` as `\u0026`, `\u003c` and `\u003e`, and
#   any JSON encoder writes a `"` or a `\` in a password as `\"` or `\\`. As in
#   the log form, the match runs from the scheme to the `@` and all of it goes.
# - An authorization header value needs a header's context: `Authorization:`
#   or `authorization=` (any case, and the `[`, `map[` or quote that Go's and
#   kubectl's printing of a header map puts after it), or a curl `-H`, before
#   `bearer` or `basic`; or `bearer` or `basic` (any case) whose value holds a
#   digit, one of `+/=_-`, or a `.` with more of the value after it. Prose ("the basic troubleshooting steps") is what the model
#   reads, and the log form's any twelve letters would blank words in it. The
#   header's name is kept; `bearer` or `basic` and the value go.
# - Private key material is the whole PEM block, armour to armour, not the
#   armour line alone: in tool output the key body is what is read. It runs
#   to the END line when one starts within 16 KiB; otherwise to the `"`
#   closing its JSON string, 16 KiB, or the end of the text
#   (`_PrivateKeyBlocks`). It fails closed: other text up to 16 KiB after a
#   BEGIN line may go with the key.
_JSON_ESCAPE_BEFORE = r'(?<=\\[ntrbf"/\\])|(?<=\\u[0-9a-fA-F]{4})'
# Not on the letter of a JSON escape: one backslash, not itself escaped, then
# an escape's letter.
_NOT_ON_AN_ESCAPE_LETTER = r"(?!(?<=(?<!\\)\\)[bfnrtu])"
JSON_ESCAPE_BOUNDARY = r"(?:" + _JSON_ESCAPE_BEFORE + r"|\b" + _NOT_ON_AN_ESCAPE_LETTER + r")"
JSON_ESCAPE_OR_NO_WORD_BEFORE = r"(?:" + _JSON_ESCAPE_BEFORE + r"|(?<![\w-])" + _NOT_ON_AN_ESCAPE_LETTER + r")"
_WORD_BOUNDARY = r"\b"
_NO_WORD_BEFORE = r"(?<![\w-])"
_CASE_INSENSITIVE = "(?i)"
PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
# How far after a BEGIN line its END line is looked for, and how much text a
# BEGIN with none takes. An RSA-8192 key in PEM is under 7 KiB.
PRIVATE_KEY_MAX_SPAN_CHARS = 16 * 1024


# Room past a block's end for a BEGIN line that starts inside the block to
# finish, so that one is found too.
_PRIVATE_KEY_ARMOUR_SLACK = 64


def _unescaped_quote(text: str, start: int) -> int:
    """The first `"` at or after `start` that no backslash escapes, or len(text)."""
    at = text.find('"', start)
    while at != -1:
        backslashes = 0
        while at - backslashes - 1 >= start and text[at - backslashes - 1] == "\\":
            backslashes += 1
        if backslashes % 2 == 0:
            return at
        at = text.find('"', at + 1)
    return len(text)


class _PrivateKeyBlocks:
    """The private-key shape's tool-output form: each PEM block, failing closed.

    Answers `subn` as a compiled pattern does, so it sits in TOOL_OUTPUT_SHAPES
    beside them. For each BEGIN line, in order: when an END line starts within
    PRIVATE_KEY_MAX_SPAN_CHARS of it, the block runs through that END line;
    otherwise it runs to the first unescaped `"` (the end of the JSON string
    it sits in), PRIVATE_KEY_MAX_SPAN_CHARS, or the end of the text, whichever
    comes first. Nothing between is checked against a key's grammar, so up to
    PRIVATE_KEY_MAX_SPAN_CHARS of other text after a BEGIN line may go with it:
    a key header in tool output is rare, and taking too much is the safe side.

    A block that ended without an END line may hold another BEGIN line (a
    stray header, then the real key): the block is extended by that BEGIN's
    own block, and so on until none is left inside, so the real key's body
    goes even past the first BEGIN's span. A block that ended at an END line
    skips the BEGIN lines inside it. The next END line and the next unescaped
    quote are searched for forward only, from where the last search stopped,
    and every step moves forward, so the pass is linear in the text.
    """

    pattern = PRIVATE_KEY_BEGIN.pattern

    def subn(self, replacement: Any, text: str) -> tuple[str, int]:
        begin = PRIVATE_KEY_BEGIN.search(text)
        if begin is None:
            return text, 0
        pieces: list[str] = []
        kept_from = count = 0
        # The next END line and unescaped quote at or after the last place
        # looked from, kept across BEGIN lines.
        end = PRIVATE_KEY_END.search(text, begin.end())
        quote = -1
        while begin is not None:
            first, start, block_end = begin, begin.start(), begin.end()
            while begin is not None:
                limit = min(len(text), begin.start() + PRIVATE_KEY_MAX_SPAN_CHARS)
                if end is not None and end.start() < begin.end():
                    end = PRIVATE_KEY_END.search(text, begin.end())
                if end is not None and end.start() < limit:
                    block_end = max(block_end, end.end())
                    break
                if quote < begin.end():
                    quote = _unescaped_quote(text, begin.end())
                block_end = max(block_end, min(quote, limit))
                begin = PRIVATE_KEY_BEGIN.search(text, begin.end(), min(len(text), block_end + _PRIVATE_KEY_ARMOUR_SLACK))
                if begin is not None and begin.start() >= block_end:
                    begin = None
            pieces.append(text[kept_from:start])
            pieces.append(replacement(first) if callable(replacement) else replacement)
            count += 1
            kept_from = block_end
            begin = PRIVATE_KEY_BEGIN.search(text, block_end)
        pieces.append(text[kept_from:])
        return "".join(pieces), count


PRIVATE_KEY_BLOCK = _PrivateKeyBlocks()


def redact_cut_private_key(text: str) -> tuple[str, int]:
    """A text a byte cap cut, with everything from its last BEGIN line on replaced when no END line follows it.

    Run on the cut text before `redact_tool_output_counted`: the key the cap
    cut has lost its END line, so what follows its BEGIN line, however long,
    goes. Returns the text and 1 when it replaced a key, else 0.
    """
    last = None
    for last in PRIVATE_KEY_BEGIN.finditer(text):
        pass
    if last is None or PRIVATE_KEY_END.search(text, last.end()) is not None:
        return text, 0
    return text[: last.start()] + REDACTED, 1
PRIVATE_KEY_SHAPE_NAME = "private key material"
HEADER_VALUE_SHAPE_NAME = "an authorization header value"
URL_PASSWORD_SHAPE_NAME = "a password in a URL"

# One JSON escape, whole: what the URL shape's user and password may hold in
# tool output beside their log-form characters.
_JSON_ESCAPE = r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])'
_URL_SCHEME = r"[a-zA-Z][a-zA-Z0-9+.-]{0,31}://"
_URL_USER = r"(?:[^\s:/?#@\[\]\"'<>{}\\|^`,;]|" + _JSON_ESCAPE + r")*"
_URL_PASSWORD_CHAR = r"(?:[^\s/?#\[\]\"'<>{}\\|^`,;]|" + _JSON_ESCAPE + r")"
URL_PASSWORD_TOOL_FORM = re.compile(JSON_ESCAPE_BOUNDARY + _URL_SCHEME + _URL_USER + ":" + _URL_PASSWORD_CHAR + "+@")

# The group a tool-output match keeps: the header's name before its value.
KEPT_LEAD_GROUP = "lead"
_HEADER_VALUE_CHAR = r"[A-Za-z0-9_.=+/-]"
_HEADER_LEAD = (
    r"(?P<" + KEPT_LEAD_GROUP + r">" + JSON_ESCAPE_BOUNDARY
    + r"(?i:authorization)(?:\\?[\"'])?\s*[:=]\s*(?:map\[|\[)?\s*(?:\\?[\"'])?|(?<![\w-])-H\s+(?:\\?[\"'])?)"
)
_HEADER_SCHEME_IN_CONTEXT = _HEADER_LEAD + r"(?i:bearer|basic)\s+"
_HEADER_SCHEME_BARE = JSON_ESCAPE_BOUNDARY + r"(?i:bearer|basic)\s+"
# A value's mark of a token rather than a word: a digit, one of `+/=_-`, or a
# `.` with more of the value after it (a sentence's full stop is not one).
_HEADER_TOKEN_MARK = "(?=" + _HEADER_VALUE_CHAR + r"*(?:[0-9+/=_-]|\.[A-Za-z0-9_=+/-]))"
HEADER_VALUE_TOOL_FORM = re.compile(
    _HEADER_SCHEME_IN_CONTEXT + _HEADER_VALUE_CHAR + "{12,}"
    + "|" + _HEADER_SCHEME_BARE + _HEADER_TOKEN_MARK + _HEADER_VALUE_CHAR + "{12,}"
)


def _tool_output_form(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """`pattern` with its leading boundary widened to accept a preceding JSON escape."""
    source, flags = pattern.pattern, ""
    if source.startswith(_CASE_INSENSITIVE):
        flags, source = _CASE_INSENSITIVE, source[len(_CASE_INSENSITIVE):]
    if source.startswith(_WORD_BOUNDARY):
        source = JSON_ESCAPE_BOUNDARY + source[len(_WORD_BOUNDARY):]
    elif source.startswith(_NO_WORD_BEFORE):
        source = JSON_ESCAPE_OR_NO_WORD_BEFORE + source[len(_NO_WORD_BEFORE):]
    return re.compile(flags + source)


# The shapes whose tool-output form is a pattern of its own rather than the
# log form with the widened boundary.
_TOOL_OUTPUT_OWN_FORMS: dict[str, Any] = {
    PRIVATE_KEY_SHAPE_NAME: PRIVATE_KEY_BLOCK,
    HEADER_VALUE_SHAPE_NAME: HEADER_VALUE_TOOL_FORM,
    URL_PASSWORD_SHAPE_NAME: URL_PASSWORD_TOOL_FORM,
}
TOOL_OUTPUT_SHAPES: tuple[Any, ...] = tuple(
    _TOOL_OUTPUT_OWN_FORMS.get(name) or _tool_output_form(pattern) for name, pattern in CREDENTIAL_SHAPES
)


# What each shape's start looks like when a byte cap cut the text inside it:
# the shape's leading literal and at least CUT_BODY_MIN_CHARS of its body, at
# the very end of the text. The whole-shape patterns above need a minimum body
# length, so a token cut short of it would pass them; these match a cut text's
# last characters only (`redact_cut_tail`), never the middle of a text. Each
# carries its whole shape's leading boundary (none for the GitHub shapes, as
# there), and a lead with fewer body characters after it is kept: a word that
# happens to end in `ghs_`, `ASIA` or `github_` is not a token, and three
# characters of a body are not enough of one to use. The PEM armour line is
# absent: it is a label, not key material.
# They are run over the text's last CUT_TAIL_WINDOW_CHARS characters, which
# holds every shape above at the lengths its issuer mints (a JWT, the longest,
# runs to a few hundred bytes per segment) and bounds the work on a cut text
# of any size.
CUT_TAIL_WINDOW_CHARS = 4096
CUT_BODY_MIN_CHARS = 4
_CUT_BODY = "{" + str(CUT_BODY_MIN_CHARS) + ",}"
CUT_PREFIX_SHAPES: tuple[re.Pattern[str], ...] = tuple(
    _tool_output_form(re.compile(source))
    for source in (
        r"gh[pousr]_[A-Za-z0-9]" + _CUT_BODY + r"\Z",
        r"github_pat_[A-Za-z0-9_]" + _CUT_BODY + r"\Z",
        r"\bAIza[0-9A-Za-z_-]" + _CUT_BODY + r"\Z",
        r"\bya29\.[0-9A-Za-z_.-]" + _CUT_BODY + r"\Z",
        r"\b(?:AKIA|ASIA)[A-Z2-7]" + _CUT_BODY + r"\Z",
        r"\bsk-[A-Za-z0-9_-]" + _CUT_BODY + r"\Z",
        r"\bxox[abprs]-[A-Za-z0-9-]" + _CUT_BODY + r"\Z",
        r"\beyJ[A-Za-z0-9_-]" + _CUT_BODY + r"(?:\.[A-Za-z0-9_-]*){0,2}\Z",
    )
) + (
    # The header value's two contexts, as in its tool-output form; a cut value
    # may stop before its digit, so the bare form's mark is not required, and
    # prose that a cut ends four letters after `basic` or `bearer` goes.
    re.compile("(?:" + _HEADER_SCHEME_IN_CONTEXT + "|" + _HEADER_SCHEME_BARE + ")" + _HEADER_VALUE_CHAR + _CUT_BODY + r"\Z"),
    # The password, with the start of a JSON escape the cut fell inside.
    re.compile(
        JSON_ESCAPE_BOUNDARY + _URL_SCHEME + _URL_USER + ":" + _URL_PASSWORD_CHAR + _CUT_BODY
        + r"(?:\\(?:u[0-9a-fA-F]{0,3})?)?\Z"
    ),
)


def _kept_lead(match: re.Match[str]) -> str:
    """The part of a tool-output match that stays: a header's name, or nothing."""
    return match.groupdict().get(KEPT_LEAD_GROUP) or ""


def _tool_output_replacement(match: re.Match[str]) -> str:
    return _kept_lead(match) + REDACTED


def redact_counted(text: str) -> tuple[str, int]:
    """text with every shape replaced by REDACTED, and how many replacements were made."""
    total = 0
    for _, pattern in CREDENTIAL_SHAPES:
        text, replaced = pattern.subn(REDACTED, text)
        total += replaced
    return text, total


def redact_tool_output_counted(text: str) -> tuple[str, int]:
    """Tool output with every shape, in its tool-output form, replaced by REDACTED, and the count."""
    total = 0
    for pattern in TOOL_OUTPUT_SHAPES:
        text, replaced = pattern.subn(_tool_output_replacement, text)
        total += replaced
    return text, total


def redact(text: str) -> str:
    """text with every shape replaced by REDACTED."""
    return redact_counted(text)[0]


def redact_cut_tail(text: str) -> tuple[str, int]:
    """A text a byte cap cut, with a shape's partial start at its end replaced by REDACTED.

    Run after `redact_tool_output_counted`, which has already taken every whole shape, so
    what is left at the end is a token the cap cut short of its minimum
    length. Returns the text and 1 when it replaced a tail, else 0.
    """
    offset = max(0, len(text) - CUT_TAIL_WINDOW_CHARS)
    window = text[offset:]
    for pattern in CUT_PREFIX_SHAPES:
        match = pattern.search(window)
        if match is not None:
            return text[: offset + match.start()] + _kept_lead(match) + REDACTED, 1
    return text, 0
