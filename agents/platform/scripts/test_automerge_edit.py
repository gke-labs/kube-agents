"""Adversarial tests for the edit-manifest verb (automerge_edit.py).

Every refusal case is a pull request a hostile or confused sandbox could open:
the verb must accept exactly the declared scalar edits to one manifest and
refuse everything else. Diffs are built the way git prints them.
"""

import difflib
import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge_edit as ae
from fix_recipes import registry

PATH = "clusters/fleet-1/shop/budgets.yaml"
BASE = """\
# Budgets for the shop namespace.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: shop
spec:
  replicas: 3
---
# Raised by hand after the March incident.
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: checkout-pdb
  namespace: shop
  labels:
    team: 'payments'   # owner
spec:
  minAvailable: 1  # keep one up
  maxUnavailable: "25%"
  selector:
    matchLabels:
      app: checkout
"""
RECIPE = registry.parse(
    "kind: raise-pdb\nname: budget raises\ntitle: Budget raises\ndial_environment: dev\n"
    "verify: edit-manifest\nedit_api_version: policy/v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec.minAvailable spec.maxUnavailable\n"
)


def allowed_edit(path, old, new):
    if path == ("spec", "minAvailable"):
        return isinstance(new, int) and not isinstance(new, bool) and isinstance(old, int) and old < new <= 10
    return isinstance(new, str) and new.endswith("%")


FIX = types.SimpleNamespace(allowed_edit=allowed_edit)


def git_diff(old: str, new: str, path: str = PATH, mode: str = "100644", new_path: str | None = None) -> str:
    new_path = new_path or path
    body = "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{new_path}")
    )
    return f"diff --git a/{path} b/{new_path}\nindex {ae.git_blob_id(old)[:7]}..5d6e7f8 {mode}\n" + body


def verify(head: str, base: str = BASE, path: str = PATH, diff: str | None = None, recipe=RECIPE, fix=FIX):
    return ae.verify_edited_manifest(recipe, fix, git_diff(base, head, path) if diff is None else diff, path, base, head)


class Accepted(unittest.TestCase):
    def test_one_declared_field(self):
        edit = verify(BASE.replace("minAvailable: 1  #", "minAvailable: 2  #"))
        self.assertEqual((edit.kind, edit.cluster, edit.doc_index), ("raise-pdb", "fleet-1", 1))
        self.assertEqual((edit.object_kind, edit.name, edit.namespace), ("PodDisruptionBudget", "checkout-pdb", "shop"))
        self.assertEqual(edit.changes, ((("spec", "minAvailable"), 1, 2),))

    def test_two_declared_fields_keep_their_quoting(self):
        head = BASE.replace("minAvailable: 1  #", "minAvailable: 3  #").replace('"25%"', '"50%"')
        self.assertEqual(len(verify(head).changes), 2)

    def test_diff_with_function_context_in_hunk_header(self):
        head = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #")
        diff = git_diff(BASE, head).replace(" @@\n", " @@ spec:\n", 1)
        self.assertEqual(verify(head, diff=diff).changes[0][2], 2)


