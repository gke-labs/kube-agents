#!/usr/bin/env python3
"""Unit tests for the extractors in ``check_iac_parity.py``.

The checks themselves are self-verifying: they compare two real files, so a
broken comparison shows up as a CI failure on a repository that is actually in
sync. The extractors are not. Roughly two thirds of that script is bespoke
parsing — a YAML subset, Terraform variable blocks and lists, bash arrays,
``init_var`` defaults, LiteLLM aliases — and a parser that stops matching fails
loudly (``sys.exit``) while a parser that matches the *wrong text* does not: it
hands the comparison a plausible value, both sides "agree", and CI reports
parity across surfaces that have drifted. That is the failure these tests
exist to catch, and every case below is written against it.

Run with ``make test-python`` (the Makefile discovers ``scripts/test_*.py``) or
directly::

    python3 -m unittest scripts.test_check_iac_parity -v
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_iac_parity", Path(__file__).with_name("check_iac_parity.py")
)
parity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parity)

# Inside the repo root: the extractors' error messages call
# path.relative_to(REPO), which raises on a path from anywhere else.
FAKE = parity.REPO / "fake" / "source.tf"


class SimpleYamlTest(unittest.TestCase):
    def test_nests_by_indentation_and_strips_comments(self):
        tree = parity.simple_yaml(
            textwrap.dedent(
                """\
                # leading comment
                litellm:
                  image:
                    repository: ghcr.io/berriai/litellm
                    tag: v1.95.0 # trailing comment
                  replicaCount: 2
                platformAgent:
                  name: platform-agent
                """
            )
        )
        self.assertEqual(tree["litellm"]["image"]["tag"], "v1.95.0")
        self.assertEqual(tree["litellm"]["image"]["repository"], "ghcr.io/berriai/litellm")
        self.assertEqual(tree["litellm"]["replicaCount"], "2")
        self.assertEqual(tree["platformAgent"]["name"], "platform-agent")

    def test_dedent_pops_back_to_the_right_parent(self):
        """A sibling after a nested block must not be filed under the nephew."""
        tree = parity.simple_yaml(
            textwrap.dedent(
                """\
                operator:
                  image:
                    tag: a
                  replicaCount: 1
                litellm:
                  enabled: true
                """
            )
        )
        self.assertEqual(tree["operator"]["replicaCount"], "1")
        self.assertNotIn("replicaCount", tree["operator"]["image"])
        self.assertEqual(tree["litellm"]["enabled"], "true")

    def test_list_items_are_skipped_not_misfiled(self):
        tree = parity.simple_yaml(
            textwrap.dedent(
                """\
                operator:
                  extraEnv:
                    - name: FLUENT_BIT_IMAGE
                      value: registry/mirror
                  replicaCount: 1
                """
            )
        )
        self.assertEqual(tree["operator"]["replicaCount"], "1")
        self.assertNotIn("name", tree["operator"])

    def test_quotes_are_stripped(self):
        tree = parity.simple_yaml('a:\n  b: "quoted"\n  c: \'single\'\n')
        self.assertEqual(tree["a"]["b"], "quoted")
        self.assertEqual(tree["a"]["c"], "single")


class TerraformVariableTest(unittest.TestCase):
    def test_scalar_default(self):
        text = textwrap.dedent(
            """\
            variable "topic_name" {
              description = "Pub/Sub topic"
              type        = string
              default     = "platform-agent-chat-events"
            }
            """
        )
        self.assertEqual(
            parity.tf_variable_default(text, "topic_name", FAKE),
            "platform-agent-chat-events",
        )

    def test_list_default_drops_comments(self):
        text = textwrap.dedent(
            """\
            variable "project_roles" {
              type = list(string)
              default = [
                "roles/container.viewer",
                # a commented-out role must not be extracted
                # "roles/owner",
                "roles/logging.viewer",
              ]
            }
            """
        )
        self.assertEqual(
            parity.tf_variable_default(text, "project_roles", FAKE),
            ["roles/container.viewer", "roles/logging.viewer"],
        )

    def test_validation_mentioning_default_does_not_win(self):
        """The regression the anchoring exists for.

        An unanchored `default\\s*=` also matches inside a validation block. The
        extractor would then return "must not be default = unset", the
        comparison against the scripts would fail on a repository that is
        perfectly in sync, and the fix would look like a source-of-truth change.
        """
        text = textwrap.dedent(
            """\
            variable "mode" {
              type    = string
              default = "quiet"

              validation {
                condition     = var.mode != ""
                error_message = "mode must not be default = unset."
              }
            }
            """
        )
        self.assertEqual(parity.tf_variable_default(text, "mode", FAKE), "quiet")

    def test_block_scan_stops_at_its_own_closing_brace(self):
        """A variable with no default must not borrow the next variable's."""
        text = textwrap.dedent(
            """\
            variable "first" {
              type = string
            }

            variable "second" {
              type    = string
              default = "second-value"
            }
            """
        )
        with self.assertRaises(SystemExit):
            parity.tf_variable_default(text, "first", FAKE)

    def test_missing_variable_exits(self):
        with self.assertRaises(SystemExit):
            parity.tf_variable_default('variable "other" {\n  type = string\n}\n', "absent", FAKE)


