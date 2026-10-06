"""The onboarding sweep's hand-off to ranking, run by ``bootstrap_scan_gate.py``.

After the gate files the sweep card, this module runs once a minute on the
gate's tick: it files one ``bootstrap-inventory-cluster-*`` card per Cluster
Agent, waits for those cards and the sweep to settle, writes
``INVENTORY.raw.md`` from their structured ``metadata``, and files the
``bootstrap-inventory-prioritize`` card that ranks it into the report the user
receives — or, when no cluster's audit reached the raw file, writes that report
itself, saying so, and files no ranking card. The sweep's worker only lists the fleet and audits any cluster with
no Cluster Agent.

It is code rather than SOP text because each of these steps is mechanical
and the worker could not do them reliably: it has no tool that waits
(``execute_code`` is blocked in single-query mode, and ``sleep`` in the shell
sandbox returns early, #1981), so it closed or blocked its card over running
children; it typed the raw file by hand and left out the ```findings block
``inventory_findings.py extract`` needs; and it filed the ranking card without
its idempotency key.

Once-only, like the sweep: ``.bootstrap_handoff_filed`` records which sweep was
handed off and which ranking card it got (``none`` when it filed none). It names the sweep so that a re-armed
discovery, which deletes ``.bootstrap_scan_filed`` but may not know this marker,
still gets its own hand-off.
"""

import hashlib
import json
import re
import shlex
import sqlite3
import sys
import time
from pathlib import Path

# bootstrap_scan_gate.py's, which imports this module; test_bootstrap_handoff.py
# holds the two copies equal.
CLUSTER_KEY_PREFIX = "bootstrap-inventory-cluster-"
PRIORITIZE_KEY = "bootstrap-inventory-prioritize"
ASSIGNEE = "platform"
RAW_PATH = "/opt/data/INVENTORY.raw.md"
REPORT_PATH = "/opt/data/INVENTORY.md"
PRIORITIZE_INSTRUCTIONS_PATHS = (
    "/opt/data/profiles/platform/governance/inventory_prioritize_sop.md",
    "/opt/platform-template/governance/inventory_prioritize_sop.md",
)
PRIORITIZE_TITLE = "Prioritize the onboarding inventory report"
CLUSTER_AUDIT_INSTRUCTIONS_PATHS = (
    "/opt/data/profiles/platform/governance/cluster_inventory_audit_sop.md",
    "/opt/platform-template/governance/cluster_inventory_audit_sop.md",
)

HANDOFF_MARKER = ".bootstrap_handoff_filed"
MARKER_FIELD = re.compile(r"^(\w+)=(\S+)$")
MARKER_LINE = re.compile(r"^\s*(\w+)\s*=\s*(\S+)\s*$")
BOARD_FILE = "kanban.db"
SQLITE_BUSY_TIMEOUT_SECONDS = 10

# Settled: nothing more will come from the card without someone acting on it.
DONE = "done"
BLOCKED = "blocked"
# Hermes routes a card blocked twice for one cause to triage, which waits for a
# person; for the hand-off it has given what it will.
TRIAGE = "triage"
# Hermes's other terminal statuses (kanban_workspace_gc.py): a card a person
# cancelled or one that failed outright will not report either.
FAILED = "failed"
CANCELLED = "cancelled"
SETTLED = (DONE, BLOCKED, TRIAGE, FAILED, CANCELLED)
# A blocked sweep is not finished: unblocked, it may still list the fleet.
SWEEP_FINISHED = (DONE, FAILED, CANCELLED)
ARCHIVED = "archived"
# How long after the sweep was filed the hand-off stops waiting for unsettled
# cards and writes what it has, naming the rest as gaps. Seven clusters on two
# dispatcher slots took about seven minutes on a live install; a card that has
# not settled in an hour is stuck, and onboarding runs once, so a report with a
# gap beats no report.
DEADLINE_SECONDS = 3600
# Added per cluster card on top, so a fleet larger than the dispatcher clears in
# an hour is not cut off while its cards are only queued.
DEADLINE_PER_CARD_SECONDS = 300
SECONDS_PER_MINUTE = 60

SANDBOX_TIMEOUT_SECONDS = 30
# Absolute paths, so nothing the model left in the sandbox shadows them.
REMOTE_SH = "/bin/sh"
REMOTE_WRITE = 'umask 022 && /bin/cat > "$1.tmp" && /bin/mv -f -- "$1.tmp" "$1"'
TMP_SUFFIX = ".tmp"

