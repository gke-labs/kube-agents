#!/usr/bin/env python3
"""Tests for `selfimprove_enable.py`.

Everything here runs without a cluster and without the network: the GitHub calls
go through one seam (`github`) and kubectl through another (`kube_json`), and
both are replaced. What is left to test is the part that decides things --
which scopes are missing, which repository the pull request is opened against,
whether a gate can ever fire -- plus the emitters, whose output another tool
parses.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

MODULE = pathlib.Path(__file__).resolve().parent / "selfimprove_enable.py"
_spec = importlib.util.spec_from_file_location("selfimprove_enable", MODULE)
enable = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(enable)


def ns(**kwargs) -> argparse.Namespace:
    """A Namespace carrying the defaults every check reads, overridden per test."""
    base = dict(
        namespace="kubeagents-system",
        context=None,
        colour=False,
        mode="upstream",
        upstream_repo="gke-labs/kube-agents",
        fork_repo="robot/kube-agents",
        base_branch="main",
        pr_label="self-improvement",
        severity_label_prefix="severity:",
        pat_secret="kube-agents-selfimprove-pat",
        pat_secret_key="token",
        ksa_name="kubeagents-selfimprove",
        gsa_name="kubeagents-selfimprove",
        gcp_project="",
        agent_deployment="platform-agent-gateway",
        token_file=None,
        token_stdin=False,
        api_server_cidrs=[],
        dns_cidrs=[],
        json=False,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def resp(status: int, body=None, headers=None) -> "enable.GitHubResponse":
    return enable.GitHubResponse(status, body if body is not None else {}, headers or {})


class TestScopes(unittest.TestCase):
    def test_absent_header_is_not_an_empty_scope_list(self):
        """A fine-grained token reports no header at all, and the caller has to
        tell that apart from a classic token that happens to hold nothing."""
        self.assertEqual(enable.parse_scopes(None), [])
        self.assertEqual(enable.parse_scopes(""), [])
        self.assertEqual(enable.parse_scopes("repo, read:org"), ["repo", "read:org"])
        self.assertEqual(enable.parse_scopes(" repo ,, gist "), ["repo", "gist"])

    def test_both_required_scopes_satisfy(self):
        self.assertEqual(enable.missing_scopes(["repo", "read:org"]), [])

    def test_public_repo_does_not_substitute_for_repo(self):
        """`gh auth login --with-token` reads the scope list, not the visibility
        of the repository, so a public_repo token fails against a public repo."""
        self.assertEqual(enable.missing_scopes(["public_repo", "read:org"]), ["repo"])

    def test_repo_implies_its_children_but_not_read_org(self):
        self.assertEqual(enable.missing_scopes(["repo"]), ["read:org"])

    def test_admin_org_and_write_org_both_cover_read_org(self):
        self.assertEqual(enable.missing_scopes(["repo", "admin:org"]), [])
        self.assertEqual(enable.missing_scopes(["repo", "write:org"]), [])

    def test_nothing_is_both_missing(self):
        self.assertEqual(enable.missing_scopes([]), ["repo", "read:org"])


class TestSlugs(unittest.TestCase):
    def test_a_browser_url_reduces_to_owner_name(self):
        self.assertEqual(enable.repo_slug("https://github.com/a/b"), "a/b")
        self.assertEqual(enable.repo_slug("https://github.com/a/b.git"), "a/b")
        self.assertEqual(enable.repo_slug("  a/b/  "), "a/b")

    def test_validity(self):
        self.assertTrue(enable.valid_slug("gke-labs/kube-agents"))
        self.assertFalse(enable.valid_slug("kube-agents"))
        self.assertFalse(enable.valid_slug("a/b/c"))
        self.assertFalse(enable.valid_slug(""))

    def test_comparison_is_case_insensitive_like_the_chart(self):
        """The chart refuses a fork equal to the upstream compared this way.
        Disagreeing here would pass a configuration that then fails the render."""
        self.assertTrue(enable.same_repo("Robot/Kube-Agents", "robot/kube-agents"))
        self.assertFalse(enable.same_repo("robot/kube-agents", "gke-labs/kube-agents"))


class TestRepoRoles(unittest.TestCase):
    def test_fork_mode_opens_the_pull_request_on_the_fork(self):
        self.assertEqual(enable.pr_base_repo("fork", "up/r", "fk/r"), "fk/r")

    def test_upstream_mode_opens_it_on_the_upstream(self):
        self.assertEqual(enable.pr_base_repo("upstream", "up/r", "fk/r"), "up/r")

    def test_report_only_opens_nothing(self):
        self.assertEqual(enable.pr_base_repo("report-only", "up/r", "fk/r"), "")

    def test_the_source_is_the_upstream_in_every_mode(self):
        """The revision under investigation is stamped into the image and is an
        upstream revision, so fork mode must not read its source from the fork."""
        for mode in enable.MODES:
            self.assertEqual(enable.source_repo("up/r"), "up/r", mode)


class TestSchedule(unittest.TestCase):
    def test_hourly(self):
        self.assertEqual(enable.schedule_period_hours("0 * * * *"), 1)

    def test_every_n_hours(self):
        self.assertEqual(enable.schedule_period_hours("0 */6 * * *"), 6)

    def test_a_fixed_hour_is_daily(self):
        self.assertEqual(enable.schedule_period_hours("30 2 * * *"), 24)

    def test_anything_else_declines_to_answer(self):
        """0 means "draw no conclusion", which every caller honours by skipping
        the check rather than reporting a gate as unreachable on a guess."""
        self.assertEqual(enable.schedule_period_hours("0 1,13 * * *"), 0)
        self.assertEqual(enable.schedule_period_hours("nonsense"), 0)
        self.assertEqual(enable.schedule_period_hours(""), 0)


class TestGateReachability(unittest.TestCase):
    def test_a_rule_above_the_run_rate_can_never_fire(self):
        gate = {"rules": [{"severity": "high", "minOccurrencesPerDay": 5}]}
        problems = enable.gate_reachable(24, gate)  # once a day
        self.assertEqual(len(problems), 1)
        self.assertIn("at most 1", problems[0])

    def test_a_rule_below_the_floor_is_reported_as_one_run_later(self):
        gate = {"rules": [{"severity": "critical", "minOccurrencesPerDay": 1}]}
        problems = enable.gate_reachable(1, gate)
        self.assertEqual(len(problems), 1)
        self.assertIn("floor", problems[0])

    def test_a_reachable_gate_is_silent(self):
        gate = {"rules": [{"severity": "high", "minOccurrencesPerDay": 3}]}
        self.assertEqual(enable.gate_reachable(1, gate), [])

    def test_an_unparsed_schedule_produces_no_complaints(self):
        gate = {"rules": [{"severity": "high", "minOccurrencesPerDay": 99}]}
        self.assertEqual(enable.gate_reachable(0, gate), [])

    def test_the_floor_matches_the_ledger_module(self):
        """A drift here would make this tool endorse a gate the runner ignores."""
        sys.path.insert(
            0,
            str(pathlib.Path(__file__).resolve().parent.parent / "agents" / "selfimprove" / "scripts"),
        )
        import selfimprove_ledger  # noqa: PLC0415 - imported for the comparison alone

        self.assertEqual(enable.MIN_CORROBORATING_RUNS, selfimprove_ledger.MIN_CORROBORATING_RUNS)
        self.assertEqual(tuple(enable.SEVERITIES), tuple(selfimprove_ledger.SEVERITIES))


class TestBuildValues(unittest.TestCase):
    def test_report_only_emits_no_credential_keys(self):
        """report-only renders no credential proxy, so a patSecret in its values
        is a name nothing reads and a reader's wrong impression of the mode."""
        values = enable.build_values(ns(mode="report-only"))
        gh = values["github"]
        for key in ("forkRepo", "patSecret", "patSecretKey", "prLabel"):
            self.assertNotIn(key, gh)
        self.assertEqual(values["mode"], "report-only")
        self.assertTrue(values["enabled"])

    def test_filing_modes_emit_the_credential_keys(self):
        for mode in ("fork", "upstream"):
            gh = enable.build_values(ns(mode=mode))["github"]
            self.assertEqual(gh["forkRepo"], "robot/kube-agents")
            self.assertEqual(gh["patSecret"], "kube-agents-selfimprove-pat")
            self.assertEqual(gh["patSecretKey"], "token")

    def test_there_is_no_project_id_under_github(self):
        """The chart takes the project from platformAgent.harness.projectId and
        has no selfImprovement.github.projectId; emitting one would be a key
        Helm silently ignores."""
        values = enable.build_values(ns(gcp_project="some-project"))
        self.assertNotIn("projectId", values["github"])

    def test_cidrs_are_only_emitted_when_given(self):
        self.assertNotIn("apiServerCIDRs", enable.build_values(ns()))
        values = enable.build_values(ns(api_server_cidrs=["10.0.0.1/32"]))
        self.assertEqual(values["apiServerCIDRs"], ["10.0.0.1/32"])

    def test_slugs_are_normalised_on_the_way_in(self):
        values = enable.build_values(
            ns(upstream_repo="https://github.com/gke-labs/kube-agents.git")
        )
        self.assertEqual(values["github"]["upstreamRepo"], "gke-labs/kube-agents")


