"""The `no-pdb` recipe's generator writes exactly the bytes the fleet audit wrote before it moved.

The expected texts below were captured from `audit_report._pdb_manifest` before
the generator moved into `fix_recipes/no-pdb/fix.py`. They are literals on
purpose: the broker verifies a pull request by re-running the generator and
comparing bytes (`automerge_eligibility.verify_added_manifest`), so any
change to this output is a change to what auto-merge accepts, and has to show
up here as an edited expectation.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from fix_recipes import registry

FIX = registry.generator("no-pdb")

PINNED = [
    (
        {
            "cluster": "prod-us",
            "object": "Deployment/checkout",
            "namespace": "shop",
            "pod_selector": {"matchLabels": {"app": "checkout", "tier": "web"}},
        },
        "no-pdb.3f9a2c",
        "checkout-pdb",
        (
            "# prod-us: no-pdb on Deployment/checkout (no-pdb.3f9a2c)\n"
            "apiVersion: policy/v1\n"
            "kind: PodDisruptionBudget\n"
            "metadata:\n"
            "  name: checkout-pdb\n"
            "  namespace: shop\n"
            "spec:\n"
            "  maxUnavailable: 1\n"
            '  selector: {"matchLabels": {"app": "checkout", "tier": "web"}}\n'
        ),
    ),
    (
        {
            "cluster": "",
            "object": "StatefulSet/db",
            "namespace": "data",
            "pod_selector": {
                "matchExpressions": [{"key": "app", "operator": "In", "values": ["db", "db-replica"]}],
                "matchLabels": {"z": "1", "a": "é"},
            },
        },
        "no-pdb.f-1",
        "db-pdb",
        (
            "# : no-pdb on StatefulSet/db (no-pdb.f-1)\n"
            "apiVersion: policy/v1\n"
            "kind: PodDisruptionBudget\n"
            "metadata:\n"
            "  name: db-pdb\n"
            "  namespace: data\n"
            "spec:\n"
            "  maxUnavailable: 1\n"
            '  selector: {"matchExpressions": [{"key": "app", "operator": "In", "values": ["db", "db-replica"]}], '
            '"matchLabels": {"a": "\\u00e9", "z": "1"}}\n'
        ),
    ),
    (
        # No `cluster` key at all: the header still has its ": " prefix.
        {"object": "Deployment/x", "namespace": "ns", "pod_selector": {"matchLabels": {"k": "v"}}},
        "id",
        "x-pdb",
        (
            "# : no-pdb on Deployment/x (id)\n"
            "apiVersion: policy/v1\n"
            "kind: PodDisruptionBudget\n"
            "metadata:\n"
            "  name: x-pdb\n"
            "  namespace: ns\n"
            "spec:\n"
            "  maxUnavailable: 1\n"
            '  selector: {"matchLabels": {"k": "v"}}\n'
        ),
    ),
]

REFUSALS = [
    ({}, "the collector attached no selector to write it from"),
    ({"pod_selector": {"foo": {}}}, "the collector's selector has keys `policy/v1` does not take"),
    ({"pod_selector": {"matchLabels": {}}}, "the collector's selector is empty, and would select every pod in the namespace"),
    (
        {"pod_selector": {"matchLabels": {"a": "b"}}, "object": "Deployment/Bad_Name", "namespace": "ns"},
        "its name would not be a valid Kubernetes name",
    ),
    (
        {"pod_selector": {"matchLabels": {"a": "b"}}, "object": "Deployment/ok", "namespace": ""},
        "the collector named no valid namespace for it",
    ),
]


class NoPdbGenerate(unittest.TestCase):
    def test_output_is_pinned_byte_for_byte(self):
        for candidate, finding_id, name, text in PINNED:
            with self.subTest(finding_id=finding_id):
                self.assertEqual(FIX.generate(candidate, finding_id), (name, text))
                self.assertEqual(FIX.unbuildable_reason(candidate), "")

    def test_refusals_keep_their_words(self):
        for candidate, reason in REFUSALS:
            with self.subTest(reason=reason):
                self.assertEqual(FIX.unbuildable_reason(candidate), reason)
                self.assertIsNone(FIX.generate(candidate, "no-pdb.f-1"))

    def test_kind_matches_the_recipe(self):
        self.assertEqual(FIX.KIND, "no-pdb")
        self.assertIn(FIX.KIND, registry.kinds())


class ReadInputs(unittest.TestCase):
    def test_reads_back_what_generate_wrote(self):
        for candidate, finding_id, _, text in PINNED[:1]:
            self.assertEqual(FIX.read_inputs(text), (candidate, finding_id))

    def test_finding_id_shape_is_the_audits(self):
        # The broker reads only the finding ids the audit mints.
        audit = SCRIPTS.parent / "skills" / "fleet-audit" / "scripts"
        sys.path.insert(0, str(audit))
        try:
            import audit_report
        finally:
            sys.path.remove(str(audit))
        self.assertEqual(FIX.FINDING_ID.pattern, audit_report.FINDING_ID_RE.pattern.removeprefix("^").removesuffix("\\Z"))


class Generator(unittest.TestCase):
    def test_loaded_once(self):
        self.assertIs(registry.generator("no-pdb"), FIX)
        self.assertIs(registry.generator("no-pdb", registry.HERE), FIX)

    def test_unknown_kind_is_refused(self):
        for kind in ("no-such-kind", "../no-pdb", "No-Pdb", ""):
            with self.subTest(kind=kind), self.assertRaises(registry.RecipeError):
                registry.generator(kind)

    def test_recipe_without_fix_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bare").mkdir()
            (root / "bare" / registry.RECIPE_FILE).write_text(
                "kind: bare\nname: bare fixes\ntitle: Bare\ndial_environment: dev\n", encoding="utf-8"
            )
            with self.assertRaises(registry.RecipeError):
                registry.generator("bare", root)


if __name__ == "__main__":
    unittest.main()
