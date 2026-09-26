"""Unit tests for the submit-suggestion skill's PR submitter.

The subject lives in `agents/platform/skills/submit-suggestion/scripts/`, but
the test lives here: CI discovers tests in exactly two directories
(.github/workflows/python-tests.yml), and that is not one of them. Loading it by
path keeps the coverage without a third discovery step.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_submit_suggestion.py' -v

Real git against a local repository throughout, with the broker faked at the
one seam the script has: `vcs_client.call`. A recorded runner on this side
would make most of it vacuous — whether a second round extends a branch or
diverges from it is a question only real objects answer — while a real broker
would need a credential CI does not have. So the fake serves bundles out of a
real repository and pushes them back into it, and the proposals it keeps are a
table.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock
import unittest
from contextlib import redirect_stdout
from itertools import count
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gitops_workspace  # noqa: E402
import vcs_client  # noqa: E402
from providers import validate_branch  # noqa: E402

SUBJECT = (
    HERE.parent / "skills" / "submit-suggestion" / "scripts" / "submit_suggestion.py"
)
REAL_GIT = shutil.which("git") or "/usr/bin/git"


def _load_subject():
    spec = importlib.util.spec_from_file_location("submit_suggestion", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


submit_suggestion = _load_subject()

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **GIT_ENV},
    )


class FakeBroker:
    """A real repository on one side, a table of proposals on the other.

    Faithful where the script can tell the difference: `clone` bundles from a
    working copy the way the broker does, `publish` unbundles and pushes so the
    next clone sees the branch, and the proposals answer in the neutral shape
    with the forge's own vocabulary nowhere in them.
    """

    def __init__(self, origin: Path, scratch: Path):
        self.origin = origin
        self.scratch = scratch
        # What the remote calls its default. The real broker reads it from the
        # remote's HEAD, and a fleet whose trunk is not `main` is exactly the
        # case the protected-branch list cannot cover.
        self.default_branch = "main"
        self.proposals: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.numbers = count(101)
        self.serial = count()
        self.create_fails_with: Exception | None = None
        self.update_fails_with: Exception | None = None
        self.identity_fails_with: Exception | None = None
        # Who the credential authenticates as, which is what `proposal_create`
        # records as the author. Set it to somebody else and the proposals this
        # fake already holds become a stranger's.
        self.viewer = "kube-agents"

    def __call__(self, verb: str, payload: dict) -> dict:
        self.calls.append((verb, dict(payload)))
        return getattr(self, verb.replace("-", "_"))(payload)

    def payloads(self, verb: str) -> list[dict]:
        return [payload for seen, payload in self.calls if seen == verb]

    # -- repository verbs ------------------------------------------------

    def _serving_copy(self) -> Path:
        work = self.scratch / f"serve-{next(self.serial)}"
        git(self.scratch, "clone", "--quiet", str(self.origin), work.name)
        return work

    def clone(self, payload):
        branch = payload.get("branch") or self.default_branch
        work = self._serving_copy()
        git(work, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        bundle = work.parent / f"{work.name}.bundle"
        git(work, "bundle", "create", str(bundle), "HEAD", branch)
        blob = bundle.read_bytes()
        return {
            "forge": "local",
            "repo": payload["repository"],
            "branch": branch,
            "revision": git(work, "rev-parse", "HEAD").stdout.strip(),
            "size": len(blob),
            "bundleBase64": base64.b64encode(blob).decode("ascii"),
        }

    def publish(self, payload):
        branch = payload["branch"]
        if payload.get("advance") and not [
            proposal
            for proposal in self.proposals
            if proposal["state"] == "open" and proposal["source"] == branch
        ]:
            # `vcs_broker._require_open_proposal`, which runs on every
            # `advance` publish: the flag says this copy was taken of a
            # proposal branch in order to add to it, so a branch carrying no
            # open proposal is refused before anything is pushed. Kept here
            # because a fake that pushes anyway can prove a route the shipped
            # broker refuses -- which it did, for a refusal whose advice was a
            # dead end on the real thing.
            raise vcs_client.VcsError(
                f"`advance` says {branch} is a proposal branch this copy was "
                "cloned in order to add to, but no open proposal on this "
                "repository has it as its source.",
                code="CLONED_BRANCH",
            )
        work = self._serving_copy()
        bundle = work.parent / f"{work.name}.in.bundle"
        bundle.write_bytes(base64.b64decode(payload["bundleBase64"]))
        git(work, "fetch", "--quiet", str(bundle), f"refs/heads/{branch}:refs/heads/{branch}")
        git(work, "push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
        tip = git(work, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        return {"forge": "local", "repo": payload["repository"], "branch": branch, "revision": tip}

    # -- collaboration verbs ---------------------------------------------

    def identity(self, payload):
        if self.identity_fails_with:
            raise self.identity_fails_with
        return {"identity": {"login": self.viewer, "canWrite": True}}

    def proposal_list(self, payload):
        found = [
            proposal
            for proposal in self.proposals
            if payload.get("source") in (None, proposal["source"])
            and payload.get("state", "open") in ("all", proposal["state"])
            and payload.get("target") in (None, proposal["target"])
        ]
        return {"proposals": found, "count": len(found), "truncated": False}

    def proposal_create(self, payload):
        if self.create_fails_with:
            raise self.create_fails_with
        number = next(self.numbers)
        proposal = {
            "number": number,
            "title": payload["title"],
            "body": payload.get("body", ""),
            "state": "open",
            "draft": False,
            "author": self.viewer,
            "source": payload["source"],
            # Where the branch is when the proposal is read, which is what
            # `translate.proposal` reports as the forge does: the last
            # revision the branch was at, merged or not.
            "sourceRevision": self._tip(payload["source"]),
            "target": payload["target"],
            "url": f"https://forge.test/acme/infra/pull/{number}",
            "created": "2026-09-15T00:00:00Z",
            "updated": "2026-09-15T00:00:00Z",
        }
        self.proposals.append(proposal)
        return {"proposal": proposal}

    def proposal_update(self, payload):
        if self.update_fails_with:
            raise self.update_fails_with
        for proposal in self.proposals:
            if proposal["number"] == payload["number"]:
                for field in ("title", "body"):
                    if payload.get(field) is not None:
                        proposal[field] = payload[field]
                return {"proposal": proposal}
        raise AssertionError(f"no proposal {payload['number']}")

    def _tip(self, branch: str) -> str:
        shown = git(self.origin, "rev-parse", f"refs/heads/{branch}", check=False)
        return shown.stdout.strip() if shown.returncode == 0 else ""


@unittest.skipIf(shutil.which("git") is None, "git is not on PATH")
class SubmitSuggestionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)

        self.origin = base / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "--quiet", "--initial-branch=main")
        (self.origin / "app.yaml").write_text("replicas: 1\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "--quiet", "-m", "seed")

        self.served = base / "served"
        self.served.mkdir()
        self.broker = FakeBroker(self.origin, self.served)

        root = base / "vcs"
        for attribute, value in (
            ("ROOT", root),
            ("SESSIONS", root / ".sessions"),
            ("LOCAL_GIT", REAL_GIT),
            ("call", self.broker),
        ):
            patch = mock.patch.object(vcs_client, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.scratch = base / "scratch"
        self.scratch.mkdir()
        patch = mock.patch.object(submit_suggestion, "SCRATCH_DIR", str(self.scratch))
        patch.start()
        self.addCleanup(patch.stop)

        self.logged: list[str] = []
        patch = mock.patch.object(submit_suggestion, "log", self.logged.append)
        patch.start()
        self.addCleanup(patch.stop)

        for name, value in (
            ("resolve_repo", lambda workspace=None: "acme/infra"),
            ("get_managed_github_repos", lambda: []),
        ):
            patch = mock.patch.object(gitops_workspace, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    # -- helpers ----------------------------------------------------------

    def run_subject(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = submit_suggestion.dispatch(list(argv))
        return code, buffer.getvalue().strip()

    def prepare(self, branch="platform-agent/scale-web", **extra) -> dict:
        argv = ["prepare", "--branch", branch]
        for flag, value in extra.items():
            argv += [f"--{flag.replace('_', '-')}"] + ([] if value is True else [str(value)])
        _, out = self.run_subject(*argv)
        return json.loads(out)

    def edit(self, prepared: dict, text: str = "replicas: 3\n") -> None:
        (Path(prepared["workspace"]) / "app.yaml").write_text(text)

    def body_file(self, text: str = "why this change\n") -> str:
        path = self.scratch / "body.md"
        path.write_text(text)
        return str(path)

    def remote_branches(self) -> list[str]:
        listing = git(self.origin, "branch", "--format=%(refname:short)")
        return listing.stdout.split()

    def existing_proposal(self, branch: str, target: str = "main", **fields) -> dict:
        return self.broker.proposal_create(
            {"title": "under review", "body": "somebody else wrote this", "source": branch, "target": target, **fields}
        )["proposal"]

    # -- prepare ----------------------------------------------------------

    def test_prepare_cuts_a_new_branch_from_the_base(self):
        prepared = self.prepare()
        self.assertEqual(prepared["repo"], "acme/infra")
        self.assertEqual(prepared["branch"], "platform-agent/scale-web")
        self.assertEqual(prepared["base"], "main")
        self.assertEqual(prepared["started_from"], "main")
        self.assertEqual(prepared["proposal"], "")
        copy = Path(prepared["workspace"])
        self.assertTrue((copy / "app.yaml").is_file())
        self.assertEqual(
            git(copy, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "platform-agent/scale-web",
        )

    def test_prepare_leaves_nothing_behind_when_it_refuses_the_name(self):
        """The refusal says to use another name, so this one must be free to retry.

        The copy has to come down before the spent-branch question can be
        answered -- it is a question about the copy. Keeping it after the
        refusal left a tree and a session record under the very name the caller
        was just told to stop using, and the next `prepare` of it refused for
        having found a copy instead.
        """
        branch = "platform-agent/scale-web"
        git(self.origin, "checkout", "--quiet", "-b", branch)
        (self.origin / "app.yaml").write_text("replicas: 9\n")
        git(self.origin, "commit", "--quiet", "-am", "round one")
        git(self.origin, "checkout", "--quiet", "main")
        (self.origin / "app.yaml").write_text("replicas: 7\n")
        git(self.origin, "commit", "--quiet", "-am", "round one, squashed")
        self.existing_proposal(branch)["state"] = "merged"
        for proposal in self.broker.proposals:
            proposal["state"] = "merged"

        with self.assertRaises(ValueError):
            self.prepare(branch)
        self.assertEqual(list(vcs_client.ROOT.glob("*/.git")), [])
        with self.assertRaises(vcs_client.VcsError):
            vcs_client.resolve_session("acme/infra", key=branch)
        # And the name works on the next attempt, which is what the refusal
        # told the caller to do -- with the flag, since the branch is genuinely
        # still there.
        self.assertEqual(self.prepare(branch, allow_reused_branch=True)["branch"], branch)

    def test_prepare_takes_a_copy_of_a_branch_that_already_has_a_proposal(self):
        # Step 5 of the SKILL: another round on a proposal under review. The
        # branch's own revisions have to come down with it -- cutting it afresh
        # from the base is what replaced every reviewed revision.
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        reviewed = git(self.origin, "rev-parse", f"refs/heads/{branch}").stdout.strip()

        again = self.prepare(branch, force=True)
        self.assertEqual(again["started_from"], branch)
        self.assertEqual(again["base"], "main")
        self.assertTrue(again["proposal"].endswith("/101"))
        self.assertEqual(
            git(Path(again["workspace"]), "rev-parse", "HEAD").stdout.strip(), reviewed
        )

    def test_prepare_refuses_a_branch_whose_open_proposal_is_somebody_elses(self):
        """The refusal the broker makes at `publish`, made before the work.

        `prepare` reads an open proposal on the branch as "this run is adding
        to it" and never asked whose it was; `publish --advance` is then
        refused `CLONED_BRANCH` by the broker because the author is not this
        credential. Between them the whole turn is written into a copy of
        somebody else's branch and thrown away at the end of it. Reachable
        because branch names here are derived, so a human can have opened a
        pull request from one.
        """
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        # The same branch, the same open proposal -- and now the credential is
        # somebody else, which is the collision seen from this side.
        self.broker.viewer = "a-colleague"

        cloned = len(self.broker.payloads("clone"))
        with self.assertRaises(ValueError) as caught:
            self.run_subject("prepare", "--branch", branch, "--force")
        self.assertIn("a-colleague", str(caught.exception))
        self.assertIn("kube-agents", str(caught.exception))
        self.assertIn("branch name of your own", str(caught.exception))
        # And it stopped before the copy: nothing was cloned for this call.
        self.assertEqual(len(self.broker.payloads("clone")), cloned)

    def test_prepare_still_adds_to_the_install_s_own_proposal_under_another_spelling(self):
        """`kube-agents[bot]` and `kube-agents` are one account.

        The provider strips an automation's marking off every author it emits
        and the credential store keeps it, so the raw comparison makes an
        install a stranger to its own proposals and refuses every second round
        on an App-authenticated install -- which is all of them.
        """
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        self.broker.viewer = "kube-agents[bot]"

        again = self.prepare(branch, force=True)
        self.assertEqual(again["started_from"], branch)

    def test_the_marking_rule_is_the_broker_s_and_not_a_narrower_copy(self):
        """Any bracketed suffix, because that is what `vcs_broker._login_key` folds.

        This side cannot import the broker, so the rule is restated -- and a
        restatement that is narrower is worse than none: the broker would
        accept the `advance` while this side refuses the round outright, and
        which of the two answers the agent gets would depend on how the forge
        happens to spell its automations.
        """
        self.assertEqual(
            submit_suggestion._login_key("kube-agents[bot]"),
            submit_suggestion._login_key("Kube-Agents"),
        )
        for marking in ("[bot]", "[app]", "[BOT]", "[service account]"):
            with self.subTest(marking=marking):
                self.assertEqual(
                    submit_suggestion._login_key(f"kube-agents{marking}"),
                    "kube-agents",
                )
        # Not a suffix, so not a marking: two accounts stay two.
        self.assertNotEqual(
            submit_suggestion._login_key("kube-agents[bot]-staging"),
            submit_suggestion._login_key("kube-agents"),
        )

    def test_prepare_refuses_a_branch_name_whose_squash_merged_branch_is_still_there(self):
        """The reuse the docstring promises, on the forge default that breaks it.

        Squash-merge leaves the source branch on the remote at a revision the
        base does not contain, so a branch cut fresh from the base does not
        build on it and `publish` is refused as `BRANCH_DIVERGED` -- after the
        agent has written the whole change. Refused here instead, where the
        fault is and before the work.
        """
        branch = "platform-agent/scale-web"
        # The first round: a branch with a commit on it, and a proposal that is
        # then squash-merged -- the content lands on main as a new revision and
        # the branch is left where it was.
        git(self.origin, "checkout", "--quiet", "-b", branch)
        (self.origin / "app.yaml").write_text("replicas: 2\n")
        git(self.origin, "commit", "--quiet", "-am", "round one")
        git(self.origin, "checkout", "--quiet", "main")
        (self.origin / "app.yaml").write_text("replicas: 2\n")
        git(self.origin, "commit", "--quiet", "-am", "round one, squashed")
        merged = self.existing_proposal(branch)
        merged["state"] = "merged"

        with self.assertRaises(ValueError) as caught:
            self.prepare(branch)
        message = str(caught.exception)
        self.assertIn("BRANCH_DIVERGED", message)
        self.assertIn(merged["url"], message)
        self.assertIn("has not used", message)
        # The revision named is the one the remote's branch is actually at.
        self.assertIn(merged["sourceRevision"][:12], message)

    def test_a_repository_that_deletes_merged_branches_can_reuse_the_name(self):
        """The refusal above is right only while the remote still holds the branch.

        GitHub's "automatically delete head branches" is on in plenty of
        repositories, and there it deletes the branch as it squash-merges. The
        name is then free, `publish` would create it and succeed, and the check
        cannot tell the two cases apart: no read verb in the vocabulary reports
        whether a branch exists. So the caller says which it is, and is told
        what it is being trusted about.
        """
        branch = "platform-agent/scale-web"
        git(self.origin, "checkout", "--quiet", "-b", branch)
        (self.origin / "app.yaml").write_text("replicas: 2\n")
        git(self.origin, "commit", "--quiet", "-am", "round one")
        git(self.origin, "checkout", "--quiet", "main")
        (self.origin / "app.yaml").write_text("replicas: 2\n")
        git(self.origin, "commit", "--quiet", "-am", "round one, squashed")
        merged = self.existing_proposal(branch)
        merged["state"] = "merged"
        # What deleting the head branch looks like from here: the remote no
        # longer has it, and the revision it was at is still unreachable from
        # the base, so `stale_tip` answers exactly as it does above.
        git(self.origin, "branch", "--quiet", "-D", branch)

        prepared = self.prepare(branch, allow_reused_branch=True)

        self.assertEqual(prepared["branch"], branch)
        standing = git(Path(prepared["workspace"]), "rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(standing.stdout.strip(), branch)
        # Trusted, not silently. If the caller was wrong, this line is what
        # makes the BRANCH_DIVERGED at the end of the turn legible.
        said = "\n".join(self.logged)
        self.assertIn("BRANCH_DIVERGED", said)
        self.assertIn(merged["url"], said)

    def test_prepare_names_the_real_tip_of_a_long_spent_branch(self):
        """The tip is the proposal's `sourceRevision`, not the last commit of a page.

        `proposal-commits` is oldest first and bounded, so for a proposal past
        the page its last entry was an old revision -- one the base may well
        contain while the real tip is not. Six commits, then a squash-merge:
        the refusal has to name the sixth, and it has to refuse at all.
        """
        branch = "platform-agent/scale-web"
        git(self.origin, "checkout", "--quiet", "-b", branch)
        for n in range(6):
            (self.origin / "app.yaml").write_text(f"replicas: {n + 2}\n")
            git(self.origin, "commit", "--quiet", "-am", f"round one, step {n}")
        tip = git(self.origin, "rev-parse", "HEAD").stdout.strip()
        git(self.origin, "checkout", "--quiet", "main")
        (self.origin / "app.yaml").write_text("replicas: 7\n")
        git(self.origin, "commit", "--quiet", "-am", "round one, squashed")
        merged = self.existing_proposal(branch)
        merged["state"] = "merged"

        with self.assertRaises(ValueError) as caught:
            self.prepare(branch)
        self.assertIn(tip[:12], str(caught.exception))
        # Answered off the proposal itself. Asserting the whole sequence and
        # not the absence of one verb: `submit_suggestion` issues no
        # `proposal-commits` on any path, so a check for that alone passes
        # however many calls `prepare` makes.
        self.assertEqual(
            [verb for verb, _ in self.broker.calls],
            ["proposal-list", "proposal-list", "clone"],
        )

    def test_prepare_reuses_a_name_whose_branch_was_merged_whole(self):
        """The case that works, and it must keep working.

        A proposal merged with a merge commit leaves its tip reachable from the
        base, so a fresh cut descends from what the remote holds and `publish`
        fast-forwards it. Refusing here on the mere existence of a spent
        proposal would stop the reuse the naming convention is built on.

        The accepting direction of the `merge-base` check, and the test has to
        show the check ran: an earlier shape of this test passed because the
        fake answered an empty commit list and `stale_tip` returned before
        comparing anything.
        """
        branch = "platform-agent/scale-web"
        git(self.origin, "checkout", "--quiet", "-b", branch)
        (self.origin / "app.yaml").write_text("replicas: 2\n")
        git(self.origin, "commit", "--quiet", "-am", "round one")
        tip = git(self.origin, "rev-parse", "HEAD").stdout.strip()
        git(self.origin, "checkout", "--quiet", "main")
        git(self.origin, "merge", "--quiet", "--no-ff", "-m", "merge round one", branch)
        merged = self.existing_proposal(branch)
        merged["state"] = "merged"

        with mock.patch.object(
            submit_suggestion.vcs_client, "local", wraps=vcs_client.local
        ) as local:
            prepared = self.prepare(branch)
        self.assertEqual(prepared["branch"], branch)
        self.assertEqual(prepared["base"], "main")
        compared = [
            call.args[1] for call in local.call_args_list
            if call.args[1][:2] == ["merge-base", "--is-ancestor"]
        ]
        self.assertEqual(compared, [["merge-base", "--is-ancestor", tip, "HEAD"]])

    def test_prepare_is_unbothered_by_a_name_nobody_has_used(self):
        """The ordinary card, and the one the extra lookup must not cost anything.

        One `proposal-list` for the open proposal, one for the name's history,
        and nothing else across the seam. The open-proposal lookup is the first
        of the two -- it decides which arm `prepare` takes -- and the
        spent-name lookup is the second, which is the whole of what the check
        added here.
        """
        self.prepare()
        self.assertEqual(
            [verb for verb, _ in self.broker.calls],
            ["proposal-list", "proposal-list", "clone"],
        )

    def test_prepare_reads_the_base_off_the_open_proposal_not_the_default_branch(self):
        git(self.origin, "checkout", "--quiet", "-b", "release")
        git(self.origin, "checkout", "--quiet", "main")
        branch = "platform-agent/scale-web"
        self.existing_proposal(branch, target="release")
        git(self.origin, "branch", branch, "main")
        self.assertEqual(self.prepare(branch)["base"], "release")

    def test_check_branch_refuses_an_empty_name_before_the_protected_list(self):
        # `--branch ""` and `--branch "  "` reach here as a falsy name. Without
        # this arm `_short_branch("")` is "", which is in no protected set, and
        # the empty name goes on to be a directory key and a `switch --create`.
        for branch in ("", "   ", None):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError) as caught:
                    submit_suggestion.check_branch(branch)
                self.assertIn("must not be empty", str(caught.exception))

    def test_check_branch_refuses_the_standing_protected_names(self):
        # The list that needs no environment to be set, and the `refs/heads/`
        # and `heads/` spellings of it -- a caller that passes a full ref would
        # otherwise walk straight past a set holding only the short names.
        for name in submit_suggestion.PROTECTED_BRANCHES:
            for branch in (name, f"refs/heads/{name}", f"heads/{name}"):
                with self.subTest(branch=branch):
                    with self.assertRaises(ValueError) as caught:
                        submit_suggestion.check_branch(branch)
                    self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_check_branch_accepts_a_suggestion_branch_and_hands_back_a_trimmed_name(self):
        # The accepting direction, and the return value: every caller uses what
        # comes back rather than what it passed in, so the trim is load-bearing.
        self.assertEqual(
            submit_suggestion.check_branch("  platform-agent/scale-web  "),
            "platform-agent/scale-web",
        )

    def test_check_branch_refuses_a_run_branch(self):
        # `run/**` is the harness's own namespace. A suggestion pushed there is
        # not reviewed by anyone; it is picked up as if a run had produced it.
        for branch in ("run/nightly", "refs/heads/run/1234", "RUN/Loud"):
            with self.assertRaises(ValueError) as caught:
                submit_suggestion.check_branch(branch)
            self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_check_branch_refuses_the_configured_base_branch(self):
        # A fleet that renamed its trunk says so in one of these two, and the
        # list of three would otherwise wave the rename straight through.
        for variable in ("GITOPS_BASE_BRANCH", "CREDENTIAL_PROXY_BASE_BRANCH"):
            with mock.patch.dict(os.environ, {variable: "custom-trunk"}):
                for branch in ("custom-trunk", "refs/heads/custom-trunk", "heads/custom-trunk"):
                    with self.assertRaises(ValueError) as caught:
                        submit_suggestion.check_branch(branch)
                    self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_check_branch_refuses_a_base_branch_passed_in(self):
        with self.assertRaises(ValueError) as caught:
            submit_suggestion.check_branch("custom-base", base_branch="custom-base")
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertEqual(
            submit_suggestion.check_branch("platform-agent/x", base_branch="custom-base"),
            "platform-agent/x",
        )

    def test_prepare_refuses_a_protected_branch(self):
        for branch in ("main", "MASTER", "refs/heads/production"):
            with self.assertRaises(ValueError) as caught:
                self.run_subject("prepare", "--branch", branch)
            self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertEqual(self.broker.calls, [])

    def test_prepare_refuses_the_branch_it_would_be_proposing_onto(self):
        # The list of three cannot name a fleet's own trunk. This is the guard
        # that can: the base comes back from the broker's clone, so a repository
        # whose default branch is `trunk` refuses `--branch trunk` here rather
        # than at the push.
        git(self.origin, "branch", "trunk", "main")
        self.broker.default_branch = "trunk"
        with self.assertRaises(ValueError) as caught:
            self.run_subject("prepare", "--branch", "trunk")
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertIn("same as the base branch", str(caught.exception))

    def test_prepare_refuses_a_name_git_will_not_take_as_a_branch(self):
        """The guard on the switch's exit status, reached without a mock.

        `check_branch` validates the protected-name list, not git's ref syntax,
        so a name git will not take gets past it, the clone happens, and `git
        switch --create` exits 128. `branch` reports that rather than raising,
        so without the guard `prepare` prints a JSON line naming a branch the
        copy is not standing on and the whole turn is spent editing the base.

        The names are chosen to reach that guard against the real broker and
        not only this fake. A space, `..` or a trailing `.lock` would not: the
        GitHub provider runs `validate_branch` over the `source` of the
        `proposal-list` that `open_proposal` sends first, and refuses all three
        there, before anything is cloned. `BRANCH_RE` is a character class, so
        a name whose characters it allows can still be one git rejects for
        where they sit -- a trailing separator, an empty component, a component
        ending in a dot. Those are the ones below, and the assertion that they
        clear `validate_branch` is what keeps them that way.
        """
        for branch in (
            "platform-agent/x/",
            "platform-agent/a//b",
            "platform-agent/x.",
        ):
            with self.subTest(branch=branch):
                self.assertEqual(validate_branch(branch), branch)
                with self.assertRaises(vcs_client.VcsError) as caught:
                    self.run_subject("prepare", "--branch", branch)
                self.assertIn("could not take the branch", str(caught.exception))
                self.assertEqual(self.broker.payloads("publish"), [])

    def test_prepare_refuses_a_repository_outside_the_managed_list(self):
        with mock.patch.object(
            gitops_workspace, "get_managed_github_repos", lambda: ["acme/infra"]
        ):
            with self.assertRaises(ValueError) as caught:
                self.run_subject("prepare", "--branch", "b", "--repo", "other/elsewhere")
        self.assertIn("not in the managed repositories list", str(caught.exception))
        self.assertEqual(self.broker.calls, [])

    def test_prepare_is_refused_when_the_managed_list_cannot_be_read(self):
        # Fail-closed, and pinned here because nothing else would go red if
        # `validate_repo` were ever taught to read a failed ConfigMap read as an
        # empty allowlist -- after which every `--repo` the model names is
        # accepted whenever that read hiccups, and the suite stays green.
        with mock.patch.object(
            gitops_workspace, "get_managed_github_repos",
            mock.Mock(side_effect=RuntimeError("ConfigMap missing")),
        ):
            with self.assertRaises(RuntimeError):
                self.run_subject("prepare", "--branch", "b")
        self.assertEqual(self.broker.calls, [])

    def test_prepare_honours_repo_over_the_resolved_default(self):
        self.prepare(repo="acme/other")
        self.assertEqual(self.broker.payloads("clone")[0]["repository"], "acme/other")

    def test_prepare_refuses_to_replace_a_copy_holding_unpublished_work(self):
        branch = "platform-agent/scale-web"
        prepared = self.prepare(branch)
        self.edit(prepared)
        vcs_client.commit("not yet published", ["app.yaml"], spec="acme/infra")
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject("prepare", "--branch", branch)
        self.assertIn("unpublished revision", str(caught.exception))
        # And `--force` is the way past it, named in the refusal itself.
        self.assertIn("--force", str(caught.exception))
        self.prepare(branch, force=True)

    def test_two_cards_on_one_repository_get_two_working_copies(self):
        # The scratch root is shared by every card in this container. Keyed on
        # the repository alone, the second card's `prepare` either refused or,
        # with `--force`, deleted the first card's unpublished work.
        first = self.prepare("platform-agent/scale-web")
        self.edit(first, "replicas: 3\n")
        vcs_client.commit("first card", ["app.yaml"], spec="acme/infra")

        second = self.prepare("platform-agent/other")
        self.assertNotEqual(second["workspace"], first["workspace"])
        self.edit(second, "replicas: 9\n")

        # Neither card can see the other's work, and the first one's revision
        # is still there to publish.
        self.assertEqual((Path(first["workspace"]) / "app.yaml").read_text(), "replicas: 3\n")
        self.assertEqual(
            git(Path(first["workspace"]), "log", "-1", "--format=%s").stdout.strip(),
            "first card",
        )
        self.assertEqual(
            git(Path(second["workspace"]), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "platform-agent/other",
        )

    def test_submit_sends_the_copy_keyed_on_the_branch_it_names(self):
        # Two copies of one repository, and `--repo` names both. The branch is
        # what tells them apart: submitting the second card's change must not
        # publish the first card's.
        first = self.prepare("platform-agent/scale-web")
        self.edit(first, "replicas: 3\n")
        second = self.prepare("platform-agent/other")
        self.edit(second, "replicas: 9\n")

        self.run_subject(
            "submit", "--branch", "platform-agent/other",
            "--repo", "acme/infra", "--title", "t", "--body", "b",
        )
        self.assertIn("platform-agent/other", self.remote_branches())
        self.assertNotIn("platform-agent/scale-web", self.remote_branches())

    # -- submit -----------------------------------------------------------

    def test_submit_publishes_the_branch_and_opens_the_proposal(self):
        prepared = self.prepare()
        self.edit(prepared)
        code, out = self.run_subject(
            "submit",
            "--branch", "platform-agent/scale-web",
            "--title", "fix(capacity): raise the replica floor",
            "--body-file", self.body_file(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        self.assertIn("platform-agent/scale-web", self.remote_branches())
        created = self.broker.payloads("proposal-create")[0]
        self.assertEqual(created["source"], "platform-agent/scale-web")
        self.assertEqual(created["target"], "main")
        self.assertEqual(created["body"], "why this change\n")
        # The commit is the change, not the whole tree of the container.
        published = self.broker.payloads("publish")[0]
        self.assertEqual(published["target"], "main")
        self.assertIs(published["advance"], False)

    def test_submit_is_refused_when_the_managed_list_cannot_be_read(self):
        # The same gate on the way out. A copy already prepared is not a repository
        # already cleared: the allowlist is re-read, and a read that raises refuses.
        prepared = self.prepare()
        self.edit(prepared)
        with mock.patch.object(
            gitops_workspace, "get_managed_github_repos",
            mock.Mock(side_effect=RuntimeError("ConfigMap missing")),
        ):
            with self.assertRaises(RuntimeError):
                self.run_subject(
                    "submit", "--branch", "platform-agent/scale-web",
                    "--title", "t", "--body", "b",
                )
        self.assertNotIn("platform-agent/scale-web", self.remote_branches())

    def test_a_second_round_updates_the_proposal_instead_of_failing_to_open_one(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "first", "--body", "one")
        again = self.prepare("platform-agent/scale-web", force=True)
        self.edit(again, "replicas: 5\n")
        _, out = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "second", "--body", "two"
        )
        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        self.assertEqual(len(self.broker.payloads("proposal-create")), 1)
        self.assertEqual(self.broker.proposals[0]["title"], "second")
        self.assertEqual(self.broker.proposals[0]["body"], "two")
        # The branch was extended rather than replaced: both revisions are on it.
        log = git(self.origin, "log", "--format=%s", "refs/heads/platform-agent/scale-web")
        self.assertEqual(log.stdout.split("\n")[:2], ["second", "first"])

    def test_the_second_round_tells_the_broker_it_means_to_extend_the_branch(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "first", "--body", "one")
        again = self.prepare("platform-agent/scale-web", force=True)
        self.edit(again, "replicas: 5\n")
        self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "second", "--body", "two")
        self.assertTrue(self.broker.payloads("publish")[1]["advance"])
        self.assertEqual(self.broker.payloads("publish")[1]["clonedFrom"], "platform-agent/scale-web")

    def test_keep_description_leaves_the_body_alone_and_still_publishes(self):
        branch = "platform-agent/scale-web"
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", branch, "--title", "first", "--body", "one")
        again = self.prepare(branch, force=True)
        self.edit(again, "replicas: 7\n")
        _, out = self.run_subject(
            "submit", "--branch", branch, "--title", "a merge commit", "--keep-description"
        )
        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        self.assertEqual(self.broker.proposals[0]["title"], "first")
        self.assertEqual(self.broker.proposals[0]["body"], "one")
        self.assertEqual(self.broker.payloads("proposal-update"), [])
        self.assertEqual(len(self.broker.payloads("publish")), 2)
        # Both halves of what the notice now claims: the title did not reach
        # the proposal, and it is still what the pending edit was recorded
        # under. The earlier wording said "ignored", which was false here.
        self.assertTrue(
            any("does not reach the proposal" in line for line in self.logged)
        )
        self.assertTrue(
            any("commit message" in line for line in self.logged)
        )

    def test_keep_description_with_no_open_proposal_refuses_before_publishing(self):
        prepared = self.prepare()
        self.edit(prepared)
        with self.assertRaises(RuntimeError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--keep-description"
            )
        self.assertIn("no proposal is open", str(caught.exception))
        self.assertEqual(self.broker.payloads("publish"), [])
        self.assertNotIn("platform-agent/scale-web", self.remote_branches())

    def test_submit_refuses_a_copy_standing_on_another_branch(self):
        """And does not offer that branch: the copy is not this caller's.

        Nothing is keyed on the branch asked for, so the copy came from the
        keyless fallback and the scratch root is shared -- it is whichever
        single copy of the repository is here, which on a pod running two
        cards is the sibling card's, mid-edit. "Pass the branch you are
        actually on" would have this caller commit those edits under its own
        title and open a pull request for them, so the refusal names the
        branch as somebody else's and sends the caller to `prepare`.
        """
        prepared = self.prepare()
        self.edit(prepared)
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "platform-agent/something-else", "--title", "t", "--body", "b")
        said = str(caught.exception)
        self.assertIn("is on branch 'platform-agent/scale-web'", said)
        self.assertIn("was taken for 'platform-agent/scale-web'", said)
        self.assertIn("prepare --branch platform-agent/something-else", said)
        self.assertNotIn("pass the branch you are actually on", said)
        self.assertEqual(self.broker.payloads("publish"), [])
        # And the sibling's edits are still uncommitted, where it left them.
        self.assertEqual(git(Path(prepared["workspace"]), "status", "--porcelain").stdout.split(), ["M", "app.yaml"])

    def test_submit_inside_the_callers_own_copy_still_offers_the_branch_it_is_on(self):
        """The keyed lookup answered, so the other branch is the caller's too.

        `prepare --branch A` then a hand-cut B inside that tree: A still names
        the copy, so `submit --branch A` finds it by key and the branch it is
        standing on is one this caller cut. Here the shorter advice is right,
        and it is the advice the refusal above has to withhold.
        """
        prepared = self.prepare()
        git(Path(prepared["workspace"]), "checkout", "--quiet", "-b", "platform-agent/second-thought")
        self.edit(prepared)
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b")
        said = str(caught.exception)
        self.assertIn("is on branch 'platform-agent/second-thought'", said)
        self.assertIn("pass the branch you are actually on", said)
        self.assertEqual(self.broker.payloads("publish"), [])

    def test_submit_finds_a_copy_whose_key_is_not_the_branch_it_is_standing_on(self):
        """The fallback lookup has a proceed path, and it used to end in "no local copy".

        `prepare --branch A` keys the copy on A. An agent that then cuts B
        inside that tree and submits B resolves through the keyless fallback,
        gets past the "you are on another branch" check because it is standing
        on B -- and then `commit` and `publish` asked for the copy keyed on B,
        which is the lookup that had already failed.
        """
        prepared = self.prepare()
        workspace = Path(prepared["workspace"])
        git(workspace, "checkout", "--quiet", "-b", "platform-agent/second-thought")
        self.edit(prepared)

        _, out = self.run_subject(
            "submit",
            "--branch",
            "platform-agent/second-thought",
            "--title",
            "second thought",
            "--body",
            "why",
        )

        self.assertEqual(out, "https://forge.test/acme/infra/pull/101")
        published = self.broker.payloads("publish")
        self.assertEqual([item["branch"] for item in published], ["platform-agent/second-thought"])
        self.assertEqual(self.broker.proposals[0]["source"], "platform-agent/second-thought")

    def test_submit_refuses_without_a_title_and_a_body(self):
        self.prepare()
        for argv in (
            ("submit", "--branch", "b", "--title", "t"),
            ("submit", "--branch", "b", "--body", "only a body"),
        ):
            with self.assertRaises(ValueError) as caught:
                self.run_subject(*argv)
            self.assertIn("--title and one of --body / --body-file", str(caught.exception))

    def test_submit_refuses_a_protected_branch(self):
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "main", "--title", "t", "--body", "b")
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))

    def test_submit_refuses_a_base_that_is_the_branch_itself(self):
        prepared = self.prepare()
        self.edit(prepared)
        with self.assertRaises(ValueError) as caught:
            self.run_subject(
                "submit",
                "--branch", "platform-agent/scale-web",
                "--base", "platform-agent/scale-web",
                "--title", "t",
                "--body", "b",
            )
        self.assertIn("CRITICAL SECURITY REFUSAL", str(caught.exception))
        self.assertEqual(self.broker.payloads("publish"), [])

    def test_submit_commits_the_tracked_changes_the_copy_holds_under_the_title(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "one file", "--body", "b"
        )
        listing = git(self.origin, "show", "--name-only", "--format=%s", "refs/heads/platform-agent/scale-web")
        self.assertEqual(listing.stdout.split(), ["one", "file", "app.yaml"])

    def test_submit_refuses_rather_than_sweeping_a_file_the_agent_never_staged(self):
        """The SKILL forbids `git add .`; a helper doing it for them forbids nothing.

        The copy is a real clone on a filesystem the agent also scratches in, so
        the untracked file here is as likely to be a debug dump as a manifest.
        Refusing names it and says what to do; the alternatives are shipping it
        in a public proposal or dropping a real change without saying so.
        """
        prepared = self.prepare()
        self.edit(prepared)
        (Path(prepared["workspace"]) / "scratch.log").write_text("debug\n")
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        self.assertIn("scratch.log", str(caught.exception))
        self.assertEqual(self.broker.payloads("publish"), [])

    def test_submit_records_a_new_file_the_agent_staged_itself(self):
        """Staging is the agent saying this one belongs, which is the whole gate."""
        prepared = self.prepare()
        self.edit(prepared)
        (Path(prepared["workspace"]) / "new.yaml").write_text("added\n")
        vcs_client.local(
            vcs_client.resolve_session("acme/infra"), ["add", "--", "new.yaml"], "add"
        )
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "two files", "--body", "b"
        )
        listing = git(self.origin, "show", "--name-only", "--format=%s", "refs/heads/platform-agent/scale-web")
        self.assertEqual(listing.stdout.split(), ["two", "files", "app.yaml", "new.yaml"])

    def test_submit_takes_a_copy_the_agent_committed_itself(self):
        # The SKILL has always let the agent commit; nothing here insists on
        # making the revision, only that there is one.
        prepared = self.prepare()
        self.edit(prepared)
        vcs_client.commit("the agent's own message", spec="acme/infra")
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        subject = git(self.origin, "log", "--format=%s", "-1", "refs/heads/platform-agent/scale-web")
        self.assertEqual(subject.stdout.strip(), "the agent's own message")

    def test_a_retry_after_the_create_failed_opens_the_proposal_it_never_got(self):
        """Publish landed, `proposal-create` did not: the retry has to reach it.

        Without this the second `submit` finds nothing new to send and is
        refused before the step that failed, and `prepare` is no way out either
        -- it cuts the branch afresh and the broker refuses the publish as
        `BRANCH_DIVERGED`. The `git push --force-with-lease` + `gh pr create`
        pair this replaced was idempotent on retry.
        """
        prepared = self.prepare()
        self.edit(prepared)
        self.broker.create_fails_with = vcs_client.VcsError(
            "secondary rate limit", code="FORGE_RATE_LIMITED"
        )
        with self.assertRaises(vcs_client.VcsError):
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        published = git(self.origin, "rev-parse", "refs/heads/platform-agent/scale-web")
        self.assertTrue(published.stdout.strip())

        self.broker.create_fails_with = None
        _, url = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        self.assertEqual(url, self.broker.proposals[0]["url"])
        # And it did not publish a second time: there was nothing new to send.
        self.assertEqual(len(self.broker.payloads("publish")), 1)

    def test_a_retry_after_the_second_round_update_failed_reaches_the_update(self):
        """The same shape one round later, and it used to have no route at all.

        Publish lands, `proposal-update` fails -- the rate limit or 5xx the
        first-round comment already names. Reading `already_published` only when
        no proposal was open meant the retry went back through `publish`, which
        answers "there are no new revisions to publish" because the tip it is
        being asked to send is the one it just sent. The description update the
        retry exists for is on the far side of that refusal.
        """
        first = self.prepare()
        self.edit(first)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        self.assertEqual(len(self.broker.proposals), 1)

        second = self.prepare(force=True)
        self.edit(second, "replicas: 4\n")
        self.broker.update_fails_with = vcs_client.VcsError(
            "secondary rate limit", code="FORGE_RATE_LIMITED"
        )
        with self.assertRaises(vcs_client.VcsError):
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web",
                "--title", "round two", "--body", "b",
            )
        self.assertEqual(len(self.broker.payloads("publish")), 2)

        self.broker.update_fails_with = None
        _, url = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web",
            "--title", "round two", "--body", "b",
        )
        self.assertEqual(url, self.broker.proposals[0]["url"])
        self.assertEqual(self.broker.proposals[0]["title"], "round two")
        # And it did not publish a third time: there was nothing new to send.
        self.assertEqual(len(self.broker.payloads("publish")), 2)

    def test_a_second_round_that_changes_only_the_description_reaches_the_update(self):
        """Step 5 with nothing to commit: a corrected title or body.

        A fresh `prepare` is a copy *of* the branch with nothing published from
        it, so `already_published` cannot see the earlier publish, and `publish`
        refuses a copy with no new revisions. The proposal is open and the
        branch is where it should be; the update is what the round is for.
        """
        first = self.prepare()
        self.edit(first)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        self.prepare(force=True)
        _, url = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web",
            "--title", "the title the reviewer asked for", "--body", "and the body",
        )
        self.assertEqual(url, self.broker.proposals[0]["url"])
        self.assertEqual(self.broker.proposals[0]["title"], "the title the reviewer asked for")
        self.assertEqual(self.broker.proposals[0]["body"], "and the body")
        self.assertEqual(len(self.broker.payloads("publish")), 1)
        self.assertTrue(any("nothing" in line and "refreshing" in line for line in self.logged))

    def test_submit_refuses_a_description_only_round_on_somebody_else_s_proposal(self):
        """The one route to `proposal-update` that no publish stands in front of.

        `prepare` warns rather than refuses when the `identity` lookup failed,
        on the reasoning that the publish settles ownership afterwards. This
        round has no publish: nothing was committed, so `submit` skips straight
        to `proposal-update`, which is a plain forge verb with no
        `_require_open_proposal` behind it. Without a check here the stranger's
        title and body are overwritten by a run that pushed nothing.
        """
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        # The collision, arriving the only way it can reach `submit`: the
        # credential is somebody else now, and the lookup that would have said
        # so at `prepare` was down for that call.
        self.broker.viewer = "a-colleague"
        self.broker.identity_fails_with = vcs_client.VcsError(
            "the forge did not answer", code="FORGE_CALL_FAILED"
        )
        self.prepare(branch, force=True)
        self.broker.identity_fails_with = None

        with self.assertRaises(ValueError) as caught:
            self.run_subject(
                "submit", "--branch", branch, "--title", "not theirs", "--body", "nor this"
            )
        self.assertIn("kube-agents", str(caught.exception))
        self.assertIn("a-colleague", str(caught.exception))
        # And the description is as its author left it.
        self.assertEqual(self.broker.payloads("proposal-update"), [])
        self.assertEqual(self.broker.proposals[0]["title"], "first round")

    def test_submit_refuses_the_refresh_it_cannot_establish_ownership_for(self):
        """A failed lookup is not a pass on the route that nothing else guards.

        Empty from `this_install` means "do not compare", which is right where
        the broker compares next and wrong here, where it does not. A lookup
        the forge could not answer is therefore a refusal on this route and a
        warning on every other one.
        """
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        self.prepare(branch, force=True)
        self.broker.identity_fails_with = vcs_client.VcsError(
            "the forge did not answer", code="FORGE_CALL_FAILED"
        )

        with self.assertRaises(ValueError) as caught:
            self.run_subject(
                "submit", "--branch", branch, "--title", "round two", "--body", "b"
            )
        self.assertIn("who this install is", str(caught.exception))
        self.assertEqual(self.broker.payloads("proposal-update"), [])

    def test_a_proposal_closed_mid_round_says_so_instead_of_crying_security(self):
        """A reviewer merging while the agent works is ordinary, not an attack.

        The second round's copy is taken *of* the branch, so with the proposal
        gone `base` falls through to the branch itself and the branch-on-its-own
        -base refusal fires -- a CRITICAL SECURITY REFUSAL naming a state that is
        nothing of the kind, advising a separate feature branch when the real
        answer is that there is no proposal left to add to.
        """
        branch = "platform-agent/scale-web"
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", branch, "--title", "first", "--body", "one")
        again = self.prepare(branch, force=True)
        self.edit(again, "replicas: 5\n")
        self.broker.proposals[0]["state"] = "merged"
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", branch, "--title", "second", "--body", "two")
        said = str(caught.exception)
        self.assertIn("no proposal is open", said)
        self.assertNotIn("SECURITY REFUSAL", said)
        self.assertEqual(len(self.broker.payloads("proposal-create")), 1)

    def test_keep_description_on_a_closed_proposal_does_not_send_the_caller_in_a_circle(self):
        """Its refusal names `--title`/`--body-file`, which land on the same state."""
        branch = "platform-agent/scale-web"
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", branch, "--title", "first", "--body", "one")
        again = self.prepare(branch, force=True)
        self.edit(again, "replicas: 5\n")
        self.broker.proposals[0]["state"] = "closed"
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", branch, "--keep-description")
        self.assertIn("no proposal is open", str(caught.exception))

    def test_a_closed_proposal_is_not_escapable_with_a_base_either(self):
        """Because the broker shuts that door, the refusal must not open it.

        A copy taken *of* the branch publishes with `advance`, and the broker
        refuses an `advance` publish onto a branch carrying no open proposal --
        `--base` changes the target, not that. A refusal that offered it would
        be sending the agent to a 409 it reaches *after* writing the change.
        """
        branch = "platform-agent/scale-web"
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject("submit", "--branch", branch, "--title", "first", "--body", "one")
        again = self.prepare(branch, force=True)
        self.edit(again, "replicas: 5\n")
        self.broker.proposals[0]["state"] = "closed"
        published = len(self.broker.payloads("publish"))
        with self.assertRaises(ValueError) as caught:
            self.run_subject(
                "submit", "--branch", branch, "--base", "main",
                "--title", "second", "--body", "two",
            )
        said = str(caught.exception)
        self.assertIn("no proposal is open", said)
        self.assertIn("--base does not get past that", said)
        # Nothing further was sent: the refusal is ahead of the publish, which
        # is the half that matters -- the 409 would arrive after the push.
        self.assertEqual(len(self.broker.payloads("publish")), published)
        self.assertEqual(len(self.broker.payloads("proposal-create")), 1)

    def test_a_first_submission_with_nothing_to_publish_is_still_refused(self):
        """The same state with no proposal open is a mistake, and stays one."""
        self.prepare()
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        self.assertIn("no new revisions", str(caught.exception))
        self.assertEqual(self.broker.payloads("proposal-create"), [])

    def test_resubmitting_an_open_proposal_with_nothing_new_is_not_an_error(self):
        """SKILL.md's Step 3 says so -- "resubmitting is not an error" -- and a
        card retry is the ordinary way there.

        Under the `git push --force-with-lease` + `gh pr edit` pair this
        replaced it was true; the publish refusal made it false for one round.
        """
        first = self.prepare()
        self.edit(first)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
        )
        second = self.prepare(force=True)
        self.edit(second, "replicas: 4\n")
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t2", "--body", "b"
        )
        published = len(self.broker.payloads("publish"))

        _, url = self.run_subject(
            "submit", "--branch", "platform-agent/scale-web",
            "--title", "t3", "--body", "b",
        )
        self.assertEqual(url, self.broker.proposals[0]["url"])
        self.assertEqual(self.broker.proposals[0]["title"], "t3")
        self.assertEqual(len(self.broker.payloads("publish")), published)

    def test_a_proposal_opened_by_a_racing_run_is_updated_not_reported_as_failure(self):
        prepared = self.prepare()
        self.edit(prepared)
        raced = self.existing_proposal("platform-agent/scale-web")
        # The lookup before the publish is what would normally find it; this is
        # the window after that read, so the fake refuses the create the way a
        # forge does and the script has to ask again.
        self.broker.create_fails_with = vcs_client.VcsError(
            "a pull request for branch already exists", code="FORGE_REJECTED"
        )
        with mock.patch.object(submit_suggestion, "open_proposal", side_effect=[None, raced]):
            _, out = self.run_subject(
                "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b"
            )
        self.assertEqual(out, raced["url"])
        self.assertEqual(self.broker.proposals[0]["title"], "t")

    def test_a_create_that_fails_for_another_reason_is_not_swallowed(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.broker.create_fails_with = vcs_client.VcsError("base is protected", code="FORGE_REJECTED")
        with self.assertRaises(vcs_client.VcsError) as caught:
            self.run_subject("submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b")
        self.assertEqual(caught.exception.code, "FORGE_REJECTED")

    def test_base_names_what_the_change_merges_into(self):
        git(self.origin, "branch", "release", "main")
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b",
            "--base", "release",
        )
        self.assertEqual(self.broker.payloads("proposal-create")[0]["target"], "release")
        self.assertEqual(self.broker.payloads("publish")[0]["target"], "release")

    def test_base_on_a_second_round_says_the_proposal_does_not_move(self):
        """`--base` is read as a base, and is not a retarget.

        It is what a caller reaches for when they mean "publish this onto
        `release` instead", and the publish does honour it -- but no forge in
        this protocol lets `proposal-update` move an open proposal's target, so
        the proposal goes on pointing where it was opened. Silence there reads
        as agreement, which is a round published against a base nobody is
        reviewing it against.
        """
        git(self.origin, "branch", "release", "main")
        branch = "platform-agent/scale-web"
        self.edit(self.prepare(branch))
        self.run_subject("submit", "--branch", branch, "--title", "first round", "--body", "b")
        proposal = self.broker.proposals[-1]
        self.edit(self.prepare(branch, force=True), "replicas: 4\n")
        self.logged.clear()

        self.run_subject("submit", "--branch", branch, "--title", "second round", "--body", "b",
                         "--base", "release")

        said = "\n".join(self.logged)
        self.assertIn("--base release", said)
        self.assertIn(proposal["url"], said)
        self.assertIn("still targets main", said)
        # Said, not done: the publish goes where the flag says and the proposal
        # stays where it was opened.
        self.assertEqual(self.broker.payloads("publish")[-1]["target"], "release")
        self.assertEqual(proposal["target"], "main")

    # -- the description file ---------------------------------------------

    def test_a_body_file_outside_scratch_is_refused(self):
        self.prepare()
        outside = Path(self.tmp.name) / "elsewhere.md"
        outside.write_text("secrets\n")
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "b", "--title", "t", "--body-file", str(outside))
        self.assertIn("resolves outside", str(caught.exception))

    def test_a_body_file_symlinked_out_of_scratch_is_refused(self):
        self.prepare()
        outside = Path(self.tmp.name) / "elsewhere.md"
        outside.write_text("secrets\n")
        link = self.scratch / "body.md"
        link.symlink_to(outside)
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "b", "--title", "t", "--body-file", str(link))
        self.assertIn("resolves outside", str(caught.exception))

    def test_an_empty_body_file_is_refused(self):
        self.prepare()
        empty = self.scratch / "body.md"
        empty.write_text("   \n")
        with self.assertRaises(ValueError) as caught:
            self.run_subject("submit", "--branch", "b", "--title", "t", "--body-file", str(empty))
        self.assertIn("is empty", str(caught.exception))

    # -- the call shapes that outlive a roll -------------------------------

    def test_a_retired_flag_is_ignored_with_a_line_saying_so(self):
        prepared = self.prepare()
        self.edit(prepared)
        self.run_subject(
            "submit", "--branch", "platform-agent/scale-web", "--title", "t", "--body", "b",
            "--workspace", "/opt/data/gitops/t_9f3c/acme__infra",
            "--lease", "t_9f3c",
        )
        self.assertTrue(any("--workspace is no longer read" in line for line in self.logged))
        self.assertTrue(any("--lease is no longer read" in line for line in self.logged))

    def test_a_submit_with_no_copy_is_sent_to_prepare_and_not_to_clone(self):
        """What the retired flags promise the caller will be told instead.

        A command written against the old shape reaches `submit` with nothing
        on the volume. `vcs_client`'s own refusal ends "Run `vcs.py clone
        <url>` first", which is the wrong verb here: it brings the repository
        down without cutting the branch, so the next `submit` is refused again
        for standing on the trunk and there is a stray copy to clean up. The
        refusal has to name `prepare`, which does both.
        """
        with self.assertRaises(ValueError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web",
                "--title", "t", "--body", "b",
                "--workspace", "/opt/data/gitops/t_9f3c/acme__infra",
            )
        said = str(caught.exception)
        self.assertIn("prepare", said)
        self.assertIn("platform-agent/scale-web", said)
        self.assertNotIn("vcs.py clone", said.split("(")[0])

    def test_the_content_mode_submit_reaches_the_refusal_that_names_prepare(self):
        """The shape that was actually live, not the one the flags were named for.

        `--workspace`/`--lease` belong to the leased-clone mode. The operator
        renders `CREDENTIAL_PROXY_CONTENT_WORKSPACE=1` unconditionally and
        gives no field to turn it off, so the card in flight during a rollout
        is calling content mode: `--handle … --from … --base … --base-sha …`.
        Leave `--from` and `--delete` out of `RETIRED` and argparse exits on
        "unrecognized arguments" before `handle_submit` is ever reached, which
        is the one thing the shim exists to prevent.
        """
        with self.assertRaises(ValueError) as caught:
            self.run_subject(
                "submit", "--branch", "platform-agent/scale-web",
                "--title", "t", "--body", "b",
                "--handle", "ws_7c21",
                "--from", "/tmp/scratch",
                "--base", "main",
                "--base-sha", "a" * 40,
                "--delete", "old/thing.yaml",
            )
        said = str(caught.exception)
        self.assertIn("prepare", said)
        self.assertIn("platform-agent/scale-web", said)

    def test_the_read_half_of_content_mode_says_where_the_files_are(self):
        """`list` and `fetch` were commands, so they fail before any flag is read.

        `normalise_argv` prefixes an unrecognised argv with `submit`, which
        turns `list --handle X` into `submit list --handle X` and loses the
        whole call behind "unrecognized arguments: list". These two have
        somewhere to send the caller that the retired flags do not -- the files
        are on disk in the working copy -- so they say it.
        """
        for command in ("list", "fetch"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError) as caught:
                    self.run_subject(command, "--handle", "ws_7c21")
                said = str(caught.exception)
                self.assertIn(f"`{command}` is no longer a command", said)
                self.assertIn("working copy", said)
                self.assertIn("prepare", said)

    def test_an_argv_with_no_subcommand_is_read_as_submit(self):
        self.assertEqual(
            submit_suggestion.normalise_argv(["--branch", "b", "--title", "t"]),
            ["submit", "--branch", "b", "--title", "t"],
        )
        for argv in ([], ["prepare", "--branch", "b"], ["-h"]):
            self.assertEqual(submit_suggestion.normalise_argv(argv), argv)

    def test_the_two_commands_are_the_whole_surface(self):
        # `list` and `fetch` were the read half of a mode where the agent had
        # no checkout. It has one again.
        self.assertEqual(submit_suggestion.COMMANDS, ("prepare", "submit"))
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", io.StringIO()):
                submit_suggestion.build_parser().parse_args(["list", "--handle", "x"])


class TestValidateRepo(unittest.TestCase):
    """The repository gate, asked directly rather than through a run.

    Here because the gate is the one thing in this script standing between a
    repository the model named and a credentialed push, and the properties that
    make it a gate -- a malformed slug is refused, an unreadable allowlist is
    refused -- are invisible in a run that supplies a well-formed slug and a
    readable one.
    """

    def test_a_malformed_slug_is_refused(self):
        for bad in ("", "foo", "foo/bar/baz", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    submit_suggestion.validate_repo(bad)
                self.assertIn("Invalid repository format", str(caught.exception))

    def test_an_unreadable_managed_list_is_refused_rather_than_read_as_empty(self):
        with mock.patch.object(
            gitops_workspace, "get_managed_github_repos",
            mock.Mock(side_effect=RuntimeError("kubectl failed: Forbidden")),
        ):
            with self.assertRaises(RuntimeError) as caught:
                submit_suggestion.validate_repo("acme/any")
        self.assertIn("kubectl failed: Forbidden", str(caught.exception))

    def test_an_empty_managed_list_means_no_allowlist_is_configured(self):
        # The other reading of an empty list, and the reason the one above
        # matters: "" and "the read failed" must not arrive at the same place.
        with mock.patch.object(gitops_workspace, "get_managed_github_repos", lambda: []):
            with mock.patch.object(gitops_workspace, "validate_repo_org", lambda repo: repo):
                self.assertEqual(submit_suggestion.validate_repo("acme/any"), "acme/any")

    def test_a_repository_outside_a_populated_managed_list_is_refused(self):
        with mock.patch.object(
            gitops_workspace, "get_managed_github_repos", lambda: ["acme/managed"]
        ):
            with self.assertRaises(ValueError) as caught:
                submit_suggestion.validate_repo("acme/unmanaged")
        self.assertIn("not in the managed repositories list", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
