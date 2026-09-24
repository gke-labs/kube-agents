"""Typed evidence and artifact records for delegated kanban tasks.

Installed into the image at ``/opt/hermes/tools/kanban_evidence_tools.py`` and
registered into ``tools/kanban_tools.py`` by ``apply_kanban_evidence_tools.py``.

A worker's prose report is not evidence. The admin portal's CUJ contract
(#804, #867) scores typed records — which API was called with what request and
what the analysis found; which manifest was produced, paired with what,
targeting where — projected on the task. These two worker-only tools give a
worker a place to put them: rows in two tables beside the task in
``kanban.db``, which the admin console's in-pod reader projects
(``admin_console/agent_runtime.py``) with provenance nested under ``details``.

The recorder checks shape, not truth: a known type, objects where objects are
expected, the fields a record must name to be checkable at all, a bounded
size, and a task that exists and is the worker's own. What the record claims
is for the evaluators and reviewers who read it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

EVIDENCE_TYPES = frozenset(
    {
        "quota_check",
        "advice_service_capacity",
        "computeclass_server_dry_run",
        "advice_service_workload_obtainability_planning",
        "workload_obtainability_planning_analysis",
    }
)
ARTIFACT_TYPES = frozenset(
    {
        "computeclass",
        "node_auto_provisioning",
        "provisioning_request",
        "local_queue",
    }
)
EVIDENCE_STATUSES = frozenset({"completed", "failed"})

#: What a completed record must name before it means anything: the method
#: that answered, and the request fields a reader needs to check it without
#: replaying it. A failed probe may be recorded thinly.
REQUIRED_EVIDENCE_ARGS = {
    "advice_service_capacity": ("api_method",),
    "advice_service_workload_obtainability_planning": ("api_method",),
}
REQUIRED_REQUEST_FIELDS = {
    "quota_check": ("region",),
    "advice_service_capacity": ("region", "acceleratorType", "acceleratorCount"),
    "advice_service_workload_obtainability_planning": (
        "region",
        "nodeCount",
        "futureResourcesSpecs",
    ),
}
#: An artifact whose manifest does not say what it provisions needs the shape
#: recorded beside it.
REQUIRED_ARTIFACT_ARGS = {"provisioning_request": ("machine_spec",)}

#: One serialized object may not exceed this: large enough for a full
#: ComputeClass or a multi-zone analysis, small enough that a runaway worker
#: cannot turn the board into a blob store.
MAX_OBJECT_BYTES = 65536

_DDL = """
CREATE TABLE IF NOT EXISTS task_evidence (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    api_method TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    analysis_json TEXT NOT NULL DEFAULT '{}',
    execution_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_evidence_task ON task_evidence(task_id);
CREATE TABLE IF NOT EXISTS task_artifacts (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    pair_id TEXT NOT NULL DEFAULT '',
    target_json TEXT NOT NULL DEFAULT '{}',
    machine_spec_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id);
"""

#: kanban.db lives on the profile's persistent volume, so a board an earlier
#: image created keeps the first shape of task_artifacts until these run.
_ARTIFACT_MIGRATIONS = (
    "ALTER TABLE task_artifacts ADD COLUMN target_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE task_artifacts ADD COLUMN machine_spec_json TEXT NOT NULL DEFAULT '{}'",
)

RECORD_EVIDENCE_SCHEMA = {
    "name": "record_evidence",
    "description": (
        "Record one typed, machine-readable evidence entry on your current "
        "task: which API or command you executed (``api_method``), the "
        "request you made, and the analysis you derived from its real "
        "output. The admin portal projects these records, so they are how a "
        "reviewer verifies your work happened — claims that exist only in "
        "your prose report are not evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task to attach the record to; defaults to your own "
                    "task when running under the dispatcher."
                ),
            },
            "type": {
                "type": "string",
                "enum": sorted(EVIDENCE_TYPES),
                "description": "The evidence contract this record satisfies.",
            },
            "status": {
                "type": "string",
                "enum": sorted(EVIDENCE_STATUSES),
                "description": "Whether the underlying check succeeded.",
            },
            "api_method": {
                "type": "string",
                "description": (
                    "Canonical method behind the record, e.g. "
                    "compute.beta.AdviceService.Capacity."
                ),
            },
            "request": {
                "type": "object",
                "description": "The request you actually made, as an object.",
            },
            "analysis": {
                "type": "object",
                "description": (
                    "Structured findings derived from the real response "
                    "(quantities, zones, windows, provisioning models)."
                ),
            },
            "execution_ref": {
                "type": "string",
                "description": "Pointer at the raw execution that produced this.",
            },
        },
        "required": ["type", "analysis"],
    },
}

ATTACH_ARTIFACT_SCHEMA = {
    "name": "attach_artifact",
    "description": (
        "Attach one structured artifact you produced to your current task: "
        "a ComputeClass manifest, a Node Auto-Provisioning spec, a "
        "ProvisioningRequest, or a Kueue LocalQueue. Pass the parsed "
        "manifest as an object, not YAML text. Artifacts that belong "
        "together share a ``pair_id`` and a ``target``."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task to attach the artifact to; defaults to your own "
                    "task when running under the dispatcher."
                ),
            },
            "type": {
                "type": "string",
                "enum": sorted(ARTIFACT_TYPES),
                "description": "What kind of artifact this is.",
            },
            "manifest": {
                "type": "object",
                "description": "The artifact itself, as a parsed object.",
            },
            "pair_id": {
                "type": "string",
                "description": "Shared id linking artifacts produced together.",
            },
            "target": {
                "type": "object",
                "description": (
                    "Where the artifact points: region, zone, and startTime "
                    "of the recommended window."
                ),
            },
            "machine_spec": {
                "type": "object",
                "description": (
                    "The shape the artifact provisions, e.g. "
                    "{'acceleratorType': 'tpu-v5e', 'chipsPerNode': 4}."
                ),
            },
        },
        "required": ["type", "manifest"],
    },
}


def _connect():
    """Connect to the active kanban board (lazily, like kanban_tools)."""
    from hermes_cli import kanban_db as kb

    return kb.connect()


def _ensure_tables(connection) -> None:
    connection.executescript(_DDL)
    for statement in _ARTIFACT_MIGRATIONS:
        try:
            connection.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _scoped_task_id(requested: object, tool_error):
    """Resolve the target task, refusing cross-task writes from a worker."""
    env_tid = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    tid = str(requested or "").strip() or env_tid
    if not tid:
        return None, tool_error(
            "no task in scope: pass task_id or run under the dispatcher"
        )
    if env_tid and tid != env_tid:
        return None, tool_error(
            f"worker is scoped to task {env_tid}; refusing to write records "
            f"onto {tid}"
        )
    return tid, None


def _bounded_json(value: object, field: str, tool_error):
    if not isinstance(value, dict):
        return None, tool_error(f"{field} must be an object")
    try:
        rendered = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return None, tool_error(f"{field} must be JSON-serializable")
    if len(rendered.encode("utf-8")) > MAX_OBJECT_BYTES:
        return None, tool_error(
            f"{field} exceeds {MAX_OBJECT_BYTES} bytes; record a summary and "
            "reference the raw output instead"
        )
    return rendered, None


def _missing(container: object, fields: tuple[str, ...]) -> list[str]:
    if not isinstance(container, dict):
        return list(fields)
    return [f for f in fields if not str(container.get(f) or "").strip()]


def _task_exists(connection, task_id: str) -> bool:
    return connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is not None


def make_handlers(tool_error):
    """Build the two handlers around the caller's ``tool_error`` shape."""

    def handle_record_evidence(args: dict, **_kw) -> str:
        kind = str(args.get("type") or "")
        if kind not in EVIDENCE_TYPES:
            return tool_error(
                f"unknown evidence type {kind!r}; expected one of {sorted(EVIDENCE_TYPES)}"
            )
        status = str(args.get("status") or "completed")
        if status not in EVIDENCE_STATUSES:
            return tool_error(
                f"unknown status {status!r}; expected one of {sorted(EVIDENCE_STATUSES)}"
            )
        tid, err = _scoped_task_id(args.get("task_id"), tool_error)
        if err:
            return err
        request = args.get("request") or {}
        if status == "completed":
            missing = _missing(args, REQUIRED_EVIDENCE_ARGS.get(kind, ()))
            if missing:
                return tool_error(
                    f"{kind} evidence must name {', '.join(missing)} — the "
                    "canonical method that answered, e.g. "
                    "compute.beta.AdviceService.Capacity"
                )
            missing = _missing(request, REQUIRED_REQUEST_FIELDS.get(kind, ()))
            if missing:
                return tool_error(
                    f"{kind} evidence must name {', '.join(missing)} in its "
                    "request — record what you actually asked the API for, so "
                    "the finding can be checked and replayed"
                )
        request_json, err = _bounded_json(request, "request", tool_error)
        if err:
            return err
        analysis_json, err = _bounded_json(args.get("analysis") or {}, "analysis", tool_error)
        if err:
            return err
        connection = _connect()
        _ensure_tables(connection)
        if not _task_exists(connection, tid):
            return tool_error(f"task {tid} does not exist on this board")
        with connection:
            connection.execute(
                "INSERT INTO task_evidence (task_id, type, status, api_method,"
                " request_json, analysis_json, execution_ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    kind,
                    status,
                    str(args.get("api_method") or ""),
                    request_json,
                    analysis_json,
                    str(args.get("execution_ref") or ""),
                    time.time(),
                ),
            )
        return f"recorded {kind} evidence on {tid}"

    def handle_attach_artifact(args: dict, **_kw) -> str:
        kind = str(args.get("type") or "")
        if kind not in ARTIFACT_TYPES:
            return tool_error(
                f"unknown artifact type {kind!r}; expected one of {sorted(ARTIFACT_TYPES)}"
            )
        manifest = args.get("manifest")
        # A manifest whose structured sections collapsed to scalars is not a
        # deliverable — a live worker once attached {"metadata": 1, "spec": 1}.
        if isinstance(manifest, dict):
            for section in ("metadata", "spec"):
                if section in manifest and not isinstance(manifest[section], dict):
                    return tool_error(
                        f"manifest.{section} must be an object; pass the full "
                        "parsed manifest, not a summary of it"
                    )
        missing = _missing(args, REQUIRED_ARTIFACT_ARGS.get(kind, ()))
        if missing:
            return tool_error(
                f"a {kind} artifact must carry {', '.join(missing)} naming the "
                "shape it provisions (e.g. acceleratorType, with the chip arithmetic)"
            )
        tid, err = _scoped_task_id(args.get("task_id"), tool_error)
        if err:
            return err
        manifest_json, err = _bounded_json(manifest, "manifest", tool_error)
        if err:
            return err
        target_json, err = _bounded_json(args.get("target") or {}, "target", tool_error)
        if err:
            return err
        machine_spec_json, err = _bounded_json(
            args.get("machine_spec") or {}, "machine_spec", tool_error
        )
        if err:
            return err
        connection = _connect()
        _ensure_tables(connection)
        if not _task_exists(connection, tid):
            return tool_error(f"task {tid} does not exist on this board")
        with connection:
            connection.execute(
                "INSERT INTO task_artifacts (task_id, type, manifest_json,"
                " pair_id, target_json, machine_spec_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    kind,
                    manifest_json,
                    str(args.get("pair_id") or ""),
                    target_json,
                    machine_spec_json,
                    time.time(),
                ),
            )
        return f"attached {kind} artifact to {tid}"

    return handle_record_evidence, handle_attach_artifact


def register(registry, check_fn, tool_error) -> None:
    """Register both tools; called from inside ``tools/kanban_tools.py``."""
    record_evidence, attach_artifact = make_handlers(tool_error)
    registry.register(
        name="record_evidence",
        toolset="kanban",
        schema=RECORD_EVIDENCE_SCHEMA,
        handler=record_evidence,
        check_fn=check_fn,
        emoji="🧾",
    )
    registry.register(
        name="attach_artifact",
        toolset="kanban",
        schema=ATTACH_ARTIFACT_SCHEMA,
        handler=attach_artifact,
        check_fn=check_fn,
        emoji="📎",
    )
