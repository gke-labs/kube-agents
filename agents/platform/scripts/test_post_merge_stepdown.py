"""Tests for post_merge_stepdown: revert an auto-merged fix that is still broken, and step down."""

import subprocess
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import automation_rules as ar
import post_merge_stepdown as pms

REPO = "acme/gitops"
RULES = """\
version: 1
automerge:
  allow:
    - fix: no-pdb
      environments: [dev]
"""


class FakeForge:
    def __init__(self, *, state="merged", labels=(ar.AUTOMERGE_LABEL,), comments=(), existing=None, commits=1):
        self.calls = []
        self.state, self.labels, self.comments = state, list(labels), list(comments)
        self.existing = existing or {}
        self.commits = commits

    def __call__(self, verb, payload):
        self.calls.append((verb, payload))
        if verb == "proposal-view" and payload.get("comments"):
            return {"proposal": {}, "comments": self.comments}
        if verb == "proposal-view":
            return {"proposal": {
                "number": 7, "title": "fix(pdb): add a budget for api", "state": self.state,
                "labels": self.labels, "url": "https://github.com/acme/gitops/pull/7",
                "target": "main", "mergeCommit": "abc123" if self.state == "merged" else None,
            }}
        if verb == "identity":
            return {"identity": {"login": "kube-agents-app[bot]"}}
        if verb == "proposal-list":
            found = self.existing.get(payload["source"])
            return {"proposals": [{"url": found}] if found else []}
        if verb == "proposal-commits":
            return {"commits": [{}] * self.commits}
        if verb == "proposal-comment":
            return {"comment": {"url": "https://github.com/acme/gitops/pull/7#c1"}}
        raise AssertionError(f"unexpected verb {verb}")

    def verbs(self):
        return [verb for verb, _ in self.calls]


class FakeOpen:
    def __init__(self, fail=()):
        self.opened, self.fail = [], set(fail)

    def __call__(self, forge, repo, branch, base, title, body, write):
        if branch in self.fail:
            raise pms.StepDownError(f"{branch} broke")
        self.opened.append((branch, base, title))
        return f"https://github.com/acme/gitops/pull/{len(self.opened) + 7}"


def run(forge, opener=None, *, automerge="on", outcome="still_broken"):
    return pms.step_down(
        REPO, 7, "no-pdb", "dev", outcome, automerge=automerge, forge=forge, open_proposal=opener or FakeOpen()
    )


class TestGates(unittest.TestCase):
    def test_switch_not_on_is_report_only(self):
        for value in ("off", "practice", "garbage"):
            with self.subTest(value):
                forge = FakeForge()
                result = run(forge, automerge=value)
                self.assertEqual(result["action"], "none")
                self.assertIn("report only", result["reason"])
                self.assertEqual(forge.calls, [])

    def test_other_outcomes_do_nothing(self):
        for outcome in ("verified_fixed", "applied_not_rechecked", "not_applied"):
            with self.subTest(outcome):
                forge = FakeForge()
                self.assertEqual(run(forge, outcome=outcome)["action"], "none")
                self.assertEqual(forge.calls, [])

    def test_not_merged(self):
        forge = FakeForge(state="closed")
        self.assertIn("not merged", run(forge)["reason"])
        self.assertNotIn("proposal-comment", forge.verbs())

    def test_merged_by_a_person(self):
        forge = FakeForge(labels=["audit:no-pdb"])
        result = run(forge)
        self.assertEqual(result["action"], "none")
        self.assertIn("a person merged it", result["reason"])
        self.assertNotIn("proposal-comment", forge.verbs())

    def test_already_stepped_down(self):
        forge = FakeForge(comments=[{"author": "kube-agents-app", "body": f"done\n\n{pms.MARKER}", "url": "u"}])
        opener = FakeOpen()
        result = run(forge, opener)
        self.assertEqual(result["reason"], "already stepped down")
        self.assertEqual(opener.opened, [])

    def test_marker_from_someone_else_does_not_count(self):
        forge = FakeForge(comments=[{"author": "mallory", "body": pms.MARKER, "url": "u"}])
        self.assertEqual(run(forge)["action"], "stepped-down")


