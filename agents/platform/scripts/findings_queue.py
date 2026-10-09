#!/usr/bin/env python3
"""The findings queue: storage, ranking and lifecycle.

Implements the core of `docs/designs/inventory-findings-queue.md` — the
`findings` table (§3), the priority rubric (§4), the per-state upsert rules
(§5.2) and the order `GET /v1/findings/ranked` returns (§6.1). No HTTP and no
repository concepts (§6.2): `session_kv_server.py` wraps this in endpoints and
publishers live outside it entirely.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

__all__ = [
    "FindingError",
    "FindingNotFound",
    "init_findings_schema",
    "derive_finding_id",
    "rank_score",
    "severity_for",
    "ranked_sort_key",
    "register_findings",
    "ranked_findings",
    "list_findings",
    "mark_surfaced",
    "patch_finding",
    "expire_snoozes",
    "record_verification",
    "get_publication",
    "put_publication",
    "additions_on",
    "item_key",
    "rolled_up",
    "decided_with_item",
    "PacingLimits",
    "pacing_limits",
    "Item",
    "PacingPlan",
    "pace",
]


class FindingError(ValueError):
    """A registration or transition the queue refuses, with the reason."""


class FindingNotFound(KeyError):
    """No row with that id. A distinct type because the caller maps it to 404,
    and a bare `KeyError` from a missing dict key anywhere in the call tree
    would then be reported to the agent as a finding that does not exist."""


SOURCES = ("inventory", "event-watcher", "audit")
SEVERITIES = ("critical", "major", "minor")
REMEDIATION_KINDS = ("manifest", "gcloud", "manual")
VERIFICATION_KINDS = ("kubectl", "gcloud", "manual")
PR_STATES = ("open", "merged", "closed")

STATES = ("queued", "surfaced", "snoozed", "accepted", "dismissed", "resolved", "stale")
OPEN_STATES = ("queued", "surfaced", "accepted")

# §5.2's three upsert classes. Everything not named here is an open state and
# takes the ordinary "same problem, seen again" update.
STICKY_STATES = ("dismissed",)
RECURRENCE_STATES = ("resolved", "stale")

# `resolved` and `stale` are the daily job's to write, through
# `record_verification`; `queued` is registration's; a lapsed snooze is
# `expire_snoozes`'s. What is left is the three human transitions plus
# `surfaced`, which ends a snooze early (§3.2, §6.1).
PATCHABLE_STATES = ("accepted", "dismissed", "snoozed", "surfaced")
# The user's three decisions. One on any row of a gathered line covers the
# line (§7.2): the rows of that item still waiting to be decided take it too.
DECISION_STATES = ("accepted", "dismissed", "snoozed")
# The states of a row still waiting to be decided, which a decision on another
# row of its item reaches.
UNDECIDED_STATES = ("queued", "surfaced")

VERIFY_OUTCOMES = ("still_failing", "resolved", "unverifiable")

PUBLISHERS = ("backlog", "nudge")
PUBLICATION_TARGET_KINDS = ("github-issue", "repo-file", "chat")

# Pacing (§7.2). Only these publishers may mark a row shown, which is what
# makes it count against a day's limit and, while it waits for a decision,
# pending. A model naming a finding in answer to a pull is not one of them.
PACED_PUBLISHERS = ("nudge",)
# The two classes an item is counted under when it is added. Stored with the
# addition so a later re-score cannot move it from one day's count to the other.
ITEM_CLASSES = ("critical", "noncritical")
# The first UTC hour new criticals may be added and pending ones reminded:
# after the last daily audit, and not the middle of the night in the US.
REMIND_HOUR = 12
HOURS_PER_DAY = 24
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z", re.ASCII)

# The limits' defaults, here and nowhere else, and the environment variables
# that override them. 0 means "add none of that kind", not "no limit".
DEFAULT_FIRST_REPORT_CRITICALS = 2
DEFAULT_DAILY_CRITICALS = 2
DEFAULT_NONCRITICAL_MAX = 3
DEFAULT_NONCRITICAL_AFTER_HOUR = 16
PACING_ENV = {
    "first_report_criticals": "FINDINGS_FIRST_REPORT_CRITICALS",
    "daily_criticals": "FINDINGS_DAILY_CRITICALS",
    "noncritical_max": "FINDINGS_NONCRITICAL_MAX",
    "noncritical_after_hour": "FINDINGS_NONCRITICAL_AFTER_HOUR",
}


# --------------------------------------------------------------------------
# Identity (§3.1)
# --------------------------------------------------------------------------
#
# Transcribed from `derive_finding_id` and `_shorten_id` in
# agents/platform/skills/fleet-audit/scripts/audit_report.py rather than
# imported: that module is a 3000-line CLI that shells out to git and gh, it
# ships in the skills tree rather than on this server's PYTHONPATH, and the
# derivation is a pure function of the finding's fields. One deliberate
# extension: the queue's key carries `project` as a second segment, which the
# audit's single-project ledger does not. Cluster names are only unique within
# a project, so without it two clusters named `prod` in different projects
# derive one id and the second silently overwrites the first — while keeping
# the first's workflow state. `test_findings_queue.py` asserts segment-level
# parity against the audit's derivation so the shared logic cannot drift.

ID_EMPTY_SEGMENT = "_"
ID_SEGMENTS = 5
MAX_FINDING_ID = 100
ID_DIGEST_CHARS = 6
FINDING_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?\Z")


def _id_segment(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return out or ID_EMPTY_SEGMENT


def _shorten_id(fid: str) -> str:
    if len(fid) <= MAX_FINDING_ID:
        return fid
    digest = hashlib.sha256(fid.encode("utf-8")).hexdigest()[:ID_DIGEST_CHARS]
    budget = MAX_FINDING_ID - (len(digest) + 1)
    parts = fid.split(".")
    while len(".".join(parts)) > budget:
        longest = max(range(1, ID_SEGMENTS), key=lambda i: (len(parts[i]), i), default=None)
        if longest is None or len(parts[longest]) <= 1:
            break
        parts[longest] = parts[longest][:-1].rstrip("-") or ID_EMPTY_SEGMENT
    return f"{'.'.join(parts)[:budget].rstrip('.-')}-{digest}"


def derive_finding_id(check: str, project: str, cluster: str, namespace: str, object_name: str) -> str:
    """`(check, project, cluster, namespace, object)`, the finding's identity.

    Same string for the same problem whichever source found it, which is what
    §10's cross-source collision depends on — so every source supplies the
    same project, and a source that cannot name one has no business writing
    to the queue.
    """
    full = ".".join(
        (
            _id_segment(check),
            _id_segment(project) if project.strip() else ID_EMPTY_SEGMENT,
            _id_segment(cluster),
            _id_segment(namespace) if namespace.strip() else ID_EMPTY_SEGMENT,
            _id_segment(object_name),
        )
    )
    return _shorten_id(full)


# --------------------------------------------------------------------------
# The rubric (§4.2)
# --------------------------------------------------------------------------

B_ANCHORS = (1, 2, 3, 5, 8)
L_ANCHORS = (1, 2, 4, 6, 10)
E_ANCHORS = (1, 2, 3)

# Confidence as an integer percent. `round(B * L * (d + r) * 0.9)` is a binary
# float away from the wrong integer and rounds halves to even; the queue's
# order has to be reproducible from the vector, so the multiply is integral.
C_PERCENTS = (100, 90, 60)
_C_FROM_FLOAT = {1.0: 100, 0.9: 90, 0.6: 60}

SEVERITY_CRITICAL_AT = 150
SEVERITY_MAJOR_AT = 40

# §4.2's floor: failing now, on something the user depends on.
FLOOR_LIKELIHOOD = 10
FLOOR_BLAST_RADIUS = 3

PROVIDER_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")
PROVIDER_NAMESPACE_RE = re.compile(r"^(?:gke|gmp)-")

MAX_LABEL_CHARS = 500
MAX_TEXT_CHARS = 4000
MAX_BATCH = 500

# Rejected values are quoted back so the caller can see what it sent, but the
# rejection happens before any length cap, and the message ends up verbatim in
# an agent's context window. 80 characters is enough to recognise a value and
# not enough to carry a payload.
MAX_ECHO_CHARS = 80


def _brief(value: Any) -> str:
    shown = repr(value)
    return shown if len(shown) <= MAX_ECHO_CHARS else f"{shown[:MAX_ECHO_CHARS]}... ({len(shown)} chars)"


def _parse_timestamp(raw: Any, field: str) -> str:
    """An ISO 8601 instant, normalised to what SQLite's `datetime()` compares against.

    The expiry sweep asks `snoozed_until <= datetime('now')`, which is a string
    comparison against UTC `YYYY-MM-DD HH:MM:SS`. An ISO string with a `T` or an
    offset sorts wrong against that, so a snooze stored as the caller typed it
    would expire at the wrong time or never.
    """
    value = _text(raw, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FindingError(f"{field} is {_brief(value)}; must be an ISO 8601 date or timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _rubric_percent(value: Any) -> int:
    if isinstance(value, bool):
        raise FindingError("rubric.C must be one of 1.0, 0.9, 0.6")
    if isinstance(value, int) and value in C_PERCENTS:
        return value
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        raise FindingError("rubric.C must be one of 1.0, 0.9, 0.6") from None
    if as_float not in _C_FROM_FLOAT:
        raise FindingError(f"rubric.C is {_brief(value)}; anchors are 1.0 (measured), 0.9 (live state), 0.6 (inferred)")
    return _C_FROM_FLOAT[as_float]


def validate_rubric(raw: Any) -> dict:
    """Normalise `{B, L, detect, recover, C}` against §4.2's anchors.

    Strict on purpose: anchored ordinals are what make the same finding score
    the same way twice (§4.1), and a value off the scale is a classification
    that did not happen.
    """
    if not isinstance(raw, dict):
        raise FindingError("rubric must be an object with B, L, detect, recover and C")
    out = {}
    for key, anchors in (("B", B_ANCHORS), ("L", L_ANCHORS), ("detect", E_ANCHORS), ("recover", E_ANCHORS)):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value not in anchors:
            raise FindingError(f"rubric.{key} is {_brief(value)}; anchors are {list(anchors)}")
        out[key] = value
    out["C"] = _rubric_percent(raw.get("C"))
    return out


def rank_score(rubric: dict) -> int:
    """`round(B × L × (detect + recover) × C)`, 1 to 480."""
    base = rubric["B"] * rubric["L"] * (rubric["detect"] + rubric["recover"])
    return (base * rubric["C"] + 50) // 100


def severity_for(score: int, rubric: dict) -> str:
    if rubric["L"] >= FLOOR_LIKELIHOOD and rubric["B"] >= FLOOR_BLAST_RADIUS:
        return "critical"
    if score >= SEVERITY_CRITICAL_AT:
        return "critical"
    if score >= SEVERITY_MAJOR_AT:
        return "major"
    return "minor"


def is_provider_managed(namespace: str) -> bool:
    ns = (namespace or "").strip()
    return ns in PROVIDER_NAMESPACES or bool(PROVIDER_NAMESPACE_RE.match(ns))


def ranked_sort_key(finding: dict) -> tuple:
    """§6.1's order: the gate, then the rubric, then a deterministic tie-break.

    `_finding_sort_key`'s tuple from `audit_report.py` is the tail, so two
    findings the rubric cannot separate come out in the same order the fleet
    audit renders them.
    """
    remediation = finding.get("remediation") or {}
    return (
        0 if finding.get("actionable") else 1,
        -int(finding.get("rank_score") or 0),
        # §4.5: fix cost enters the order exactly once, here. Two findings the
        # rubric scores the same are not equally useful to surface — the one
        # with a manifest to change can be handed over today as a diff.
        0 if remediation.get("kind") == "manifest" else 1,
        str(finding.get("project") or ""),
        str(finding.get("cluster") or ""),
        str(finding.get("namespace") or ""),
        str(finding.get("object") or ""),
        str(finding.get("title") or ""),
        str(finding.get("id") or ""),
    )


# --------------------------------------------------------------------------
# Schema (§3.1)
# --------------------------------------------------------------------------

FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    check_slug        TEXT NOT NULL,
    project           TEXT NOT NULL,
    cluster           TEXT NOT NULL,
    namespace         TEXT NOT NULL DEFAULT '',
    object            TEXT NOT NULL,
    title             TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    root_cause        TEXT,
    severity          TEXT NOT NULL,
    rank_score        INTEGER NOT NULL,
    rubric            TEXT NOT NULL,
    provider_managed  INTEGER NOT NULL DEFAULT 0,
    actionable        INTEGER NOT NULL DEFAULT 1,
    recommendation    TEXT NOT NULL,
    remediation       TEXT NOT NULL,
    verification      TEXT NOT NULL,
    pr_url            TEXT,
    pr_state          TEXT,
    state             TEXT NOT NULL DEFAULT 'queued',
    first_seen        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_verified     TIMESTAMP,
    last_verification TEXT,
    surfaced_at       TIMESTAMP,
    surface_count     INTEGER NOT NULL DEFAULT 0,
    first_shown_at    TIMESTAMP,
    added_class       TEXT,
    absent_since      TIMESTAMP,
    snoozed_until     TIMESTAMP,
    alarmed_at        TIMESTAMP,
    chat_id           TEXT,
    thread_id         TEXT,
    likelihood        INTEGER GENERATED ALWAYS AS (json_extract(rubric, '$.L')) VIRTUAL,
    blast_radius      INTEGER GENERATED ALWAYS AS (json_extract(rubric, '$.B')) VIRTUAL
)
"""

PUBLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_publications (
    publisher      TEXT PRIMARY KEY,
    target_kind    TEXT NOT NULL,
    target_ref     TEXT,
    content_hash   TEXT,
    last_published TIMESTAMP
)
"""

# One row per addition (§7.2), and nothing else writes or clears it: the day's
# limits count from here, so neither a decision nor a recurrence (which clears
# the row's `first_shown_at`, §5.2) gives budget back. `run` tells the members
# of one item, marked one by one in the same run, from a second addition of the
# same line later that day.
ADDITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings_additions (
    day         TEXT NOT NULL DEFAULT (date('now')),
    item_key    TEXT NOT NULL,
    run         TEXT NOT NULL DEFAULT '',
    added_class TEXT NOT NULL,
    added_at    TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    UNIQUE (day, item_key, run)
)
"""

FINDINGS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS findings_ranked ON findings(state, rank_score DESC)",
    "CREATE INDEX IF NOT EXISTS findings_urgent ON findings(likelihood, blast_radius, alarmed_at)",
    "CREATE INDEX IF NOT EXISTS findings_object ON findings(project, cluster, namespace, object)",
    "CREATE INDEX IF NOT EXISTS findings_pr     ON findings(pr_state) WHERE pr_state IS NOT NULL",
)

_COLUMNS = (
    "id", "source", "check_slug", "project", "cluster", "namespace", "object", "title", "detail",
    "root_cause", "severity", "rank_score", "rubric", "provider_managed", "actionable",
    "recommendation", "remediation", "verification", "pr_url", "pr_state", "state",
    "first_seen", "last_verified", "last_verification", "surfaced_at", "surface_count",
    "first_shown_at", "added_class", "absent_since", "snoozed_until", "alarmed_at", "chat_id", "thread_id",
)

