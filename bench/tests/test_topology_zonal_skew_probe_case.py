"""topology-zonal-skew-probe's checks, run through the verifiers that grade them.

The four declared-line objectives are loaded from task.yaml and graded with
``ReportContainsVerifier`` on sample answers: the four lines as the prompt
dictates them, plain and as an agent renders them (a kind prefix, a
parenthetical on the name, a bullet, bold, a trailing Pending note), and the
misses: the wrong cause word, a running share whose numerator and
denominator disagree, an evidence field that names no mechanism, and a
line about another workload. ``leeway-report-ran`` is graded with
``WorkerCommandsVerifier`` on one tagged terminal call at a time: a run of
``leeway report`` by any path, behind an environment assignment, inside a
chain or ahead of a pipe, counts; a ``--help``, an echoed or grepped
mention, a read of the shipped example and another leeway verb do not.

Run:  cd bench && .venv/bin/python -m pytest -q tests/test_topology_zonal_skew_probe_case.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml

from kube_agents_bench import fleet, transcript
from kube_agents_bench.verifiers import ReportContainsVerifier, WorkerCommandsVerifier

from conftest import TASKS

CASE = "topology-zonal-skew-probe"
TASK = TASKS / CASE / "task.yaml"
CATALOG = TASKS.parent / "tf" / "fleet" / "fixtures.json"
SPEC = yaml.safe_load(TASK.read_text(encoding="utf-8"))
CARD = "t_1"

# Objective -> the line the prompt dictates for its workload, as a correct
# answer on the seeded fleet writes it.
LINES = {
    "report-attributes-scheduling-skew": (
        "zone-pinned-api: cause is scheduling; primary evidence: required nodeAffinity on "
        "topology.kubernetes.io/zone beside a ScheduleAnyway spread; blast-radius zone: us-central1-a; "
        "running there: 2/2"
    ),
    "report-attributes-volume-skew": (
        "zone-bound-store: cause is volume; primary evidence: PVCs on StorageClass seeded-zonal-pd, whose "
        "allowedTopologies names one zone; blast-radius zone: us-central1-a; running there: 2/2"
    ),
    "report-attributes-capacity-skew": (
        "capacity-starved-worker: cause is capacity; primary evidence: 3 replicas Pending with "
        "Insufficient cpu, the first zone's node is full; blast-radius zone: us-central1-b; "
        "running there: 1/1 (3 Pending)"
    ),
    "report-attributes-node-pool-skew": (
        "first-zone-sponge: cause is node-pool; primary evidence: nodeSelector targets default-pool, "
        "which has nodes in one zone only; blast-radius zone: us-central1-a; running there: 6/6"
    ),
}
# Each objective's cause word, and the three the forbidden half names.
CAUSES = {
    "report-attributes-scheduling-skew": "scheduling",
    "report-attributes-volume-skew": "volume",
    "report-attributes-capacity-skew": "capacity",
    "report-attributes-node-pool-skew": "node-pool",
}


def _check(name: str) -> dict:
    (entry,) = [c for c in SPEC["verification_spec"] if c.get("name") == name]
    return entry["check"]


def _fleet_dir(root: Path) -> None:
    """A runner-shaped directory with every catalogue slot reached: the
    objectives' ``fixture_roles`` resolve to slot d's credential."""
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    roles = catalog["roles"]
    (root / "clusters").mkdir(exist_ok=True)
    lines = ["project=kube-agents-evals"] + [f"slot.{role}={spec['cluster_slot']}" for role, spec in roles.items()]
    for slot in sorted({spec["cluster_slot"] for spec in roles.values()}):
        lines += [f"cluster.{slot}=seeded-{slot}", f"location.{slot}=us-central1"]
        (root / "clusters" / f"{slot}.kubeconfig").write_text("apiVersion: v1\n", encoding="utf-8")
    (root / ".fleet-context").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _grades(objective: str, text: str, tmp_path: Path) -> bool:
    verifier = ReportContainsVerifier(**_check(objective))
    _fleet_dir(tmp_path)
    transcript.set("", [], final_message=text)
    try:
        with mock.patch.dict(os.environ, {fleet.FLEET_KUBECONFIG_DIR_ENV: str(tmp_path)}):
            result = verifier.verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success


def _answer(**replace: str) -> str:
    """The four declared lines under a heading, one objective's line swapped."""
    lines = {**LINES, **replace}
    return "Summary:\n" + "\n".join(lines[o] for o in LINES)


@pytest.mark.parametrize("objective", sorted(LINES))
def test_the_declared_line_passes_its_objective(objective: str, tmp_path: Path) -> None:
    assert _grades(objective, _answer(), tmp_path)


@pytest.mark.parametrize(
    ("objective", "line"),
    [
        (
            "report-attributes-scheduling-skew",
            "zone-pinned-api: cause is scheduling; primary evidence: constraint_ignored, whenUnsatisfiable: ScheduleAnyway with maxSkew 1 and observed skew 2; blast-radius zone: europe-west10-a; running there: 2/2",
        ),
        (
            "report-attributes-volume-skew",
            "zone-bound-store: cause is volume; primary evidence: volume_pinning on seeded-zonal-pd; blast-radius zone: europe-west10-a; running there: 2/2",
        ),
        (
            "report-attributes-capacity-skew",
            "capacity-starved-worker: cause is capacity; primary evidence: domain_capacity_shortfall (3 Pending); blast-radius zone: europe-west10-b; running there: 1/1",
        ),
        (
            "report-attributes-node-pool-skew",
            "first-zone-sponge: cause is node-pool; primary evidence: single-zone node pool; blast-radius zone: europe-west10-a; running there: 6/6",
        ),
    ],
)
def test_leeway_cause_words_and_two_digit_regions_pass_their_objectives(
    objective: str, line: str, tmp_path: Path
) -> None:
    assert _grades(objective, _answer(**{objective: line}), tmp_path)


