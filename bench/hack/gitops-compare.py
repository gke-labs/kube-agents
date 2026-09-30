#!/usr/bin/env python3
"""Side-by-side comparison of two GitOps fix-cycle run records (gke-labs/kube-agents#1773).

For one task run by two models in the same campaign repetition, prints what
the records show, so a diagnosis of why one did better rests on evidence: the
scores; each verification entry's verdict; the fix each opened (PR title and
the files it touched, read from GitHub when a token is available); whether
each delegated to a Cluster Agent; cluster reads before the fix and time from
the first worker call to the fix (from the tagged worker trajectory #1746
records, through gitops-audit); the diagnosis each wrote in its heartbeats;
and tool calls that returned an error.

Usage: gitops-compare.py <results.json A> <results.json B> [--markdown]
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

AUDIT_SCRIPT = pathlib.Path(__file__).with_name("gitops-audit.py")
# Ceiling on one `gh pr view`; a PR that takes longer to read is reported as unavailable.
GH_TIMEOUT_S = 30
GITOPS_ENTRY_NAME = "gitops_fix_cycle"
HEARTBEAT_NAME = "kanban_heartbeat"
MAX_HEARTBEATS = 6
MAX_NOTE_CHARS = 220
ERROR_STATUSES = ("error", "failed", "failure")
PR_FIELDS = "title,files,additions,deletions,body"
PR_BODY_CHARS = 400


def _load_audit():
    spec = importlib.util.spec_from_loader("gitops_audit", importlib.machinery.SourceFileLoader("gitops_audit", str(AUDIT_SCRIPT)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _pr(url: str) -> dict:
    """Title, files and body of the fix PR through `gh`; empty when unavailable."""
    if not url:
        return {}
    try:
        out = subprocess.run(["gh", "pr", "view", url, "--json", PR_FIELDS], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    data = json.loads(out.stdout)
    return {
        "title": data.get("title", ""),
        "files": [f.get("path", "") for f in data.get("files", [])],
        "additions": data.get("additions"),
        "deletions": data.get("deletions"),
        "body": (data.get("body") or "")[:PR_BODY_CHARS],
    }


def _note(args) -> str:
    if isinstance(args, str) and args.startswith("{"):
        try:
            args = json.loads(args)
        except ValueError:
            return args[:MAX_NOTE_CHARS]
    if isinstance(args, dict):
        return str(args.get("note") or args)[:MAX_NOTE_CHARS]
    return str(args)[:MAX_NOTE_CHARS]


def _model(path: str, record: dict) -> str:
    """The row's model: results.json does not carry it, rows.json beside it does."""
    rows = pathlib.Path(path).with_name("rows.json")
    if rows.exists():
        data = json.load(open(rows, encoding="utf-8"))
        row = data[0] if isinstance(data, list) and data else data
        if isinstance(row, dict) and row.get("model"):
            return str(row["model"])
    return str(record.get("model") or record.get("agent_model") or "")


def summarize(path: str, audit_mod) -> dict:
    record = json.load(open(path, encoding="utf-8"))
    if isinstance(record, list):
        record = record[0]
    trajectory = record.get("trajectory") or []
    workers = [e for e in trajectory if e.get("agent")]
    audit = audit_mod.audit(record)
    scores = record.get("scores") or {}
    entries = record.get("verification_report") or []
    first_at = min((_epoch(e.get("at")) for e in workers if _epoch(e.get("at"))), default=None)
    fix_at = _epoch(audit.get("fix_submitted_at"))
    heartbeats = [{"agent": e.get("agent"), "note": _note(e.get("args"))} for e in workers if e.get("name") == HEARTBEAT_NAME][:MAX_HEARTBEATS]
    errors = [f'{e.get("agent")}: {e.get("name")} {str(e.get("args"))[:80]}' for e in workers if str(e.get("status", "")).lower() in ERROR_STATUSES]
    outcome = next((e.get("result") for e in trajectory if e.get("name") == GITOPS_ENTRY_NAME), {}) or {}
    return {
        "record": path,
        "model": _model(path, record),
        "task": record.get("task_id") or record.get("task") or "",
        "outcome_score": (scores.get("OutcomeScore") or {}).get("score") if isinstance(scores.get("OutcomeScore"), dict) else scores.get("OutcomeScore"),
        "entries": {e.get("name"): e.get("status") for e in entries},
        "entry_counts": {s: sum(1 for e in entries if e.get("status") == s) for s in ("pass", "fail", "error")},
        "gitops_outcome": outcome.get("outcome"),
        "pr": _pr(outcome.get("pr_url", "")) | {"url": outcome.get("pr_url", "")},
        "delegated": audit["delegated_to_cluster_agent"],
        "agents": audit["agents"],
        "worker_entries": audit["worker_entries"],
        "cluster_reads_before_fix": audit["cluster_reads_before_fix"],
        "repo_lookups_before_fix": audit["repo_lookups_before_fix"],
        "minutes_to_fix": round((fix_at - first_at) / 60, 1) if fix_at and first_at else None,
        "heartbeats": heartbeats,
        "tool_errors": errors,
    }


def markdown(a: dict, b: dict) -> str:
    rows = [
        ("model", a["model"], b["model"]),
        ("outcomeScore", a["outcome_score"], b["outcome_score"]),
        ("entries pass/fail/error", "/".join(str(a["entry_counts"][k]) for k in ("pass", "fail", "error")), "/".join(str(b["entry_counts"][k]) for k in ("pass", "fail", "error"))),
        ("fix cycle", a["gitops_outcome"], b["gitops_outcome"]),
        ("PR", a["pr"].get("title") or a["pr"].get("url"), b["pr"].get("title") or b["pr"].get("url")),
        ("files changed", ", ".join(a["pr"].get("files", [])), ", ".join(b["pr"].get("files", []))),
        ("delegated to Cluster Agent", a["delegated"], b["delegated"]),
        ("worker entries", a["worker_entries"], b["worker_entries"]),
        ("cluster reads before fix", a["cluster_reads_before_fix"], b["cluster_reads_before_fix"]),
        ("repo/card lookups before fix", a["repo_lookups_before_fix"], b["repo_lookups_before_fix"]),
        ("minutes first call -> fix", a["minutes_to_fix"], b["minutes_to_fix"]),
        ("tool errors", len(a["tool_errors"]), len(b["tool_errors"])),
    ]
    out = ["| | A | B |", "| --- | --- | --- |"] + [f"| {k} | {x} | {y} |" for k, x, y in rows]
    out.append("")
    out.append("Per-entry verdicts (A / B):")
    for name in sorted(set(a["entries"]) | set(b["entries"])):
        out.append(f"- {name}: {a['entries'].get(name, '-')} / {b['entries'].get(name, '-')}")
    for label, s in (("A", a), ("B", b)):
        out.append("")
        out.append(f"{label} diagnosis, from heartbeats ({s['model']}):")
        if s["heartbeats"]:
            out.extend(f"- [{h['agent']}] {h['note']}" for h in s["heartbeats"])
        else:
            out.append("- none recorded")
        if s["pr"].get("body"):
            out.append(f"{label} PR body (start): {s['pr']['body']!r}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    audit_mod = _load_audit()
    a, b = summarize(argv[1], audit_mod), summarize(argv[2], audit_mod)
    if "--markdown" in argv:
        print(markdown(a, b))
    else:
        print(json.dumps({"A": a, "B": b}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
