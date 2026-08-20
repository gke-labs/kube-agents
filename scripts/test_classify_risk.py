"""Tests for classify_risk.py: rule evaluation, tier arithmetic, the declared-
vs-computed parse, and that the repository's own rules file loads and
classifies the shapes it was written for.

Run: cd scripts && python3 -m unittest test_classify_risk
"""

import json
import os
import unittest

import classify_risk


def _file(filename, patch=None):
    entry = {"filename": filename, "status": "modified"}
    if patch is not None:
        entry["patch"] = patch
    return entry


def _config(*rules, default_tier="medium"):
    config = {"default_tier": default_tier, "rules": list(rules)}
    classify_risk.validate_rules(config)
    return config


REPO_RULES = os.path.join(os.path.dirname(__file__), "..", ".github", "risk-rules.yml")


class ValidateRulesTest(unittest.TestCase):
    def test_low_rule_without_only_match_is_refused(self):
        with self.assertRaisesRegex(ValueError, "must use only_match"):
            _config({"id": "x", "tier": "low", "why": "w", "match": ["docs/**"]})

    def test_unknown_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            _config({"id": "x", "tier": "high", "why": "w", "match": ["a/**"], "paths": ["b"]})

    def test_rule_without_selector_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no selector"):
            _config({"id": "x", "tier": "high", "why": "w"})

    def test_only_match_combined_with_patch_contains_is_refused(self):
        with self.assertRaisesRegex(ValueError, "combines"):
            _config(
                {"id": "x", "tier": "high", "why": "w", "only_match": ["a/**"], "patch_contains": ["b"]}
            )

    def test_duplicate_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, "appears twice"):
            _config(
                {"id": "x", "tier": "high", "why": "w", "match": ["a/**"]},
                {"id": "x", "tier": "low", "why": "w", "only_match": ["b/**"]},
            )

    def test_bad_tier_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not one of"):
            _config({"id": "x", "tier": "critical", "why": "w", "match": ["a/**"]})

    def test_bad_default_tier_is_refused(self):
        with self.assertRaisesRegex(ValueError, "default_tier"):
            _config(
                {"id": "x", "tier": "high", "why": "w", "match": ["a/**"]},
                default_tier="none",
            )

    def test_unsupported_glob_is_refused(self):
        with self.assertRaises(ValueError):
            _config({"id": "x", "tier": "high", "why": "w", "match": ["a/{b,c}/**"]})


class RuleTriggerTest(unittest.TestCase):
    def test_match_triggers_on_any_file(self):
        rule = {"id": "x", "tier": "high", "why": "w", "match": [".github/workflows/**"]}
        files = [_file(".github/workflows/ci.yml"), _file("README.md")]
        self.assertEqual(classify_risk.rule_trigger(rule, files), [".github/workflows/ci.yml"])

    def test_match_and_patch_contains_need_the_same_file(self):
        rule = {
            "id": "x",
            "tier": "high",
            "why": "w",
            "match": ["deploy/docker/Dockerfile"],
            "patch_contains": ["^\\+\\s*(RUN|COPY)\\b"],
        }
        added = [_file("deploy/docker/Dockerfile", patch="+RUN apt-get install jq")]
        context_only = [_file("deploy/docker/Dockerfile", patch=" RUN existing\n+ENV FOO=1")]
        elsewhere = [_file("scripts/x.sh", patch="+RUN not the dockerfile")]
        self.assertEqual(classify_risk.rule_trigger(rule, added), ["deploy/docker/Dockerfile"])
        self.assertIsNone(classify_risk.rule_trigger(rule, context_only))
        self.assertIsNone(classify_risk.rule_trigger(rule, elsewhere))

    def test_missing_patch_never_satisfies_patch_contains(self):
        rule = {"id": "x", "tier": "medium", "why": "w", "patch_contains": ["anything"]}
        self.assertIsNone(classify_risk.rule_trigger(rule, [_file("big.bin")]))

    def test_only_match_requires_every_file(self):
        rule = {"id": "x", "tier": "low", "why": "w", "only_match": ["docs/site/**", "README.md"]}
        all_docs = [_file("docs/site/src/a.md"), _file("README.md")]
        mixed = all_docs + [_file("agents/platform/skills/x/SKILL.md")]
        self.assertEqual(len(classify_risk.rule_trigger(rule, all_docs)), 2)
        self.assertIsNone(classify_risk.rule_trigger(rule, mixed))
        self.assertIsNone(classify_risk.rule_trigger(rule, []))

    def test_all_of_requires_every_group(self):
        rule = {
            "id": "x",
            "tier": "medium",
            "why": "w",
            "all_of": [["k8s-operator/scripts/**"], ["charts/**", "terraform/**"]],
        }
        both = [_file("k8s-operator/scripts/common.sh"), _file("charts/kube-agents/values.yaml")]
        one = [_file("k8s-operator/scripts/common.sh")]
        self.assertEqual(
            classify_risk.rule_trigger(rule, both),
            ["charts/kube-agents/values.yaml", "k8s-operator/scripts/common.sh"],
        )
        self.assertIsNone(classify_risk.rule_trigger(rule, one))