# Added after the table shipped, so a released database gains them by ALTER.
# `absent_since` gets no backfill: a row an older build downgraded for absence
# carries C = 0.6 like one registered at that confidence, and nothing tells the
# two apart, so both start out as still reported: the direction that keeps
# reminding. That keeps a critical registered as inferred pending and
# reminded; the cost is that a shown row an older build downgraded stays
# pending until a complete sweep misses it again.
PACING_COLUMNS = (("first_shown_at", "TIMESTAMP"), ("added_class", "TEXT"), ("absent_since", "TIMESTAMP"))

# How many criticals the released nudge named each morning: its TOP_N. This
# mirrors that released value and is not FINDINGS_DAILY_CRITICALS.
OLD_NUDGE_TOP_N = 2

# The criticals the old nudge named were shown, and it named them daily, so
# they keep being reminded rather than coming back as new and taking a day's
# budget. `surfaced_at` is when they were last named, which is close enough
# for that. `added_class` stays NULL, so none of them counts as an addition.
# Nothing records who marked a row: the MCP tool marks after a pull with the
# same update. So only the rows the old nudge would have named are backfilled:
# the top OLD_NUDGE_TOP_N nameable criticals that were marked, in its ranked
# order. Any other marked row, a non-critical or a critical a pull marked, is
# left unshown and comes back once as new; calling it shown would make it
# pending without it ever being added.
PACING_BACKFILL = "UPDATE findings SET first_shown_at = surfaced_at WHERE id = ?"


def _old_nudge_named(conn: sqlite3.Connection) -> list[str]:
    marked = [
        f["id"]
        for f in ranked_findings(conn)
        if f["severity"] == "critical" and not rolled_up(f) and f["surface_count"] > 0 and f["surfaced_at"]
    ]
    return marked[:OLD_NUDGE_TOP_N]


_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM findings"


def init_findings_schema(conn: sqlite3.Connection) -> None:
    # `project` joined the primary key before any release shipped this table,
    # so the only databases without the column are pre-release dev installs.
    # CREATE IF NOT EXISTS would keep the old shape there, and every INSERT
    # would then raise `no such column: project` — which the HTTP layer maps
    # to a retryable-looking 503, on every registration, forever. Dropping is
    # safe precisely because no released writer ever filled the table, and
    # self-healing beats the manual `DROP TABLE` note `intercepted_events`
    # ships with because this failure never surfaces as its own error.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(findings)")}
    if columns and "project" not in columns:
        conn.execute("DROP TABLE findings")
        columns = set()
    conn.execute(FINDINGS_SCHEMA)
    # A released table predates the pacing columns. Without them every
    # SELECT fails with `no such column`, the same 503 as above.
    if columns:
        for name, kind in PACING_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE findings ADD COLUMN {name} {kind}")
        if "first_shown_at" not in columns:
            conn.executemany(PACING_BACKFILL, [(fid,) for fid in _old_nudge_named(conn)])
    conn.execute(PUBLICATIONS_SCHEMA)
    conn.execute(ADDITIONS_SCHEMA)
    for statement in FINDINGS_INDEXES:
        conn.execute(statement)


# --------------------------------------------------------------------------
# Validation (§3.1, §5.1)
# --------------------------------------------------------------------------


def _text(raw: Any, field: str, *, required: bool = True, limit: int = MAX_LABEL_CHARS) -> str:
    value = "" if raw is None else str(raw).strip()
    if not value:
        if required:
            raise FindingError(f"{field} is required")
        return ""
    return value[:limit]


