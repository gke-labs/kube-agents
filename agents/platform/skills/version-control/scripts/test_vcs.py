"""Tests for the sandbox side of the version-control abstraction.

`vcs.py` is two halves and they are tested differently. The local half runs a
real `git` against a real working copy, because what it claims is that the
sandbox holds a repository rather than a directory listing — `log`, `annotate`
and the file modes are the evidence, and a mock would supply them for free. The
broker half is replaced by a recorder that speaks the same JSON, so the tests can
assert the request that would have gone over loopback.

The recorder is not a second implementation of the broker. It answers `clone`
with a bundle built from a real repository and records everything else, which is
enough for these tests and deliberately not enough to hide a protocol mismatch:
`test_vcs_broker.py` runs the real thing against the real payloads.

One class here asserts about the source text rather than about behaviour.
`AbstractionTest` is the check that this container names no forge — the property
the whole design exists for, and the one a behavioural test cannot see, because
code that shells out to `gh` for the one case the abstraction missed passes every
other test in this file.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[4] / "scripts")
)

import vcs  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

REAL_GIT = shutil.which("git") or "/usr/bin/git"


def git(cwd: Path, *args: str, check: bool = True):
    return subprocess.run(
        [REAL_GIT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **GIT_ENV},
    )


class Broker:
    """A recorder in the shape of `POST /v1/vcs/<verb>`.

    `clone` is answered for real, from a repository on disk, because everything
    the local half does afterwards depends on getting a usable bundle. The rest
    records the payload and returns a canned object; what those tests are about
    is the request, which is the only thing this container composes.
    """

    def __init__(self, origin: Path, branch: str = "main") -> None:
        self.origin = origin
        self.branch = branch
        self.calls: list[tuple[str, dict]] = []
        self.answers: dict[str, dict] = {}
        self.fail: dict[str, str] = {}

    def __call__(self, verb: str, payload: dict) -> dict:
        self.calls.append((verb, payload))
        if verb in self.fail:
            raise vcs.VcsError(self.fail[verb])
        if verb == "clone":
            return self._clone(payload)
        if verb == "publish":
            return {
                "forge": "local",
                "repo": "acme/infra",
                "branch": payload["branch"],
                "revision": self._tip_of(payload),
            }
        if verb == "capabilities":
            return {
                "forge": "local",
                "repo": "acme/infra",
                "proposalNoun": "change proposal",
                "verbs": ["clone", "publish"],
                "missing": [],
            }
        return self.answers.get(verb, {"forge": "local", "repo": "acme/infra"})

    def _clone(self, payload: dict) -> dict:
        branch = payload.get("branch") or self.branch
        bundle = self.origin.parent / "served.bundle"
        git(self.origin, "bundle", "create", str(bundle), "HEAD", branch)
        blob = bundle.read_bytes()
        return {
            "forge": "local",
            "repo": "acme/infra",
            "branch": branch,
            "revision": git(self.origin, "rev-parse", branch).stdout.strip(),
            "size": len(blob),
            "bundleBase64": base64.b64encode(blob).decode("ascii"),
        }

    def _tip_of(self, payload: dict) -> str:
        """Read the bundle the caller sent, the way the broker would."""
        scratch = self.origin.parent / "received.bundle"
        scratch.write_bytes(base64.b64decode(payload["bundleBase64"]))
        listed = git(self.origin, "bundle", "list-heads", str(scratch)).stdout
        return listed.split()[0] if listed.split() else ""

    def payload(self, verb: str) -> dict:
        for name, payload in reversed(self.calls):
            if name == verb:
                return payload
        raise AssertionError(f"{verb} was never called; saw {[c[0] for c in self.calls]}")

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


class VcsTestCase(unittest.TestCase):
    """A working copy, a recorder, and `vcs.py` pointed at both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)

        self.origin = base / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet", "--initial-branch=main")
        (self.origin / "README.md").write_text("first line\nsecond line\n")
        (self.origin / "rotate-keys.sh").write_text("#!/bin/sh\necho rotate\n")
        os.chmod(self.origin / "rotate-keys.sh", 0o755)
        (self.origin / "inventory").mkdir()
        (self.origin / "inventory/clusters.yaml").write_text("replicas: 2\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "--quiet", "-m", "seed the repository")
        (self.origin / "README.md").write_text("first line\nchanged line\n")
        git(self.origin, "commit", "--quiet", "-a", "-m", "change the second line")
        self.origin_head = git(self.origin, "rev-parse", "HEAD").stdout.strip()

        self.root = base / "vcsroot"
        self.broker = Broker(self.origin)
        for attribute, value in (
            ("ROOT", self.root),
            ("SESSIONS", self.root / ".sessions"),
            ("LOCAL_GIT", REAL_GIT),
            ("call", self.broker),
        ):
            patch = mock.patch.object(vcs, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_vcs(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = vcs.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def clone(self, *extra: str) -> dict:
        code, answer = self.run_vcs("clone", "local.test/acme/infra", *extra)
        self.assertEqual(code, 0, answer)
        return answer

    def tree(self) -> Path:
        return Path(vcs.all_sessions()[0]["path"])


# ---------------------------------------------------------------------------
# clone
# ---------------------------------------------------------------------------


class CloneTest(VcsTestCase):
    def test_a_clone_lands_as_a_repository_not_a_listing(self):
        answer = self.clone()
        self.assertEqual(answer["revision"], self.origin_head)
        self.assertEqual(answer["history"], "complete")
        self.assertEqual(answer["files"], 3)
        tree = Path(answer["path"])
        self.assertEqual(
            git(tree, "rev-list", "--count", "HEAD").stdout.strip(), "2"
        )
        self.assertEqual((tree / "inventory/clusters.yaml").read_text(), "replicas: 2\n")

    def test_the_copy_has_no_remote_to_be_talked_into_using(self):
        # A remote is a thing a later command can fetch from or push to. There
        # is nothing in this container that should ever do either; revisions go
        # up through `publish`.
        answer = self.clone()
        self.assertEqual(answer["remotes"], [])
        self.assertEqual(git(Path(answer["path"]), "remote").stdout.strip(), "")

    def test_the_executable_bit_survives_the_bundle(self):
        answer = self.clone()
        self.assertTrue(os.access(Path(answer["path"]) / "rotate-keys.sh", os.X_OK))

    def test_a_second_clone_replaces_a_clean_first(self):
        first = self.clone()
        second = self.clone()
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(len(vcs.all_sessions()), 1)

    def test_a_second_clone_refuses_to_discard_work(self):
        # It used to replace the tree silently, so a commit made here and never
        # published was gone with no message. Worse in context: the publish
        # refusals say to clone again, which pointed the caller straight at it.
        first = self.clone()
        (Path(first["path"]) / "stale.txt").write_text("x")
        code, answer = self.run_vcs("clone", "local.test/acme/infra")
        self.assertEqual(code, 1)
        self.assertIn("--force", answer["error"])
        self.assertTrue((Path(first["path"]) / "stale.txt").exists())

    def test_force_replaces_it_anyway(self):
        first = self.clone()
        (Path(first["path"]) / "stale.txt").write_text("x")
        second = self.clone("--force")
        self.assertEqual(first["path"], second["path"])
        self.assertFalse((Path(second["path"]) / "stale.txt").exists())
        self.assertEqual(len(vcs.all_sessions()), 1)

    def test_the_bundle_file_is_not_left_behind(self):
        self.clone()
        leftovers = [p.name for p in self.root.glob("*.bundle")]
        self.assertEqual(leftovers, [])

    def test_a_named_branch_is_passed_through(self):
        answer = self.clone("--branch", "main")
        self.assertEqual(self.broker.payload("clone")["branch"], "main")
        self.assertEqual(answer["branch"], "main")

    def test_there_is_no_depth_option_to_reach_for(self):
        # A bundle cannot carry a shallow boundary -- `git bundle create` in a
        # shallow repository writes one whose boundary revisions name parents it
        # does not hold. So the option that would produce that does not exist,
        # rather than failing at the far end where the caller cannot act on it.
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            vcs.main(["clone", "acme/infra", "--depth", "1"])

    def test_the_base_revision_is_what_the_broker_handed_out(self):
        self.clone()
        self.assertEqual(vcs.all_sessions()[0]["baseRevision"], self.origin_head)


# ---------------------------------------------------------------------------
# reading history, locally
# ---------------------------------------------------------------------------


class HistoryTest(VcsTestCase):
    def setUp(self):
        super().setUp()
        self.clone()
        self.broker.calls.clear()

    def test_reading_history_asks_the_broker_nothing(self):
        # This is the arm-C claim in one assertion: every question about the
        # past is answered in this container, with no credential spent.
        for argv in (
            ["log"],
            ["history", "-n", "1"],
            ["show", "HEAD"],
            ["annotate", "README.md"],
            ["blame", "README.md"],
            ["files"],
            ["manifest"],
            ["grep", "replicas"],
            ["search", "replicas"],
            ["status"],
            ["diff"],
            ["branch"],
        ):
            with self.subTest(argv=argv):
                code, answer = self.run_vcs(*argv)
                self.assertEqual(code, 0, answer)
        self.assertEqual(self.broker.calls, [])

    def test_log_prints_revisions_rather_than_an_ambiguous_argument(self):
        # `--format` carries a format string. Appended raw it becomes a
        # positional and git reads it as a revision.
        code, answer = self.run_vcs("log", "--format", "%h %s")
        self.assertEqual(code, 0)
        self.assertEqual(answer["exitCode"], 0)
        self.assertNotIn("ambiguous argument", answer["stderr"])
        self.assertIn("change the second line", answer["stdout"])

    def test_log_restricted_to_a_path(self):
        code, answer = self.run_vcs("log", "--", "inventory/clusters.yaml")
        self.assertEqual(code, 0)
        self.assertEqual(len(answer["stdout"].splitlines()), 1)

    def test_annotate_attributes_each_line_to_a_revision(self):
        code, answer = self.run_vcs("annotate", "README.md")
        self.assertEqual(code, 0)
        lines = answer["stdout"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[0].split()[0], lines[1].split()[0])

    def test_files_reports_the_mode_the_revision_records(self):
        code, answer = self.run_vcs("files")
        self.assertEqual(code, 0)
        modes = {entry["path"]: entry["mode"] for entry in answer["files"]}
        self.assertEqual(modes["rotate-keys.sh"], "100755")
        self.assertEqual(modes["README.md"], "100644")
        self.assertEqual(answer["count"], 3)
        self.assertNotIn("stdout", answer)

    def test_grep_with_no_match_is_an_answer_not_a_failure(self):
        code, answer = self.run_vcs("grep", "nothing-matches-this")
        self.assertEqual(code, 0)
        self.assertEqual(answer["exitCode"], 0)
        self.assertEqual(answer["matches"], 0)

    def test_grep_counts_its_matches(self):
        code, answer = self.run_vcs("grep", "line")
        self.assertEqual(code, 0)
        self.assertEqual(answer["matches"], 2)

    def test_grep_treats_the_pattern_as_text_unless_asked(self):
        code, plain = self.run_vcs("grep", "replicas:")
        self.assertEqual(plain["matches"], 1)
        code, regex = self.run_vcs("grep", "--regex", "replicas: *[0-9]")
        self.assertEqual(code, 0)
        self.assertEqual(regex["matches"], 1)

    def test_status_separates_state_from_path(self):
        (self.tree() / "new.txt").write_text("x\n")
        code, answer = self.run_vcs("status")
        self.assertEqual(code, 0)
        self.assertEqual(answer["changes"], [{"state": "??", "path": "new.txt"}])


# ---------------------------------------------------------------------------
# writing, locally, then publishing
# ---------------------------------------------------------------------------


class WriteTest(VcsTestCase):
    def setUp(self):
        super().setUp()
        self.clone()
        self.broker.calls.clear()

    def change(self, path: str, text: str) -> None:
        target = self.tree() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_branch_and_commit_are_local(self):
        code, branched = self.run_vcs("branch", "fix/replicas")
        self.assertEqual(code, 0)
        self.assertTrue(branched["created"])
        self.assertEqual(branched["branch"], "fix/replicas")
        self.change("inventory/clusters.yaml", "replicas: 5\n")
        code, committed = self.run_vcs("commit", "-m", "raise the replica count")
        self.assertEqual(code, 0)
        self.assertEqual(committed["files"], ["inventory/clusters.yaml"])
        self.assertFalse(committed["published"])
        self.assertEqual(self.broker.calls, [])

    def test_a_branch_of_several_changes_stays_several_revisions(self):
        # The reason `commit` is local. A protocol that only carries file
        # contents flattens the work into one revision at the far end.
        self.run_vcs("branch", "fix/replicas")
        for index in range(3):
            self.change("inventory/clusters.yaml", f"replicas: {index}\n")
            self.run_vcs("commit", "-m", f"step {index}")
        code, answer = self.run_vcs("log")
        self.assertEqual(code, 0)
        self.assertEqual(len(answer["stdout"].splitlines()), 5)
        code, published = self.run_vcs("publish")
        self.assertEqual(code, 0)
        self.assertEqual(published["revisions"], 3)

    def test_branch_switches_back_to_one_that_exists(self):
        self.run_vcs("branch", "fix/replicas")
        code, answer = self.run_vcs("branch", "main")
        self.assertEqual(code, 0)
        self.assertFalse(answer["created"])
        self.assertEqual(answer["branch"], "main")
        code, listing = self.run_vcs("branch")
        self.assertEqual(sorted(listing["branches"]), ["fix/replicas", "main"])

    def test_commit_refuses_when_nothing_changed(self):
        code, answer = self.run_vcs("commit", "-m", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("nothing to record", answer["error"])

    def test_commit_takes_named_paths_only(self):
        self.change("inventory/clusters.yaml", "replicas: 9\n")
        self.change("untouched.txt", "leave me\n")
        code, answer = self.run_vcs(
            "commit", "inventory/clusters.yaml", "-m", "one file"
        )
        self.assertEqual(code, 0)
        self.assertEqual(answer["files"], ["inventory/clusters.yaml"])
        code, status = self.run_vcs("status")
        self.assertEqual([c["path"] for c in status["changes"]], ["untouched.txt"])

    def test_publish_sends_a_bundle_of_what_came_after_the_base(self):
        self.run_vcs("branch", "fix/replicas")
        self.change("inventory/clusters.yaml", "replicas: 5\n")
        code, committed = self.run_vcs("commit", "-m", "raise it")
        code, answer = self.run_vcs("publish")
        self.assertEqual(code, 0)
        payload = self.broker.payload("publish")
        self.assertEqual(payload["branch"], "fix/replicas")
        self.assertEqual(payload["target"], "main")
        self.assertEqual(payload["baseRevision"], self.origin_head)
        self.assertEqual(answer["revisions"], 1)
        # The bundle really is a bundle, and it really carries that revision.
        blob = base64.b64decode(payload["bundleBase64"])
        self.assertTrue(blob.startswith(b"# v2 git bundle"), blob[:20])
        self.assertIn(committed["revision"], blob.decode("latin-1"))

    def test_publish_advances_the_base_so_a_second_one_sends_only_the_rest(self):
        self.run_vcs("branch", "fix/replicas")
        self.change("a.txt", "a\n")
        self.run_vcs("commit", "-m", "a")
        self.run_vcs("publish")
        first_base = vcs.all_sessions()[0]["published"]["fix/replicas"]
        self.assertNotEqual(first_base, self.origin_head)
        self.change("b.txt", "b\n")
        self.run_vcs("commit", "-m", "b")
        code, answer = self.run_vcs("publish")
        self.assertEqual(code, 0)
        self.assertEqual(answer["revisions"], 1)
        self.assertEqual(self.broker.payload("publish")["baseRevision"], first_base)

    def test_a_second_branch_publishes_from_the_clone_point_not_the_first_tip(self):
        # Found live. The published tip used to be one scalar on the session, so
        # a branch made after another was published inherited that branch's tip
        # as its base -- a revision on no target, which the broker's ancestry
        # check reads as a rewritten target and refuses.
        self.run_vcs("branch", "fix/one")
        self.change("a.txt", "a\n")
        self.run_vcs("commit", "-m", "a")
        self.run_vcs("publish")
        first_tip = vcs.all_sessions()[0]["published"]["fix/one"]

        self.run_vcs("branch", "fix/two")
        self.change("b.txt", "b\n")
        self.run_vcs("commit", "-m", "b")
        code, answer = self.run_vcs("publish")
        self.assertEqual(code, 0, answer)
        payload = self.broker.payload("publish")
        self.assertEqual(payload["branch"], "fix/two")
        self.assertEqual(payload["baseRevision"], self.origin_head)
        self.assertNotEqual(payload["baseRevision"], first_tip)
        # And the first branch keeps its own answer.
        self.assertEqual(vcs.all_sessions()[0]["published"]["fix/one"], first_tip)

    def test_publish_refuses_when_there_is_nothing_new(self):
        code, answer = self.run_vcs("publish")
        self.assertEqual(code, 1)
        self.assertIn("no new revisions", answer["error"])
        self.assertEqual(self.broker.verbs, [])

    def test_publish_leaves_no_bundle_behind_even_when_the_broker_refuses(self):
        self.run_vcs("branch", "fix/replicas")
        self.change("a.txt", "a\n")
        self.run_vcs("commit", "-m", "a")
        self.broker.fail["publish"] = "main has moved on the remote"
        code, answer = self.run_vcs("publish")
        self.assertEqual(code, 1)
        self.assertIn("moved on", answer["error"])
        self.assertEqual([p.name for p in self.root.glob("*.bundle")], [])
        # And the base is not advanced by a publish that did not happen.
        self.assertEqual(vcs.all_sessions()[0]["baseRevision"], self.origin_head)

    def test_discard_removes_the_copy_and_its_session(self):
        path = self.tree()
        code, answer = self.run_vcs("discard")
        self.assertEqual(code, 0)
        self.assertEqual(answer["removed"], str(path))
        self.assertFalse(path.exists())
        self.assertEqual(vcs.all_sessions(), [])
        # Nothing is released on the credential side because nothing was held.
        self.assertEqual(self.broker.calls, [])


# ---------------------------------------------------------------------------
# which working copy a verb is about
# ---------------------------------------------------------------------------


class SessionTest(VcsTestCase):
    def test_a_verb_needs_no_repository_when_there_is_only_one(self):
        self.clone()
        code, _ = self.run_vcs("log")
        self.assertEqual(code, 0)

    def test_the_spec_the_caller_typed_finds_the_copy(self):
        self.clone()
        for spec in ("acme/infra", "infra", "local.test/acme/infra"):
            with self.subTest(spec=spec):
                code, answer = self.run_vcs("log", "--repo", spec)
                self.assertEqual(code, 0, answer)

    def test_an_unknown_repository_says_how_to_get_one(self):
        self.clone()
        code, answer = self.run_vcs("log", "--repo", "someone/else")
        self.assertEqual(code, 1)
        self.assertIn("clone", answer["error"])

    def test_with_no_copy_at_all_the_error_says_so(self):
        code, answer = self.run_vcs("log")
        self.assertEqual(code, 1)
        self.assertIn("no local copy of anything", answer["error"])

    def test_two_copies_and_no_repo_asks_which(self):
        self.clone()
        second = dict(vcs.all_sessions()[0])
        second.update({"repo": "acme/other", "spec": "acme/other"})
        vcs.save_session(second)
        code, answer = self.run_vcs("log")
        self.assertEqual(code, 1)
        self.assertIn("--repo", answer["error"])
        self.assertIn("acme/other", answer["error"])

    def test_a_session_whose_tree_is_gone_says_to_clone_again(self):
        self.clone()
        shutil.rmtree(self.tree())
        code, answer = self.run_vcs("log")
        self.assertEqual(code, 1)
        self.assertIn("clone", answer["error"])

    def test_an_unreadable_session_file_is_skipped_not_fatal(self):
        self.clone()
        (self.root / ".sessions/broken.json").write_text("{not json")
        code, _ = self.run_vcs("log")
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# the collaboration verbs
# ---------------------------------------------------------------------------


class CollaborationTest(VcsTestCase):
    def setUp(self):
        super().setUp()
        self.clone()
        self.broker.calls.clear()

    def test_proposal_create_defaults_both_branches_from_the_copy(self):
        self.run_vcs("branch", "fix/replicas")
        code, _ = self.run_vcs("proposal", "create", "--title", "Raise replicas")
        self.assertEqual(code, 0)
        payload = self.broker.payload("proposal-create")
        self.assertEqual(payload["source"], "fix/replicas")
        self.assertEqual(payload["target"], "main")
        self.assertEqual(payload["title"], "Raise replicas")

    def test_pr_and_mr_reach_the_same_verb(self):
        # The neutral noun is the command; a caller who knows one forge's word
        # for it should not have to unlearn it.
        for alias in ("pr", "mr", "proposal"):
            with self.subTest(alias=alias):
                self.broker.calls.clear()
                code, _ = self.run_vcs(alias, "list")
                self.assertEqual(code, 0)
                self.assertEqual(self.broker.verbs, ["proposal-list"])

    def test_absent_options_are_not_sent_as_nulls(self):
        code, _ = self.run_vcs("proposal", "view", "7")
        self.assertEqual(code, 0)
        payload = self.broker.payload("proposal-view")
        self.assertEqual(payload["number"], 7)
        self.assertNotIn("comments", payload)
        self.assertNotIn("diff", payload)
        self.assertNotIn("limit", payload)

    def test_flags_are_sent_when_given(self):
        code, _ = self.run_vcs("proposal", "view", "7", "--comments", "--diff", "-n", "5")
        self.assertEqual(code, 0)
        payload = self.broker.payload("proposal-view")
        self.assertTrue(payload["comments"])
        self.assertTrue(payload["diff"])
        self.assertEqual(payload["limit"], 5)

    def test_issue_list_carries_state_and_labels(self):
        code, _ = self.run_vcs(
            "issue", "list", "--state", "closed", "--labels", "bug", "p1"
        )
        self.assertEqual(code, 0)
        payload = self.broker.payload("issue-list")
        self.assertEqual(payload["state"], "closed")
        self.assertEqual(payload["labels"], ["bug", "p1"])

    def test_issue_create_and_comment(self):
        code, _ = self.run_vcs("issue", "create", "--title", "Drift", "--body", "why")
        self.assertEqual(code, 0)
        self.assertEqual(self.broker.payload("issue-create")["title"], "Drift")
        code, _ = self.run_vcs("issue", "comment", "12", "--body", "fixed by #13")
        self.assertEqual(code, 0)
        self.assertEqual(self.broker.payload("issue-comment")["number"], 12)

    def test_the_repository_is_the_only_thing_resolved_locally(self):
        # Which forge this is, what it calls a proposal, and how to reach its
        # API are all decided on the credential side.
        self.run_vcs("issue", "list")
        payload = self.broker.payload("issue-list")
        self.assertEqual(payload["repository"], "local.test/acme/infra")
        self.assertNotIn("forge", payload)

    def test_an_explicit_repo_needs_no_local_copy(self):
        self.run_vcs("discard")
        code, _ = self.run_vcs("issue", "list", "--repo", "other/repo")
        self.assertEqual(code, 0)
        self.assertEqual(self.broker.payload("issue-list")["repository"], "other/repo")

    def test_a_broker_refusal_reaches_the_caller_as_its_own_message(self):
        self.broker.fail["issue-view"] = "#4 is a pull request, not an issue"
        code, answer = self.run_vcs("issue", "view", "4")
        self.assertEqual(code, 1)
        self.assertIn("pull request", answer["error"])


# ---------------------------------------------------------------------------
# talking to the broker
# ---------------------------------------------------------------------------


class BrokerCallTest(unittest.TestCase):
    """`vcs.call` itself, which the other classes replace."""

    def test_without_an_endpoint_the_error_says_where_this_runs(self):
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}):
            with self.assertRaises(vcs.VcsError) as caught:
                vcs.call("clone", {})
        self.assertIn("shell sandbox", str(caught.exception))

    def test_a_broker_without_the_routes_is_named_as_an_old_image(self):
        # There is no switch, so the refusal must not read like one. "Turned
        # off" would send whoever hit it looking for a configuration field that
        # does not exist.
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8080"}
        ), mock.patch.object(
            vcs.credential_proxy_client,
            "vcs_call",
            side_effect=vcs.credential_proxy_client.WorkspaceUnavailable(
                "VCS_UNAVAILABLE"
            ),
        ):
            with self.assertRaises(vcs.VcsError) as caught:
                vcs.call("clone", {})
        self.assertIn("older than this skill", str(caught.exception))
        self.assertNotIn("turned off", str(caught.exception))

    def test_a_request_error_surfaces_the_broker_s_own_wording(self):
        error = vcs.credential_proxy_client.WorkspaceRequestError(
            "publish failed", payload={"error": "fix/x has diverged", "code": "X"}
        )
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8080"}
        ), mock.patch.object(
            vcs.credential_proxy_client, "vcs_call", side_effect=error
        ):
            with self.assertRaises(vcs.VcsError) as caught:
                vcs.call("publish", {})
        self.assertEqual(str(caught.exception), "fix/x has diverged")


