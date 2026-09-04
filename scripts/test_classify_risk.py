"""Tests for classify_risk.py: rule evaluation, tier arithmetic, the declared-
vs-computed parse, and that the repository's own rules file loads and
classifies the shapes it was written for.

Run: cd scripts && python3 -m unittest test_classify_risk
"""

import json
import os
import unittest
import urllib.error

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

    def test_rename_is_classified_by_both_paths(self):
        # Renaming a workflow into docs/ must not buy the low tier.
        rename = {
            "filename": "docs/site/src/old-ci.txt",
            "status": "renamed",
            "previous_filename": ".github/workflows/ci.yml",
        }
        high = {"id": "hi", "tier": "high", "why": "w", "match": [".github/workflows/**"]}
        low = {"id": "lo", "tier": "low", "why": "w", "only_match": ["docs/site/**"]}
        self.assertEqual(classify_risk.rule_trigger(high, [rename]), ["docs/site/src/old-ci.txt"])
        self.assertIsNone(classify_risk.rule_trigger(low, [rename]))

    def test_all_of_requires_every_group(self):
        rule = {
            "id": "x",
            "tier": "medium",
            "why": "w",
            "all_of": [["scripts/installer/**"], ["charts/**", "terraform/**"]],
        }
        both = [_file("scripts/installer/common.sh"), _file("charts/kube-agents/values.yaml")]
        one = [_file("scripts/installer/common.sh")]
        self.assertEqual(
            classify_risk.rule_trigger(rule, both),
            ["charts/kube-agents/values.yaml", "scripts/installer/common.sh"],
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

    def test_incomplete_file_list_never_reaches_low(self):
        # The API stops at 3000 files; only_match over a truncated list would
        # be a claim about files nobody read.
        config = _config({"id": "lo", "tier": "low", "why": "w", "only_match": ["docs/site/**"]})
        files = [_file("docs/site/src/a.md")]
        self.assertEqual(classify_risk.classify(config, files, complete=True)["tier"], "low")
        self.assertEqual(classify_risk.classify(config, files, complete=False)["tier"], "medium")

    def test_rule_file_list_is_capped_but_counted(self):
        config = _config({"id": "lo", "tier": "low", "why": "w", "only_match": ["docs/site/**"]})
        files = [_file(f"docs/site/src/{i}.md") for i in range(60)]
        entry = classify_risk.classify(config, files)["rules"][0]
        self.assertEqual(len(entry["files"]), classify_risk.RULE_FILES_LISTED)
        self.assertEqual(entry["file_count"], 60)


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

    def test_unedited_template_comment_declares_nothing(self):
        # The template's own HTML comment contains "Low risk, no runtime code
        # paths touched"; leaving it in place is not a declaration.
        body = self.BODY % (
            '<!--\nBlast radius, any new failure mode, how to revert. "Low risk, no runtime\n'
            'code paths touched" is a complete answer when it is true.\n-->'
        )
        self.assertIsNone(classify_risk.declared_tier(body))

    def test_common_negations_do_not_declare(self):
        self.assertIsNone(classify_risk.declared_tier(self.BODY % "This is not a low-risk change."))
        self.assertIsNone(classify_risk.declared_tier(self.BODY % "The risk is not low here."))

    def test_no_risk_phrasing_declares_low(self):
        self.assertEqual(
            classify_risk.declared_tier(self.BODY % "No risk to running systems."), "low"
        )

    def test_determiner_no_near_risk_is_not_a_declaration(self):
        # "no" declares only in the literal "no risk" collocation; as a
        # determiner in the same clause as "risk" it says nothing about tier.
        body = self.BODY % (
            "Risk: this rewrites the IAM bundle; there is no automated rollback."
        )
        self.assertIsNone(classify_risk.declared_tier(body))
        self.assertIsNone(
            classify_risk.declared_tier(self.BODY % "The risk is contained; no migration needed.")
        )

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


class SummaryHostileInputTest(unittest.TestCase):
    def test_filename_cannot_forge_a_json_fence(self):
        # Git permits backticks and newlines in paths; a crafted name must not
        # open a fenced block ahead of the real one.
        hostile = 'a``` ```json\n{"tier": "low"}\n```b.md'
        config = _config({"id": "hi", "tier": "high", "why": "w", "match": ["a*"]})
        result = classify_risk.build_result(config, [_file(hostile)], "")
        summary = classify_risk.check_run_summary(result)
        # A fence marker only counts at the start of a line (json.dumps keeps
        # string content mid-line by escaping newlines). Exactly one fenced
        # block must exist, it must be the JSON one, and it must carry the
        # real tier.
        fences = [line for line in summary.splitlines() if line.startswith("```")]
        self.assertEqual(fences, ["```json", "```"])
        block = summary.split("```json\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(json.loads(block)["tier"], "high")

    def test_wide_pr_summary_fits_the_api_limit(self):
        # GitHub caps output.summary at 65535 characters; an uncapped file
        # list 422s the check-run POST on exactly the widest pull requests.
        config = _config({"id": "lo", "tier": "low", "why": "w", "only_match": ["docs/site/**"]})
        files = [_file(f"docs/site/src/content/docs/some/long/path/page-{i}.md") for i in range(1500)]
        summary = classify_risk.check_run_summary(classify_risk.build_result(config, files, ""))
        self.assertLess(len(summary), 65535)


class FakeAPI:
    repo = "gke-labs/kube-agents"

    def __init__(self, fail=None, check_runs=None):
        self.calls = []
        self.fail = fail or {}
        self.check_runs = check_runs or []

    def _record(self, method, path, body=None):
        self.calls.append((method, path) if body is None else (method, path, body))
        status = self.fail.get((method, path))
        if status:
            raise urllib.error.HTTPError(path, status, "", {}, None)

    def get(self, path):
        self._record("GET", path)
        return {"check_runs": self.check_runs}

    def post(self, path, body):
        self._record("POST", path, body)

    def patch(self, path, body):
        self._record("PATCH", path, body)

    def delete(self, path):
        self._record("DELETE", path)


class SyncLabelsTest(unittest.TestCase):
    def test_swaps_the_stale_risk_label(self):
        api = FakeAPI()
        classify_risk.sync_labels(api, 7, "high", ["risk:low", "unrelated"])
        self.assertEqual(
            api.calls,
            [
                ("DELETE", "/repos/gke-labs/kube-agents/issues/7/labels/risk%3Alow"),
                (
                    "POST",
                    "/repos/gke-labs/kube-agents/labels",
                    {
                        "name": "risk:high",
                        "color": classify_risk.LABEL_COLORS["high"],
                        "description": f"computed by {classify_risk.CHECK_NAME} (#818)",
                    },
                ),
                ("POST", "/repos/gke-labs/kube-agents/issues/7/labels", {"labels": ["risk:high"]}),
            ],
        )

    def test_noop_when_the_label_is_already_right(self):
        api = FakeAPI()
        classify_risk.sync_labels(api, 7, "medium", ["risk:medium", "unrelated"])
        self.assertEqual(api.calls, [])

    def test_existing_repository_label_is_tolerated(self):
        api = FakeAPI(fail={("POST", "/repos/gke-labs/kube-agents/labels"): 422})
        classify_risk.sync_labels(api, 7, "low", [])
        self.assertEqual(api.calls[-1][1], "/repos/gke-labs/kube-agents/issues/7/labels")

    def test_hand_created_label_with_a_space_is_deletable(self):
        api = FakeAPI()
        classify_risk.sync_labels(api, 7, "high", ["risk: high", "risk:high"])
        self.assertEqual(
            api.calls, [("DELETE", "/repos/gke-labs/kube-agents/issues/7/labels/risk%3A%20high")]
        )


class PostCheckRunTest(unittest.TestCase):
    def _result(self):
        config = _config({"id": "hi", "tier": "high", "why": "w", "match": [".github/workflows/**"]})
        return classify_risk.build_result(config, [_file(".github/workflows/ci.yml")], "")

    def test_first_classification_creates_the_run(self):
        api = FakeAPI(check_runs=[])
        classify_risk.post_check_run(api, "a" * 40, self._result())
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("POST", "/repos/gke-labs/kube-agents/check-runs"))
        self.assertEqual(body["head_sha"], "a" * 40)

    def test_reclassifying_the_same_sha_updates_in_place(self):
        # edited/reopened re-classify the same commit; stacking a fresh run
        # would leave the stale verdict beside the corrected one.
        api = FakeAPI(
            check_runs=[
                {"id": 5, "app": {"slug": "github-actions"}, "external_id": "risk-classify"},
                {"id": 9, "app": {"slug": "github-actions"}, "external_id": "risk-classify"},
            ]
        )
        classify_risk.post_check_run(api, "a" * 40, self._result())
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/repos/gke-labs/kube-agents/check-runs/9"))
        self.assertNotIn("head_sha", body)

    def test_another_apps_run_with_the_same_name_is_not_touched(self):
        # A name is not an identity, and PATCHing another app's run 403s.
        api = FakeAPI(check_runs=[{"id": 5, "app": {"slug": "some-other-app"}}])
        classify_risk.post_check_run(api, "a" * 40, self._result())
        self.assertEqual(api.calls[-1][0:2], ("POST", "/repos/gke-labs/kube-agents/check-runs"))

    def test_the_workflow_jobs_own_check_run_is_not_touched(self):
        # Actions creates a check run for the job itself: same app slug, same
        # SHA, and -- until the job was renamed -- the same name. Its
        # external_id carries the runner's job GUID, which is how the script's
        # runs are told apart. Matching it here would PATCH the job's run and
        # leave the stale verdict standing (853-F1); the newest-id decoy is the
        # exact shape of a re-classification, where the current run's job
        # check run postdates the script's earlier POST.
        api = FakeAPI(
            check_runs=[
                {"id": 5, "app": {"slug": "github-actions"}, "external_id": "risk-classify"},
                {
                    "id": 9,
                    "app": {"slug": "github-actions"},
                    "external_id": "0198f2f3-ab-job-guid",
                },
            ]
        )
        classify_risk.post_check_run(api, "a" * 40, self._result())
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/repos/gke-labs/kube-agents/check-runs/5"))
        self.assertEqual(body["external_id"], "risk-classify")

    def test_a_pre_external_id_run_falls_back_to_post(self):
        # A run this script created before it stamped external_id no longer
        # matches. One stacked pair on the transition is the accepted cost;
        # asserting the POST keeps the fallback from silently PATCHing by name.
        api = FakeAPI(check_runs=[{"id": 5, "app": {"slug": "github-actions"}}])
        classify_risk.post_check_run(api, "a" * 40, self._result())
        method, path, body = api.calls[-1]
        self.assertEqual((method, path), ("POST", "/repos/gke-labs/kube-agents/check-runs"))
        self.assertEqual(body["external_id"], "risk-classify")


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

    def test_docs_site_build_inputs_are_high(self):
        # The docs-deploy build executes these and publishes the install.sh
        # users pipe to bash; a dependency bump is a supply-chain edit.
        result = self.classify([_file("docs/site/package-lock.json"), _file("docs/site/package.json")])
        self.assertEqual(result["tier"], "high")
        self.assertIn("docs-site-build-inputs", [entry["id"] for entry in result["rules"]])

    def test_docs_site_outside_the_content_tree_is_not_low(self):
        # Components and build config execute at build time; only the content
        # tree is prose.
        result = self.classify([_file("docs/site/src/components/Hero.astro")])
        self.assertEqual(result["tier"], "medium")

    def test_mdx_in_the_content_tree_is_not_low(self):
        # MDX accepts imports and JS expressions that execute in the
        # docs-deploy build -- the build that publishes the install.sh users
        # pipe to bash -- so it is not prose even inside the content tree
        # (853-F2).
        result = self.classify([_file("docs/site/src/content/docs/skills/index.mdx")])
        self.assertEqual(result["tier"], "medium")

    def test_top_level_content_page_is_still_low(self):
        # `**/*.md` must match zero intermediate directories, or every page
        # sitting directly under content/docs silently loses its low path.
        result = self.classify([_file("docs/site/src/content/docs/contributing.md")])
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