class TerraformListTest(unittest.TestCase):
    def test_preserves_order_and_ignores_commented_entries(self):
        text = textwrap.dedent(
            """\
            locals {
              gke_admin_roles = [
                "roles/container.clusterAdmin",
                # The agent must not administer the audit-log sink.
                # "roles/logging.admin",
                "roles/logging.viewer",
              ]
            }
            """
        )
        self.assertEqual(
            parity.tf_list(text, "gke_admin_roles", FAKE),
            ["roles/container.clusterAdmin", "roles/logging.viewer"],
        )

    def test_missing_list_exits(self):
        with self.assertRaises(SystemExit):
            parity.tf_list("locals {\n}\n", "absent_roles", FAKE)


class ShellExtractorTest(unittest.TestCase):
    def test_bash_array_ignores_comments(self):
        text = textwrap.dedent(
            """\
            get_roles() {
              local read_only_roles=(
                "roles/container.viewer"
                # "roles/owner"
                "roles/logging.viewer"
              )
            }
            """
        )
        self.assertEqual(
            parity.bash_array(text, "local read_only_roles", FAKE),
            ["roles/container.viewer", "roles/logging.viewer"],
        )

    def test_init_var_default(self):
        text = 'init_var "BACKUP_CRON_SCHEDULE" "0 2 * * *" "Enter cron"\n'
        self.assertEqual(
            parity.init_var_default(text, "BACKUP_CRON_SCHEDULE", FAKE), "0 2 * * *"
        )

    def test_init_var_empty_default_is_a_value_not_a_miss(self):
        text = 'init_var "GITHUB_ORG" "" "Enter GitHub Organization"\n'
        self.assertEqual(parity.init_var_default(text, "GITHUB_ORG", FAKE), "")

    def test_shell_assignment_accepts_repeated_agreeing_definitions(self):
        """common.sh exports its identifiers in several branches."""
        text = textwrap.dedent(
            """\
            if a; then
              export NAMESPACE="kubeagents-system"
            else
              export NAMESPACE="kubeagents-system"
            fi
            """
        )
        self.assertEqual(parity.shell_assignment(text, "NAMESPACE", FAKE), "kubeagents-system")

    def test_shell_assignment_rejects_disagreeing_definitions(self):
        """Two values means no single value for the other surfaces to mirror."""
        text = textwrap.dedent(
            """\
            export NAMESPACE="kubeagents-system"
            export NAMESPACE="something-else"
            """
        )
        with self.assertRaises(SystemExit):
            parity.shell_assignment(text, "NAMESPACE", FAKE)

    def test_shell_assignment_missing_exits(self):
        with self.assertRaises(SystemExit):
            parity.shell_assignment("export OTHER=1\n", "NAMESPACE", FAKE)