class ClassifyTest(unittest.TestCase):
    def test_highest_tier_wins(self):
        config = _config(
            {"id": "med", "tier": "medium", "why": "w", "match": ["agents/**/*.md"]},
            {"id": "hi", "tier": "high", "why": "w", "match": [".github/workflows/**"]},
        )
        files = [_file("agents/platform/skills/x/SKILL.md"), _file(".github/workflows/ci.yml")]
        result = classify_risk.classify(config, files)
        self.assertEqual(result["tier"], "high")
        self.assertEqual([entry["id"] for entry in result["rules"]], ["med", "hi"])
        self.assertFalse(result["default_applied"])

    def test_nothing_matched_falls_back_to_default(self):
        config = _config({"id": "hi", "tier": "high", "why": "w", "match": [".github/workflows/**"]})
        result = classify_risk.classify(config, [_file("bench/run.py")])
        self.assertEqual(result["tier"], "medium")
        self.assertTrue(result["default_applied"])
        self.assertEqual(result["rules"], [])


class DeclaredTierTest(unittest.TestCase):
    BODY = "## Summary\n\nwords\n\n## Risk & Rollout\n\n%s\n"

    def test_template_low_risk_phrase(self):
        body = self.BODY % "Low risk, no runtime code paths touched."
        self.assertEqual(classify_risk.declared_tier(body), "low")

    def test_highest_mentioned_tier_counts(self):
        body = self.BODY % "Mostly low risk, but the migration itself is high risk."
        self.assertEqual(classify_risk.declared_tier(body), "high")

    def test_no_section_is_none(self):
        self.assertIsNone(classify_risk.declared_tier("## Summary\n\nlow risk change"))

    def test_bare_leading_verdict(self):
        # The shapes real pull requests write: #770, #733, #721.
        self.assertEqual(classify_risk.declared_tier(self.BODY % "Low. No runtime code paths."), "low")
        self.assertEqual(
            classify_risk.declared_tier(self.BODY % "Moderate, and concentrated in one place."),
            "medium",
        )
        self.assertEqual(
            classify_risk.declared_tier(self.BODY % "- **Risk: none to any running system.**"), "low"
        )

    def test_leading_compound_word_is_not_a_verdict(self):
        body = self.BODY % "High-level summary: nothing changes at runtime."
        self.assertIsNone(classify_risk.declared_tier(body))

    def test_crlf_body_parses(self):
        body = (self.BODY % "Low risk, nothing at runtime.").replace("\n", "\r\n")
        self.assertEqual(classify_risk.declared_tier(body), "low")

    def test_section_without_tier_word_is_none(self):
        body = self.BODY % "Reverting this commit removes the page."
        self.assertIsNone(classify_risk.declared_tier(body))

    def test_mismatch_only_for_declared_low_computed_high(self):
        config = _config({"id": "hi", "tier": "high", "why": "w", "match": [".github/workflows/**"]})
        files = [_file(".github/workflows/ci.yml")]
        low = classify_risk.build_result(config, files, self.BODY % "Low risk.")
        none = classify_risk.build_result(config, files, "")
        self.assertTrue(low["mismatch"])
        self.assertFalse(none["mismatch"])


class RenderingTest(unittest.TestCase):
    def _result(self):
        config = _config({"id": "hi", "tier": "high", "why": "w", "match": [".github/workflows/**"]})
        return classify_risk.build_result(
            config,
            [_file(".github/workflows/ci.yml")],
            "## Risk & Rollout\n\nLow risk.",
            pr_number=1,
            head_sha="a" * 40,
        )

    def test_title_leads_with_the_mismatch(self):
        self.assertIn("declares low", classify_risk.check_run_title(self._result()))

    def test_summary_carries_a_parseable_json_block(self):
        summary = classify_risk.check_run_summary(self._result())
        block = summary.split("```json\n", 1)[1].split("\n```", 1)[0]
        parsed = json.loads(block)
        self.assertEqual(parsed["schema"], classify_risk.SCHEMA_VERSION)
        self.assertEqual(parsed["tier"], "high")
        self.assertTrue(parsed["mismatch"])


class RepositoryRulesTest(unittest.TestCase):
    """The committed rules file, against the shapes it was written for."""

    @classmethod
    def setUpClass(cls):
        cls.config = classify_risk.load_rules(REPO_RULES)

    def classify(self, files):
        return classify_risk.classify(self.config, files)

    def test_docs_site_only_is_low(self):
        result = self.classify([_file("docs/site/src/content/docs/concepts.md"), _file("README.md")])
        self.assertEqual(result["tier"], "low")

    def test_skill_md_is_not_docs(self):
        result = self.classify(
            [_file("docs/site/src/content/docs/concepts.md"), _file("agents/cluster/skills/x/SKILL.md")]
        )
        self.assertEqual(result["tier"], "medium")
        self.assertIn("agent-behavior-is-config-not-docs", [entry["id"] for entry in result["rules"]])

    def test_workflow_edit_is_high(self):
        self.assertEqual(self.classify([_file(".github/workflows/tests.yml")])["tier"], "high")

    def test_the_rules_file_itself_is_high(self):
        self.assertEqual(self.classify([_file(".github/risk-rules.yml")])["tier"], "high")

    def test_added_iam_role_is_high(self):
        files = [_file("terraform/kube-agents-iam/main.tf", patch='+    "roles/container.admin",')]
        self.assertEqual(self.classify(files)["tier"], "high")

    def test_terraform_without_role_change_is_default_medium(self):
        files = [_file("terraform/gke-cluster/variables.tf", patch="+variable \"zone\" {}")]
        result = self.classify(files)
        self.assertEqual(result["tier"], "medium")
        self.assertTrue(result["default_applied"])

    def test_deleted_go_test_is_medium(self):
        files = [_file("k8s-operator/internal/controller/thing_test.go", patch="-func TestReconcile(t *testing.T) {")]
        result = self.classify(files)
        self.assertIn("test-deletion", [entry["id"] for entry in result["rules"]])


if __name__ == "__main__":
    unittest.main()
