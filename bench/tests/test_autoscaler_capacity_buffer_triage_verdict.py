"""autoscaler-capacity-buffer-triage-probe grades two labeled lines of the reply.

- ``COMMAND:`` names ``kubectl get capacitybuffers.autoscaling.x-k8s.io -A``
  (or ``capacitybuffers.v1beta1.autoscaling.x-k8s.io`` / ``--all-namespaces``),
  rejecting the wrong API group (``autoscaling.gke.io``), invalid
  ``resource.group/version`` syntax, and mutating verbs.
- ``CONDITIONS:`` binds ``ReadyForProvisioning=False``,
  ``LimitedByQuotas=True``, and ``Provisioning=False`` tightly to their own
  boolean values (rejecting inverted booleans) and names ``NotTriggerScaleUp``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "autoscaler-capacity-buffer-triage-probe"
    / "task.yaml"
)
COMMAND_CHECK = "command-lists-capacitybuffers-fqn"
CONDITIONS_CHECK = "conditions-name-v1beta1-triad-and-starvation-event"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(check_name: str, report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == check_name]
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**check["check"]).verify(TIMEOUT_SEC).success


def test_expected_output_passes_both_objectives():
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    expected = doc["expected_output"]
    assert _passes(COMMAND_CHECK, expected)
    assert _passes(CONDITIONS_CHECK, expected)


def test_prompt_echo_fails_both_objectives():
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    prompt = doc["prompt"]
    assert not _passes(COMMAND_CHECK, prompt)
    assert not _passes(CONDITIONS_CHECK, prompt)


def test_command_objective_accepts_valid_read_only_spellings():
    for report in (
        "COMMAND: kubectl get capacitybuffers.autoscaling.x-k8s.io -A",
        "COMMAND: kubectl get capacitybuffers.v1beta1.autoscaling.x-k8s.io -A",
        "COMMAND: kubectl get capacitybuffer.v1beta1.autoscaling.x-k8s.io --all-namespaces",
        "**COMMAND:** `kubectl get capacitybuffers.autoscaling.x-k8s.io -A`",
        "- COMMAND: kubectl get capacitybuffers.autoscaling.x-k8s.io --all-namespaces",
        "COMMAND: kubectl get -A capacitybuffers.autoscaling.x-k8s.io -o wide",
        "COMMAND: kubectl get capacitybuffers.autoscaling.x-k8s.io -A --chunk-size=500 -o json",
    ):
        assert _passes(COMMAND_CHECK, report), report


def test_command_objective_rejects_wrong_group_invalid_version_missing_all_ns_or_writes():
    for report in (
        "COMMAND: kubectl get capacitybuffers.autoscaling.gke.io -A",
        "COMMAND: kubectl get capacitybuffers.autoscaling.x-k8s.io/v1beta1 -A",
        "COMMAND: kubectl get capacitybuffers -A",
        "COMMAND: kubectl get capacitybuffers.autoscaling.x-k8s.io -n serving",
        "COMMAND: kubectl delete capacitybuffers.autoscaling.x-k8s.io -A",
        "COMMAND: kubectl get capacitybuffers.autoscaling.x-k8s.io -A?",
        "kubectl get capacitybuffers.autoscaling.x-k8s.io -A",
        "",
    ):
        assert not _passes(COMMAND_CHECK, report), report


def test_conditions_objective_accepts_valid_triage_spellings():
    for report in (
        "CONDITIONS: ReadyForProvisioning=False for template/config error; LimitedByQuotas=True for quota cap; Provisioning=False for fake-pod failure; NotTriggerScaleUp events on the CapacityBuffer.",
        "Healthy buffers stay `Provisioning=True`.\n**CONDITIONS:** `ReadyForProvisioning` is `False`; `LimitedByQuotas` is `True`; `Provisioning` is `False`; `NotTriggerScaleUp` events.",
        "Healthy buffers stay Provisioning=True.\n- CONDITIONS: ReadyForProvisioning: False, LimitedByQuotas: True, Provisioning: False, and NotTriggerScaleUp events.",
        "CONDITIONS: ReadyForProvisioning (False) / LimitedByQuotas (True) / Provisioning (False) / NotTriggerScaleUp",
    ):
        assert _passes(CONDITIONS_CHECK, report), report


def test_conditions_objective_rejects_inverted_booleans_and_incomplete_replies():
    for report in (
        # Inverted ReadyForProvisioning=True (must not cross into Provisioning=False)
        "CONDITIONS: ReadyForProvisioning=True, LimitedByQuotas=True, Provisioning=False, NotTriggerScaleUp",
        "CONDITIONS: ReadyForProvisioning=True and Provisioning=False; LimitedByQuotas=True; NotTriggerScaleUp",
        # Inverted LimitedByQuotas=False (must not cross into Provisioning=True)
        "CONDITIONS: ReadyForProvisioning=False, LimitedByQuotas=False, Provisioning=False, NotTriggerScaleUp",
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=False and Provisioning=True; Provisioning=False; NotTriggerScaleUp",
        # Inverted Provisioning=True (no Provisioning=False)
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=True; NotTriggerScaleUp",
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=True (Ready=False); NotTriggerScaleUp",
        # Hedged both booleans on one line
        "CONDITIONS: ReadyForProvisioning=True or ReadyForProvisioning=False; LimitedByQuotas=False or LimitedByQuotas=True; Provisioning=True or Provisioning=False; NotTriggerScaleUp",
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=True or Provisioning=False; NotTriggerScaleUp",
        # Negated boolean (word, contraction, !=, or leading negation)
        "CONDITIONS: ReadyForProvisioning is not False; LimitedByQuotas=True; Provisioning=False; NotTriggerScaleUp",
        "CONDITIONS: ReadyForProvisioning!=False; LimitedByQuotas!=True; Provisioning!=False; NotTriggerScaleUp",
        "CONDITIONS: ReadyForProvisioning isn't False; LimitedByQuotas=True; Provisioning=False; NotTriggerScaleUp",
        "CONDITIONS: not ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=False; no NotTriggerScaleUp",
        "CONDITIONS: never ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=False; NotTriggerScaleUp",
        # Wrong v1alpha1 Ready=False claim
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=False; Ready=False; NotTriggerScaleUp",
        # Unbound condition before another condition
        "CONDITIONS: ReadyForProvisioning and Provisioning=False; LimitedByQuotas=True; NotTriggerScaleUp",
        # Missing ReadyForProvisioning
        "CONDITIONS: LimitedByQuotas=True; Provisioning=False; NotTriggerScaleUp",
        # Missing LimitedByQuotas
        "CONDITIONS: ReadyForProvisioning=False; Provisioning=False; NotTriggerScaleUp",
        # Missing Provisioning=False
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; NotTriggerScaleUp",
        # Missing NotTriggerScaleUp
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=False",
        # Hedged with a question mark
        "CONDITIONS: ReadyForProvisioning=False; LimitedByQuotas=True; Provisioning=False; NotTriggerScaleUp?",
        # Only in prose, no CONDITIONS: line
        "ReadyForProvisioning=False, LimitedByQuotas=True, Provisioning=False, NotTriggerScaleUp",
        "",
    ):
        assert not _passes(CONDITIONS_CHECK, report), report
