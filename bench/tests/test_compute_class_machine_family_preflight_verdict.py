"""compute-class-machine-family-preflight-probe pins verdict contrast and YAML/HCL refusal.

The report check requires ``VERDICT: INCOMPATIBLE`` alongside the four rule IDs
and ``hyperdisk-balanced``, while using ``fold_decoration: true`` and
line-anchored ``forbidden_patterns`` so that:

- prose contrasting verdicts ("returned VERDICT: INCOMPATIBLE rather than
  VERDICT: COMPATIBLE") or echoing the skill wording passes, while a standalone
  ``VERDICT: COMPATIBLE`` line fails;
- prose noting that ComputeClass/NodePool manifests were withheld passes, while
  emitting ``kind: ComputeClass``, ``kind: ContainerNodePool``, or a Terraform
  ``resource "google_container_node_pool"`` block after ``VERDICT: INCOMPATIBLE``
  fails.
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
    / "compute-class-machine-family-preflight-probe"
    / "task.yaml"
)
CHECK = "the-report-flags-n1-to-n4-security-disk-and-local-ssd-blockers"
TIMEOUT_SEC = 5.0

VALID_REPORT = (
    "VERDICT: INCOMPATIBLE\n\n"
    "Blockers:\n"
    "- `disk-type-pd-on-hyperdisk-only`: switch boot disk to `hyperdisk-balanced`.\n"
    "- `local-ssd-unsupported-on-n4`: remove Local SSDs before moving to `n4`.\n\n"
    "Warnings:\n"
    "- `shielded-vtpm-disabled`\n"
    "- `shielded-secure-boot-disabled`\n\n"
    "Stopped before generating any ComputeClass or NodePool manifest.\n"
)


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (entry,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    assert entry["check"]["type"] == "report_contains"
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**entry["check"]).verify(TIMEOUT_SEC).success


def test_valid_incompatible_report_and_prose_verdict_contrasts_pass():
    for report in (
        VALID_REPORT,
        # Contrasting the two verdicts in prose must not false-fail.
        VALID_REPORT
        + "\nThe validator returned `VERDICT: INCOMPATIBLE` rather than `VERDICT: COMPATIBLE`.\n",
        # Echoing the skill wording that sets both verdicts side by side passes.
        VALID_REPORT
        + "\nPer the skill, report the validator's verdict (`VERDICT: COMPATIBLE` or `VERDICT: INCOMPATIBLE`).\n",
        # A negated compatible verdict line with trailing context or a cross passes.
        VALID_REPORT + "\nVERDICT: COMPATIBLE was not reached.\n",
        VALID_REPORT + "\nVERDICT: COMPATIBLE \u274c\n",
        # Prose mentioning refused manifest types without emitting a YAML/HCL block passes.
        VALID_REPORT
        + "\nI refused to generate `kind: ComputeClass`, `kind: ContainerNodePool`, "
        'or `resource "google_container_node_pool"` manifests until the blockers are fixed.\n',
        VALID_REPORT
        + "\n- `kind: ComputeClass` YAML was withheld because two blockers remain.\n",
        VALID_REPORT
        + '\n- `resource "google_container_node_pool"` was not generated.\n',
    ):
        assert _passes(report), report


def test_standalone_compatible_verdict_line_fails():
    for wrong_line in (
        "VERDICT: COMPATIBLE",
        "**VERDICT: COMPATIBLE**",
        "`VERDICT: COMPATIBLE`",
        "> VERDICT: COMPATIBLE",
        "- VERDICT: COMPATIBLE",
        "1. VERDICT: COMPATIBLE.",
        "### VERDICT: COMPATIBLE",
        "VERDICT: COMPATIBLE \u2705",
    ):
        report = f"{VALID_REPORT}\n{wrong_line}\n"
        assert not _passes(report), report


def test_generated_compute_class_or_node_pool_yaml_or_hcl_fails():
    for manifest in (
        "```yaml\napiVersion: cloud.google.com/v1\nkind: ComputeClass\nmetadata:\n  name: n4-class\n```",
        "```yaml\napiVersion: container.cnrm.cloud.google.com/v1beta1\nkind: ContainerNodePool\nmetadata:\n  name: n4-pool\n```",
        '```yaml\nkind: "ComputeClass"\nmetadata:\n  name: n4-class\n```',
        "```yaml\n  - kind: ComputeClass\n    metadata:\n      name: n4-class\n```",
        "```yaml\nkind: ComputeClass  # proposed manifest\n```",
        '```json\n{\n  "apiVersion": "cloud.google.com/v1",\n  "kind": "ComputeClass"\n}\n```',
        '```hcl\nresource "google_container_node_pool" "n4_pool" {\n  name = "n4-pool"\n}\n```',
        '  resource "google_container_node_pool" "primary" {',
    ):
        report = f"{VALID_REPORT}\n{manifest}\n"
        assert not _passes(report), report
