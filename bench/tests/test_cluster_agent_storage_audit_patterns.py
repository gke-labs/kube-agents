"""The cluster-agent-storage-audit-collector case's patterns, read from its task.yaml.

The route check requires the storage collector run from the path SOUL.md names
and forbids a result that shows the script missing; the report check requires
the storage kinds the collector's summary names.

Run:  cd bench && uv run python -m pytest -q tests/test_cluster_agent_storage_audit_patterns.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from kube_agents_bench.verifiers import ReportContainsVerifier, WorkerCommandsVerifier, _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-storage-audit-collector" / "task.yaml"


def _check(name):
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


ROUTE = WorkerCommandsVerifier(**_check("the-storage-collector-ran-from-the-staged-path"))
REPORT = ReportContainsVerifier(**_check("report-carries-the-storage-summary"))


def runs_from_the_staged_path(command: str) -> bool:
    return all(re.search(p, command) for p in ROUTE.required_patterns)


def shows_it_missing(output: str) -> bool:
    stored = json.dumps({"output": output, "exit_code": 2})
    return any(re.search(p, stored) for p in ROUTE.forbidden_result_patterns)


@pytest.mark.parametrize(
    "command",
    [
        "python3 /opt/data/scripts/audit_storage.py",
        "python /opt/data/scripts/audit_storage.py --kubeconfig /opt/data/kubeconfig",
        "cd /opt/data/scripts && python3 audit_storage.py",
        "cd /opt/data/scripts; ./audit_storage.py",
    ],
)
def test_a_run_from_the_staged_path_counts(command):
    assert runs_from_the_staged_path(command)


@pytest.mark.parametrize(
    "command",
    [
        "kubectl get pvc -A",
        "kubectl describe pvc data -n prod",
        "find / -name audit_storage.py 2>/dev/null",
        "python3 /tmp/audit_storage.py",
        "cd /opt/data/scripts && python3 /tmp/audit_storage.py",
    ],
)
def test_any_other_run_does_not_count(command):
    assert not runs_from_the_staged_path(command)


@pytest.mark.parametrize(
    "output",
    [
        "python3: can't open file '/opt/data/scripts/audit_storage.py': [Errno 2] No such file or directory\n",
        "bash: ./audit_storage.py: No such file or directory\n",
        "bash: audit_storage.py: command not found\n",
    ],
)
def test_a_missing_script_result_is_seen(output):
    assert shows_it_missing(output)


def test_the_collector_output_is_not_a_missing_script():
    assert not shows_it_missing("## Kubernetes Storage Fleet Audit\n\n- **StorageClasses:** 3 available (Default: `standard-rwo`)\n")


@pytest.mark.parametrize("line", ["- **PersistentVolumeClaims (PVCs):** 0 total", "2 PVCs are Bound", "one PersistentVolumeClaim is Pending"])
def test_the_pvc_words_count(line):
    assert re.search(REPORT.any_of_patterns[0], _normalize_lines(line))