class RefusedDiffShapes(unittest.TestCase):
    head = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #")

    def assertRefused(self, diff, base=BASE, path=PATH, recipe=RECIPE, fix=FIX, head=None):
        if (base, path) == (BASE, PATH):
            self.assertNotEqual(diff, git_diff(BASE, self.head), "the case is the accepted diff, unchanged")
        with self.assertRaises(ae.EditRefused):
            ae.verify_edited_manifest(recipe, fix, diff, path, base, self.head if head is None else head)

    def test_rename(self):
        diff = git_diff(BASE, self.head, new_path="clusters/fleet-1/shop/other.yaml")
        self.assertRefused(diff)
        renamed = (
            f"diff --git a/{PATH} b/clusters/fleet-1/x.yaml\nsimilarity index 95%\n"
            f"rename from {PATH}\nrename to clusters/fleet-1/x.yaml\n" + git_diff(BASE, self.head).split("\n", 2)[2]
        )
        self.assertRefused(renamed)

    def test_mode_changes(self):
        self.assertRefused(git_diff(BASE, self.head, mode="100755"))
        self.assertRefused(git_diff(BASE, self.head, mode="120000"))
        with_mode = re.sub(r"index (\w+)\.\.(\w+) 100644\n", r"old mode 100644\nnew mode 100755\nindex \1..\2\n", git_diff(BASE, self.head))
        self.assertRefused(with_mode)

    def test_a_second_file(self):
        self.assertRefused(git_diff(BASE, self.head) + git_diff("a: 1\n", "a: 2\n", "clusters/fleet-1/other.yaml"))

    def test_new_deleted_and_binary(self):
        one = git_diff(BASE, self.head)
        self.assertRefused(re.sub(r"index \w+\.\.\w+ 100644", "new file mode 100644\nindex 0000000..5d6e7f8", one))
        self.assertRefused(re.sub(r"index \w+\.\.\w+ 100644", "deleted file mode 100644\nindex 1a2b3c4..0000000", one))
        self.assertRefused(f"diff --git a/{PATH} b/{PATH}\nindex 1a2b3c4..5d6e7f8 100644\nBinary files a/{PATH} and b/{PATH} differ\n")

    def test_missing_final_newline(self):
        self.assertRefused(git_diff(BASE, self.head) + "\\ No newline at end of file\n")
        self.assertRefused(git_diff(BASE.rstrip("\n"), self.head.rstrip("\n")), base=BASE.rstrip("\n"))

    def test_quoted_or_odd_paths(self):
        for path in ('"clusters/fleet-1/a b.yaml"', "clusters/fleet-1/../x.yaml", "clusters/fleet-1/.hidden/x.yaml",
                     "apps/shop/budgets.yaml", "clusters/budgets.yaml", "clusters/fleet-1/shop/budgets.json",
                     "clusters//shop/budgets.yaml", "clusters/a\\..\\b/x.yaml", "clusters/fleet\u22151/x.yaml",
                     "clusters/fleet-1/caf\u00e9.yaml"):
            with self.subTest(path=path):
                self.assertRefused(git_diff(BASE, self.head, path=path), path=path)

    def test_diff_path_must_be_the_file_the_broker_read(self):
        self.assertRefused(git_diff(BASE, self.head), path="clusters/fleet-1/shop/other.yaml")

    def test_a_diff_that_does_not_apply_to_the_broker_base(self):
        moved = BASE.replace("replicas: 3", "replicas: 4")
        diff = git_diff(BASE, self.head)
        # The hunk's context still matches the moved base, but the diff was taken
        # against another blob: refused, since by line number it could land elsewhere.
        self.assertRefused(diff, base=moved)
        stale = git_diff(BASE.replace("keep one up", "keep two"), self.head.replace("keep one up", "keep two"))
        self.assertRefused(stale)

    def test_lying_hunk_headers(self):
        diff = git_diff(BASE, self.head)
        header = next(line for line in diff.split("\n") if line.startswith("@@"))
        for lie in ("@@ -1,3 +1,3 @@", "@@ -16,0 +16,1 @@", "@@ -99,7 +99,7 @@", header.replace("+", "+1")):
            with self.subTest(lie=lie):
                self.assertRefused(diff.replace(header, lie))

    def test_unrecognised_lines_in_a_hunk(self):
        diff = git_diff(BASE, self.head)
        self.assertRefused(diff.replace("\n-  minAvailable", "\n\n-  minAvailable"))
        self.assertRefused(diff + "garbage\n")

    def test_not_a_diff(self):
        for diff in (None, "", 42, "diff --git a/x b/x\n"):
            with self.subTest(diff=diff):
                self.assertIsNone(ae.modified_file_from_diff(diff))