class TestEmitters(unittest.TestCase):
    def test_a_trailing_colon_is_quoted(self):
        """`severity:` unquoted is a YAML mapping key, so the emitted values
        file would not parse as the value it looks like."""
        out = "\n".join(enable.emit_yaml({"severityLabelPrefix": "severity:"}))
        self.assertEqual(out, 'severityLabelPrefix: "severity:"')

    def test_nesting_and_lists(self):
        out = enable.emit_yaml({"a": {"b": "c"}, "d": ["x", "y"], "e": []})
        self.assertEqual(out, ["a:", "  b: c", "d:", "  - x", "  - y", "e: []"])

    def test_booleans_are_yaml_booleans_not_python_ones(self):
        self.assertEqual(enable.emit_yaml({"enabled": True}), ["enabled: true"])
        self.assertEqual(enable.emit_yaml({"enabled": False}), ["enabled: false"])

    def test_hcl_quotes_strings_and_leaves_booleans_bare(self):
        out = enable.emit_hcl({"enabled": True, "mode": "fork", "c": ["1/32"]})
        self.assertEqual(out, ["enabled = true", 'mode = "fork"', 'c = ["1/32"]'])

    def test_hcl_nests_objects(self):
        out = enable.emit_hcl({"github": {"forkRepo": "a/b"}})
        self.assertEqual(out, ["github = {", '  forkRepo = "a/b"', "}"])


class TestSecretManifest(unittest.TestCase):
    def test_it_uses_string_data_so_nothing_here_base64s(self):
        doc = json.loads(enable.secret_manifest("s", "ns", "token", "SECRET"))
        self.assertEqual(doc["kind"], "Secret")
        self.assertEqual(doc["metadata"], {
            "name": "s",
            "namespace": "ns",
            "labels": {"app.kubernetes.io/part-of": "kube-agents"},
        })
        self.assertEqual(doc["stringData"], {"token": "SECRET"})
        self.assertNotIn("data", doc)

    def test_the_key_is_the_configured_one(self):
        doc = json.loads(enable.secret_manifest("s", "ns", "pat", "X"))
        self.assertEqual(list(doc["stringData"]), ["pat"])


