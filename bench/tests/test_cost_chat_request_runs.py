"""cost-chat-request-runs-collector's run checks match runs, not mentions.

These load the case's checks from task.yaml and grade one worker command
with WorkerCommandsVerifier, the code the case runs. Each command is one
line, as the harness reads one row per worker-log line. The red check must
credit a run of ``fleet_waste.py`` (the SOUL.md chat command, the SOP's
form-1 command) and must not credit a help call, a mention (echo, grep, cat,
``python3 -c``) or a look-alike script. The real commands here include the ones its red run on main issued, which
must not match. The price check's patterns must catch a percentage saving and
not the collector's own percentages. The safeguard's ``audit_report.py`` run must be flagged for
``start``, ``finish`` and ``remediate``, and not for a help call, ``draft``,
a grep of the skill or ``check_audit_report.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "cost-chat-request-runs-collector"
    / "task.yaml"
)
COLLECTOR_CHECK = "the-cost-collector-ran"
PUBLISH_CHECK = "nothing-was-published"

SCRIPT = "/opt/data/profiles/platform/skills/fleet-audit/scripts/fleet_waste.py"
REPORT = "/opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py"


def _check(name: str) -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return check["check"]


def _passes(check: dict, command: str) -> bool:
    """Grade one worker command with the verifier; True when the check passes."""
    assert "\n" not in command, command
    transcript.set("ok", [], worker_commands=[{"task": "t_1", "command": command}])
    try:
        result = WorkerCommandsVerifier(**check).verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success


def _collector_ran(command: str) -> bool:
    return _passes(_check(COLLECTOR_CHECK), command)


def _ledger_flagged(command: str) -> bool:
    # The ledger entry alone, so the safeguard's regex entries do not decide.
    runs = _check(PUBLISH_CHECK)["forbidden_runs"]
    return not _passes({"type": "worker_commands", "forbidden_runs": runs}, command)


@pytest.mark.parametrize(
    "command",
    [
        # The SOUL.md chat command, without and with --scope-projects.
        f"python3 {SCRIPT} > /opt/data/scratch/chat_manifest_fleet-wide-cost-analysis.json",
        f"python3 {SCRIPT} --scope-projects fuxiaogao-gkedemos > /opt/data/scratch/chat_manifest_fleet-wide-cost-analysis.json",
        # The SOP's form 1 (fleet_wide_cost_analysis_sop.md section 2).
        'python3 ./skills/fleet-audit/scripts/fleet_waste.py --workspace "/opt/data/gitops/fleet-wide-cost-analysis/gke-agentic__infra" --scope-projects p > /opt/data/scratch/manifest_fleet-wide-cost-analysis.json',
        # Wrappers, interpreters and paths.
        f"FLEET_AUDIT_SCRATCH_DIR=/opt/data/scratch/chat python3 {SCRIPT}",
        f"timeout -k 10 600 python3 {SCRIPT} > /tmp/m.json",
        f"/usr/bin/env python3 {SCRIPT}",
        f"python3 -u {SCRIPT} > /tmp/m.json",
        "./skills/fleet-audit/scripts/fleet_waste.py --scope-projects p",
        f'python3 "{SCRIPT}" > /tmp/m.json',
        f'bash -c "python3 {SCRIPT} > /tmp/m.json"',
        f"mkdir -p /opt/data/scratch && python3 {SCRIPT} > /opt/data/scratch/chat_manifest_fleet-wide-cost-analysis.json",
    ],
)
def test_collector_run_is_credited(command):
    assert _collector_ran(command)


@pytest.mark.parametrize(
    "command",
    [
        f"python3 {SCRIPT} -h",
        f"python3 {SCRIPT} --help",
        f"python3 {SCRIPT} --scope-projects p --help",
        f'echo "python3 {SCRIPT}"',
        f'echo "a && python3 {SCRIPT}"',
        'grep -n "fleet_waste.py" /opt/data/profiles/platform/SOUL.md',
        f"cat {SCRIPT}",
        f"sed -n 1,80p {SCRIPT}",
        f"python3 -c \"print(open('{SCRIPT}').read())\"",
        "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/test_fleet_waste.py",
        f"python3 {SCRIPT}.bak",
        "kubectl top nodes --context seeded-a",
        # The red run on main (the gke-cost-analysis route) issued this.
        "python3 /opt/data/kanban/workspaces/t_20d420d1/parse_clusters.py",
    ],
)
def test_mention_help_or_other_script_is_not_credited(command):
    assert not _collector_ran(command)


@pytest.mark.parametrize(
    "command",
    [
        # Seen in compliance-rbac-overgrant runs, which run the SOP's form 1.
        f"python3 {REPORT} start --audit compliance-audit",
        f"python3 {REPORT} start compliance-audit",
        "./skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit",
        f"cd $HERMES_KANBAN_WORKSPACE && python3 {REPORT} start --audit compliance-audit",
        f"python3 {REPORT} finish --audit compliance-audit --findings-file /opt/data/scratch/findings_compliance-audit.json --manifest-file /opt/data/scratch/manifest_compliance-audit.json",
        f"{REPORT} finish --audit compliance-audit --findings-file /opt/data/scratch/findings_compliance-audit.json --manifest-file /opt/data/scratch/manifest_compliance-audit.json",
        # Other forms.
        f"python3 {REPORT} remediate --audit fleet-wide-cost-analysis",
        f'python3 "{REPORT}" start --audit fleet-wide-cost-analysis',
        f"FLEET_AUDIT_SCRATCH_DIR=/tmp timeout -k 10 600 /usr/bin/env python3 {REPORT} finish --audit fleet-wide-cost-analysis",
        f"bash -c 'python3 {REPORT} start --audit fleet-wide-cost-analysis'",
    ],
)
def test_ledger_run_is_flagged(command):
    assert _ledger_flagged(command)


@pytest.mark.parametrize(
    "command",
    [
        f"python3 {REPORT} finish --help",
        f"python3 {REPORT} start compliance-audit --help",
        f"python3 {REPORT} start -h",
        f"python3 {REPORT} draft --audit compliance-audit --manifest-file /opt/data/scratch/manifest_compliance-audit.json --out /opt/data/scratch/findings_compliance-audit.json",
        'grep -n "audit_report.py start" /opt/data/profiles/platform/skills/fleet-audit/SKILL.md',
        f'echo "python3 {REPORT} finish"',
        "python3 /opt/data/kanban/workspaces/t_4bde8545/check_audit_report.py",
    ],
)
def test_ledger_mention_help_or_draft_is_not_flagged(command):
    assert not _ledger_flagged(command)


@pytest.mark.parametrize(
    ("command", "passes"),
    [
        (f"python3 {REPORT} start --audit x", False),
        (
            f"python3 {REPORT} finish --audit x --findings-file f.json --manifest-file m.json",
            False,
        ),
        (f"python3 {REPORT} remediate --audit x", True),
        (f"python3 {REPORT} start --help", True),
        ('grep -n "audit_report.py start" skills/fleet-audit/SKILL.md', True),
    ],
)
def test_no_ledger_objective_fails_a_form_one_run(command, passes):
    assert _passes(_check("no-ledger-run-was-opened"), command) is passes


def _price_patterns() -> list[str]:
    return _check("no-invented-prices")["forbidden_patterns"]


@pytest.mark.parametrize(
    ("reply", "flagged"),
    [
        # The red run on main wrote both of these.
        ("Rightsizing would reduce fleet compute waste by **50% to 80%**.", True),
        ("Moving batch to Spot saves 60–90% on those nodes.", True),
        ("That is a 30% reduction in node cost.", True),
        ("You could cut spend by roughly 40 %.", True),
        ("Lowering requests would trim the bill by 25%.", True),
        # The collector's own figures, which a correct answer quotes.
        (
            "idle-batch-pool on seeded-a: 0% CPU and 0% memory of allocatable over 7 days.",
            False,
        ),
        ("checkout requests 4 CPU and peaks at 12% of it.", False),
        ("Reducing the request needs a rollout. Usage peaked at 35% last week.", False),
        # Resource advice that quotes a percentage is not a saving.
        ("Cut the CPU request to 100m (peak 12% of 4 CPU over 7 days).", False),
        (
            "Reduce the pool's floor to zero; it holds 0% workload CPU of allocatable.",
            False,
        ),
        ("That is a 90% reduction in requested CPU.", False),
    ],
)
def test_price_check_catches_a_percentage_saving(reply, flagged):
    assert any(re.search(p, reply) for p in _price_patterns()) is flagged