class RefusedContent(unittest.TestCase):
    def assertRefused(self, head, base=BASE, fix=FIX, recipe=RECIPE, *, unchanged_ok=False):
        if not unchanged_ok:
            self.assertNotEqual(head, base, "the case changes nothing, so it would pass for the wrong reason")
        with self.assertRaises(ae.EditRefused) as cm:
            verify(head, base=base, fix=fix, recipe=recipe)
        return str(cm.exception)

    def test_an_undeclared_field(self):
        self.assertRefused(BASE.replace("app: checkout", "app: everything"))
        self.assertRefused(BASE.replace("minAvailable: 1  #", "minAvailable: 2  #").replace("app: checkout", "app: x"))

    def test_identity_fields(self):
        for old, new in (("name: checkout-pdb", "name: other"), ("kind: PodDisruptionBudget", "kind: Secret"),
                         ("apiVersion: policy/v1\nkind: Pod", "apiVersion: policy/v1beta1\nkind: Pod"),
                         ("  namespace: shop\n  labels", "  namespace: kube-system\n  labels")):
            with self.subTest(new=new):
                self.assertRefused(BASE.replace("minAvailable: 1  #", "minAvailable: 2  #").replace(old, new))

    def test_comment_and_whitespace_elsewhere(self):
        edited = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #")
        for head in (
            edited.replace("# owner", "# owners"),
            edited.replace("by hand after", "by hand, after"),
            edited.replace("replicas: 3", "replicas: 3 "),
            edited.replace("minAvailable: 2  # keep", "minAvailable: 2 # keep"),
            edited.replace("minAvailable: 2  # keep one up", "minAvailable: 2  # changed"),
            edited.replace("    team: 'payments'", "    team:  'payments'"),
        ):
            with self.subTest(head=head):
                self.assertRefused(head)

    def test_the_wrong_document_or_a_document_swap(self):
        self.assertIn("not a policy/v1 PodDisruptionBudget", self.assertRefused(BASE.replace("replicas: 3", "replicas: 9").replace("kind: Deployment", "kind: Deployment")))
        first, second = BASE.split("---\n")
        self.assertRefused(second + "---\n" + first, base=BASE)

    def test_documents_added_or_split(self):
        self.assertRefused(BASE.replace("minAvailable: 1  # keep one up", "minAvailable: 2\n---"))
        self.assertRefused(BASE.replace("# Raised by hand after the March incident.", "---"))

    def test_yaml_smuggling_in_the_new_value(self):
        for value in ("&a 2", "*a", "!!int 2", "[2]", "{a: 2}", "2 # x", "|", "{{ .Values.min }}", "2\r", "yes", "0x10", "2.0"):
            with self.subTest(value=value):
                self.assertRefused(BASE.replace("minAvailable: 1  #", f"minAvailable: {value}  #"))

    def test_a_type_change_is_refused_before_allowed_edit(self):
        def accepts_anything(path, old, new):
            return True

        anything = types.SimpleNamespace(allowed_edit=accepts_anything)
        for old, new, said in (
            ("minAvailable: 1  #", 'minAvailable: "2"  #', "spec.minAvailable (int to str)"),
            ("minAvailable: 1  #", "minAvailable: true  #", "spec.minAvailable (int to bool)"),
            ('"25%"', "3", "spec.maxUnavailable (str to int)"),
        ):
            with self.subTest(new=new):
                why = self.assertRefused(BASE.replace(old, new), fix=anything)
                self.assertIn(f"changes the type of {said}", why)

    def test_a_value_the_recipe_does_not_allow(self):
        for value in ("0", "11", "-1"):
            with self.subTest(value=value):
                self.assertRefused(BASE.replace("minAvailable: 1  #", f"minAvailable: {value}  #"))

    def test_a_recipe_without_allowed_edit_accepts_nothing(self):
        head = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #")
        self.assertIn("has no allowed_edit", self.assertRefused(head, fix=types.SimpleNamespace()))
        self.assertRefused(head, fix=types.SimpleNamespace(allowed_edit=lambda *a: 1))

    def test_an_add_manifest_recipe_never_verifies_an_edit(self):
        plain = registry.parse("kind: x\nname: x fixes\ntitle: X\ndial_environment: dev\n")
        self.assertRefused(BASE.replace("minAvailable: 1  #", "minAvailable: 2  #"), recipe=plain)

    def test_creating_a_declared_field(self):
        base = BASE.replace('  maxUnavailable: "25%"\n', "")
        self.assertRefused(base.replace("  selector:", '  maxUnavailable: "50%"\n  selector:'), base=base)

    def test_flow_style_and_hostile_constructs_on_the_base(self):
        flow = BASE.replace("spec:\n  minAvailable: 1  # keep one up\n", "spec: {minAvailable: 1}\nx:\n")
        self.assertRefused(flow.replace("{minAvailable: 1}", "{minAvailable: 2}"), base=flow)
        for old, new in (("app: checkout", "app: &a checkout"), ("replicas: 3", "replicas: !!int 3"),
                         ("replicas: 3", "replicas: {{ .Values.r }}"), ("app: checkout", "app: checkout\n      app: other")):
            with self.subTest(new=new):
                base = BASE.replace(old, new)
                self.assertRefused(base.replace("minAvailable: 1  #", "minAvailable: 2  #"), base=base)

    def test_crlf_and_oversize(self):
        crlf = BASE.replace("\n", "\r\n")
        self.assertRefused(crlf.replace("minAvailable: 1", "minAvailable: 2"), base=crlf)
        big = BASE + "# " + "x" * ae.MAX_FILE_BYTES + "\n"
        self.assertIn("larger than", self.assertRefused(big.replace("minAvailable: 1  #", "minAvailable: 2  #"), base=big))

    def test_no_change_at_all(self):
        self.assertRefused(BASE, unchanged_ok=True)


