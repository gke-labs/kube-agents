"""Tests for automerge_eligibility.py: the no-pdb fix's file format.

`fleet-audit/scripts/test_broker_budget_parity.py` holds `render_budget` to the
audit's own `_pdb_manifest`.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge_eligibility as ae

CLUSTER = "acme-prod-1/us-central1/fleet-1"
NS = "shop"
LABELS = {"app": "checkout"}
FID = "no-pdb.acme-prod-1-us-central1-fleet-1.shop.deployment-checkout"
NAME, TEXT = ae.render_budget(CLUSTER, "Deployment/checkout", NS, {"matchLabels": LABELS}, FID)
PATH = "clusters/fleet-1/shop/checkout-pdb.yaml"


def diff_adding(path: str, text: str) -> str:
    lines = text.split("\n")[:-1]
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..653b552\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


class BudgetTest(unittest.TestCase):
    def test_the_generated_budget_parses_back(self):
        budget = ae.parse_budget(TEXT)
        self.assertEqual((budget.cluster, budget.object, budget.namespace, budget.name), (CLUSTER, "Deployment/checkout", NS, NAME))
        self.assertEqual(budget.selector, {"matchLabels": LABELS})

    def test_a_field_the_generator_cannot_write_is_refused(self):
        smuggled = "x\r---\rapiVersion: rbac.authorization.k8s.io/v1\rkind: ClusterRoleBinding\r---\r#"
        for cluster, obj, ns, fid in (
            (CLUSTER, "Deployment/checkout", NS, smuggled),
            (CLUSTER + "\r---\rkind: Secret", "Deployment/checkout", NS, FID),
            (CLUSTER, "Deployment/checkout", "shop # c", FID),
            (CLUSTER, "Deployment/Checkout", NS, FID),
            (CLUSTER, "DaemonSet/checkout", NS, FID),
            (CLUSTER, "Deployment/checkout", NS, "Not An Id"),
            ("bad cluster", "Deployment/checkout", NS, FID),
            (CLUSTER, "Deployment/caf\u00e9", NS, FID),
        ):
            with self.subTest(cluster=cluster, obj=obj, ns=ns, fid=fid):
                _, text = ae.render_budget(cluster, obj, ns, {"matchLabels": LABELS}, fid)
                self.assertIsNone(ae.parse_budget(text))

    def test_any_edit_is_not_the_generated_budget(self):
        for text in (
            TEXT.replace("maxUnavailable: 1", "minAvailable: 3"),
            TEXT.replace("maxUnavailable: 1", "maxUnavailable: 1\n  minAvailable: 3"),
            TEXT.replace("name: checkout-pdb", "name: other"),
            TEXT + "---\nkind: ClusterRoleBinding\n",
            TEXT.replace('{"app": "checkout"}', '{ "app": "checkout" }'),
            TEXT.replace("policy/v1", "policy/v1beta1"),
            TEXT.rstrip("\n"),
            "\n" + TEXT,
            TEXT.replace('{"matchLabels": {"app": "checkout"}}', '{"matchLabels": [1]}'),
            TEXT.replace('{"matchLabels": {"app": "checkout"}}', '{"matchLabels": {"app": 1}}'),
            TEXT.replace('{"matchLabels": {"app": "checkout"}}', '{"matchExpressions": [{"key": "app"}]}'),
        ):
            with self.subTest(text=text):
                self.assertIsNone(ae.parse_budget(text))


class DiffTest(unittest.TestCase):
    def test_one_added_file_comes_back_byte_for_byte(self):
        self.assertEqual(ae.added_file_from_diff(diff_adding(PATH, TEXT)), (PATH, TEXT))

    def test_a_diff_with_no_mode_or_index_line_is_still_read(self):
        diff = diff_adding(PATH, TEXT).replace("new file mode 100644\nindex 0000000..653b552\n", "")
        self.assertEqual(ae.added_file_from_diff(diff), (PATH, TEXT))

    def test_no_newline_at_the_end_is_kept(self):
        diff = diff_adding(PATH, TEXT) + "\\ No newline at end of file\n"
        self.assertEqual(ae.added_file_from_diff(diff), (PATH, TEXT.rstrip("\n")))

    def test_anything_else_is_refused(self):
        one = diff_adding(PATH, TEXT)
        for diff in (
            None,
            "",
            one + diff_adding("other.yaml", "a: 1\n"),
            one.replace("new file mode 100644", "new file mode 120000"),
            one.replace("--- /dev/null", "--- a/" + PATH),
            one.replace("+++ b/", "+++ b/x"),
            one.replace("+apiVersion", " apiVersion"),
            one.replace("@@ -0,0 +1,9 @@", "@@ -0,0 +1,8 @@"),
            one.replace(f"a/{PATH}", 'a/"odd name"'),
        ):
            with self.subTest(diff=diff):
                self.assertIsNone(ae.added_file_from_diff(diff))


class PathTest(unittest.TestCase):
    budget = ae.parse_budget(TEXT)

    def test_the_budget_goes_under_its_own_name_and_cluster(self):
        for path, fits in (
            (PATH, True),
            ("clusters/fleet-1/checkout-pdb.yaml", True),
            ("apps/shop/checkout-pdb.yaml", False),
            ("checkout-pdb.yaml", False),
            ("clusters/prod-1/shop/checkout-pdb.yaml", False),
            ("clusters/checkout-pdb.yaml", False),
            ("apps/shop/other.yaml", False),
            (".github/checkout-pdb.yaml", False),
            ("apps/.kube-agents/checkout-pdb.yaml", False),
        ):
            with self.subTest(path=path):
                self.assertEqual(ae.path_fits(path, self.budget), fits)


if __name__ == "__main__":
    unittest.main()
