"""gitops-audit.py: isolation counts from a run record's worker trajectory."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hack" / "gitops-audit.py"
_spec = importlib.util.spec_from_loader("gitops_audit", importlib.machinery.SourceFileLoader("gitops_audit", str(_SCRIPT)))
gitops_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gitops_audit)


def _worker(name, args, at, agent="platform"):
    return {"name": name, "args": args, "result": "", "status": "ok", "agent": agent, "task": "t_1", "session": "s_1", "at": at}


def test_counts_reads_and_lookups_before_the_fix_only():
    record = {
        "trajectory": [
            {"name": "kanban_create", "args": {}, "result": "t_1", "status": "ok"},
            _worker("terminal", '{"command": "kubectl get pods -n payments"}', 100),
            _worker("mcp__gke__describe", '{"kind": "deployment"}', 110, agent="cluster-x"),
            _worker("terminal", '{"command": "gh pr list --repo o/r"}', 120),
            _worker("skill_view", '{"name": "submit-suggestion"}', 105),
            _worker("kanban_show", '{"task_id": "t_1"}', 106),
            _worker("kanban_show", '{"task_id": "t_other"}', 107),
            _worker("terminal", '{"command": "kubectl --kubeconfig=/p/k.yaml -n payments describe rs checkout-1"}', 108, agent="cluster-x"),
            _worker("kanban_show", '{}', 109, agent="cluster-x"),
            _worker("terminal", '{"command": "python3 submit_suggestion.py submit --help"}', 112),
            _worker("terminal", '{"command": "python3 submit_suggestion.py prepare --repo o/r"}', 125),
            _worker("terminal", '{"command": "python3 \\"$S/submit_suggestion.py\\" submit \\\\\n --handle h"}', 130),
            _worker("terminal", '{"command": "gh pr view 7"}', 140),
            _worker("terminal", '{"command": "kubectl get pods"}', 150),
            {"name": "gitops_fix_cycle", "args": {}, "result": {"outcome": "merged", "pr_url": "u", "merged_at": "2026-09-18T00:00:00Z"}, "status": "harness"},
        ]
    }
    report = gitops_audit.audit(record)
    assert report["worker_entries"] == 13
    assert report["delegated_to_cluster_agent"] is True
    assert report["cluster_reads_before_fix"] == 3
    assert report["repo_lookups_before_fix"] == 2
    assert report["repo_lookups"] == ['terminal {"command": "gh pr list --repo o/r"}', 'kanban_show {"task_id": "t_other"}']
    assert report["gitops_outcome"] == "merged"


def test_the_fix_time_is_a_terminal_submit_not_prose_about_one():
    record = {
        "trajectory": [
            _worker("terminal", {"command": "kubectl get pods"}, 100, agent="cluster-x"),
            _worker("write_file", {"path": "/tmp/notes.md", "content": "next: git push the fix and gh pr create"}, 110),
            _worker("kanban_heartbeat", {"note": "about to git push"}, 115),
            _worker("terminal", {"command": "kubectl get rs"}, 120, agent="cluster-x"),
            _worker("terminal", {"command": 'python3 "$S/submit_suggestion.py" submit --handle h'}, 130),
            {"name": "gitops_fix_cycle", "args": {}, "result": {"outcome": "merged", "merged_at": "2026-09-18T00:00:00Z"}, "status": "harness"},
        ]
    }
    report = gitops_audit.audit(record)
    assert report["fix_submitted_at"] == "1970-01-01T00:02:10+00:00"
    assert report["cluster_reads_before_fix"] == 2


def test_falls_back_to_the_merge_time_without_a_submit_call():
    record = {
        "trajectory": [
            _worker("terminal", '{"command": "kubectl get pods"}', 1_758_153_600),
            {"name": "gitops_fix_cycle", "args": {}, "result": {"outcome": "merged", "merged_at": "2026-09-18T00:00:00Z"}, "status": "harness"},
        ]
    }
    report = gitops_audit.audit(record)
    assert report["fix_submitted_at"] == "2026-09-18T00:00:00+00:00"
    assert report["cluster_reads_before_fix"] == 1


def test_record_without_worker_entries():
    report = gitops_audit.audit({"trajectory": [{"name": "kanban_create", "args": {}, "result": "", "status": "ok"}]})
    assert report["worker_entries"] == 0
    assert report["fix_submitted_at"] is None
    assert report["cluster_reads_before_fix"] == 0


def test_reads_are_invocations_not_mentions():
    record = {
        "trajectory": [
            # A wrapper around kubectl, defined and then used: one read.
            _worker("terminal", {"command": 'KC=/p/k.yaml\nk() { kubectl --kubeconfig "$KC" "$@"; }\necho "=== pods"; k get pods -n storefront'}, 100, agent="cluster-x"),
            # The same through a variable holding the command: one read.
            _worker("terminal", {"command": 'K="kubectl --kubeconfig=$KC -n payments"\n$K get rs -o wide'}, 101, agent="cluster-x"),
            # The preflight script run, and the same script only read: one read.
            _worker("terminal", {"command": "HERMES_HOME=/p bash /opt/data/scripts/cluster_preflight.sh --json"}, 102, agent="cluster-x"),
            _worker("terminal", {"command": "head -60 /opt/data/scripts/cluster_preflight.sh"}, 103, agent="cluster-x"),
            # Environment reads and kubeconfig bookkeeping are not cluster reads.
            _worker("terminal", {"command": "printenv GKE_PROJECT_ID GKE_LOCATION GKE_CLUSTER_NAME"}, 104),
            _worker("terminal", {"command": "export KUBECONFIG=/p/k.yaml; kubectl config current-context"}, 105, agent="cluster-x"),
            # A read the sandbox refused ran nothing.
            {**_worker("terminal", {"command": "export KUBECONFIG=/p/k.yaml; bash /opt/data/scripts/cluster_preflight.sh"}, 105.5, agent="cluster-x"),
             "status": "error", "result": '{"output": "", "exit_code": -1, "error": "BLOCKED: Security scan", "status": "blocked"}'},
            # Prose that quotes kubectl: a PR body, a card result, a tool description.
            _worker("write_file", {"path": "/tmp/pr.md", "content": "Ran `$ kubectl get deploy` and saw 0/2"}, 106),
            _worker("kanban_complete", {"summary": "kubectl get deploy shows shelfview at 0 replicas"}, 107, agent="cluster-x"),
            _worker("tool_describe", {"name": "mcp__gke__get_k8s_resource"}, 108, agent="cluster-x"),
            # MCP cluster tools, named and with the name clipped: two reads.
            _worker("tool_call", {"name": "mcp__gke__list_k8s_events", "arguments": {"parent": "projects/p/locations/l/clusters/c"}}, 109, agent="cluster-x"),
            _worker("tool_call", {"arguments": {"resourceType": "pod", "parent": "projects/p/locations/l/clusters/c"}}, 110, agent="cluster-x"),
            _worker("tool_call", {"name": "mcp__developer_knowledge__answer_query", "arguments": {"query": "kubectl get"}}, 111, agent="cluster-x"),
            _worker("terminal", {"command": 'python3 "$S/submit_suggestion.py" submit --handle h'}, 120),
            {"name": "gitops_fix_cycle", "args": {}, "result": {"outcome": "merged", "merged_at": "2026-09-18T00:00:00Z"}, "status": "harness"},
        ]
    }
    report = gitops_audit.audit(record)
    assert report["cluster_reads_before_fix"] == 5

