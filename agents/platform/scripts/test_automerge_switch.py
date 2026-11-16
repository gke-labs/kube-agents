"""Tests for automerge_switch: the dial's per-kind switch as a rules-file pull request."""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import automation_rules as ar
import automerge_switch as sw

REPO = "acme/gitops"
ALLOWS = "version: 1\nautomerge:\n  allow:\n    - fix: no-pdb\n      environments: [dev]\n"
DENIES = "version: 1\nautomerge:\n  deny:\n    - fix: no-pdb\n      environments: [dev]\n"
EMPTY = "version: 1\n"


class FakeForge:
    def __init__(self, pending=None):
        self.calls, self.pending, self.refuse_delete = [], pending or {}, False

    def __call__(self, verb, payload):
        self.calls.append((verb, payload))
        if verb == "proposal-view":
            return {"proposal": self.pending}
        if verb == "proposal-close":
            return {"proposal": {"state": "closed"}}
        if verb == "branch-delete":
            if self.refuse_delete:
                raise RuntimeError("moved")
            return {"deleted": True}
        if verb == "proposal-create":
            if getattr(self, "refuse_create", False):
                raise RuntimeError("forge down")
            return {"proposal": {"url": "https://x/pull/12", "number": 12}}
        raise AssertionError(verb)

    def verbs(self):
        return [v for v, _ in self.calls]


class FakeVcs:
    """Clones a copy of `rules` (None for no file) and records what is written and published."""

    def __init__(self, rules):
        self.root = Path(tempfile.mkdtemp())
        self.rules, self.calls, self.written = rules, [], None

    def clone(self, repo, *, force, key):
        self.calls.append(("clone", key))
        tree = self.root / "tree"
        shutil.rmtree(tree, ignore_errors=True)
        tree.mkdir()
        if self.rules is not None:
            (tree / ".kube-agents").mkdir()
            (tree / ar.RULES_FILE).write_text(self.rules)
        self.tree = tree
        return {"branch": "main", "path": str(tree)}

    def branch(self, repo, name, *, key):
        self.calls.append(("branch", name))
        return {"exitCode": 0, "stderr": ""}

    def commit(self, message, *, paths, spec, key):
        self.written = (self.tree / ar.RULES_FILE).read_text()
        self.calls.append(("commit", message))

    def publish(self, repo, *, target, key):
        self.calls.append(("publish", target))

    def discard(self, repo, *, key):
        self.calls.append(("discard", key))

    def verbs(self):
        return [v for v, _ in self.calls]


def run(rules, on, pending=None, proposal=None):
    forge, vcs = FakeForge(proposal), FakeVcs(rules)
    result = sw.toggle(REPO, "no-pdb", "dev", on, pending, forge=forge, vcs=vcs, now=lambda: 0)
    return result, forge, vcs


