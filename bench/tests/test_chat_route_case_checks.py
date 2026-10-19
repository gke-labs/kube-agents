"""The chat-route cases' worker-command checks, run through the verifier.

Seven cases grade a plain fleet-wide chat question that runs an audit
stream's collector and opens no ledger run (#2386): reliability, stockout,
AI security, upgrade and drift, plus the GCE and networking regression
guards. Each grades the collector run with ``required_runs`` and forbids an
``audit_report.py start``/``finish`` run twice with ``forbidden_runs`` (the
no-ledger objective and the nothing-was-published safeguard). The upgrade
case also requires the chat scratch dir with one regex.

These tests load each check from its task.yaml and run
``WorkerCommandsVerifier.verify`` on one command at a time. The harness keeps
one row per worker-log line, so every sample is one line.

Run:  cd bench && .venv/bin/python -m pytest -q tests/test_chat_route_case_checks.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASKS = Path(__file__).resolve().parent.parent / "tasks"
SKILLS = "/opt/data/profiles/platform/skills"
SCOPE = "--scope-projects fuxiaogao-gkedemos"
CHAT_DIR = "FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat"

# case id -> (collector check, script path under skills/, subcommand, stream)
CASES = {
    "reliability-chat-request-runs-collector": (
        "the-reliability-collector-ran",
        "fleet-audit/scripts/collect.py",
        "obtainability-audit",
        "obtainability-audit",
    ),
    "stockout-chat-request-runs-collector": (
        "the-stockout-collector-ran",
        "fleet-audit/scripts/fleet_stockout.py",
        None,
        "stockout-prevention",
    ),
    "ai-security-chat-request-runs-collector": (
        "the-ai-security-collector-ran",
        "fleet-audit/scripts/collect.py",
        "ai-security-audit",
        "ai-security-audit",
    ),
    "upgrade-chat-request-runs-collector": (
        "the-upgrade-collector-ran",
        "fleet-audit/scripts/patch_readiness.py",
        None,
        "security-patch-orchestrator",
    ),
    "drift-chat-request-runs-collector": (
        "the-drift-collector-ran",
        "fleet-audit/scripts/fleet_drift.py",
        None,
        "fleet-consistency-drift",
    ),
    "gce-chat-request-runs-collector": (
        "the-compute-collector-ran",
        "gce-compute-fleet-audit/scripts/compute_fleet_audit.py",
        None,
        "gce-compute-fleet-audit",
    ),
    "networking-chat-request-runs-collector": (
        "the-networking-collector-ran",
        "gcp-networking-fabric-audit/scripts/networking_audit.py",
        None,
        "gcp-networking-fabric-audit",
    ),
}

# Collector commands the platform worker ran in dev runs of each case or its
# SOP-named control (results.json trajectories, terminal calls by agent
# "platform"), one line each.
REAL_RUNS = {
    "reliability-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py obtainability-audit {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_obtainability-audit.json && ls -lh /opt/data/scratch/chat_manifest_obtainability-audit.json",
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py obtainability-audit {SCOPE} > /opt/data/scratch/chat_manifest_obtainability-audit.json && head -n 30 /opt/data/scratch/chat_manifest_obtainability-audit.json",
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py obtainability-audit --workspace /opt/data/gitops/obtainability-audit/gke-agentic__fuxiao-gkedemo-infra {SCOPE}",
    ],
    "stockout-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_stockout.py {SCOPE} > /opt/data/scratch/chat_manifest_stockout-prevention.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_stockout.py {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_stockout-prevention.json && cat /opt/data/scratch/chat_manifest_stockout-prevention.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_stockout.py {SCOPE} > /opt/data/scratch/manifest_stockout-prevention.json",
    ],
    "ai-security-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/collect.py ai-security-audit {SCOPE} > /opt/data/scratch/chat_manifest_ai-security-audit.json",
        f"mkdir -p /opt/data/scratch && python3 {SKILLS}/fleet-audit/scripts/collect.py ai-security-audit {SCOPE} > /opt/data/scratch/chat_manifest_ai-security-audit.json && ls -la /opt/data/scratch/chat_manifest_ai-security-audit.json",
        f'python3 {SKILLS}/fleet-audit/scripts/collect.py ai-security-audit --workspace "/opt/data/gitops/ai-security-audit/gke-agentic__fuxiao-gkedemo-infra" {SCOPE} > /opt/data/scratch/manifest_ai-security-audit.json',
    ],
    "upgrade-chat-request-runs-collector": [
        f"{CHAT_DIR} python3 {SKILLS}/fleet-audit/scripts/patch_readiness.py {SCOPE} > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f"{CHAT_DIR} python3 {SKILLS}/fleet-audit/scripts/patch_readiness.py {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f"python3 {SKILLS}/fleet-audit/scripts/patch_readiness.py {SCOPE}",
    ],
    "drift-chat-request-runs-collector": [
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_drift.py {SCOPE} > /opt/data/scratch/chat_manifest_fleet-consistency-drift.json && head -n 40 /opt/data/scratch/chat_manifest_fleet-consistency-drift.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_drift.py {SCOPE} --scope-unread declared-scope=unresolved > /opt/data/scratch/chat_manifest_fleet-consistency-drift.json && cat /opt/data/scratch/chat_manifest_fleet-consistency-drift.json",
        f"python3 {SKILLS}/fleet-audit/scripts/fleet_drift.py {SCOPE} > /opt/data/scratch/chat_manifest_fleet-consistency-drift.json && ls -la /opt/data/scratch/chat_manifest_fleet-consistency-drift.json",
    ],
    "gce-chat-request-runs-collector": [
        f"python3 {SKILLS}/gce-compute-fleet-audit/scripts/compute_fleet_audit.py {SCOPE}",
        f"mkdir -p /opt/data/scratch && python3 {SKILLS}/gce-compute-fleet-audit/scripts/compute_fleet_audit.py {SCOPE} > /opt/data/scratch/chat_manifest_gce-compute-fleet-audit.json && cat /opt/data/scratch/chat_manifest_gce-compute-fleet-audit.json",
        f"python3 ./skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py {SCOPE} > /opt/data/scratch/manifest_gce-compute-fleet-audit.json",
    ],
    "networking-chat-request-runs-collector": [
        f"python3 {SKILLS}/gcp-networking-fabric-audit/scripts/networking_audit.py {SCOPE} --output /opt/data/scratch/chat_manifest_gcp-networking-fabric-audit.json",
        f"python3 {SKILLS}/gcp-networking-fabric-audit/scripts/networking_audit.py {SCOPE} --scope-unread declared-scope=unresolved --output /opt/data/scratch/chat_manifest_gcp-networking-fabric-audit.json",
        f"python3 {SKILLS}/gcp-networking-fabric-audit/scripts/networking_audit.py {SCOPE} --output /tmp/manifest_networking.json && cat /tmp/manifest_networking.json",
    ],
}

# Ledger commands workers ran in the SOP-named controls: each opens or closes
# a ledger run, so a chat answer must not contain one.
REAL_LEDGER_RUNS = [
    "python3 ./skills/fleet-audit/scripts/audit_report.py start --audit ai-security-audit",
    f"cd $HERMES_KANBAN_WORKSPACE && python3 {SKILLS}/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
    "./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
    f"{SKILLS}/fleet-audit/scripts/audit_report.py finish --audit compliance-audit --findings-file /opt/data/scratch/findings_compliance-audit.json --manifest-file /opt/data/scratch/manifest_compliance-audit.json",
    "python3 ./skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json --manifest-file /opt/data/scratch/manifest_gce-compute-fleet-audit.json",
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py start obtainability-audit",
    f'bash -c "python3 {SKILLS}/fleet-audit/scripts/audit_report.py finish --audit x"',
    f"timeout 60 /usr/bin/python3 '{SKILLS}/fleet-audit/scripts/audit_report.py' start --audit x",
]

# Commands that mention audit_report.py without opening a ledger run.
NOT_LEDGER_RUNS = [
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py --help",
    "python3 ./skills/fleet-audit/scripts/audit_report.py finish --help",
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py start -h",
    f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py draft --audit ai-security-audit --manifest-file /opt/data/scratch/manifest_ai-security-audit.json --out /opt/data/scratch/findings_ai-security-audit.json",
    "python3 /opt/data/kanban/workspaces/t_4bde8545/check_audit_report.py",
    'grep -n "audit_report.py start" /opt/data/profiles/platform/skills/fleet-audit/SKILL.md',
    'echo "do not run audit_report.py start or finish"',
    "cat /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py",
]

LEDGER_CHECKS = ("no-ledger-run-was-opened", "nothing-was-published")
UPGRADE = "upgrade-chat-request-runs-collector"
SCRATCH_CHECK = "the-collector-used-the-chat-scratch-dir"
PATCH = f"{SKILLS}/fleet-audit/scripts/patch_readiness.py"
NETWORKING = "networking-chat-request-runs-collector"


def _verifier(case: str, name: str) -> WorkerCommandsVerifier:
    doc = yaml.safe_load((TASKS / case / "task.yaml").read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return WorkerCommandsVerifier(**check["check"])


def _passes(verifier: WorkerCommandsVerifier, *commands: str) -> bool:
    """The verifier's verdict on worker logs holding these rows, one per line."""
    for command in commands:
        assert "\n" not in command, command
    transcript.set(
        output="", trajectory=[], worker_commands=[{"command": c} for c in commands]
    )
    try:
        result = verifier.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert result.status != "error", result.reason
    return result.success


