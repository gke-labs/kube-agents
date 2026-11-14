"""Tests for post_merge_offer: the draft rules-file pull request that offers auto-merge."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import automation_rules as ar
import post_merge_offer as pmo

REPO = "acme/gitops"
BRANCH = "platform-agent/automerge-offer-no-pdb-dev"


class FakeForge:
    def __init__(self, *, state="merged", existing=None, held=False):
        self.calls, self.state, self.existing, self.held = [], state, existing, held

    def __call__(self, verb, payload):
        self.calls.append((verb, payload))
        if verb == "branch-view":
            return {"branch": {"exists": self.held}}
        if verb == "proposal-list":
            return {"proposals": [self.existing] if self.existing else []}
        if verb == "proposal-view":
            return {"proposal": {"state": self.state, "target": "main", "url": "https://x/pull/9"}}
        if verb == "proposal-comment":
            return {"comment": {"url": "https://x/pull/9#c"}}
        raise AssertionError(verb)

    def verbs(self):
        return [v for v, _ in self.calls]


class FakeOpen:
    def __init__(self, raises=None):
        self.opened, self.raises = [], raises

    def __call__(self, forge, repo, branch, base, title, body, write, *, draft=False):
        if self.raises:
            raise self.raises
        self.opened.append({"branch": branch, "base": base, "title": title, "body": body, "draft": draft})
        return "https://x/pull/10"


def run(forge, opener=None, *, automerge="practice", environment="dev", clean=5, days=2, kind="no-pdb"):
    return pmo.offer(
        REPO, 9, kind, environment, clean, days, automerge=automerge, forge=forge, open_proposal=opener or FakeOpen()
    )


class TestOffer(unittest.TestCase):
    def test_switch_off_does_nothing(self):
        forge = FakeForge()
        self.assertEqual(run(forge, automerge="off")["action"], "none")
        self.assertEqual(forge.calls, [])

    def test_offered_under_practice_and_on(self):
        for value in ("practice", "on"):
            with self.subTest(value):
                forge, opener = FakeForge(), FakeOpen()
                result = run(forge, opener, automerge=value)
                self.assertEqual(result["action"], "offered")
                self.assertEqual(len(opener.opened), 1)
                opened = opener.opened[0]
                self.assertEqual((opened["branch"], opened["base"], opened["draft"]), (BRANCH, "main", True))
                self.assertEqual(opened["title"], "Auto-merge disruption-budget fixes in dev")
                comments = [p for v, p in forge.calls if v == "proposal-comment"]
                self.assertEqual(len(comments), 1)
                self.assertEqual(comments[0]["number"], 9)
                body = comments[0]["body"]
                self.assertTrue(body.startswith("Disruption-budget fixes: 5 merged in acme/gitops, none changed or undone."))
                self.assertIn("have merged in acme/gitops with nobody changing them", opened["body"])
                self.assertIn("https://x/pull/10", body)
                self.assertIn(pmo.MARKER, body)

    def test_gates(self):
        cases = {
            "prod": dict(environment="prod"),
            "not enough": dict(clean=4),
            "one day": dict(days=1),
            "unknown kind": dict(kind="no-hpa"),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                forge, opener = FakeForge(), FakeOpen()
                self.assertEqual(run(forge, opener, **kwargs)["action"], "none")
                self.assertEqual(opener.opened, [])
                self.assertNotIn("proposal-comment", forge.verbs())

    def test_never_repeats(self):
        for state in ("open", "closed", "merged"):
            with self.subTest(state):
                existing = {"url": "https://x/pull/3", "state": state, "body": "...\n\nOffered on https://x/pull/4"}
                forge, opener = FakeForge(existing=existing), FakeOpen()
                result = run(forge, opener)
                self.assertEqual(result, {"action": "none", "reason": "already offered"})
                self.assertEqual(opener.opened, [])
                self.assertNotIn("proposal-comment", forge.verbs())
                listed = next(p for v, p in forge.calls if v == "proposal-list")
                self.assertEqual(listed, {"source": BRANCH, "state": "all", "limit": 1})

    def test_the_repository_is_named_as_a_person_reads_it(self):
        for repo in ("https://github.com/org/infra", "https://github.com/org/infra.git/", "git@github.com:org/infra.git",
                     "org/infra"):
            with self.subTest(repo):
                self.assertEqual(pmo.repo_name(repo), "org/infra")
        self.assertEqual(pmo.repo_name("https://gitlab.example.com/group/sub/infra"), "group/sub/infra")
        for repo in ("github.com/org/infra", "github.com:org/infra.git", "https://github.com/org/infra.git//"):
            with self.subTest(repo):
                self.assertEqual(pmo.repo_name(repo), "org/infra")

    def test_body_names_the_fix(self):
        opener = FakeOpen()
        run(FakeForge(), opener)
        self.assertTrue(opener.opened[0]["body"].endswith("Offered on https://x/pull/9"))

    def test_a_race_reads_as_already_offered(self):
        class Racing(FakeForge):
            def __call__(self, verb, payload):
                if verb == "proposal-list" and any(v == "proposal-list" for v, _ in self.calls):
                    self.calls.append((verb, payload))
                    return {"proposals": [{"url": "https://x/pull/11", "state": "open", "body": "Offered on https://x/pull/8"}]}
                return super().__call__(verb, payload)

        forge = Racing()
        result = run(forge, FakeOpen(raises=RuntimeError("422 a pull request already exists")))
        self.assertEqual(result["reason"], "already offered")
        self.assertNotIn("proposal-comment", forge.verbs())

    def test_other_errors_propagate(self):
        with self.assertRaises(RuntimeError):
            run(FakeForge(), FakeOpen(raises=RuntimeError("broker down")))

    def test_a_published_branch_without_a_pull_request_is_not_offered_from(self):
        forge, opener = FakeForge(held=True), FakeOpen()
        result = run(forge, opener)
        self.assertEqual(result["action"], "none")
        self.assertIn("published without a pull request", result["reason"])
        self.assertEqual(opener.opened, [])
        self.assertNotIn("proposal-comment", forge.verbs())

    def test_unmerged_fix(self):
        forge = FakeForge(state="open")
        self.assertIn("not merged", run(forge)["reason"])

    def test_writer_refusal_is_no_offer(self):
        forge = FakeForge()
        result = run(forge, FakeOpen(raises=pmo.NoOffer("already allows")))
        self.assertEqual(result, {"action": "none", "reason": "already allows"})
        self.assertNotIn("proposal-comment", forge.verbs())


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class FakeVcs:
    def __init__(self, tree):
        self.tree, self.committed = tree, None

    def commit(self, message, paths=(), spec=None, key=None):
        self.committed = (message, list(paths), spec, key)


class TestAllowWriter(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        self.vcs = FakeVcs(self.tree)

    def _write(self, text):
        (self.tree / ".kube-agents").mkdir(exist_ok=True)
        (self.tree / ar.RULES_FILE).write_text(text)

    def _run(self):
        pmo._allow_writer("no-pdb", "dev")(self.tree, self.vcs, REPO, BRANCH)

    def test_creates_the_file_when_absent(self):
        self._run()
        rules = ar.load(self.tree)
        self.assertEqual(rules.allow, (ar.Rule("no-pdb", ("dev",)),))
        self.assertEqual(self.vcs.committed, ("chore(kube-agents): auto-merge no-pdb fixes in dev", [ar.RULES_FILE], REPO, BRANCH))

    def test_adds_to_an_existing_file(self):
        self._write("version: 1\nautomerge:\n  allow:\n    - fix: no-pdb\n      environments: [staging]\n")
        self._run()
        self.assertTrue(ar.decide(ar.load(self.tree), "no-pdb", "dev").allowed)
        self.assertTrue(ar.decide(ar.load(self.tree), "no-pdb", "staging").allowed)

    def test_a_rules_path_that_is_not_a_file_is_no_offer(self):
        for name in ("directory where the file goes", "file where the directory goes"):
            with self.subTest(name):
                self.tree = Path(tempfile.mkdtemp())
                if name.startswith("directory"):
                    (self.tree / ar.RULES_FILE).mkdir(parents=True)
                else:
                    (self.tree / ".kube-agents").write_text("x")
                with self.assertRaises(pmo.NoOffer):
                    self._run()
                self.assertIsNone(self.vcs.committed)

    def test_refusals(self):
        for name, text in {
            "already allowed": "version: 1\nautomerge: {allow: [{fix: no-pdb, environments: [dev]}]}\n",
            "denied": "version: 1\nautomerge: {deny: [{fix: '*', environments: [dev]}]}\n",
            "refused file": "version: 2\n",
        }.items():
            with self.subTest(name):
                self._write(text)
                with self.assertRaises(pmo.NoOffer):
                    self._run()
                self.assertEqual((self.tree / ar.RULES_FILE).read_text(), text)
                self.assertIsNone(self.vcs.committed)


if __name__ == "__main__":
    unittest.main()
