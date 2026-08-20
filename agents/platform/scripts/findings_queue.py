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
from typing import Any, Iterable

__all__ = [
    "FindingError",
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
    "record_verification",
    "get_publication",
    "put_publication",
]


class FindingError(ValueError):
    """A registration or transition the queue refuses, with the reason."""


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
# `record_verification`; `queued` is registration's. What is left is the three
# human transitions plus the snooze expiry the daily job runs (§3.2, §6.1).
PATCHABLE_STATES = ("accepted", "dismissed", "snoozed", "surfaced")

VERIFY_OUTCOMES = ("still_failing", "resolved", "unverifiable")

PUBLISHERS = ("backlog", "nudge")
PUBLICATION_TARGET_KINDS = ("github-issue", "repo-file", "chat")


# --------------------------------------------------------------------------
# Identity (§3.1)
# --------------------------------------------------------------------------
#
# Transcribed from `derive_finding_id` and `_shorten_id` in
# agents/platform/skills/fleet-audit/scripts/audit_report.py rather than
# imported: that module is a 3000-line CLI that shells out to git and gh, it
# ships in the skills tree rather than on this server's PYTHONPATH, and the
# derivation is a pure function of four fields. `test_findings_queue.py`
# asserts byte-equality against it over a corpus so the two cannot drift.

ID_EMPTY_SEGMENT = "_"
ID_SEGMENTS = 4
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