class ModelNamesTest(unittest.TestCase):
    def test_kustomize_placeholder_normalises(self):
        text = textwrap.dedent(
            """\
            model_list:
              - model_name: model-default
              - model_name: hermes-agent
              - model_name: ${MODEL_DEFAULT_NAME}
            """
        )
        self.assertEqual(
            parity.model_names(text, FAKE),
            ["model-default", "hermes-agent", parity.MODEL_PLACEHOLDER],
        )

    def test_chart_placeholder_normalises_to_the_same_token(self):
        """Both spellings the chart has used must compare equal to kustomize's."""
        for spelling in ("{{ .model }}", "{{ $model }}"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    parity.model_names(f"  - model_name: {spelling}\n", FAKE),
                    [parity.MODEL_PLACEHOLDER],
                )

    def test_no_aliases_exits_rather_than_reporting_none(self):
        """The config moving to another file must not read as 'serves nothing'."""
        with self.assertRaises(SystemExit):
            parity.model_names("apiVersion: v1\nkind: ConfigMap\n", FAKE)


class CacheControlPointsTest(unittest.TestCase):
    """The prompt-cache breakpoints, which the chart repeats verbatim.

    The chart's copy sits inside a `define`, so the extractor has to end the
    block on the template's own `{{- end }}` as readily as on a sibling YAML key.
    """

    KUSTOMIZE = textwrap.dedent(
        """\
        router_settings:
          default_litellm_params:
            cache_control_injection_points:
              - location: message
                role: system
                control:
                  type: ephemeral
                  ttl: 1h
              - location: message
                index: -1
        general_settings:
          master_key: sk-1234
        """
    )

    CHART = textwrap.dedent(
        """\
        {{- define "kube-agents.litellmConfig" -}}
        router_settings:
          default_litellm_params:
            cache_control_injection_points:
              # Same points, one comment and a template terminator later.
              - control:
                  ttl: 1h
                  type: ephemeral
                role: system
                location: message
              - location: message
                index: -1
        {{- end }}
        """
    )

    def test_flattens_each_point_and_stops_at_the_next_key(self):
        self.assertEqual(
            parity.cache_control_points(self.KUSTOMIZE, FAKE),
            ["location=message role=system ttl=1h type=ephemeral", "index=-1 location=message"],
        )

    def test_chart_spelling_compares_equal(self):
        """Key order within a point is not a difference; the two must match."""
        self.assertEqual(
            parity.cache_control_points(self.CHART, FAKE),
            parity.cache_control_points(self.KUSTOMIZE, FAKE),
        )

    def test_point_order_is_a_difference(self):
        """Breakpoints are positional: the same two in the other order differ."""
        reordered = textwrap.dedent(
            """\
            cache_control_injection_points:
              - location: message
                index: -1
              - location: message
                role: system
                control:
                  type: ephemeral
                  ttl: 1h
            """
        )
        self.assertNotEqual(
            parity.cache_control_points(reordered, FAKE),
            parity.cache_control_points(self.KUSTOMIZE, FAKE),
        )

    def test_no_block_exits_rather_than_reporting_none(self):
        """Caching dropped from both surfaces must fail, not compare equal."""
        with self.assertRaises(SystemExit):
            parity.cache_control_points("litellm_settings:\n  callbacks: []\n", FAKE)


class DigTest(unittest.TestCase):
    def test_missing_key_exits(self):
        with self.assertRaises(SystemExit):
            parity.dig({"litellm": {"image": {}}}, "litellm.image.tag")

    def test_walks_nested_path(self):
        self.assertEqual(parity.dig({"a": {"b": {"c": "v"}}}, "a.b.c"), "v")


