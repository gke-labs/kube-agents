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

"""Each drift stack passes `drift_build_payload` the arguments its case needs.

`test_drift_inject_payload.py` pins the builder's semantics against synthetic
argv. It cannot see what the stacks actually send, so a stack that swapped two
adjacent free-form scalars -- `location` and `principal` are both strings --
or that grew an `owners` argument where its case requires none would still
produce a payload the builder accepts and the daemon files. The case would
then fail on a judged objective as though the agent were wrong.

That is worth a test rather than a comment because of when these run: every
one is nightly-only, so a swapped argument is seen once a night at best,
and the nightly truncated three times in the week before the two CUJ 1 halves
were registered into it. A test sees it on the pull request instead.

This reads the `.tf` files rather than mocking them: the subject is the call
site, and a test that restated the arguments would drift from the stack the
same way the three scaffolding copies drifted from each other.
"""

import re
import shlex
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PREBUILT = REPO_ROOT / "bench" / "tf" / "prebuilt"

# The call and its backslash continuations, up to the first unescaped newline.
CALL = re.compile(r"^\s*drift_build_payload\s*\\\n((?:.*\\\n)*.*)$", re.M)
# A Terraform interpolation. Rewritten to a token that KEEPS THE EXPRESSION,
# not to one opaque placeholder: most arguments at these call sites are
# interpolations, so collapsing them all to the same word makes every swap
# between two of them invisible -- which is the swap this file exists to
# catch, and which an earlier version of it could not see.
INTERPOLATION = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")


def _token(match):
    return "TF_" + re.sub(r"[^A-Za-z0-9_.]", "_", match.group(0)[2:-1])

# Position of each argument in build_payload.py's contract, after the output
# path that drift_build_payload supplies itself.
SCALARS = (
    "summary",
    "cluster",
    "project",
    "location",
    "principal",
    "user_agent",
    "verb",
    "method_name",
    "timestamp",
    "insert_id",
)
RESOURCE = ("group", "version", "namespace", "resource", "name")
TRAILING = ("join", "reconciled")
FIXED = len(SCALARS) + len(RESOURCE) + len(TRAILING)


def call_arguments(stack):
    """The words a stack passes to drift_build_payload, interpolations opaque."""
    text = (PREBUILT / stack / "main.tf").read_text(encoding="utf-8")
    match = CALL.search(text)
    assert match is not None, f"{stack} has no drift_build_payload call"
    body = match.group(1).replace("\\\n", " ")
    return shlex.split(INTERPOLATION.sub(_token, body))


def named(stack):
    """The call's arguments as {field: value}, plus the owner specs."""
    args = call_arguments(stack)
    fields = dict(zip(SCALARS + RESOURCE + TRAILING, args))
    return fields, args[FIXED:]