class TestTokenInput(unittest.TestCase):
    def test_no_subcommand_accepts_a_token_argument(self):
        """The load-bearing security property of this tool. A --token flag puts
        the PAT in argv, where any process on the machine can read it and the
        shell keeps it in history."""
        parser = enable.build_parser()
        actions = list(parser._actions)  # noqa: SLF001 - the only way to walk subparsers
        for action in parser._actions:  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for sub in action.choices.values():
                    actions.extend(sub._actions)  # noqa: SLF001
        flags = {opt for a in actions for opt in a.option_strings}
        self.assertNotIn("--token", flags)
        self.assertIn("--token-file", flags)
        self.assertIn("--token-stdin", flags)

    def test_a_file_is_stripped(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".pat", delete=False) as fh:
            fh.write("  ghp_example  \n")
            path = fh.name
        try:
            self.assertEqual(enable.read_token(ns(token_file=path)), "ghp_example")
        finally:
            pathlib.Path(path).unlink()

    def test_the_environment_is_the_fallback(self):
        with mock.patch.dict(enable.os.environ, {"SELFIMPROVE_PAT": "ghp_env"}):
            self.assertEqual(enable.read_token(ns()), "ghp_env")

    def test_absent_and_required_exits_with_a_message_naming_the_three_ways(self):
        with mock.patch.dict(enable.os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                enable.read_token(ns(), required=True)
        self.assertIn("--token-file", str(caught.exception))

    def test_absent_and_optional_is_none(self):
        with mock.patch.dict(enable.os.environ, {}, clear=True):
            self.assertIsNone(enable.read_token(ns(), required=False))

    def test_a_missing_file_is_not_silently_no_token(self):
        with self.assertRaises(SystemExit):
            enable.read_token(ns(token_file="/nonexistent/pat"))


class TestNetworkPolicy(unittest.TestCase):
    POLICY = {
        "spec": {
            "egress": [
                {"ports": [{"port": 53}], "to": [{"ipBlock": {"cidr": "10.96.0.10/32"}}]},
                {
                    "ports": [{"port": 443}, {"port": 6443}],
                    "to": [
                        {"ipBlock": {"cidr": "34.118.224.1/32"}},
                        {"podSelector": {"matchLabels": {"a": "b"}}},
                    ],
                },
            ]
        }
    }

    # The shape the chart actually renders on 443: everything, less the five
    # private ranges, alongside the /32s it discovered for the API server.
    WIDE_POLICY = {
        "spec": {
            "egress": [
                {
                    "ports": [{"port": 443}],
                    "to": [
                        {"ipBlock": {"cidr": "34.118.224.1/32"}},
                        {"ipBlock": {"cidr": "10.96.0.1/32"}},
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": [
                                    "10.0.0.0/8",
                                    "172.16.0.0/12",
                                    "192.168.0.0/16",
                                    "169.254.0.0/16",
                                    "100.64.0.0/10",
                                ],
                            }
                        },
                    ],
                }
            ]
        }
    }

    def test_blocks_are_collected_per_port(self):
        self.assertEqual(
            enable.policy_ip_blocks(self.POLICY, 6443), [{"cidr": "34.118.224.1/32"}]
        )
        self.assertEqual(
            enable.policy_ip_blocks(self.POLICY, 53), [{"cidr": "10.96.0.10/32"}]
        )
        self.assertEqual(enable.policy_ip_blocks(self.POLICY, 80), [])

    def test_containment(self):
        self.assertTrue(enable.covered("34.118.224.1", ["34.118.224.0/24"]))
        self.assertFalse(enable.covered("10.150.0.2", ["34.118.224.0/24"]))

    def test_a_malformed_cidr_does_not_raise(self):
        """A policy written by hand can carry anything; a crash here would hide
        every other finding in the report."""
        self.assertFalse(enable.covered("10.0.0.1", ["not-a-cidr"]))
        self.assertFalse(enable.covered("not-an-ip", ["10.0.0.0/8"]))

    def test_an_excepted_address_is_not_covered(self):
        """`0.0.0.0/0` with the private ranges excepted does not reach a private
        API server. Reading `cidr` and ignoring `except` made every address on
        earth covered, so `verify`'s api-server egress check could not fail --
        it reported OK on precisely the install it exists to catch."""
        blocks = enable.policy_ip_blocks(self.WIDE_POLICY, 443)
        self.assertFalse(enable.covered("10.150.0.2", blocks))

    def test_a_narrower_rule_still_covers_an_excepted_address(self):
        """Egress rules are additive. 10.96.0.1 falls in the wide rule's
        `except 10.0.0.0/8` and is still allowed, because a discovered /32
        names it -- the normal shape of a correct install, which must not now
        report as blocked."""
        blocks = enable.policy_ip_blocks(self.WIDE_POLICY, 443)
        self.assertTrue(enable.covered("10.96.0.1", blocks))
        self.assertTrue(enable.covered("34.118.224.1", blocks))
        # And a public address the wide rule does allow.
        self.assertTrue(enable.covered("140.82.121.4", blocks))

    def test_a_malformed_except_does_not_read_as_excepting_nothing(self):
        """The safe direction for an unreadable policy is "not covered": the
        other one turns a policy nobody can parse into a clean report."""
        blocks = [{"cidr": "0.0.0.0/0", "except": ["not-a-cidr"]}]
        self.assertFalse(enable.covered("10.150.0.2", blocks))

    def test_a_label_selected_kube_dns_counts_as_reachable(self):
        """A NetworkPolicy matches the destination pod, so kube-dns reached by
        label needs no ipBlock for its Service's ClusterIP."""
        policy = {
            "spec": {
                "egress": [
                    {
                        "ports": [{"port": 53}],
                        "to": [{"podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                    }
                ]
            }
        }
        self.assertTrue(enable.has_kube_dns_selector(policy))

    def test_node_local_dns_counts_too(self):
        policy = {
            "spec": {
                "egress": [
                    {
                        "ports": [{"port": 53}],
                        "to": [{"podSelector": {"matchLabels": {"k8s-app": "node-local-dns"}}}],
                    }
                ]
            }
        }
        self.assertTrue(enable.has_kube_dns_selector(policy))

    def test_a_selector_on_another_port_does_not_count(self):
        policy = {
            "spec": {
                "egress": [
                    {
                        "ports": [{"port": 443}],
                        "to": [{"podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                    }
                ]
            }
        }
        self.assertFalse(enable.has_kube_dns_selector(policy))

    def test_the_ip_only_policy_has_no_selector(self):
        self.assertFalse(enable.has_kube_dns_selector(self.POLICY))


class TestReport(unittest.TestCase):
    def test_only_a_failure_fails(self):
        rep = enable.Report(colour=False)
        rep.ok("a", "fine")
        rep.warn("b", "hmm")
        rep.skip("c", "n/a")
        self.assertFalse(rep.failed)
        rep.fail("d", "no")
        self.assertTrue(rep.failed)

    def test_the_tally_counts_every_row(self):
        rep = enable.Report(colour=False)
        rep.ok("a", "")
        rep.ok("b", "")
        rep.fail("c", "")
        self.assertIn("2 passed, 0 warnings, 1 failed, 0 skipped", rep.render()[-1])

    def test_json_carries_the_fix_text(self):
        rep = enable.Report(colour=False)
        rep.fail("a", "broken", "do this")
        self.assertEqual(
            rep.to_json(),
            [{"status": "fail", "check": "a", "detail": "broken", "fix": "do this"}],
        )


class TestCheckGithub(unittest.TestCase):
    """The one function with enough branching to be worth driving end to end."""

    def run_with(self, args, responses):
        """Replace `github` with a lookup over (method, path-prefix)."""

        def fake(path, token, method="GET", payload=None, timeout=30):
            for prefix, value in responses:
                if path.startswith(prefix):
                    return value
            return resp(404, {"message": "not stubbed: %s" % path})

        rep = enable.Report(colour=False)
        with mock.patch.object(enable, "github", fake):
            enable.check_github(rep, args, "ghp_token")
        return rep

    def statuses(self, rep):
        return {name: status for status, name, _, _ in rep.rows}

    def test_a_fork_equal_to_the_upstream_fails_before_any_api_call(self):
        rep = enable.Report(colour=False)
        with mock.patch.object(enable, "github", mock.Mock(side_effect=AssertionError)):
            enable.check_github(rep, ns(fork_repo="gke-labs/kube-agents"), "t")
        self.assertTrue(rep.failed)
        self.assertIn("same repository", rep.rows[-1][2])

    def test_report_only_needs_no_fork(self):
        rep = self.run_with(
            ns(mode="report-only", fork_repo=""),
            [
                ("/user", resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"})),
                ("/repos/gke-labs/kube-agents", resp(200, {})),
            ],
        )
        self.assertFalse(rep.failed)
        self.assertEqual(self.statuses(rep)["base branch"], enable.SKIP)

    def test_a_missing_scope_is_a_failure_not_a_warning(self):
        rep = self.run_with(
            ns(),
            [
                ("/user", resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo"})),
                ("/repos/", resp(200, {"permissions": {"push": True}, "fork": True,
                                       "source": {"full_name": "gke-labs/kube-agents"}})),
            ],
        )
        self.assertEqual(self.statuses(rep)["token scopes"], enable.FAIL)

    def test_a_fine_grained_token_warns_rather_than_failing(self):
        """No X-OAuth-Scopes header means the scopes cannot be read, which is
        not the same as their being absent."""
        rep = self.run_with(
            ns(),
            [
                ("/user", resp(200, {"login": "bot"}, {})),
                ("/repos/", resp(200, {"permissions": {"push": True}, "fork": True,
                                       "source": {"full_name": "gke-labs/kube-agents"}})),
            ],
        )
        self.assertEqual(self.statuses(rep)["token scopes"], enable.WARN)

    def test_a_fork_the_token_cannot_push_to_fails(self):
        rep = self.run_with(
            ns(),
            [
                ("/user", resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"})),
                ("/repos/robot/kube-agents", resp(200, {"permissions": {"push": False},
                                                        "fork": True,
                                                        "source": {"full_name": "gke-labs/kube-agents"}})),
                ("/repos/gke-labs/kube-agents", resp(200, {})),
            ],
        )
        self.assertEqual(self.statuses(rep)["fork writable"], enable.FAIL)

    def test_a_fork_of_something_else_warns(self):
        rep = self.run_with(
            ns(),
            [
                ("/user", resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"})),
                ("/repos/robot/kube-agents", resp(200, {"permissions": {"push": True},
                                                        "fork": True,
                                                        "source": {"full_name": "someone/else"}})),
                ("/repos/gke-labs/kube-agents", resp(200, {})),
            ],
        )
        self.assertEqual(self.statuses(rep)["fork network"], enable.WARN)

    def test_a_missing_base_branch_fails(self):
        rep = self.run_with(
            ns(),
            [
                ("/user", resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"})),
                ("/repos/robot/kube-agents", resp(200, {"permissions": {"push": True},
                                                        "fork": True,
                                                        "source": {"full_name": "gke-labs/kube-agents"}})),
                ("/repos/gke-labs/kube-agents/branches/main", resp(404)),
                ("/repos/gke-labs/kube-agents", resp(200, {})),
            ],
        )
        self.assertEqual(self.statuses(rep)["base branch"], enable.FAIL)

    def test_missing_labels_warn_rather_than_fail(self):
        """The loop attaches only labels that exist, so a missing one costs the
        label and not the pull request."""
        rep = self.run_with(
            ns(),
            [
                ("/user", resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"})),
                ("/repos/robot/kube-agents", resp(200, {"permissions": {"push": True},
                                                        "fork": True,
                                                        "source": {"full_name": "gke-labs/kube-agents"}})),
                ("/repos/gke-labs/kube-agents/branches/main", resp(200, {})),
                ("/repos/gke-labs/kube-agents/labels/", resp(404)),
                ("/repos/gke-labs/kube-agents", resp(200, {})),
            ],
        )
        self.assertEqual(self.statuses(rep)["pull request labels"], enable.WARN)
        self.assertFalse(rep.failed)

    def test_no_token_skips_every_github_check(self):
        rep = enable.Report(colour=False)
        with mock.patch.object(enable, "github", mock.Mock(side_effect=AssertionError)):
            enable.check_github(rep, ns(), None)
        self.assertEqual(self.statuses(rep)["github token"], enable.SKIP)


class TestCheckRevision(unittest.TestCase):
    def test_a_base_that_lacks_the_revision_fails_with_the_diff_size(self):
        """The trap that produced a forty-nine-file pull request: the filing turn
        branches from the base tip, so a base behind the running revision carries
        every commit between them into the diff."""
        rep = enable.Report(colour=False)
        responses = {
            "/repos/gke-labs/kube-agents/commits/abc123": resp(200, {}),
            "/repos/gke-labs/kube-agents/compare/main...abc123": resp(
                200, {"status": "diverged", "behind_by": 49}
            ),
        }
        with mock.patch.object(enable, "github", lambda p, t, **k: responses.get(p, resp(404))):
            enable.check_revision(rep, ns(), "t", "abc123", "pod-1")
        row = [r for r in rep.rows if r[1] == "base contains the revision"][0]
        self.assertEqual(row[0], enable.FAIL)
        self.assertIn("49", row[2])

    def test_a_base_at_or_ahead_of_the_revision_passes(self):
        rep = enable.Report(colour=False)
        responses = {
            "/repos/gke-labs/kube-agents/commits/abc123": resp(200, {}),
            "/repos/gke-labs/kube-agents/compare/main...abc123": resp(
                200, {"status": "behind", "behind_by": 0}
            ),
        }
        with mock.patch.object(enable, "github", lambda p, t, **k: responses.get(p, resp(404))):
            enable.check_revision(rep, ns(), "t", "abc123", "pod-1")
        self.assertFalse(rep.failed)

    def test_a_source_repo_that_does_not_serve_the_revision_fails(self):
        rep = enable.Report(colour=False)
        with mock.patch.object(enable, "github", lambda p, t, **k: resp(404)):
            enable.check_revision(rep, ns(), "t", "abc123", "pod-1")
        row = [r for r in rep.rows if r[1] == "revision in source repo"][0]
        self.assertEqual(row[0], enable.FAIL)

    def test_an_unreadable_stamp_warns_and_stops(self):
        rep = enable.Report(colour=False)
        with mock.patch.object(enable, "github", mock.Mock(side_effect=AssertionError)):
            enable.check_revision(rep, ns(), "t", None, "exec denied")
        self.assertEqual(rep.rows[0][0], enable.WARN)
        self.assertEqual(len(rep.rows), 1)


class TestValuesShape(unittest.TestCase):
    def test_a_bad_secret_key_is_caught_before_terraform_runs(self):
        rep = enable.Report(colour=False)
        enable.check_values_shape(rep, ns(pat_secret_key="my token"))
        self.assertTrue(rep.failed)

    def test_a_too_short_gsa_name_is_caught(self):
        rep = enable.Report(colour=False)
        enable.check_values_shape(rep, ns(gsa_name="abc"))
        self.assertTrue(rep.failed)

    def test_the_defaults_pass(self):
        rep = enable.Report(colour=False)
        enable.check_values_shape(rep, ns())
        self.assertFalse(rep.failed)
        self.assertEqual(rep.rows, [])


class TestCLI(unittest.TestCase):
    def test_values_yaml_round_trips_through_the_documented_helm_shape(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a: out.append(" ".join(map(str, a)))):
            rc = enable.main(
                [
                    "values",
                    "--mode",
                    "upstream",
                    "--upstream-repo",
                    "gke-labs/kube-agents",
                    "--fork-repo",
                    "robot/kube-agents",
                ]
            )
        self.assertEqual(rc, 0)
        text = "\n".join(out)
        self.assertIn("selfImprovement:", text)
        self.assertIn("  mode: upstream", text)
        self.assertIn("platformAgent.harness.clusterName", text)

    def test_values_hcl_wraps_in_extra_helm_values(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a: out.append(" ".join(map(str, a)))):
            enable.main(["values", "--format", "hcl", "--mode", "report-only"])
        text = "\n".join(out)
        self.assertIn("extra_helm_values = {", text)
        self.assertIn("selfImprovement = {", text)

    def test_values_json_is_parseable(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a: out.append(" ".join(map(str, a)))):
            enable.main(["values", "--format", "json", "--mode", "report-only"])
        doc = json.loads("\n".join(out))
        self.assertEqual(doc["selfImprovement"]["mode"], "report-only")

    def test_secret_dry_run_never_prints_the_token(self):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            with mock.patch.dict(enable.os.environ, {"SELFIMPROVE_PAT": "ghp_supersecret"}):
                with mock.patch.object(enable, "github", lambda *a, **k: resp(
                    200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"}
                )):
                    rc = enable.main(["secret", "--dry-run"])
        self.assertEqual(rc, 0)
        text = "\n".join(out)
        self.assertNotIn("ghp_supersecret", text)
        self.assertIn("not shown", text)

    def test_secret_refuses_a_token_missing_a_scope(self):
        with mock.patch.dict(enable.os.environ, {"SELFIMPROVE_PAT": "ghp_x"}):
            with mock.patch.object(enable, "github", lambda *a, **k: resp(
                200, {"login": "bot"}, {"x-oauth-scopes": "public_repo"}
            )):
                with mock.patch.object(enable, "kubectl", mock.Mock(side_effect=AssertionError)):
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        rc = enable.main(["secret"])
        self.assertEqual(rc, 1)
        self.assertIn("missing scope", err.getvalue())

    def run_secret(self, argv=("secret", "--no-check-token"), kubectl=None):
        """`secret` with a recording kubectl, returning (rc, calls).

        `args` is recorded as well as `stdin`: the apply mode is not visible in
        the manifest, and it is half of what keeps the token out of the
        Secret's metadata.
        """
        calls = []

        def fake_kubectl(args, namespace=None, context=None, stdin=None, check=True):
            calls.append({"args": list(args), "stdin": stdin, "check": check})
            if kubectl is not None:
                return kubectl(list(args))
            return ""

        with mock.patch.dict(enable.os.environ, {"SELFIMPROVE_PAT": "ghp_x"}):
            with mock.patch.object(enable, "kubectl", fake_kubectl):
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(io.StringIO()):
                        rc = enable.main(list(argv))
        return rc, calls

    def test_secret_can_be_forced_past_the_scope_check(self):
        rc, calls = self.run_secret()
        self.assertEqual(rc, 0)
        applied = next(c for c in calls if c["args"][0] == "apply")
        self.assertEqual(json.loads(applied["stdin"])["stringData"], {"token": "ghp_x"})

    def test_the_apply_is_server_side_under_a_named_field_manager(self):
        """A client-side apply copies the submitted manifest into
        kubectl.kubernetes.io/last-applied-configuration, which for a Secret
        built from `stringData` puts the PAT in cleartext in metadata beside
        its base64 `data`. The field manager has to be named too: under the
        default one, apply preserves an annotation that is already there."""
        rc, calls = self.run_secret()
        self.assertEqual(rc, 0)
        applied = next(c for c in calls if c["args"][0] == "apply")
        self.assertIn("--server-side", applied["args"])
        self.assertIn("--field-manager=selfimprove-enable", applied["args"])

    def test_the_stale_annotation_is_removed_and_its_absence_is_not_fatal(self):
        """SSA leaves the annotation behind on a Secret an earlier version of
        this tool created client-side, so it is stripped explicitly rather than
        left to kubectl's migration path. `check=False` because a Secret that
        never had one makes `kubectl annotate` exit non-zero."""
        rc, calls = self.run_secret()
        self.assertEqual(rc, 0)
        annotate = next(c for c in calls if c["args"][0] == "annotate")
        self.assertIn(enable.LAST_APPLIED_ANNOTATION + "-", annotate["args"])
        self.assertFalse(annotate["check"])

    def test_a_field_manager_conflict_is_reported_rather_than_forced(self):
        """Another owner -- a chart, an ExternalSecret -- is a collision worth
        surfacing. --force-conflicts would turn it into a silent clobber."""

        def conflicting(args):
            if args[0] == "apply":
                raise enable.KubeError("Apply failed with 1 conflict: conflict with \"helm\"")
            return ""

        rc, calls = self.run_secret(kubectl=conflicting)
        self.assertEqual(rc, 1)
        self.assertNotIn("--force-conflicts", next(c for c in calls if c["args"][0] == "apply")["args"])
        self.assertEqual([c for c in calls if c["args"][0] == "annotate"], [])


class TestJSONOutput(unittest.TestCase):
    """`--json` is the agent-facing surface, so the shape is the contract.

    Each case parses stdout whole rather than searching it: a banner or a
    stray progress line would make `json.loads` raise, which is the failure
    a caller would hit and the one worth asserting.
    """

    def run_json(self, argv, github_response, kubectl=None):
        out = io.StringIO()
        with mock.patch.dict(enable.os.environ, {"SELFIMPROVE_PAT": "ghp_supersecret"}):
            with mock.patch.object(enable, "github", github_response):
                with mock.patch.object(
                    enable, "kubectl", kubectl or mock.Mock(return_value="")
                ):
                    with contextlib.redirect_stdout(out):
                        with contextlib.redirect_stderr(io.StringIO()):
                            rc = enable.main(argv)
        return rc, json.loads(out.getvalue()), out.getvalue()

    def test_secret_json_parses_whole_and_never_carries_the_token(self):
        rc, doc, raw = self.run_json(
            ["secret", "--dry-run", "--json"],
            lambda *a, **k: resp(200, {"login": "bot"}, {"x-oauth-scopes": "repo, read:org"}),
        )
        self.assertEqual(rc, 0)
        self.assertFalse(doc["failed"])
        self.assertNotIn("ghp_supersecret", raw)
        checks = {c["check"]: c for c in doc["checks"]}
        self.assertEqual(checks["token"]["status"], "ok")
        self.assertEqual(checks["secret"]["status"], "ok")
        self.assertIn("not shown", checks["secret"]["detail"])

    def test_secret_json_turns_a_scope_refusal_into_a_failed_check_with_a_fix(self):
        rc, doc, _ = self.run_json(
            ["secret", "--json"],
            lambda *a, **k: resp(200, {"login": "bot"}, {"x-oauth-scopes": "public_repo"}),
            kubectl=mock.Mock(side_effect=AssertionError("must not apply")),
        )
        self.assertEqual(rc, 1)
        self.assertTrue(doc["failed"])
        row = [c for c in doc["checks"] if c["status"] == "fail"][0]
        self.assertEqual(row["check"], "token scopes")
        self.assertIn("repo", row["detail"])
        self.assertIn("--no-check-token", row["fix"])

    def test_labels_json_carries_a_row_per_label(self):
        rc, doc, _ = self.run_json(
            [
                "labels",
                "--json",
                "--dry-run",
                "--mode",
                "upstream",
                "--upstream-repo",
                "gke-labs/kube-agents",
            ],
            lambda *a, **k: resp(404, {"message": "Not Found"}),
        )
        self.assertEqual(rc, 0)
        self.assertFalse(doc["failed"])
        names = [c["check"] for c in doc["checks"]]
        self.assertIn("label self-improvement", names)
        for sev in enable.SEVERITIES:
            self.assertIn("label severity:%s" % sev, names)
        self.assertTrue(all(c["status"] == "warn" for c in doc["checks"]))

    def test_labels_json_marks_a_denied_label_failed_and_sets_the_exit_code(self):
        rc, doc, _ = self.run_json(
            ["labels", "--json", "--mode", "upstream", "--upstream-repo", "gke-labs/kube-agents"],
            lambda path, token, method="GET", payload=None, timeout=30: resp(
                404 if method == "GET" else 403, {"message": "Resource not accessible"}
            ),
        )
        self.assertEqual(rc, 1)
        self.assertTrue(doc["failed"])
        self.assertTrue(all(c["status"] == "fail" for c in doc["checks"]))
        self.assertIn("cannot create labels", doc["checks"][0]["detail"])

    def test_report_only_labels_is_a_skip_not_a_failure(self):
        rc, doc, _ = self.run_json(
            ["labels", "--json", "--mode", "report-only"],
            mock.Mock(side_effect=AssertionError("must not call GitHub")),
        )
        self.assertEqual(rc, 0)
        self.assertFalse(doc["failed"])
        self.assertEqual(doc["checks"][0]["status"], "skip")

    def test_verify_json_still_parses_when_the_cronjob_is_absent(self):
        """The state a caller polls until the upgrade lands, and the one place
        verify returns before reaching its emitter. Printing the table there
        made `json.loads` raise on exactly the install `--json` exists for."""
        rc, doc, _ = self.run_json(
            ["verify", "--json"],
            mock.Mock(side_effect=AssertionError("must not call GitHub")),
        )
        self.assertEqual(rc, 1)
        self.assertTrue(doc["failed"])
        self.assertEqual(doc["checks"][0]["check"], "cronjob")
        self.assertIn("does not exist", doc["checks"][0]["detail"])

    def test_every_json_capable_subcommand_emits_the_same_two_keys(self):
        """One reader for all of them, or the flag is not worth having."""
        parser = enable.build_parser()
        with_json = []
        for name, sub in parser._subparsers._group_actions[0].choices.items():
            if any("--json" in a.option_strings for a in sub._actions):
                with_json.append(name)
        self.assertEqual(sorted(with_json), ["labels", "preflight", "secret", "verify"])

    def test_emit_json_writes_nothing_but_the_document(self):
        rep = enable.Report(colour=False)
        rep.warn("a", "hmm", "do this")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            enable.emit_json(rep)
        self.assertEqual(
            json.loads(out.getvalue()),
            {"checks": [{"status": "warn", "check": "a", "detail": "hmm", "fix": "do this"}],
             "failed": False},
        )


class TestPromotionGap(unittest.TestCase):
    def test_a_finding_that_already_has_a_pull_request_is_not_a_warning(self):
        """The steady state of a working install: promoted again on every run,
        deliberately not re-filed, so `promoted > filed` holds forever."""
        rep = enable.Report(colour=False)
        ledger = {
            "findings": {
                "f1": {"promotions": [{"url": "https://github.com/o/r/pull/1"}]},
            }
        }
        enable.report_promotion_gap(rep, ledger, 1, 0, "")
        self.assertEqual(rep.rows[0][0], enable.OK)
        self.assertIn("already carry a pull request", rep.rows[0][2])

    def test_a_refusal_still_explains_the_gap(self):
        rep = enable.Report(colour=False)
        ledger = {"findings": {"f1": {"refused": {"why": "unsafe"}}}}
        enable.report_promotion_gap(rep, ledger, 1, 0, "")
        self.assertEqual(rep.rows[0][0], enable.OK)
        self.assertIn("recorded refusal", rep.rows[0][2])

    def test_a_promotion_with_neither_is_still_the_warning(self):
        rep = enable.Report(colour=False)
        ledger = {"findings": {"f1": {"promotions": []}}}
        enable.report_promotion_gap(rep, ledger, 1, 0, "")
        self.assertEqual(rep.rows[0][0], enable.WARN)
        self.assertEqual(rep.rows[0][1], "last run filed nothing new")

    def test_findings_the_gate_is_still_holding_are_not_counted_against_the_gap(self):
        """A row still accumulating sightings carries the empty `promotions`
        list `record_finding` gave it and was never promoted, so it cannot be a
        promotion nobody filed. Counting it as one warned on every ledger
        holding more findings than it has filed -- the live install's 18
        findings and 8 promotions among them."""
        rep = enable.Report(colour=False)
        findings = {
            "pr%d" % i: {"promotions": [{"url": "https://github.com/o/r/pull/%d" % i}]}
            for i in range(8)
        }
        findings.update({"held%d" % i: {"promotions": []} for i in range(10)})
        enable.report_promotion_gap(rep, {"findings": findings}, 8, 0, "")
        self.assertEqual(rep.rows[0][0], enable.OK)
        self.assertIn("8 already carry a pull request", rep.rows[0][2])

    def test_a_promotion_beyond_what_the_ledger_explains_is_still_the_warning(self):
        """The fault the check exists for, on a ledger that also holds filed
        findings: nine promotions against eight pull requests leaves one the
        filing turn neither filed nor refused."""
        rep = enable.Report(colour=False)
        findings = {
            "pr%d" % i: {"promotions": [{"url": "https://github.com/o/r/pull/%d" % i}]}
            for i in range(8)
        }
        findings.update({"held%d" % i: {"promotions": []} for i in range(10)})
        enable.report_promotion_gap(rep, {"findings": findings}, 9, 0, "")
        self.assertEqual(rep.rows[0][0], enable.WARN)
        self.assertIn("1 promotion(s)", rep.rows[0][3])


class TestPreflightOrder(unittest.TestCase):
    def test_the_kubernetes_version_is_checked_before_the_agent_exists(self):
        """`check_cluster` returns None when the Deployment is merely absent --
        the state a first preflight runs in -- and the 1.29 floor is a property
        of the cluster, not of that Deployment."""
        called = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(enable, "check_values_shape", lambda *a: None)
            )
            stack.enter_context(mock.patch.object(enable, "check_github", lambda *a: None))
            stack.enter_context(mock.patch.object(enable, "read_token", lambda a, **k: None))
            stack.enter_context(
                mock.patch.object(
                    enable, "check_kubernetes_version", lambda *a: called.append("version")
                )
            )
            stack.enter_context(mock.patch.object(enable, "check_cluster", lambda *a: None))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            enable.cmd_preflight(ns())
        self.assertEqual(called, ["version"])


class TestVerify(unittest.TestCase):
    """`verify` reads a live install, so what it reads has to be the install.

    Every test here covers one instance of a single defect: a check that reports
    on the flags the command was invoked with, or on nothing at all, while
    reading as though it had read the cluster. None of them fails loudly when it
    regresses -- the row goes green.
    """

    REVISION = "abc123def456"

    def cron(self, *, mode="fork", pat_secret="mounted-pat", pat_key="gh-token", env_extra=None):
        """A CronJob shaped like the one `self-improvement.yaml` renders.

        Fork mode on purpose: it is the mode where the two repository variables
        disagree, so it is the one that catches a check reading the wrong one.
        """
        env = [
            {"name": "SELFIMPROVE_MODE", "value": mode},
            # The upstream, always -- what the runner clones.
            {"name": "SELFIMPROVE_SOURCE_REPO", "value": "gke-labs/kube-agents"},
            # The pull request's base, which under fork mode is the fork.
            {"name": "SELFIMPROVE_UPSTREAM_REPO", "value": "robot/kube-agents"},
            {"name": "SELFIMPROVE_FORK_REPO", "value": "robot/kube-agents"},
            {"name": "SELFIMPROVE_BASE_BRANCH", "value": "main"},
            {"name": "SELFIMPROVE_AGENT_DEPLOYMENT", "value": "platform-agent-gateway"},
        ]
        env.extend(env_extra or [])
        return {
            "spec": {
                "schedule": "0 * * * *",
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {"name": "runner", "image": "img:1", "env": env}
                                ],
                                "volumes": [
                                    {
                                        "name": enable.PAT_VOLUME_NAME,
                                        "secret": {
                                            "secretName": pat_secret,
                                            "items": [{"key": pat_key, "path": pat_key}],
                                        },
                                    }
                                ],
                            }
                        }
                    }
                },
            }
        }

    def run_verify(self, objects, *, cron=None, github=None, **overrides):
        """Drive `cmd_verify` against a dict of cluster objects.

        Keyed by (kind, first argument), which is how `kube_json` is called
        everywhere in the command. Returns the report rows, the keys that were
        actually requested, and the GitHub paths that were fetched.
        """
        objects = dict(objects)
        objects.setdefault(("cronjob", "kube-agents-selfimprove"), cron or self.cron())
        asked, fetched = [], []

        def fake_kube_json(argv, namespace=None, context=None):
            asked.append((argv[1], argv[2]))
            return objects.get((argv[1], argv[2]))

        def fake_github(path, token, **kwargs):
            fetched.append(path)
            return (github or {}).get(path, resp(404))

        args = ns(
            cronjob="kube-agents-selfimprove",
            networkpolicy="kube-agents-selfimprove",
            mode="upstream",
            **overrides,
        )
        rep = enable.Report(colour=False)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(enable, "kube_json", fake_kube_json))
            stack.enter_context(mock.patch.object(enable, "github", fake_github))
            stack.enter_context(mock.patch.object(enable, "read_token", lambda a, **k: "t"))
            stack.enter_context(
                mock.patch.object(
                    enable, "stamped_revision", lambda *a, **k: (self.REVISION, "pod-1")
                )
            )
            stack.enter_context(mock.patch.object(enable, "Report", lambda **k: rep))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            enable.cmd_verify(args)
        return rep, asked, fetched

    def row(self, rep, name):
        matches = [r for r in rep.rows if r[1] == name]
        self.assertEqual(len(matches), 1, "expected one %r row, got %d" % (name, len(matches)))
        return matches[0]

    def ledger_cm(self, promoted, filed):
        """A ledger ConfigMap whose last run promoted more than it filed, with
        one finding still behind the gate and none carrying a pull request."""
        doc = {
            "runs": [{"at": "t", "outcome": "ok", "promoted": promoted, "filed": filed}],
            "findings": {"held": {"promotions": []}},
        }
        return {"data": {"ledger.json": json.dumps(doc)}}

    def test_the_secret_checked_is_the_one_the_cronjob_mounts(self):
        """Not the one `--pat-secret` defaults to. An operator who set
        `patSecret` got a green row about a Secret nothing mounts, while the pod
        that needs the real one sat in CreateContainerConfigError."""
        rep, asked, _ = self.run_verify(
            {("secret", "mounted-pat"): {"data": {"gh-token": "eA=="}}}
        )
        row = self.row(rep, "pat secret")
        self.assertEqual(row[0], enable.OK)
        self.assertIn("mounted-pat", row[2])
        self.assertIn("gh-token", row[2])
        self.assertIn(("secret", "mounted-pat"), asked)
        self.assertNotIn(("secret", "kube-agents-selfimprove-pat"), asked)

    def test_a_mounted_secret_that_is_absent_fails_under_its_mounted_name(self):
        rep, _, _ = self.run_verify({})
        row = self.row(rep, "pat secret")
        self.assertEqual(row[0], enable.FAIL)
        self.assertIn("mounted-pat", row[2])

    def test_the_stamped_revision_is_looked_for_in_the_source_repo(self):
        """`SELFIMPROVE_UPSTREAM_REPO` is the pull request's base, which under
        fork mode is the fork. Reading it as the source asks a fork to serve an
        upstream commit -- a FAIL against a correct install, and no check at all
        of the repository the runner clones."""
        rep, _, fetched = self.run_verify(
            {},
            github={
                "/repos/gke-labs/kube-agents/commits/%s" % self.REVISION: resp(200, {}),
                "/repos/robot/kube-agents/compare/main...%s" % self.REVISION: resp(
                    200, {"status": "behind", "behind_by": 0}
                ),
            },
        )
        self.assertIn("/repos/gke-labs/kube-agents/commits/%s" % self.REVISION, fetched)
        self.assertNotIn("/repos/robot/kube-agents/commits/%s" % self.REVISION, fetched)
        self.assertEqual(self.row(rep, "revision in source repo")[0], enable.OK)
        # The base is still the fork -- this fix moves the source, not the base.
        self.assertEqual(self.row(rep, "base contains the revision")[0], enable.OK)

    def test_report_only_does_not_warn_that_it_filed_nothing(self):
        """That mode files nothing by design, so `promoted > filed` is its
        steady state and the row was a standing warning on the chart's own
        default. The check exists to catch a GitHub write path failing
        silently, which is a path this mode does not have."""
        rep, _, _ = self.run_verify(
            {("configmap", enable.DEFAULT_LEDGER_CONFIGMAP): self.ledger_cm(3, 0)},
            cron=self.cron(mode="report-only"),
        )
        self.assertEqual([r for r in rep.rows if r[1] == "last run filed nothing new"], [])

    def test_a_writing_mode_still_warns_that_it_filed_nothing(self):
        """The control for the test above: same ledger, a mode that files."""
        rep, _, _ = self.run_verify(
            {("configmap", enable.DEFAULT_LEDGER_CONFIGMAP): self.ledger_cm(3, 0)},
            cron=self.cron(mode="fork"),
        )
        self.assertEqual(self.row(rep, "last run filed nothing new")[0], enable.WARN)

    def test_an_unreadable_kube_dns_does_not_read_as_reachable(self):
        """`discovered_endpoints` turns a failed read into an empty list, and an
        empty list leaves nothing blocked -- so the row went green having
        checked nothing. The api-server branch already guarded against this."""
        rep, _, _ = self.run_verify(
            {
                ("networkpolicy", "kube-agents-selfimprove"): TestNetworkPolicy.POLICY,
                ("endpoints", "kubernetes"): {
                    "subsets": [{"addresses": [{"ip": "34.118.224.1"}]}]
                },
            }
        )
        self.assertEqual(self.row(rep, "api server egress")[0], enable.OK)
        self.assertEqual(self.row(rep, "dns egress")[0], enable.WARN)

    def test_a_cronjob_with_no_mode_fails_rather_than_echoing_it(self):
        """`rep.ok("mode", mode)` reported whatever string it found, including
        none. Every row below branches on the value."""
        cron = self.cron()
        env = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
        cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e for e in env if e["name"] != "SELFIMPROVE_MODE"
        ]
        rep, _, _ = self.run_verify({}, cron=cron)
        self.assertEqual(self.row(rep, "mode")[0], enable.FAIL)

    def test_a_mode_the_runner_does_not_know_is_a_failure(self):
        cron = self.cron(mode="forked")
        rep, _, _ = self.run_verify({}, cron=cron)
        self.assertEqual(self.row(rep, "mode")[0], enable.FAIL)

    def test_an_unset_repository_variable_still_warns(self):
        """The arm exists and is reachable: `verify` reads whatever CronJob is
        there, and one that is not chart-rendered can be missing these."""
        cron = self.cron()
        env = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
        cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e for e in env if e["name"] != "SELFIMPROVE_BASE_BRANCH"
        ]
        rep, _, _ = self.run_verify({}, cron=cron)
        self.assertEqual(self.row(rep, "base_branch")[0], enable.WARN)

    def test_a_kube_dns_that_is_readable_and_allowed_still_passes(self):
        rep, _, _ = self.run_verify(
            {
                ("networkpolicy", "kube-agents-selfimprove"): TestNetworkPolicy.POLICY,
                ("endpoints", "kubernetes"): {
                    "subsets": [{"addresses": [{"ip": "34.118.224.1"}]}]
                },
                ("svc", "-l"): {"items": [{"spec": {"clusterIP": "10.96.0.10"}}]},
            }
        )
        self.assertEqual(self.row(rep, "dns egress")[0], enable.OK)


if __name__ == "__main__":
    unittest.main()
