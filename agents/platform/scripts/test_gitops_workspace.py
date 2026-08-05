"""Unit tests for gitops_workspace — the leased clone every skill writes in.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_gitops_workspace.py' -v

Stdlib only, matching the other agent-script tests. The clone/fetch/reset path
is driven through the injected runner, except where a recorded runner would make
a test vacuous: `git clean -fd` deletes nothing when it is only recorded, so the
test that the audit's untracked manifests survive a reattach runs real git
against a local bare repository.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_workspace  # noqa: E402

LEASE = "compliance-audit"


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.root = self.tmp_path / "gitops"
        self.calls = []
        # Resolved once per clone and memoised; a temp path is unique per test,
        # but clearing keeps a rename or a reused fixture from leaking an answer.
        gitops_workspace.forget_base_branch()
        self.addCleanup(gitops_workspace.forget_base_branch)
        # What the remote advertises, in the shape `git symbolic-ref --short`
        # prints it. Tests that model a `master` fleet reassign this.
        self.origin_head = "origin/main"

    def runner(self, cmd, *, cwd=None, check=True):
        self.calls.append(list(cmd))
        if cmd[:2] == ["git", "clone"]:
            (Path(cmd[-1]) / ".git").mkdir(parents=True, exist_ok=True)
        if cmd[1:2] == ["symbolic-ref"]:
            if not self.origin_head:
                return CompletedProcess(cmd, 1, "", "")
            return CompletedProcess(cmd, 0, self.origin_head + "\n", "")
        return CompletedProcess(cmd, 0, "", "")

    def ensure(self, repo="acme/fleet", **kwargs):
        kwargs.setdefault("lease", LEASE)
        kwargs.setdefault("root", self.root)
        return gitops_workspace.ensure_workspace(repo, self.runner, **kwargs)


# --------------------------------------------------------------------------- #
# Where the clone goes — one per lease, so agents stop sharing one tree
# --------------------------------------------------------------------------- #


class TestWorkspacePath(WorkspaceTestCase):
    def test_the_path_carries_the_lease_and_the_repository(self):
        self.assertEqual(
            gitops_workspace.workspace_path("acme/fleet", self.root, lease=LEASE),
            self.root / LEASE / "acme__fleet",
        )

    def test_two_leases_never_share_a_working_tree(self):
        # The whole point. Before leases this returned one path for the pod, and
        # `submit-suggestion` branched inside a running audit's clone.
        first = gitops_workspace.workspace_path("acme/fleet", self.root, lease="audit")
        second = gitops_workspace.workspace_path("acme/fleet", self.root, lease="t_99")
        self.assertNotEqual(first, second)

    def test_a_malformed_repository_is_refused(self):
        for repo in ("fleet", "", "acme/"):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    gitops_workspace.workspace_path(repo, self.root, lease=LEASE)

    def test_a_lease_cannot_climb_out_of_the_root(self):
        # The lease reaches this module from an env var and, for
        # submit-suggestion, from an agent-supplied flag. Traversal must be
        # impossible rather than merely unlikely: either the id sanitises down
        # to one harmless segment, or it is refused outright.
        for lease in ("../../etc", "a/../..", "/etc", "..", "."):
            with self.subTest(lease=lease):
                try:
                    target = gitops_workspace.workspace_path(
                        "acme/fleet", self.root, lease=lease
                    )
                except ValueError:
                    continue
                self.assertEqual(target.parent.parent, self.root)
                self.assertNotIn("..", target.parts)

    def test_an_empty_lease_is_refused_rather_than_defaulted(self):
        # Silently falling back to a shared default here would put every
        # caller with a bad id back in one tree — the exact bug.
        for lease in ("", "   ", "...", "///", None):
            with self.subTest(lease=lease):
                with self.assertRaises(ValueError):
                    gitops_workspace.sanitize_lease(lease)


class TestLeaseId(WorkspaceTestCase):
    def test_an_explicit_lease_wins(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_from_env"}):
            self.assertEqual(gitops_workspace.lease_id("t_explicit"), "t_explicit")

    def test_the_kanban_task_is_the_default(self):
        # Every dispatcher-spawned worker has this pinned, and it is exactly the
        # granularity wanted: one card, one unit of work, one tree.
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_751ffb70"}):
            self.assertEqual(gitops_workspace.lease_id(), "t_751ffb70")

    def test_the_session_id_is_the_fallback(self):
        with patch.dict(
            os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_SESSION_ID": "s-42"}
        ):
            self.assertEqual(gitops_workspace.lease_id(), "s-42")

    def test_no_identity_still_yields_an_isolated_lease(self):
        # Not recoverable by a later process, but still nobody else's tree —
        # which is the property that matters.
        with patch.dict(
            os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_SESSION_ID": ""}
        ):
            first = gitops_workspace.lease_id()
            second = gitops_workspace.lease_id()
        self.assertTrue(first.startswith("adhoc-"))
        self.assertNotEqual(first, second)


# --------------------------------------------------------------------------- #
# The clone itself
# --------------------------------------------------------------------------- #


class TestEnsureWorkspace(WorkspaceTestCase):
    def test_the_first_run_clones_and_lands_on_main(self):
        target = self.ensure()
        self.assertTrue((target / ".git").is_dir())
        self.assertEqual(self.calls[0][:2], ["git", "clone"])
        self.assertIn(["git", "checkout", "-B", "main", "origin/main"], self.calls)

    def test_a_later_run_fetches_instead_of_cloning(self):
        self.ensure()
        self.calls.clear()
        self.ensure()
        self.assertFalse([c for c in self.calls if c[:2] == ["git", "clone"]])
        self.assertIn(["git", "fetch", "--quiet", "--prune", "origin"], self.calls)

    def test_a_second_lease_clones_again_rather_than_reusing(self):
        self.ensure(lease="audit-one")
        self.calls.clear()
        self.ensure(lease="audit-two")
        self.assertEqual(self.calls[0][:2], ["git", "clone"])

    def test_a_half_finished_clone_is_cleared_rather_than_blocking_forever(self):
        target = gitops_workspace.workspace_path("acme/fleet", self.root, lease=LEASE)
        (target / "leftover").mkdir(parents=True)
        self.ensure()
        self.assertFalse((target / "leftover").exists())
        self.assertTrue((target / ".git").is_dir())

    def test_a_clone_that_produced_no_tree_raises(self):
        def dead(cmd, *, cwd=None, check=True):
            return CompletedProcess(cmd, 0, "", "")

        with self.assertRaises(RuntimeError):
            gitops_workspace.ensure_workspace(
                "acme/fleet", dead, lease=LEASE, root=self.root
            )

    def test_the_working_tree_is_reset_before_use(self):
        self.ensure()
        joined = [" ".join(c) for c in self.calls]
        self.assertIn("git reset --hard --quiet", joined)
        self.assertIn("git clean -fdq", joined)

    def test_reset_false_fetches_but_scrubs_nothing(self):
        # `finish` reattaches to a tree that already holds the audit's
        # remediation manifests, untracked. A clean here deletes every one of
        # them and the run then reports each fix as a file the model forgot.
        self.ensure()
        self.calls.clear()
        self.ensure(reset=False)
        joined = [" ".join(c) for c in self.calls]
        self.assertIn("git fetch --quiet --prune origin", joined)
        self.assertNotIn("git clean -fdq", joined)
        self.assertNotIn("git reset --hard --quiet", joined)
        self.assertFalse([c for c in self.calls if c[:2] == ["git", "checkout"]])

    def test_reset_false_still_clones_when_there_is_nothing_to_preserve(self):
        target = self.ensure(reset=False)
        self.assertEqual(self.calls[0][:2], ["git", "clone"])
        self.assertTrue((target / ".git").is_dir())

    def test_the_clone_runs_from_the_lease_directory(self):
        # Not the shared root. `git clone` is the one mutating-ish verb the
        # credential proxy lets through unleased, and it only stays safe because
        # it runs one directory above a tree that does not exist yet.
        self.ensure()
        clone = next(c for c in self.calls if c[:2] == ["git", "clone"])
        self.assertEqual(Path(clone[-1]).parent, self.root / LEASE)

    def test_an_untracked_manifest_survives_a_real_reattach(self):
        """The mocked runner cannot see this one, so run real git.

        `git clean -fd` on the way into `finish` is invisible to a recorded
        runner: nothing actually deletes anything, so the fixture the test
        wrote is still on disk and the assertion passes on code that would
        wipe the tree in production.
        """
        if shutil.which("git") is None:  # pragma: no cover - git is always present
            self.skipTest("git is not on PATH")

        def real(cmd, *, cwd=None, check=True):
            return subprocess.run(
                cmd, cwd=cwd, check=check, capture_output=True, text=True
            )

        origin = seed_origin(self.tmp_path)

        target = gitops_workspace.ensure_workspace(
            "acme/fleet", real, lease=LEASE, root=self.root, remote_url=str(origin)
        )
        manifest = target / "clusters/prod/netpol.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("kind: NetworkPolicy\n", encoding="utf-8")

        gitops_workspace.ensure_workspace(
            "acme/fleet",
            real,
            lease=LEASE,
            root=self.root,
            remote_url=str(origin),
            reset=False,
        )
        self.assertTrue(manifest.is_file(), "finish deleted the fix it was about to open")

        gitops_workspace.ensure_workspace(
            "acme/fleet",
            real,
            lease=LEASE,
            root=self.root,
            remote_url=str(origin),
            reset=True,
        )
        self.assertFalse(manifest.exists(), "start must hand the audit a clean tree")

    def test_the_identity_is_repository_local_never_global(self):
        target = gitops_workspace.workspace_path("acme/fleet", self.root, lease=LEASE)
        gitops_workspace.configure_identity(target, self.runner)
        self.assertTrue(self.calls)
        for call in self.calls:
            self.assertNotIn("--global", call)
        self.assertEqual(self.calls[0][:3], ["git", "config", "user.name"])


class TestWorkspaceLock(WorkspaceTestCase):
    def test_the_lock_is_best_effort_not_a_reason_to_skip_the_audit(self):
        # A read-only or absent PVC must cost a retry, not the day's audit.
        with gitops_workspace.workspace_lock("/proc/nonexistent/gitops"):
            pass

    def test_the_lock_serialises_and_releases(self):
        with gitops_workspace.workspace_lock(self.root):
            pass
        with gitops_workspace.workspace_lock(self.root):
            pass

    def test_the_lock_is_released_before_the_caller_does_its_work(self):
        """Nothing may hold the root lock across a clone, a fetch or an audit.

        Holding it there is what the lease layout replaced: a ten-minute audit
        must not queue an interactive provisioning request behind it. Re-entering
        the lock from inside the runner would deadlock if `ensure_workspace`
        still held it.
        """
        entered = []

        def reentrant(cmd, *, cwd=None, check=True):
            with gitops_workspace.workspace_lock(self.root):
                entered.append(cmd[:2])
            return self.runner(cmd, cwd=cwd, check=check)

        gitops_workspace.ensure_workspace(
            "acme/fleet", reentrant, lease=LEASE, root=self.root
        )
        self.assertIn(["git", "clone"], entered)


# --------------------------------------------------------------------------- #
# The lease marker — who holds the tree, and when it may be reclaimed
# --------------------------------------------------------------------------- #


class TestLeaseMarker(WorkspaceTestCase):
    def holder(self, lease=LEASE):
        return self.root / lease

    def test_ensure_stamps_a_marker_the_proxy_can_find(self):
        target = self.ensure()
        record = gitops_workspace.read_lease(target.parent)
        self.assertEqual(record["lease"], LEASE)
        self.assertEqual(record["repo"], "acme/fleet")

    def test_refreshing_moves_the_clock_but_keeps_the_start_time(self):
        gitops_workspace.write_lease(self.holder(), LEASE, "acme/fleet")
        first = gitops_workspace.read_lease(self.holder())
        marker = self.holder() / gitops_workspace.LEASE_FILENAME
        os.utime(marker, (time.time() - 7200, time.time() - 7200))

        gitops_workspace.write_lease(self.holder(), LEASE, "acme/fleet")
        second = gitops_workspace.read_lease(self.holder())
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertGreater(marker.stat().st_mtime, time.time() - 60)

    def test_unreadable_and_malformed_markers_read_as_absent(self):
        self.holder().mkdir(parents=True)
        self.assertIsNone(gitops_workspace.read_lease(self.holder()))
        (self.holder() / gitops_workspace.LEASE_FILENAME).write_text("{oops", "utf-8")
        self.assertIsNone(gitops_workspace.read_lease(self.holder()))
        (self.holder() / gitops_workspace.LEASE_FILENAME).write_text("[]", "utf-8")
        self.assertIsNone(gitops_workspace.read_lease(self.holder()))

    def test_a_foreign_lease_is_refused(self):
        # The check the credential proxy cannot make: it sees that a push is
        # inside *some* lease, never whose.
        target = self.ensure(lease="audit-one")
        with self.assertRaises(PermissionError) as caught:
            gitops_workspace.assert_lease_owner(target, "t_someone_else")
        self.assertIn("audit-one", str(caught.exception))

    def test_an_unleased_directory_is_refused(self):
        loose = self.tmp_path / "profile" / "acme__fleet"
        loose.mkdir(parents=True)
        with self.assertRaises(PermissionError):
            gitops_workspace.assert_lease_owner(loose, LEASE)

    def test_our_own_lease_is_allowed(self):
        target = self.ensure()
        self.assertEqual(
            gitops_workspace.assert_lease_owner(target, LEASE)["lease"], LEASE
        )

    def test_a_subdirectory_of_our_own_tree_is_allowed(self):
        # `submit --workspace` defaults to os.getcwd(), and an agent that has
        # `cd`'d in to write a manifest reports the subdirectory. The credential
        # proxy walks ancestors for exactly this reason, so a check here that
        # only looked one level up would refuse a push the proxy allows.
        target = self.ensure()
        deep = target / "clusters" / "prod"
        deep.mkdir(parents=True)
        self.assertEqual(
            gitops_workspace.assert_lease_owner(deep, LEASE)["lease"], LEASE
        )

    def test_a_subdirectory_of_someone_else_tree_is_still_refused(self):
        # The walk must not turn the ownership check into a formality.
        target = self.ensure(lease="audit-one")
        deep = target / "clusters" / "prod"
        deep.mkdir(parents=True)
        with self.assertRaises(PermissionError) as caught:
            gitops_workspace.assert_lease_owner(deep, "t_someone_else")
        self.assertIn("audit-one", str(caught.exception))


# --------------------------------------------------------------------------- #
# Where the root is — `agentHome` moves it, and the proxy moves with it
# --------------------------------------------------------------------------- #


class TestAgentHome(WorkspaceTestCase):
    def test_the_default_is_the_operator_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gitops_workspace.agent_home(), "/opt/data")
            self.assertEqual(gitops_workspace.default_root(), "/opt/data/gitops")
            self.assertEqual(
                gitops_workspace.default_settings_path(), "/opt/data/SETTINGS.md"
            )

    def test_a_custom_agent_home_moves_the_root_and_the_settings_file(self):
        # `spec.harness.hermes.agentHome` sets PLATFORM_AGENT_HOME on the agent
        # and CREDENTIAL_PROXY_WORKSPACE_ROOT on the sidecar to the same value.
        # A clone under a hardcoded /opt/data would be outside the sidecar's
        # workspace root, and every git in it refused.
        with patch.dict(os.environ, {"PLATFORM_AGENT_HOME": "/srv/agent/"}, clear=True):
            self.assertEqual(gitops_workspace.agent_home(), "/srv/agent")
            self.assertEqual(gitops_workspace.default_root(), "/srv/agent/gitops")
            self.assertEqual(
                gitops_workspace.default_settings_path(), "/srv/agent/SETTINGS.md"
            )

    def test_the_root_defaults_are_resolved_per_call_not_at_import(self):
        # The signatures take None rather than a constant evaluated at import,
        # so a test — or a process that learns its home late — is not stuck with
        # whatever the environment said when the module first loaded.
        with patch.dict(os.environ, {"PLATFORM_AGENT_HOME": "/srv/agent"}, clear=True):
            self.assertEqual(
                gitops_workspace.workspace_path("acme/fleet", lease=LEASE),
                Path("/srv/agent/gitops") / LEASE / "acme__fleet",
            )


# --------------------------------------------------------------------------- #
# Which branch a pull request targets — not every fleet calls its trunk `main`
# --------------------------------------------------------------------------- #


class TestResolveBaseBranch(WorkspaceTestCase):
    def resolve(self, workspace=None):
        return gitops_workspace.resolve_base_branch(
            workspace if workspace is not None else self.tmp_path, self.runner
        )

    def test_the_remote_default_is_used(self):
        self.assertEqual(self.resolve(), "main")

    def test_a_master_fleet_is_not_forced_onto_main(self):
        # The bug this closes: `origin/main` does not resolve on this
        # repository, so every remediation checkout failed and the audit
        # reported the fix it could not push as one the model never wrote.
        self.origin_head = "origin/master"
        self.assertEqual(self.resolve(), "master")

    def test_an_operator_override_beats_the_remote(self):
        # A repository whose default branch is not the branch the fleet deploys
        # from. Nothing observable here would say so.
        self.origin_head = "origin/main"
        with patch.dict(os.environ, {"GITOPS_BASE_BRANCH": "release"}):
            self.assertEqual(self.resolve(), "release")

    def test_no_clone_yet_falls_back_without_running_git(self):
        self.assertEqual(
            gitops_workspace.resolve_base_branch(None, self.runner), "main"
        )
        self.assertEqual(self.calls, [])

    def test_an_absent_origin_head_is_re_asked_then_defaulted(self):
        # A clone made before this module started asking, or a remote that
        # changed its default afterwards, leaves the ref missing.
        self.origin_head = ""
        self.assertEqual(self.resolve(), "main")
        self.assertIn(
            ["git", "remote", "set-head", "origin", "--auto"], self.calls
        )

    def test_set_head_repairing_the_ref_is_believed(self):
        calls = []

        def runner(cmd, *, cwd=None, check=True):
            calls.append(list(cmd))
            if cmd[1:2] == ["symbolic-ref"]:
                if ["git", "remote", "set-head", "origin", "--auto"] in calls:
                    return CompletedProcess(cmd, 0, "origin/trunk\n", "")
                return CompletedProcess(cmd, 1, "", "")
            return CompletedProcess(cmd, 0, "", "")

        self.assertEqual(
            gitops_workspace.resolve_base_branch(self.tmp_path, runner), "trunk"
        )

    def test_the_answer_is_asked_for_once_per_clone(self):
        self.resolve()
        before = len(self.calls)
        self.resolve()
        self.assertEqual(len(self.calls), before)

    def test_ensure_workspace_checks_out_what_the_remote_advertises(self):
        self.origin_head = "origin/master"
        self.ensure()
        self.assertIn(
            ["git", "checkout", "-B", "master", "origin/master"], self.calls
        )

    def test_an_explicit_base_branch_still_wins(self):
        self.origin_head = "origin/master"
        self.ensure(base_branch="develop")
        self.assertIn(
            ["git", "checkout", "-B", "develop", "origin/develop"], self.calls
        )

    def test_a_real_master_repository_is_detected(self):
        """Driven through real git, because the ref this reads is git's own.

        A recorded runner proves only that the module parses whatever it is
        handed. Whether `git clone` leaves `refs/remotes/origin/HEAD` pointing
        at the remote's default — which is the entire premise — can only be
        settled by git.
        """
        if shutil.which("git") is None:  # pragma: no cover - git is always present
            self.skipTest("git is not on PATH")

        def real(cmd, *, cwd=None, check=True):
            return subprocess.run(
                cmd, cwd=cwd, check=check, capture_output=True, text=True
            )

        origin = seed_origin(self.tmp_path, branch="master")
        target = gitops_workspace.ensure_workspace(
            "acme/fleet", real, lease=LEASE, root=self.root, remote_url=str(origin)
        )
        self.assertEqual(gitops_workspace.resolve_base_branch(target, real), "master")
        head = real(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(target), check=True
        )
        self.assertEqual(head.stdout.strip(), "master")


class TestReaper(WorkspaceTestCase):
    def age(self, path, hours):
        marker = path / gitops_workspace.LEASE_FILENAME
        stamp = time.time() - hours * 3600
        os.utime(marker, (stamp, stamp))

    def test_an_abandoned_lease_is_reclaimed(self):
        stale = self.ensure(lease="dead-worker").parent
        self.age(stale, 48)
        removed = gitops_workspace.reap_stale_leases(self.root)
        self.assertEqual(removed, ["dead-worker"])
        self.assertFalse(stale.exists())

    def test_a_live_lease_is_left_alone(self):
        fresh = self.ensure(lease="working").parent
        self.assertEqual(gitops_workspace.reap_stale_leases(self.root), [])
        self.assertTrue(fresh.exists())

    def test_the_caller_own_lease_is_never_reaped_however_old(self):
        # `ensure_workspace` reaps before it refreshes, so without this a run
        # that straddles the TTL would delete the tree it is about to use.
        mine = self.ensure(lease="mine").parent
        self.age(mine, 999)
        self.assertEqual(
            gitops_workspace.reap_stale_leases(self.root, keep={"mine"}), []
        )
        self.assertTrue(mine.exists())

    def test_only_directories_holding_a_marker_are_touched(self):
        # The flat clone from before leases existed, plus anything a human left
        # under the root. Old and unreferenced is not the same as reapable.
        legacy = self.root / "acme__fleet"
        (legacy / ".git").mkdir(parents=True)
        os.utime(legacy, (time.time() - 999 * 3600,) * 2)
        (self.root / ".lock").write_text("", encoding="utf-8")

        gitops_workspace.reap_stale_leases(self.root)
        self.assertTrue(legacy.exists())

    def test_a_zero_ttl_disables_reaping_entirely(self):
        stale = self.ensure(lease="dead-worker").parent
        self.age(stale, 999)
        self.assertEqual(
            gitops_workspace.reap_stale_leases(self.root, ttl_hours=0), []
        )
        self.assertTrue(stale.exists())

    def test_the_ttl_is_operator_overridable(self):
        stale = self.ensure(lease="dead-worker").parent
        self.age(stale, 2)
        with patch.dict(os.environ, {"GITOPS_LEASE_TTL_HOURS": "1"}):
            self.assertEqual(
                gitops_workspace.reap_stale_leases(self.root), ["dead-worker"]
            )
        self.assertFalse(stale.exists())

    def test_an_unparsable_ttl_falls_back_rather_than_crashing(self):
        with patch.dict(os.environ, {"GITOPS_LEASE_TTL_HOURS": "soon"}):
            self.assertEqual(
                gitops_workspace.lease_ttl_hours(),
                gitops_workspace.DEFAULT_LEASE_TTL_HOURS,
            )

    def test_an_absent_root_is_not_an_error(self):
        self.assertEqual(gitops_workspace.reap_stale_leases(self.root / "nope"), [])

    def test_ensure_reaps_on_the_way_in(self):
        stale = self.ensure(lease="dead-worker").parent
        self.age(stale, 48)
        self.ensure(lease="alive")
        self.assertFalse(stale.exists())


# --------------------------------------------------------------------------- #
# Which repository — the one source that works before anything is cloned
# --------------------------------------------------------------------------- #


class TestResolveRepo(WorkspaceTestCase):
    def settings(self, text):
        path = self.tmp_path / "SETTINGS.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_the_operator_written_line_is_parsed(self):
        path = self.settings(
            "# GKE Scope Configuration\n"
            "- **Git Repo:** https://github.com/acme/fleet.git\n"
        )
        self.assertEqual(gitops_workspace.resolve_repo(path), "acme/fleet")

    def test_an_ssh_remote_is_parsed(self):
        path = self.settings("- **Git Repo:** git@github.com:acme/fleet.git\n")
        self.assertEqual(gitops_workspace.resolve_repo(path), "acme/fleet")

    def test_the_unset_placeholder_is_not_a_repository(self):
        # The operator writes the literal `None` when the CR omits the repo.
        path = self.settings("- **Git Repo:** None\n")
        self.assertIsNone(gitops_workspace.repo_from_settings(path))

    def test_a_missing_settings_file_is_not_an_error_on_its_own(self):
        absent = str(self.tmp_path / "absent.md")
        self.assertIsNone(gitops_workspace.repo_from_settings(absent))

    def test_it_falls_back_to_the_git_remote(self):
        module = type(sys)("github_token_refresh")
        module.get_current_git_repo = lambda: "acme/from-remote"
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            self.assertEqual(
                gitops_workspace.resolve_repo(str(self.tmp_path / "absent.md")),
                "acme/from-remote",
            )

    def test_both_sources_failing_names_both_sources(self):
        missing = str(self.tmp_path / "absent.md")
        module = type(sys)("github_token_refresh")
        module.get_current_git_repo = lambda: None
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            with self.assertRaises(RuntimeError) as caught:
                gitops_workspace.resolve_repo(missing)
        self.assertIn(missing, str(caught.exception))
        self.assertIn("origin remote", str(caught.exception))

    def test_the_settings_path_is_operator_overridable(self):
        path = self.settings("- **Git Repo:** acme/fleet\n")
        with patch.dict(os.environ, {"GITOPS_SETTINGS": path}):
            self.assertEqual(gitops_workspace.repo_from_settings(), "acme/fleet")


def seed_origin(tmp_path: Path, branch: str = "main") -> Path:
    """A local bare repository with one commit on `branch`.

    `branch` is a parameter because the harness must work against a repository
    whose trunk is not `main` — that is the case the hardcoded base branch got
    wrong, and a fixture that can only be `main` cannot show it.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    for cmd in (
        ["git", "init", "--quiet", "--bare", f"--initial-branch={branch}", str(origin)],
        ["git", "init", "--quiet", f"--initial-branch={branch}", str(seed)],
    ):
        subprocess.run(cmd, check=True, capture_output=True)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    for cmd in (
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "add", "README.md"],
        ["git", "commit", "--quiet", "-m", "seed"],
        ["git", "remote", "add", "origin", str(origin)],
        ["git", "push", "--quiet", "origin", branch],
    ):
        subprocess.run(cmd, cwd=seed, check=True, capture_output=True)
    return origin


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