def derive_finding_id(check: str, cluster: str, namespace: str, object_name: str) -> str:
    """`(check, cluster, namespace, object)`, the identity an audit finding gets.

    Same string for the same problem whichever source found it, which is what
    §10's cross-source collision depends on.
    """
    full = ".".join(
        (
            _id_segment(check),
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
        raise FindingError(f"rubric.C is {value!r}; anchors are 1.0 (measured), 0.9 (live state), 0.6 (inferred)")
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
            raise FindingError(f"rubric.{key} is {value!r}; anchors are {list(anchors)}")
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
    return (
        0 if finding.get("actionable") else 1,
        -int(finding.get("rank_score") or 0),
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

FINDINGS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS findings_ranked ON findings(state, rank_score DESC)",
    "CREATE INDEX IF NOT EXISTS findings_urgent ON findings(likelihood, blast_radius, alarmed_at)",
    "CREATE INDEX IF NOT EXISTS findings_object ON findings(cluster, namespace, object)",
    "CREATE INDEX IF NOT EXISTS findings_pr     ON findings(pr_state) WHERE pr_state IS NOT NULL",
)

_COLUMNS = (
    "id", "source", "check_slug", "cluster", "namespace", "object", "title", "detail",
    "root_cause", "severity", "rank_score", "rubric", "provider_managed", "actionable",
    "recommendation", "remediation", "verification", "pr_url", "pr_state", "state",
    "first_seen", "last_verified", "last_verification", "surfaced_at", "surface_count",
    "snoozed_until", "alarmed_at", "chat_id", "thread_id",
)

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM findings"


def init_findings_schema(conn: sqlite3.Connection) -> None:
    conn.execute(FINDINGS_SCHEMA)
    conn.execute(PUBLICATIONS_SCHEMA)
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
        raise FindingError(f"remediation.kind is {kind!r}; must be one of {list(REMEDIATION_KINDS)}")
    path = _text(raw.get("path"), "remediation.path", required=False)
    if path and kind != "manifest":
        raise FindingError(f"remediation.path is only meaningful when kind is 'manifest', not {kind!r}")
    out = {"kind": kind, "note": _text(raw.get("note"), "remediation.note", limit=MAX_TEXT_CHARS)}
    if path:
        out["path"] = path
    return out


def _validate_verification(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise FindingError("verification must be an object with kind, command and still_failing_when")
    kind = _text(raw.get("kind"), "verification.kind")
    if kind not in VERIFICATION_KINDS:
        raise FindingError(f"verification.kind is {kind!r}; must be one of {list(VERIFICATION_KINDS)}")
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
        raise FindingError(f"source is {source!r}; must be one of {list(SOURCES)}")

    check = _text(raw.get("check") if raw.get("check") is not None else raw.get("check_slug"), "check")
    cluster = _text(raw.get("cluster"), "cluster")
    namespace = _text(raw.get("namespace"), "namespace", required=False)
    object_name = _text(raw.get("object"), "object")

    rubric = validate_rubric(raw.get("rubric"))
    score = rank_score(rubric)

    finding_id = derive_finding_id(check, cluster, namespace, object_name)
    if not FINDING_ID_RE.match(finding_id):
        # Reachable only when a field is entirely outside `[a-z0-9]`, which
        # `_id_segment` collapses to the empty-segment sentinel.
        raise FindingError(
            f"check/cluster/namespace/object derive the unusable id {finding_id!r}; "
            "each must carry at least one alphanumeric character"
        )

    return {
        "id": finding_id,
        "source": source,
        "check_slug": check,
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
) -> list[dict]:
    clauses, params = [], []
    if cluster:
        clauses.append("cluster = ?")
        params.append(cluster)
    if state:
        if state not in STATES:
            raise FindingError(f"state is {state!r}; must be one of {list(STATES)}")
        clauses.append("state = ?")
        params.append(state)
    if severity:
        if severity not in SEVERITIES:
            raise FindingError(f"severity is {severity!r}; must be one of {list(SEVERITIES)}")
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
    "source", "check_slug", "cluster", "namespace", "object", "title", "detail",
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

    if state in STICKY_STATES:
        # The user rejected this. Record that it was seen again so the row's
        # freshness is honest, and change nothing else (§5.2).
        conn.execute("UPDATE findings SET last_verified = datetime('now') WHERE id = ?", (row["id"],))
        return "suppressed"

    if state in RECURRENCE_STATES:
        conn.execute(
            f"UPDATE findings SET {assignments}, state = 'queued', last_verified = datetime('now'), "
            "surface_count = 0, alarmed_at = NULL WHERE id = ?",
            (*values, row["id"]),
        )
        return "updated"

    conn.execute(
        f"UPDATE findings SET {assignments}, last_verified = datetime('now') WHERE id = ?",
        (*values, row["id"]),
    )
    return "updated"


def _downgrade_absent(conn: sqlite3.Connection, cluster: str, seen: set[str]) -> int:
    """§5.2's reciprocal case: absence lowers confidence, it does not resolve.

    A sweep that died halfway produces the same silence as a fleet that got
    healthier, so a row the run did not re-report is re-ranked down at the
    rubric's own value for "inferred from absence" and left on the list for
    §7.4 to settle.
    """
    rows = conn.execute(
        "SELECT id, rubric FROM findings WHERE cluster = ? AND state = 'queued'", (cluster,)
    ).fetchall()
    downgraded = 0
    for finding_id, raw in rows:
        if finding_id in seen:
            continue
        rubric = json.loads(raw)
        if rubric.get("C") == 60:
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
        cluster = _text(scope.get("cluster"), "scope.cluster")
        downgraded = _downgrade_absent(conn, cluster, seen)

    return {"results": results, "downgraded": downgraded}


# --------------------------------------------------------------------------
# Transitions (§3.2)
# --------------------------------------------------------------------------


def mark_surfaced(conn: sqlite3.Connection, finding_id: str, chat_id: str = "", thread_id: str = "") -> dict:
    """Record that a publisher named this row, after the send."""
    if get_finding(conn, finding_id) is None:
        raise KeyError(finding_id)
    conn.execute(
        "UPDATE findings SET surface_count = surface_count + 1, surfaced_at = datetime('now'), "
        "state = CASE WHEN state = 'queued' THEN 'surfaced' ELSE state END, "
        "chat_id = COALESCE(NULLIF(?, ''), chat_id), thread_id = COALESCE(NULLIF(?, ''), thread_id) "
        "WHERE id = ?",
        (chat_id or "", thread_id or "", finding_id),
    )
    return get_finding(conn, finding_id)


def patch_finding(conn: sqlite3.Connection, finding_id: str, patch: Any) -> dict:
    """The three human transitions, the snooze expiry, and PR reconciliation."""
    if not isinstance(patch, dict):
        raise FindingError("patch must be an object")
    if get_finding(conn, finding_id) is None:
        raise KeyError(finding_id)

    assignments, values = [], []

    if "state" in patch:
        state = _text(patch.get("state"), "state")
        if state not in PATCHABLE_STATES:
            raise FindingError(
                f"state is {state!r}; this route sets {list(PATCHABLE_STATES)}. "
                "'resolved' and 'stale' are verification outcomes and 'queued' is registration's"
            )
        assignments.append("state = ?")
        values.append(state)
        if state == "snoozed":
            until = _text(patch.get("snoozed_until"), "snoozed_until")
            assignments.append("snoozed_until = ?")
            values.append(until)
        elif state == "surfaced":
            assignments.append("snoozed_until = NULL")
    elif "snoozed_until" in patch:
        raise FindingError("snoozed_until is set by the transition to 'snoozed'")

    if "pr_url" in patch:
        assignments.append("pr_url = ?")
        values.append(_text(patch.get("pr_url"), "pr_url", required=False) or None)
    if "pr_state" in patch:
        pr_state = _text(patch.get("pr_state"), "pr_state", required=False)
        if pr_state and pr_state not in PR_STATES:
            raise FindingError(f"pr_state is {pr_state!r}; must be one of {list(PR_STATES)}")
        assignments.append("pr_state = ?")
        values.append(pr_state or None)

    if not assignments:
        raise FindingError("patch names no field this route can set")

    conn.execute(f"UPDATE findings SET {', '.join(assignments)} WHERE id = ?", (*values, finding_id))
    return get_finding(conn, finding_id)


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
        raise KeyError(finding_id)
    outcome = _text(outcome, "outcome")
    if outcome not in VERIFY_OUTCOMES:
        raise FindingError(f"outcome is {outcome!r}; must be one of {list(VERIFY_OUTCOMES)}")

    assignments = ["last_verification = ?"]
    values: list[Any] = [
        json.dumps(
            {"outcome": outcome, "observed": _text(observed, "observed", required=False, limit=MAX_TEXT_CHARS)},
            sort_keys=True,
        )
    ]

    if outcome == "unverifiable":
        # `last_verified` deliberately does not advance: the queue did not
        # manage to ask, and a row that looks freshly checked is the lie this
        # third outcome exists to prevent.
        if object_missing:
            assignments.append("state = 'stale'")
    else:
        assignments.append("last_verified = datetime('now')")
        if outcome == "resolved":
            assignments.append("state = 'resolved'")

    if rubric is not None:
        new_rubric = validate_rubric(rubric)
        score = rank_score(new_rubric)
        assignments += ["rubric = ?", "rank_score = ?", "severity = ?"]
        values += [json.dumps(new_rubric, sort_keys=True), score, severity_for(score, new_rubric)]
        # §4.6's fourth re-rank event, the only one that lowers a score: the
        # fault stopped firing, so the alarm may fire again if it comes back.
        if current["rubric"]["L"] == FLOOR_LIKELIHOOD and new_rubric["L"] < FLOOR_LIKELIHOOD:
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
        raise FindingError(f"publisher is {publisher!r}; must be one of {list(PUBLISHERS)}")
    if not isinstance(body, dict):
        raise FindingError("body must be an object with target_kind, target_ref and content_hash")
    target_kind = _text(body.get("target_kind"), "target_kind")
    if target_kind not in PUBLICATION_TARGET_KINDS:
        raise FindingError(f"target_kind is {target_kind!r}; must be one of {list(PUBLICATION_TARGET_KINDS)}")
    conn.execute(
        "INSERT INTO queue_publications (publisher, target_kind, target_ref, content_hash, last_published) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(publisher) DO UPDATE SET target_kind = excluded.target_kind, "
        "target_ref = excluded.target_ref, content_hash = excluded.content_hash, "
        "last_published = excluded.last_published",
        (
            publisher,
            target_kind,
            _text(body.get("target_ref"), "target_ref", required=False, limit=MAX_TEXT_CHARS) or None,
            _text(body.get("content_hash"), "content_hash", required=False) or None,
        ),
    )
    return get_publication(conn, publisher)