@pytest.mark.parametrize("objective", sorted(LINES))
@pytest.mark.parametrize(
    "render",
    [
        lambda line: f"- {line}",
        lambda line: f"**{line}**",
        lambda line: f"deployment/{line}" if not line.startswith("zone-bound-store") else f"statefulset/{line}",
        lambda line: line.replace(": cause is", " (seeded-topology): cause is", 1),
        lambda line: line + " -- no Pending replicas",
    ],
)
def test_the_declared_line_passes_as_an_agent_renders_it(objective: str, render, tmp_path: Path) -> None:
    assert _grades(objective, _answer(**{objective: render(LINES[objective])}), tmp_path)


@pytest.mark.parametrize("objective", sorted(LINES))
def test_the_other_three_causes_fail_the_objective(objective: str, tmp_path: Path) -> None:
    line = LINES[objective]
    for other in sorted(set(CAUSES.values()) - {CAUSES[objective]}):
        wrong = line.replace(f"cause is {CAUSES[objective]}", f"cause is {other}", 1)
        assert not _grades(objective, _answer(**{objective: wrong}), tmp_path), wrong


@pytest.mark.parametrize("objective", sorted(LINES))
def test_a_partial_share_or_an_empty_evidence_field_fails_the_objective(objective: str, tmp_path: Path) -> None:
    line = LINES[objective]
    share = line[line.rindex("running there: ") + len("running there: ") :].split()[0]
    n, m = share.split("/")
    partial = line.replace(f"running there: {n}/{m}", f"running there: {n}/{int(m) + 1}", 1)
    assert not _grades(objective, _answer(**{objective: partial}), tmp_path), partial
    head, _, tail = line.partition("primary evidence: ")
    rest = tail[tail.index("; blast-radius zone:") :]
    vague = f"{head}primary evidence: see above{rest}"
    assert not _grades(objective, _answer(**{objective: vague}), tmp_path), vague


def test_a_line_about_another_workload_does_not_stand_in(tmp_path: Path) -> None:
    # The node-pool objective wants first-zone-sponge's own line; the other
    # three lines, however right, do not carry it.
    three = {o: LINES[o] for o in LINES if o != "report-attributes-node-pool-skew"}
    text = "Summary:\n" + "\n".join(three.values())
    assert not _grades("report-attributes-node-pool-skew", text, tmp_path)


# ---------------------------------------------------------------- leeway-report-ran


def _call(command: str, at: float) -> dict:
    """One tagged terminal call of the worker card, as worker_trajectory records it."""
    return {
        "name": "terminal",
        "args": {"command": command},
        "result": "",
        "status": "completed",
        "agent": "platform",
        "task": CARD,
        "session": "s_1",
        "at": at,
    }


def _ran(*commands: str) -> bool:
    """The check's verdict on a card whose tagged calls read ``commands``,
    each also on the card log as itself (one line, no chain)."""
    verifier = WorkerCommandsVerifier(**_check("leeway-report-ran"))
    rows = [{"task": CARD, "command": c} for c in commands if "\n" not in c and "&&" not in c]
    transcript.set("", [_call(c, 100.0 + i) for i, c in enumerate(commands)], worker_commands=rows)
    try:
        result = verifier.verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success


@pytest.mark.parametrize(
    "command",
    [
        "leeway report --clusters kube-agents-evals/us-central1/seeded-d --namespace seeded-topology --output json",
        "/usr/local/bin/leeway report -n seeded-topology -o json",
        "KUBECONFIG=/opt/data/kube/config leeway report -n seeded-topology -o json | jq '.findings[]'",
        "cd /opt/data/scratch && kubectl get nodes -o wide && leeway report -n seeded-topology -o json > report.json",
        "timeout 300 leeway report --namespace seeded-topology --output json 2>&1 | tee /opt/data/scratch/leeway.json",
        "leeway report -n seeded-topology -o json\nleeway report -n seeded-topology -o table",
    ],
)
def test_a_leeway_report_run_is_credited(command: str) -> None:
    assert _ran("kubectl get pods -n seeded-topology -o wide", command)


@pytest.mark.parametrize(
    "command",
    [
        "leeway report --help",
        "leeway -h",
        'echo "leeway report -n seeded-topology -o json"',
        'grep -n "leeway report" /opt/data/profiles/platform/skills/topology-drift/SKILL.md',
        "cat /opt/data/profiles/platform/skills/topology-drift/references/report-example.json",
        "leeway rank -n seeded-topology -o json",
        "leeway version",
        "kubectl get pods -n seeded-topology -o wide",
    ],
)
def test_a_mention_a_help_call_or_another_verb_is_not_credited(command: str) -> None:
    assert not _ran("kubectl get nodes -o wide", command)


def test_no_card_is_an_error_not_a_pass() -> None:
    # The front door filed nothing: no tagged call and no card log. The check
    # cannot observe its subject, so it errors rather than failing or passing.
    verifier = WorkerCommandsVerifier(**_check("leeway-report-ran"))
    transcript.set("", [{"name": "kanban_create", "args": {}, "result": "", "status": "completed"}])
    try:
        result = verifier.verify(5.0)
    finally:
        transcript.clear()
    assert result.status == "error", result.reason