class TestToggle(unittest.TestCase):
    def test_on_with_no_file_proposes_a_new_one(self):
        result, forge, vcs = run(None, True)
        self.assertEqual((result["state"], result["pr"]), (sw.PROPOSED, "https://x/pull/12"))
        self.assertEqual(vcs.written, ar.new_file("no-pdb", "dev"))
        create = dict(forge.calls)["proposal-create"]
        self.assertEqual(create["source"], "platform-agent/automerge-switch-on-no-pdb-dev-19700101000000")
        self.assertEqual((create["target"], create["title"]), ("main", "Auto-merge disruption-budget fixes in dev"))
        self.assertEqual(vcs.verbs()[-1], "discard")

    def test_on_adds_an_allow_rule(self):
        result, _, vcs = run(EMPTY, True)
        self.assertEqual(result["state"], sw.PROPOSED)
        self.assertTrue(ar.decide(ar.parse(vcs.written), "no-pdb", "dev").allowed)

    def test_off_adds_a_deny_rule_and_keeps_the_allow(self):
        result, forge, vcs = run(ALLOWS, False)
        self.assertEqual(result["state"], sw.PROPOSED)
        edited = ar.parse(vcs.written)
        self.assertEqual(len(edited.allow), 1)
        self.assertFalse(ar.decide(edited, "no-pdb", "dev").allowed)
        self.assertIn("Stop auto-merging", dict(forge.calls)["proposal-create"]["title"])

    def test_a_file_that_already_agrees_gets_no_pull_request(self):
        for rules, on, state in ((ALLOWS, True, sw.ON), (EMPTY, False, sw.OFF), (None, False, sw.OFF)):
            with self.subTest(rules=rules, on=on):
                result, forge, vcs = run(rules, on)
                self.assertEqual(result["state"], state)
                self.assertNotIn("proposal-create", forge.verbs())
                self.assertNotIn("publish", vcs.verbs())
                self.assertIn("discard", vcs.verbs())

    def test_a_deny_rule_blocks_turning_on(self):
        result, forge, _ = run(DENIES, True)
        self.assertEqual(result["state"], sw.BLOCKED)
        self.assertIn("a person must remove", result["reason"])
        self.assertNotIn("proposal-create", forge.verbs())

    def test_turning_back_on_takes_out_the_switchs_own_deny_rule(self):
        _, _, off = run(ALLOWS, False)
        result, _, on = run(off.written, True)
        self.assertEqual((result["state"], result["reason"]), (sw.PROPOSED, "switch's deny rule taken out"))
        edited, original = ar.parse(on.written), ar.parse(ALLOWS)
        self.assertEqual((edited.allow, edited.deny), (original.allow, ()))

    def test_turning_on_after_an_off_with_no_allow_also_adds_one(self):
        denied = ar.with_deny(EMPTY, "no-pdb", "dev", sw.DENY_NOTE)
        result, _, vcs = run(denied, True)
        self.assertEqual(result["state"], sw.PROPOSED)
        edited = ar.parse(vcs.written)
        self.assertEqual((edited.deny, len(edited.allow)), ((), 1))
        self.assertTrue(ar.decide(edited, "no-pdb", "dev").allowed)

    def test_a_persons_deny_beside_the_switchs_still_blocks(self):
        both = ar.with_deny(DENIES, "no-pdb", "dev", sw.DENY_NOTE)
        result, _, _ = run(both, True)
        self.assertEqual(result["state"], sw.BLOCKED)

    def test_the_body_claims_no_admin_approval(self):
        for on in (True, False):
            _, _, body = sw._wording("no-pdb", "dev", on)
            self.assertNotIn("admin turned", body)

    def test_a_refused_file_blocks(self):
        result, _, _ = run("version: 2\n", True)
        self.assertEqual(result["state"], sw.BLOCKED)

    def test_flipping_back_closes_the_pending_pull_request(self):
        proposal = {"state": "open", "source": "platform-agent/automerge-switch-on-no-pdb-dev-20261111000000", "url": "https://x/pull/11"}
        result, forge, _ = run(EMPTY, False, 11, proposal)
        self.assertEqual(forge.calls[1], ("proposal-close", {"number": 11}))
        self.assertEqual((result["state"], result["closed"]), (sw.OFF, "https://x/pull/11"))

    def test_closing_takes_the_spent_branch_off_the_forge(self):
        source = "platform-agent/automerge-switch-on-no-pdb-dev-20261111000000"
        proposal = {"state": "open", "source": source, "url": "u", "sourceRevision": "abc"}
        _, forge, _ = run(EMPTY, False, 11, proposal)
        self.assertEqual(forge.calls[2], ("branch-delete", {"branch": source, "revision": "abc"}))
        forge = FakeForge(proposal)
        forge.refuse_delete = True
        result = sw.toggle(REPO, "no-pdb", "dev", False, 11, forge=forge, vcs=FakeVcs(EMPTY), now=lambda: 0)
        self.assertIn("moved", result["branchKept"])
        self.assertEqual(result["state"], sw.OFF)

    def test_the_same_direction_reuses_the_pending_pull_request(self):
        proposal = {"state": "open", "source": "platform-agent/automerge-switch-on-no-pdb-dev-20261111000000", "url": "https://x/pull/11"}
        result, forge, vcs = run(EMPTY, True, 11, proposal)
        self.assertEqual((result["state"], result["pr"], result["number"]), (sw.PROPOSED, "https://x/pull/11", 11))
        self.assertEqual(forge.verbs(), ["proposal-view"])
        self.assertEqual(vcs.calls, [])

    def test_a_pending_number_that_is_not_ours_or_not_open_is_left_alone(self):
        for proposal in (
            {"state": "open", "source": "feature/x", "url": "u"},
            {"state": "merged", "source": "platform-agent/automerge-switch-on-no-pdb-dev-20261111000000", "url": "u"},
            {"state": "open", "source": "platform-agent/automerge-switch-on-other-dev-20261111000000", "url": "u"},
        ):
            with self.subTest(proposal=proposal):
                _, forge, _ = run(EMPTY, False, 11, proposal)
                self.assertNotIn("proposal-close", forge.verbs())