# What the findings block's provider_managed means: an object the platform
# provider runs, which the ranking stage scores apart from the user's own.
PROVIDER_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")
PROVIDER_NAMESPACE_PREFIXES = ("gke-", "gmp-")
SEVERITIES = ("high", "medium", "low")
# Where the raw file's remediation plan puts each audit area: security first,
# then reliability, then observability.
AREA_HEADINGS = (
    ("security", "Priority 1 — Security & Identity Hardening"),
    ("reliability", "Priority 2 — Workload Reliability & Probes"),
    ("observability", "Priority 3 — Observability & Telemetry"),
)
OTHER_AREA_HEADING = "Other"
NOT_APPLICABLE = "n/a"
UNRATED = "unrated"
NO_REASON = "no reason given"
# GKE's status for a cluster serving normally; anything else is worth naming.
HEALTHY_CLUSTER_STATUSES = ("RUNNING",)
DEFAULT_CHECK = "finding"
STAMP_FORMAT = "%Y-%m-%d %H:%M UTC"
# A check slug derived from the issue text when the Cluster Agent gave none.
CHECK_SLUG_WORDS = 6
# A short digest of the whole title, so two findings whose titles share their
# first words keep separate identities in the findings queue.
CHECK_DIGEST_CHARS = 6
# cluster_inventory_audit_sop.md: the metadata fields that hold one entry each.
LIST_FIELDS = ("findings", "workloads")
# Recorded as the ranking card when no cluster was audited and none was filed.
NO_RANKING = "none"
GAPS_HEADING = "## Gaps"
# The no-coverage report goes to chat verbatim; the rest stay in the raw file.
MAX_REPORT_GAPS = 10
# Placeholders a Cluster Agent writes where a field names more than one object.
NOT_AN_OBJECT = ("", "multiple", "multiple workloads", "various", "n/a", "none")


def _log(msg: str) -> None:
    sys.stderr.write(f"bootstrap_handoff: {msg}\n")


def _board_path(data_dir: Path) -> Path:
    try:
        from hermes_cli.kanban_db import kanban_db_path

        return Path(kanban_db_path())
    except Exception:  # noqa: BLE001 - outside the pod, or an older board
        return data_dir / BOARD_FILE


