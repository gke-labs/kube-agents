"""Shared security helpers for the audit plugins and hooks.

Not a Hermes plugin: there is no `plugin.yaml` and nothing registers, so the
loader never treats this directory as loadable. It sits under `plugins/` only
so that it ships and syncs with the plugins that import it.
"""

from .redactor import AuditRedactor, SALT_ENV_VAR

__all__ = ["AuditRedactor", "SALT_ENV_VAR"]