class TestStepDown(unittest.TestCase):
    def test_opens_both_and_says_so_once(self):
        forge, opener = FakeForge(), FakeOpen()
        result = run(forge, opener)
        self.assertEqual(result["action"], "stepped-down")
        self.assertEqual(
            [(b, base) for b, base, _ in opener.opened],
            [("kube-agents/revert-7", "main"), ("kube-agents/automerge-step-down-7", "main")],
        )
        comments = [p for v, p in forge.calls if v == "proposal-comment"]
        self.assertEqual(len(comments), 1)
        body = comments[0]["body"]
        self.assertIn(pms.MARKER, body)
        self.assertIn(result["revert"], body)
        self.assertIn(result["stepDown"], body)
        self.assertEqual(body.count("\n"), 2)  # one line, a blank, the marker
        self.assertNotIn("errors", result)
        for verb in ("proposal-update", "proposal-close", "label-ensure"):
            self.assertNotIn(verb, forge.verbs())

    def test_existing_pull_requests_are_reused(self):
        forge = FakeForge(existing={"kube-agents/revert-7": "R", "kube-agents/automerge-step-down-7": "S"})
        opener = FakeOpen()
        result = run(forge, opener)
        self.assertEqual(opener.opened, [])
        self.assertEqual((result["revert"], result["stepDown"]), ("R", "S"))
        self.assertEqual(forge.verbs().count("proposal-comment"), 1)

    def test_a_refusal_is_said_and_the_other_still_opens(self):
        forge = FakeForge()
        result = run(forge, FakeOpen(fail={"kube-agents/revert-7"}))
        self.assertEqual(result["action"], "stepped-down")
        self.assertNotIn("revert", result)
        self.assertTrue(result["stepDown"])
        self.assertEqual(result["refused"], {"revert": "kube-agents/revert-7 broke"})
        body = next(p for v, p in forge.calls if v == "proposal-comment")["body"]
        self.assertTrue(body.startswith("Still broken after merge, so I opened a pull request to stop"))
        self.assertIn("I did not open a revert: kube-agents/revert-7 broke.", body)

    def test_both_refused_still_comments_once(self):
        forge = FakeForge()
        run(forge, FakeOpen(fail={"kube-agents/revert-7", "kube-agents/automerge-step-down-7"}))
        body = next(p for v, p in forge.calls if v == "proposal-comment")["body"]
        self.assertTrue(body.startswith("Still broken after merge. I did not open a revert"))

    def test_a_transient_failure_waits_for_the_next_run(self):
        class Flaky(FakeOpen):
            def __call__(self, forge, repo, branch, *rest):
                if branch == "kube-agents/revert-7":
                    raise OSError("broker did not answer")
                return super().__call__(forge, repo, branch, *rest)

        forge = FakeForge()
        result = run(forge, Flaky())
        self.assertEqual(result["action"], "retry")
        self.assertTrue(result["stepDown"])
        self.assertNotIn("proposal-comment", forge.verbs())


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class FakeVcsClient:
    """The two `vcs_client` calls the writers make, against a real local repository."""

    def local_git(self, tree, *args, check=True, text=True):
        return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=text, check=check)

    def commit(self, message, paths=(), spec=None, key=None):
        self.committed = (message, list(paths), spec, key)
        tree = self.tree
        if paths:
            _git(tree, "add", "--", *paths)
        else:
            _git(tree, "add", "--update")
        _git(tree, "commit", "-q", "-m", message)