class PlainStringsInTheBase(unittest.TestCase):
    def test_unquoted_percentages_quantities_and_digit_led_names(self):
        base = BASE.replace('maxUnavailable: "25%"', "maxUnavailable: 25%").replace("name: checkout-pdb", "name: 3ds-pdb")
        edit = verify(base.replace("minAvailable: 1  #", "minAvailable: 2  #"), base=base)
        self.assertEqual((edit.name, edit.changes), ("3ds-pdb", ((("spec", "minAvailable"), 1, 2),)))
        # set_scalar writes a new percentage quoted, so that is the only head it accepts.
        self.assertEqual(verify(base.replace("maxUnavailable: 25%", 'maxUnavailable: "50%"'), base=base).changes,
                         ((("spec", "maxUnavailable"), "25%", "50%"),))
        with self.assertRaises(ae.EditRefused):
            verify(base.replace("maxUnavailable: 25%", "maxUnavailable: 50%"), base=base)

    def test_plain_forms_yaml_reads_as_other_types_stay_unreadable(self):
        from fix_recipes import yaml_edit

        # The second row: go-yaml reads these as numbers where PyYAML reads text.
        for raw in ("yes", "0x10", "1_000", "2.5", "~", "2024-01-01", "1:20",
                    "08", "09", "0_9", "-09", "+08", "+_1", "-_0", "007"):
            with self.subTest(raw=raw), self.assertRaises(yaml_edit.RefusedEdit):
                yaml_edit.read_scalar(f"a: {raw}\n", 0, ["a"])
        self.assertEqual(yaml_edit.read_scalar("a: 128Mi\n", 0, ["a"]), "128Mi")


class ApiVersion(unittest.TestCase):
    def test_a_lookalike_kind_in_another_group_is_refused(self):
        base = BASE.replace("apiVersion: policy/v1", "apiVersion: evil.example.com/v1")
        with self.assertRaises(ae.EditRefused) as cm:
            verify(base.replace("minAvailable: 1  #", "minAvailable: 2  #"), base=base)
        self.assertIn("evil.example.com/v1 PodDisruptionBudget, not a policy/v1 PodDisruptionBudget", str(cm.exception))


class Hardening(unittest.TestCase):
    def test_a_failing_allowed_edit_is_a_refusal(self):
        def broken(path, old, new):
            return {}[path]  # a recipe bug: raises KeyError

        head = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #")
        with self.assertRaises(ae.EditRefused) as cm:
            verify(head, fix=types.SimpleNamespace(allowed_edit=broken))
        self.assertIn("allowed_edit failed (KeyError)", str(cm.exception))

    def test_base_drift_repro(self):
        # The reviewer's repro: the PR edits `checkout`'s budget; main then put a
        # same-shaped budget, `payments-db`, on top of the file. By line number
        # the hunk applies to `payments-db`, which git would not edit. The diff
        # is git's own, so its blob id 6296333 is the real hash of the merge base.
        budget = (
            "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: {name}\n  namespace: shop\n"
            "  labels:\n    team: payments\nspec:\n  minAvailable: 1\n  selector:\n    matchLabels:\n"
            "      tier: web\n      app: {app}\n"
        )
        merge_base = budget.format(name="checkout", app="checkout")
        base_tip = budget.format(name="payments-db", app="db") + "---\n" + merge_base
        diff = (
            "diff --git a/clusters/fleet-1/b.yaml b/clusters/fleet-1/b.yaml\n"
            "index 6296333..d6adbb7 100644\n"
            "--- a/clusters/fleet-1/b.yaml\n+++ b/clusters/fleet-1/b.yaml\n"
            "@@ -6,7 +6,7 @@ metadata:\n   labels:\n     team: payments\n spec:\n-  minAvailable: 1\n+  minAvailable: 2\n"
            "   selector:\n     matchLabels:\n       tier: web\n"
        )
        recipe = registry.parse(
            "kind: raise-pdb\nname: n\ntitle: t\ndial_environment: dev\n"
            "verify: edit-manifest\nedit_api_version: policy/v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec.minAvailable\n"
        )
        self.assertTrue(ae.git_blob_id(merge_base).startswith("6296333"))
        head = merge_base.replace("minAvailable: 1", "minAvailable: 2")
        edit = ae.verify_edited_manifest(recipe, FIX, diff, "clusters/fleet-1/b.yaml", merge_base, head)
        self.assertEqual(edit.name, "checkout")
        with self.assertRaises(ae.EditRefused) as cm:
            ae.verify_edited_manifest(recipe, FIX, diff, "clusters/fleet-1/b.yaml", base_tip, head)
        self.assertIn("the base branch moved", str(cm.exception))
        # A crafted base whose blob id shares the diff's 7-hex prefix (simulated
        # here by printing the tip's own prefix) still applies to the wrong
        # object, and is refused because the result is not the real head.
        crafted = diff.replace("index 6296333..", f"index {ae.git_blob_id(base_tip)[:7]}..")
        with self.assertRaises(ae.EditRefused) as cm:
            ae.verify_edited_manifest(recipe, FIX, crafted, "clusters/fleet-1/b.yaml", base_tip, head)
        self.assertIn("is not the proposal's head", str(cm.exception))

    def test_a_head_that_is_not_the_diff_applied_is_refused(self):
        head = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #")
        with self.assertRaises(ae.EditRefused):
            ae.verify_edited_manifest(RECIPE, FIX, git_diff(BASE, head), PATH, BASE, head.replace("replicas: 3", "replicas: 4"))

    def test_text_that_cannot_be_encoded_is_a_refusal(self):
        head = BASE.replace("minAvailable: 1  #", "minAvailable: 2  #  \udc80")
        with self.assertRaises(ae.EditRefused):
            ae.verify_edited_manifest(RECIPE, FIX, git_diff(BASE, head), PATH, BASE, head)

    def test_a_raising_allowed_edit_is_one_recipe_refusal(self):
        # The broker catches EditRefused per recipe, so a recipe bug cannot end the judgement early.
        with self.assertRaises(ae.EditRefused):
            verify(BASE.replace("minAvailable: 1  #", "minAvailable: 2  #"),
                   fix=types.SimpleNamespace(allowed_edit=lambda *a: 1 / 0))

    def test_blob_id_is_gits(self):
        # `git hash-object` of "a: 1\n".
        self.assertEqual(ae.git_blob_id("a: 1\n"), "a8926a52d8dcba5c4386945476ab6a2ad008afd4")


