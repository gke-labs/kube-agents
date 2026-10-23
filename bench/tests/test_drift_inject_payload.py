# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`build_payload.py` composes what each drift stack used to build by hand.

The three gitops-drift stacks built their payload in an unquoted heredoc, each
with its own copy. `bench/tf/modules/drift-inject/build_payload.py` replaced
all three, and nothing else in the repository renders a payload -- so without
this file a mistake in it reaches a judge rather than a test.

All three consumers are registered in the nightly, so the builder is
exercised there -- but once a night, three repetitions, against a chain that
truncated three times in the week before the two CUJ 1 halves joined it. A
defect here would reach a judge before it reached a test, and on a truncated
night it might reach neither.

The shape under test is `payloadForEvent` in
`k8s-operator/cmd/drift-detector/inject.go`. The case that earns this file is
`owners`: a delete leaves `event.Owners` nil against a `json:"owners,omitempty"`
field, so the detector emits no key, and `gitops-drift-deleted-policy` says in
its own comments that sending `[]` would be "the shape that does not occur".
"""

import importlib.util
import re
import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "bench" / "tf" / "modules" / "drift-inject" / "build_payload.py"

# The ordered scalars every caller passes, with the fixed part of the argv the
# stacks use. Values are a stand-in for the Terraform interpolations; what is
# under test is placement and type, not the strings themselves.
SCALARS = [
    "eval-operator@example.com patched a thing",  # summary
    "host-cluster",  # cluster
    "a-project",  # project
    "us-east4",  # location
    "eval-operator@example.com",  # principal
    "kubectl/v1.31.0 (linux/amd64)",  # user_agent
    "patch",  # verb
    "io.k8s.apps.v1.deployments.patch",  # method_name
    "2026-10-19T00:00:00Z",  # timestamp
    "eval-drift-1-2",  # insert_id
]
RESOURCE = ["apps", "v1", "eval-ns", "deployments", "eval-workload"]


def load_builder():
    spec = importlib.util.spec_from_file_location("build_payload", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildPayload(unittest.TestCase):
    def setUp(self):
        self.builder = load_builder()

    def build(self, *trailing):
        """Run the builder the way drift_build_payload does, argv and all."""
        argv = ["/dev/null", *SCALARS, *RESOURCE, *trailing]
        return self.builder.build(argv)

    def test_the_enriched_shape_two_stacks_send(self):
        doc = self.build(
            "enriched",
            "false",
            "argocd-controller|Apply|.spec.replicas,.spec.template.spec.containers",
            "kubectl-edit|Update|.spec.template.spec.securityContext.runAsNonRoot",
        )
        self.assertEqual(doc["kind"], "gitops-drift")
        self.assertEqual(doc["join"], "enriched")
        self.assertIs(doc["reconciled"], False)
        self.assertEqual(doc["principal"], "eval-operator@example.com")
        self.assertEqual(doc["insert_id"], "eval-drift-1-2")
        self.assertEqual(
            doc["resource"],
            {
                "group": "apps",
                "version": "v1",
                "namespace": "eval-ns",
                "resource": "deployments",
                "name": "eval-workload",
            },
        )
        self.assertEqual(
            doc["owners"],
            [
                {
                    "manager": "argocd-controller",
                    "operation": "Apply",
                    "paths": [".spec.replicas", ".spec.template.spec.containers"],
                },
                {
                    "manager": "kubectl-edit",
                    "operation": "Update",
                    "paths": [".spec.template.spec.securityContext.runAsNonRoot"],
                },
            ],
        )

    def test_no_owner_means_no_owners_key_at_all(self):
        """A delete leaves event.Owners nil, and the field is omitempty.

        gitops-drift-deleted-policy depends on the key being absent. An empty
        list is a shape the detector never emits, and `_drift_ownership_block`
        only declines to read it because it returns early on a non-enriched
        join -- so an `owners: []` here is one reordering away from the card
        claiming the live object records no managedFields entries at all.
        """
        doc = self.build("not-attempted", "false")
        self.assertNotIn("owners", doc)
        self.assertNotIn("owners", json.loads(json.dumps(doc)))

    def test_reconciled_is_a_json_boolean_both_ways(self):
        self.assertIs(self.build("enriched", "true")["reconciled"], True)
        self.assertIs(self.build("enriched", "false")["reconciled"], False)

    def test_a_reconciled_value_that_is_not_a_boolean_is_refused(self):
        """It used to become False silently, on a field the card renders."""
        for value in ("1", "yes", "True ", "", "no"):
            with self.subTest(value=value):
                if value.strip().lower() in ("true", "false"):
                    continue
                with self.assertRaises(SystemExit):
                    self.build("enriched", value)

    def test_a_short_argv_is_refused_rather_than_shifted(self):
        """Positional argv truncates silently; a miscount must not build a payload.

        Dropping one argument used to leave `join` holding an owner spec and
        `insert_id` holding a resource field -- a payload the daemon accepts,
        failing minutes later as a card that never appeared.
        """
        argv = ["/dev/null", *SCALARS, *RESOURCE[:-1], "enriched", "false"]
        with self.assertRaises(SystemExit):
            self.builder.build(argv)

    def test_a_malformed_owner_spec_is_refused(self):
        with self.assertRaises(SystemExit):
            self.build("enriched", "false", "argocd-controller|Apply")

    def test_a_pipe_in_the_manager_does_not_become_the_operation(self):
        """The field managers are unvalidated `var` strings at the call sites.

        Splitting from the left made `argo|cd|Apply|.spec.replicas` parse as
        manager `argo`, operation `cd`, and `Apply|.spec.replicas` as a path:
        three parts, accepted, and a record naming an owner the agent cannot
        find on the live object.
        """
        doc = self.build("enriched", "false", "argo|cd|Apply|.spec.replicas")
        owner = doc["owners"][0]
        self.assertEqual(owner["manager"], "argo|cd")
        self.assertEqual(owner["operation"], "Apply")
        self.assertEqual(owner["paths"], [".spec.replicas"])

    def test_an_owner_spec_that_yields_an_empty_field_is_refused(self):
        """Judged on what ships, not on the raw parts.

        A paths part of `","` is a non-empty string that yields no paths, and
        a part of `" "` is non-empty too. Validating the parts and emitting a
        value derived from them afterwards let both through: the record then
        asserts that a manager the plant named owns no recorded paths, which
        `_drift_ownership_block` renders as written and a judge reads as the
        agent being wrong.
        """
        for spec in (
            "|Apply|.spec.replicas",
            "argocd-controller||.spec.replicas",
            "argocd-controller|Apply|",
            "argocd-controller|Apply|,",
            "argocd-controller|Apply|, ,",
            "argocd-controller| |.spec.replicas",
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(SystemExit):
                    self.build("enriched", "false", spec)

    def test_an_owner_spec_is_emitted_stripped(self):
        """` Apply ` is not the operation anyone meant, and used to ship."""
        doc = self.build(
            "enriched", "false", " argocd-controller | Apply | .spec.replicas , .spec.replicas "
        )
        self.assertEqual(
            doc["owners"],
            [
                {
                    "manager": "argocd-controller",
                    "operation": "Apply",
                    "paths": [".spec.replicas", ".spec.replicas"],
                }
            ],
        )

    def test_every_scalar_lands_under_its_own_key(self):
        """Placement, so a reordering of SCALARS cannot pass unnoticed."""
        doc = self.build("enriched", "false")
        for name, value in zip(self.builder.SCALARS, SCALARS):
            with self.subTest(field=name):
                self.assertEqual(doc[name], value)

    def test_the_shell_probes_for_the_kind_the_payload_stamps(self):
        """`DRIFT_INJECT_KIND` and `KIND` are the same string in two files.

        `drift_require_daemon` refuses a daemon that does not advertise
        `DRIFT_INJECT_KIND`; this builder stamps `KIND`. If they ever disagree
        the probe passes a daemon for a kind the record does not carry, which
        is the failure that comment calls the worst way for a fixture to be
        wrong -- the daemon takes the payload down its event path, answers
        200, and files a card about something else.
        """
        shell = (BUILDER.parent / "drift_inject.sh").read_text(encoding="utf-8")
        declared = re.search(r'^DRIFT_INJECT_KIND="([^"]*)"', shell, re.M)
        self.assertIsNotNone(declared, "DRIFT_INJECT_KIND is gone or reshaped")
        self.assertEqual(declared.group(1), self.builder.KIND)

    def test_the_script_itself_writes_the_file_the_shell_expects(self):
        """Exercise `main`, not just `build`.

        Every other test calls `build` directly, so `main`'s `out = argv[0]` /
        `build(argv)` split is not covered: were it `main(sys.argv)` instead of
        `main(sys.argv[1:])`, every field would shift by one and the payload
        would be written to whatever path the summary happens to be -- and all
        of them would still pass.
        """
        import subprocess
        import sys
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "payload.json"
            result = subprocess.run(
                [sys.executable, str(BUILDER), str(out), *SCALARS, *RESOURCE, "enriched", "false"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.is_file(), f"{out} was not written; stderr: {result.stderr}")
            doc = json.loads(out.read_text())
        self.assertEqual(doc["kind"], "gitops-drift")
        self.assertEqual(doc["summary"], SCALARS[0])
        self.assertEqual(doc["insert_id"], SCALARS[-1])
        self.assertEqual(doc["resource"]["name"], RESOURCE[-1])

    def test_a_value_that_would_break_hand_built_json_survives(self):
        """The quoting hazard the builder exists to remove.

        A principal carrying a double quote, a brace and a newline went into an
        unquoted heredoc before this; here it is placed by json.dumps.
        """
        hostile = 'he said "hi" {\n} \\ end'
        argv = ["/dev/null", hostile, *SCALARS[1:], *RESOURCE, "enriched", "false"]
        doc = self.builder.build(argv)
        self.assertEqual(doc["summary"], hostile)
        self.assertEqual(json.loads(json.dumps(doc))["summary"], hostile)


if __name__ == "__main__":
    unittest.main()
