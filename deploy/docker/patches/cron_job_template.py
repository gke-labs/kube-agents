"""The origin a tick-started cron run carries, and the per-job record behind it.

Installed into the image at ``/opt/hermes/tools/cron_job_template.py`` and
wired into ``cron/scheduler.py`` and ``tools/cronjob_tools.py`` by
``deploy/docker/patches/apply_cron_run_scope.py``. The design is
``docs/designs/proposal-origin.md`` §2.1, "A cron run".

A run the scheduler's own tick started is ``audit`` or ``scheduled`` only when
the job's definition equals its entry in the image's template,
:data:`TEMPLATE_JOBS_PATH`: the same id, prompt and skills. The profile's own
``cron/jobs.json`` is the agent's to write, so an id alone proves nothing.
``audit`` is a matching job whose ``skills`` names ``fleet-audit``; any other
matching job is ``scheduled``.

A job that no longer matches, or that the model created, carries the origin
recorded for its id in the Session KV server (``GET /v1/cron/jobs/{id}/origin``).
:func:`record_job_origin` writes that record from the ``cronjob_manage``
handler on ``create`` and ``update``, with the creating turn's origin
(``kanban_origin.turn_origin``), never from the jobs file. A job with neither
is ``other``.

Nothing here raises: an unreadable template matches nothing, and an unreachable
Session KV server is no record.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The image's shipped cron roster (``agents/platform/cron/jobs.json``).
TEMPLATE_JOBS_PATH = "/opt/platform-template/cron/jobs.json"
FLEET_AUDIT_SKILL = "fleet-audit"

#: The origin vocabulary, as ``agents/platform/scripts/session_kv_server.py``
#: ``ORIGINS`` spells it.
ORIGINS = ("chat", "console", "api", "event", "drift", "audit", "scheduled", "issue", "other")
ORIGIN_AUDIT = "audit"
ORIGIN_SCHEDULED = "scheduled"
NO_ORIGIN = ("other", "")

#: The Session KV server's loopback address and key, as ``kanban_origin`` spells them.
SESSION_KV_URL = "http://127.0.0.1:8699"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
CRON_JOB_ORIGIN_PATH = "/v1/cron/jobs/{job_id}/origin"
#: A tick or a cronjob_manage call waits at most this long, on loopback.
SESSION_KV_TIMEOUT_SECONDS = 2.0
HTTP_NOT_FOUND = 404

#: Where Hermes installs the module that resolves the creating turn's origin,
#: and the name a test loads it under.
KANBAN_ORIGIN_MODULES = ("hermes_cli.kanban_origin", "kanban_origin")


def _skills(definition: Mapping[str, Any]) -> list[str]:
    """The job's skill list; Hermes may store none as ``None`` or a legacy ``skill``."""
    skills = definition.get("skills")
    if isinstance(skills, (list, tuple)):
        return [str(name) for name in skills]
    legacy = definition.get("skill")
    return [str(legacy)] if legacy else []


def _template_entry(job_id: str) -> Mapping[str, Any] | None:
    """The shipped entry with ``job_id``, or None (also for an unreadable template)."""
    try:
        with open(TEMPLATE_JOBS_PATH, encoding="utf-8") as handle:
            roster = json.load(handle)
    except (OSError, ValueError):
        return None
    jobs = roster.get("jobs") if isinstance(roster, dict) else roster
    if not isinstance(jobs, list):
        return None
    for entry in jobs:
        if isinstance(entry, dict) and str(entry.get("id") or "") == job_id:
            return entry
    return None


def shipped_job_matches(job: Mapping[str, Any]) -> bool:
    """True when the template's entry with ``job``'s id has the same prompt and skills."""
    job_id = str(job.get("id") or "")
    entry = _template_entry(job_id) if job_id else None
    if entry is None:
        return False
    return (job.get("prompt") or "") == (entry.get("prompt") or "") and _skills(job) == _skills(entry)


def _headers() -> dict[str, str] | None:
    key = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else None


def _origin_url(job_id: str) -> str:
    return SESSION_KV_URL + CRON_JOB_ORIGIN_PATH.format(job_id=urllib.parse.quote(job_id, safe=""))


def recorded_origin(job_id: str) -> tuple[str, str] | None:
    """The ``(origin, platform)`` recorded for ``job_id``, or None when there is none."""
    headers = _headers()
    if not job_id or headers is None:
        return None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(_origin_url(job_id), headers=headers), timeout=SESSION_KV_TIMEOUT_SECONDS
        ) as response:
            loaded = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code != HTTP_NOT_FOUND:
            logger.warning("cron origin: job %s record unreadable: HTTP %s", job_id, exc.code)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:  # down, timed out, or not JSON
        logger.warning("cron origin: job %s record unreadable: %r", job_id, exc)
        return None
    if not isinstance(loaded, dict):
        return None
    origin, platform = loaded.get("origin"), loaded.get("platform")
    if not isinstance(origin, str) or origin not in ORIGINS:
        return None
    return origin, platform if isinstance(platform, str) else ""


def run_origin(job: Mapping[str, Any]) -> tuple[str, str]:
    """The ``(origin, platform)`` of a tick-started run of ``job``. Never raises."""
    try:
        if shipped_job_matches(job):
            return (ORIGIN_AUDIT if FLEET_AUDIT_SKILL in _skills(job) else ORIGIN_SCHEDULED), ""
        return recorded_origin(str(job.get("id") or "")) or NO_ORIGIN
    except Exception as exc:  # noqa: BLE001 — an unresolved run is other, never a failed run
        logger.warning("cron origin: job %r unresolved: %r", job.get("id"), exc)
        return NO_ORIGIN


def _turn_origin() -> tuple[str, str]:
    """The creating turn's origin (``kanban_origin.turn_origin``), or ``other`` without it."""
    for name in KANBAN_ORIGIN_MODULES:
        try:
            return importlib.import_module(name).turn_origin()
        except ImportError:
            continue
    return NO_ORIGIN


def record_job_origin(job_id: str) -> bool:
    """Record the current turn's origin for ``job_id`` (an upsert); True when written.

    Called by the ``cronjob_manage`` handler after ``create`` and ``update``.
    Never raises: a job with no record runs as ``other`` when it stops matching
    the template.
    """
    headers = _headers()
    if not job_id or headers is None:
        return False
    try:
        origin, platform = _turn_origin()
        body = json.dumps({"origin": origin, "platform": platform or ""}).encode()
        request = urllib.request.Request(
            _origin_url(job_id),
            data=body,
            method="PUT",
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=SESSION_KV_TIMEOUT_SECONDS):
            return True
    except Exception as exc:  # noqa: BLE001 — the create or update itself has succeeded
        logger.warning("cron origin: could not record job %s: %r", job_id, exc)
        return False
