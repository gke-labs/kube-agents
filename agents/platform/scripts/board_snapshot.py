"""The board snapshot behind the Slack Home tab: rows this pod already wrote, read in one pass.

``session_kv_server.py`` serves it as ``GET /v1/board``; the Hermes bridge relays
it to the A2A gateway over the bus (``a2a/lib/board.go`` is the wire shape and
holds the field names this module writes). No model is in the loop:

- clusters: every Cluster Agent profile's ``cluster_identity`` (the managed
  fleet), each with the open findings the findings queue holds for it, ranked
  by the queue's own order (``findings_queue.ranked_findings``);
- running: the kanban cards whose status is ``running``, the cluster their
  assignee profile is scoped to beside each;
- prs: the newest ``pr_opened`` events (``agents/platform/plugins/pr_opened``);
- audit: the most recent fleet-audit run to finish, from the report store
  (``skills/fleet-audit/scripts/report_status.py``);
- first_report_pending: onboarding has filed its sweep, its first report is
  not out yet (the findings nudge's own marker pair,
  ``findings_nudge.first_report_markers``), and delivery has not yet put the
  sweep's findings in the queue (``.bootstrap_findings_registered``), so a
  cluster with no open finding has not been checked yet; first_message_pending
  adds that no message has primed onboarding yet (no ``.bootstrap_greeted``)
  and that the sweep is young enough for a message to prime it.

Every list is capped here, so the answer stays a few kilobytes. A source that
cannot be read is an error on the whole snapshot, never an empty list: an
unreadable findings table must not draw as a healthy fleet.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import findings_nudge
import findings_queue

HERMES_HOME = os.environ.get("HERMES_HOME") or "/opt/data"
PROFILES_DIR = Path(HERMES_HOME) / "profiles"
# The onboarding plugin's marker for a message that primed onboarding, in the
# same home as the nudge's pair. Past FIRST_MESSAGE_MAX_AGE_SECONDS after the
# sweep was filed, the plugin primes nothing (its SCAN_MAX_AGE_SECONDS), so
# the tab stops asking for a message then.
FIRST_MESSAGE_MARKER = ".bootstrap_greeted"
FIRST_MESSAGE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# Delivery's record that the sweep's findings are in the queue
# (agents/chat/scripts/bootstrap_delivery.py, FINDINGS_REGISTERED_MARKER),
# written before its claim: from then on the fleet was checked, whether or not
# the report's post ever lands.
FINDINGS_REGISTERED_MARKER = ".bootstrap_findings_registered"
# The fleet-audit skill's scripts, scaffolded into the platform profile; the
# report store's reader (report_status) and the stream titles (audit_report)
# live there.
FLEET_AUDIT_SCRIPTS = PROFILES_DIR / "platform" / "skills" / "fleet-audit" / "scripts"

CLUSTERS_MAX = 15
RUNNING_MAX = 5
PRS_MAX = 5
STATUS_RUNNING = "running"
PR_OPENED_KIND = "pr_opened"
# What a malformed row (a hand-written payload, a text timestamp) raises on the
# way into the wire shape; that row is skipped. OSError is gmtime() refusing a
# year past a C int, which datetime.fromtimestamp documents alongside the rest.
ROW_ERRORS = (TypeError, ValueError, OverflowError, OSError)
# The range of the Go ints the wire shape decodes into (a2a/lib/board.go); a
# number outside it would fail the gateway's decode of the whole board.
WIRE_INT_MIN = -(1 << 63)
WIRE_INT_MAX = (1 << 63) - 1
# Joins a project and a cluster when a cluster name repeats across projects.
PROJECT_CLUSTER_SEP = "/"
CONFIG_NAME = "config.yaml"
IDENTITY_KEY = "cluster_identity"


class BoardError(Exception):
    """A source the snapshot needs could not be read."""


def _iso(epoch: float | None) -> str | None:
    """A board timestamp (Unix seconds) as RFC 3339, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _wire_int(value: Any) -> int:
    """``value`` as an int the wire shape's Go int holds; raises ValueError otherwise.

    None (a field the row leaves out) is 0. Anything else must already be an
    integer, or a float with no fraction: a string, a list or ``41.9`` is not
    coerced into some other number.
    """
    if value is None:
        return 0
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{value!r} is not an integer")
    if not WIRE_INT_MIN <= value <= WIRE_INT_MAX:
        raise ValueError(f"{value} is outside the wire int range")
    return value