class TestPortFixes(unittest.TestCase):
    def test_a_pending_branch_matches_its_environment_exactly(self):
        # An environment may contain "-": dev's switch must not take dev-eu's branch for its own.
        for source in (
            "platform-agent/automerge-switch-on-no-pdb-dev-eu-20261111000000",
            "platform-agent/automerge-switch-off-no-pdb-dev-eu-20261111000000",
            "platform-agent/automerge-switch-on-no-pdb-dev-1",
        ):
            with self.subTest(source=source):
                _, forge, _ = run(EMPTY, True, 11, {"state": "open", "source": source, "url": "u"})
                self.assertNotIn("proposal-close", forge.verbs())
        self.assertTrue(sw._is_switch_branch("platform-agent/automerge-switch-on-no-pdb-dev-20261111000000", "on", "no-pdb", "dev"))

    def test_a_failure_after_closing_still_reports_the_closed_pull_request(self):
        proposal = {"state": "open", "source": "platform-agent/automerge-switch-off-no-pdb-dev-20261111000000", "url": "https://x/pull/11"}
        forge, vcs = FakeForge(proposal), FakeVcs(EMPTY)
        vcs.clone = mock.Mock(side_effect=RuntimeError("broker down"))
        result = sw.toggle(REPO, "no-pdb", "dev", True, 11, forge=forge, vcs=vcs, now=lambda: 0)
        self.assertEqual((result["state"], result["closed"]), (sw.BLOCKED, "https://x/pull/11"))
        self.assertIn("broker down", result["reason"])

    def test_a_published_branch_whose_pull_request_failed_is_reported(self):
        forge, vcs = FakeForge(), FakeVcs(EMPTY)
        forge.refuse_create = True
        result = sw.toggle(REPO, "no-pdb", "dev", True, None, forge=forge, vcs=vcs, now=lambda: 0)
        self.assertEqual(result["state"], sw.BLOCKED)
        self.assertEqual(result["branchKept"], "platform-agent/automerge-switch-on-no-pdb-dev-19700101000000")


class TestMain(unittest.TestCase):
    def test_refuses_with_the_dial_off(self):
        out = io.StringIO()
        with mock.patch("kubeagents_switches.switch", return_value="off"), redirect_stdout(out):
            code = sw.main(["--kind", "no-pdb", "--environment", "dev", "--state", "on", "--repo", REPO])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())[0]["reason"], "KUBEAGENTS_DIAL is off")

    def test_runs_each_repository_and_reports_a_failure_without_stopping(self):
        seen = []

        def fake(repo, kind, environment, on, pending):
            seen.append((repo, pending, on))
            if repo == "a/one":
                raise RuntimeError("boom")
            return {"repo": repo, "state": sw.OFF}

        out = io.StringIO()
        with mock.patch("kubeagents_switches.switch", return_value="on"), mock.patch.object(sw, "toggle", fake), redirect_stdout(out):
            code = sw.main(["--kind", "no-pdb", "--environment", "dev", "--state", "off", "--repo", "a/one#4", "--repo", "b/two"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("a/one", 4, False), ("b/two", None, False)])
        results = json.loads(out.getvalue())
        self.assertEqual([r["state"] for r in results], [sw.BLOCKED, sw.OFF])
        self.assertIn("boom", results[0]["reason"])

    def test_check_reports_each_pull_requests_state_and_opens_nothing(self):
        seen = []

        def fake(repo, number):
            seen.append((repo, number))
            if repo == "a/one":
                raise RuntimeError("boom")
            return {"repo": repo, "number": number, "state": "merged", "pr": "https://x/pull/7"}

        out = io.StringIO()
        with (
            mock.patch("kubeagents_switches.switch", return_value="on"),
            mock.patch.object(sw, "check", fake),
            mock.patch.object(sw, "toggle", side_effect=AssertionError("toggled")),
            redirect_stdout(out),
        ):
            code = sw.main(["--kind", "no-pdb", "--environment", "dev", "--check", "--repo", "a/one#4", "--repo", "b/two#7"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("a/one", 4), ("b/two", 7)])
        results = json.loads(out.getvalue())
        self.assertEqual([r["state"] for r in results], ["", "merged"])
        self.assertIn("boom", results[0]["reason"])

    def test_check_needs_a_number_and_a_switch_needs_a_state(self):
        with mock.patch("kubeagents_switches.switch", return_value="on"), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                sw.main(["--kind", "no-pdb", "--environment", "dev", "--check", "--repo", REPO])
            with self.assertRaises(SystemExit):
                sw.main(["--kind", "no-pdb", "--environment", "dev", "--repo", REPO])

    def test_check_reads_the_proposal_state(self):
        forge = FakeForge({"state": "merged", "url": "https://x/pull/7"})
        self.assertEqual(
            sw.check(REPO, 7, forge=forge),
            {"repo": REPO, "number": 7, "state": "merged", "pr": "https://x/pull/7"},
        )
        self.assertEqual(forge.verbs(), ["proposal-view"])


if __name__ == "__main__":
    unittest.main()