class ModelDefaultsTest(unittest.TestCase):
    """check_model_defaults against common.sh's `case` fall-through.

    default_model_for_provider names only the providers whose default differs
    from the catch-all; everything else lands on `*)`. Reading that arm as an
    alias for one named provider made every OTHER fall-through provider look
    absent from common.sh — which is how adding vertex_ai to the chart produced
    "chart knows provider 'vertex_ai', common.sh does not" about a provider the
    scripts have validated and handled all along.
    """

    COMMON_SH = textwrap.dedent(
        """\
        default_model_for_provider() {
          case "$1" in
            chatgpt | openai) echo "gpt-5.4" ;;
            anthropic) echo "claude-sonnet-4-5-20250929" ;;
            *) echo "gemini-3.5-flash" ;;
          esac
        }
        is_valid_model_provider() {
          [[ "${1:-}" =~ ^(gemini|vertex_ai|anthropic|chatgpt|openai)$ ]]
        }
        """
    )

    def _run(self, chart_dict: str) -> list[tuple[str, str]]:
        chart = '{{- $defaultModels := dict %s }}' % chart_dict
        f = parity.Failures()
        with unittest.mock.patch.object(
            parity, "read", side_effect=lambda path: self.COMMON_SH if path == parity.COMMON_SH else chart
        ):
            parity.check_model_defaults(f)
        return list(f)

    def test_fall_through_providers_agree(self):
        """Neither gemini nor vertex_ai is named in the case; both resolve to `*`."""
        self.assertEqual(
            self._run('"gemini" "gemini-3.5-flash" "vertex_ai" "gemini-3.5-flash"'), []
        )

    def test_explicitly_cased_provider_still_compared(self):
        self.assertEqual(self._run('"anthropic" "claude-sonnet-4-5-20250929"'), [])

    def test_drift_on_a_fall_through_provider_is_caught(self):
        """The regression the `*`-as-gemini reading would have hidden."""
        failures = self._run('"vertex_ai" "some-other-model"')
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("vertex_ai", failures[0][1])

    def test_provider_the_scripts_reject_is_caught(self):
        """`*` answers for any string, so this can only come from the validator."""
        failures = self._run('"nosuchprovider" "gemini-3.5-flash"')
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("common.sh does not", failures[0][1])


class WebhookParityTest(unittest.TestCase):
    """The two webhook checks, exercised against drift they must catch.

    Both compare a real file against a real file, so the end-to-end test only
    proves they pass while the tree is in sync — it cannot distinguish a
    working comparison from one whose regex stopped matching and now compares
    two empty sets. These drive the mismatch branch directly.
    """

    def test_cert_manager_version_comes_from_the_inventory(self):
        """The inventory pin is what Terraform is compared against.

        provision_03 no longer spells a version out — it builds the release URL
        from images.json — so the check reads the pin there. Two things have to
        hold for that to mean anything: the pin exists and matches Terraform,
        and the script really is deriving its URL from it. If the script went
        back to a hard-coded version, this check would compare Terraform
        against a pin nothing installs and still pass.
        """
        _, version = parity.inventory_pin("cert-manager-controller")
        self.assertRegex(version, r"^v[\d.]+$")
        self.assertEqual(
            version,
            parity.tf_variable_default(
                parity.read(parity.TF_FULL_INSTALL_VARS),
                "cert_manager_version",
                parity.TF_FULL_INSTALL_VARS,
            ),
        )

        script = parity.read(parity.PROVISION_03)
        self.assertRegex(
            script,
            r"cert-manager/releases/download/\$\{version\}/cert-manager\.yaml",
            "provision_03 no longer builds the release URL from a version variable",
        )
        self.assertRegex(
            script,
            r'select\(\.name == "cert-manager-controller"\)',
            "provision_03 no longer reads the cert-manager pin from images.json",
        )

    def test_webhook_paths_are_found_on_both_surfaces(self):
        """An empty set on either side would make the comparison vacuous."""
        kustomize = set(
            re.findall(r"^\s*path:\s*(/\S+)", parity.read(parity.WEBHOOK_MANIFESTS), re.M)
        )
        chart = set(
            re.findall(r"^\s*path:\s*(/\S+)", parity.read(parity.CHART_WEBHOOKS), re.M)
        )
        self.assertTrue(kustomize, "no admission paths parsed from the kustomize manifests")
        self.assertTrue(chart, "no admission paths parsed from the chart template")
        self.assertEqual(kustomize, chart)

    def test_a_renamed_chart_path_is_reported(self):
        """Drive the mismatch branch without editing the tree under test.

        The chart constant is redirected at a scratch copy rather than the real
        template being rewritten and restored: a restore that does not run —
        an interrupt, a crash in the assertion — would leave a broken chart in
        the working tree and every later check reporting drift that is this
        test's fault.
        """
        original = parity.CHART_WEBHOOKS.read_text(encoding="utf-8")
        patched = original.replace(
            "/validate-kubeagents-x-k8s-io-v1alpha1-platformagent",
            "/validate-kubeagents-x-k8s-io-v1alpha1-renamed",
        )
        self.assertNotEqual(original, patched, "the path this test rewrites has moved")

        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "operator-webhooks.yaml"
            scratch.write_text(patched, encoding="utf-8")
            with unittest.mock.patch.object(parity, "CHART_WEBHOOKS", scratch):
                failures = parity.Failures()
                parity.check_webhook_paths(failures)
        self.assertEqual([name for name, _ in failures], ["webhook-paths"])