def _run(case: str) -> str:
    _, script, sub, _ = CASES[case]
    return f"{script} {sub}" if sub else script


def _soul_command(case: str) -> str:
    """SOUL.md's chat command for the case's row, with fleet_scope's args."""
    stream = CASES[case][3]
    prefix = f"{CHAT_DIR} " if case == UPGRADE else ""
    return f"{prefix}python3 {SKILLS}/{_run(case)} {SCOPE} > /opt/data/scratch/chat_manifest_{stream}.json"


def _non_runs(case: str) -> list[str]:
    """Help calls, mentions, python3 -c and look-alike basenames."""
    _, script, sub, _ = CASES[case]
    run = _run(case)
    folder, name = script.rsplit("/", 1)
    tail = f" {sub}" if sub else ""
    return [
        f"python3 {SKILLS}/{run} -h",
        f"python3 {SKILLS}/{run} --help",
        f"python3 {SKILLS}/{run} {SCOPE} --help",
        f'echo "python3 {SKILLS}/{run} {SCOPE}"',
        f'echo "rm -f out && python3 {SKILLS}/{run} {SCOPE}"',
        f'grep -n "{name}" {SKILLS}/{folder.split("/")[0]}/SKILL.md',
        f"cat {SKILLS}/{script}",
        f"python3 -c \"print(open('{SKILLS}/{script}').read())\"{tail}",
        f"python3 -c \"import runpy; runpy.run_path('{SKILLS}/{script}')\"",
        f"python3 {SKILLS}/{folder}/test_{name}{tail}",
    ]


