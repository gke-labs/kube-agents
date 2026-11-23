"""The dispatcher's admission defaults, held equal across the operator and the patch.

    python3 -m unittest tests.test_kanban_admission_defaults_parity

The operator renders ``kanban.admission`` into the managed scope on every
install (``resolveKanbanAdmission`` in
``k8s-operator/internal/controller/platformagent_manifests.go``), so its
constants are the numbers in force. The patch
(``deploy/docker/patches/kanban_admission.py``) keeps a copy for a key that is
missing or malformed and for a tick whose config read fails, so a copy that
drifted would act silently on exactly those ticks. Nothing but this test holds
the two equal. The files are read as text: the Go side is not importable and
the patch is written for the image's Python.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/platformagent_manifests.go"
MANIFEST_HELPERS = REPO_ROOT / "k8s-operator/internal/controller/manifest_helpers.go"
COMMON_TYPES = REPO_ROOT / "k8s-operator/api/v1alpha1/common_types.go"
PATCH = REPO_ROOT / "deploy/docker/patches/kanban_admission.py"
CHART_CR_TEMPLATE = REPO_ROOT / "charts/kube-agents/templates/platform-agent-cr.yaml"
VALUES_SCHEMA = REPO_ROOT / "charts/kube-agents/values.schema.json"
UPGRADE_SH = REPO_ROOT / "upgrade.sh"

# (operator name, patch name, parser for the literal on each side).
TERMS = (
    ("defaultAdmissionWorkerRssCapMiB", "DEFAULT_WORKER_RSS_CAP_MIB", int),
    ("defaultAdmissionQueueBoundSeconds", "DEFAULT_QUEUE_BOUND_SECONDS", int),
    ("defaultAdmissionFailClosed", "DEFAULT_FAIL_CLOSED", str),
)
GO_BOOLS = {"true": "True", "false": "False"}
MIB_PER_GIB = 1024


def _resolve_default_memory_budget_mib() -> int:
    text = MANIFEST_HELPERS.read_text(encoding="utf-8")
    func_text = text[text.index("func resolveResources(") :]
    req_block = func_text[func_text.index("Requests:") : func_text.index("Limits:")]
    raw_gi = _literal(r'corev1\.ResourceMemory:\s*resource\.MustParse\("(\d+)Gi"\)', req_block, MANIFEST_HELPERS)
    return int(raw_gi) * MIB_PER_GIB


def _literal(pattern: str, text: str, where: pathlib.Path) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    if found is None:
        raise AssertionError(f"{where.relative_to(REPO_ROOT)} declares nothing matching {pattern!r}")
    return found.group(1)


def operator_terms() -> dict[str, object]:
    text = OPERATOR.read_text(encoding="utf-8")
    terms: dict[str, object] = {}
    for go_name, _, parse in TERMS:
        raw = _literal(rf"^\s*{go_name}\s*=\s*(\w+)\s*$", text, OPERATOR)
        terms[go_name] = parse(GO_BOOLS.get(raw, raw))
    return terms


def patch_terms() -> dict[str, object]:
    text = PATCH.read_text(encoding="utf-8")
    terms: dict[str, object] = {}
    for _, py_name, parse in TERMS:
        terms[py_name] = parse(_literal(rf"^{py_name} = (\w+)$", text, PATCH))
    return terms


class AdmissionDefaultsParityTest(unittest.TestCase):
    def test_the_patch_mirrors_the_operators_defaults(self):
        operator, patch = operator_terms(), patch_terms()
        for go_name, py_name, _ in TERMS:
            with self.subTest(term=go_name):
                self.assertEqual(
                    patch[py_name], operator[go_name],
                    f"{py_name} in the patch and {go_name} in the operator disagree",
                )

    def test_chart_and_upgrader_mirror_the_operators_defaults(self):
        operator = operator_terms()
        op_raw = OPERATOR.read_text(encoding="utf-8")
        floor_mib = int(_literal(r"^\s*admissionMemoryBudgetFloorMiB\s*=\s*(\d+)\s*$", op_raw, OPERATOR))
        default_budget_mib = _resolve_default_memory_budget_mib()

        chart = CHART_CR_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(int(_literal(r"\$defaultBudgetMiB := (\d+)", chart, CHART_CR_TEMPLATE)), default_budget_mib)
        self.assertEqual(int(_literal(r"\$defaultRssCapMiB := (\d+)", chart, CHART_CR_TEMPLATE)), operator["defaultAdmissionWorkerRssCapMiB"])

        upgrade = UPGRADE_SH.read_text(encoding="utf-8")
        self.assertEqual(int(_literal(r"^readonly ADMISSION_DEFAULT_MEMORY_BUDGET_MIB=(\d+)$", upgrade, UPGRADE_SH)), default_budget_mib)
        self.assertEqual(int(_literal(r"^readonly ADMISSION_DEFAULT_WORKER_RSS_CAP_MIB=(\d+)$", upgrade, UPGRADE_SH)), operator["defaultAdmissionWorkerRssCapMiB"])
        self.assertEqual(int(_literal(r"^readonly ADMISSION_MEMORY_BUDGET_FLOOR_MIB=(\d+)$", upgrade, UPGRADE_SH)), floor_mib)

        schema = json.loads(VALUES_SCHEMA.read_text(encoding="utf-8"))
        schema_budget = schema["properties"]["platformAgent"]["properties"]["harness"]["properties"]["tuning"]["properties"]["admission"]["properties"]["memoryBudgetMiB"]
        self.assertEqual(schema_budget["minimum"], floor_mib)

        types_text = COMMON_TYPES.read_text(encoding="utf-8")
        admission_block = types_text[types_text.index("type AdmissionSpec struct {") :]
        admission_block = admission_block[: admission_block.index("\n}\n")]
        self.assertEqual(int(_literal(r"\+kubebuilder:validation:Minimum=(\d+)\s*\n\s*//\s*\+kubebuilder:validation:Maximum=\d+\s*\n\s*//\s*\+optional\s*\n\s*MemoryBudgetMiB", admission_block, COMMON_TYPES)), floor_mib)


if __name__ == "__main__":
    unittest.main()