class HclExtractorTest(unittest.TestCase):
    def test_string_local_reads_only_the_named_assignment(self):
        text = textwrap.dedent(
            """\
            locals {
              other_ksa   = "wrong-name"
              litellm_ksa = "kubeagents-litellm"
            }
            """
        )
        self.assertEqual(
            parity.hcl_string_local(text, "litellm_ksa", FAKE), "kubeagents-litellm"
        )

    def test_string_local_exits_when_absent(self):
        with self.assertRaises(SystemExit):
            parity.hcl_string_local("locals {}\n", "litellm_ksa", FAKE)

    def test_resource_buckets_keeps_requests_and_limits_apart(self):
        text = textwrap.dedent(
            """\
            locals {
              cert_manager_resources = {
                requests = {
                  cpu    = "10m"
                  memory = "32Mi"
                }
                limits = {
                  cpu    = "100m"
                  memory = "128Mi"
                }
              }
            }
            """
        )
        buckets = parity.hcl_resource_buckets(text, "cert_manager_resources", FAKE)
        self.assertEqual(buckets["requests"], {"cpu": "10m", "memory": "32Mi"})
        self.assertEqual(buckets["limits"], {"cpu": "100m", "memory": "128Mi"})

    def test_resource_buckets_exits_on_a_missing_bucket(self):
        text = 'cert_manager_resources = {\n    requests = { cpu = "10m" }\n  }\n'
        with self.assertRaises(SystemExit):
            parity.hcl_resource_buckets(text, "cert_manager_resources", FAKE)


class CertManagerPatchTest(unittest.TestCase):
    def test_a_changed_terraform_quota_is_reported(self):
        """Redirect the composition at a scratch copy with a drifted limit."""
        real = parity.read(parity.TF_FULL_INSTALL)
        drifted = real.replace('memory = "128Mi"', 'memory = "256Mi"', 1)
        self.assertNotEqual(real, drifted)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "main.tf"
            scratch.write_text(drifted, encoding="utf-8")
            with unittest.mock.patch.object(parity, "TF_FULL_INSTALL", scratch):
                failures = parity.Failures()
                parity.check_cert_manager_resources(failures)
        self.assertEqual([name for name, _ in failures], ["cert-manager-resources"])


class HostLabelTest(unittest.TestCase):
    def test_a_renamed_label_key_is_reported(self):
        real = parity.read(parity.TF_FULL_INSTALL)
        drifted = real.replace('"kube-agents-host" = "true"', '"kube-agents" = "true"')
        self.assertNotEqual(real, drifted)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "main.tf"
            scratch.write_text(drifted, encoding="utf-8")
            with unittest.mock.patch.object(parity, "TF_FULL_INSTALL", scratch):
                failures = parity.Failures()
                parity.check_host_label(failures)
        self.assertEqual([name for name, _ in failures], ["host-label"])


class EndToEndTest(unittest.TestCase):
    def test_every_check_passes_against_the_real_tree(self):
        """The checks are only meaningful if the repository itself is in sync."""
        failures = parity.Failures()
        for check in parity.CHECKS:
            check(failures)
        self.assertEqual(list(failures), [], f"parity failures: {list(failures)}")


if __name__ == "__main__":
    unittest.main()