def _validate_recommendation(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise FindingError("recommendation must be an object with action, rationale and risk")
    return {key: _text(raw.get(key), f"recommendation.{key}", limit=MAX_TEXT_CHARS) for key in ("action", "rationale", "risk")}


def _validate_remediation(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise FindingError("remediation must be an object with kind, path and note")
    kind = _text(raw.get("kind"), "remediation.kind")
    if kind not in REMEDIATION_KINDS:
        raise FindingError(f"remediation.kind is {_brief(kind)}; must be one of {list(REMEDIATION_KINDS)}")
    path = _text(raw.get("path"), "remediation.path", required=False)
    if path and kind != "manifest":
        raise FindingError(f"remediation.path is only meaningful when kind is 'manifest', not {_brief(kind)}")
    out = {"kind": kind, "note": _text(raw.get("note"), "remediation.note", limit=MAX_TEXT_CHARS)}
    if path:
        out["path"] = path
    return out


def _validate_verification(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise FindingError("verification must be an object with kind, command and still_failing_when")
    kind = _text(raw.get("kind"), "verification.kind")
    if kind not in VERIFICATION_KINDS:
        raise FindingError(f"verification.kind is {_brief(kind)}; must be one of {list(VERIFICATION_KINDS)}")
    # A `manual` finding is one no command can settle (§7.4), so it is the one
    # kind that may arrive without one.
    command = _text(raw.get("command"), "verification.command", required=kind != "manual", limit=MAX_TEXT_CHARS)
    return {
        "kind": kind,
        "command": command,
        "still_failing_when": _text(
            raw.get("still_failing_when"), "verification.still_failing_when", limit=MAX_TEXT_CHARS
        ),
    }


def validate_finding(raw: Any) -> dict:
    """A registration payload, normalised into the row it will become."""
    if not isinstance(raw, dict):
        raise FindingError("each finding must be an object")
    for computed in ("severity", "rank_score"):
        if computed in raw:
            raise FindingError(
                f"{computed} is derived from the rubric and may not be supplied; "
                "the queue orders on one scale (§4.1)"
            )

    source = _text(raw.get("source"), "source")
    if source not in SOURCES:
        raise FindingError(f"source is {_brief(source)}; must be one of {list(SOURCES)}")

    check = _text(raw.get("check") if raw.get("check") is not None else raw.get("check_slug"), "check")
    # Required, not defaulted: cluster names are only unique within a project,
    # so a finding that omits its project is one key collision away from
    # silently overwriting another cluster's row (and keeping its state).
    project = _text(raw.get("project"), "project")
    cluster = _text(raw.get("cluster"), "cluster")
    namespace = _text(raw.get("namespace"), "namespace", required=False)
    object_name = _text(raw.get("object"), "object")

    rubric = validate_rubric(raw.get("rubric"))
    score = rank_score(rubric)

    finding_id = derive_finding_id(check, project, cluster, namespace, object_name)
    if not FINDING_ID_RE.match(finding_id):
        # Reachable only when a field is entirely outside `[a-z0-9]`, which
        # `_id_segment` collapses to the empty-segment sentinel.
        raise FindingError(
            f"check/project/cluster/namespace/object derive the unusable id {finding_id!r}; "
            "each must carry at least one alphanumeric character"
        )

    return {
        "id": finding_id,
        "source": source,
        "check_slug": check,
        "project": project,
        "cluster": cluster,
        "namespace": namespace,
        "object": object_name,
        "title": _text(raw.get("title"), "title"),
        "detail": _text(raw.get("detail"), "detail", required=False, limit=MAX_TEXT_CHARS),
        "root_cause": _text(raw.get("root_cause"), "root_cause", required=False, limit=MAX_TEXT_CHARS) or None,
        "severity": severity_for(score, rubric),
        "rank_score": score,
        "rubric": rubric,
        # OR-ed rather than taken from the payload alone: §4.4's namespace rule
        # is a property of the fleet, and a source that forgets it would put a
        # pull request on a manifest the operator does not own.
        "provider_managed": bool(raw.get("provider_managed")) or is_provider_managed(namespace),
        "actionable": bool(raw.get("actionable", True)),
        "recommendation": _validate_recommendation(raw.get("recommendation")),
        "remediation": _validate_remediation(raw.get("remediation")),
        "verification": _validate_verification(raw.get("verification")),
    }


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

_JSON_COLUMNS = ("rubric", "recommendation", "remediation", "verification", "last_verification")


def _row_to_finding(row: Iterable) -> dict:
    finding = dict(zip(_COLUMNS, row))
    for column in _JSON_COLUMNS:
        raw = finding.get(column)
        finding[column] = json.loads(raw) if raw else None
    rubric = finding.get("rubric") or {}
    if "C" in rubric:
        rubric["C"] = rubric["C"] / 100
    finding["check"] = finding["check_slug"]
    finding["provider_managed"] = bool(finding["provider_managed"])
    finding["actionable"] = bool(finding["actionable"])
    return finding


def ranked_findings(conn: sqlite3.Connection) -> list[dict]:
    """The open queue, whole, in §6.1's order.

    Sorted here rather than in SQL because the order is the product decision
    this design rests on and it earns a unit test (§6.1).
    """
    placeholders = ", ".join("?" * len(OPEN_STATES))
    rows = conn.execute(f"{_SELECT} WHERE state IN ({placeholders})", OPEN_STATES).fetchall()
    return sorted((_row_to_finding(row) for row in rows), key=ranked_sort_key)


def list_findings(
    conn: sqlite3.Connection,
    cluster: str = "",
    state: str = "",
    severity: str = "",
    limit: int = 200,
    project: str = "",
) -> list[dict]:
    clauses, params = [], []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)
    if state:
        if state not in STATES:
            raise FindingError(f"state is {_brief(state)}; must be one of {list(STATES)}")
        clauses.append("state = ?")
        params.append(state)
    if severity:
        if severity not in SEVERITIES:
            raise FindingError(f"severity is {_brief(severity)}; must be one of {list(SEVERITIES)}")
        clauses.append("severity = ?")
        params.append(severity)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"{_SELECT}{where}", params).fetchall()
    # Sorted before the limit, not after: slicing an unordered result makes
    # which rows come back a property of insertion order.
    ordered = sorted((_row_to_finding(row) for row in rows), key=ranked_sort_key)
    return ordered[: max(1, min(int(limit), 1000))]


def get_finding(conn: sqlite3.Connection, finding_id: str) -> dict | None:
    row = conn.execute(f"{_SELECT} WHERE id = ?", (finding_id,)).fetchone()
    return _row_to_finding(row) if row else None


# --------------------------------------------------------------------------
# Registration (§5.2)
# --------------------------------------------------------------------------

_DESCRIPTIVE = (
    "source", "check_slug", "project", "cluster", "namespace", "object", "title", "detail",
    "root_cause", "severity", "rank_score", "rubric", "provider_managed",
    "actionable", "recommendation", "remediation", "verification",
)


def _blobs(finding: dict) -> dict:
    row = dict(finding)
    for column in ("rubric", "recommendation", "remediation", "verification"):
        row[column] = json.dumps(row[column], sort_keys=True)
    row["provider_managed"] = int(row["provider_managed"])
    row["actionable"] = int(row["actionable"])
    return row


def _register_one(conn: sqlite3.Connection, finding: dict) -> str:
    row = _blobs(finding)
    existing = conn.execute("SELECT state FROM findings WHERE id = ?", (row["id"],)).fetchone()

    if existing is None:
        columns = ("id", *_DESCRIPTIVE)
        conn.execute(
            f"INSERT INTO findings ({', '.join(columns)}, last_verified) "
            f"VALUES ({', '.join('?' * len(columns))}, datetime('now'))",
            tuple(row[column] for column in columns),
        )
        return "created"

    state = existing[0]
    assignments = ", ".join(f"{column} = ?" for column in _DESCRIPTIVE)
    values = tuple(row[column] for column in _DESCRIPTIVE)

    # Every path below clears `absent_since`: the row was reported again.
    if state in STICKY_STATES:
        # The user rejected this. Record that it was seen again so the row's
        # freshness is honest, and change nothing else (§5.2).
        conn.execute(
            "UPDATE findings SET last_verified = datetime('now'), absent_since = NULL WHERE id = ?", (row["id"],)
        )
        return "suppressed"

    if state in RECURRENCE_STATES:
        conn.execute(
            f"UPDATE findings SET {assignments}, state = 'queued', last_verified = datetime('now'), "
            "surface_count = 0, alarmed_at = NULL, first_shown_at = NULL, added_class = NULL, "
            "absent_since = NULL WHERE id = ?",
            (*values, row["id"]),
        )
        return "updated"

    conn.execute(
        f"UPDATE findings SET {assignments}, last_verified = datetime('now'), absent_since = NULL WHERE id = ?",
        (*values, row["id"]),
    )
    return "updated"


def _downgrade_absent(conn: sqlite3.Connection, project: str, cluster: str, seen: set[str]) -> int:
    """§5.2's reciprocal case: absence lowers confidence, it does not resolve.

    A sweep that died halfway produces the same silence as a fleet that got
    healthier, so a row the run did not re-report is re-ranked down at the
    rubric's own value for "inferred from absence" and left on the list for
    §7.4 to settle. It also gets `absent_since`, which is what pacing reads
    (§7.2): C alone cannot say why a row is at 0.6, since a source may
    register a finding at that confidence. A row already at 0.6 gets the
    marker without a re-rank. Returns how many rows were re-ranked, so a row
    that only gained the marker is not counted.
    """
    # Matched case-insensitively because the caller's `scope.cluster` and the
    # row's `cluster` reach here by different routes, and a byte-for-byte miss
    # is silent: the sweep reports zero downgrades, which reads as a clean run.
    # `surfaced` as well as `queued`: surfacing records that the nudge named the
    # row, which says nothing about whether the problem is still there. Exempting
    # it would make the rows the nudge names the only ones absence never reaches.
    rows = conn.execute(
        "SELECT id, rubric FROM findings WHERE project = ? COLLATE NOCASE "
        "AND cluster = ? COLLATE NOCASE AND state IN ('queued', 'surfaced')",
        (project, cluster),
    ).fetchall()
    downgraded = 0
    for finding_id, raw in rows:
        if finding_id in seen:
            continue
        try:
            rubric = validate_rubric(json.loads(raw))
        except (FindingError, ValueError):
            # One unreadable row must not fail the batch of new findings it
            # arrived with; leaving it at its old score is the safe direction.
            continue
        # The first miss is the one recorded; later misses keep it.
        conn.execute(
            "UPDATE findings SET absent_since = COALESCE(absent_since, datetime('now')) WHERE id = ?", (finding_id,)
        )
        if rubric["C"] == 60:
            continue
        rubric["C"] = 60
        score = rank_score(rubric)
        conn.execute(
            "UPDATE findings SET rubric = ?, rank_score = ?, severity = ? WHERE id = ?",
            (json.dumps(rubric, sort_keys=True), score, severity_for(score, rubric), finding_id),
        )
        downgraded += 1
    return downgraded


def register_findings(conn: sqlite3.Connection, findings: Any, scope: Any = None) -> dict:
    """Upsert a batch, then apply the absence rule if the run says it was complete."""
    if not isinstance(findings, list):
        raise FindingError("findings must be a list")
    if not findings:
        raise FindingError("findings is empty")
    if len(findings) > MAX_BATCH:
        raise FindingError(f"batch is {len(findings)} findings, over the {MAX_BATCH} limit")

    results, seen = [], set()
    for index, raw in enumerate(findings):
        try:
            finding = validate_finding(raw)
        except FindingError as exc:
            raise FindingError(f"findings[{index}]: {exc}") from None
        results.append({"id": finding["id"], "outcome": _register_one(conn, finding)})
        seen.add(finding["id"])

    downgraded = 0
    if isinstance(scope, dict) and scope.get("complete"):
        project = _text(scope.get("project"), "scope.project")
        cluster = _text(scope.get("cluster"), "scope.cluster")
        downgraded = _downgrade_absent(conn, project, cluster, seen)

    return {"results": results, "downgraded": downgraded}


# --------------------------------------------------------------------------
# Transitions (§3.2)
# --------------------------------------------------------------------------


def mark_surfaced(
    conn: sqlite3.Connection,
    finding_id: str,
    chat_id: str = "",
    thread_id: str = "",
    publisher: str = "",
    added_class: str = "",
    run: str = "",
) -> dict:
    """Record that this row was named in a message, after the send.

    Only a paced publisher passes `publisher`, and only that sets
    `first_shown_at`, on the row's first paced showing: a row named in answer
    to a pull is not shown for pacing, so it counts against no limit and never
    becomes pending. `added_class` is the class of the item the row was added
    under, and is recorded only with the first showing, together with one row
    in `findings_additions` per item and `run`; a row that joins an item
    already shown is shown without one and is not an addition. `run` names the
    publisher's run, so the members of one item marked in it are one addition.
    """
    if publisher and publisher not in PACED_PUBLISHERS:
        raise FindingError(f"publisher is {_brief(publisher)}; must be one of {list(PACED_PUBLISHERS)}")
    if added_class:
        if not publisher:
            raise FindingError("added_class is recorded only by a paced publisher")
        if added_class not in ITEM_CLASSES:
            raise FindingError(f"added_class is {_brief(added_class)}; must be one of {list(ITEM_CLASSES)}")
    current = get_finding(conn, finding_id)
    if current is None:
        raise FindingNotFound(finding_id)
    if added_class and current["first_shown_at"] is None:
        conn.execute(
            "INSERT OR IGNORE INTO findings_additions (item_key, run, added_class) VALUES (?, ?, ?)",
            (json.dumps(item_key(current)), str(run or ""), added_class),
        )
    shown = 1 if publisher else 0
    # Every right-hand side reads the row as it was before this statement, so
    # `added_class` sees the old `first_shown_at` whatever order they are in.
    conn.execute(
        "UPDATE findings SET surface_count = surface_count + 1, surfaced_at = datetime('now'), "
        "state = CASE WHEN state = 'queued' THEN 'surfaced' ELSE state END, "
        "added_class = CASE WHEN ? AND first_shown_at IS NULL THEN NULLIF(?, '') ELSE added_class END, "
        "first_shown_at = CASE WHEN ? THEN COALESCE(first_shown_at, datetime('now')) ELSE first_shown_at END, "
        "chat_id = COALESCE(NULLIF(?, ''), chat_id), thread_id = COALESCE(NULLIF(?, ''), thread_id) "
        "WHERE id = ?",
        (shown, added_class or "", shown, chat_id or "", thread_id or "", finding_id),
    )
    return get_finding(conn, finding_id)


def patch_finding(conn: sqlite3.Connection, finding_id: str, patch: Any) -> dict:
    """The three human transitions, the early end of a snooze, and PR reconciliation.

    A decision (`DECISION_STATES`) on a row that is an item at all (not
    `rolled_up`) covers its whole item: every other row with the same
    `item_key` that `decided_with_item` accepts takes the same state and
    `snoozed_until`. Their ids come
    back as `item_rows_decided`. Without it, the nudge names one id for a
    gathered line, the user decides it, and the line stays pending.
    """
    if not isinstance(patch, dict):
        raise FindingError("patch must be an object")
    current = get_finding(conn, finding_id)
    if current is None:
        raise FindingNotFound(finding_id)

    assignments, values = [], []

    decision = None
    if "state" in patch:
        state = _text(patch.get("state"), "state")
        if state not in PATCHABLE_STATES:
            raise FindingError(
                f"state is {_brief(state)}; this route sets {list(PATCHABLE_STATES)}. "
                "'resolved' and 'stale' are verification outcomes and 'queued' is registration's"
            )
        assignments.append("state = ?")
        values.append(state)
        if state == "snoozed":
            assignments.append("snoozed_until = ?")
            values.append(_parse_timestamp(patch.get("snoozed_until"), "snoozed_until"))
        else:
            # Leaving the snooze by any door clears its deadline. Only the exit
            # to 'surfaced' used to, which left an accepted row carrying a
            # wake-up time the expiry sweep would act on.
            assignments.append("snoozed_until = NULL")
        if state in DECISION_STATES and not rolled_up(current):
            decision = (list(assignments), list(values))
    elif "snoozed_until" in patch:
        raise FindingError("snoozed_until is set by the transition to 'snoozed'")

    if ("pr_url" in patch or "pr_state" in patch) and current["provider_managed"]:
        raise FindingError(
            f"{finding_id} is provider-managed: there is no file to change, so it never "
            "gets a pull request (§4.4)"
        )

    if "pr_url" in patch:
        assignments.append("pr_url = ?")
        values.append(_text(patch.get("pr_url"), "pr_url", required=False) or None)
    if "pr_state" in patch:
        pr_state = _text(patch.get("pr_state"), "pr_state", required=False)
        if pr_state and pr_state not in PR_STATES:
            raise FindingError(f"pr_state is {_brief(pr_state)}; must be one of {list(PR_STATES)}")
        assignments.append("pr_state = ?")
        values.append(pr_state or None)

    if not assignments:
        raise FindingError("patch names no field this route can set")

    conn.execute(f"UPDATE findings SET {', '.join(assignments)} WHERE id = ?", (*values, finding_id))
    result = get_finding(conn, finding_id)
    if decision is not None:
        decided_assignments, decided_values = decision
        key = item_key(current)
        placeholders = ", ".join("?" * len(UNDECIDED_STATES))
        siblings = [
            sibling["id"]
            for sibling in map(
                _row_to_finding,
                conn.execute(f"{_SELECT} WHERE state IN ({placeholders}) AND id != ?", (*UNDECIDED_STATES, finding_id)),
            )
            if item_key(sibling) == key and decided_with_item(sibling)
        ]
        for sibling in siblings:
            conn.execute(
                f"UPDATE findings SET {', '.join(decided_assignments)} WHERE id = ?", (*decided_values, sibling)
            )
        result["item_rows_decided"] = siblings
    return result


def expire_snoozes(conn: sqlite3.Connection) -> int:
    """§3.2's snooze exit: a lapsed `snoozed_until` returns the row to `surfaced`.

    The nudge calls this before composing its message, so "snoozed
    until <date>" is a promise something keeps: without a caller, a lapsed
    snooze stays hidden until someone thinks to query `state=snoozed`, and
    the repeat-daily guarantee for a critical is silently off.
    """
    return conn.execute(
        "UPDATE findings SET state = 'surfaced', snoozed_until = NULL "
        "WHERE state = 'snoozed' AND snoozed_until <= datetime('now')"
    ).rowcount


def record_verification(
    conn: sqlite3.Connection,
    finding_id: str,
    outcome: str,
    observed: str = "",
    rubric: Any = None,
    object_missing: bool = False,
) -> dict:
    """§7.4's three outcomes. "Could not verify" is not "no longer reproduces"."""
    current = get_finding(conn, finding_id)
    if current is None:
        raise FindingNotFound(finding_id)
    outcome = _text(outcome, "outcome")
    if outcome not in VERIFY_OUTCOMES:
        raise FindingError(f"outcome is {_brief(outcome)}; must be one of {list(VERIFY_OUTCOMES)}")

    assignments = ["last_verification = ?"]
    values: list[Any] = [
        json.dumps(
            {"outcome": outcome, "observed": _text(observed, "observed", required=False, limit=MAX_TEXT_CHARS)},
            sort_keys=True,
        )
    ]

    # A dismissed row records that it was checked and moves nowhere. `resolved`
    # and `stale` are both in RECURRENCE_STATES, so letting verification write
    # either would arm the next sweep to re-queue the one row the user took off
    # the list — the same revival §5.2 blocks on the registration path.
    sticky = current["state"] in STICKY_STATES

    if outcome == "unverifiable":
        # `last_verified` deliberately does not advance: the queue did not
        # manage to ask, and a row that looks freshly checked is the lie this
        # third outcome exists to prevent.
        if object_missing and not sticky:
            assignments.append("state = 'stale'")
    else:
        assignments.append("last_verified = datetime('now')")
        if outcome == "resolved" and not sticky:
            assignments.append("state = 'resolved'")
        if outcome == "still_failing":
            # Seen again, as a sweep reporting it would be (§5.2).
            assignments.append("absent_since = NULL")

    if rubric is not None:
        new_rubric = validate_rubric(rubric)
        score = rank_score(new_rubric)
        assignments += ["rubric = ?", "rank_score = ?", "severity = ?"]
        values += [json.dumps(new_rubric, sort_keys=True), score, severity_for(score, new_rubric)]
        # §4.6's fourth re-rank event, the only one that lowers a score: the
        # fault stopped firing, so the alarm may fire again if it comes back.
        if (current["rubric"] or {}).get("L") == FLOOR_LIKELIHOOD and new_rubric["L"] < FLOOR_LIKELIHOOD:
            assignments.append("alarmed_at = NULL")

    conn.execute(f"UPDATE findings SET {', '.join(assignments)} WHERE id = ?", (*values, finding_id))
    return get_finding(conn, finding_id)


# --------------------------------------------------------------------------
# Publisher state (§3.1)
# --------------------------------------------------------------------------


def get_publication(conn: sqlite3.Connection, publisher: str) -> dict | None:
    row = conn.execute(
        "SELECT publisher, target_kind, target_ref, content_hash, last_published "
        "FROM queue_publications WHERE publisher = ?",
        (publisher,),
    ).fetchone()
    if not row:
        return None
    return dict(zip(("publisher", "target_kind", "target_ref", "content_hash", "last_published"), row))


def put_publication(conn: sqlite3.Connection, publisher: str, body: Any) -> dict:
    if publisher not in PUBLISHERS:
        raise FindingError(f"publisher is {_brief(publisher)}; must be one of {list(PUBLISHERS)}")
    if not isinstance(body, dict):
        raise FindingError("body must be an object with target_kind, target_ref and content_hash")
    target_kind = _text(body.get("target_kind"), "target_kind")
    if target_kind not in PUBLICATION_TARGET_KINDS:
        raise FindingError(f"target_kind is {_brief(target_kind)}; must be one of {list(PUBLICATION_TARGET_KINDS)}")
    # An omitted key leaves its column alone rather than nulling it. The two
    # publishers write for different reasons -- the backlog to remember the
    # document it rewrites, the nudge to remember the hash it posted -- and a
    # full-row replace lets either erase the other's memory.
    existing = get_publication(conn, publisher) or {}
    conn.execute(
        "INSERT INTO queue_publications (publisher, target_kind, target_ref, content_hash, last_published) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(publisher) DO UPDATE SET target_kind = excluded.target_kind, "
        "target_ref = excluded.target_ref, content_hash = excluded.content_hash, "
        "last_published = excluded.last_published",
        (
            publisher,
            target_kind,
            _text(body.get("target_ref"), "target_ref", required=False, limit=MAX_TEXT_CHARS) or None
            if "target_ref" in body
            else existing.get("target_ref"),
            _text(body.get("content_hash"), "content_hash", required=False) or None
            if "content_hash" in body
            else existing.get("content_hash"),
        ),
    )
    return get_publication(conn, publisher)


# --------------------------------------------------------------------------
# Pacing (§7.2)
# --------------------------------------------------------------------------


def item_key(finding: Mapping) -> tuple[str, str, str]:
    """The gathered line a row belongs to: one condition, on one cluster.

    Rows sharing it are one item: named together, marked together, and pending
    while any of them is. Normalised as the id's segments are, so two spellings
    of one check do not split a line. Counting rows instead of lines is a
    change to this function alone.
    """
    check = finding.get("check_slug") or finding.get("check") or ""
    return (
        _id_segment(str(check)),
        _id_segment(str(finding.get("project") or "")),
        _id_segment(str(finding.get("cluster") or "")),
    )


def rolled_up(finding: Mapping) -> bool:
    """§4.4: a provider-managed observation is never an item; a provider-managed fault is.

    The fault exception turns on `actionable`, which the design defines as
    whether a next step exists rather than who takes it -- a support case counts.
    """
    return bool(finding.get("provider_managed")) and not finding.get("actionable", True)


def decided_with_item(finding: Mapping) -> bool:
    """Whether a decision on another row of this row's item applies to it too (§7.2)."""
    return finding.get("state") in UNDECIDED_STATES and not rolled_up(finding)


def additions_on(conn: sqlite3.Connection, day: str) -> dict:
    """How many items of each class were added on a UTC day, whatever happened to them since.

    Read from `findings_additions`, which only `mark_surfaced` writes and
    nothing clears: a dismissed, snoozed, resolved or recurred addition still
    spent that day's budget, and a line added twice in a day counts twice.
    """
    if not isinstance(day, str) or not DAY_RE.match(day):
        raise FindingError(f"day is {_brief(day)}; must be a UTC date, YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise FindingError(f"day is {_brief(day)}; must be a UTC date, YYYY-MM-DD") from None
    counts = {added_class: 0 for added_class in ITEM_CLASSES}
    for added_class, count in conn.execute(
        "SELECT added_class, COUNT(*) FROM findings_additions WHERE day = ? GROUP BY added_class", (day,)
    ):
        counts[added_class] = count
    return {"day": day, **counts}


@dataclass(frozen=True)
class PacingLimits:
    """How many items may be added, and when. 0 adds none of that kind."""

    first_report_criticals: int = DEFAULT_FIRST_REPORT_CRITICALS
    daily_criticals: int = DEFAULT_DAILY_CRITICALS
    noncritical_max: int = DEFAULT_NONCRITICAL_MAX
    # A UTC hour, 0-23.
    noncritical_after_hour: int = DEFAULT_NONCRITICAL_AFTER_HOUR


def pacing_limits(environ: Mapping[str, str]) -> PacingLimits:
    """The limits, from `PACING_ENV` over the defaults.

    Unset or empty is the default. A value that is not a whole number, is
    negative, or (for the hour) is not 0-23 is reported on stderr and replaced
    by the default, so one bad value does not stop the queue being paced.
    """
    defaults = PacingLimits()
    values = {}
    for name, variable in PACING_ENV.items():
        raw = (environ.get(variable) or "").strip()
        if not raw:
            continue
        default = getattr(defaults, name)
        try:
            value = int(raw)
        except ValueError:
            value = None
        too_big = name == "noncritical_after_hour" and value is not None and value >= HOURS_PER_DAY
        if value is None or value < 0 or too_big:
            expected = "an hour from 0 to 23" if name == "noncritical_after_hour" else "a whole number, 0 or more"
            sys.stderr.write(f"findings_queue: {variable}={raw!r} is not {expected}; using the default {default}\n")
            continue
        values[name] = value
    return PacingLimits(**values)


@dataclass(eq=False)
class Item:
    """One gathered line: the rows that are pending or new, in ranked order."""

    key: tuple
    members: list = field(default_factory=list)
    pending: bool = False
    # The highest severity among the members.
    severity: str = SEVERITIES[-1]
    # The earliest showing among the pending members, None for a new item.
    first_shown: datetime | None = None
    # How many rows a decision on any member's id applies to (`patch_finding`):
    # the members, and any row of the line that is neither pending nor new
    # but still undecided, such as a shown row the sweep stopped reporting.
    covers: int = 0

    @property
    def item_class(self) -> str:
        return ITEM_CLASSES[0] if self.severity == SEVERITIES[0] else ITEM_CLASSES[1]


@dataclass
class PacingPlan:
    # New items to name and mark now, criticals first.
    add: list = field(default_factory=list)
    # Pending critical items, top `daily_criticals`, not first shown today.
    # The caller names them once a UTC day; they are not additions.
    remind: list = field(default_factory=list)
    # Pending non-critical items. Any at all stops every addition (stop-add).
    blocking: list = field(default_factory=list)
    # New items this run did not add.
    waiting: list = field(default_factory=list)
    # Lines (`item_key`) of open provider-managed observations, never items.
    rolled_up: int = 0


def _utc(value: str) -> datetime:
    """A stored timestamp as an aware UTC datetime; SQLite writes them naive."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _still_reported(finding: Mapping) -> bool:
    return not finding.get("absent_since")


def _gather(rows: Iterable[Mapping]) -> tuple[list[Item], int]:
    """Rows in ranked order into items in ranked order, plus the rolled-up line count.

    A row is pending when a paced publisher showed it, it still waits for a
    decision (`surfaced`), and no complete sweep has missed it since it was
    last reported (`absent_since` unset). It is new when no paced publisher
    has shown it and it is `queued` or `surfaced`. Everything else --
    accepted, a shown row the sweep stopped reporting -- is in no item. An
    item's order is its best member's.
    """
    items: dict[tuple, Item] = {}
    managed: set[tuple] = set()
    covered: dict[tuple, int] = {}
    for row in rows:
        key = item_key(row)
        if rolled_up(row):
            managed.add(key)
            continue
        if decided_with_item(row):
            covered[key] = covered.get(key, 0) + 1
        state = row.get("state") or "queued"
        shown = row.get("first_shown_at")
        pending = bool(shown) and state == "surfaced" and _still_reported(row)
        new = not shown and state in ("queued", "surfaced")
        if not (pending or new):
            continue
        item = items.setdefault(key, Item(key=key))
        item.members.append(row)
        severity = row.get("severity")
        if severity in SEVERITIES and SEVERITIES.index(severity) < SEVERITIES.index(item.severity):
            item.severity = severity
        if pending:
            item.pending = True
            at = _utc(shown)
            item.first_shown = at if item.first_shown is None else min(item.first_shown, at)
    for key, item in items.items():
        item.covers = covered.get(key, 0)
    return list(items.values()), len(managed)


def pace(
    rows: Iterable[Mapping],
    now: datetime,
    limits: PacingLimits,
    added_today: Mapping[str, int],
    may_add: bool = True,
    announced: Iterable[Iterable[str]] = (),
) -> PacingPlan:
    """What a paced publisher names now, from the open rows in ranked order.

    `added_today` is `additions_on` for now's UTC date. The rules, in order:

    - Stop-add: while any pending item is non-critical, nothing is added.
    - Criticals are added from `REMIND_HOUR`, up to `daily_criticals` a day.
    - Non-criticals are added from `noncritical_after_hour`, up to
      `noncritical_max` a day, and only while no critical is pending and none
      is waiting for the budget or the hour.
    - Pending criticals not first shown today are offered as reminders, top
      `daily_criticals`; the caller names them once a day.

    `may_add=False` adds nothing whatever the rest says (the first inventory
    report is on its way).

    `announced` is the keys of the items already added today. One that is
    still new (every mark of it failed) is not added again, counts as an
    addition of its class, as if `added_today` held it, and holds additions
    back as a pending item of its class would.
    """
    now = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    items, managed = _gather(rows)
    critical, noncritical = ITEM_CLASSES

    pending = [item for item in items if item.pending]
    new = [item for item in items if not item.pending]
    announced_keys = {tuple(key) for key in announced}
    unrecorded = [item for item in new if item.key in announced_keys]
    new = [item for item in new if item.key not in announced_keys]

    def spent(item_class: str) -> int:
        return int(added_today.get(item_class) or 0) + sum(1 for item in unrecorded if item.item_class == item_class)

    blocking = [item for item in pending if item.item_class == noncritical]
    pending_critical = [item for item in pending if item.item_class == critical]
    remind = [item for item in pending_critical if item.first_shown.date() != now.date()][: limits.daily_criticals]

    # An unrecorded non-critical stops every addition, as a pending one does.
    # It is not in `blocking`, which the caller marks and names daily.
    noncritical_unrecorded = any(item.item_class == noncritical for item in unrecorded)
    add: list[Item] = []
    if may_add and not blocking and not noncritical_unrecorded:
        new_critical = [item for item in new if item.item_class == critical]
        if now.hour >= REMIND_HOUR:
            budget = max(0, limits.daily_criticals - spent(critical))
            add = new_critical[:budget]
        # A critical left out of `add` waits for tomorrow's budget or today's
        # hour, and a non-critical added now would hold it back behind
        # stop-add. With a daily limit of 0 no critical is ever added, so none
        # is waiting. A critical announced today but never recorded holds them
        # back as a pending one would.
        critical_waiting = limits.daily_criticals > 0 and len(new_critical) > len(add)
        critical_unrecorded = any(item.item_class == critical for item in unrecorded)
        if (
            not pending_critical
            and not critical_unrecorded
            and not add
            and not critical_waiting
            and now.hour >= limits.noncritical_after_hour
        ):
            budget = max(0, limits.noncritical_max - spent(noncritical))
            add = [item for item in new if item.item_class == noncritical][:budget]

    return PacingPlan(
        add=add,
        remind=remind,
        blocking=blocking,
        waiting=[item for item in new if item not in add],
        rolled_up=managed,
    )
