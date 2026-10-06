"""The chart renders `spec.security.scopedServiceAccountPool` from
`platformAgent.security.scopedServiceAccountPool`, in three states.

The chart default (`enabled: false`, no members) renders no block: the CR the
operator reads says "no pool" rather than carrying a present block with the
switch off. A disabled block with members renders `enabled: false` and the
members: the mapping is declared, visible in the CR, and inert, because
`enabled` is the arming rule and the list alone arms nothing. `enabled: true`
with no members fails the render, where the message reaches the person running
the upgrade, rather than installing a CR the broker refuses to start on. The
schema closes the pool and each member, so a misspelt key is named rather than
dropped.

An armed pool also has to reach a CRD that knows the field. `helm upgrade` never
applies `crds/`, so on an install whose live CRD predates the pool the API server
admits the CR with the block pruned: the operator sees no pool, the broker starts
with the pool off, and every cluster read runs on the agent's own identity while
the release record says "armed". The template guards that the way it guards
`integration.forges`: `lookup` the installed CRD and fail the render when
`spec.security` lacks `scopedServiceAccountPool`. `lookup` returns nothing under
`helm template`, so the guard is pinned as text here and the render tests are
unaffected by it. The text-structural tests run everywhere; the render tests need
a helm binary, which the agent-startup job lacks.

Run: python3 -m unittest discover -s tests -p 'test_chart_scoped_pool_block.py' -v
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "platformAgent.security.scopedServiceAccountPool"
_FAIL_MESSAGE = "requires at least one serviceAccounts entry"
_CRD_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'
_CRD_DIG = 'dig "schema" "openAPIV3Schema" "properties" "spec" "properties" "security" "properties" (dict) .'
_CRD_REMEDY = "kubectl apply --server-side -f charts/kube-agents/crds/"

# Deliberately not in alphabetical order, so a render that sorted the list
# would fail the verbatim comparison.
MEMBERS = [
    {"projectId": "payments-prod",
     "serviceAccountEmail": "ka-payments-prod-1a2b3c4d@host-project.iam.gserviceaccount.com"},
    {"projectId": "billing-staging",
     "serviceAccountEmail": "ka-billing-staging-5e6f7a8b@host-project.iam.gserviceaccount.com"},
    {"projectId": "analytics-dev",
     "serviceAccountEmail": "ka-analytics-dev-9c0d1e2f@host-project.iam.gserviceaccount.com"},
]


class ScopedPoolBlockShapeTest(unittest.TestCase):
    """What the template says, readable without helm."""

    def setUp(self):
        self.template = _TEMPLATE.read_text()

    def test_the_block_is_gated_on_armed_or_populated_and_not_on_with(self):
        # `with` on the whole value would render the default's `enabled: false`
        # as a present block; the gate has to read both keys.
        block = re.search(r"\{\{- \$pool := \.Values\.platformAgent\.security\.scopedServiceAccountPool \| default dict \}\}\n(.*?)scopedServiceAccountPool:\n",
                          self.template, re.DOTALL)
        self.assertIsNotNone(block, "the scoped pool block is missing from the CR template")
        self.assertIn("{{- if or $pool.enabled $pool.serviceAccounts }}", block.group(1))
        self.assertNotIn("compactFields", block.group(1))

    def test_an_armed_empty_pool_fails_the_render_by_name(self):
        guard = re.search(r'\{\{- if and \$pool\.enabled \(not \$pool\.serviceAccounts\) \}\}\n\s*\{\{- fail "([^"]*)" \}\}',
                          self.template)
        self.assertIsNotNone(guard, "the armed-but-empty guard is missing from the CR template")
        self.assertIn(_VALUE_PATH + ".enabled", guard.group(1))
        self.assertIn(_FAIL_MESSAGE, guard.group(1))

    def _crd_guard(self):
        """The text from the pool's `$pool` binding to the pool block, which is
        where the CRD guard has to sit: next to the block it protects."""
        block = re.search(r"\{\{- \$pool := \.Values\.platformAgent\.security\.scopedServiceAccountPool \| default dict \}\}\n(.*?)scopedServiceAccountPool:\n",
                          self.template, re.DOTALL)
        self.assertIsNotNone(block, "the scoped pool block is missing from the CR template")
        return block.group(1)

    def test_an_armed_pool_looks_up_the_installed_crd(self):
        # `lookup` is empty under `helm template`, so the guard can only be
        # pinned as text: the lookup, the dig into `spec.security`, and the key.
        guard = self._crd_guard()
        self.assertIn(_CRD_LOOKUP, guard)
        self.assertIn(_CRD_DIG, guard)
        self.assertIn('hasKey $props "scopedServiceAccountPool"', guard)

    def test_the_crd_guard_is_gated_on_the_switch(self):
        # A disabled block pruned by an old CRD changes nothing; only an armed
        # pool silently falls back onto the agent's own identity, so only an
        # armed pool pays for the lookup and can fail on it.
        guard = self._crd_guard()
        gate = re.search(r"\{\{- if \$pool\.enabled \}\}\n\s*\{\{- \$crd := " + re.escape(_CRD_LOOKUP), guard)
        self.assertIsNotNone(gate, "the CRD lookup is not gated on $pool.enabled")

    def test_the_crd_guard_fails_naming_the_value_and_the_remedy(self):
        guard = self._crd_guard()
        fail = re.search(r'\{\{- if not \(hasKey \$props "scopedServiceAccountPool"\) \}\}\n\s*\{\{- fail "([^"]*)" \}\}', guard)
        self.assertIsNotNone(fail, "the CRD guard does not fail on a missing key")
        self.assertIn(_VALUE_PATH + ".enabled", fail.group(1))
        self.assertIn("helm upgrade does not update CRDs", fail.group(1))
        self.assertIn(_CRD_REMEDY, fail.group(1))

    def test_the_switch_defaults_to_false_in_the_rendered_block(self):
        self.assertIn("enabled: {{ $pool.enabled | default false }}", self.template)

    def test_the_chart_default_is_disabled_with_no_members(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        pool = values["platformAgent"]["security"]["scopedServiceAccountPool"]
        self.assertEqual(pool, {"enabled": False, "serviceAccounts": []})

    def test_the_schema_closes_the_pool_and_each_member(self):
        schema = json.loads((_CHART / "values.schema.json").read_text())
        pool = schema["properties"]["platformAgent"]["properties"]["security"]["properties"]["scopedServiceAccountPool"]
        self.assertIs(pool["additionalProperties"], False)
        self.assertEqual(set(pool["properties"]), {"enabled", "serviceAccounts"})
        member = pool["properties"]["serviceAccounts"]["items"]
        self.assertIs(member["additionalProperties"], False)
        self.assertEqual(set(member["properties"]), {"projectId", "serviceAccountEmail"})


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ScopedPoolBlockRenderTest(unittest.TestCase):
    def _render(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True,
        )
        return proc

    def _pool_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]["security"].get("scopedServiceAccountPool", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def _values_file(self, pool):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"platformAgent": {"security": {"scopedServiceAccountPool": pool}}}, handle)
        handle.close()
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def test_the_default_renders_no_block(self):
        self.assertEqual(self._pool_of(self._render()), "ABSENT")

    def test_disabled_with_no_members_renders_no_block_however_it_is_spelt(self):
        # The default written out by hand, and the list set to null, both
        # coalesce to the same "nothing declared" and render no block.
        for args in (["-f", self._values_file({"enabled": False, "serviceAccounts": []})],
                     ["--set-json", f"{_VALUE_PATH}.serviceAccounts=null"],
                     ["--set-json", f"{_VALUE_PATH}=null"]):
            with self.subTest(args=args):
                self.assertEqual(self._pool_of(self._render(*args)), "ABSENT")

    def test_disabled_with_members_renders_the_switch_off_and_the_members(self):
        rendered = self._pool_of(self._render("-f", self._values_file({"enabled": False, "serviceAccounts": MEMBERS})))
        self.assertEqual(rendered, {"enabled": False, "serviceAccounts": MEMBERS})

    def test_armed_with_members_renders_the_switch_on_and_the_rows_verbatim_in_order(self):
        rendered = self._pool_of(self._render("-f", self._values_file({"enabled": True, "serviceAccounts": MEMBERS})))
        self.assertIs(rendered["enabled"], True)
        self.assertEqual(rendered["serviceAccounts"], MEMBERS)
        self.assertEqual([row["projectId"] for row in rendered["serviceAccounts"]],
                         [row["projectId"] for row in MEMBERS])
        self.assertEqual(set(rendered), {"enabled", "serviceAccounts"})

    def test_armed_with_no_members_fails_the_render(self):
        # Against the chart default's empty list, and with the list set to
        # null, which deletes the key: both are an armed pool with nothing in it.
        for args in (["--set", f"{_VALUE_PATH}.enabled=true"],
                     ["--set", f"{_VALUE_PATH}.enabled=true",
                      "--set-json", f"{_VALUE_PATH}.serviceAccounts=null"],
                     ["-f", self._values_file({"enabled": True, "serviceAccounts": []})]):
            with self.subTest(args=args):
                proc = self._render(*args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(_FAIL_MESSAGE, proc.stderr)
                self.assertIn(_VALUE_PATH, proc.stderr)

    def test_members_without_the_switch_render_the_switch_off(self):
        # Through the chart default, and with the key deleted outright, so the
        # template's own `default false` is what is under test, not values.yaml.
        members_only = ["-f", self._values_file({"serviceAccounts": MEMBERS})]
        for args in (members_only,
                     [*members_only, "--set-json", f"{_VALUE_PATH}.enabled=null"]):
            with self.subTest(args=args):
                rendered = self._pool_of(self._render(*args))
                self.assertEqual(rendered, {"enabled": False, "serviceAccounts": MEMBERS})

    def test_an_unknown_key_is_refused_by_the_schema(self):
        # A key the CRD does not have is a mistake the schema names rather
        # than drops, under the pool and under a member alike.
        for args in (["--set", f"{_VALUE_PATH}.hostProject=host-project"],
                     ["--set-json", f"{_VALUE_PATH}.serviceAccounts=" + json.dumps(
                         [{**MEMBERS[0], "role": "roles/container.viewer"}])]):
            with self.subTest(args=args):
                proc = self._render(*args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("scopedServiceAccountPool", proc.stderr)

    def test_the_retired_per_cluster_key_is_tolerated_empty_and_refused_populated(self):
        # Every release the composition applied before the pool moved to
        # projects recorded `platformAgent.security.scopedServiceAccounts: []`,
        # and a harness- or operator-mode retag re-applies the recorded values
        # over this chart after checking them against the schema. The empty
        # list is therefore admitted for one release and renders nothing, so
        # the first retag after the move is the no-op it was before; a
        # populated list was a pool armed under the old field, and that
        # install takes a full upgrade, so the schema refuses it by name.
        empty = self._render("--set-json", "platformAgent.security.scopedServiceAccounts=[]")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertNotIn("scopedServiceAccounts", empty.stdout)
        self.assertNotIn("scopedServiceAccountPool", empty.stdout)
        populated = self._render("--set-json", "platformAgent.security.scopedServiceAccounts=" + json.dumps(
            [{"projectId": "p", "location": "l", "clusterName": "c",
              "serviceAccountEmail": "ka-c-12345678@p.iam.gserviceaccount.com"}]))
        self.assertNotEqual(populated.returncode, 0)
        self.assertIn("scopedServiceAccounts", populated.stderr)

    def test_a_non_boolean_switch_is_refused_by_the_schema(self):
        # `--set ...enabled=yes` is a string, which the CRD would reject at
        # apply; the schema refuses it at render.
        proc = self._render("--set-string", f"{_VALUE_PATH}.enabled=yes")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("enabled", proc.stderr)
