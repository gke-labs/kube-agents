"""Security gates for cron runs: risk escalation, code execution refuse, and content checks.

Installed into the image at ``/opt/hermes/tools/cron_risk_gate.py`` and wired
into ``tools/approval.py`` by ``deploy/docker/Dockerfile``.

Addresses the three documented residues in ``deploy/docker/patches/cron_tirith_scan.py``
and Issue #993 (THREAT-002):
1. Terminal escape and control character injection (_ESC pattern).
2. Pure-ASCII lookalike TLD / domain evasion (e.g. kubernetes.io.evil-cdn.co).
3. Unconditional block on execute_code in autonomous cron runs.
4. Risk-keyed mode escalation mapping 'high' risk to 'deny' approval mode.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RISK_LOW = "low"
RISK_HIGH = "high"

MODE_DENY = "deny"

CRON_SCAN_KEY = "cron_scan"
APPROVALS_KEY = "approvals"

MAX_LOG_COMMAND_LEN = 200

MSG_EXECUTE_CODE_REFUSED = (
    "BLOCKED: execute_code is refused during autonomous cron runs "
    "(THREAT-002). Autonomous watchdogs may not execute raw code."
)
MSG_ESC_REFUSED = (
    "BLOCKED: command contains raw terminal escape or control characters "
    "(THREAT-002). Terminal escape injection is refused during cron runs."
)
MSG_LOOKALIKE_TEMPLATE = (
    "BLOCKED: command contains lookalike domain '{host}' mimicking trusted apex '{apex}' "
    "(THREAT-002). Lookalike domain evasion is refused during cron runs."
)

#: Characters that alter terminal state or conceal command strings:
#: C0 control characters (excluding newline \n, tab \t, carriage return \r),
#: DEL (\x7f), and C1 control characters (\x80-\x9f, including 8-bit CSI \x9b).
_ESC = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")

#: Apex domains trusted for Kubernetes and GKE platform operations.
TRUSTED_APEX = (
    "kubernetes.io",
    "googleapis.com",
    "github.com",
    "githubusercontent.com",
    "k8s.io",
    "x-k8s.io",
    "google.com",
    "gke.io",
)

#: Extracts hostname candidates from URLs, CLI flags (--server=...), @hosts, quotes, or tokens.
#: Uses fixed-width lookbehinds so chained delimiters (e.g. comma, semicolon, pipes, brackets)
#: are recognized without prematurely consuming the separator.
_HOST_TOKEN = re.compile(
    r"(?:https?://|--[a-z0-9_-]+=|(?<=^)|(?<=[\s@'\"=,;|([{`]))([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)",
    re.IGNORECASE,
)


def _load_config_readonly() -> dict:
    """Read config.yaml without taking a write lock, or ``{}``.

    Deferred import so importing approval.py at startup does not load configuration early.
    """
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:
        return {}


def cron_scan_enabled(config: Optional[dict]) -> bool:
    """Whether ``approvals.cron_scan`` leaves the scan on. Default: yes.

    Anything other than an explicit false-y value keeps the scan, matching the
    opt-out contract in cron_tirith_scan.py.
    """
    approvals = (config or {}).get(APPROVALS_KEY)
    if not isinstance(approvals, dict):
        return True
    return bool(approvals.get(CRON_SCAN_KEY, True))


def cron_effective_mode(mode: str, risk: str | None) -> str:
    """Escalate approval mode based on the job's declared risk tier.

    A job declared as 'high' risk (or unannotated, defaulting fail-closed to 'high') is
    escalated to 'deny' mode so it is evaluated against strict pattern and policy gates.
    Explicit 'low' risk retains the profile's configured mode (typically 'approve' in
    non-interactive cron runs).
    """
    effective_risk = (risk or RISK_HIGH).strip().lower()
    if effective_risk != RISK_LOW:
        return MODE_DENY
    return mode


def cron_execute_code_block() -> Optional[dict]:
    """Refuse execute_code unconditionally during autonomous cron runs.

    Autonomous watchdogs have no human operator present and must perform their
    actions using declared tools and read-only commands rather than running
    arbitrary embedded scripts.
    """
    logger.warning("Cron risk gate block [execute_code]: %s", MSG_EXECUTE_CODE_REFUSED)
    return {
        "approved": False,
        "message": MSG_EXECUTE_CODE_REFUSED,
    }


def find_lookalike_domain(command: str) -> Optional[tuple[str, str]]:
    """Detect whether any host token in the command mimics a trusted apex domain.

    Returns (detected_host, matched_apex) if a lookalike is detected, else None.
    Legitimate exact matches (e.g. 'k8s.io') and proper subdomains (e.g.
    'storage.googleapis.com', 'raw.githubusercontent.com', 'kubeagents.x-k8s.io')
    pass cleanly.
    """
    if not command or not isinstance(command, str):
        return None

    for match in _HOST_TOKEN.finditer(command):
        raw = match.group(1).lower().rstrip(".:/'\"")
        for apex in TRUSTED_APEX:
            # Legitimate apex or proper subdomain of apex
            if raw == apex or raw.endswith("." + apex):
                continue
            # Lookalike evasion: token contains the apex at a dot/label boundary
            # (e.g. 'kubernetes.io.evil-cdn.co' or 'sub.kubernetes.io.attacker.com')
            # rather than terminating at the apex.
            if raw.startswith(apex + ".") or ("." + apex + ".") in raw:
                return raw, apex
    return None


def cron_content_block(
    command: str,
    *,
    load_config: Optional[Callable[[], dict]] = None,
) -> Optional[dict]:
    """Scan a cron command for content-level evasions not caught by standard pattern filters.

    Checks:
    1. Terminal escape sequences and raw control characters (ANSI / C0 / C1).
    2. Pure-ASCII lookalike TLDs mimicking trusted infrastructure domains.

    Returns a refusal dictionary matching check_all_command_guards contract, or None.
    """
    if not command or not isinstance(command, str):
        return None

    config: dict = {}
    try:
        config = (load_config or _load_config_readonly)() or {}
    except Exception as exc:
        logger.debug("cron risk gate: config unreadable (%s); using defaults", exc)

    if not cron_scan_enabled(config):
        return None

    if _ESC.search(command):
        logger.warning(
            "Cron risk gate block [escape]: command contains raw control/escape characters (command: %s)",
            command[:MAX_LOG_COMMAND_LEN],
        )
        return {
            "approved": False,
            "message": MSG_ESC_REFUSED,
        }

    lookalike = find_lookalike_domain(command)
    if lookalike is not None:
        host, apex = lookalike
        logger.warning(
            "Cron risk gate block [lookalike]: command contains lookalike domain '%s' mimicking apex '%s' (command: %s)",
            host,
            apex,
            command[:MAX_LOG_COMMAND_LEN],
        )
        return {
            "approved": False,
            "message": MSG_LOOKALIKE_TEMPLATE.format(host=host, apex=apex),
        }

    return None
