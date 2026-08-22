"""The command policy the operator actually ships, exercised by the real engine.

    python3 -m unittest discover -s tests -p 'test_*.py'

The policy is a Go string constant in the operator and the matcher is Python in the
broker, so nothing was checking that the shipped rules do what they are named for.
`test_credential_proxy.py` exercises `Policy` against a fixture it writes itself, which
proves the engine works and says nothing about the document a cluster receives.

This reads `credentialProxyPolicyJSON` out of the operator source and runs the broker's
own `Policy.blocked_by` over it.  One document, one matcher, no second copy of either.

Every case here asserts a **denial**.  A test that only walks the permitted path passes
just as happily when a rule is deleted -- which is how `gh pr merge` reached a live
cluster.  The permitted cases at the end exist for the other half of the 8/10 rule:
proving the denials did not simply break the product.
"""

import json
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFESTS_GO = (
    REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
)

sys.path.insert(0, str(REPO_ROOT / "agents" / "platform" / "scripts"))


def shipped_policy_document() -> dict:
    source = MANIFESTS_GO.read_text(encoding="utf-8")
    match = re.search(r"credentialProxyPolicyJSON = `(.*?)`", source, re.DOTALL)
    if match is None:
        raise AssertionError(
            "credentialProxyPolicyJSON moved or was renamed in "
            f"{MANIFESTS_GO.relative_to(REPO_ROOT)}"
        )
    return json.loads(match.group(1))


