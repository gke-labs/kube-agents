"""The cluster-agent-security-audit-script case's patterns, read from its task.yaml.

The route check requires the audit run from the path the Cluster Agent's skill
names and forbids a result that shows the script missing; the report check
requires the script's result words. The shapes below are the commands, results
and report lines recorded on main and on the fix, plus the spellings review
walked. The verifier's own behaviour (windows, lineages) is pinned in
test_verifiers.py.

Run:  cd bench && uv run python -m pytest -q tests/test_cluster_agent_security_audit_patterns.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from kube_agents_bench.verifiers import ReportContainsVerifier, WorkerCommandsVerifier, _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-security-audit-script" / "task.yaml"
PROFILE_SKILL = "/opt/data/profiles/cluster-p-seeded-a-us-central1-a/skills/gke-workload-security/scripts/audit_cluster.sh"


def _check(name):
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


ROUTE = WorkerCommandsVerifier(**_check("the-audit-script-ran-from-the-skill-path"))
REPORT = ReportContainsVerifier(**_check("report-carries-the-script-results"))


def runs_from_the_skill_path(command: str) -> bool:
    return all(re.search(p, command) for p in ROUTE.required_patterns)


def shows_it_missing(output: str) -> bool:
    stored = json.dumps({"output": output, "exit_code": 127})
    return any(re.search(p, stored) for p in ROUTE.forbidden_result_patterns)


@pytest.mark.parametrize(
    "command",
    [
        "/opt/data/scripts/audit_cluster.sh seeded-a us-central1-a p",
        '/opt/data/scripts/audit_cluster.sh "seeded-a" us-central1-a p',
        "bash /opt/data/scripts/audit_cluster.sh seeded-a us-central1-a p",
        "which gcloud jq && /opt/data/scripts/audit_cluster.sh seeded-a us-central1-a p",
        "cd /opt/data/scripts && bash audit_cluster.sh seeded-a us-central1-a p",
        "cd /opt/data/scripts; ./audit_cluster.sh seeded-a us-central1-a p",
        "cd /opt/data/scripts\n./audit_cluster.sh seeded-a us-central1-a p",
    ],
)
def test_a_run_from_the_skill_path_counts(command):
    assert runs_from_the_skill_path(command)


@pytest.mark.parametrize(
    "command",
    [
        # Recorded on main: the copies a search found, and the profile's skill path.
        "bash /opt/defaults/skills/gke-workload-security/scripts/audit_cluster.sh seeded-a us-central1-a p",
        "bash /opt/data/skills/gke-workload-security/scripts/audit_cluster.sh seeded-a us-central1-a p",
        f"bash {PROFILE_SKILL} seeded-a us-central1-a p",
        "./scripts/audit_cluster.sh seeded-a us-central1-a p",
        # A search is not a run.
        "find / -name audit_cluster.sh 2>/dev/null",
        "find /opt/data -type f 2>/dev/null | grep audit_cluster",
        # A cd into the staged directory does not credit a copy named elsewhere.
        "cd /opt/data/scripts && bash /opt/defaults/skills/gke-workload-security/scripts/audit_cluster.sh seeded-a r p",
        # Another cluster is not the one asked for.
        "/opt/data/scripts/audit_cluster.sh seeded-b us-central1-a p",
    ],
)
def test_any_other_run_does_not_count(command):
    assert not runs_from_the_skill_path(command)


@pytest.mark.parametrize(
    "output",
    [
        f"bash: {PROFILE_SKILL}: No such file or directory\n",
        "bash: line 1: ./scripts/audit_cluster.sh: No such file or directory\n",
        "bash: audit_cluster.sh: command not found\n",
        "ls: cannot access '/opt/data/scripts/audit_cluster.sh': No such file or directory\n",
    ],
)
def test_a_missing_script_result_is_seen(output):
    assert shows_it_missing(output)


def test_the_script_output_is_not_a_missing_script():
    assert not shows_it_missing("Auditing Cluster: seeded-a in us-central1-a (Project: p)\n[PASS] Workload Identity is ENABLED\n")


@pytest.mark.parametrize(
    "line",
    [
        "- [PASS] Workload Identity is ENABLED",
        "Shielded Nodes: failed",
        "Binary Authorization: warning",
        "**FAIL** Network Policy",
        "Workload Identity passes; Shielded Nodes fails; Binary Authorization warns",
        "one failure",
    ],
)
def test_the_result_words_count(line):
    assert re.search(REPORT.any_of_patterns[0], _normalize_lines(line))


def test_a_password_is_not_a_result_word():
    assert not re.search(REPORT.any_of_patterns[0], _normalize_lines("the password was rotated"))
