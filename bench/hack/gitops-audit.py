#!/usr/bin/env python3
"""Isolation audit of a GitOps fix-cycle run record (gke-labs/kube-agents#1773).

Reads a devops-bench ``results.json`` whose ``trajectory`` carries the worker
entries #1746 records (each tagged with ``agent``, ``task``, ``session``,
``at``) and the harness's ``gitops_fix_cycle`` entry, and prints the three
values a handoff states per run:

- ``cluster_reads_before_fix``: worker calls that read the task cluster
  before the fix was submitted: a terminal command that invokes kubectl (by
  name or through a wrapper the command defined) with a read verb or runs the
  Cluster Agent's preflight, an MCP cluster tool call, or a Cluster Agent
  delegation. Prose that mentions kubectl (a PR body, a card result) and reads
  of the worker's own environment are not counted;
- ``repo_lookups_before_fix``: worker calls that could have shown another run's
  work before the fix: pull-request listing or viewing, foreign card views,
  session or memory search;
- ``fix_submitted_at``: the first worker call that submits the fix (a
  submit-suggestion run, a ``gh pr create`` or a ``git push``), falling back to
  the pull request's merge time from the harness entry.

Usage: gitops-audit.py <results.json> [--json]
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

GITOPS_ENTRY_NAME = "gitops_fix_cycle"
#: A terminal command reads the cluster when a segment of it (split on newlines,
#: `;`, `&&`, `||` and `|`) invokes kubectl -- by name, or through a wrapper the
#: command itself defined around it -- with one of these verbs, or runs the
#: Cluster Agent's preflight script. Prose that quotes `kubectl get`, a PR body,
#: a card result, `printenv GKE_*` or `head` over a script's source are not
#: reads and do not count; neither does `kubectl config`, which reads a file.
KUBECTL_VERB_RE = re.compile(r"\b(get|describe|logs|top|events|explain|rollout\s+history|api-resources|cluster-info|auth\s+can-i|wait)\b")
#: `k() { kubectl ...; }`, `alias k=kubectl ...`, `K="kubectl ..."`: the name is a wrapper.
WRAPPER_DEF_RE = re.compile(
    r"(?:function\s+)?\b([A-Za-z_]\w*)\s*\(\)\s*\{\s*kubectl\b"
    r"|\balias\s+([A-Za-z_]\w*)=['\"]?kubectl\b"
    r"|\b([A-Za-z_]\w*)=['\"]kubectl\b"
)
SEGMENT_SPLIT_RE = re.compile(r"\n|;|&&|\|\||\|")
LEADING_ASSIGNMENTS_RE = re.compile(r"^(?:[A-Za-z_]\w*=\S*\s+)+")
INTERPRETERS = frozenset({"bash", "sh", "python3", "python", "uv"})
PREFLIGHT_RE = re.compile(r"cluster_preflight\.(?:sh|py)$")
#: The MCP cluster tools, by tool name (`tool_call` carries it in its arguments).
MCP_CLUSTER_TOOL_RE = re.compile(r"^mcp__(?:gke|k8s|kubernetes)__", re.IGNORECASE)
#: Entries that carry text about the cluster without touching it.
NEVER_A_READ = frozenset(
    {"tool_describe", "write_file", "read_file", "search_files", "patch", "kanban_complete", "kanban_heartbeat", "skill_view", "skill_manage"}
)
#: Delegating to a Cluster Agent counts as a cluster read by proxy.
DELEGATION_RE = re.compile(r"kanban_create|delegate", re.IGNORECASE)
#: Calls that could surface another run's work.
REPO_LOOKUP_RE = re.compile(
    r"gh\s+pr\s+(list|view)|gh\s+api\s+\S*pulls|gh\s+search|git\s+log|session_search|memory_search|kanban_show|kanban_list",
    re.IGNORECASE,
)
#: Pull-request listing or viewing, the subset of lookups never excused by
#: being scoped to the run's own branch.
PR_LOOKUP_RE = re.compile(r"gh\s+pr\s+(list|view)|gh\s+api\s+\S*pulls|gh\s+search", re.IGNORECASE)
#: A worker viewing its own card is how it reads its assignment, not a lookup.
OWN_CARD_RE = re.compile(r'"task_id":\s*"([^"]+)"')
#: How much of a repository lookup the report quotes.
LOOKUP_PREVIEW_CHARS = 160
#: The call that submits the fix: submit-suggestion's `submit` verb (its
#: `prepare`, `list` and `fetch` verbs and viewing the skill are not), or a
#: direct `gh pr create` / `git push`. Matched against terminal commands only:
#: a PR body or a heartbeat note that mentions a push is not the push.
FIX_SUBMIT_RE = re.compile(r"submit_suggestion\.py[\\\"']*\s+submit\b(?!\s*--help)|gh\s+pr\s+create|git\s+push", re.IGNORECASE)


def _args(entry: dict) -> dict:
    args = entry.get("args")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args, strict=False)  # a recorded command may carry a raw newline
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _terminal_reads_cluster(command: str) -> bool:
    wrappers = {name for m in WRAPPER_DEF_RE.finditer(command) for name in m.groups() if name}
    for raw in SEGMENT_SPLIT_RE.split(command):
        segment = LEADING_ASSIGNMENTS_RE.sub("", raw.strip())
        words = segment.split()
        if not words:
            continue
        head = words[0].strip("(){}")
        if head in INTERPRETERS:
            script = words[1] if len(words) > 1 else ""
            if PREFLIGHT_RE.search(script):
                return True
            continue
        if PREFLIGHT_RE.search(head):
            return True
        if head.rsplit("/", 1)[-1] == "kubectl" or head.lstrip("$").strip("{}") in wrappers:
            if KUBECTL_VERB_RE.search(segment):
                return True
    return False


def _blocked(entry: dict) -> bool:
    """A call the sandbox's command policy or security scan refused ran nothing.

    Only a refusal is excluded: a read that ran and failed (a denied verb, a
    missing metrics API) still reached the cluster and still counts.
    """
    result = entry.get("result")
    if isinstance(result, str) and result.startswith("{"):
        try:
            result = json.loads(result)
        except ValueError:
            return False
    return isinstance(result, dict) and result.get("status") == "blocked"


def reads_cluster(entry: dict) -> bool:
    """Whether one worker entry read the task cluster."""
    name = entry.get("name") or ""
    if name in NEVER_A_READ or _blocked(entry):
        return False
    if DELEGATION_RE.search(name):
        return True
    args = _args(entry)
    if name == "terminal":
        command = args.get("command")
        return isinstance(command, str) and _terminal_reads_cluster(command)
    tool = name
    if name == "tool_call":
        tool = str(args.get("name") or "")
        arguments = args.get("arguments")
        if not tool and isinstance(arguments, dict) and "resourceType" in arguments and "/clusters/" in str(arguments.get("parent", "")):
            # The record clipped the tool name; the arguments are the GKE MCP shape.
            return True
    return bool(MCP_CLUSTER_TOOL_RE.match(tool))


def _text(entry: dict) -> str:
    args = entry.get("args")
    return f'{entry.get("name", "")} {args if isinstance(args, str) else json.dumps(args or {})}'


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def audit(record: dict) -> dict:
    trajectory = record.get("trajectory") or []
    workers = [e for e in trajectory if e.get("agent")]
    harness = next((e for e in trajectory if e.get("name") == GITOPS_ENTRY_NAME), None)
    outcome = (harness or {}).get("result") or {}

    fix_at = None
    for e in workers:
        command = _args(e).get("command") if e.get("name") == "terminal" else None
        if isinstance(command, str) and FIX_SUBMIT_RE.search(command):
            fix_at = _epoch(e.get("at"))
            break
    if fix_at is None:
        fix_at = _epoch(outcome.get("merged_at"))

    def before_fix(e: dict) -> bool:
        at = _epoch(e.get("at"))
        return fix_at is None or at is None or at <= fix_at

    cluster_reads = [e for e in workers if before_fix(e) and reads_cluster(e)]
    own_cards = {e.get("task") for e in workers if e.get("task")}
    own_branch = str(outcome.get("run_branch") or "")

    def foreign_lookup(e: dict) -> bool:
        text = _text(e)
        if not REPO_LOOKUP_RE.search(text):
            return False
        if e.get("name") == "kanban_show":
            # No task_id shows the worker's own card; a card this run's
            # workers ran (the Cluster Agent card it delegated to) is its own.
            m = OWN_CARD_RE.search(text)
            return bool(m) and m.group(1) not in own_cards
        # Listing or viewing pull requests always counts: on a per-run
        # repository it can only show the run's own, and the handoff says so,
        # but the count must not hide the call. Reading the run's own branch
        # (its tree, its commits, its log) is in-scope repository work.
        if PR_LOOKUP_RE.search(text):
            return True
        return not (own_branch and own_branch in text)

    repo_lookups = [e for e in workers if before_fix(e) and foreign_lookup(e)]
    agents = sorted({e["agent"] for e in workers})
    return {
        "worker_entries": len(workers),
        "agents": agents,
        "delegated_to_cluster_agent": any(a != "platform" for a in agents),
        "fix_submitted_at": datetime.fromtimestamp(fix_at, timezone.utc).isoformat() if fix_at else None,
        "cluster_reads_before_fix": len(cluster_reads),
        "repo_lookups_before_fix": len(repo_lookups),
        "repo_lookups": [_text(e)[:LOOKUP_PREVIEW_CHARS] for e in repo_lookups],
        "gitops_outcome": outcome.get("outcome"),
        "pr_url": outcome.get("pr_url"),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        record = json.load(fh)
    if isinstance(record, list):
        record = record[0]
    report = audit(record)
    if "--json" in argv:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
