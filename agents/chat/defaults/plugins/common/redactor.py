"""Centralised redaction and pseudonymisation for audit logs and session metadata.

Two independent jobs live here because both are needed by the same four call
sites (the two audit hooks, the session store, and the OTel bridge):

* :meth:`AuditRedactor.redact` / :meth:`AuditRedactor.redact_text` strip
  credentials and e-mail addresses out of anything on its way to stdout.
* :meth:`AuditRedactor.hmac_hash` turns a user identity into a stable
  pseudonym, so session rows and span attributes carry a hash rather than the
  address itself.

Deliberately *not* here: raising on a match. These helpers are called from
`pre_gateway_dispatch` and from `start_span`, so an exception — including one
from a regex false positive — would land in the message-dispatch path or in
every span the agent opens. Redaction fails open by design; the enforcement
boundary is Kubernetes RBAC and the credential proxy, not a logging hook.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("hermes.plugin.common.redactor")

SALT_ENV_VAR = "SESSION_KV_SALT"

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
    def redact_text(cls, text: str) -> str:
        if not text:
            return text
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
    def redact(cls, value: Any) -> Any:
        """Recursively redact a value, keying off mapping keys where present."""
        if isinstance(value, bytes):
            return cls.redact_text(value.decode("utf-8", errors="replace")).encode("utf-8")
        if isinstance(value, str):
            return cls.redact_text(value)
        if isinstance(value, dict):
            redacted: Dict[Any, Any] = {}
            for key, item in value.items():
                words = cls._get_key_words(key)
                if words & cls.SENSITIVE_KEYS:
                    redacted[key] = (
                        "[REDACTED_SECRET]" if isinstance(item, (str, bytes)) else cls.redact(item)
                    )
                elif "email" in words or "mail" in words:
                    redacted[key] = (
                        "[REDACTED_EMAIL]" if isinstance(item, (str, bytes)) else cls.redact(item)
                    )
                else:
                    redacted[key] = cls.redact(item)
            return redacted
        if isinstance(value, list):
            return [cls.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls.redact(item) for item in value)
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
