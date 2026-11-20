"""list-gitops-repos route, discovery, and gate checks against the verifier and scorer.

Verifies the case's `read-from-gitops-preflight`, `no-external-discovery`
(card-log + typed-call `all` pair), and `no-web-or-mcp-discovery` checks on
Hermes-rendered card rows and tagged worker tool calls, including chained and
multi-line shell calls collapsed by Hermes's one-line card logger, harness
forge/clone scripts (`vcs.py`, `inspect_repository.py`, `submit_suggestion.py`,
`gitops_preflight.py --repo`), and wrapped MCP `tool_call` entries. Also pins
that a discovery violation fails `classify_rep` at the gate even when
`gitops_preflight.py` ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.rollup import rollup
from devops_bench.verification.spec import VerificationEntry, parse_entries
from kube_agents_bench import cases, scoring, transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "list-gitops-repos" / "task.yaml"
PREFLIGHT_CHECK = "read-from-gitops-preflight"
DISCOVERY_CHECK = "no-external-discovery"
MCP_WEB_CHECK = "no-web-or-mcp-discovery"
PREFLIGHT_LIST = 'python3 "$HERMES_HOME"/scripts/gitops_preflight.py --list'
PREFLIGHT_BARE = 'python3 "$HERMES_HOME"/scripts/gitops_preflight.py'
_OK = '{"output": "MANAGED: gke-agentic/example-infra\\nCONTEXT: none", "exit_code": 0}'


def _entry(check_name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (entry,) = [c for c in doc["verification_spec"] if c.get("name") == check_name]
    return entry


def _terminal(command: str, at: float, task: str = "t_1") -> dict:
    return {
        "name": "terminal",
        "args": {"command": command},
        "result": _OK,
        "status": "completed",
        "agent": "platform",
        "task": task,
        "session": "s_1",
        "at": at,
    }


def _as_row(command: str) -> str:
    return " ".join(command.split())


def _run_entry(
    check_name: str,
    commands: list[str],
    *,
    rows: list[str] | None = None,
    extra_trajectory: list[dict] | None = None,
) -> str:
    entries = [_terminal(cmd, at=float(i)) for i, cmd in enumerate(commands, 1)]
    if extra_trajectory:
        entries.extend(extra_trajectory)
    card_rows = [_as_row(cmd) for cmd in commands] if rows is None else rows
    transcript.set(
        "Manages gke-agentic/example-infra.",
        entries,
        worker_commands=[{"task": "t_1", "command": r} for r in card_rows],
    )
    try:
        res = VerifierAgent().run_entry(VerificationEntry(**_entry(check_name)), timeout_sec=10.0)
    finally:
        transcript.clear()
    return res.status


def test_discovery_check_keeps_card_log_and_typed_members_in_sync():
    check = _entry(DISCOVERY_CHECK)["check"]
    assert check["type"] == "all" and len(check["checks"]) == 2
    row_member, typed_member = check["checks"]
    assert row_member["type"] == "worker_commands" and "after_result_pattern" not in row_member
    assert typed_member["type"] == "worker_commands" and typed_member["after_result_pattern"] == "(?!)"
    assert row_member["forbidden_runs"] == typed_member["forbidden_runs"]


def test_preflight_accepts_list_and_bare_invocations_and_rejects_help_or_grep():
    assert _run_entry(PREFLIGHT_CHECK, [PREFLIGHT_LIST]) == "pass"
    assert _run_entry(PREFLIGHT_CHECK, [PREFLIGHT_BARE]) == "pass"
    assert _run_entry(PREFLIGHT_CHECK, ['python3 "$HERMES_HOME"/scripts/gitops_preflight.py --help']) == "fail"
    assert _run_entry(PREFLIGHT_CHECK, ["grep gitops_preflight.py agents/platform/SOUL.md"]) == "fail"
    assert _run_entry(PREFLIGHT_CHECK, ["cat /etc/gitops/managed_repos"]) == "fail"


def test_discovery_allows_clean_preflight_and_grep_in_soul():
    assert _run_entry(DISCOVERY_CHECK, [PREFLIGHT_LIST]) == "pass"
    assert _run_entry(DISCOVERY_CHECK, [PREFLIGHT_LIST, 'grep -n "kubectl" agents/platform/SOUL.md']) == "pass"
    assert _run_entry(DISCOVERY_CHECK, [PREFLIGHT_LIST, "git status -s"]) == "pass"


def test_discovery_rejects_cluster_cloud_forge_harness_scripts_and_http_probes():
    forbidden = [
        "kubectl get configmap -n kubeagents-system platform-agent-gitops-state -o yaml",
        "gcloud container clusters list",
        "gh repo list gke-agentic",
        "glab repo list",
        "git ls-remote https://github.com/gke-agentic/example-infra.git",
        r"git \ ls-remote https://github.com/gke-agentic/example-infra.git",
        "curl -sS https://ghes.eval.internal/api/v3/users/bot/repos",
        "curl -sS https://container.googleapis.com/v1/projects/p/locations/-/clusters",
        "wget -qO- https://git.corp.example/api/v4/groups/gke-agentic/projects",
        "python3 /opt/data/profiles/platform/skills/version-control/scripts/vcs.py clone gke-agentic/example-infra",
        "python3 /opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py clone --repo gke-agentic/example-infra",
        "python3 /opt/data/profiles/platform/skills/submit-suggestion/scripts/submit_suggestion.py prepare --repo gke-agentic/example-infra",
        'python3 "$HERMES_HOME"/scripts/gitops_preflight.py --repo gke-agentic/example-infra',
        'python3 "$HERMES_HOME"/scripts/gitops_preflight.py --repo=gke-agentic/example-infra',
    ]
    for cmd in forbidden:
        assert _run_entry(DISCOVERY_CHECK, [PREFLIGHT_LIST, cmd]) == "fail", f"should fail on {cmd!r}"


def test_discovery_catches_chained_and_multiline_commands_collapsed_in_card_log():
    chained = f"{PREFLIGHT_LIST} && kubectl get configmap -n kubeagents-system platform-agent-gitops-state -o yaml"
    # Hermes collapses chains in the card log to `<first> + 1 command`, so the row member
    # alone passes while the paired typed-call member catches the chained kubectl.
    row_member = _entry(DISCOVERY_CHECK)["check"]["checks"][0]
    transcript.set(
        "Manages gke-agentic/example-infra.",
        [_terminal(chained, at=1.0)],
        worker_commands=[{"task": "t_1", "command": f"{PREFLIGHT_LIST} + 1 command"}],
    )
    try:
        assert WorkerCommandsVerifier(**row_member).verify(5.0).status == "pass"
    finally:
        transcript.clear()
    assert _run_entry(DISCOVERY_CHECK, [chained], rows=[f"{PREFLIGHT_LIST} + 1 command"]) == "fail"

    multiline = "printenv GKE_PROJECT_ID\nkubectl get configmaps -n kubeagents-system"
    assert _run_entry(DISCOVERY_CHECK, [PREFLIGHT_LIST, multiline]) == "fail"


def test_web_and_mcp_discovery_allows_clean_run_and_rejects_web_search_and_mcp_reads():
    assert _run_entry(MCP_WEB_CHECK, [PREFLIGHT_LIST]) == "pass"

    web_call = {
        "name": "web_search",
        "args": {"query": '"gke-agentic/example-infra"'},
        "result": "{}",
        "status": "completed",
        "agent": "platform",
        "task": "t_1",
        "session": "s_1",
        "at": 2.0,
    }
    assert _run_entry(MCP_WEB_CHECK, [PREFLIGHT_LIST], extra_trajectory=[web_call]) == "fail"

    gke_mcp_call = {
        "name": "tool_call",
        "args": {"calls": [{"name": "mcp__gke__get_k8s_resource", "arguments": {"kind": "ConfigMap"}}]},
        "result": "{}",
        "status": "completed",
        "agent": "platform",
        "task": "t_1",
        "session": "s_1",
        "at": 2.0,
    }
    assert _run_entry(MCP_WEB_CHECK, [PREFLIGHT_LIST], extra_trajectory=[gke_mcp_call]) == "fail"

    dev_docs_mcp_call = {
        "name": "tool_call",
        "args": {"name": "mcp__developer_knowledge__search_documents", "arguments": {"query": "gitops"}},
        "result": "{}",
        "status": "completed",
        "agent": "platform",
        "task": "t_1",
        "session": "s_1",
        "at": 2.0,
    }
    assert _run_entry(MCP_WEB_CHECK, [PREFLIGHT_LIST], extra_trajectory=[dev_docs_mcp_call]) == "fail"


def test_discovery_violation_fails_gate_even_when_preflight_also_ran(tmp_path: Path):
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    entries, errors = parse_entries(doc["verification_spec"])
    assert not errors
    spec = cases.load_case(TASK)
    by_name = {e.name: e for e in entries}
    assert DISCOVERY_CHECK in spec.worker_blind_checks
    assert MCP_WEB_CHECK in spec.worker_blind_checks

    for failed_check in (DISCOVERY_CHECK, MCP_WEB_CHECK):
        report = [
            {"name": "the-request-was-delegated", "role": by_name["the-request-was-delegated"].role, "status": "pass", "reason": "ok"},
            {"name": "names-the-managed-repo", "role": by_name["names-the-managed-repo"].role, "status": "pass", "reason": "ok"},
            {"name": PREFLIGHT_CHECK, "role": by_name[PREFLIGHT_CHECK].role, "status": "pass", "reason": "ok"},
            {
                "name": DISCOVERY_CHECK,
                "role": by_name[DISCOVERY_CHECK].role,
                "status": "fail" if failed_check == DISCOVERY_CHECK else "pass",
                "reason": "violation" if failed_check == DISCOVERY_CHECK else "ok",
            },
            {
                "name": MCP_WEB_CHECK,
                "role": by_name[MCP_WEB_CHECK].role,
                "status": "fail" if failed_check == MCP_WEB_CHECK else "pass",
                "reason": "violation" if failed_check == MCP_WEB_CHECK else "ok",
            },
        ]
        rolled = rollup(report)
        assert rolled.correctness < 1.0
        run_dir = tmp_path / f"run_{failed_check}"
        run_dir.mkdir()
        (run_dir / "results.json").write_text(
            json.dumps(
                [
                    {
                        "status": "success",
                        "output": "Manages gke-agentic/example-infra.",
                        "latency": 1.5,
                        "tokens": {"total": 100},
                        "scores": {
                            "VerificationCorrectness": rolled.correctness,
                            "VerificationCoverage": 1.0,
                            "OutcomeValidity": 1.0,
                        },
                        "trajectory": [{"tool": "kanban_create"}],
                        "verification_report": report,
                        "verification_parse_errors": [],
                        "verification_status": "fail",
                    }
                ]
            ),
            encoding="utf-8",
        )
        rep = scoring.classify_rep(spec, run_dir, 1, correctness_floor=1.0)
        assert rep.outcome == "fail", rep
