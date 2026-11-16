"""Tests for post_merge_offer: the draft rules-file pull request that offers auto-merge."""

import tempfile
import unittest
from pathlib import Path

import automation_rules as ar
import post_merge_offer as pmo

REPO = "acme/gitops"
BRANCH = "platform-agent/automerge-offer-no-pdb-dev"


class FakeForge:
    def __init__(self, *, state="merged", existing=None, held=False, viewer="kube-agents", comments=(), truncated=False):
        self.calls, self.state, self.existing, self.held = [], state, existing, held
        self.viewer, self.comments, self.truncated = viewer, list(comments), truncated

    def __call__(self, verb, payload):
        self.calls.append((verb, payload))
        if verb == "identity":
            return {"identity": {"login": self.viewer}}
        if verb == "proposal-view" and payload.get("comments"):
            return {"comments": self.comments, "commentsTruncated": self.truncated}
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


def run(forge, opener=None, *, automerge="practice", environment="dev", kind="no-pdb"):
    return pmo.offer(REPO, 9, kind, environment, automerge=automerge, forge=forge, open_proposal=opener or FakeOpen())


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
                self.assertTrue(body.startswith("Disruption-budget fixes: at least 5 merged in acme/gitops, none changed or undone."))
                self.assertIn("At least 5 disruption-budget fixes have merged on 2 or more days in acme/gitops", opened["body"])
                self.assertIn("https://x/pull/10", body)
                self.assertIn(pmo.MARKER, body)

    def test_gates(self):
        cases = {
            "prod": dict(environment="prod"),
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
                self.assertEqual(result, {"action": "none", "reason": "already offered", "offer": "https://x/pull/3"})
                self.assertEqual(opener.opened, [])
                self.assertNotIn("proposal-comment", forge.verbs())
                listed = next(p for v, p in forge.calls if v == "proposal-list")
                self.assertEqual(listed, {"source": BRANCH, "state": "all", "limit": 1})

    def test_a_draft_left_without_its_comment_is_finished_on_its_own_fix_once(self):
        draft = {"url": "https://x/pull/3", "state": "open", "body": "...\n\nOffered on https://x/pull/9\n",
                 "author": "kube-agents", "sourceRepo": "Acme/GitOps"}
        theirs = {"author": {"login": "someone"}, "body": pmo.MARKER}
        forge, opener = FakeForge(existing=draft, viewer="kube-agents[bot]", comments=[theirs]), FakeOpen()
        result = run(forge, opener)
        self.assertEqual(result["action"], "offered")
        self.assertEqual(result["offer"], "https://x/pull/3")
        self.assertEqual(opener.opened, [])
        comments = [p for v, p in forge.calls if v == "proposal-comment"]
        self.assertEqual([c["number"] for c in comments], [9])
        self.assertIn("https://x/pull/3", comments[0]["body"])
        self.assertIn(pmo.MARKER, comments[0]["body"])

    def test_an_offer_is_not_finished_twice_late_or_on_another_fix(self):
        mine = {"author": {"login": "kube-agents[bot]"}, "url": "https://x/pull/9#c1", "body": "...\n\n" + pmo.MARKER}
        cases = {
            "commented": (dict(comments=[mine]), "open", "https://x/pull/9", "already offered"),
            "closed": ({}, "closed", "https://x/pull/9", "already offered"),
            "merged": ({}, "merged", "https://x/pull/9", "already offered"),
            "another fix": ({}, "open", "https://x/pull/90", "already offered"),
            "too many comments": (dict(truncated=True), "open", "https://x/pull/9", "not commenting twice"),
            "no identity": (dict(viewer=""), "open", "https://x/pull/9", "did not say who"),
            "a fork's branch": ({}, "open", "https://x/pull/9", "already offered", {"sourceRepo": "mallory/gitops"}),
            "a deleted fork": ({}, "open", "https://x/pull/9", "already offered", {"sourceRepo": ""}),
            "someone else's": ({}, "open", "https://x/pull/9", "already offered", {"author": "mallory"}),
        }
        for name, (kwargs, state, fix, reason, *changed) in cases.items():
            with self.subTest(name):
                draft = {"url": "https://x/pull/3", "state": state, "body": f"Offered on {fix}",
                         "author": {"login": "kube-agents[bot]"}, "sourceRepo": REPO, **(changed[0] if changed else {})}
                forge = FakeForge(existing=draft, **kwargs)
                result = run(forge)
                self.assertIn(reason, result["reason"])
                self.assertNotIn("proposal-comment", forge.verbs())

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


class TestCountsAreNotAnArgument(unittest.TestCase):
    def test_the_cli_takes_no_counts(self):
        with self.assertRaises(SystemExit):
            pmo.main(["--repo", REPO, "--number", "9", "--kind", "no-pdb", "--environment", "dev", "--clean", "99"])


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