def _read_marker(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    fields = {}
    for line in text.splitlines():
        for part in line.split():
            # Tolerates `key = value` as a person types it into a marker by hand.
            match = MARKER_FIELD.match(part) if "=" in part else None
            if match:
                fields[match.group(1)] = match.group(2)
        match = MARKER_LINE.match(line)
        if match and match.group(2):
            fields.setdefault(match.group(1), match.group(2))
    return fields


def _metadata(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        meta = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _block_reason(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        return str(json.loads(row[0]).get("reason") or "") if row and row[0] else ""
    except (ValueError, AttributeError):
        return ""


def read_board(board: Path, sweep_id: str) -> dict | None:
    """The sweep card and its cluster cards, or None when the board cannot say."""
    try:
        conn = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as e:
        _log(f"cannot open the board: {e}")
        return None
    try:
        row = conn.execute("SELECT status, created_at FROM tasks WHERE id = ?", (sweep_id,)).fetchone()
        if row is None:
            _log(f"sweep card {sweep_id} is not on the board")
            return None
        status, created_at = row
        sweep = {"id": sweep_id, "status": status, "created_at": created_at, "metadata": _metadata(conn, sweep_id),
                 "block_reason": _block_reason(conn, sweep_id) if status in (BLOCKED, TRIAGE) else ""}
        clusters = []
        archived = 0
        archived_keys = set()
        for tid, cstatus, key, title in conn.execute(
            "SELECT id, status, idempotency_key, title FROM tasks WHERE idempotency_key LIKE ? "
            "AND created_at >= ? ORDER BY created_at, id",
            (CLUSTER_KEY_PREFIX + "%", created_at),
        ).fetchall():
            if cstatus == ARCHIVED:
                archived += 1
                archived_keys.add(key)
                continue
            clusters.append({
                "id": tid, "status": cstatus, "key": key, "title": title or "",
                "metadata": _metadata(conn, tid) if cstatus == DONE else {},
                "block_reason": _block_reason(conn, tid) if cstatus in (BLOCKED, TRIAGE) else "",
            })
        # Open onboarding cards the board would answer a create with instead of
        # filing a new one: an earlier run's, and a ranking card this hand-off
        # did not file, such as one a sweep worker filed before the raw file
        # existed. The body tells the hand-off's own card apart, so a tick that
        # filed it but failed to record it still finds it.
        stale = {
            key: tid for tid, key, body, at in conn.execute(
                "SELECT id, idempotency_key, body, created_at FROM tasks WHERE status != ? "
                "AND (idempotency_key LIKE ? OR idempotency_key = ?)",
                (ARCHIVED, CLUSTER_KEY_PREFIX + "%", PRIORITIZE_KEY),
            ).fetchall()
            if at < created_at or (key == PRIORITIZE_KEY and (body or "") != _prioritize_body())
        }
        return {"sweep": sweep, "clusters": clusters, "archived_clusters": archived, "archived_keys": archived_keys,
                "stale_keys": stale}
    except sqlite3.Error as e:
        _log(f"cannot read the board: {e}")
        return None
    finally:
        conn.close()


def settled(state: dict) -> bool:
    """True once the sweep is done and no cluster card has more to give.

    A blocked sweep is not settled: it may still list the fleet or audit a
    cluster with no Cluster Agent once unblocked. The deadline hands off what
    there is, which leaves a person time to unblock it first. A failed or
    cancelled sweep will not, so it settles.
    """
    return state["sweep"]["status"] in SWEEP_FINISHED and all(c["status"] in SETTLED for c in state["clusters"])


def deadline(state: dict) -> int:
    """Seconds after the sweep was filed that the hand-off stops waiting."""
    return DEADLINE_SECONDS + DEADLINE_PER_CARD_SECONDS * len(state["clusters"])


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value) -> list:
    """A list from model-written metadata, or empty when it is anything else."""
    return value if isinstance(value, list) else []


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    digest = hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()[:CHECK_DIGEST_CHARS]
    return "-".join((words[:CHECK_SLUG_WORDS] or [DEFAULT_CHECK]) + [digest])


def _provider_managed(namespace: str) -> bool:
    return namespace in PROVIDER_NAMESPACES or namespace.startswith(PROVIDER_NAMESPACE_PREFIXES)


def finding_lines(meta: dict) -> list[dict]:
    """The ```findings block lines for one cluster's audit ``metadata``.

    One line per finding the Cluster Agent reported. A finding that names no
    single workload is filed against its namespace, or against the cluster when
    it names no single namespace either, rather than dropped: the user can still
    act on it.
    """
    project, cluster = _text(meta.get("project")), _text(meta.get("cluster"))
    if not project or not cluster:
        return []
    lines = []
    for finding in _list(meta.get("findings")):
        if not isinstance(finding, dict):
            continue
        title = _text(finding.get("issue")) or _text(finding.get("title"))
        if not title:
            continue
        workload = _text(finding.get("workload"))
        namespace = _text(finding.get("namespace"))
        scoped = namespace.lower() not in NOT_AN_OBJECT
        named = workload.lower() not in NOT_AN_OBJECT
        if named:
            obj = workload
        elif scoped:
            obj = namespace
        else:
            obj = cluster
        line = {
            "check": _text(finding.get("check")) or _slug(f"{_text(finding.get('area'))} {title}"),
            "project": project,
            "cluster": cluster,
            "object": obj,
            "title": title,
        }
        if scoped:
            line["namespace"] = namespace
        detail = _text(finding.get("recommendation"))
        if detail:
            line["detail"] = detail
        severity = _text(finding.get("severity")).lower()
        if severity in SEVERITIES:
            line["severity_hint"] = severity
        if scoped and _provider_managed(namespace):
            line["provider_managed"] = True
        lines.append(line)
    return lines


def _cell(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None or value == "":
        return NOT_APPLICABLE
    return str(value).replace("|", "/").replace("\n", " ")


def _named(meta) -> bool:
    """An audit the report can list: it names its project and cluster."""
    return isinstance(meta, dict) and bool(_text(meta.get("cluster")) and _text(meta.get("project")))


def _sweep_audits(sweep: dict) -> list:
    """The sweep's own audits; one object where the SOP asks for a list is one audit."""
    clusters = sweep["metadata"].get("clusters")
    return [clusters] if isinstance(clusters, dict) else _list(clusters)


def _malformed(meta: dict) -> list[tuple[str, str]]:
    """The list fields the audit SOP prescribes that a card set to something else.

    An empty value of the wrong type (``{}``, ``""``, ``0``) says "none" and
    loses nothing, so it is not reported.
    """
    bad = [(f, type(meta[f]).__name__) for f in LIST_FIELDS if meta.get(f) and not isinstance(meta[f], list)]
    gaps = meta.get("gaps")
    if gaps and not isinstance(gaps, (list, str)):
        bad.append(("gaps", type(gaps).__name__))
    return bad


def _gap_list(meta: dict) -> list:
    gaps = meta.get("gaps")
    if isinstance(gaps, str):
        gaps = [gaps]
    return [g for g in gaps if g] if isinstance(gaps, list) else []


def _node_pools(topology: dict) -> str:
    pools = _list(topology.get("node_pools"))
    return ", ".join(
        f"{_text(p.get('name'))} ({_text(p.get('machine_type')) or NOT_APPLICABLE})" for p in pools if isinstance(p, dict)
    ) or NOT_APPLICABLE


def _flag(block: dict, *keys: str) -> str:
    return "/".join(_cell(block.get(k)) for k in keys) if isinstance(block, dict) else NOT_APPLICABLE


def compose(state: dict, timed_out: bool, now: float | None = None) -> str:
    """``INVENTORY.raw.md`` for one sweep, from its cards' metadata.

    The sweep reports the clusters it audited itself (those with no Cluster
    Agent) under ``metadata.clusters`` and the fleet it enumerated under
    ``metadata.fleet``; a fleet cluster nobody reported on is a gap, because a
    silent gap reads as a clean cluster.
    """
    sweep = state["sweep"]
    audits: list[tuple[dict, str]] = []  # (metadata, source card)
    gaps: list[str] = []
    for card in state["clusters"]:
        meta = card["metadata"]
        if card["status"] == DONE and _named(meta):
            audits.append((meta, card["id"]))
        elif card["status"] == DONE:
            own = "; ".join(_cell(g) for g in _gap_list(meta))
            gaps.append(
                f"{_cell(card['title'] or card['key'])} ({card['id']}): completed without the `project` and `cluster` "
                "its findings need, so none of them are listed" + (f" — it reported: {own}" if own else "")
            )
        elif card["status"] in (BLOCKED, TRIAGE):
            gaps.append(
                f"{_cell(card['title'] or card['key'])} ({card['id']}): {card['status']} — "
                f"{_cell(card['block_reason'] or NO_REASON)}"
            )
        elif card["status"] in (FAILED, CANCELLED):
            gaps.append(f"{_cell(card['title'] or card['key'])} ({card['id']}): {card['status']} before it reported")
        else:
            gaps.append(f"{_cell(card['title'] or card['key'])} ({card['id']}): still {_cell(card['status'])} when the hand-off ran")
    for meta in _sweep_audits(sweep):
        if _named(meta):
            audits.append((meta, sweep["id"]))
            gaps.append(
                f"{_cell(meta.get('cluster'))} ({_cell(meta.get('project'))}): no Cluster Agent, so the sweep "
                "audited it itself"
            )
        elif isinstance(meta, dict):
            gaps.append(f"sweep {sweep['id']}: an audit it reported has no `project` and `cluster`, so it is not listed")
    for meta, card_id in audits:
        for gap in _gap_list(meta):
            gaps.append(f"{_cell(meta.get('cluster'))} ({card_id}): {_cell(gap)}")
        for field, kind in _malformed(meta):
            gaps.append(
                f"{_cell(meta.get('cluster'))} ({card_id}): `{field}` was a {kind}, not a list, so nothing "
                "from it is listed"
            )
        dropped = len(_list(meta.get("findings"))) - len(finding_lines(meta))
        if dropped:
            gaps.append(
                f"{_cell(meta.get('cluster'))} ({card_id}): {dropped} reported finding(s) had no `issue` "
                "or `title` to list, so they are not in the plan or the findings block"
            )
    for gap in _gap_list(sweep["metadata"]):
        gaps.append(f"sweep {sweep['id']}: {_cell(gap)}")
    if sweep["status"] in (BLOCKED, TRIAGE):
        gaps.append(f"sweep {sweep['id']} {sweep['status']} — {_cell(sweep['block_reason'] or NO_REASON)}")
    elif sweep["status"] in (FAILED, CANCELLED):
        gaps.append(f"sweep {sweep['id']} {sweep['status']}: the fleet list and any agent-less audits are missing")
    elif sweep["status"] != DONE:
        gaps.append(
            f"sweep {sweep['id']} was still {_cell(sweep['status'])}: discovery did not finish, so clusters "
            "it would have listed or audited may be missing"
        )
    reported = {(_text(m.get("project")), _text(m.get("cluster"))) for m, _ in audits}
    fleet = [f for f in _list(sweep["metadata"].get("fleet")) if isinstance(f, dict)]
    for entry in fleet:
        status = _text(entry.get("status"))
        if status and status.upper() not in HEALTHY_CLUSTER_STATUSES:
            gaps.append(f"{_cell(entry.get('cluster'))} ({_cell(entry.get('project'))}): cluster status {_cell(status)}")
        if (_text(entry.get("project")), _text(entry.get("cluster"))) not in reported:
            gaps.append(
                f"{_cell(entry.get('cluster'))} ({_cell(entry.get('project'))}, {_cell(entry.get('location'))}): "
                "listed in the fleet but no audit reported on it"
            )
    if not fleet:
        gaps.append("the sweep reported no fleet list, so clusters with no Cluster Agent may be missing")
    for agent in state.get("unfiled", []):
        if agent.get("stale_card"):
            gaps.append(
                f"{_cell(agent['title'])}: an earlier run's card {agent['stale_card']} holds this Cluster "
                "Agent's key and could not be archived, so no card was filed for this sweep"
            )
        else:
            gaps.append(f"{_cell(agent['title'])}: the gate could not file this Cluster Agent's card")
    if state.get("archived_clusters"):
        gaps.append(f"{state['archived_clusters']} cluster card(s) were archived before they reported")
    if timed_out:
        gaps.append(f"the hand-off stopped waiting {deadline(state) // SECONDS_PER_MINUTE} minutes after the sweep was filed")

    stamp = time.strftime(STAMP_FORMAT, time.gmtime(now if now is not None else time.time()))
    out = [
        "# GKE Environment Discovery Report — raw findings",
        "",
        (
            f"First-time environment scan, compiled at {stamp} from the per-cluster audit cards of "
            f"sweep `{sweep['id']}`. It carries every entry the audits filed under `findings`, and the delivered "
            "report is ranked from those alone; a condition an audit recorded only in a structured field (HPA, "
            "security context, namespace governance, node autoscaling) is not carried forward."
        ),
        "",
        "## Coverage",
        "",
        "| Cluster | Project | Location | Card | Workloads audited | Scanned in full |",
        "| :------ | :------ | :------- | :--- | ----------------: | :-------------- |",
    ]
    for meta, card_id in audits:
        complete = not _gap_list(meta) and not _malformed(meta)
        out.append(
            f"| {_cell(meta.get('cluster'))} | {_cell(meta.get('project'))} | {_cell(meta.get('location'))} "
            f"| {card_id} | {len(_list(meta.get('workloads')))} | {'yes' if complete else 'no — see Gaps'} |"
        )
    out += [
        "",
        "## GKE Fleet Discovery",
        "",
        ("| Cluster Name | GCP Region / Zone | K8s Version | Node Pools / Machine Types | Workload Identity "
        "| Dataplane V2 | Observability Stack | Deployment Toolchain |"),
        ("| :----------- | :---------------- | :---------- | :------------------------- | :---------------- "
        "| :----------- | :------------------ | :------------------- |"),
    ]
    for meta, _ in audits:
        topo = meta.get("topology") if isinstance(meta.get("topology"), dict) else {}
        out.append(
            f"| {_cell(meta.get('cluster'))} | {_cell(meta.get('location'))} | {_cell(topo.get('k8s_version'))} "
            f"| {_cell(_node_pools(topo))} | {_cell(topo.get('workload_identity'))} | {_cell(topo.get('dataplane_v2'))} "
            f"| {_cell(topo.get('observability'))} | {_cell(topo.get('deployment_toolchain'))} |"
        )
    telemetry = _text(sweep["metadata"].get("telemetry"))
    if telemetry:
        out += [
            "",
            (
                f"Agent telemetry (the PlatformAgent's `.status.telemetry`, as the sweep read it): {_cell(telemetry)}. "
                "A Cluster Agent cannot see this resource, so an observability finding below may be contradicted by it."
            ),
        ]
    out += [
        "",
        "## Workloads Inventory",
        "",
        ("| Cluster | Namespace | Workload Name | Kind | Replicas | Probes (Live/Ready/Startup) "
        "| Requests/Limits/QoS | Telemetry | NonRoot/ReadOnlyFS/Privileged |"),
        ("| :------ | :-------- | :------------ | :--- | :------- | :-------------------------- "
        "| :------------------ | :-------- | :---------------------------- |"),
    ]
    for meta, _ in audits:
        for w in _list(meta.get("workloads")):
            if not isinstance(w, dict):
                continue
            out.append(
                f"| {_cell(meta.get('cluster'))} | {_cell(w.get('namespace'))} | {_cell(w.get('name'))} "
                f"| {_cell(w.get('kind'))} | {_cell(w.get('replicas'))} "
                f"| {_flag(w.get('probes'), 'liveness', 'readiness', 'startup')} "
                f"| {_flag(w.get('resources'), 'requests', 'limits', 'qos')} | {_cell(w.get('telemetry'))} "
                f"| {_flag(w.get('security_context'), 'run_as_non_root', 'read_only_root_fs', 'privileged')} |"
            )
    lines: list[dict] = []
    out += ["", "## Prioritized SRE Remediation Plan", ""]
    by_area: dict[str, list[str]] = {}
    for meta, _ in audits:
        for finding, line in zip(
            [f for f in _list(meta.get("findings")) if isinstance(f, dict) and (_text(f.get("issue")) or _text(f.get("title")))],
            finding_lines(meta),
        ):
            lines.append(line)
            area = _text(finding.get("area")).lower()
            where = "/".join(p for p in (line["cluster"], line.get("namespace"), line["object"]) if p)
            sev = line.get("severity_hint", UNRATED).upper()
            rec = f" Recommended: {_cell(line['detail'])}" if line.get("detail") else ""
            by_area.setdefault(area, []).append(f"- **[{sev}]** `{_cell(where)}`: {_cell(line['title'])}.{rec}")
    known = [a for a, _ in AREA_HEADINGS]
    for area, heading in AREA_HEADINGS + ((None, OTHER_AREA_HEADING),):
        items = by_area.get(area, []) if area else [i for a, v in by_area.items() if a not in known for i in v]
        out += [f"### {heading}", ""] + (items or ["- None reported."]) + [""]
    out += ["## Gaps", ""] + ([f"- {g}" for g in gaps] or ["- None."]) + [""]
    out += ["## Machine-Readable Findings", "", "```findings"]
    out += [json.dumps(line, ensure_ascii=True) for line in lines]
    out += ["```", ""]
    return "\n".join(out)


def _sandbox():
    import sandbox_exec  # beside this script in the pod

    return sandbox_exec


def report_written(data_dir: Path) -> bool | None:
    """Whether ``INVENTORY.md`` is already where delivery reads it; None when unknown."""
    try:
        sx = _sandbox()
        if not sx.sandbox_enabled():
            return (data_dir / Path(REPORT_PATH).name).exists()
        return sx.read_bytes(
            REPORT_PATH, max_bytes=1, principal=sx.TERMINAL_PRINCIPAL, timeout=SANDBOX_TIMEOUT_SECONDS
        ) is not None
    except Exception as e:  # noqa: BLE001 - retry next tick
        _log(f"cannot tell whether {REPORT_PATH} exists: {e}")
        return None


def covered(state: dict) -> bool:
    """True when at least one cluster's audit reached the report."""
    return any(c["status"] == DONE and _named(c["metadata"]) for c in state["clusters"]) or any(
        _named(m) for m in _sweep_audits(state["sweep"])
    )


def no_coverage_report(raw: str) -> str:
    """The delivered report when no cluster was audited: what went wrong, never a clean result."""
    # A heading on its own line: _cell keeps table and list values on one line.
    heading = f"\n{GAPS_HEADING}\n"
    start = raw.find(heading)
    body = start + len(heading)
    end = raw.find("\n## ", body) if start >= 0 else -1
    section = raw[body:end if end >= 0 else None] if start >= 0 else ""
    lines = [line for line in section.splitlines() if line.strip()]
    shown = "\n".join(lines[:MAX_REPORT_GAPS])
    more = len(lines) - MAX_REPORT_GAPS
    tail = f"\n\n{more} more in the full record." if more > 0 else ""
    return (
        "## Onboarding scan: no cluster was audited\n\n"
        "The first-time scan finished without an audit of any cluster, so this is not a clean result. "
        f"What it recorded:\n\n{shown}{tail}\n\nThe full record is `{RAW_PATH}`.\n"
    )


def write_raw(data_dir: Path, text: str, path: str = RAW_PATH) -> bool:
    """Write the raw file (or ``path``) where the ranking card's terminal reads it."""
    try:
        sx = _sandbox()
        in_sandbox = sx.sandbox_enabled()
    except Exception as e:  # noqa: BLE001 - retry next tick
        _log(f"cannot tell where {path} goes: {e}")
        return False
    if not in_sandbox:
        target = data_dir / Path(path).name
        tmp = target.with_suffix(TMP_SUFFIX)
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)
            return True
        except OSError as e:
            _log(f"could not write {target}: {e}")
            return False
    # As the terminal's login, which owns the sandbox's /opt/data; the ranking
    # card's worker writes its own files beside this one.
    try:
        done = sx.run(
            [REMOTE_SH, "-c", REMOTE_WRITE, "sh", path],
            stdin=text, principal=sx.TERMINAL_PRINCIPAL, timeout=SANDBOX_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 - retry next tick
        _log(f"could not write {path} in the sandbox: {e}")
        return False
    if done.returncode != 0:
        _log(f"could not write {path} in the sandbox: {(done.stderr or '').strip()}")
        return False
    return True


def _prioritize_body() -> str:
    paths = "\n".join(f"  - {p}" for p in PRIORITIZE_INSTRUCTIONS_PATHS)
    return (
        "Rank the onboarding inventory into the report the user receives. Follow the prioritization "
        f"SOP, reading whichever of these exists:\n{paths}\n\n"
        f"Your only input is `{RAW_PATH}`; write the ranked report to `{REPORT_PATH}`. A separate "
        "delivery job posts that file to the user verbatim. Do not message the user yourself."
    )


def _cluster_body(agent: dict) -> str:
    paths = "\n".join(f"  - {p}" for p in CLUSTER_AUDIT_INSTRUCTIONS_PATHS)
    return (
        f"Audit {agent['cluster_label']}, the cluster you are pinned to, for first-time onboarding. Follow "
        f"the single-cluster audit SOP, reading whichever of these exists:\n{paths}\n\n"
        "Complete this card with the structured `metadata` that SOP specifies; the onboarding gate reads "
        "it into the inventory report. Never block this card: record anything you could not do in `gaps` "
        "and complete."
    )


def _create(parse_task_id, assignee: str, key: str, title: str, body: str) -> str | None:
    try:
        from hermes_cli.kanban import run_slash
    except Exception as e:  # noqa: BLE001 - kanban unavailable; retry next tick
        _log(f"kanban API unavailable: {e}")
        return None
    cmd = (
        f"create --json --assignee {shlex.quote(assignee)} --idempotency-key {shlex.quote(key)} "
        f"--body {shlex.quote(body)} {shlex.quote(title)}"
    )
    try:
        out = str(run_slash(cmd)).strip()
    except Exception as e:  # noqa: BLE001 - never fail the cron run
        _log(f"could not file {key}: {e}")
        return None
    task_id = parse_task_id(out)
    if not task_id:
        _log(f"could not read a task id from the board response for {key}: {out}")
    return task_id


def _archive(task_id: str) -> bool:
    """Archive an earlier run's onboarding card so its key can be filed again."""
    try:
        from hermes_cli.kanban import run_slash

        out = str(run_slash(f"archive {shlex.quote(task_id)}")).strip()
    except Exception as e:  # noqa: BLE001 - retry next tick
        _log(f"could not archive {task_id}: {e}")
        return False
    _log(f"asked the board to archive {task_id}, a card holding an onboarding key: {out}")
    return True


def file_cluster_cards(state: dict, roster: list, parse_task_id) -> tuple[int, list]:
    """File a card for every Cluster Agent on the roster that has none for this sweep.

    The gate, not the sweep's worker, is the one creator of these cards: asked
    to make the calls itself, the worker once completed saying it had fanned out
    to seven clusters having filed none, and the report that followed called
    the fleet clean. Re-run every tick until the hand-off, so a create that
    failed is retried; the idempotency key keeps a retry from filing twice, and
    a card someone archived is not filed again. Returns how many it filed and
    the agents still without a card.
    """
    have = {c["key"] for c in state["clusters"]} | state.get("archived_keys", set())
    stale = state.get("stale_keys", {})
    filed, unfiled = 0, []
    for agent in roster:
        if agent["key"] in have:
            continue
        old = stale.get(agent["key"])
        if old and not _archive(old):
            unfiled.append(dict(agent, stale_card=old))
            continue
        task_id = _create(parse_task_id, agent["name"], agent["key"], agent["title"], _cluster_body(agent))
        if old and task_id == old:
            # run_slash reports a failed archive as text rather than raising;
            # the board handing back the old card is the sign it failed.
            unfiled.append(dict(agent, stale_card=old))
        elif task_id:
            filed += 1
        else:
            unfiled.append(agent)
    return filed, unfiled


def file_prioritize(parse_task_id, stale: dict | None = None) -> str | None:
    old = (stale or {}).get(PRIORITIZE_KEY)
    if old and not _archive(old):
        return None
    task_id = _create(parse_task_id, ASSIGNEE, PRIORITIZE_KEY, PRIORITIZE_TITLE, _prioritize_body())
    if old and task_id == old:
        _log(f"the board handed back {old}, a ranking card the hand-off did not file and the archive did not remove")
        return None
    return task_id


def _record(marker: Path, sweep_id: str, task_id: str, now: float) -> None:
    marker.write_text(f"sweep={sweep_id}\ntask_id={task_id}\nfiled_at={int(now)}\n", encoding="utf-8")


def hand_off(data_dir: Path, scan_marker: Path, parse_task_id, roster=None, now: float | None = None) -> str | None:
    """Advance the hand-off one tick. Returns the ranking card id once filed.

    ``roster`` is the gate's list of ready Cluster Agents (or a callable that
    returns it), each ``{name, key, title, cluster_label}``; their cards are
    filed here, once per sweep.
    """
    now = time.time() if now is None else now
    filed = _read_marker(scan_marker)
    sweep_id = filed.get("task_id", "")
    if not sweep_id:
        _log(f"{scan_marker} names no task_id; nothing to hand off")
        return None
    marker = data_dir / HANDOFF_MARKER
    done = _read_marker(marker)
    if done.get("sweep") == sweep_id:
        return None
    state = read_board(_board_path(data_dir), sweep_id)
    if state is None:
        return None
    # Archiving the sweep, or every one of its cards, is how a person, the
    # re-arm runbook or a bench stack cancels it, often to plant a raw file of
    # their own; writing over that file once the deadline passed would undo
    # them. The runbook archives the sweep first for this reason: one archived
    # card among live ones only skips that cluster.
    if state["sweep"]["status"] == ARCHIVED or (state["archived_clusters"] and not state["clusters"]):
        _log(f"sweep {sweep_id} or all of its cluster cards are archived; not handing off")
        return None
    agents = (roster() if callable(roster) else roster) or []
    filed_now, unfiled = file_cluster_cards(state, agents, parse_task_id)
    if filed_now:
        # The cards just filed have not run; nothing is settled this tick.
        return None
    state["unfiled"] = unfiled
    try:
        filed_at = float(filed["filed_at"])
    except (KeyError, ValueError):
        # A marker written by hand may carry only the card id; its own
        # timestamp is when the sweep was filed, near enough.
        filed_at = scan_marker.stat().st_mtime
    ready = settled(state) and not unfiled
    timed_out = not ready and now - filed_at >= deadline(state)
    if not ready and not timed_out:
        return None
    # An install upgraded mid-delivery: the previous design's ranking card has
    # already written the report the user is waiting for, and this sweep has no
    # hand-off marker. Handing off would replace that report.
    written = report_written(data_dir)
    if written is None:
        return None
    if written:
        _log(f"{REPORT_PATH} is already written and sweep {sweep_id} has no hand-off; leaving it for delivery")
        return None
    raw = compose(state, timed_out, now)
    if not write_raw(data_dir, raw):
        return None
    if not covered(state):
        # Ranked, an empty raw file reads as a clean environment; the user gets
        # what went wrong instead, through the same delivery job.
        if not write_raw(data_dir, no_coverage_report(raw), REPORT_PATH):
            return None
        try:
            _record(marker, sweep_id, NO_RANKING, now)
        except OSError as e:
            _log(f"wrote {REPORT_PATH} but could not write {marker}: {e}")
        _log(f"no cluster was audited for sweep {sweep_id}; wrote {REPORT_PATH} without ranking")
        return None
    task_id = file_prioritize(parse_task_id, state.get("stale_keys"))
    if not task_id:
        return None
    try:
        _record(marker, sweep_id, task_id, now)
    except OSError as e:
        # The board's idempotency key stops a second ranking card; the raw file is
        # rewritten from the same cards on the next tick.
        _log(f"filed {task_id} but could not write {marker}: {e}")
    _log(f"wrote {RAW_PATH} for sweep {sweep_id} and filed ranking card {task_id}")
    return task_id
