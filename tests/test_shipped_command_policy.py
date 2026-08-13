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