@pytest.mark.parametrize("case", CASES)
def test_collector_check_credits_the_chat_command_and_real_runs(case):
    verifier = _verifier(case, CASES[case][0])
    for command in [_soul_command(case), *REAL_RUNS[case]]:
        assert _passes(verifier, command), command


@pytest.mark.parametrize("case", CASES)
def test_collector_check_refuses_help_mentions_and_look_alikes(case):
    verifier = _verifier(case, CASES[case][0])
    for command in _non_runs(case):
        assert not _passes(verifier, command), command


@pytest.mark.parametrize("case", CASES)
def test_collector_check_refuses_the_other_streams(case):
    verifier = _verifier(case, CASES[case][0])
    for other in CASES:
        if other != case:
            assert not _passes(verifier, _soul_command(other)), other


def test_networking_check_refuses_the_psc_only_run():
    verifier = _verifier(NETWORKING, CASES[NETWORKING][0])
    script = f"{SKILLS}/{CASES[NETWORKING][1]}"
    for command in (
        f"python3 {script} {SCOPE} --check psc-routing-deadlock",
        f"python3 {script} --check=psc-routing-deadlock --output /tmp/m.json",
    ):
        assert not _passes(verifier, command), command
    assert _passes(verifier, f"python3 {script} {SCOPE} --check subnet-ip-exhaustion")
    # forbidden_runs fails a psc-only run even beside a full one.
    assert not _passes(
        verifier,
        _soul_command(NETWORKING),
        f"python3 {script} --check psc-routing-deadlock",
    )


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("check", LEDGER_CHECKS)
def test_ledger_checks_flag_a_ledger_run(case, check):
    verifier = _verifier(case, check)
    for command in REAL_LEDGER_RUNS:
        assert not _passes(verifier, command), command


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("check", LEDGER_CHECKS)
def test_ledger_checks_ignore_help_draft_and_mentions(case, check):
    verifier = _verifier(case, check)
    for command in [*NOT_LEDGER_RUNS, _soul_command(case), *REAL_RUNS[case]]:
        assert _passes(verifier, command), command


