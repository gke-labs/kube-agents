"""Tests for automerge_eligibility.py: whether a pull request adds exactly a recipe's generated fix."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge_eligibility as ae
from fix_recipes import registry

FIX = registry.generator("no-pdb")
CLUSTER = "acme-prod-1/us-central1/fleet-1"
NS = "shop"
LABELS = {"app": "checkout"}
FID = "no-pdb.acme-prod-1-us-central1-fleet-1.shop.deployment-checkout"
CANDIDATE = {"cluster": CLUSTER, "object": "Deployment/checkout", "namespace": NS, "pod_selector": {"matchLabels": LABELS}}
NAME, TEXT = FIX.generate(CANDIDATE, FID)
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


def hostile(cluster: str, obj: str, ns: str, fid: str, selector=None) -> str:
    """The generated format with any fields, including ones the generator refuses."""
    import json

    return (
        f"# {cluster}: no-pdb on {obj} ({fid})\n"
        "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n"
        f"  name: {obj.partition('/')[2]}-pdb\n  namespace: {ns}\n"
        "spec:\n  maxUnavailable: 1\n"
        f"  selector: {json.dumps(selector or {'matchLabels': LABELS}, sort_keys=True)}\n"
    )


def verify(path: str, text: str):
    return ae.verify_added_manifest("no-pdb", diff_adding(path, text))


class VerifyTest(unittest.TestCase):
    def test_the_generated_fix_is_verified(self):
        fix = verify(PATH, TEXT)
        self.assertEqual((fix.kind, fix.path, fix.name, fix.finding_id), ("no-pdb", PATH, NAME, FID))
        self.assertEqual(fix.candidate, CANDIDATE)

    def test_a_field_the_audit_does_not_write_is_refused(self):
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
                self.assertIsNone(verify(PATH, hostile(cluster, obj, ns, fid)))

    def test_what_the_generator_refuses_is_never_verified(self):
        for selector in ({"matchLabels": {}}, {"matchLabels": {}, "matchExpressions": []}, {"matchLabels": LABELS, "extra": "1"}):
            with self.subTest(selector=selector):
                self.assertIsNone(verify(PATH, hostile(CLUSTER, "Deployment/checkout", NS, FID, selector)))

    def test_any_edit_is_not_the_generated_fix(self):
        for text in (
            TEXT.replace("maxUnavailable: 1", "minAvailable: 3"),
            TEXT.replace("maxUnavailable: 1", "maxUnavailable: 1\n  minAvailable: 3"),
            TEXT.replace("name: checkout-pdb", "name: other"),
            TEXT + "---\nkind: ClusterRoleBinding\n",
            TEXT.replace('{"app": "checkout"}', '{ "app": "checkout" }'),
            TEXT.replace("policy/v1", "policy/v1beta1"),
            "\n" + TEXT,
            TEXT.replace('{"matchLabels": {"app": "checkout"}}', '{"matchLabels": [1]}'),
            TEXT.replace('{"matchLabels": {"app": "checkout"}}', '{"matchLabels": {"app": 1}}'),
            TEXT.replace('{"matchLabels": {"app": "checkout"}}', '{"matchExpressions": [{"key": "app"}]}'),
        ):
            with self.subTest(text=text):
                self.assertIsNone(verify(PATH, text))

    def test_whitespace_only_differences_are_refused(self):
        for text in (
            TEXT.replace("kind: PodDisruptionBudget", "kind: PodDisruptionBudget "),
            TEXT.replace("spec:\n", "spec:\n\n"),
            TEXT.replace("  maxUnavailable", "   maxUnavailable"),
            TEXT.replace("\n", "\r\n"),
            TEXT + "\n",
            "\ufeff" + TEXT,
        ):
            with self.subTest(text=text):
                self.assertIsNone(verify(PATH, text))
        diff = diff_adding(PATH, TEXT) + "\\ No newline at end of file\n"
        self.assertIsNone(ae.verify_added_manifest("no-pdb", diff))

    def test_extra_files_and_mode_changes_are_refused(self):
        one = diff_adding(PATH, TEXT)
        for diff in (
            one + diff_adding("clusters/fleet-1/shop/other.yaml", "a: 1\n"),
            one.replace("new file mode 100644", "new file mode 100755"),
            one.replace("new file mode 100644", "new file mode 120000"),
        ):
            with self.subTest(diff=diff[:80]):
                self.assertIsNone(ae.verify_added_manifest("no-pdb", diff))

    def test_a_path_outside_the_clusters_tree_is_refused(self):
        for path in ("apps/shop/checkout-pdb.yaml", "clusters/prod-1/shop/checkout-pdb.yaml", "clusters/fleet-1/../x/checkout-pdb.yaml"):
            with self.subTest(path=path):
                self.assertIsNone(verify(path, TEXT))

    def test_check_path_false_leaves_the_path_to_the_caller(self):
        diff = diff_adding("apps/shop/checkout-pdb.yaml", TEXT)
        self.assertIsNone(ae.verify_added_manifest("no-pdb", diff))
        self.assertEqual(ae.verify_added_manifest("no-pdb", diff, check_path=False).path, "apps/shop/checkout-pdb.yaml")

    def test_an_oversized_file_is_refused(self):
        big = TEXT.replace("# acme", "# " + "a" * ae.MAX_ADDED_FILE_BYTES + " acme", 1)
        self.assertIsNone(verify(PATH, big))

    def test_an_unknown_kind_is_an_error(self):
        with self.assertRaises(registry.RecipeError):
            ae.verify_added_manifest("no-such-kind", diff_adding(PATH, TEXT))


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
    def test_the_fix_goes_under_its_own_name_and_cluster(self):
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
            ("clusters/fleet-1/../checkout-pdb.yaml", False),
            ("clusters/fleet-1//checkout-pdb.yaml", False),
        ):
            with self.subTest(path=path):
                self.assertEqual(ae.path_fits(path, NAME, CLUSTER), fits)


if __name__ == "__main__":
    unittest.main()
