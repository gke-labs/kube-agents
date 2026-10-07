"""Centralised redaction and pseudonymisation for audit logs and session metadata.

Two independent jobs live here because both are needed by the same four call
sites (the two audit hooks, the session store, and the OTel bridge):

* :meth:`AuditRedactor.redact` / :meth:`AuditRedactor.redact_text` strip
  credentials and e-mail addresses out of anything on its way to the audit
  file or a log.
* :meth:`AuditRedactor.hmac_hash` turns a user identity into a stable
  pseudonym, so session rows and span attributes carry a hash rather than the
  address itself.

A third, optional layer sits on top of the first: :class:`RedactionRule`
objects an operator configures -- IP literals, a cluster name, a project id --
each masked or pseudonymised after the built-in credential patterns have run.
:meth:`AuditRedactor.rules_from_config` builds them from the plain mapping the
chart renders, and the LiteLLM gateway hook is the consumer today. A caller
that passes no rules sees exactly what it saw before the layer existed.

The canonical copy is `agents/chat/defaults/plugins/common/redactor.py`;
`charts/kube-agents/files/redactor.py` is a byte-identical mirror the chart
mounts into the stock LiteLLM image, because Helm cannot read outside the chart.
`tests/test_litellm_redaction.py` fails when the two drift: edit the plugin copy
and copy it over.

Deliberately *not* here: raising on a match. These helpers are called from
`pre_gateway_dispatch` and from `start_span`, so an exception — including one
from a regex false positive — would land in the message-dispatch path or in
every span the agent opens. Redaction fails open by design; the enforcement
boundary is Kubernetes RBAC and the credential proxy, not a logging hook.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger("hermes.plugin.common.redactor")

SALT_ENV_VAR = "SESSION_KV_SALT"

# The two things a configured rule may do to a match. `mask` replaces it with
# a fixed marker; `pseudonym` replaces it with a salted HMAC prefix, so the
# same value maps to the same token within one salt and nothing maps back.
RULE_ACTION_MASK = "mask"
RULE_ACTION_PSEUDONYM = "pseudonym"
RULE_ACTIONS = frozenset({RULE_ACTION_MASK, RULE_ACTION_PSEUDONYM})
# The built-in IP rule accepts one more: `off` leaves IP literals alone while
# the credential patterns and any custom rules still run.
IP_RULE_ACTION_OFF = "off"
IP_RULE_ACTIONS = RULE_ACTIONS | {IP_RULE_ACTION_OFF}
IP_RULE_NAME = "ip"
# Twelve hex characters (48 bits) of the HMAC: enough that two identifiers in
# one estate do not collide, short enough to read in a prompt or a log line.
PSEUDONYM_HEX_LENGTH = 12
# A rule name ends up inside the replacement token and in a log line keyed by
# it, so it is kept to the characters that survive both unambiguously.
RULE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
# A mask marker is the rule name upper-cased with every run of characters
# outside [A-Za-z0-9] folded to one underscore: `cluster-name` masks as
# `[REDACTED_CLUSTER_NAME]`. Names that differ only in punctuation share a
# marker, though their counts stay distinct.
MASK_MARKER_FORMAT = "[REDACTED_{}]"
MASK_NAME_FOLD_PATTERN = re.compile(r"[^A-Za-z0-9]+")
# The keys a configured rule may carry. `pattern` is a regular expression,
# `literal` an exact string; a rule names exactly one of the two.
RULE_CONFIG_KEYS = frozenset({"name", "pattern", "literal", "action"})
RULE_CONFIG_KEY_NAME = "name"
RULE_CONFIG_KEY_PATTERN = "pattern"
RULE_CONFIG_KEY_LITERAL = "literal"
RULE_CONFIG_KEY_ACTION = "action"
CONFIG_KEY_IP = "ip"
CONFIG_KEY_IP_ACTION = "action"
CONFIG_KEY_IP_ALLOW_CIDRS = "allowCidrs"
CONFIG_KEY_RULES = "rules"
# Candidates only: both are validated with the ipaddress module before they
# are touched, which is what keeps `999.1.1.1`, a `12:30:45` timestamp and a
# six-group MAC address out. The IPv4 lookarounds refuse to take the tail of a
# longer dotted run such as `1.2.3.4.5`; the IPv6 ones refuse to start or end
# inside a word or a longer colon run.
IPV4_CANDIDATE_PATTERN = re.compile(r"(?<!\d\.)\b(?:\d{1,3}\.){3}\d{1,3}\b(?!\.\d)")
IPV6_CANDIDATE_PATTERN = re.compile(
    r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"
)
# Counter names for the two built-in layers when a caller asks for counts.
COUNT_NAME_CREDENTIAL = "credential"
COUNT_NAME_EMAIL = "email"
REDACTED_SECRET_MARKER = "[REDACTED_SECRET]"
CREDENTIAL_MARKERS = (REDACTED_SECRET_MARKER, "[REDACTED_PRIVATE_KEY]")
EMAIL_MARKER = "[REDACTED_EMAIL]"
# Every built-in and rule mask marker starts with this. A value that already
# holds one is left alone rather than masked again, which is what used to turn
# a blanked `password: [REDACTED_SECRET]` line into `[REDACTED_SECRET]]`.
REDACTED_MARKER_PREFIX = "[REDACTED_"
WHOLE_MARKER_PATTERN = re.compile(r"^\[REDACTED_[A-Z0-9_]+\]$")
# Values a credential-named key can hold that are not credentials:
# `automountServiceAccountToken: false` is a switch, `value: none` a
# placeholder, and masking either puts a marker where a manifest needs the
# literal.
NON_CREDENTIAL_VALUES = frozenset({"true", "false", "null", "none"})
# An env value that only references another variable (`$(DB_PASSWORD)`) names
# a credential rather than holding one.
ENV_REFERENCE_PATTERN = re.compile(r"^\$\([A-Za-z_][A-Za-z0-9_]*\)$")
# A YAML block scalar indicator (`|`, `>-`, `|2+`): the value is on the lines
# below, not on this one.
BLOCK_SCALAR_PATTERN = re.compile(r"^[|>][+-]?[0-9]?[+-]?$")
YAML_QUOTES = ("'", '"')
# The key names the key/value and env-pair patterns treat as credentials. The
# secret half of an AWS key pair has no shape of its own, so it is caught here
# by name (`aws_secret_access_key`), as is a `SECRET_KEY` setting. `secret_key`
# needs its separator: camel-case `secretKey` is how an ExternalSecret names a
# key inside a Secret, which is not a credential.
CREDENTIAL_NAME_WORDS = (
    r"(?:password|passwd|secret|token|api[_-]?key|apikey"
    r"|access[_-]?token|access[_-]?key|secret[_-]key|client[_-]?secret)"
)
# A Kubernetes Secret as a parsed object: the fields whose every value is
# credential material, whatever the keys under them are called.
CREDENTIAL_OBJECT_KIND = "Secret"
CREDENTIAL_OBJECT_FIELDS = frozenset({"data", "stringData"})
KIND_KEY = "kind"
# A parsed env entry: `{"name": "DB_PASSWORD", "value": "…"}`.
ENV_NAME_KEY = "name"
ENV_VALUE_KEY = "value"
# A mapping key that names or points at a credential rather than holding one:
# `secretName`, `tokenPath`, `passwordFile`, `authMode`. Its last word decides,
# so `api_key` and `client_secret` still mask. The text patterns reach the
# same answer by requiring the credential word at the end of the name.
NON_CREDENTIAL_KEY_SUFFIXES = frozenset({"name", "names", "path", "file", "mode", "ref", "type", "kind"})
KEY_WORD_SPLIT_PATTERN = re.compile(r"[^a-z0-9]+")
CAMEL_CASE_BOUNDARY_PATTERN = re.compile(r"([a-z0-9])([A-Z])")

_fallback_salt: Optional[bytes] = None
_fallback_salt_lock = threading.Lock()


def _resolve_salt() -> bytes:
    """Return the HMAC salt, generating a per-process one if none is configured.

    Failing closed here was tried and is wrong: ``hmac_hash`` is called
    unconditionally for any Google Chat user id, from ``SessionMetadata``'s
    constructor, and the caller swallows the exception — so a missing salt took
    out session metadata entirely (no session_id, chat_id or thread_id row ever
    written) and with it thread resolution, incident lookup and span identity.

    The salt is optional in every install path, so "absent" is the common case
    on upgrade rather than a misconfiguration. Degrade loudly instead: hashes
    stay correct and unlinkable, they simply stop being comparable across a pod
    restart.
    """
    configured = (os.getenv(SALT_ENV_VAR) or "").strip()
    if configured:
        return configured.encode("utf-8")

    global _fallback_salt
    with _fallback_salt_lock:
        if _fallback_salt is None:
            _fallback_salt = secrets.token_bytes(32)
            logger.warning(
                "%s is not configured; falling back to a per-process random salt. "
                "Identity pseudonyms remain safe but will not be stable across pod "
                "restarts. Set %s in the agent Secret to make them stable.",
                SALT_ENV_VAR,
                SALT_ENV_VAR,
            )
        return _fallback_salt


@dataclass(frozen=True)
class RedactionRule:
    """One operator-configured substitution, applied after the credential patterns.

    ``canonical`` is the hook the IP rules use: given the matched text it
    returns the value to act on, or ``None`` to leave the match untouched (an
    invalid address, or one inside an allowlisted CIDR). A pseudonym is taken
    over the canonical form, so ``::1`` and ``0:0:0:0:0:0:0:1`` share a token.
    """

    name: str
    pattern: "re.Pattern[str]"
    action: str = RULE_ACTION_MASK
    canonical: Optional[Callable[[str], Optional[str]]] = None

    def __post_init__(self) -> None:
        if not RULE_NAME_PATTERN.match(self.name or ""):
            raise ValueError(
                f"redaction rule name {self.name!r} must match {RULE_NAME_PATTERN.pattern}"
            )
        if self.action not in RULE_ACTIONS:
            raise ValueError(
                f"redaction rule {self.name!r}: action {self.action!r} is not one of "
                f"{sorted(RULE_ACTIONS)}"
            )

    @property
    def mask(self) -> str:
        return MASK_MARKER_FORMAT.format(MASK_NAME_FOLD_PATTERN.sub("_", self.name).upper())


class AuditRedactor:
    """Stateless regex and dictionary redactor for secrets and PII."""

    PRIVATE_KEY_PATTERN = re.compile(
        r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----"
        r"[\s\S]*?"
        r"-----END\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----",
        re.IGNORECASE,
    )
    GCP_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z\-_]{35}")
    GCP_OAUTH_TOKEN_PATTERN = re.compile(r"ya29\.[0-9A-Za-z\-_.]{20,}")
    # `basic` as well as `bearer`, and the base64 alphabet in the value: a
    # `Authorization: Basic <b64>` header is a credential in exactly the way a
    # bearer token is. The scheme is preserved so the record still says which.
    BEARER_TOKEN_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+([a-zA-Z0-9_\-.=+/]{12,})")
    GITHUB_TOKEN_PATTERN = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")
    OPENAI_TOKEN_PATTERN = re.compile(r"sk-[A-Za-z0-9]{20,}")
    # `sk-` keys with a hyphenated family segment -- Anthropic (`sk-ant-api03-…`)
    # and OpenAI project, service-account and admin keys -- which
    # OPENAI_TOKEN_PATTERN misses because it stops at the first hyphen. Real
    # ones are long and mixed-case; a Kubernetes name is lower-case by rule, so
    # the upper-case lookahead keeps `deploy/sk-proj-ingest-worker` readable,
    # and the lookbehind a name that merely contains `sk-proj-`.
    PREFIXED_SK_TOKEN_PATTERN = re.compile(
        r"(?<![\w-])sk-(?:ant|proj|svcacct|admin)-(?=[A-Za-z0-9_\-]*[A-Z])[A-Za-z0-9_\-]{32,}"
    )
    # An AWS access key id: `AKIA` for a long-term key, `ASIA` for an STS
    # session credential, then sixteen base32 characters.
    AWS_ACCESS_KEY_ID_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z2-7]{16}\b")
    # The password in a URL's userinfo, `scheme://user:password@host`. Only the
    # password is masked, so the record still names the user and the host. It
    # runs before EMAIL_PATTERN, which would otherwise read `password@host` as
    # an address and take the host with it, and before the IP rules, which
    # would otherwise leave the password beside a pseudonymised host.
    # - The password runs to the last `@` before the host, as URL parsers read
    #   it, so a raw `@` inside it is masked too.
    # - Neither part may hold a character RFC 3986 keeps out of userinfo, nor
    #   `,`, `;` or `'`, so the match cannot run across the fields of compact JSON
    #   or a CSV line to the next `@`, and `<password>` / `${VAR}` placeholders
    #   stay readable.
    # - The scheme is capped so a long alphanumeric run is not rescanned from
    #   every position.
    URL_PASSWORD_PATTERN = re.compile(
        r"\b([a-zA-Z][a-zA-Z0-9+.\-]{0,31}://[^\s:/?#@\[\]\"'<>{}\\|^`,;]*:)"
        r"([^\s/?#\[\]\"'<>{}\\|^`,;]+)@"
    )
    # The three token shapes this redactor was missing that `redact_secrets` in
    # agents/platform/skills/fleet-audit/scripts/audit_report.py already had.
    # The JWT shape is what a projected ServiceAccount token looks like, so it
    # is the one most likely to reach a tool result in this deployment.
    GITHUB_PAT_PATTERN = re.compile(r"github_pat_[A-Za-z0-9_]{20,}")
    SLACK_TOKEN_PATTERN = re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")
    JWT_PATTERN = re.compile(
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
    )
    # The key name may carry a prefix — `SESSION_KV_API_KEY` and
    # `ANTHROPIC_API_KEY` are the two this repository writes most often, and a
    # bare `\b` before `api_key` matches neither, because `_` is a word
    # character. The trailing `\b` still does the work that matters:
    # `TOKENIZER_PATH` does not match, since `token` is not followed by one.
    # The separator stays on one line: a key that ends its line
    # (`serviceAccountToken:`, `secret:` in every pod spec) opens a nested
    # mapping, and the next line's key is not its value.
    # The value is one of three shapes. A double-quoted value may hold escapes
    # (`"a\"b"`); a single-quoted one runs to its quote. An unquoted value
    # stops at a JSON escape (`\n`, `\"`), so YAML kept inside a JSON string
    # does not lose its line break to the mask, and never starts with `[`, so
    # a list under the key (`"tokens": [`) and an existing marker are left
    # alone. The name's prefix is capped so a long hyphenated run is not
    # rescanned from every word boundary in it.
    SECRET_KV_PATTERN = re.compile(
        r"(?i)\b([\w.\-]{0,64}?" + CREDENTIAL_NAME_WORDS + r")\b"
        r"([\"']?[ \t]*[:=][ \t]*)"
        r"(?:\"(?P<dq>(?:[^\"\\\r\n]|\\.)*)\"|'(?P<sq>[^'\r\n]*)'"
        r"|(?P<bare>(?:[^\"'\s,}{\[\]\\]|\\(?![nrt\"/]))(?:[^\"'\s,}{\]\\]|\\(?![nrt\"/]))*))"
    )
    # A container env entry. Its value is a credential when its name is one,
    # but kubectl prints the two on separate lines (YAML) or as sibling fields
    # (JSON), where neither SECRET_KV_PATTERN nor a token shape sees them.
    # Both orders, because only kubectl is guaranteed to print `name` first.
    # A YAML value is captured to the end of its line and unquoted in code: it
    # has to start with a non-space so the split between the separator and the
    # value is fixed, which keeps a long run of spaces from being rescanned.
    CREDENTIAL_NAME_PATTERN = re.compile(r"(?i)^[\w.\-]*?" + CREDENTIAL_NAME_WORDS + r"$")
    ENV_PAIR_YAML_PATTERNS = (
        re.compile(
            r"(?m)^(?P<indent>[ \t]*)-[ \t]+name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)"
            r"[ \t]*\r?\n(?P=indent)[ \t]+value:[ \t]*(?P<value>(?:\S[^\r\n]*)?)(?=\r?$)"
        ),
        re.compile(
            r"(?m)^(?P<indent>[ \t]*)-[ \t]+value:[ \t]*(?P<value>(?:\S[^\r\n]*)?)\r?\n"
            r"(?P=indent)[ \t]+name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)[ \t]*(?=\r?$)"
        ),
    )
    ENV_PAIR_JSON_PATTERNS = (
        re.compile(
            r"\"name\"\s*:\s*\"(?P<name>[\w.\-]+)\"\s*,\s*"
            r"\"value\"\s*:\s*\"(?P<value>(?:[^\"\\]|\\.)*)\""
        ),
        re.compile(
            r"\"value\"\s*:\s*\"(?P<value>(?:[^\"\\]|\\.)*)\"\s*,\s*"
            r"\"name\"\s*:\s*\"(?P<name>[\w.\-]+)\""
        ),
    )
    # An env entry whose value is a YAML block scalar (kubectl prints a
    # multi-line value as `value: |`): the lines indented under `value:` are
    # the value. They are replaced by one marker line, which keeps the block
    # a valid scalar.
    ENV_BLOCK_PATTERN = re.compile(
        r"(?m)^(?P<indent>[ \t]*)-[ \t]+name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)"
        r"[ \t]*\r?\n(?P=indent)(?P<keyindent>[ \t]+)value:[ \t]*[|>][+-]?[0-9]?[+-]?[ \t]*\r?\n"
        r"(?P<body>(?:(?:[ \t]*\r?\n)*(?P=indent)(?P=keyindent)[ \t]+[^\r\n]*(?:\r?\n|$))+)"
    )
    # A credential-named key whose value is a YAML block scalar
    # (`password: |`): the lines indented under it are the value, so they are
    # replaced by one marker line and the indicator stays.
    CREDENTIAL_BLOCK_SCALAR_PATTERN = re.compile(
        r"(?im)^(?P<indent>[ \t]*)(?:-[ \t]+)?[\w.\-]{0,64}?" + CREDENTIAL_NAME_WORDS
        + r"[\"']?[ \t]*:[ \t]*[|>][+-]?[0-9]?[+-]?[ \t]*\r?\n"
        r"(?P<body>(?:(?:[ \t]*\r?\n)*(?P=indent)[ \t]+[^\r\n]*(?:\r?\n|$))+)"
    )
    # A JSON string that itself holds JSON or multi-line text, such as the
    # `kubectl.kubernetes.io/last-applied-configuration` annotation in
    # `kubectl get -o json`, or YAML kept in a ConfigMap. Its contents are
    # escaped, so no pattern above sees them until the string is decoded.
    # A string never starts at an escaped quote: on text cut off inside an
    # escaped string, every `\"` would otherwise start a scan to the end.
    JSON_STRING_PATTERN = re.compile(r"(?<!\\)\"(?:[^\"\\]|\\.)*\"")
    JSON_NESTED_ESCAPES = ('\\"', "\\n")
    # The JSON twin of SECRET_BLOCK_PATTERN, for `kubectl get secret -o json`.
    # It runs only on text that carries a Secret, because `data` is also the
    # envelope of ordinary JSON such as a Prometheus query result; within such
    # text it blanks every `data` object, a ConfigMap's included, as the YAML
    # rule does. A string value may itself hold braces, so the object body is
    # matched string by string rather than up to the first `}`.
    JSON_CREDENTIAL_KIND_PATTERN = re.compile(r"\"kind\"\s*:\s*\"Secret\"")
    JSON_DATA_OBJECT_PATTERN = re.compile(
        r"(\"(?:data|stringData)\"\s*:\s*\{)((?:[^{}\"]|\"(?:[^\"\\]|\\.)*\")*)(\})"
    )
    JSON_STRING_MEMBER_PATTERN = re.compile(r"(\"(?:[^\"\\]|\\.)*\"\s*:\s*)\"(?:[^\"\\]|\\.)*\"")
    # The opener of a Kubernetes Secret payload, and a key/value pair indented
    # under it. Everything in that block is credential material whatever the
    # individual keys are called, which is the one thing neither the key-name
    # heuristic nor a token shape can see. Ported from `_redact_secret_blocks`
    # in audit_report.py; a ConfigMap's `data:` is blanked too, which costs an
    # audit record some readability and is the safe direction to err in.
    SECRET_BLOCK_PATTERN = re.compile(r"^(\s*)(data|stringData)\s*:\s*$")
    INDENTED_PAIR_PATTERN = re.compile(r"^(\s*)([\w.\-/]+)\s*:\s*(\S.*)$")
    # The negative lookahead exempts GCP service-account addresses. They are not
    # personal data, and in this repository the principal is the one thing an
    # operator greps an IAM audit record for — redacting it leaves a record that
    # says which role was granted on which resource but not to whom, which is
    # the over-eager-redactor failure mode that gets redaction switched off.
    #
    # Both edges of the exemption are anchored. On the left, whole labels, so
    # `a@notgserviceaccount.com` is still redacted. On the right, `(?!\.?[\w\-])`
    # rather than `\b`, so a domain that merely *contains* the label sequence —
    # `victim@corp.gserviceaccount.com.attacker.io` — is redacted too, while an
    # address that simply ends a sentence still is not.
    EMAIL_PATTERN = re.compile(
        r"[a-zA-Z0-9._%+\-]+@(?!(?:[a-zA-Z0-9\-]+\.)*gserviceaccount\.com(?!\.?[\w\-]))"
        r"[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    )

    SENSITIVE_KEYS = {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "client_secret",
        "authorization",
        "auth",
        "private_key",
        "credential",
        "credentials",
    }

    @staticmethod
    def _get_key_words(key: Any) -> Set[str]:
        """Split a mapping key into lowercase words, camelCase included.

        ``clientSecret`` and ``client_secret`` must both match, while
        ``tokenizer`` and ``author`` must not — hence whole-word matching
        against :attr:`SENSITIVE_KEYS` rather than a substring test.
        """
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
        words = set(re.split(r"[^a-z0-9]+", text))
        words.add(text)
        return {word for word in words if word}

    @classmethod
    def _redact_secret_blocks(cls, text: str) -> str:
        """Blank every value indented under a `data:` / `stringData:` key.

        A line scan rather than a YAML parse, because what reaches here is a
        tool result — a fragment as often as a document — and indentation is
        the only structure a fragment reliably carries.
        """
        if "data:" not in text and "stringData:" not in text:
            return text
        out = []
        block_indent: Optional[int] = None
        # A pair whose value is a block scalar (`tls.key: |`) keeps its
        # indicator; the lines indented under it become one marker line.
        scalar_indent: Optional[int] = None
        scalar_marked = False
        for line in text.split("\n"):
            indent = len(line) - len(line.lstrip())
            if scalar_indent is not None:
                if not line.strip() or indent > scalar_indent:
                    if line.strip() and not scalar_marked:
                        out.append(f"{line[:indent]}{REDACTED_SECRET_MARKER}")
                        scalar_marked = True
                    continue
                scalar_indent = None
            opener = cls.SECRET_BLOCK_PATTERN.match(line)
            if opener:
                block_indent = len(opener.group(1))
                out.append(line)
                continue
            if block_indent is not None:
                pair = cls.INDENTED_PAIR_PATTERN.match(line)
                if pair and len(pair.group(1)) > block_indent:
                    if BLOCK_SCALAR_PATTERN.match(pair.group(3).strip()):
                        out.append(line)
                        scalar_indent = len(pair.group(1))
                        scalar_marked = False
                    else:
                        out.append(f"{pair.group(1)}{pair.group(2)}: [REDACTED_SECRET]")
                    continue
                if line.strip() and indent <= block_indent:
                    block_indent = None
            out.append(line)
        return "\n".join(out)

    @classmethod
    def _mask_credential_block(cls, match: "re.Match[str]") -> str:
        body = match.group("body")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if all(line.startswith(REDACTED_MARKER_PREFIX) for line in lines):
            return match.group(0)
        body_indent = body[: len(body) - len(body.lstrip(" \t"))]
        ending = "\n" if body.endswith("\n") else ""
        head = match.group(0)[: match.start("body") - match.start()]
        return f"{head}{body_indent}{REDACTED_SECRET_MARKER}{ending}"

    @classmethod
    def _redact_json_credential_data(cls, text: str) -> str:
        """Blank every string value in a `data` / `stringData` object of JSON that holds a Secret."""
        if not cls.JSON_CREDENTIAL_KIND_PATTERN.search(text):
            return text

        def blank(match: "re.Match[str]") -> str:
            body = cls.JSON_STRING_MEMBER_PATTERN.sub(
                lambda member: f'{member.group(1)}"{REDACTED_SECRET_MARKER}"', match.group(2)
            )
            return f"{match.group(1)}{body}{match.group(3)}"

        return cls.JSON_DATA_OBJECT_PATTERN.sub(blank, text)

    @staticmethod
    def _is_credential_value(value: str) -> bool:
        """False for what a credential-named field can hold that is not one.

        A block scalar indicator is one of those: the value is on the lines
        below it, which the block rules mask.
        """
        return bool(value) and not (
            value.startswith(REDACTED_MARKER_PREFIX)
            or value.lower() in NON_CREDENTIAL_VALUES
            or ENV_REFERENCE_PATTERN.match(value)
            or BLOCK_SCALAR_PATTERN.match(value)
        )

    @staticmethod
    def _last_key_word(key: Any) -> str:
        words = KEY_WORD_SPLIT_PATTERN.split(CAMEL_CASE_BOUNDARY_PATTERN.sub(r"\1_\2", str(key)).lower())
        return next((word for word in reversed(words) if word), "")

    @classmethod
    def _mask_env_span(cls, match: "re.Match[str]", start: int, end: int) -> str:
        """Mask `[start, end)` of the match -- offsets into the whole text -- if the entry is a credential."""
        value = match.string[start:end]
        if not cls._is_credential_value(value) or not cls.CREDENTIAL_NAME_PATTERN.match(
            match.group("name")
        ):
            return match.group(0)
        whole = match.group(0)
        return (
            f"{whole[:start - match.start()]}{REDACTED_SECRET_MARKER}{whole[end - match.start():]}"
        )

    @classmethod
    def _mask_env_value_yaml(cls, match: "re.Match[str]") -> str:
        raw = match.group("value").rstrip()
        start = match.start("value")
        if len(raw) > 1 and raw[0] in YAML_QUOTES and raw[-1] == raw[0]:
            return cls._mask_env_span(match, start + 1, start + len(raw) - 1)
        return cls._mask_env_span(match, start, start + len(raw))

    @classmethod
    def _mask_env_value_json(cls, match: "re.Match[str]") -> str:
        return cls._mask_env_span(match, match.start("value"), match.end("value"))

    @classmethod
    def _mask_env_block(cls, match: "re.Match[str]") -> str:
        if not cls.CREDENTIAL_NAME_PATTERN.match(match.group("name")):
            return match.group(0)
        body = match.group("body")
        body_indent = body[: len(body) - len(body.lstrip(" \t"))]
        ending = "\n" if body.endswith("\n") else ""
        return f"{match.group(0)[: match.start('body') - match.start()]}{body_indent}{REDACTED_SECRET_MARKER}{ending}"

    @classmethod
    def _redact_env_pairs(cls, text: str) -> str:
        if "name" not in text or "value" not in text:
            return text
        text = cls.ENV_BLOCK_PATTERN.sub(cls._mask_env_block, text)
        for pattern in cls.ENV_PAIR_YAML_PATTERNS:
            text = pattern.sub(cls._mask_env_value_yaml, text)
        for pattern in cls.ENV_PAIR_JSON_PATTERNS:
            text = pattern.sub(cls._mask_env_value_json, text)
        return text

    @classmethod
    def _redact_nested_json_strings(cls, text: str) -> str:
        """Decode each JSON string that holds escaped JSON or lines, redact it, and re-encode it.

        Re-encoded only when something inside changed, so an untouched string
        keeps its exact escapes. A string that does not decode is left alone.
        """
        if "\\" not in text:
            return text

        def redact_string(match: "re.Match[str]") -> str:
            literal = match.group(0)
            if not any(escape in literal for escape in cls.JSON_NESTED_ESCAPES):
                return literal
            try:
                decoded = json.loads(literal)
            except ValueError:
                return literal
            if not isinstance(decoded, str):
                return literal
            redacted = cls._redact_credentials(decoded)
            if redacted == decoded:
                return literal
            return json.dumps(redacted, ensure_ascii=False)

        return cls.JSON_STRING_PATTERN.sub(redact_string, text)

    @staticmethod
    def _mask_url_password(match: "re.Match[str]") -> str:
        if match.group(2).startswith(REDACTED_MARKER_PREFIX):
            return match.group(0)
        return f"{match.group(1)}{REDACTED_SECRET_MARKER}@"

    @classmethod
    def _mask_kv_value(cls, match: "re.Match[str]") -> str:
        """Mask the value of a credential-named key, unless it is not a credential or already masked."""
        for group, quote in (("dq", '"'), ("sq", "'"), ("bare", "")):
            value = match.group(group)
            if value is not None:
                break
        if not cls._is_credential_value(value):
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{quote}{REDACTED_SECRET_MARKER}{quote}"

    @classmethod
    def redact_text(cls, text: str, rules: Optional[Sequence[RedactionRule]] = None) -> str:
        if not text:
            return text
        text = cls._redact_credentials(text)
        if rules:
            text, _ = cls.apply_rules(text, rules)
        return text

    @classmethod
    def redact_text_counted(
        cls, text: str, rules: Optional[Sequence[RedactionRule]] = None
    ) -> Tuple[str, Dict[str, int]]:
        """:meth:`redact_text`, plus how many substitutions each layer made.

        The built-in layer is counted by the markers it adds rather than by
        instrumenting each pattern, which keeps that chain untouched; a
        credential that already arrived masked is therefore not counted, which
        is the right answer for a log line that says what this call did.
        """
        counts: Dict[str, int] = {}
        if not text:
            return text, counts
        before_credential = sum(text.count(marker) for marker in CREDENTIAL_MARKERS)
        before_email = text.count(EMAIL_MARKER)
        text = cls._redact_credentials(text)
        credential = sum(text.count(marker) for marker in CREDENTIAL_MARKERS) - before_credential
        email = text.count(EMAIL_MARKER) - before_email
        if credential > 0:
            counts[COUNT_NAME_CREDENTIAL] = credential
        if email > 0:
            counts[COUNT_NAME_EMAIL] = email
        if rules:
            text, rule_counts = cls.apply_rules(text, rules)
            counts.update(rule_counts)
        return text, counts

    @classmethod
    def apply_rules(
        cls, text: str, rules: Sequence[RedactionRule]
    ) -> Tuple[str, Dict[str, int]]:
        """Apply configured rules in order; return the text and a count per rule name.

        ``rules`` is a sequence, not a one-shot iterable: :meth:`redact` hands
        the same object to this method once per string it finds, so a
        generator would be spent after the first one and the rest of the
        structure would go out unredacted. ``redact`` materialises what it is
        given for that reason; this method reads ``rules`` once and trusts it.
        """
        counts: Dict[str, int] = {}
        if not text:
            return text, counts
        for rule in rules:

            def substitute(match: "re.Match[str]", rule: RedactionRule = rule) -> str:
                matched = match.group(0)
                value: Optional[str] = matched
                if rule.canonical is not None:
                    value = rule.canonical(matched)
                    if value is None:
                        return matched
                counts[rule.name] = counts.get(rule.name, 0) + 1
                if rule.action == RULE_ACTION_PSEUDONYM:
                    return f"[{rule.name}:{cls.hmac_hash(value)[:PSEUDONYM_HEX_LENGTH]}]"
                return rule.mask

            text = rule.pattern.sub(substitute, text)
        return text, counts

    @staticmethod
    def ip_rules(
        action: str = RULE_ACTION_PSEUDONYM, allow_cidrs: Iterable[str] = ()
    ) -> List[RedactionRule]:
        """The built-in IPv4 and IPv6 literal rules, minus the allowlisted networks.

        ``allow_cidrs`` is where an operator keeps the addresses the model must
        still see -- loopback, a well-known service range. A network that does
        not parse raises here, at load time, rather than silently allowing
        nothing.
        """
        if action == IP_RULE_ACTION_OFF:
            return []
        if action not in RULE_ACTIONS:
            raise ValueError(
                f"ip redaction action {action!r} is not one of {sorted(IP_RULE_ACTIONS)}"
            )
        networks = [ipaddress.ip_network(cidr, strict=False) for cidr in allow_cidrs]

        def canonical(candidate: str) -> Optional[str]:
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                return None
            if any(address.version == n.version and address in n for n in networks):
                return None
            return str(address)

        return [
            RedactionRule(IP_RULE_NAME, IPV4_CANDIDATE_PATTERN, action, canonical),
            RedactionRule(IP_RULE_NAME, IPV6_CANDIDATE_PATTERN, action, canonical),
        ]

    @classmethod
    def rules_from_config(cls, config: Optional[Mapping[str, Any]]) -> List[RedactionRule]:
        """Build the rule list from the mapping the chart renders as ``redaction.yaml``.

        Shape::

            ip:
              action: pseudonym        # mask | pseudonym | off
              allowCidrs: [127.0.0.0/8]
            rules:
              - name: cluster-name
                literal: prod-eu-1     # or `pattern: <regex>`
                action: pseudonym

        Raises ``ValueError`` on anything it does not understand. The gateway
        hook constructs its rules at import, so a bad rule stops the pod at
        startup rather than forwarding requests unredacted.
        """
        config = config or {}
        unknown = set(config) - {CONFIG_KEY_IP, CONFIG_KEY_RULES}
        if unknown:
            raise ValueError(f"unknown redaction config keys: {sorted(unknown)}")
        ip_config = config.get(CONFIG_KEY_IP) or {}
        unknown = set(ip_config) - {CONFIG_KEY_IP_ACTION, CONFIG_KEY_IP_ALLOW_CIDRS}
        if unknown:
            raise ValueError(f"unknown redaction ip keys: {sorted(unknown)}")
        rules = cls.ip_rules(
            ip_config.get(CONFIG_KEY_IP_ACTION, RULE_ACTION_PSEUDONYM),
            ip_config.get(CONFIG_KEY_IP_ALLOW_CIDRS) or (),
        )
        for index, entry in enumerate(config.get(CONFIG_KEY_RULES) or []):
            if not isinstance(entry, Mapping):
                raise ValueError(f"redaction rule #{index} is not a mapping")
            unknown = set(entry) - RULE_CONFIG_KEYS
            if unknown:
                raise ValueError(f"redaction rule #{index}: unknown keys {sorted(unknown)}")
            has_pattern = RULE_CONFIG_KEY_PATTERN in entry
            if has_pattern == (RULE_CONFIG_KEY_LITERAL in entry):
                raise ValueError(
                    f"redaction rule #{index}: give exactly one of `pattern` or `literal`"
                )
            # Strings only, and never empty. YAML turns a bare `yes`, a blank
            # value or `1.10` into something else, and `str()` of that would
            # quietly build a rule for a value the operator never wrote; an
            # empty source, or a pattern that matches the empty string, would
            # put a marker between every character of every request.
            source_key = RULE_CONFIG_KEY_PATTERN if has_pattern else RULE_CONFIG_KEY_LITERAL
            source = entry[source_key]
            if not isinstance(source, str) or not source:
                raise ValueError(
                    f"redaction rule #{index}: `{source_key}` must be a non-empty string, "
                    f"got {source!r}"
                )
            try:
                pattern = re.compile(source if has_pattern else re.escape(source))
            except re.error as error:
                raise ValueError(f"redaction rule #{index}: bad pattern: {error}") from error
            if pattern.match(""):
                raise ValueError(
                    f"redaction rule #{index}: `{source_key}` {source!r} matches the empty "
                    f"string, which would mark every position of every request"
                )
            name = entry.get(RULE_CONFIG_KEY_NAME)
            action = entry.get(RULE_CONFIG_KEY_ACTION, RULE_ACTION_MASK)
            for key, value in ((RULE_CONFIG_KEY_NAME, name), (RULE_CONFIG_KEY_ACTION, action)):
                if not isinstance(value, str):
                    raise ValueError(
                        f"redaction rule #{index}: `{key}` must be a string, got {value!r}"
                    )
            rules.append(RedactionRule(name, pattern, action))
        return rules

    @classmethod
    def _redact_credentials(cls, text: str) -> str:
        text = cls._redact_nested_json_strings(text)
        text = cls.PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
        text = cls._redact_secret_blocks(text)
        text = cls._redact_json_credential_data(text)
        text = cls._redact_env_pairs(text)
        text = cls.CREDENTIAL_BLOCK_SCALAR_PATTERN.sub(cls._mask_credential_block, text)
        text = cls.URL_PASSWORD_PATTERN.sub(cls._mask_url_password, text)
        text = cls.GCP_API_KEY_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.GCP_OAUTH_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.BEARER_TOKEN_PATTERN.sub(r"\1 [REDACTED_SECRET]", text)
        text = cls.GITHUB_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.GITHUB_PAT_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.SLACK_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.JWT_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.PREFIXED_SK_TOKEN_PATTERN.sub(REDACTED_SECRET_MARKER, text)
        text = cls.OPENAI_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.AWS_ACCESS_KEY_ID_PATTERN.sub(REDACTED_SECRET_MARKER, text)
        text = cls.SECRET_KV_PATTERN.sub(cls._mask_kv_value, text)
        text = cls.EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
        return text

    @classmethod
    def redact(cls, value: Any, rules: Optional[Iterable[RedactionRule]] = None) -> Any:
        """Recursively redact a value, keying off mapping keys where present."""
        return cls._redact_structure(value, cls._materialise(rules), None)

    @classmethod
    def redact_counted(
        cls, value: Any, rules: Optional[Iterable[RedactionRule]] = None
    ) -> Tuple[Any, Dict[str, int]]:
        """:meth:`redact`, plus how many substitutions each layer made.

        A value masked because of its key counts as one `credential` (or
        `email`); strings are counted as :meth:`redact_text_counted` counts
        them.
        """
        counts: Dict[str, int] = {}
        return cls._redact_structure(value, cls._materialise(rules), counts), counts

    @staticmethod
    def _materialise(
        rules: Optional[Iterable[RedactionRule]],
    ) -> Optional[Sequence[RedactionRule]]:
        # Materialised once here, because every string below receives the same
        # object and a generator would be spent after the first.
        if rules is not None and not isinstance(rules, (list, tuple)):
            return tuple(rules)
        return rules

    @classmethod
    def _redact_string(
        cls, text: str, rules: Optional[Sequence[RedactionRule]], counts: Optional[Dict[str, int]]
    ) -> str:
        if counts is None:
            return cls.redact_text(text, rules)
        redacted, made = cls.redact_text_counted(text, rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        return redacted

    @classmethod
    def _mask_field(
        cls,
        item: Any,
        marker: str,
        count_name: str,
        rules: Optional[Sequence[RedactionRule]],
        counts: Optional[Dict[str, int]],
    ) -> Any:
        """A string under a sensitive key becomes the marker; a container is walked."""
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="replace")
        elif isinstance(item, str):
            text = item
        else:
            return cls._redact_structure(item, rules, counts)
        # Only a value that is one marker, whole, is already masked here: text
        # that merely starts with one can carry a real credential after it.
        already_masked = bool(WHOLE_MARKER_PATTERN.match(text))
        if already_masked or (
            marker == REDACTED_SECRET_MARKER
            and not text.startswith(REDACTED_MARKER_PREFIX)
            and not cls._is_credential_value(text)
        ):
            return item
        if counts is not None:
            counts[count_name] = counts.get(count_name, 0) + 1
        return marker

    @classmethod
    def _redact_structure(
        cls, value: Any, rules: Optional[Sequence[RedactionRule]], counts: Optional[Dict[str, int]]
    ) -> Any:
        if isinstance(value, bytes):
            decoded = value.decode("utf-8", errors="replace")
            return cls._redact_string(decoded, rules, counts).encode("utf-8")
        if isinstance(value, str):
            return cls._redact_string(value, rules, counts)
        if isinstance(value, dict):
            redacted: Dict[Any, Any] = {}
            credential_object = value.get(KIND_KEY) == CREDENTIAL_OBJECT_KIND
            env_name = value.get(ENV_NAME_KEY)
            credential_env = isinstance(env_name, str) and bool(
                cls.CREDENTIAL_NAME_PATTERN.match(env_name)
            )
            for key, item in value.items():
                words = cls._get_key_words(key)
                if credential_object and key in CREDENTIAL_OBJECT_FIELDS and isinstance(item, dict):
                    redacted[key] = {
                        field: cls._mask_field(
                            payload, REDACTED_SECRET_MARKER, COUNT_NAME_CREDENTIAL, rules, counts
                        )
                        for field, payload in item.items()
                    }
                elif (
                    (credential_env and key == ENV_VALUE_KEY)
                    or (
                        words & cls.SENSITIVE_KEYS
                        and cls._last_key_word(key) not in NON_CREDENTIAL_KEY_SUFFIXES
                    )
                    or (isinstance(key, str) and cls.CREDENTIAL_NAME_PATTERN.match(key))
                ):
                    redacted[key] = cls._mask_field(
                        item, REDACTED_SECRET_MARKER, COUNT_NAME_CREDENTIAL, rules, counts
                    )
                elif "email" in words or "mail" in words:
                    redacted[key] = cls._mask_field(
                        item, EMAIL_MARKER, COUNT_NAME_EMAIL, rules, counts
                    )
                else:
                    redacted[key] = cls._redact_structure(item, rules, counts)
            return redacted
        if isinstance(value, list):
            return [cls._redact_structure(item, rules, counts) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._redact_structure(item, rules, counts) for item in value)
        return value

    @staticmethod
    def hmac_hash(value: str, salt: Optional[bytes] = None) -> str:
        """Pseudonymise ``value`` as a hex HMAC-SHA256 digest.

        Never raises: an unconfigured salt yields a per-process one (see
        :func:`_resolve_salt`) rather than taking the caller down.
        """
        if not value:
            return ""
        return hmac.new(
            salt if salt is not None else _resolve_salt(),
            str(value).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def pseudonymise_identity(cls, value: Any) -> str:
        """Hash ``value`` when it looks like an e-mail address, else pass it through.

        Google Chat reports the user's address as the user id; Slack reports an
        opaque member id, which is already a pseudonym and stays readable.
        """
        text = str(value or "")
        if "@" not in text:
            return text
        return cls.hmac_hash(text)
