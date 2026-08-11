"""Run the Tirith content scan on cron commands that skip the approval prompt.

Installed into the image at ``/opt/hermes/tools/cron_tirith_scan.py`` and wired
into ``tools/approval.py`` by ``deploy/docker/Dockerfile``.

The defect
----------
``tools/approval.py``'s ``check_all_command_guards`` is the single pre-exec gate
every ``terminal()`` command passes through. Its non-interactive branch reads::

    if not is_cli and not is_gateway and not is_ask:
        # Cron sessions: respect cron_mode config
        if _is_cron_approval_context():
            if _get_cron_approval_mode() == "deny":
                ...detect_dangerous_command(), then check_command_security()...
        return {"approved": True, "message": None}

Upstream put the Tirith call *inside* the ``deny`` arm. ``approvals.cron_mode``
has exactly two effective values — ``_get_cron_approval_mode`` maps
``{approve, off, allow, yes}`` to ``approve`` and everything else, including a
typo, to ``deny`` — so setting ``approve`` does not merely stop the run blocking
on a prompt it cannot answer. It returns ``approved`` before the scanner is ever
reached, and every command in every cron run executes with no content scan at
all.

That conflation is the bug. Answering the approval prompt and scanning the
command are separate questions, and only the first one needs a human. An
unattended run genuinely cannot answer a prompt; it has no trouble at all being
told "no".

Why this matters here rather than in the abstract
-------------------------------------------------
``approvals.cron_mode: approve`` is a fleet-wide default in this deployment. It
is written into the platform profile's template by
``deploy/shared/defaults/config.yaml``, into the ``default`` profile's overlay by
the operator (``platformagent_manifests.go``), and it is the value operators
hand-write into ``AgentPlugin`` config. Every governance watchdog on
``agents/platform/cron/jobs.json`` therefore ran unscanned.

One of those seven jobs is ``github-issue-resolver``, firing every 30 minutes to
"poll, triage, investigate, and resolve unaddressed open issues on our target
repo". Its input is issue text written by anyone with a GitHub account, and its
output is shell commands run with the platform agent's credentials. Tirith's
subject matter is content-level threats in a command string — confusable
Unicode and homograph hostnames, pipe-to-interpreter, encoded payloads — which
is the shape of what a hostile issue body can talk a model into typing. The scan
buys two distinct things over ``DANGEROUS_PATTERNS`` alone, both measured
against the shipped image on 2026-08-08:

* Homographs are outside the pattern layer entirely. A Cyrillic-e
  ``kubernetеs.io`` matches no pattern, and under ``cron_mode: approve``
  nothing else looked at it either.
* ``curl | sh`` and base64-to-shell *are* patterns, but ``approve`` is precisely
  the instruction not to block on a pattern. Tirith refuses them on its own
  authority, so the waiver stops shipping them.

Do not over-read the coverage. A pure-ASCII lookalike TLD
(``kubernetes.io.evil-cdn.co``) and terminal escape injection are caught by
neither layer — ANSI is stripped before pattern matching, and real ESC payloads
did not trip the scanner either.

What this patch does
--------------------
The scan is moved out from under the mode. ``cron_tirith_block`` runs the same
``tools.tirith_security.check_command_security`` the ``deny`` arm runs, on the
``approve`` path as well, and returns a block result when the scanner reports a
verdict. ``cron_mode`` keeps its documented job — deciding whether a
*pattern*-flagged dangerous command is refused or allowed to run unprompted —
and loses the undocumented one.

The wiring calls this only when the mode is not ``deny``, so the ``deny`` arm's
own Tirith call is left exactly as upstream wrote it and no command is scanned
twice (each scan is a subprocess spawn).

Why a finding blocks the command rather than being logged
---------------------------------------------------------
Blocking is the safe option, and here it is also the cheap one:

* **A broken scanner cannot stop the roster.** Every infrastructure failure mode
  is already handled *inside* ``check_command_security`` and fails open under
  the default ``security.tirith_fail_open: true`` — spawn failure, timeout,
  unexpected exit code, the consecutive-crash circuit breaker, and an
  unsupported platform all return ``action: allow``. Reaching a block therefore
  requires the scanner to have actually run and actually objected to the text.
  The one remaining failure — the module not importing at all — is handled
  below, and honours the same ``tirith_fail_open`` flag the ``deny`` arm does.
* **A finding on an unattended run is a statement about content, not shape.**
  It says this command contains something the operator did not write. Logging
  and continuing would run the exact string the scanner objected to, and nobody
  is watching the log at 06:20 on a Sunday.
* **It is recoverable and visible.** The agent receives an ordinary denial
  string, keeps running, and can take another route; the gateway logs the
  refusal (``Cron Tirith block: ...``). The execution ledger has no per-command
  rows, so it does not record the refusal, and ``save_job_output`` persists the
  run's final response rather than its tool results — the refusal reaches the
  saved output only if the model narrates it. This is not a new class of
  outcome — it is the treatment ``cron_mode: deny`` already gives the identical
  scan, now available without also refusing every ``kubectl delete`` the
  watchdogs legitimately issue.

``approvals.cron_scan: false`` turns the scan back off for one profile, for the
case where a watchdog trips the scanner for a reason the operator has examined
and accepted. It is deliberately a separate key from ``cron_mode``: making the
scan opt-in through a new ``cron_mode`` value would have left every
already-written ``cron_mode: approve`` — the operator's overlay, every hand-authored
``AgentPlugin`` — silently unscanned, which is the state this patch exists to end.

Out of scope, recorded so it is not mistaken for covered
--------------------------------------------------------
``check_execute_code_guard`` still auto-approves ``execute_code`` under
``cron_mode: approve``. Tirith scans POSIX shell strings (``--shell posix``);
handing it a Python script would produce noise, not a verdict. A cron run can
therefore still reach ``subprocess`` through ``execute_code`` without a content
scan. Closing that needs a different scanner, not this one.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Per-profile opt-out, read from ``approvals.cron_scan`` in config.yaml.
#: Absent means enabled: the scan is the default and turning it off is the
#: decision that has to be written down.
CRON_SCAN_KEY = "cron_scan"


def _load_config_readonly() -> dict:
    """Read config.yaml without taking a write lock, or ``{}``.

    Deferred import: ``tools/approval.py`` is imported very early in Hermes
    startup and this module is imported from inside one of its functions, so
    nothing here may pull in configuration at module import time.
    """
    from hermes_cli.config import load_config_readonly

    return load_config_readonly() or {}


def _default_scan(command: str) -> dict:
    """Run the real Tirith scan. Raises ``ImportError`` if it is not installed.

    Kept as a named default rather than inlined so the unit suite can drive
    ``cron_tirith_block`` without ``/opt/hermes`` on ``sys.path``, and so the
    ``ImportError`` this raises reaches the caller's handler unchanged.
    """
    from tools.tirith_security import check_command_security

    return check_command_security(command)


def _describe(result: dict) -> str:
    """Last-resort renderer for a Tirith result.

    The wiring passes ``tools.approval._format_tirith_description`` in, so this
    is only reached by the unit suite, which cannot import Hermes. Keep it a
    recognisable subset of the real thing rather than a second implementation
    that could drift into disagreeing with the message users actually see.
    """
    findings = (result or {}).get("findings") or []
    parts = []
    for finding in findings:
        severity = finding.get("severity", "")
        title = finding.get("title", "")
        if title:
            parts.append(f"[{severity}] {title}" if severity else title)
    if not parts:
        summary = (result or {}).get("summary") or "security issue detected"
        return f"Security scan: {summary}"
    return "Security scan — " + "; ".join(parts)


def cron_scan_enabled(config: Optional[dict]) -> bool:
    """Whether ``approvals.cron_scan`` leaves the scan on. Default: yes.

    Anything other than an explicit false-y value keeps the scan, including a
    malformed config — an operator who cannot write the key correctly has not
    opted out of it.
    """
    approvals = (config or {}).get("approvals")
    if not isinstance(approvals, dict):
        return True
    return bool(approvals.get(CRON_SCAN_KEY, True))


def tirith_fail_open(config: Optional[dict]) -> bool:
    """Whether an unavailable scanner should allow the command through.

    Mirrors the ``ImportError`` handler on ``check_all_command_guards``' cron
    ``deny`` arm exactly, including its ordering: ``tirith_fail_open`` is only
    consulted when ``tirith_enabled`` is true, so an operator who switched
    Tirith off entirely is not then blocked by its absence.
    """
    security = (config or {}).get("security")
    if not isinstance(security, dict):
        return True
    if not security.get("tirith_enabled", True):
        return True
    return bool(security.get("tirith_fail_open", True))


def _block(message: str) -> dict:
    """A refusal in the shape ``check_all_command_guards`` returns.

    Two keys and no more, matching the cron ``deny`` arm above it. Callers
    branch on ``approved`` and surface ``message`` to the model verbatim.
    """
    return {"approved": False, "message": message}


def cron_tirith_block(
    command: str,
    *,
    describe: Optional[Callable[[dict], str]] = None,
    scan: Optional[Callable[[str], dict]] = None,
    load_config: Optional[Callable[[], dict]] = None,
) -> Optional[dict]:
    """Scan one cron command; return a refusal, or ``None`` to let it run.

    Called from ``check_all_command_guards`` when the session is a cron session
    and ``approvals.cron_mode`` is not ``deny`` — that is, on the path where the
    approval prompt has been waived because no one is there to answer it. The
    waiver covers the prompt. It does not cover the scanner, and this restores
    the scanner.

    ``describe``, ``scan`` and ``load_config`` exist so the unit suite can drive
    the decision without Hermes on ``sys.path``; the wiring passes ``describe``
    (Hermes' own ``_format_tirith_description``, so the model sees the same
    finding text an interactive user would) and leaves the other two default.
    """
    config: dict = {}
    try:
        config = (load_config or _load_config_readonly)() or {}
    except Exception as exc:
        # An unreadable config must not decide security policy by accident. Fall
        # through on the built-in defaults, which scan and fail open — the same
        # posture the rest of approval.py takes when its config read fails.
        logger.debug("cron Tirith scan: config unreadable (%s); using defaults", exc)

    if not cron_scan_enabled(config):
        return None

    try:
        result = (scan or _default_scan)(command) or {}
    except ImportError:
        # Tirith is not installed. The deny arm's handler is reproduced rather
        # than shared because this is a policy decision an operator can have set
        # either way, and the two paths must not drift apart.
        if tirith_fail_open(config):
            return None
        logger.warning(
            "cron Tirith scan: scanner unavailable and security.tirith_fail_open "
            "is false; blocking (command: %s)",
            command[:200],
        )
        return _block(
            "BLOCKED: the Tirith security scanner could not be imported and "
            "security.tirith_fail_open is false, so this command cannot be "
            "silently allowed — and cron jobs run without a user present to "
            "approve it. Find an alternative approach, install tirith, or set "
            "security.tirith_fail_open: true if an unscanned cron run is "
            "acceptable on this profile."
        )

    if result.get("action") not in ("block", "warn"):
        return None

    description = (describe or _describe)(result)
    logger.warning(
        "Cron Tirith block: %s (command: %s)", description, command[:200]
    )
    return _block(
        f"BLOCKED: {description} — and this is an unattended cron run. "
        "approvals.cron_mode waives the approval prompt, which no one is here "
        "to answer; it does not waive the content scan. The scanner objected to "
        "what is IN this command rather than to the shape of it, so look for "
        "something you did not author: a URL, a piped installer, or text lifted "
        "out of an issue, a log line, or a tool result. Find another way to do "
        "this, or narrow the command until it scans clean. Do not retry it "
        "unchanged."
    )
