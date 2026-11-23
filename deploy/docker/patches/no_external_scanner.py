"""Stand-in for Hermes' external command scanner, which this image does not run.

Copied over ``/opt/hermes/tools/tirith_security.py`` by
``deploy/docker/Dockerfile``. The upstream module spawns a third-party scanner
binary before each terminal command and, when none is on ``PATH``, downloads the
latest release into ``$HERMES_HOME/bin``. That binary is AGPL-licensed, so the
image neither carries it nor fetches it: this file replaces the module, and the
downloader goes with it.

The three names below are the module's whole surface as Hermes v2026.9.14 uses
it (``tools/approval.py``, ``cli.py``, ``gateway/run.py``). Every command gets
the verdict the upstream module returns with ``security.tirith_enabled: false``.
Hermes' pattern detection (``tools/approval_detection.py``) and the cron gates
(``tools/cron_risk_gate.py``) do not go through this module and still run.
"""

from __future__ import annotations

ACTION_ALLOW = "allow"


def is_platform_supported() -> bool:
    """No scanner build exists for this image, so callers skip their warning."""
    return False


def ensure_installed(*, log_failures: bool = True) -> None:
    """Install nothing and report no scanner."""
    return None


def check_command_security(command: str) -> dict:
    """Allow, with no findings: the shape upstream returns when the scan is off."""
    return {"action": ACTION_ALLOW, "findings": [], "summary": ""}
