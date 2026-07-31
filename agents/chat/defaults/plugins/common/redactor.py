"""Centralized stateless redaction engine for audit logs and pseudonymization."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any, Dict, Optional, Set


class AuditRedactor:
    """Stateless regex and dictionary redactor for secrets and PII."""

    PRIVATE_KEY_PATTERN = re.compile(
        r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
        re.IGNORECASE,
    )
    GCP_API_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z-_]{35}")
    BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+([a-zA-Z0-9_\-\.=]{15,})\b")
    GITHUB_TOKEN_PATTERN = re.compile(r"ghp_[a-zA-Z0-9]{36}")
    OPENAI_TOKEN_PATTERN = re.compile(r"sk-[a-zA-Z0-9]{48}")
    SECRET_KV_PATTERN = re.compile(
        r"(?i)\b(password|secret|token|api_key|apikey|access_token|client_secret)\b([\"']?\s*[:=]\s*)([\"']?)([^\"'\s,}{\]]+)\3"
    )
    EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    SENSITIVE_KEYS = {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "client_secret",
        "authorization",
        "auth",
        "private_key",
    }

    @staticmethod
    def _get_key_words(key: Any) -> Set[str]:
        s = str(key)
        # Split camelCase and PascalCase
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()
        words = set(re.split(r"[^a-z0-9]+", s))
        words.add(s.lower())
        return {w for w in words if w}

    @classmethod
    def redact_text(cls, text: str) -> str:
        if not text:
            return text
        text = cls.PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", text)
        text = cls.GCP_API_KEY_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED_SECRET]", text)
        text = cls.GITHUB_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.OPENAI_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        text = cls.SECRET_KV_PATTERN.sub(r"\1\2\3[REDACTED_SECRET]\3", text)
        text = cls.EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
        return text

    @classmethod
    def redact(cls, value: Any) -> Any:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
            return cls.redact_text(text).encode("utf-8")
        elif isinstance(value, str):
            return cls.redact_text(value)
        elif isinstance(value, dict):
            redacted_dict: Dict[Any, Any] = {}
            for k, v in value.items():
                words = cls._get_key_words(k)
                if words & cls.SENSITIVE_KEYS:
                    redacted_dict[k] = "[REDACTED_SECRET]" if isinstance(v, (str, bytes)) else cls.redact(v)
                elif any("email" in w or "mail" == w for w in words):
                    redacted_dict[k] = "[REDACTED_EMAIL]" if isinstance(v, (str, bytes)) else cls.redact(v)
                else:
                    redacted_dict[k] = cls.redact(v)
            return redacted_dict
        elif isinstance(value, list):
            return [cls.redact(item) for item in value]
        elif isinstance(value, tuple):
            return tuple(cls.redact(item) for item in value)
        return value

    @staticmethod
    def hmac_hash(value: str, salt: Optional[bytes] = None) -> str:
        if not value:
            return ""
        if salt is None:
            salt_str = os.getenv("SESSION_KV_SALT")
            if not salt_str:
                raise ValueError(
                    "SESSION_KV_SALT environment variable is not configured"
                )
            salt = salt_str.encode("utf-8")
        return hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()

