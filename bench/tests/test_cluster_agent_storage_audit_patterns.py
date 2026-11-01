r"""The cluster-agent-storage-audit-collector case's checks, read from its task.yaml.

The route check pairs `required_runs` (which uses the verifier's argv/wrapper
reader so reading the script or passing `--help` does not count) with
`required_patterns` matching the staged path or `cd /opt/data/scripts` outside
`#` comments without duplicating `_SHELL_EXEC_WRAPPERS`. Until #3967 lands
`WorkerRun.script_path` after #3348, cross-segment `cp`/`cd` + `/tmp`, bare
`cd /opt/data/scripts && audit_storage.py`, same-segment argument mention
(`python3 /tmp/audit_storage.py /opt/data/scripts/audit_storage.py`),
two-command mention-plus-`/tmp`, and parent-directory / redirected-`cd` /
`cd --` / `cd ... || exit` / variable-held path or directory / double-slash
`//opt` / escaped-slash `\/opt` / `#` inside single-quoted
`bash -c 'echo "# x"; ...'` / `pushd` (`cd /opt/data && ...`,
`cd /opt/data/scripts 2>/dev/null && ...`, `cd -- /opt/data/scripts && ...`,
`cd /opt/data/scripts || exit 1; ...`,
`SCRIPTS=/opt/data/scripts; python3 $SCRIPTS/audit_storage.py`,
`SCRIPT=/opt/data/scripts/audit_storage.py; python3 "$SCRIPT"`,
`python3 //opt/data/scripts/audit_storage.py`,
`python3 \/opt/data/scripts/audit_storage.py`,
`bash -c 'echo "# x"; python3 /opt/data/scripts/audit_storage.py'`,
`pushd /opt/data/scripts && ...`) shapes remain deferred and are pinned below as
strict `xfail` cases. It also
forbids a later turn's result showing
the script missing (inside the window opened by the worker's first non-empty
result, normally `cluster_preflight.sh`). The report check requires both
storage kinds (PVCs and StorageClasses, CamelCase, spaced, or hyphenated) the
collector's summary names.

Run:  cd bench && uv run python -m pytest -q tests/test_cluster_agent_storage_audit_patterns.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier, WorkerCommandsVerifier, _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-storage-audit-collector" / "task.yaml"
PREFLIGHT_CMD = "bash /opt/data/scripts/cluster_preflight.sh --json"
PREFLIGHT_OK = json.dumps({"output": '{"status": "ok"}\n', "exit_code": 0})
COLLECTOR_OK = json.dumps(
    {
        "output": "## Kubernetes Storage Fleet Audit\n\n- **StorageClasses:** 3 available (Default: `standard-rwo`)\n",
        "exit_code": 0,
    }
)


def _check(name):
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


ROUTE = WorkerCommandsVerifier(**_check("the-storage-collector-ran-from-the-staged-path"))
REPORT = ReportContainsVerifier(**_check("report-carries-the-storage-summary"))


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
        "python3 /opt/data/scripts/audit_storage.py",
        "python /opt/data/scripts/audit_storage.py --kubeconfig /opt/data/kubeconfig",
        "/opt/data/scripts/audit_storage.py",
        "KUBECONFIG=/opt/data/kubeconfig python3 /opt/data/scripts/audit_storage.py",
        "timeout 120 python3 /opt/data/scripts/audit_storage.py",
        "nohup python3 /opt/data/scripts/audit_storage.py",
        "time python3 /opt/data/scripts/audit_storage.py",
        "python3 -u /opt/data/scripts/audit_storage.py",
        'bash -c "python3 /opt/data/scripts/audit_storage.py"',
        "cd /opt/data/scripts && python3 ./audit_storage.py",
        "cd /opt/data/scripts && python3 audit_storage.py",
        "cd /opt/data/scripts && KUBECONFIG=/opt/data/kubeconfig python3 audit_storage.py",
        "cd /opt/data/scripts && timeout 120 python3 audit_storage.py",
        "cd /opt/data/scripts && env KUBECONFIG=/opt/data/kubeconfig timeout -k 5 120 python3 ./audit_storage.py",
        "cd /opt/data/scripts && nohup python3 -u audit_storage.py",
        "cd /opt/data/scripts && stdbuf -oL python3 audit_storage.py",
        "cd /opt/data/scripts && exec python3 audit_storage.py",
        "cd /opt/data/scripts && /usr/bin/env python3 audit_storage.py",
        "cd /opt/data/scripts && python3 -m audit_storage",
        "cd /opt/data/scripts # enter staged dir\npython3 audit_storage.py",
        "cd /opt/data/scripts; ./audit_storage.py",
        "OUT=/tmp/r#1 python3 /opt/data/scripts/audit_storage.py",
        "python3 /opt/data/scripts/audit_storage.py # run staged collector",
        'echo "# storage audit" && python3 /opt/data/scripts/audit_storage.py',
        '(cd /opt/data/scripts && python3 ./audit_storage.py)',
        'bash -c "cd /opt/data/scripts && python3 audit_storage.py"',
        'python3 "/opt/data/scripts/audit_storage.py"',
        "python3 '/opt/data/scripts/audit_storage.py'",
        r'bash -c "python3 \"/opt/data/scripts/audit_storage.py\""',
        'cd "/opt/data/scripts" && python3 audit_storage.py',
        "cd '/opt/data/scripts' && python3 audit_storage.py",
        'cd "/opt/data/scripts/" && python3 ./audit_storage.py',
        "cd /opt/data/scripts \\\n&& python3 audit_storage.py",
        "cd \\\n/opt/data/scripts && python3 audit_storage.py",
    ],
)
def test_a_run_from_the_staged_path_counts(command):
    assert runs_from_the_staged_path(command)


@pytest.mark.parametrize(
    "commands",
    [
        ("kubectl get pvc -A",),
        ("kubectl describe pvc data -n prod",),
        ("find / -name audit_storage.py 2>/dev/null",),
        ("python3 /tmp/audit_storage.py",),
        ("python3 /mnt/opt/data/scripts/audit_storage.py",),
        ("python3 ./opt/data/scripts/audit_storage.py",),
        ("python3 ~/opt/data/scripts/audit_storage.py",),
        ("python3 ${PWD}/opt/data/scripts/audit_storage.py",),
        ("python3 $(pwd)/opt/data/scripts/audit_storage.py",),
        ("python3 `pwd`/opt/data/scripts/audit_storage.py",),
        ("python3 /mnt/*/opt/data/scripts/audit_storage.py",),
        ("python3 /mnt/?/opt/data/scripts/audit_storage.py",),
        ("python3 /mnt/[a]/opt/data/scripts/audit_storage.py",),
        (r"python3 /mnt/a\ /opt/data/scripts/audit_storage.py",),
        (r"python3 /mnt/a\;/opt/data/scripts/audit_storage.py",),
        (r"python3 /mnt/a\'/opt/data/scripts/audit_storage.py",),
        (r'python3 /mnt/a\"/opt/data/scripts/audit_storage.py',),
        ("python3 /mnt/a'/opt/data/scripts/audit_storage.py'",),
        ('python3 /mnt/a"/opt/data/scripts/audit_storage.py"',),
        ("python3 /mnt/a''/opt/data/scripts/audit_storage.py",),
        ('python3 /mnt/a""/opt/data/scripts/audit_storage.py',),
        ('python3 "/mnt/a"\'/opt/data/scripts/audit_storage.py\'',),
        ("python3 /mnt/a\u00a0/opt/data/scripts/audit_storage.py",),
        ('python3 "$PWD"/opt/data/scripts/audit_storage.py',),
        ("SCRIPT=/opt/data/scripts/audit_storage.py; python3 /tmp/audit_storage.py",),
        ("SCRIPT=/opt/data/scripts/audit_storage.py",),
        ('SCRIPT="/opt/data/scripts/audit_storage.py"; python3 /tmp/audit_storage.py',),
        ('SCRIPT="/opt/data/scripts/audit_storage.py"',),
        ("SCRIPT='/opt/data/scripts/audit_storage.py'; python3 /tmp/audit_storage.py",),
        (r'bash -c "SCRIPT=\"/opt/data/scripts/audit_storage.py\"; python3 /tmp/audit_storage.py"',),
        ("python3 /tmp/audit_storage.py # staged at /opt/data/scripts/audit_storage.py",),
        ("# staged at /opt/data/scripts/audit_storage.py\npython3 /tmp/audit_storage.py",),
        ('bash -c "python3 /tmp/audit_storage.py # /opt/data/scripts/audit_storage.py"',),
        ('bash -c "# /opt/data/scripts/audit_storage.py\npython3 /tmp/audit_storage.py"',),
        ("cd /opt/data/scripts_other && python3 /tmp/audit_storage.py",),
        ("cd /opt/data/scripts#other && python3 /tmp/audit_storage.py",),
        ("cd \"/opt/data/scripts' && python3 /tmp/audit_storage.py",),
        ("(cd /opt/data/scripts); python3 audit_storage.py",),
        ("cd /opt/data/scripts | python3 audit_storage.py",),
        ("cd /opt/data/scripts || python3 audit_storage.py",),
        ("cd /tmp && python3 -m audit_storage",),
        ("cat /opt/data/scripts/audit_storage.py",),
        ("head -40 /opt/data/scripts/audit_storage.py",),
        ("grep CLEAN_AUDIT_STOP /opt/data/scripts/audit_storage.py",),
        ("python3 /opt/data/scripts/audit_storage.py --help",),
        ("python3 /opt/data/scripts/audit_storage.py -h",),
        ("python3 -c \"print(open('/opt/data/scripts/audit_storage.py').read())\"",),
    ],
)
def test_reads_help_flags_and_other_paths_do_not_count(commands):
    assert not runs_from_the_staged_path(*commands)


@pytest.mark.xfail(strict=True, reason="#3967: requires WorkerRun.script_path")
@pytest.mark.parametrize(
    "commands",
    [
        ("cp /opt/data/scripts/audit_storage.py /tmp/ && python3 /tmp/audit_storage.py",),
        ("cd /opt/data/scripts && python3 /tmp/audit_storage.py",),
        ("cd /opt/data/scripts && audit_storage.py",),
        ("python3 /tmp/audit_storage.py /opt/data/scripts/audit_storage.py",),
        ("cat /opt/data/scripts/audit_storage.py", "python3 /tmp/audit_storage.py"),
    ],
)
def test_cross_segment_copy_or_two_command_tmp_run_rejected_once_script_path_lands(commands):
    assert not runs_from_the_staged_path(*commands)


@pytest.mark.xfail(strict=True, reason="#3967: requires WorkerRun.script_path")
@pytest.mark.parametrize(
    "command",
    [
        "cd /opt/data && python3 scripts/audit_storage.py",
        "cd /opt/data/scripts 2>/dev/null && python3 audit_storage.py",
        "cd -- /opt/data/scripts && python3 audit_storage.py",
        "cd /opt/data/scripts || exit 1; python3 audit_storage.py",
        "SCRIPTS=/opt/data/scripts; python3 $SCRIPTS/audit_storage.py",
        'SCRIPT=/opt/data/scripts/audit_storage.py; python3 "$SCRIPT"',
        "python3 //opt/data/scripts/audit_storage.py",
        r"python3 \/opt/data/scripts/audit_storage.py",
        'bash -c \'echo "# x"; python3 /opt/data/scripts/audit_storage.py\'',
        "pushd /opt/data/scripts && python3 audit_storage.py",
    ],
)
def test_parent_dir_cd_relative_run_accepted_once_script_path_lands(command):
    assert runs_from_the_staged_path(command)


@pytest.mark.parametrize(
    "output",
    [
        "python3: can't open file '/opt/data/scripts/audit_storage.py': [Errno 2] No such file or directory\n",
        "bash: ./audit_storage.py: No such file or directory\n",
        "bash: audit_storage.py: command not found\n",
    ],
)
def test_missing_script_result_in_later_turn_fails_window_check(output):
    missing = json.dumps({"output": output, "exit_code": 2})
    assert not _route_verdict(
        [
            (PREFLIGHT_CMD, PREFLIGHT_OK, 100.0),
            ("python3 /opt/data/scripts/audit_storage.py", missing, 101.0),
        ]
    )


def test_first_turn_opener_is_outside_the_forbidden_result_window():
    missing = json.dumps(
        {
            "output": "python3: can't open file '/opt/data/scripts/audit_storage.py': [Errno 2] No such file or directory\n",
            "exit_code": 2,
        }
    )
    assert _route_verdict([("python3 /opt/data/scripts/audit_storage.py", missing, 100.0)])


def test_the_collector_output_after_preflight_passes():
    assert _route_verdict(
        [
            (PREFLIGHT_CMD, PREFLIGHT_OK, 100.0),
            ("python3 /opt/data/scripts/audit_storage.py", COLLECTOR_OK, 101.0),
        ]
    )


@pytest.mark.parametrize(
    "line",
    [
        "- **PersistentVolumeClaims (PVCs):** 0 total",
        "2 PVCs are Bound",
        "one PersistentVolumeClaim is Pending",
        "All persistent volume claims are Bound",
        "one persistent-volume-claim is Pending",
    ],
)
def test_the_pvc_words_count(line):
    assert re.search(REPORT.required_patterns[0], _normalize_lines(line))


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
        "**All persistent storage on seeded-a is healthy.** 1 PVC Bound; 10 StorageClasses available (default standard-rwo).",
        "PersistentVolumeClaims: 0 total. Default Storage Class: standard-rwo.",
        "All persistent volume claims are Bound; 10 storage classes available (default standard-rwo).",
        "All persistent-volume-claims are Bound; 10 storage-classes available (default standard-rwo).",
    ],
)
def test_report_naming_both_pvcs_and_storage_classes_passes(report):
    assert _report_verdict(report)


@pytest.mark.parametrize(
    "report",
    [
        "3 PVCs Bound, nothing unhealthy.",
        "10 StorageClasses available (Default: standard-rwo), nothing unhealthy.",
    ],
)
def test_report_naming_only_one_storage_kind_fails(report):
    assert not _report_verdict(report)