class ShippedPolicyTest(unittest.TestCase):
    """What the shipped rules refuse, and what they leave alone."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        from credential_proxy import Policy

        document = shipped_policy_document()
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(document, handle)
        handle.close()
        cls.policy = Policy.load(handle.name)
        cls.rule_ids = {rule["id"] for rule in document["rules"]}

    def assertBlocked(self, argv, rule_id=None):
        rule = self.policy.blocked_by(argv)
        self.assertIsNotNone(rule, f"not blocked: {' '.join(argv)}")
        if rule_id is not None:
            self.assertEqual(rule_id, rule.rule_id, f"wrong rule for {' '.join(argv)}")

    def assertAllowed(self, argv):
        rule = self.policy.blocked_by(argv)
        self.assertIsNone(
            rule,
            f"{' '.join(argv)} was blocked by {rule.rule_id if rule else ''}, "
            "and the product needs it",
        )

    def test_merging_a_pull_request_is_refused(self):
        """The headline finding, end to end on a live cluster on 10 August.

        The agent opened a PR and merged it through the broker -- proposer and
        approver collapsed into one actor.  Refused here even against a correctly
        protected repository, because whether the merge *succeeds* is the customer's
        branch protection and whether it is *attempted* is ours.
        """
        for argv in (
            ["gh", "pr", "merge", "1"],
            ["gh", "pr", "merge", "--squash", "--admin", "1"],
            ["gh", "--repo", "owner/repo", "pr", "merge", "1"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.merge")

    def test_approving_a_pull_request_is_refused(self):
        """B2: gatekeepers veto, approvers assent, and an agent never assents.

        A veto is monotone in the safe direction -- injecting a false block costs one
        annoying PR.  Injecting a false approval is a production change.  So
        --request-changes stays permitted below and --approve does not.
        """
        for argv in (
            ["gh", "pr", "review", "--approve", "1"],
            ["gh", "pr", "review", "1", "--approve"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.assent")

    def test_the_rest_api_cannot_be_used_to_go_around_those_two(self):
        """An allowlist that forgets `gh api` is not an allowlist.

        `gh api -X PUT repos/o/r/pulls/1/merge` merges a pull request without ever
        typing `gh pr merge`, and `-f` alone turns a request into a POST with no -X in
        sight.  Both shapes are refused; plain reads are not.
        """
        for argv in (
            ["gh", "api", "-X", "PUT", "repos/o/r/pulls/1/merge"],
            ["gh", "api", "--method", "PUT", "repos/o/r/pulls/1/merge"],
            ["gh", "api", "-X", "POST", "repos/o/r/issues/1/comments"],
            ["gh", "api", "repos/o/r/pulls/1/merge", "-f", "merge_method=squash"],
            ["gh", "api", "repos/o/r/pulls/1/merge", "--field", "x=y"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.api-mutation")

    def test_the_pipeline_cannot_be_triggered_or_a_release_cut(self):
        """A workflow run is a production change wearing a different hat."""
        for argv in (
            ["gh", "workflow", "run", "deploy.yml"],
            ["gh", "run", "rerun", "123"],
            ["gh", "release", "create", "v1.0.0"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.pipeline-trigger")

    def test_repository_administration_is_refused(self):
        """Setting a secret or editing a ruleset rewrites the gate itself."""
        for argv in (
            ["gh", "secret", "set", "TOKEN"],
            ["gh", "ruleset", "delete", "1"],
            ["gh", "repo", "delete", "owner/repo"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.repo-administration")

    def test_the_pre_existing_rules_still_hold(self):
        """Regression cover for the rules that were already here.

        None of these had a case until now -- the coverage check below found them.
        They shipped and were presumably correct; nothing was confirming it.
        """
        for argv, rule_id in (
            (["gh", "auth", "token"], "github.token-disclosure"),
            (["gh", "auth", "status", "--show-token"], "github.token-disclosure"),
            (["gcloud", "auth", "print-access-token"], "gcp.access-token-disclosure"),
            (["gcloud", "auth", "print-identity-token"], "gcp.access-token-disclosure"),
            (["gcloud", "config", "config-helper"], "gcp.config-helper-disclosure"),
            (["kubectl", "config", "view", "--raw"], "kubernetes.token-disclosure"),
            (["kubectl", "create", "token", "default"], "kubernetes.token-disclosure"),
            (["git", "credential", "fill"], "git.credential-disclosure"),
            (["gcloud", "auth", "login"], "gcp.credential-replacement"),
            (
                ["gcloud", "auth", "activate-service-account", "--key-file=k.json"],
                "gcp.credential-replacement",
            ),
            (["gh", "auth", "login"], "github.credential-replacement"),
            (["gh", "auth", "logout"], "github.credential-replacement"),
            (["gcloud", "components", "install", "beta"], "tool.self-modification"),
            (["gh", "extension", "install", "owner/ext"], "tool.self-modification"),
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, rule_id)

    def test_the_product_still_works(self):
        """The other half of 8/10: the denials must not have broken proposing.

        Everything the agent legitimately does with `gh` -- open a pull request,
        comment, read, file an issue, and block a change -- stays permitted.
        """
        for argv in (
            ["gh", "pr", "create", "--title", "x", "--body", "y"],
            ["gh", "pr", "list", "--state", "open"],
            ["gh", "pr", "view", "1"],
            ["gh", "pr", "diff", "1"],
            ["gh", "pr", "comment", "1", "--body", "z"],
            ["gh", "pr", "review", "--request-changes", "--body", "no"],
            ["gh", "issue", "create", "--title", "x"],
            ["gh", "issue", "list", "--label", "audit"],
            ["gh", "issue", "comment", "1", "--body", "z"],
            ["gh", "api", "repos/o/r/pulls/1/comments"],
            ["gh", "auth", "status"],
            ["kubectl", "get", "pods"],
        ):
            with self.subTest(argv=argv):
                self.assertAllowed(argv)

    def test_every_rule_is_named_by_a_case_above(self):
        """A rule nobody exercises is a rule nobody knows still works.

        Cheap coverage check: each shipped rule id has to appear somewhere in this
        file.  Adding a rule without a case fails here rather than passing quietly.
        """
        source = pathlib.Path(__file__).read_text(encoding="utf-8")
        # `in source` rather than assertIn: assertIn on a failure prints the whole
        # haystack, and the haystack is this file.
        unexercised = sorted(r for r in self.rule_ids if f'"{r}"' not in source)
        self.assertEqual(
            [],
            unexercised,
            f"shipped rules with no case in this file: {', '.join(unexercised)}",
        )


if __name__ == "__main__":
    unittest.main()


class TheRulesReadCommandsNotProse(ShippedPolicyTest):
    """The two directions the joined-string match got wrong.

    Both were found by review on the pull request that added these rules, and
    both are properties of how matching works rather than of the patterns: the
    rules are word searches over `shlex.join(argv)`, and join leaves the spaces
    inside a quoted argument as real spaces.
    """

    def test_the_agents_own_pull_request_is_not_refused(self):
        # submit-suggestion/SKILL.md tells the agent to close every body with
        # this sentence, and submit_suggestion.py passes it to `gh pr create`
        # inline. Before the fix the joined string held a `pr` token and a
        # later `merge` token, so github.merge refused every GitOps suggestion
        # the product exists to raise -- the denylist taking the product down
        # rather than an attacker.
        body = (
            "Automated suggestion from the Platform Agent.\n\n"
            "Please review the code diffs and merge this PR to trigger the "
            "GitOps CI/CD rollout!"
        )
        for argv, desc in (
            (["gh", "pr", "create", "--repo", "acme/fleet", "--title",
              "chore: raise replicas", "--body", body], "--body <prose>"),
            (["gh", "pr", "create", "--repo", "acme/fleet", "--body=" + body],
             "--body=<prose>"),
            (["gh", "pr", "review", "1", "--request-changes", "--body",
              "do not merge this until the drift is confirmed"],
             "a veto whose body says merge"),
            (["gh", "issue", "comment", "7", "--body",
              "run the release workflow once this lands"],
             "a comment naming a workflow run"),
        ):
            with self.subTest(desc=desc):
                self.assertIsNone(self.policy.blocked_by(argv), desc)

    def test_an_attached_shorthand_still_reaches_the_rule(self):
        # gh is Cobra/pflag, which takes a shorthand's value with no separator.
        # `-XPUT` is `-X PUT` and performs the merge, and
        # PUT /repos/{o}/{r}/pulls/{n}/merge needs no request body -- so before
        # the fix a single command merged with the real credential while
        # matching neither branch of github.api-mutation.
        for argv, desc in (
            (["gh", "api", "-XPUT", "repos/o/r/pulls/1/merge"], "-XPUT"),
            (["gh", "api", "-XPOST", "repos/o/r/releases"], "-XPOST"),
            (["gh", "api", "repos/o/r/pulls/1/merge", "-fmerge_method=squash"],
             "-f with attached key=value"),
            (["gh", "api", "-X", "PUT", "repos/o/r/pulls/1/merge"],
             "the detached spelling still refused"),
        ):
            with self.subTest(desc=desc):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(rule, desc)
                self.assertEqual("github.api-mutation", rule.rule_id, desc)

    def test_the_older_rules_are_unchanged(self):
        # The normalisation runs for every rule, not only the new ones, so the
        # pre-existing disclosure and replacement rules are pinned here too.
        for argv, expected in (
            (["gh", "auth", "token"], "github.token-disclosure"),
            (["gh", "auth", "login"], "github.credential-replacement"),
            (["gcloud", "auth", "print-access-token"], "gcp.access-token-disclosure"),
            (["kubectl", "create", "token", "default"], "kubernetes.token-disclosure"),
        ):
            with self.subTest(rule=expected):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(rule, expected)
                self.assertEqual(expected, rule.rule_id)

    def test_a_free_text_flag_cannot_swallow_the_next_flag(self):
        """A name on the free-text list is not always value-taking.

        The list is applied without knowing which subcommand is running, and
        `--comment` takes prose on `gh issue close` while being a boolean on
        `gh pr review`. Swallowing the token after it there drops `--approve`
        out of the match text and hides an approval from github.assent.

        gh refuses this particular argv itself ("need exactly one of
        --approve, --request-changes, or --comment"), so it was not an escape
        -- but it is one flag's arity away from being one, which is not a
        margin to leave to the upstream CLI's flag table.
        """
        for argv in (
            ["gh", "pr", "review", "--comment", "--approve", "1"],
            ["gh", "pr", "review", "--body", "--approve", "1"],
            ["gh", "pr", "review", "--title", "--approve", "1"],
        ):
            with self.subTest(argv=argv):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(
                    rule, f"{argv} slipped past: a flag value swallowed --approve")
                self.assertEqual("github.assent", rule.rule_id)

    def test_prose_is_still_dropped_when_it_is_ordinary_text(self):
        """The guard above must not undo the prose fix it sits inside."""
        body = ("Drift detected.\n\nPlease review the code diffs and merge this "
                "PR to trigger the GitOps CI/CD rollout!")
        argv = ["gh", "pr", "create", "--repo", "o/r", "--title", "fix: drift",
                "--body", body]
        self.assertIsNone(self.policy.blocked_by(argv))