class Registry(unittest.TestCase):
    HEAD = "kind: k\nname: k fixes\ntitle: K\ndial_environment: dev\n"

    def test_edit_fields_are_validated(self):
        for extra in (
            "verify: patch-manifest\n",
            "edit_kind: PodDisruptionBudget\nedit_paths: spec.minAvailable\n",
            "verify: edit-manifest\nedit_paths: spec.minAvailable\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: pdb\nedit_paths: spec.minAvailable\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: metadata.name\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: kind\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec spec.minAvailable\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec..x\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec.01\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec.a spec.a\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: List\nedit_paths: items.0.kind\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: spec.template.metadata.namespace\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: metadata.ownerReferences.0.uid\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: metadata.labels.app\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: PodDisruptionBudget\nedit_paths: spec.selector.matchLabels.app\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: status.replicas\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: spec.template.spec.nodeSelector.pool\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: spec.template.spec.nodeName\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: spec.template.spec.serviceAccountName\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: metadata.resourceVersion\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: metadata.generation\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: metadata.managedFields.0.manager\n",
            "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Service\nedit_paths: spec.topologySelector\n",
            "verify: edit-manifest\nedit_kind: PodDisruptionBudget\nedit_paths: spec.minAvailable\n",
            "verify: edit-manifest\nedit_api_version: policy\nedit_kind: PodDisruptionBudget\nedit_paths: spec.minAvailable\n",
            "edit_api_version: policy/v1\n",
        ) + tuple(
            f"verify: edit-manifest\nedit_api_version: apps/v1\nedit_kind: Deployment\nedit_paths: spec.template.spec.{path}\n"
            for path in ("tolerations.0.key", "affinity.nodeAffinity.x", "priorityClassName", "schedulerName",
                         "runtimeClassName", "topologySpreadConstraints.0.topologyKey", "hostNetwork",
                         "containers.0.securityContext.privileged", "automountServiceAccountToken")
        ) + (
        ):
            with self.subTest(extra=extra), self.assertRaises(registry.RecipeError):
                registry.parse(self.HEAD + extra)

    def test_paths_with_indexes(self):
        recipe = registry.parse(
            self.HEAD + "verify: edit-manifest\nedit_api_version: v1\nedit_kind: Deployment\nedit_paths: spec.template.spec.containers.0.image\n"
        )
        self.assertEqual(recipe.edit_path_list(), (("spec", "template", "spec", "containers", 0, "image"),))

    def test_no_shipped_recipe_opts_in_yet(self):
        self.assertEqual([r.kind for r in registry.recipes() if r.verify == "edit-manifest"], [])
        self.assertEqual(registry.recipe("no-pdb").verify, "add-manifest")


if __name__ == "__main__":
    unittest.main()
