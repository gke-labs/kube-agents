"""Centralised redaction and pseudonymisation for audit logs and session metadata.

Two independent jobs live here because both are needed by the same four call
sites (the two audit hooks, the session store, and the OTel bridge):

* :meth:`AuditRedactor.redact` / :meth:`AuditRedactor.redact_text` strip
  credentials and e-mail addresses out of anything on its way to stdout.
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
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

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
# The keys a configured rule may carry. `pattern` is a regular expression,
# `literal` an exact string; a rule names exactly one of the two.
RULE_CONFIG_KEYS = frozenset({"name", "pattern", "literal", "action"})
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
CREDENTIAL_MARKERS = ("[REDACTED_SECRET]", "[REDACTED_PRIVATE_KEY]")
EMAIL_MARKER = "[REDACTED_EMAIL]"

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
        return "[REDACTED_" + re.sub(r"[^A-Za-z0-9]+", "_", self.name).upper() + "]"


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
    SECRET_KV_PATTERN = re.compile(
        r"(?i)\b([\w.\-]*?(?:password|passwd|secret|token|api[_-]?key|apikey"
        r"|access[_-]?token|client[_-]?secret))\b"
        r"([\"']?\s*[:=]\s*)([\"']?)([^\"'\s,}{\]]+)\3"
    )
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
        for line in text.split("\n"):
            opener = cls.SECRET_BLOCK_PATTERN.match(line)
            if opener:
                block_indent = len(opener.group(1))
                out.append(line)
                continue
            if block_indent is not None:
                pair = cls.INDENTED_PAIR_PATTERN.match(line)
                if pair and len(pair.group(1)) > block_indent:
                    out.append(f"{pair.group(1)}{pair.group(2)}: [REDACTED_SECRET]")
                    continue
                if line.strip() and (len(line) - len(line.lstrip())) <= block_indent:
                    block_indent = None
            out.append(line)
        return "\n".join(out)

    @classmethod
    def redact_text(cls, text: str, rules: Optional[Iterable[RedactionRule]] = None) -> str:
        if not text:
            return text
        text = cls._redact_credentials(text)
        if rules:
            text, _ = cls.apply_rules(text, rules)
        return text

    @classmethod
    def redact_text_counted(
        cls, text: str, rules: Optional[Iterable[RedactionRule]] = None
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
        cls, text: str, rules: Iterable[RedactionRule]
    ) -> Tuple[str, Dict[str, int]]:
        """Apply configured rules in order; return the text and a count per rule name."""
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
            if ("pattern" in entry) == ("literal" in entry):
                raise ValueError(
                    f"redaction rule #{index}: give exactly one of `pattern` or `literal`"
                )
            source = entry.get("pattern")
            if source is None:
                source = re.escape(str(entry["literal"]))
            try:
                pattern = re.compile(str(source))
            except re.error as error:
                raise ValueError(f"redaction rule #{index}: bad pattern: {error}") from error
            rules.append(
                RedactionRule(
                    str(entry.get("name", "")), pattern, entry.get("action", RULE_ACTION_MASK)
                )
            )
        return rules

    @classmethod
    def _redact_credentials(cls, text: str) -> str:
        text = cls.PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
        text = cls._redact_secret_blocks(text)
        text = cls.GCP_API_KEY_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.GCP_OAUTH_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.BEARER_TOKEN_PATTERN.sub(r"\1 [REDACTED_SECRET]", text)
        text = cls.GITHUB_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.GITHUB_PAT_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.SLACK_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.JWT_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.OPENAI_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.SECRET_KV_PATTERN.sub(r"\1\2\3[REDACTED_SECRET]\3", text)
        text = cls.EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
        return text

    @classmethod
    def redact(cls, value: Any, rules: Optional[Iterable[RedactionRule]] = None) -> Any:
        """Recursively redact a value, keying off mapping keys where present."""
        if isinstance(value, bytes):
            return cls.redact_text(value.decode("utf-8", errors="replace"), rules).encode("utf-8")
        if isinstance(value, str):
            return cls.redact_text(value, rules)
        if isinstance(value, dict):
            redacted: Dict[Any, Any] = {}
            for key, item in value.items():
                words = cls._get_key_words(key)
                if words & cls.SENSITIVE_KEYS:
                    redacted[key] = (
                        "[REDACTED_SECRET]"
                        if isinstance(item, (str, bytes))
                        else cls.redact(item, rules)
                    )
                elif "email" in words or "mail" in words:
                    redacted[key] = (
                        "[REDACTED_EMAIL]"
                        if isinstance(item, (str, bytes))
                        else cls.redact(item, rules)
                    )
                else:
                    redacted[key] = cls.redact(item, rules)
            return redacted
        if isinstance(value, list):
            return [cls.redact(item, rules) for item in value]
        if isinstance(value, tuple):
            return tuple(cls.redact(item, rules) for item in value)
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
