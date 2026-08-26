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
        """Setting a secret or reshaping the repository rewrites the gate itself.

        `ruleset` is deliberately absent. An earlier version of this rule
        listed it and this test asserted `gh ruleset delete 1` was refused --
        a command that does not exist, so the case was green and proved
        nothing. `gh ruleset` has only check, list and view; rulesets are
        mutable only through `gh api`, which github.api-mutation covers.
        """
        for argv in (
            ["gh", "secret", "set", "TOKEN"],
            ["gh", "variable", "delete", "FLAG"],
            ["gh", "repo", "delete", "owner/repo"],
            ["gh", "repo", "archive", "owner/repo"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.repo-administration")

    def test_a_ruleset_is_covered_where_the_docs_say_it_is(self):
        """Rulesets are refused by `github.api-mutation`, not by the rule above.

        Worth a case of its own because the docs make that claim in prose.
        `docs/credential-isolation-design.md` and the site reference page are
        where an operator looks up a rule id from a SECURITY_POLICY_BLOCKED
        line, and both of them said `github.repo-administration` enumerated
        rulesets after the `ruleset` branch had been dropped from it -- a page
        describing a deny surface the shipped policy did not have. The prose now
        names `github.api-mutation` instead, so this is what stops the two
        drifting apart again.
        """
        for argv in (
            ["gh", "api", "-X", "PUT", "repos/o/r/rulesets/1"],
            ["gh", "api", "-X", "POST", "repos/o/r/rulesets"],
            ["gh", "api", "-X", "DELETE", "repos/o/r/rulesets/1"],
            ["gh", "api", "-XPUT", "repos/o/r/rulesets/1"],
            ["gh", "api", "repos/o/r/rulesets", "-f", "name=main"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.api-mutation")

        # And the read verbs `gh ruleset` actually has stay usable, which is the
        # other half of why the branch was dropped rather than fixed.
        for argv in (
            ["gh", "ruleset", "list"],
            ["gh", "ruleset", "view", "1"],
            ["gh", "ruleset", "check", "main"],
            ["gh", "api", "repos/o/r/rulesets"],
        ):
            with self.subTest(argv=argv):
                self.assertAllowed(argv)

    def test_a_denied_command_cannot_be_re_spelled_as_an_alias(self):
        """gh remembers a new name for any command, in a dir it keeps.

        `gh alias set m 'pr merge'` then `gh m 1` resolves inside gh, so every
        rule keyed on the written argv is bypassed by one permitted command --
        including the pre-existing token-disclosure rules, which is what makes
        this worse than the write path it was found in. GH_CONFIG_DIR is a
        pod-lifetime emptyDir in the sidecar that also holds the live
        installation token, so `gh alias set t 'auth token'` + `gh t` prints
        it. `gh config set http_unix_socket` is the same shape pointed at the
        shared workspace.

        The read verbs stay permitted: naming an alias is not the problem,
        creating one is.
        """
        for argv in (
            ["gh", "alias", "set", "m", "pr merge"],
            ["gh", "alias", "set", "t", "auth token"],
            ["gh", "alias", "import", "aliases.yml"],
            ["gh", "alias", "delete", "m"],
            ["gh", "config", "set", "http_unix_socket", "/opt/data/x.sock"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "tool.self-modification")
        for argv in (["gh", "alias", "list"], ["gh", "config", "get", "editor"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.policy.blocked_by(argv))

    def test_the_token_is_not_printable_by_either_spelling(self):
        """--show-token has a shorthand, and the rule only named the long one."""
        for argv in (
            ["gh", "auth", "status", "--show-token"],
            ["gh", "auth", "status", "-t"],
            ["gh", "auth", "status", "-t", "--hostname", "github.com"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.token-disclosure")

    def test_a_pipeline_is_not_reachable_by_the_adjacent_verb(self):
        """Disabling a required workflow is the same effect as triggering one.

        The rule enumerated run/rerun/cancel and release create/delete/upload,
        so `gh workflow disable` turned the gate off and `gh release edit
        --draft=false` published a draft -- both firing the workflows the rule
        exists to keep the agent away from, by a verb it did not list.
        """
        for argv in (
            ["gh", "workflow", "disable", "deploy.yml"],
            ["gh", "workflow", "enable", "deploy.yml"],
            ["gh", "run", "delete", "123"],
            ["gh", "release", "edit", "v1", "--draft=false"],
        ):
            with self.subTest(argv=argv):
                self.assertBlocked(argv, "github.pipeline-trigger")
        for argv in (["gh", "workflow", "list"], ["gh", "run", "view", "1"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.policy.blocked_by(argv))

    def test_prose_attached_to_a_shorthand_is_dropped_too(self):
        """`-bPlease merge this PR` is the same prose as `--body <that>`."""
        self.assertIsNone(
            self.policy.blocked_by(["gh", "pr", "create", "-bPlease merge this PR"]))

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

    def test_a_cluster_cannot_bury_the_flag_the_rule_keys_on(self):
        """pflag reads a boolean shorthand and a value-taking one in one token.

        `parseSingleShortArg` consumes the boolean, sets the remainder as the
        shorts still to read, and re-enters the loop -- so `gh api -iX PUT` is
        `--include --method PUT` and performs the merge. Splitting only the
        first shorthand off left the `-X` the rule matches on as a bare `X`,
        and both commands this file exists to refuse went through. Verified
        against real `gh`: `gh api -iX GET repos/cli/cli` returns headers and a
        200, so the cluster parses exactly this way.
        """
        for argv, expected, desc in (
            (["gh", "api", "-iX", "PUT", "repos/o/r/pulls/1/merge"],
             "github.api-mutation", "boolean then detached method"),
            (["gh", "api", "-iXPUT", "repos/o/r/pulls/1/merge"],
             "github.api-mutation", "boolean then attached method"),
            (["gh", "api", "-if", "merge_method=squash",
              "repos/o/r/pulls/1/merge"],
             "github.api-mutation", "boolean then field flag"),
            (["gh", "api", "-viXPUT", "repos/o/r/pulls/1/merge"],
             "github.api-mutation", "two booleans then method"),
            (["gh", "auth", "status", "-at"],
             "github.token-disclosure", "--active then --show-token"),
            (["gh", "auth", "status", "-ta"],
             "github.token-disclosure", "the ordering that already worked"),
        ):
            with self.subTest(desc=desc):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(rule, f"{desc} slipped past: {argv}")
                self.assertEqual(expected, rule.rule_id, desc)

    def test_a_long_cluster_costs_the_sidecar_nothing(self):
        """The argv is the sandbox's to choose, and the sidecar holds the keys.

        Re-emitting the suffix once per keyed letter was quadratic: every letter
        of `-aaaa...` is a keyed `-a`, so an N-letter token materialised about
        N**2/2 bytes. `["gh", "-" + "a" * 1000000]` fits inside
        `max_request_bytes`, and `gh` is an allowed executable, so it reached
        the walk and exhausted the container's 2Gi on one request -- against a
        single-threaded handler holding every agent's credentials.

        Bounded two ways: a keyed flag is emitted once, and a remainder only at
        the first value-taking shorthand. Growth is asserted rather than timed,
        so this does not turn into a flaky benchmark.
        """
        from credential_proxy import _cluster_readings

        small = _cluster_readings("-" + "a" * 1_000)
        large = _cluster_readings("-" + "a" * 100_000)
        self.assertEqual(["-a"], small)
        self.assertEqual(
            small,
            large,
            "output grows with token length: the walk is re-emitting per letter",
        )

        # A hundredfold more input for the same output is the property; the
        # quadratic version returned 1_999 and 199_999 pieces here.
        self.assertEqual(1, len(large))

    def test_a_long_cluster_still_cannot_hide_the_keyed_flag(self):
        """Bounding the walk must not buy an escape back.

        Emitting each keyed flag once is safe because the rules ask whether it
        is present. Stopping at the first value-taking shorthand is safe because
        pflag stops reading the cluster there too. Neither lets a longer cluster
        smuggle the flag past.
        """
        for argv, expected, desc in (
            (["gh", "api", "-" + "i" * 40 + "XPUT", "repos/o/r/pulls/1/merge"],
             "github.api-mutation", "forty booleans then the method"),
            (["gh", "auth", "status", "-" + "a" * 500 + "t"],
             "github.token-disclosure", "five hundred booleans then --show-token"),
        ):
            with self.subTest(desc=desc):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(rule, f"{desc} slipped past")
                self.assertEqual(expected, rule.rule_id, desc)

    def test_the_match_text_is_built_once_per_command(self):
        """Not once per rule.

        Pre-existing -- the call sat inside the generator `blocked_by` returns
        from -- but it multiplied the walk above by the rule count, which is how
        it surfaced. A thirteen-rule policy normalising thirteen times is thirteen
        times the work on every brokered command, escape or not.
        """
        import credential_proxy

        calls = []
        original = credential_proxy.policy_match_text

        def counting(argv):
            calls.append(argv)
            return original(argv)

        credential_proxy.policy_match_text = counting
        try:
            self.policy.blocked_by(["gh", "pr", "list", "--state", "open"])
        finally:
            credential_proxy.policy_match_text = original

        self.assertEqual(
            1,
            len(calls),
            f"normalised {len(calls)} times for one command; it is hoisted out "
            "of the generator so that it is once",
        )

    def test_every_shorthand_a_rule_keys_on_is_covered(self):
        """`_KEYED_SHORTHANDS` is a copy of something the operator owns.

        The broker cannot read the rules to learn which shorthands matter, so
        the table is written out by hand -- and a rule added later that keys on
        a shorthand missing from it is the same escape again, silently. This
        reads the shipped patterns and fails here instead.
        """
        from credential_proxy import _FREE_TEXT_FLAGS, _KEYED_SHORTHANDS

        keyed = set()
        for rule in shipped_policy_document()["rules"]:
            for match in re.finditer(r"(?<![-\w])-([A-Za-z])(?![\w-])", rule["pattern"]):
                keyed.add(f"-{match.group(1)}")
        self.assertTrue(keyed, "no shorthand found in the shipped rules at all")

        # A free-text shorthand is re-emitted by the walk's own break branch,
        # so it is covered without being in the table.
        missing = sorted(keyed - set(_KEYED_SHORTHANDS) - set(_FREE_TEXT_FLAGS))
        self.assertEqual(
            [],
            missing,
            "a shipped rule keys on these shorthands and a cluster can still "
            f"bury them: {', '.join(missing)}",
        )

    def test_a_cluster_does_not_invent_a_flag_out_of_a_value(self):
        """The re-dashing walk must stop where the value starts.

        A cluster of shorthands is letters by definition. Without stopping at
        the first non-letter, `-nkube-system` yields a `-t` off `system`, and
        anything holding `gh auth status` in the same command is refused for a
        namespace.
        """
        for argv, desc in (
            (["kubectl", "get", "pods", "-nkube-system"], "namespace shorthand"),
            (["kubectl", "get", "pods", "-oyaml"], "output shorthand"),
            (["kubectl", "logs", "-fdeploy/gateway"], "follow then a value"),
            (["gh", "pr", "create", "-bPlease merge this PR"], "attached prose"),
            (["gh", "api", "repos/o/r/pulls/1/comments"], "an ordinary read"),
        ):
            with self.subTest(desc=desc):
                self.assertAllowed(argv)

    def test_the_clusters_the_product_actually_runs_are_permitted(self):
        """Every clustered shorthand in executable product code, verbatim.

        Enumerated across `agents/`, `agentplugins/` and `scripts/` rather than
        guessed at: these two are the whole set, and both put a letter a rule
        keys on inside a cluster -- `-fdq` carries `-f`, `-it` carries `-t`.
        A re-dashing walk that did not stop where the value starts would refuse
        the agent's own workspace reset.
        """
        for argv, site in (
            (["git", "clean", "-fdq"],
             "gitops_workspace.py:548"),
            (["kubectl", "exec", "-it", "gateway", "-n", "kubeagents-system",
              "--", "/bin/sh"],
             "gke-basics/references/cli-reference.md:258"),
            (["gh", "issue", "comment", "7", "-R", "o/r", "-F", "/tmp/report.md"],
             "audit_report.py:4403"),
            (["gh", "pr", "comment", "7", "-R", "o/r", "-F", "/tmp/report.md"],
             "audit_report.py:4420"),
        ):
            with self.subTest(site=site):
                self.assertAllowed(argv)

    def test_prose_is_still_dropped_when_it_is_ordinary_text(self):
        """The guard above must not undo the prose fix it sits inside."""
        body = ("Drift detected.\n\nPlease review the code diffs and merge this "
                "PR to trigger the GitOps CI/CD rollout!")
        argv = ["gh", "pr", "create", "--repo", "o/r", "--title", "fix: drift",
                "--body", body]
        self.assertIsNone(self.policy.blocked_by(argv))


if __name__ == "__main__":
    unittest.main()
