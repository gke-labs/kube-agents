r"""The cluster-agent-kcc-audit-collector case's checks, read from its task.yaml (#3142).

The route check pairs `required_runs` (which uses the verifier's argv/wrapper
reader so reading the script or passing `--help` does not count) with
`required_patterns` matching the staged path or `cd /opt/data/scripts` outside
`#` comments without duplicating `_SHELL_EXEC_WRAPPERS`. Until #3967 lands
`WorkerRun.script_path`, cross-segment `cp`/`cd` + `/tmp` and variable-held path
shapes remain deferred as strict `xfail` cases. It also forbids a later turn's
result showing `kcc_fleet_audit.py` missing. The report check requires Config
Connector / `cnrm.cloud.google.com` and the installation or synchronization
summary.

Run:  cd bench && .venv/bin/python -m pytest -q tests/test_cluster_agent_kcc_audit_patterns.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier, WorkerCommandsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-kcc-audit-collector" / "task.yaml"
PREFLIGHT_CMD = "bash /opt/data/scripts/cluster_preflight.sh --json"
PREFLIGHT_OK = json.dumps({"output": '{"status": "ok"}\n', "exit_code": 0})
COLLECTOR_OK = json.dumps(
    {
        "output": (
            "## Config Connector (KCC) Fleet Audit\n\n"
            "- **kcc_installed:** `false`\n"
            "- **Config Connector Status:** NOT INSTALLED on this cluster (0 `*.cnrm.cloud.google.com` CRDs).\n"
            "CLEAN_AUDIT_STOP: YES\n"
        ),
        "exit_code": 0,
    }
)


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


ROUTE = WorkerCommandsVerifier(**_check("the-kcc-collector-ran-from-the-staged-path"))
REPORT = ReportContainsVerifier(**_check("report-carries-the-kcc-summary"))


def _call(command: str, result: str, at: float) -> dict:
    return {
        "name": "terminal",
        "args": {"command": command},
        "result": result,
        "status": "completed",
        "agent": "cluster-p-seeded-a-us-central1-a",
        "task": "t_1",
        "session": "s_1",
        "at": at,
    }


def _route_verdict(calls: list[tuple[str, str, float]]) -> bool:
    transcript.set(
        output="",
        trajectory=[_call(cmd, res, at) for cmd, res, at in calls],
        worker_commands=[{"task": "t_1", "command": cmd.splitlines()[0]} for cmd, _, _ in calls],
    )
    try:
        verdict = ROUTE.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    return verdict.success


def runs_from_the_staged_path(*commands: str) -> bool:
    calls = [(PREFLIGHT_CMD, PREFLIGHT_OK, 100.0)]
    for idx, cmd in enumerate(commands, start=1):
        calls.append((cmd, COLLECTOR_OK, 100.0 + idx))
    return _route_verdict(calls)


@pytest.mark.parametrize(
    "command",
    [
        "python3 /opt/data/scripts/kcc_fleet_audit.py",
        "python /opt/data/scripts/kcc_fleet_audit.py --kubeconfig /opt/data/kubeconfig",
        "/opt/data/scripts/kcc_fleet_audit.py",
        "KUBECONFIG=/opt/data/kubeconfig python3 /opt/data/scripts/kcc_fleet_audit.py -A",
        "timeout 120 python3 /opt/data/scripts/kcc_fleet_audit.py",
        'bash -c "python3 /opt/data/scripts/kcc_fleet_audit.py"',
        "cd /opt/data/scripts && python3 ./kcc_fleet_audit.py",
        "cd /opt/data/scripts && python3 kcc_fleet_audit.py",
        'python3 "/opt/data/scripts/kcc_fleet_audit.py"',
        "python3 '/opt/data/scripts/kcc_fleet_audit.py'",
        r'bash -c "python3 \"/opt/data/scripts/kcc_fleet_audit.py\""',
    ],
)
def test_a_run_from_the_staged_path_counts(command: str) -> None:
    assert runs_from_the_staged_path(command)


@pytest.mark.parametrize(
    "commands",
    [
        ("kubectl get crd | grep cnrm",),
        ("kubectl get sqlinstances.sql.cnrm.cloud.google.com -A",),
        ("python3 /tmp/kcc_fleet_audit.py",),
        ("python3 ./opt/data/scripts/kcc_fleet_audit.py",),
        ("python3 /tmp/kcc_fleet_audit.py # staged at /opt/data/scripts/kcc_fleet_audit.py",),
        ("cat /opt/data/scripts/kcc_fleet_audit.py",),
        ("python3 /opt/data/scripts/kcc_fleet_audit.py --help",),
        ("python3 /opt/data/scripts/kcc_fleet_audit.py -h",),
    ],
)
def test_reads_help_flags_and_other_paths_do_not_count(commands: tuple[str, ...]) -> None:
    assert not runs_from_the_staged_path(*commands)


@pytest.mark.xfail(strict=True, reason="#3967: requires WorkerRun.script_path")
@pytest.mark.parametrize(
    "commands",
    [
        ("cp /opt/data/scripts/kcc_fleet_audit.py /tmp/ && python3 /tmp/kcc_fleet_audit.py",),
        ("cat /opt/data/scripts/kcc_fleet_audit.py", "python3 /tmp/kcc_fleet_audit.py"),
    ],
)
def test_cross_segment_copy_or_two_command_tmp_run_rejected_once_script_path_lands(
    commands: tuple[str, ...],
) -> None:
    assert not runs_from_the_staged_path(*commands)


@pytest.mark.parametrize(
    "output",
    [
        "python3: can't open file '/opt/data/scripts/kcc_fleet_audit.py': [Errno 2] No such file or directory\n",
        "bash: ./kcc_fleet_audit.py: No such file or directory\n",
        "bash: kcc_fleet_audit.py: command not found\n",
    ],
)
def test_missing_script_result_in_later_turn_fails_window_check(output: str) -> None:
    missing = json.dumps({"output": output, "exit_code": 2})
    assert not _route_verdict(
        [
            (PREFLIGHT_CMD, PREFLIGHT_OK, 100.0),
            ("python3 /opt/data/scripts/kcc_fleet_audit.py", missing, 101.0),
        ]
    )


def _report_verdict(text: str) -> bool:
    transcript.set(output=text, trajectory=[])
    try:
        verdict = REPORT.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    return verdict.success


@pytest.mark.parametrize(
    "report",
    [
        "**Config Connector is not installed on `seeded-a`** (0 `*.cnrm.cloud.google.com` CRDs found).",
        "Audited `cnrm.cloud.google.com` CustomResourceDefinitions on `seeded-a`: no CRDs are installed.",
        "**Config Connector on `seeded-a` is healthy:** 12 managed resources Ready and synchronized.",
    ],
)
def test_report_naming_kcc_and_status_passes(report: str) -> None:
    assert _report_verdict(report)


@pytest.mark.parametrize(
    "report",
    [
        "All workloads on `seeded-a` are Ready.",
        "Checked Config Connector on `seeded-a`.",
        "Failed to audit Config Connector synchronization status on `seeded-a`: collector timed out.",
        "Could not read Config Connector CustomResourceDefinitions on `seeded-a`.",
        "Config Connector Audit Verdict: Incomplete (CRD listing failed).",
    ],
)
def test_report_missing_kcc_or_status_fails(report: str) -> None:
    assert not _report_verdict(report)