class LocalGitTest(VcsTestCase):
    def test_a_missing_local_git_names_the_fallback(self):
        with mock.patch.object(vcs, "LOCAL_GIT", str(self.root / "no-such-git")):
            with self.assertRaises(vcs.VcsError) as caught:
                vcs.local_git(self.root, "status")
        self.assertIn("inspect-repository", str(caught.exception))

    def test_hooks_are_pointed_at_an_empty_directory(self):
        # A hook is the one thing among the incoming objects that would not need
        # a config entry to have been supplied, so the path is set rather than
        # left to default.
        self.clone()
        tree = self.tree()
        marker = self.root / "hook-ran"
        hooks = tree / ".git/hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "post-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
        os.chmod(hooks / "post-commit", 0o755)
        (tree / "x.txt").write_text("x\n")
        self.run_vcs("commit", "-m", "with a hook present")
        self.assertFalse(marker.exists())

    def test_the_copy_inherits_no_user_configuration(self):
        self.clone()
        done = vcs.local_git(self.tree(), "config", "--get", "user.email")
        self.assertEqual(done.stdout.strip(), vcs.AUTHOR_EMAIL)


# ---------------------------------------------------------------------------
# the abstraction itself
# ---------------------------------------------------------------------------


class AbstractionTest(unittest.TestCase):
    """What this container is not allowed to know.

    These read the source rather than run it. The property is that no verb here
    names a forge or shells out to a network client — and a behavioural test
    cannot see the one code path that does, because that path works.
    """

    source = Path(vcs.__file__).read_text()

    def test_no_forge_client_is_invoked(self):
        for binary in ("gh", "glab", "hub", "tea"):
            with self.subTest(binary=binary):
                self.assertNotIn(f'"{binary}"', self.source)
                self.assertNotIn(f"'{binary}'", self.source)

    def test_no_forge_host_appears_outside_an_example(self):
        # `github.com` may appear in the help text as something a caller types.
        # It may not appear anywhere a URL is composed, which is what the
        # broker's allowlist decides.
        body = self.source.split("# ---- the broker")[1]
        for line in body.splitlines():
            if "github.com" in line or "gitlab.com" in line:
                self.assertTrue(
                    line.lstrip().startswith("#") or '"""' in line,
                    f"a forge host reached the code: {line.strip()}",
                )

    def test_the_only_network_client_is_the_broker(self):
        for module in ("requests", "urllib.request", "http.client", "socket"):
            with self.subTest(module=module):
                self.assertNotIn(f"import {module}", self.source)

    def test_every_verb_the_broker_serves_has_a_command(self):
        parser = vcs.build_parser()
        actions = [
            action
            for action in parser._subparsers._group_actions[0].choices  # noqa: SLF001
        ]
        for verb in (
            "capabilities",
            "clone",
            "log",
            "show",
            "diff",
            "annotate",
            "files",
            "grep",
            "status",
            "branch",
            "commit",
            "publish",
            "discard",
            "proposal",
            "issue",
        ):
            with self.subTest(verb=verb):
                self.assertIn(verb, actions)

    def test_the_familiar_spelling_is_an_alias_of_the_concept(self):
        parser = vcs.build_parser()
        choices = parser._subparsers._group_actions[0].choices  # noqa: SLF001
        for alias, concept in (
            ("blame", "annotate"),
            ("history", "log"),
            ("manifest", "files"),
            ("search", "grep"),
            ("push", "publish"),
            ("close", "discard"),
            ("pr", "proposal"),
            ("mr", "proposal"),
        ):
            with self.subTest(alias=alias):
                self.assertIn(alias, choices)
                self.assertIs(choices[alias], choices[concept])


if __name__ == "__main__":
    unittest.main()