class EveryStackPassesWhatItsCaseNeeds(unittest.TestCase):
    STACKS = (
        "gitops-drift",
        "gitops-drift-deleted-policy",
        "gitops-drift-load-bearing",
        "gitops-drift-unmanaged-object",
    )

    def test_every_call_has_the_arity_the_builder_requires(self):
        for stack in self.STACKS:
            with self.subTest(stack=stack):
                args = call_arguments(stack)
                self.assertGreaterEqual(
                    len(args),
                    FIXED,
                    f"{stack} passes {len(args)} arguments, fewer than the {FIXED} "
                    "fixed ones; the builder would refuse it at apply",
                )

    def test_the_runtime_values_are_the_library_s_and_not_reinvented(self):
        """`$drift_project`, `$drift_changed_at` and `$drift_insert_id` are set by
        the library, and a stack that minted its own would break the card poll."""
        for stack in self.STACKS:
            with self.subTest(stack=stack):
                fields, _ = named(stack)
                self.assertEqual(fields["project"], "$drift_project")
                self.assertEqual(fields["timestamp"], "$drift_changed_at")
                self.assertEqual(fields["insert_id"], "$drift_insert_id")

    def test_the_verb_and_method_agree_with_the_resource(self):
        expected = {
            "gitops-drift": ("patch", "deployments", "apps"),
            "gitops-drift-deleted-policy": (
                "delete",
                "networkpolicies",
                "networking.k8s.io",
            ),
            "gitops-drift-load-bearing": ("patch", "deployments", "apps"),
            # The core group is empty on the record, as the detector maps it.
            "gitops-drift-unmanaged-object": ("patch", "namespaces", ""),
        }
        for stack, (verb, resource, group) in expected.items():
            with self.subTest(stack=stack):
                fields, _ = named(stack)
                self.assertEqual(fields["verb"], verb)
                self.assertEqual(fields["resource"], resource)
                self.assertEqual(fields["group"], group)
                self.assertIn(resource, fields["method_name"])
                self.assertIn(verb, fields["method_name"])

    def test_a_delete_sends_no_owners_and_a_patch_sends_two(self):
        """The shape the deleted-policy case turns on.

        payloadForEvent appends `owners` over event.Owners, which a delete
        leaves nil against a `json:"owners,omitempty"` field, so the detector
        emits no key. An owner argument here would make the builder emit one.
        """
        fields, owners = named("gitops-drift-deleted-policy")
        self.assertEqual(fields["join"], "no_object")
        self.assertEqual(owners, [], "a delete must pass no owner spec")

        for stack in ("gitops-drift", "gitops-drift-load-bearing"):
            with self.subTest(stack=stack):
                fields, owners = named(stack)
                self.assertEqual(fields["join"], "enriched")
                self.assertEqual(len(owners), 2)
                managers = [spec.split("|", 1)[0] for spec in owners]
                self.assertEqual(
                    managers,
                    ["TF_var.gitops_field_manager", "TF_var.drift_field_manager"],
                    "the GitOps manager owns the declared spec and the drift "
                    "manager the field it took; swapping them inverts the story "
                    "the card tells",
                )
                for spec in owners:
                    self.assertEqual(
                        len(spec.split("|")), 3, f"{spec!r} is not manager|operation|paths"
                    )

    def test_reconciled_is_the_literal_the_builder_accepts(self):
        """Anything else is refused at apply, minutes into a run."""
        for stack in self.STACKS:
            with self.subTest(stack=stack):
                fields, _ = named(stack)
                self.assertIn(fields["reconciled"], ("true", "false"))

    def test_the_call_is_found_in_every_stack(self):
        """Guards the regex: a reworded call would make every assertion vacuous."""
        for stack in self.STACKS:
            with self.subTest(stack=stack):
                self.assertGreater(len(call_arguments(stack)), 0)

    def test_each_interpolated_scalar_is_the_expression_its_field_wants(self):
        """The swap this file exists for: two adjacent free-form strings.

        `location`, `principal`, `cluster`, `namespace` and the object name are
        all interpolations, so nothing about their *values* distinguishes them
        at this layer. Swapping any two ships a record whose card reports the
        change on cluster `us-east4` at location `platform-agent-host`, which
        no deterministic check sees and a judge reads as the agent being wrong.
        """
        expected = {
            "gitops-drift": {
                "cluster": "TF_var.host_cluster_name",
                "location": "TF_var.host_cluster_location",
                "principal": "TF_var.drift_principal",
                "namespace": "TF_local.ns",
                "name": "TF_local.workload",
            },
            "gitops-drift-deleted-policy": {
                "cluster": "TF_var.host_cluster_name",
                "location": "TF_var.host_cluster_location",
                "principal": "TF_var.drift_principal",
                "namespace": "TF_local.ns",
                "name": "TF_local.policy",
            },
            "gitops-drift-load-bearing": {
                "cluster": "TF_var.host_cluster_name",
                "location": "TF_var.host_cluster_location",
                "principal": "TF_var.drift_principal",
                "namespace": "TF_local.ns",
                "name": "TF_local.workload",
            },
            # The namespace object itself: no namespace scope, and the
            # namespace is the name.
            "gitops-drift-unmanaged-object": {
                "cluster": "TF_var.host_cluster_name",
                "location": "TF_var.host_cluster_location",
                "principal": "TF_var.drift_principal",
                "namespace": "",
                "name": "TF_local.ns",
            },
        }
        for stack, fields_expected in expected.items():
            fields, _ = named(stack)
            for field, value in fields_expected.items():
                with self.subTest(stack=stack, field=field):
                    self.assertEqual(fields[field], value)


if __name__ == "__main__":
    unittest.main()