def _rfc3339(value: Any) -> str:
    """An ISO timestamp with its zone, re-emitted in UTC; raises ValueError for anything else."""
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError(f"{value!r} names no zone")
    return stamp.astimezone(timezone.utc).isoformat()


def cluster_identities(profiles_dir: Path | None = None) -> dict[str, tuple[str, str]]:
    """Profile name -> (project, cluster), for every profile stamped with a ``cluster_identity``."""
    import yaml  # lazy, like cluster_agent_profile's reader

    profiles_dir = profiles_dir or PROFILES_DIR
    out: dict[str, tuple[str, str]] = {}
    if not profiles_dir.is_dir():
        return out
    for home in sorted(profiles_dir.iterdir()):
        config = home / CONFIG_NAME
        if not config.is_file():
            continue
        try:
            data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        identity = data.get(IDENTITY_KEY) if isinstance(data, dict) else None
        if isinstance(identity, dict) and identity.get("cluster"):
            out[home.name] = (str(identity.get("project") or ""), str(identity["cluster"]))
    return out


def cluster_labels(keys: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Each (project, cluster) as the board names it: the cluster, or project/cluster where the name repeats."""
    projects: dict[str, set[str]] = {}
    for project, cluster in keys:
        projects.setdefault(cluster, set()).add(project)
    return {
        (project, cluster): f"{project}{PROJECT_CLUSTER_SEP}{cluster}" if len(projects[cluster]) > 1 else cluster
        for project, cluster in keys
    }


def clusters(findings: list[dict], roster: set[tuple[str, str]], labels: dict[tuple[str, str], str]) -> list[dict]:
    """One row per cluster, worst first: the managed fleet plus any cluster a finding names.

    ``findings`` is the open queue in rank order, so the first finding seen for
    a cluster is its top one. Clusters with findings sort by their top
    finding's rank; clean ones follow by name. A cluster is its (project,
    cluster) pair, the findings queue's own key, so two projects' ``prod`` are
    two rows.
    """
    rows: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for f in findings:
        key = (str(f.get("project") or ""), str(f.get("cluster") or ""))
        if not key[1]:
            continue
        if key not in rows:
            rows[key] = {"name": labels[key], "findings": 0, "severity": f.get("severity") or "", "top": f.get("title") or ""}
            order.append(key)
        rows[key]["findings"] += 1
    for key in sorted(roster - set(rows), key=lambda k: labels[k]):
        rows[key] = {"name": labels[key], "findings": 0}
        order.append(key)
    return [rows[key] for key in order]


def running(kanban: sqlite3.Connection, identities: dict[str, tuple[str, str]], labels: dict[tuple[str, str], str]) -> list[dict]:
    rows = kanban.execute(
        "SELECT title, assignee, started_at, created_at FROM tasks WHERE status = ? "
        "ORDER BY COALESCE(started_at, created_at) DESC LIMIT ?",
        (STATUS_RUNNING, RUNNING_MAX),
    ).fetchall()
    out = []
    for title, assignee, started, created in rows:
        try:
            card = {"title": str(title or ""), "started_at": _iso(started if started is not None else created)}
        except ROW_ERRORS:
            continue  # a malformed row costs its own line, not the board
        if assignee in identities:
            card["cluster"] = labels[identities[assignee]]
        out.append(card)
    return out


def prs(kanban: sqlite3.Connection) -> list[dict]:
    """The newest pull requests cards opened, one row per URL."""
    rows = kanban.execute(
        "SELECT payload, created_at FROM task_events WHERE kind = ? ORDER BY created_at DESC, id DESC",
        (PR_OPENED_KIND,),
    ).fetchall()
    out: list[dict] = []
    seen: set[str] = set()
    for payload, created in rows:
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not data["url"] or data["url"] in seen:
            continue
        try:
            row = {
                "url": data["url"],
                "number": _wire_int(data.get("number")),
                "title": str(data.get("title") or ""),
                "opened_at": _iso(created),
            }
        except ROW_ERRORS:
            continue  # a malformed row costs its own line, not the board
        seen.add(data["url"])
        out.append(row)
        if len(out) == PRS_MAX:
            break
    return out


def last_audit(projection: dict, name_of: Callable[[str], str]) -> dict | None:
    """The newest finished run across every stream and repository in report_status's projection.

    Each run is normalised to the wire shape first; one whose fields will not
    (a zoneless or unparsable ``finished_at``, a non-numeric issue) is skipped.
    """
    best: dict | None = None
    for audit_id, stream in (projection.get("streams") or {}).items():
        for repo in (stream.get("repos") or {}).values():
            latest = repo.get("latest")
            if not isinstance(latest, dict) or not latest.get("finished_at") or latest.get("findings") is None:
                continue
            try:
                run = {
                    "name": str(name_of(audit_id)),
                    "findings": _wire_int(latest["findings"]),
                    "critical": _wire_int(latest.get("critical")),
                    "issue_url": str(latest.get("issue_url") or ""),
                    "issue": _wire_int(latest.get("issue_number")),
                    "finished_at": _rfc3339(latest["finished_at"]),
                }
            except ROW_ERRORS:
                continue
            if best is None or run["finished_at"] > best["finished_at"]:
                best = run
    return best


def _fleet_audit_modules() -> tuple[Any, Callable[[str], str]]:
    """report_status, and the stream-title lookup (audit_id itself when audit_report will not load)."""
    scripts = str(FLEET_AUDIT_SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import report_status

    try:
        import audit_report

        def name_of(audit_id: str) -> str:
            spec = audit_report.AUDITS.get(audit_id)
            return spec.title if spec else audit_id

    except Exception:  # noqa: BLE001 — a title is cosmetic; the id still names the stream
        def name_of(audit_id: str) -> str:
            return audit_id

    return report_status, name_of


def snapshot(
    session_db: sqlite3.Connection,
    kanban: sqlite3.Connection | None,
    now: datetime,
    profiles_dir: Path | None = None,
    audit: Callable[[], dict | None] | None = None,
) -> dict:
    """The whole board. ``kanban`` is None before the board's first card, which is an empty board."""
    try:
        findings = findings_queue.ranked_findings(session_db)
    except sqlite3.Error as exc:
        raise BoardError(f"the findings queue could not be read: {exc}") from None
    identities = cluster_identities(profiles_dir)
    roster = set(identities.values())
    labels = cluster_labels(roster | {(str(f.get("project") or ""), str(f.get("cluster") or "")) for f in findings})
    fleet = clusters(findings, roster, labels)
    board: dict[str, Any] = {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "clusters": fleet[:CLUSTERS_MAX],
        "cluster_count": len(fleet),
        "running": [],
        "prs": [],
    }
    if kanban is not None:
        try:
            board["running"] = running(kanban, identities, labels)
            board["prs"] = prs(kanban)
        except sqlite3.Error as exc:
            raise BoardError(f"the kanban board could not be read: {exc}") from None
    home = (profiles_dir or PROFILES_DIR).parent
    filed, out = findings_nudge.first_report_markers(home)
    if filed.exists() and not out.exists() and not (home / FINDINGS_REGISTERED_MARKER).exists():
        board["first_report_pending"] = True
        stamp = findings_nudge._filed_at(filed)
        young = stamp is not None and (now - stamp[0]).total_seconds() <= FIRST_MESSAGE_MAX_AGE_SECONDS
        if young and not (home / FIRST_MESSAGE_MARKER).exists():
            board["first_message_pending"] = True
    if audit is None:
        audit = default_audit
    last = audit()
    if last is not None:
        board["audit"] = last
    return board


def default_audit() -> dict | None:
    """The last audit from the report store; None when no stream has finished or the store will not load."""
    try:
        report_status, name_of = _fleet_audit_modules()
        return last_audit(report_status.project(), name_of)
    except Exception:  # noqa: BLE001 — no audit panel rather than no board
        return None
