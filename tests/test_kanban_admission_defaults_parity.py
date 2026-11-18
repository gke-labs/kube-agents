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

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/platformagent_manifests.go"
PATCH = REPO_ROOT / "deploy/docker/patches/kanban_admission.py"

# (operator name, patch name, parser for the literal on each side).
TERMS = (
    ("defaultAdmissionWorkerRssCapMiB", "DEFAULT_WORKER_RSS_CAP_MIB", int),
    ("defaultAdmissionQueueBoundSeconds", "DEFAULT_QUEUE_BOUND_SECONDS", int),
    ("defaultAdmissionFailClosed", "DEFAULT_FAIL_CLOSED", str),
)
GO_BOOLS = {"true": "True", "false": "False"}


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


if __name__ == "__main__":
    unittest.main()
