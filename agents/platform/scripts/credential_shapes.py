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

REDACTED = "[REDACTED]"

# The header value has two forms. The redactor masks any twelve letters after
# "bearer" or "basic", which costs a blanked word in a log line. The refusing
# gate requires a digit or a =, + or / in the value and sixteen characters,
# because "the basic troubleshooting steps" and "Bearer authentication returns
# 401" are the vocabulary of an auth bug report, and a refusal costs a person
# their report.
HEADER_VALUE_REDACT = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9_.=+/-]{12,}")
HEADER_VALUE_REFUSE = re.compile(r"(?i)\b(?:bearer|basic)\s+(?=[A-Za-z0-9_.=+/-]*[0-9=+/])[A-Za-z0-9_.=+/-]{16,}")

# The case-insensitive hint for the header-value shape, searched in place so a
# multi-MiB text is never copied just to lowercase it. It matches wherever
# "bearer" or "basic" appears in any casing (and, through re's case folding,
# possibly a little more, which only costs a regex pass).
_BEARER_OR_BASIC_HINT_RE = re.compile(r"(?i:bearer|basic)")


class _AllOf(tuple):
    """Hint tuple requiring every literal substring to appear in text."""


_HintSpec = tuple[str, ...] | _AllOf | re.Pattern[str]

# (what the shape is, the pattern, prefilter hint). The name is what a refusal
# says; it never echoes the match. Patterns apply to raw text: a caller that
# lowercases first loses the case-sensitive shapes (AKIA, the prefixed sk-
# tokens), so none does. Each hint is a mandatory literal substring tuple (any
# one must appear), an _AllOf tuple (every literal must appear), or a compiled
# hint regex, derived together so reordering or adding shapes cannot desync.
_SHAPE_SPECS: tuple[tuple[str, re.Pattern[str], _HintSpec], ...] = (
    (
        "private key material",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ("PRIVATE KEY-----",),
    ),
    # No leading word boundary on the GitHub shapes, as in the chart's list and
    # the proxy's before it: a token glued to the text before it, as a log line
    # truncated mid-word leaves it, is still a token.
    (
        "a GitHub token",
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    ),
    (
        "a fine-grained GitHub token",
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        ("github_pat_",),
    ),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), ("AIza",)),
    ("a Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_.-]{20,}"), ("ya29.",)),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[A-Z2-7]{16}\b"), ("AKIA", "ASIA")),
    # The provider keys this install holds itself (LiteLLM's ANTHROPIC_API_KEY
    # and OPENAI_API_KEY): the bare sk- form and the prefixed one.
    ("a provider API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}"), ("sk-",)),
    (
        "a provider API key",
        re.compile(r"(?<![\w-])sk-(?:ant|proj|svcacct|admin)-(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,}"),
        ("sk-ant-", "sk-proj-", "sk-svcacct-", "sk-admin-"),
    ),
    (
        "a Slack token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
        ("xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-"),
    ),
    ("an authorization header value", HEADER_VALUE_REDACT, _BEARER_OR_BASIC_HINT_RE),
    # The proxy's grammar (8+ per segment) rather than the chart's (10+): the
    # looser one matches everything the stricter one does.
    (
        "a signed JWT",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        ("eyJ",),
    ),
    (
        "a password in a URL",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s:/?#@\[\]\"'<>{}\\|^`,;]*:[^\s/?#\[\]\"'<>{}\\|^`,;]+@"),
        _AllOf(("://", "@")),
    ),
)

CREDENTIAL_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, pattern) for name, pattern, _ in _SHAPE_SPECS
)

# What the gate refuses: the redactor's list with the header value in its
# narrow form.
REFUSAL_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, HEADER_VALUE_REFUSE if pattern is HEADER_VALUE_REDACT else pattern)
    for name, pattern in CREDENTIAL_SHAPES
)

_SHAPE_HINTS: tuple[_HintSpec, ...] = tuple(hints for _, _, hints in _SHAPE_SPECS)


def _may_contain_shape(text: str, hints: _HintSpec) -> bool:
    if isinstance(hints, re.Pattern):
        return hints.search(text) is not None
    if isinstance(hints, _AllOf):
        for hint in hints:
            if hint not in text:
                return False
        return True
    for hint in hints:
        if hint in text:
            return True
    return False


def find_credential_shape(text: str) -> str | None:
    """The name of the first shape text carries that a public filing must refuse, or None."""
    if not text:
        return None
    for (name, pattern), hints in zip(REFUSAL_SHAPES, _SHAPE_HINTS, strict=True):
        if _may_contain_shape(text, hints) and pattern.search(text):
            return name
    return None


def redact(text: str) -> str:
    """text with every shape replaced by REDACTED."""
    if not text:
        return text
    for (_, pattern), hints in zip(CREDENTIAL_SHAPES, _SHAPE_HINTS, strict=True):
        if _may_contain_shape(text, hints):
            text = pattern.sub(REDACTED, text)
    return text