def _repo():
    tree = Path(tempfile.mkdtemp())
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "t")
    (tree / ".kube-agents").mkdir()
    (tree / ar.RULES_FILE).write_text(RULES)
    (tree / "app.yaml").write_text("kind: Deployment\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "base")
    return tree


class TestWriters(unittest.TestCase):
    def setUp(self):
        self.tree = _repo()
        self.vcs = FakeVcsClient()
        self.vcs.tree = self.tree

    def _fix_branch(self, commits=1):
        _git(self.tree, "switch", "-q", "-c", "fix")
        for i in range(commits):
            (self.tree / f"pdb-{i}.yaml").write_text("kind: PodDisruptionBudget\n")
            _git(self.tree, "add", "-A")
            _git(self.tree, "commit", "-q", "-m", f"fix {i}")
        _git(self.tree, "switch", "-q", "main")

    def test_revert_of_a_merge_commit(self):
        self._fix_branch(commits=2)
        _git(self.tree, "merge", "-q", "--no-ff", "-m", "merge fix", "fix")
        merge = _git(self.tree, "rev-parse", "HEAD")
        _git(self.tree, "switch", "-q", "-c", "kube-agents/revert-7")
        pms._revert_writer({"title": "fix"}, 7, merge, 2)(self.tree, self.vcs, REPO, "kube-agents/revert-7")
        self.assertFalse((self.tree / "pdb-0.yaml").exists())
        self.assertFalse((self.tree / "pdb-1.yaml").exists())
        self.assertIn(f"This reverts {merge}.", _git(self.tree, "log", "-1", "--format=%B"))
        self.assertEqual(self.vcs.committed[2:], (REPO, "kube-agents/revert-7"))

    def test_revert_of_a_squash(self):
        self._fix_branch(commits=1)
        _git(self.tree, "merge", "-q", "--squash", "fix")
        _git(self.tree, "commit", "-q", "-m", "squashed")
        merge = _git(self.tree, "rev-parse", "HEAD")
        pms._revert_writer({"title": "fix"}, 7, merge, 1)(self.tree, self.vcs, REPO, "b")
        self.assertFalse((self.tree / "pdb-0.yaml").exists())

    def test_one_parent_and_several_commits_is_refused(self):
        self._fix_branch(commits=2)
        _git(self.tree, "merge", "-q", "--ff-only", "fix")
        merge = _git(self.tree, "rev-parse", "HEAD")
        with self.assertRaises(pms.StepDownError):
            pms._revert_writer({}, 7, merge, 2)(self.tree, self.vcs, REPO, "b")
        self.assertTrue((self.tree / "pdb-0.yaml").exists())

    def test_unknown_merge_commit_is_refused(self):
        with self.assertRaises(pms.StepDownError):
            pms._revert_writer({}, 7, "0" * 40, 1)(self.tree, self.vcs, REPO, "b")

    def test_step_down_adds_a_deny_rule(self):
        pms._step_down_writer("no-pdb", "dev", "https://x/pull/7")(self.tree, self.vcs, REPO, "b")
        rules = ar.load(self.tree)
        self.assertEqual(rules.status, ar.LOADED)
        self.assertFalse(ar.decide(rules, "no-pdb", "dev").allowed)
        self.assertIn("https://x/pull/7 was still broken", (self.tree / ar.RULES_FILE).read_text())
        self.assertEqual(self.vcs.committed[1], [ar.RULES_FILE])

    def test_step_down_without_a_rules_file_is_refused(self):
        (self.tree / ar.RULES_FILE).unlink()
        with self.assertRaises(pms.StepDownError):
            pms._step_down_writer("no-pdb", "dev", "u")(self.tree, self.vcs, REPO, "b")


class TestOpen(unittest.TestCase):
    def test_published_branch_without_a_pull_request_is_opened_as_is(self):
        calls = []

        def forge(verb, payload):
            calls.append(verb)
            if verb == "branch-view":
                return {"branch": {"exists": True}}
            return {"proposal": {"url": "P"}}

        fake = types.ModuleType("vcs_client")
        with unittest.mock.patch.dict("sys.modules", {"vcs_client": fake}):
            url = pms._open(forge, REPO, "kube-agents/revert-7", "main", "t", "b", lambda *a: None)
        self.assertEqual(url, "P")
        self.assertEqual(calls, ["branch-view", "proposal-create"])


if __name__ == "__main__":
    unittest.main()
