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
  (``skills/fleet-audit/scripts/report_status.py``).

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

import findings_queue

HERMES_HOME = os.environ.get("HERMES_HOME") or "/opt/data"
PROFILES_DIR = Path(HERMES_HOME) / "profiles"
# The fleet-audit skill's scripts, scaffolded into the platform profile; the
# report store's reader (report_status) and the stream titles (audit_report)
# live there.
FLEET_AUDIT_SCRIPTS = Path(
    os.environ.get("FLEET_AUDIT_SCRIPTS_DIR") or PROFILES_DIR / "platform" / "skills" / "fleet-audit" / "scripts"
)

CLUSTERS_MAX = 15
RUNNING_MAX = 5
PRS_MAX = 5
STATUS_RUNNING = "running"
PR_OPENED_KIND = "pr_opened"
CONFIG_NAME = "config.yaml"
IDENTITY_KEY = "cluster_identity"
CRITICAL = "critical"


class BoardError(Exception):
    """A source the snapshot needs could not be read."""


def _iso(epoch: float | None) -> str | None:
    """A board timestamp (Unix seconds) as RFC 3339, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def cluster_identities(profiles_dir: Optional[Path] = None) -> dict[str, str]:
    """Profile name -> cluster name, for every profile stamped with a ``cluster_identity``."""
    import yaml  # lazy, like cluster_agent_profile's reader

    profiles_dir = profiles_dir or PROFILES_DIR
    out: dict[str, str] = {}
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
            out[home.name] = str(identity["cluster"])
    return out


def clusters(findings: list[dict], roster: list[str]) -> list[dict]:
    """One row per cluster, worst first: the managed fleet plus any cluster a finding names.

    ``findings`` is the open queue in rank order, so the first finding seen for
    a cluster is its top one. Clusters with findings sort by their top
    finding's rank; clean ones follow by name.
    """
    rows: dict[str, dict] = {}
    order: list[str] = []
    for f in findings:
        name = f.get("cluster") or ""
        if not name:
            continue
        if name not in rows:
            rows[name] = {"name": name, "findings": 0, "severity": f.get("severity") or "", "top": f.get("title") or ""}
            order.append(name)
        rows[name]["findings"] += 1
    for name in sorted(set(roster) - set(rows)):
        rows[name] = {"name": name, "findings": 0}
        order.append(name)
    return [rows[name] for name in order[:CLUSTERS_MAX]]


def running(kanban: sqlite3.Connection, identities: dict[str, str]) -> list[dict]:
    rows = kanban.execute(
        "SELECT title, assignee, started_at, created_at FROM tasks WHERE status = ? "
        "ORDER BY COALESCE(started_at, created_at) DESC LIMIT ?",
        (STATUS_RUNNING, RUNNING_MAX),
    ).fetchall()
    out = []
    for title, assignee, started, created in rows:
        card = {"title": title or "", "started_at": _iso(started if started is not None else created)}
        if assignee in identities:
            card["cluster"] = identities[assignee]
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
        if not isinstance(data, dict) or not data.get("url") or data["url"] in seen:
            continue
        seen.add(data["url"])
        out.append(
            {
                "url": str(data["url"]),
                "number": int(data.get("number") or 0),
                "title": str(data.get("title") or ""),
                "opened_at": _iso(created),
            }
        )
        if len(out) == PRS_MAX:
            break
    return out


def last_audit(projection: dict, name_of: Callable[[str], str]) -> dict | None:
    """The newest finished run across every stream and repository in report_status's projection."""
    best: dict | None = None
    for audit_id, stream in (projection.get("streams") or {}).items():
        for repo in (stream.get("repos") or {}).values():
            latest = repo.get("latest")
            if not isinstance(latest, dict) or not latest.get("finished_at") or latest.get("findings") is None:
                continue
            if best is None or latest["finished_at"] > best["finished_at"]:
                best = {
                    "name": name_of(audit_id),
                    "findings": latest["findings"],
                    "critical": latest.get("critical") or 0,
                    "issue_url": latest.get("issue_url") or "",
                    "issue": latest.get("issue_number") or 0,
                    "finished_at": latest["finished_at"],
                }
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
    profiles_dir: Optional[Path] = None,
    audit: Callable[[], dict | None] | None = None,
) -> dict:
    """The whole board. ``kanban`` is None before the board's first card, which is an empty board."""
    try:
        findings = findings_queue.ranked_findings(session_db)
    except sqlite3.Error as exc:
        raise BoardError(f"the findings queue could not be read: {exc}") from None
    identities = cluster_identities(profiles_dir)
    board: dict[str, Any] = {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "clusters": clusters(findings, sorted(set(identities.values()))),
        "running": [],
        "prs": [],
    }
    if kanban is not None:
        try:
            board["running"] = running(kanban, identities)
            board["prs"] = prs(kanban)
        except sqlite3.Error as exc:
            raise BoardError(f"the kanban board could not be read: {exc}") from None
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