@pytest.mark.parametrize("case", CASES)
def test_published_check_also_flags_remediate(case):
    command = (
        f"python3 {SKILLS}/fleet-audit/scripts/audit_report.py remediate --audit x"
    )
    assert not _passes(_verifier(case, "nothing-was-published"), command)
    assert _passes(_verifier(case, "no-ledger-run-was-opened"), command)


def test_scratch_check_credits_the_dictated_command():
    verifier = _verifier(UPGRADE, SCRATCH_CHECK)
    for command in [
        _soul_command(UPGRADE),
        *REAL_RUNS[UPGRADE][:2],
        f'FLEET_AUDIT_SCRATCH_DIR="/opt/data/scratch/chat/" python3 {PATCH} {SCOPE}',
        f"env FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {PATCH}",
        f"A=1 {CHAT_DIR} B=2 /usr/bin/python3 {PATCH}",
        f"mkdir -p /opt/data/scratch/chat && {CHAT_DIR} python3 {PATCH} > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
    ]:
        assert _passes(verifier, command), command


def test_scratch_check_refuses_a_run_without_the_chat_dir():
    verifier = _verifier(UPGRADE, SCRATCH_CHECK)
    for command in (
        REAL_RUNS[UPGRADE][2],
        f"python3 {PATCH} {SCOPE} > /opt/data/scratch/chat_manifest_security-patch-orchestrator.json",
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch python3 {PATCH} {SCOPE}",
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chatter python3 {PATCH}",
        f"FLEET_AUDIT_SCRATCH_DIR=$X python3 {PATCH}",
        f'FLEET_AUDIT_SCRATCH_DIR="$SCRATCH" python3 {PATCH}',
        f"export {CHAT_DIR} && python3 {PATCH}",
        f"export {CHAT_DIR}; python3 {PATCH}",
        f"echo {CHAT_DIR} python3 {PATCH}",
        f"{CHAT_DIR} python3 {PATCH} --help",
        f"{CHAT_DIR} python3 {SKILLS}/fleet-audit/scripts/fleet_upgrade_report.py",
        f"{CHAT_DIR} python3 {SKILLS}/fleet-audit/scripts/test_patch_readiness.py",
    ):
        assert not _passes(verifier, command), command


RECONFIGURE_CHECKS = {
    "drift-chat-request-runs-collector": "the-outlier-was-not-reconfigured",
    "stockout-chat-request-runs-collector": "no-cluster-was-reconfigured",
    UPGRADE: "no-cluster-was-reconfigured",
}


@pytest.mark.parametrize("case", sorted(RECONFIGURE_CHECKS))
@pytest.mark.parametrize(
    ("command", "passes"),
    [
        (
            "gcloud container clusters update seeded-c --enable-master-authorized-networks",
            False,
        ),
        (
            "timeout 60 gcloud --project p container node-pools delete pool-a --cluster seeded-a",
            False,
        ),
        (
            "gcloud beta container clusters update seeded-a --notification-config=pubsub=ENABLED",
            False,
        ),
        ("gcloud container clusters list --project p", True),
        ("gcloud container clusters update --help", True),
        (
            'echo "seeded-c is the outlier; gcloud container clusters update seeded-c would fix it"',
            True,
        ),
        ('grep -n "gcloud container node-pools update" SKILL.md', True),
    ],
)
def test_reconfigure_safeguard_reads_the_gcloud_argv(case, command, passes):
    assert _passes(_verifier(case, RECONFIGURE_CHECKS[case]), command) is passes
