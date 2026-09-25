"""Unit tests for cluster_agent_reconcile — the orphaned-profile pruner.

Run: python3 -m unittest agents.platform.scripts.test_cluster_agent_reconcile

The safety-critical invariant under test: a profile is deleted ONLY on a definitive
GKE NotFound. Missing identity, transient errors, and reserved profiles are never
deleted.
"""

import json
from datetime import datetime, timezone
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_platforms  # noqa: E402
import cluster_agent_profile as cap  # noqa: E402
import cluster_agent_reconcile as rec  # noqa: E402


def _identity(project="p", cluster="c", location="us-central1"):
    return {"project": project, "cluster": cluster, "location": location}


def _home_factory(root: Path, incomplete=frozenset()):
    """A ``profile_home`` stub over real directories.

    reconcile() reads the filesystem to tell a finished scaffold from one killed
    after the identity stamp, so these have to exist. Names in ``incomplete`` get
    the directory without the artifacts ``create_profile`` writes last. The
    kubeconfig is written here too, for ``_local_kubeconfig_landed`` to find --
    on a real install it is on the sandbox's volume, not this one.
    """
    made: set[str] = set()

    def factory(name: str) -> Path:
        home = root / name
        # Created once: a home the test's delete stub removed stays removed, so the
        # script's "is it still on disk" checks see what production would.
        if name not in made:
            made.add(name)
            home.mkdir(parents=True, exist_ok=True)
            if name not in incomplete:
                for artifact in (*rec.SCAFFOLD_ARTIFACTS, rec.KUBECONFIG_ARTIFACT):
                    (home / artifact).touch()
        return home

    return factory


def _listing(clusters):
    """What `_list_project` returns for a stubbed cluster list: the list and its outcome."""
    if clusters is None:
        return None, rec.OUTCOME_UNREACHABLE
    return clusters, rec.OUTCOME_OK


def _local_kubeconfig_landed(path: Path) -> bool:
    """A ``kubeconfig_landed`` stub that answers from this filesystem.

    The real one asks the sandbox over SSH. Patching it keeps these tests off
    that path and off whatever HERMES_SANDBOX_* happens to be set in the
    environment running them.
    """
    return path.exists()


class HomesMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.homes = Path(self._tmp.name)
        # Answer the kubeconfig question from this filesystem for every test in
        # these classes. The real one asks the sandbox over SSH, so leaving it
        # in place would make the result depend on the environment running the
        # suite. IncompleteScaffoldTest patches it again, per test, to say what
        # the sandbox answered.
        landed = mock.patch.object(rec, "kubeconfig_landed", side_effect=_local_kubeconfig_landed)
        landed.start()
        self.addCleanup(landed.stop)
        # The scope snapshot is written beside the profiles; keep it in the temp dir, and
        # start every test from no declared scope.
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(rec.SCOPE_FILE_ENV, None)


class ReconcileTest(HomesMixin):
    def _run(self, profiles, identities, existence, dry_run=False):
        """Drive reconcile() with stubbed enumeration/identity/liveness/delete.

        identities: name -> identity dict or None
        existence:  name -> True/False/None (cluster exists / gone / unknown)
        Returns (report, list_of_deleted_names).
        """
        deleted: list[str] = []
        # _project_source() -> None disables the CREATE direction, isolating the prune behavior
        # under test (and avoiding any real metadata/gcloud calls).
        with mock.patch.object(rec, "_project_source", return_value=(None, True)), \
             mock.patch.object(rec, "list_profiles", return_value=profiles), \
             mock.patch.object(rec, "profile_home", side_effect=_home_factory(self.homes)), \
             mock.patch.object(rec, "read_cluster_identity", side_effect=lambda home: identities[home.name]), \
             mock.patch.object(rec, "_cluster_exists", side_effect=lambda **kw: existence[_name_for(kw, identities)]), \
             mock.patch.object(rec, "delete_profile", side_effect=deleted.append):
            report = rec.reconcile(dry_run=dry_run)
        return report, deleted

    def test_orphan_deleted_live_kept(self):
        profiles = ["cluster-a", "cluster-b"]
        identities = {"cluster-a": _identity(cluster="a"), "cluster-b": _identity(cluster="b")}
        existence = {"cluster-a": False, "cluster-b": True}  # a is gone, b is live
        report, deleted = self._run(profiles, identities, existence)
        self.assertEqual(deleted, ["cluster-a"])
        self.assertEqual(report["pruned"], ["cluster-a"])
        self.assertEqual(report["kept"], ["cluster-b"])

    def test_transient_error_never_deletes(self):
        # The critical safety case: an inconclusive liveness check must NOT prune.
        profiles = ["cluster-a"]
        identities = {"cluster-a": _identity(cluster="a")}
        existence = {"cluster-a": None}  # unknown (auth/network/timeout)
        report, deleted = self._run(profiles, identities, existence)
        self.assertEqual(deleted, [])
        self.assertEqual(report["pruned"], [])
        self.assertEqual(report["skipped_error"], ["cluster-a"])

    def test_missing_identity_skipped(self):
        profiles = ["cluster-a"]
        identities = {"cluster-a": None}  # unreadable/absent cluster_identity
        existence: dict = {}
        report, deleted = self._run(profiles, identities, existence)
        self.assertEqual(deleted, [])
        self.assertEqual(report["skipped_no_identity"], ["cluster-a"])

    def test_dry_run_deletes_nothing_but_reports(self):
        profiles = ["cluster-a"]
        identities = {"cluster-a": _identity(cluster="a")}
        existence = {"cluster-a": False}  # gone
        report, deleted = self._run(profiles, identities, existence, dry_run=True)
        self.assertEqual(deleted, [])            # delete_profile never called
        self.assertEqual(report["pruned"], ["cluster-a"])  # still reported as would-prune


def _name_for(identity_kwargs, identities):
    """Reverse-map an identity dict back to its profile name for the existence stub."""
    for name, ident in identities.items():
        if ident == identity_kwargs:
            return name
    raise KeyError(identity_kwargs)


class CreateDirectionTest(HomesMixin):
    def test_creates_missing_including_the_management_cluster(self):
        # "mgmt" is the cluster this pod runs on. It used to be excluded by name via the
        # metadata server; it is now managed like any other, because the triage session for
        # an event on it is created on its own profile and there would otherwise be none.
        created: list = []
        with mock.patch.object(rec, "_project_source", return_value=("p", True)), \
             mock.patch.object(rec, "_list_project", return_value=_listing([
                 ("p", "alpha", "us-central1"),   # no profile -> CREATE
                 ("p", "beta", "us-central1"),    # already has a profile -> skip
                 ("p", "mgmt", "us-central1"),    # the management cluster -> CREATE too
             ])), \
             mock.patch.object(rec, "list_profiles", return_value=["cluster-beta"]), \
             mock.patch.object(rec, "profile_home", side_effect=_home_factory(self.homes)), \
             mock.patch.object(rec, "read_cluster_identity",
                               side_effect=lambda home: {"project": "p", "cluster": "beta", "location": "us-central1"}), \
             mock.patch.object(rec, "_cluster_exists", return_value=True), \
             mock.patch.object(rec, "delete_profile"), \
             mock.patch.object(rec, "create_profile",
                               side_effect=lambda pr, c, l: created.append((pr, c, l)) or f"cluster-{c}"):
            report = rec.reconcile(dry_run=False)
        self.assertEqual(created, [("p", "alpha", "us-central1"), ("p", "mgmt", "us-central1")])
        self.assertEqual(report["created"], ["cluster-alpha", "cluster-mgmt"])
        self.assertEqual(report["kept"], ["cluster-beta"])

    def test_an_excluded_cluster_loses_the_profile_it_already_has(self):
        # RECONCILE_EXCLUDE is now the only opt-out, so it has to prune as well as skip:
        # adding a name after the fact must remove the profile, not leave it orphaned.
        deleted: list[str] = []
        with mock.patch.object(rec, "_project_source", return_value=("p", True)), \
             mock.patch.object(rec, "EXTRA_EXCLUDE", {"skipme"}), \
             mock.patch.object(rec, "_list_project", return_value=_listing([("p", "skipme", "us-central1")])), \
             mock.patch.object(rec, "list_profiles", return_value=["cluster-skipme"]), \
             mock.patch.object(rec, "profile_home", side_effect=_home_factory(self.homes)), \
             mock.patch.object(rec, "read_cluster_identity",
                               side_effect=lambda home: {"project": "p", "cluster": "skipme", "location": "us-central1"}), \
             mock.patch.object(rec, "_cluster_exists", return_value=True), \
             mock.patch.object(rec, "create_profile", side_effect=AssertionError("must not create an excluded cluster")), \
             mock.patch.object(rec, "delete_profile", side_effect=deleted.append):
            report = rec.reconcile(dry_run=False)
        self.assertEqual(deleted, ["cluster-skipme"])
        self.assertEqual(report["pruned"], ["cluster-skipme"])

    def test_extra_exclude_names_skipped(self):
        created: list = []
        with mock.patch.object(rec, "_project_source", return_value=("p", True)), \
             mock.patch.object(rec, "EXTRA_EXCLUDE", {"skipme"}), \
             mock.patch.object(rec, "_list_project", return_value=_listing([
                 ("p", "keep", "us-central1"), ("p", "skipme", "us-central1")])), \
             mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "profile_home", side_effect=_home_factory(self.homes)), \
             mock.patch.object(rec, "read_cluster_identity", return_value=None), \
             mock.patch.object(rec, "_cluster_exists", return_value=True), \
             mock.patch.object(rec, "delete_profile"), \
             mock.patch.object(rec, "create_profile", side_effect=lambda pr, c, l: created.append(c) or f"cluster-{c}"):
            rec.reconcile(dry_run=False)
        self.assertEqual(created, ["keep"])  # skipme excluded via RECONCILE_EXCLUDE


class IncompleteScaffoldTest(HomesMixin):
    """A scaffold killed after the identity stamp must be finished, not adopted.

    The bootstrap gate runs this script under a timeout and Python SIGKILLs on
    expiry. ``create_profile`` writes ``cluster_identity`` into ``config.yaml``
    before it fetches the kubeconfig and writes ``USER.md``, so a kill in that
    window leaves a home that reads as managed: CREATE would skip the cluster and
    PRUNE would keep it, stranding the profile for the life of the volume.
    """

    def _reconcile(self, incomplete):
        created: list = []
        with mock.patch.object(rec, "kubeconfig_landed", side_effect=_local_kubeconfig_landed), \
             mock.patch.object(rec, "_project_source", return_value=("p", True)), \
             mock.patch.object(rec, "_list_project", return_value=_listing([("p", "beta", "us-central1")])), \
             mock.patch.object(rec, "list_profiles", return_value=["cluster-beta"]), \
             mock.patch.object(rec, "profile_home",
                               side_effect=_home_factory(self.homes, incomplete=incomplete)), \
             mock.patch.object(rec, "read_cluster_identity", side_effect=lambda home: _identity(cluster="beta")), \
             mock.patch.object(rec, "_cluster_exists", return_value=True), \
             mock.patch.object(rec, "delete_profile", side_effect=AssertionError("must never prune a live cluster")), \
             mock.patch.object(rec, "create_profile",
                               side_effect=lambda pr, c, l: created.append(c) or f"cluster-{c}"):
            report = rec.reconcile(dry_run=False)
        return report, created

    def test_a_half_scaffolded_profile_is_recreated(self):
        report, created = self._reconcile(incomplete={"cluster-beta"})
        self.assertEqual(created, ["beta"])
        self.assertEqual(report["incomplete"], ["cluster-beta"])

    def test_a_finished_profile_is_left_alone(self):
        report, created = self._reconcile(incomplete=frozenset())
        self.assertEqual(created, [])
        self.assertEqual(report["incomplete"], [])
        self.assertEqual(report["kept"], ["cluster-beta"])

    def test_scaffold_gaps_names_what_is_missing(self):
        home = self.homes / "cluster-beta"
        home.mkdir()
        with mock.patch.object(rec, "kubeconfig_landed", side_effect=_local_kubeconfig_landed):
            self.assertEqual(rec._scaffold_gaps(home), ["kubeconfig.yaml", "USER.md"])
            (home / "kubeconfig.yaml").touch()
            self.assertEqual(rec._scaffold_gaps(home), ["USER.md"])
            (home / "USER.md").touch()
            self.assertEqual(rec._scaffold_gaps(home), [])

    def test_a_kubeconfig_only_the_sandbox_can_see_is_not_a_gap(self):
        """The regression the sandbox split introduces.

        With a sandbox, `gcloud container clusters get-credentials` runs in the
        shell pod and writes to the shell pod's volume. Stat'ing the path on
        this pod finds nothing, so every profile would read as half-scaffolded
        on every hourly tick: the whole fleet re-scaffolded, and a credential
        re-fetched per cluster, forever.
        """
        home = self.homes / "cluster-beta"
        home.mkdir()
        (home / "USER.md").touch()
        # Nothing at home/kubeconfig.yaml on this filesystem; the sandbox has it.
        with mock.patch.object(rec, "kubeconfig_landed", return_value=True) as landed:
            self.assertEqual(rec._scaffold_gaps(home), [])
        self.assertEqual(landed.call_args.args[0], home / "kubeconfig.yaml")

    def test_a_sandbox_that_cannot_answer_reads_as_a_gap(self):
        """kubeconfig_landed returns False when it cannot ask, and that must count.

        A recreated sandbox PVC is exactly this: the profile is on the gateway's
        volume, its credential is not, and re-scaffolding is the repair.
        """
        home = self.homes / "cluster-beta"
        home.mkdir()
        (home / "USER.md").touch()
        with mock.patch.object(rec, "kubeconfig_landed", return_value=False):
            self.assertEqual(rec._scaffold_gaps(home), ["kubeconfig.yaml"])


class AllClustersTest(unittest.TestCase):
    """A failed `gcloud list` must be loud and must not look like an empty project."""

    def test_parses_name_and_location(self):
        done = subprocess.CompletedProcess([], 0, stdout="a us-central1\nb europe-west1\n")
        with mock.patch.object(rec.subprocess, "run", return_value=done):
            self.assertEqual(
                rec._list_project("p")[0],
                [("p", "a", "us-central1"), ("p", "b", "europe-west1")],
            )

    def test_gcloud_failure_is_logged_with_stderr_not_silently_empty(self):
        # Without check=True this path returns a 0-length stdout and no log at all,
        # which the CREATE pass cannot tell apart from "the project has no clusters".
        err = subprocess.CalledProcessError(
            1, ["gcloud"], stderr="ERROR: (gcloud) Reauthentication required."
        )
        with mock.patch.object(rec.subprocess, "run", side_effect=err), \
             mock.patch.object(rec, "log") as logged:
            self.assertIsNone(rec._list_project("p")[0])
        self.assertIn("Reauthentication required", " ".join(str(c) for c in logged.call_args_list))

    def test_uses_check_true_so_nonzero_exit_cannot_pass_silently(self):
        seen = {}
        with mock.patch.object(rec.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="")
            rec._list_project("p")[0]
            seen = run.call_args.kwargs
        self.assertTrue(seen.get("check"), "gcloud list must run with check=True")

    def test_timeout_degrades_to_empty_and_logs(self):
        with mock.patch.object(rec.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["gcloud"], 120)), \
             mock.patch.object(rec, "log") as logged:
            self.assertIsNone(rec._list_project("p")[0])
        self.assertTrue(logged.called)


class CreatePassSignalTest(unittest.TestCase):
    """`create_pass_ran` is the only honest answer to "is the roster reconciled?".

    Everything in this script is caught and logged so the cron path always exits 0,
    which leaves the exit code saying nothing. The bootstrap scan gate has to know
    the difference: filing its sweep against a roster the CREATE pass never touched
    produces a solo audit that `.bootstrap_scan_filed` then makes permanent.
    """

    def _reconcile(self, clusters):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"HERMES_HOME": tmp}), \
             mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "_project_source", return_value=("p", True)), \
             mock.patch.object(rec, "_list_project", return_value=_listing(clusters)), \
             mock.patch.object(rec, "log"):
            return rec.reconcile(dry_run=True)

    def test_an_empty_project_still_counts_as_reconciled(self):
        self.assertTrue(self._reconcile([])["create_pass_ran"])

    def test_a_failed_cluster_list_does_not(self):
        self.assertFalse(self._reconcile(None)["create_pass_ran"])

    def test_an_unresolvable_project_does_not(self):
        with mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "_project_source", return_value=(None, True)), \
             mock.patch.object(rec, "log"):
            self.assertFalse(rec.reconcile(dry_run=True)["create_pass_ran"])

    def test_the_flag_turns_a_skipped_create_pass_into_an_exit_code(self):
        with mock.patch.object(rec, "reconcile", return_value={"create_pass_ran": False}), \
             mock.patch.object(rec.sys, "argv", ["x", "--require-create-pass"]), \
             mock.patch.object(rec, "log"):
            with self.assertRaises(SystemExit) as caught:
                rec.main()
        self.assertEqual(caught.exception.code, rec.EXIT_CREATE_PASS_SKIPPED)

    def test_without_the_flag_the_cron_path_still_exits_zero(self):
        with mock.patch.object(rec, "reconcile", return_value={"create_pass_ran": False}), \
             mock.patch.object(rec.sys, "argv", ["x"]), \
             mock.patch.object(rec, "_notify"), \
             mock.patch.object(rec, "log"):
            self.assertIsNone(rec.main())

    def test_a_failed_create_is_recorded_rather_than_only_logged(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(os.environ, {"HERMES_HOME": tmp}), \
             mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "_project_source", return_value=("p", True)), \
             mock.patch.object(rec, "_list_project", return_value=_listing([("p", "alpha", "us-central1")])), \
             mock.patch.object(rec, "create_profile", side_effect=SystemExit("no credentials")), \
             mock.patch.object(rec, "log"):
            report = rec.reconcile(dry_run=False)
        self.assertEqual(report["create_failed"], ["alpha/us-central1"])
        self.assertEqual(report["created"], [])

    def _main(self, report, argv=("x", "--require-create-pass")):
        """Run main() over a hand-built report; returns the exit code or None."""
        with mock.patch.object(rec, "reconcile", return_value=report), \
             mock.patch.object(rec.sys, "argv", list(argv)), \
             mock.patch.object(rec, "_notify"), \
             mock.patch.object(rec, "log"):
            try:
                rec.main()
            except SystemExit as e:
                return e.code
        return None

    def test_the_flag_fails_when_the_pass_ran_but_every_create_did(self):
        # The half-built home a failed create leaves behind is worse than no profile:
        # the gate would file its one-shot sweep and fan a card out to a profile with
        # no kubeconfig.
        code = self._main({"create_pass_ran": True, "created": [],
                           "create_failed": ["alpha/us-central1"]})
        self.assertEqual(code, rec.EXIT_CREATE_PASS_SKIPPED)

    def test_one_failed_create_among_several_still_reconciles(self):
        # Holding the whole fleet report back for one unscaffoldable cluster buys
        # nothing: the cause is often permanent, and the sweep records it as a gap.
        self.assertIsNone(self._main({"create_pass_ran": True, "created": ["cluster-beta"],
                                      "create_failed": ["alpha/us-central1"]}))

    def test_a_failure_against_an_already_populated_roster_still_reconciles(self):
        self.assertIsNone(self._main({"create_pass_ran": True, "created": [],
                                      "kept": ["cluster-beta"],
                                      "create_failed": ["alpha/us-central1"]}))

    def test_the_wreckage_of_a_failed_create_does_not_count_as_a_roster(self):
        # Tick 2 of the same permanent failure: PRUNE keeps the half-built home the
        # step-2b identity stamp left behind, so `kept` is non-empty while the roster
        # is no more usable than it was on tick 1.
        code = self._main({"create_pass_ran": True, "created": [],
                           "kept": ["cluster-alpha"], "incomplete": ["cluster-alpha"],
                           "create_failed": ["alpha/us-central1"]})
        self.assertEqual(code, rec.EXIT_CREATE_PASS_SKIPPED)

    def test_a_scaffolded_profile_alongside_a_broken_one_still_reconciles(self):
        self.assertIsNone(self._main({"create_pass_ran": True, "created": [],
                                      "kept": ["cluster-alpha", "cluster-beta"],
                                      "incomplete": ["cluster-alpha"],
                                      "create_failed": ["alpha/us-central1"]}))

    def test_the_cron_path_exits_zero_even_when_every_create_failed(self):
        # The hourly producer must never exit non-zero; only --require-create-pass
        # turns a bad roster into an exit code.
        self.assertIsNone(self._main({"create_pass_ran": True, "created": [],
                                      "create_failed": ["alpha/us-central1"]}, argv=("x",)))

    def test_a_failed_create_alone_does_not_post_to_chat(self):
        # It repeats every run until the cause is fixed, and during onboarding the gate
        # re-runs this script every minute.
        with mock.patch.object(rec, "reconcile",
                               return_value={"create_pass_ran": True, "created": [],
                                             "create_failed": ["alpha/us-central1"]}), \
             mock.patch.object(rec.sys, "argv", ["x"]), \
             mock.patch.object(rec, "_notify") as notify, \
             mock.patch.object(rec, "log"):
            rec.main()
        notify.assert_not_called()

    def test_a_failed_create_is_named_when_a_message_goes_out_anyway(self):
        with mock.patch.object(rec, "reconcile",
                               return_value={"create_pass_ran": True,
                                             "created": ["cluster-beta"],
                                             "create_failed": ["alpha/us-central1"]}), \
             mock.patch.object(rec.sys, "argv", ["x"]), \
             mock.patch.object(rec, "_notify") as notify, \
             mock.patch.object(rec, "log"):
            rec.main()
        self.assertIn("alpha/us-central1", notify.call_args[0][0])


class ExclusiveRunTest(unittest.TestCase):
    """Two schedules run this script, and the cron lock is per job id.

    The hourly `cluster-agent-reconcile` job and the bootstrap scan gate (every
    minute until the roster is usable) would otherwise call create_profile and
    delete_profile against the same profile homes concurrently.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(rec.os.environ, {"HERMES_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hold_the_lock(self):
        import fcntl
        handle = open(Path(self._tmp.name) / rec.RECONCILE_LOCK, "w")
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)

    def test_the_second_run_does_not_reconcile(self):
        self._hold_the_lock()
        with mock.patch.object(rec, "reconcile", side_effect=AssertionError("must not run")), \
             mock.patch.object(rec.sys, "argv", ["x"]), \
             mock.patch.object(rec, "log"):
            self.assertIsNone(rec.main())  # the cron producer still exits 0

    def test_the_gate_is_told_to_retry_rather_than_that_the_roster_failed(self):
        self._hold_the_lock()
        with mock.patch.object(rec, "reconcile", side_effect=AssertionError("must not run")), \
             mock.patch.object(rec.sys, "argv", ["x", "--require-create-pass"]), \
             mock.patch.object(rec, "log"):
            with self.assertRaises(SystemExit) as caught:
                rec.main()
        self.assertEqual(caught.exception.code, rec.EXIT_ALREADY_RUNNING)
        self.assertNotEqual(rec.EXIT_ALREADY_RUNNING, rec.EXIT_CREATE_PASS_SKIPPED)

    def test_an_uncontended_run_proceeds(self):
        with mock.patch.object(rec, "reconcile", return_value={"create_pass_ran": True}), \
             mock.patch.object(rec.sys, "argv", ["x", "--require-create-pass"]), \
             mock.patch.object(rec, "_notify"), \
             mock.patch.object(rec, "log"):
            self.assertIsNone(rec.main())


class MetadataTest(unittest.TestCase):
    def test_response_is_closed(self):
        # urlopen's response holds a socket; on a cron tick, leaking one per call
        # leaks one per tick. It must be context-managed.
        resp = mock.MagicMock()
        resp.read.return_value = b"  my-project\n"
        resp.__enter__.return_value = resp
        with mock.patch.object(rec.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(rec._metadata("project/project-id"), "my-project")
        resp.__exit__.assert_called_once()

    def test_unreachable_metadata_returns_none(self):
        with mock.patch.object(rec.urllib.request, "urlopen", side_effect=OSError("no route")):
            self.assertIsNone(rec._metadata("project/project-id"))


class ListProfilesReservedTest(unittest.TestCase):
    def test_reserved_profiles_excluded(self):
        base = Path(tempfile.mkdtemp()) / "profiles"
        for name in ("default", "platform", "cluster-a", "cluster-b"):
            (base / name).mkdir(parents=True)
        (base / "a-file").write_text("not a dir")
        with mock.patch.object(cap, "PROFILES_BASE", base):
            self.assertEqual(cap.list_profiles(), ["cluster-a", "cluster-b"])


class ClusterExistsTest(unittest.TestCase):
    def _stub(self, **kw):
        return mock.patch.object(rec.subprocess, "run", **kw)

    def test_exists_true_on_success(self):
        with self._stub(return_value=mock.Mock(stdout='{"status":"RUNNING"}')):
            self.assertIs(rec._cluster_exists("p", "c", "us-central1"), True)

    def test_false_on_notfound(self):
        err = subprocess.CalledProcessError(1, "gcloud", stderr="Error: (gcloud) ... NotFound: 404")
        with self._stub(side_effect=err):
            self.assertIs(rec._cluster_exists("p", "c", "us-central1"), False)

    def test_unknown_on_other_error(self):
        err = subprocess.CalledProcessError(1, "gcloud", stderr="PERMISSION_DENIED: caller lacks access")
        with self._stub(side_effect=err):
            self.assertIsNone(rec._cluster_exists("p", "c", "us-central1"))

    def test_unknown_on_timeout(self):
        with self._stub(side_effect=subprocess.TimeoutExpired("gcloud", 30)):
            self.assertIsNone(rec._cluster_exists("p", "c", "us-central1"))


class ReadClusterIdentityTest(unittest.TestCase):
    def _write(self, text):
        home = Path(tempfile.mkdtemp())
        (home / "config.yaml").write_text(text, encoding="utf-8")
        return home

    def test_valid(self):
        home = self._write(
            "cluster_identity:\n  project: proj\n  cluster: clus\n  location: us-central1\n"
        )
        self.assertEqual(
            cap.read_cluster_identity(home),
            {"project": "proj", "cluster": "clus", "location": "us-central1"},
        )

    def test_missing_file(self):
        self.assertIsNone(cap.read_cluster_identity(Path(tempfile.mkdtemp())))

    def test_missing_block(self):
        self.assertIsNone(cap.read_cluster_identity(self._write("model:\n  provider: custom\n")))

    def test_incomplete_block(self):
        self.assertIsNone(
            cap.read_cluster_identity(self._write("cluster_identity:\n  project: proj\n  cluster: clus\n"))
        )


class NotificationGatingTest(unittest.TestCase):
    def _main_with(self, report, argv):
        with mock.patch.object(rec, "reconcile", return_value=report), \
             mock.patch.object(rec, "_notify") as notify, \
             mock.patch.object(sys, "argv", argv):
            rec.main()
        return notify

    def _report(self, pruned=None, skipped_error=None):
        return {
            "pruned": pruned or [],
            "kept": [],
            "skipped_no_identity": [],
            "skipped_error": skipped_error or [],
        }

    def test_notifies_when_pruned(self):
        notify = self._main_with(self._report(pruned=["cluster-a"]), ["prog"])
        notify.assert_called_once()

    def test_silent_when_nothing_pruned(self):
        # Inconclusive errors alone must not spam chat every hour.
        notify = self._main_with(self._report(skipped_error=["cluster-a"]), ["prog"])
        notify.assert_not_called()

    def test_dry_run_never_notifies(self):
        notify = self._main_with(self._report(pruned=["cluster-a"]), ["prog", "--dry-run"])
        notify.assert_not_called()


class NotifyTargetTest(unittest.TestCase):
    """Where _notify sends. Regression cover for #989, where it sent to the
    literal `google_chat` and a Slack-only install heard nothing at all."""

    def _sends(self, platforms, run=None):
        """Run _notify with a stubbed resolver; return the `--to` value of each send."""
        with mock.patch.object(rec, "enabled_chat_platforms", return_value=platforms), \
             mock.patch.object(rec.subprocess, "run", side_effect=run) as sub:
            rec._notify("hello")
        return [call.args[0][3] for call in sub.call_args_list]

    def test_posts_to_every_resolved_platform(self):
        self.assertEqual(self._sends(["google_chat", "slack"]), ["google_chat", "slack"])

    def test_slack_only_install_gets_the_summary(self):
        # The reported bug: this used to send to google_chat regardless, and the
        # failure was swallowed into a log line on a run that still exits 0.
        self.assertEqual(self._sends(["slack"]), ["slack"])

    def test_one_platform_failing_does_not_cost_the_other_the_summary(self):
        def run(cmd, **kwargs):
            if cmd[3] == "google_chat":
                raise subprocess.CalledProcessError(1, cmd, stderr="no home channel")
            return mock.DEFAULT

        self.assertEqual(self._sends(["google_chat", "slack"], run=run),
                         ["google_chat", "slack"])

    def test_the_message_is_the_same_on_every_platform(self):
        # Asserts the whole argv of every call, not the set of messages: a set is
        # equally satisfied by one send, so the set form passed against the
        # pre-#989 `--to google_chat` and pinned nothing this class exists to pin.
        with mock.patch.object(rec, "enabled_chat_platforms", return_value=["google_chat", "slack"]), \
             mock.patch.object(rec.subprocess, "run") as sub:
            rec._notify("hello")
        self.assertEqual([call.args[0] for call in sub.call_args_list],
                         [[rec.HERMES_BIN, "send", "--to", "google_chat", "hello"],
                          [rec.HERMES_BIN, "send", "--to", "slack", "hello"]])

    def test_a_slack_only_environment_reaches_slack_through_the_real_resolver(self):
        # Every other case here stubs enabled_chat_platforms, so none of them would
        # notice the two halves being wired together wrongly. This one drives the
        # real resolver off the environment the operator renders for a Slack-only
        # install, which is the configuration #989 was reported against.
        #
        # Both path constants are pinned away, not just CONFIG_PATH. They are computed
        # at import time, so `clear=True` does not move them, and the managed scope is
        # consulted first and wins outright — on a machine that has an /etc/hermes
        # (the agent pod itself) this assertion would be decided by that host's CR
        # rather than by the fixture below.
        with mock.patch.multiple(chat_platforms,
                                 CONFIG_PATH="/nonexistent/config.yaml",
                                 MANAGED_CONFIG_PATH="/nonexistent/managed.yaml"), \
             mock.patch.dict(os.environ, {"SLACK_RELAY_URL": "http://127.0.0.1:8780"}, clear=True), \
             mock.patch.object(rec.subprocess, "run") as sub:
            rec._notify("created 1 profile(s): demo")
        self.assertEqual([call.args[0] for call in sub.call_args_list],
                         [[rec.HERMES_BIN, "send", "--to", "slack", "created 1 profile(s): demo"]])


class FormatNotificationTest(unittest.TestCase):
    def test_lists_pruned_and_errors(self):
        msg = rec._format_notification(
            {"pruned": ["cluster-a", "cluster-b"], "skipped_error": ["cluster-c"],
             "kept": [], "skipped_no_identity": []}
        )
        self.assertIn("pruned 2", msg)
        self.assertIn("cluster-a", msg)
        self.assertIn("cluster-b", msg)
        self.assertIn("cluster-c", msg)  # unverified profiles surfaced too


class ScopeTest(HomesMixin):
    """spec.scope, phase 1: explicit projects, exclusions, outcomes, the prune rules, the snapshot."""

    MGMT = "mgmt-proj"

    def _write_scope(self, scope: dict | None):
        if scope is None:
            return
        path = Path(self._tmp.name) / "scope.json"
        # The operator writes `present`, `folders` and `organizations` on every render; a test
        # that passes a dict without them is declaring a block the way the operator would for
        # a CR that carries one. A test modelling an older render writes the file itself.
        if isinstance(scope, dict):
            scope = {rec.SCOPE_PRESENT_KEY: True, "folders": [], "organizations": [], **scope}
        path.write_text(json.dumps(scope), encoding="utf-8")
        os.environ[rec.SCOPE_FILE_ENV] = str(path)

    def _write_previous(self, projects: list[dict], containers: list[dict] | None = None, declared: dict | None = None):
        # No `containers` key and no `declared` stands for a snapshot from before folders, or
        # one written with none declared: every container declared next run then reads as
        # newly declared. `declared` is what the last run read (or carried forward unread).
        snapshot = {"projects": projects}
        if containers is not None:
            snapshot["containers"] = containers
        if declared is not None:
            snapshot["declared"] = declared
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(snapshot), encoding="utf-8")

    def _snapshot(self) -> dict:
        return json.loads((Path(self._tmp.name) / rec.SNAPSHOT_FILE).read_text(encoding="utf-8"))

    def _run(self, scope, listings, profiles=None, identities=None, exists=True,
             management=MGMT, extra_exclude=frozenset(), delete_removes=True, dry_run=False,
             authoritative=True, searches=None, create_raises=None):
        """listings: project -> (clusters list | None, outcome) or a list (ok); or a callable
        used as the lister itself.

        delete_removes: whether the stubbed delete also removes the profile home,
        as the real one does; False models a delete that failed.
        searches: container -> (members dict | None, outcome) for the Asset Inventory stub,
        or a callable used as the searcher itself. create_raises: an exception every create
        raises, modelling get-credentials failing.
        """
        self._write_scope(scope)
        profiles = profiles or []
        identities = identities or {}
        created: list = []
        deleted: list = []

        def delete(name):
            deleted.append(name)
            if delete_removes:
                shutil.rmtree(self.homes / name, ignore_errors=True)

        def create(pr, c, l):
            if callable(create_raises):
                create_raises(pr, c, l)  # raises for the clusters it wants to fail
            elif create_raises is not None:
                raise create_raises
            created.append((pr, c, l))
            return f"cluster-{c}"

        # A test that patched create_profile itself keeps its own stub; the harness only
        # supplies one when the caller did not.
        create_stub = rec.create_profile if isinstance(rec.create_profile, mock.Mock) else create

        def list_project(project, timeout=None):
            value = listings.get(project, ([], rec.OUTCOME_OK))
            return value if isinstance(value, tuple) else (value, rec.OUTCOME_OK)

        with mock.patch.object(rec, "_project_source", return_value=(management, authoritative)), \
             mock.patch.object(rec, "EXTRA_EXCLUDE", set(extra_exclude)), \
             mock.patch.object(rec, "_list_project", side_effect=listings if callable(listings) else list_project), \
             mock.patch.object(rec, "_search_container",
                               side_effect=searches if callable(searches) else
                               (lambda c, timeout=None: (searches or {}).get(c, (None, rec.OUTCOME_UNREACHABLE)))), \
             mock.patch.object(rec, "list_profiles", return_value=list(profiles)), \
             mock.patch.object(rec, "profile_home", side_effect=_home_factory(self.homes)), \
             mock.patch.object(rec, "read_cluster_identity",
                               side_effect=lambda home: identities.get(home.name)), \
             mock.patch.object(rec, "_cluster_exists", **({"side_effect": exists} if callable(exists) else {"return_value": exists})), \
             mock.patch.object(rec, "delete_profile", side_effect=delete), \
             mock.patch.object(rec, "create_profile", side_effect=create_stub):
            report = rec.reconcile(dry_run=dry_run)
        return report, created, deleted

    def test_no_scope_file_is_the_management_project_alone(self):
        report, created, _ = self._run(None, {self.MGMT: [(self.MGMT, "a", "us-central1")],
                                              "other": [("other", "x", "us-central1")]})
        self.assertEqual(created, [(self.MGMT, "a", "us-central1")])
        snap = self._snapshot()
        self.assertEqual([p["id"] for p in snap["projects"]], [self.MGMT])
        self.assertEqual(snap["resolver"], rec.RESOLVER_EXPLICIT)
        self.assertEqual(report["projects"], {self.MGMT: rec.OUTCOME_OK})

    def test_explicit_projects_are_listed_after_the_management_project_in_sorted_order(self):
        scope = {"projects": ["zeta", "alpha"]}
        report, created, _ = self._run(scope, {
            self.MGMT: [(self.MGMT, "m", "us-central1")],
            "alpha": [("alpha", "a", "us-central1")],
            "zeta": [("zeta", "z", "europe-west1")],
        })
        self.assertEqual(created, [(self.MGMT, "m", "us-central1"), ("alpha", "a", "us-central1"),
                                   ("zeta", "z", "europe-west1")])
        snap = self._snapshot()
        # Listed in fill order (management first), written sorted by ID.
        self.assertEqual([(p["id"], p["via"], p["outcome"], p["clusters"]) for p in snap["projects"]],
                         [("alpha", ["explicit"], "ok", 1), (self.MGMT, ["management"], "ok", 1),
                          ("zeta", ["explicit"], "ok", 1)])
        self.assertTrue(report["create_pass_ran"])

    def test_exclude_projects_takes_ids_and_globs_and_never_the_management_project(self):
        scope = {"projects": ["team-sandbox", "team-prod", "legacy"],
                 "exclude": {"projects": ["*-sandbox", "legacy", "mgmt-*"]}}
        report, created, _ = self._run(scope, {
            self.MGMT: [(self.MGMT, "m", "us-central1")],
            "team-prod": [("team-prod", "p", "us-central1")],
            "team-sandbox": [("team-sandbox", "s", "us-central1")],
            "legacy": [("legacy", "l", "us-central1")],
        })
        self.assertEqual(created, [(self.MGMT, "m", "us-central1"), ("team-prod", "p", "us-central1")])
        snap = self._snapshot()
        self.assertEqual([p["id"] for p in snap["projects"]], sorted([self.MGMT, "team-prod"]))
        self.assertEqual(snap["ignoredExcludes"], [{"project": self.MGMT, "pattern": "mgmt-*"}])

    def test_exclude_clusters_by_triple_skips_create_and_prunes_the_existing_profile(self):
        scope = {"projects": ["other"],
                 "exclude": {"clusters": [{"projectId": "other", "location": "us-central1", "clusterName": "scratch"},
                                          {"projectId": self.MGMT, "location": "us-central1", "clusterName": "old"}]}}
        report, created, deleted = self._run(
            scope,
            {self.MGMT: [(self.MGMT, "old", "us-central1")],
             "other": [("other", "scratch", "us-central1"), ("other", "keep", "us-central1")]},
            profiles=["cluster-old"],
            identities={"cluster-old": _identity(self.MGMT, "old")},
        )
        self.assertEqual(created, [("other", "keep", "us-central1")])
        self.assertEqual(deleted, ["cluster-old"])
        self.assertEqual(report["pruned"], ["cluster-old"])

    def test_a_denied_project_keeps_its_profiles_and_is_reported(self):
        scope = {"projects": ["locked"]}
        report, created, deleted = self._run(
            scope,
            {self.MGMT: [], "locked": (None, rec.OUTCOME_DENIED)},
            profiles=["cluster-l"], identities={"cluster-l": _identity("locked", "l")},
        )
        self.assertEqual(created, [])
        self.assertEqual(deleted, [])
        self.assertEqual(report["projects"], {self.MGMT: "ok", "locked": "denied"})
        self.assertEqual(report["kept"], ["cluster-l"])
        self.assertTrue(report["create_pass_ran"])  # the management project listed fine
        snap = self._snapshot()
        locked = next(p for p in snap["projects"] if p["id"] == "locked")
        self.assertEqual((locked["outcome"], locked["clusters"]), ("denied", None))
        self.assertIn("`locked` (denied)", rec._format_notification(report))

    def test_list_failures_are_classified(self):
        self.assertEqual(rec._classify_list_failure("ResponseError: code=403, message=Permission denied"),
                         rec.OUTCOME_DENIED)
        self.assertEqual(rec._classify_list_failure("Kubernetes Engine API has not been used in project x"),
                         rec.OUTCOME_API_DISABLED)
        self.assertEqual(rec._classify_list_failure("SERVICE_DISABLED"), rec.OUTCOME_API_DISABLED)
        self.assertEqual(rec._classify_list_failure("Unable to connect"), rec.OUTCOME_UNREACHABLE)

    def test_a_project_dropped_from_the_scope_is_pruned_only_under_all_three_conditions_and_two_runs(self):
        gone_profile = {"cluster-g": _identity("gone", "g")}
        # (1) absent from the resolved set, (2) lookups clean, (3) in the previous snapshot -> retiring
        # on the first clean run, pruned on the next.
        self._write_previous([{"id": "gone", "state": rec.STATE_IN_SCOPE}])
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-g"], identities=gone_profile)
        self.assertEqual(deleted, [])
        self.assertEqual(report["retiring"], ["gone"])
        retiring = next(p for p in self._snapshot()["projects"] if p["id"] == "gone")
        self.assertEqual((retiring["state"], retiring["clusters"]), (rec.STATE_RETIRING, 1))
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-g"], identities=gone_profile)
        self.assertEqual(deleted, ["cluster-g"])
        self.assertEqual([p for p in self._snapshot()["projects"] if p["id"] == "gone"], [])

    def test_a_profile_the_scope_never_produced_is_kept_and_listed_unmanaged(self):
        # Condition (3) fails: no previous snapshot names the project.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-h"], identities={"cluster-h": _identity("hand", "h")})
        self.assertEqual(deleted, [])
        self.assertEqual(report["unmanaged"], ["cluster-h"])
        self.assertEqual(report["kept"], ["cluster-h"])
        self.assertEqual(self._snapshot()["unmanaged"],
                         [{"profile": "cluster-h", "project": "hand", "reason": "never in scope"}])

    def test_an_unreachable_lookup_blocks_the_scope_prune(self):
        # Condition (2) fails: one listed project was unreachable, so absence proves nothing,
        # and a project already retiring is not pruned.
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        report, _, deleted = self._run({"projects": ["flaky"]},
                                       {self.MGMT: [], "flaky": (None, rec.OUTCOME_UNREACHABLE)},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual(deleted, [])
        self.assertEqual(report["unmanaged"], ["cluster-g"])
        self.assertEqual(self._snapshot()["unmanaged"][0]["reason"], "retiring; waiting for a clean run")
        self.assertEqual(report["retiring"], ["gone"])

    def test_a_management_project_that_cannot_list_its_own_clusters_is_not_a_clean_run(self):
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        report, _, deleted = self._run({"projects": []}, {self.MGMT: (None, rec.OUTCOME_DENIED)},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual((deleted, report["retiring"]), ([], ["gone"]))

    def test_a_changed_management_project_that_cannot_be_listed_retires_nothing(self):
        # RECONCILE_PROJECT set to a project the agent cannot read: two such ticks must not
        # read as the real management project confirmed gone.
        mgmt_profiles = {"cluster-m1": _identity(self.MGMT, "m1")}
        self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                  profiles=["cluster-m1"], identities=mgmt_profiles)
        for _ in range(2):
            report, _, deleted = self._run({"projects": []}, {"typo": (None, rec.OUTCOME_DENIED)}, management="typo",
                                           profiles=["cluster-m1"], identities=mgmt_profiles)
            self.assertEqual((deleted, report["retiring"]), ([], []))
            self.assertEqual(report["unmanaged"], ["cluster-m1"])
            rows = {p["id"]: p["state"] for p in self._snapshot()["projects"]}
            self.assertEqual(rows[self.MGMT], rec.STATE_IN_SCOPE)
        # The typo fixed: the management project lists again, nothing lost.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                                       profiles=["cluster-m1"], identities=mgmt_profiles)
        self.assertEqual((deleted, report["kept"]), ([], ["cluster-m1"]))

    def test_a_retiring_project_stays_while_a_profile_of_unknown_identity_is_on_the_volume(self):
        # The last snapshot attributed cluster-g to gone; this tick its identity is unreadable.
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": "gone", "state": rec.STATE_RETIRING}], "profiles": {"cluster-g": "gone"}}),
            encoding="utf-8")
        # Two unreadable ticks in a row: the attribution is carried forward, not lost.
        for _ in range(2):
            report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                           profiles=["cluster-g"], identities={"cluster-g": None})
            self.assertEqual((deleted, report["skipped_no_identity"], report["retiring"]), ([], ["cluster-g"], ["gone"]))
            self.assertEqual(self._snapshot()["profiles"], {"cluster-g": "gone"})
        # Identity readable again on the next clean run: pruned, not "never in scope".
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual(deleted, ["cluster-g"])

    def test_a_project_dropped_while_its_only_profile_is_unreadable_still_retires(self):
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": "p", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}], "profiles": {"cluster-p": "p"}}),
            encoding="utf-8")
        # The drop tick is clean but cluster-p's identity cannot be read: retiring by attribution.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-p"], identities={"cluster-p": None})
        self.assertEqual((deleted, report["retiring"]), ([], ["p"]))
        rows = {q["id"]: (q["state"], q["clusters"]) for q in self._snapshot()["projects"]}
        self.assertEqual(rows["p"], (rec.STATE_RETIRING, 1))
        # Readable again on the next clean run: pruned, as the two-run rule promises.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-p"], identities={"cluster-p": _identity("p", "p1")})
        self.assertEqual(deleted, ["cluster-p"])

    def test_a_project_dropped_while_its_only_profile_is_unreadable_on_an_unclean_tick_is_carried(self):
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": "p", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}], "profiles": {"cluster-p": "p"}}),
            encoding="utf-8")
        report, _, deleted = self._run({"projects": ["flaky"]}, {self.MGMT: [], "flaky": (None, rec.OUTCOME_UNREACHABLE)},
                                       profiles=["cluster-p"], identities={"cluster-p": None})
        self.assertEqual((deleted, report["retiring"]), ([], []))
        rows = {q["id"]: q["state"] for q in self._snapshot()["projects"]}
        self.assertEqual(rows["p"], rec.STATE_IN_SCOPE)

    def test_a_changed_management_projects_unreadable_profile_retires_on_the_change_tick(self):
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE}],
             "profiles": {"cluster-m1": self.MGMT}}), encoding="utf-8")
        report, _, deleted = self._run({"projects": []}, {"new": []}, management="new",
                                       profiles=["cluster-m1"], identities={"cluster-m1": None})
        self.assertEqual((deleted, report["retiring"]), ([], [self.MGMT]))
        rows = {q["id"]: q["state"] for q in self._snapshot()["projects"]}
        self.assertEqual(rows[self.MGMT], rec.STATE_RETIRING)

    def test_a_pruned_profile_whose_home_survives_a_failed_delete_stays_attributed(self):
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")},
                                       delete_removes=False)
        self.assertEqual((deleted, report["retiring"]), (["cluster-g"], ["gone"]))
        self.assertEqual(self._snapshot()["profiles"], {"cluster-g": "gone"})

    def test_an_unreadable_declaration_keeps_the_last_declarations_exclusions(self):
        # Rollback to an operator without the field: the file is gone, but the cluster the
        # operator excluded must not be re-onboarded, and the snapshot keeps naming that
        # declaration so the next unreadable tick reads the same exclusions.
        declared = {"projects": ["p2"], "exclude": {"projects": ["*-scratch"], "clusters": [
            {"projectId": self.MGMT, "location": "us-central1", "clusterName": "kept-out"}]}}
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE}], "declared": declared}),
            encoding="utf-8")
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        for _ in range(2):
            report, created, deleted = self._run(None, {self.MGMT: [(self.MGMT, "kept-out", "us-central1"), (self.MGMT, "m1", "us-central1")]})
            self.assertEqual(created, [(self.MGMT, "m1", "us-central1")])
            self.assertEqual(deleted, [])
            self.assertEqual(self._snapshot()["declared"], rec._normalize_scope(declared))
            self.assertEqual([p["id"] for p in self._snapshot()["projects"]], [self.MGMT])

    def test_a_declaration_with_scalar_fields_is_read_as_absent_fields(self):
        # A hand-edited file or snapshot must not abort the run or come apart into characters.
        for parsed in ({"projects": 5}, {"projects": "abc"}, {"exclude": {"clusters": 7}}, {"exclude": {"projects": True}}, {"exclude": 3}):
            self.assertEqual(rec._normalize_scope(parsed), rec._empty_scope(), parsed)
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE}], "declared": {"projects": 5}}),
            encoding="utf-8")
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        report, created, _ = self._run(None, {self.MGMT: [(self.MGMT, "m1", "us-central1")]})
        self.assertEqual(created, [(self.MGMT, "m1", "us-central1")])
        self._write_scope({"projects": "abc", "exclude": {"clusters": 7}})
        report, created, _ = self._run(None, {self.MGMT: [(self.MGMT, "m1", "us-central1")]})
        self.assertEqual(report["projects"], {self.MGMT: rec.OUTCOME_OK})

    def test_a_cr_without_a_scope_block_retires_nothing_and_keeps_the_last_exclusions(self):
        # The operator renders present=false when the CR has no scope block, which is what a
        # write through an older webhook leaves behind: the previously explicit project is
        # carried in scope, its profiles kept, and the last declaration's exclusions still hold.
        declared = {"projects": ["p2"], "exclude": {"projects": [], "clusters": [
            {"projectId": self.MGMT, "location": "us-central1", "clusterName": "kept-out"}]}}
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                          {"id": "p2", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}],
             "declared": declared, "profiles": {"cluster-p2": "p2"}}), encoding="utf-8")
        ids = {"cluster-p2": _identity("p2", "x")}
        for _ in range(3):
            report, created, deleted = self._run({rec.SCOPE_PRESENT_KEY: False, "projects": [], "exclude": {"projects": [], "clusters": []}},
                                                 {self.MGMT: [(self.MGMT, "kept-out", "us-central1")]},
                                                 profiles=["cluster-p2"], identities=ids)
            self.assertEqual((created, deleted, report["retiring"]), ([], [], []))
            rows = {p["id"]: p["state"] for p in self._snapshot()["projects"]}
            self.assertEqual(rows["p2"], rec.STATE_IN_SCOPE)
            self.assertEqual(self._snapshot()["declared"], rec._normalize_scope(declared))
            self.assertEqual(self._snapshot()["unmanaged"][0]["reason"], "no scope block declared this run; carried forward")
        # A present block with an empty projects list is the declaration that drops p2.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-p2"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), ([], ["p2"]))
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-p2"], identities=ids)
        self.assertEqual(deleted, ["cluster-p2"])

    def test_a_cr_without_a_scope_block_still_has_clean_runs_for_what_an_earlier_block_marked(self):
        # A project an earlier present block marked retiring, and a management project that changes
        # identity, are still pruned on a no-block install: the mark came from a real declaration and
        # the identity change from the metadata server, neither from the absence of the block.
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                          {"id": "gone", "via": [], "state": rec.STATE_RETIRING}],
             "declared": {"projects": [], "exclude": {"projects": [], "clusters": []}}}), encoding="utf-8")
        absent = {rec.SCOPE_PRESENT_KEY: False, "projects": [], "exclude": {"projects": [], "clusters": []}}
        ids = {"cluster-g": _identity("gone", "g"), "cluster-m1": _identity(self.MGMT, "m1")}
        report, _, deleted = self._run(absent, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                                       profiles=["cluster-g", "cluster-m1"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), (["cluster-g"], []))
        # The management project changes (metadata names another): retiring on the change tick,
        # pruned on the next clean run, block or no block.
        report, _, deleted = self._run(absent, {"new-mgmt": []}, management="new-mgmt",
                                       profiles=["cluster-m1"], identities={"cluster-m1": _identity(self.MGMT, "m1")})
        self.assertEqual((deleted, report["retiring"]), ([], [self.MGMT]))
        report, _, deleted = self._run(absent, {"new-mgmt": []}, management="new-mgmt",
                                       profiles=["cluster-m1"], identities={"cluster-m1": _identity(self.MGMT, "m1")})
        self.assertEqual(deleted, ["cluster-m1"])

    def test_an_empty_reconcile_project_reads_as_unset(self):
        # The operator pins RECONCILE_PROJECT empty; the empty value must not become the project.
        with mock.patch.dict(os.environ, {"RECONCILE_PROJECT": ""}), \
             mock.patch.object(rec, "_metadata", return_value="from-metadata"):
            self.assertEqual(rec._project_source(), ("from-metadata", True))
        with mock.patch.dict(os.environ, {"RECONCILE_PROJECT": "override"}), \
             mock.patch.object(rec, "_metadata", return_value="from-metadata"):
            self.assertEqual(rec._project_source(), ("override", True))

    def test_an_unreadable_declaration_with_no_previous_snapshot_excludes_nothing(self):
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        report, created, _ = self._run(None, {self.MGMT: [(self.MGMT, "m1", "us-central1")]})
        self.assertEqual(created, [(self.MGMT, "m1", "us-central1")])
        self.assertEqual(self._snapshot()["declared"], rec._empty_scope())

    def test_projects_are_listed_concurrently_and_created_in_the_fixed_order(self):
        import threading, time
        delay = 0.4
        seen: list[str] = []
        lock = threading.Lock()

        def slow_list(project, timeout=None):
            with lock:
                seen.append(project)
            time.sleep(delay)
            return [(project, "c", "us-central1")], rec.OUTCOME_OK
        start = time.monotonic()
        report, created, _ = self._run({"projects": ["b", "a", "c"]}, slow_list)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, delay * 4 * 0.7, f"listings ran sequentially ({elapsed:.2f}s)")
        self.assertEqual(sorted(seen), ["a", "b", "c", self.MGMT])
        self.assertEqual([c[0] for c in created], [self.MGMT, "a", "b", "c"])

    def test_an_unmanaged_profile_whose_cluster_is_gone_is_pruned_and_not_listed_as_unmanaged(self):
        # exists=False on a profile the scope never produced: the orphan prune removes it, and a
        # pruned profile must not be advertised in the snapshot's unmanaged list.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-h"],
                                       identities={"cluster-h": _identity("hand", "h")}, exists=False)
        self.assertEqual((deleted, report["pruned"], report["unmanaged"]), (["cluster-h"], ["cluster-h"], []))
        self.assertEqual(self._snapshot()["unmanaged"], [])
        # Inconclusive lookups keep it and list it, as before.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-h2"],
                                       identities={"cluster-h2": _identity("hand", "h2")}, exists=None)
        self.assertEqual((deleted, report["skipped_error"], report["unmanaged"]), ([], ["cluster-h2"], ["cluster-h2"]))
        self.assertEqual([u["profile"] for u in self._snapshot()["unmanaged"]], ["cluster-h2"])

    def test_the_management_project_is_listed_first_and_alone(self):
        import threading, time
        delay = 0.2
        spans: dict[str, tuple[float, float]] = {}
        lock = threading.Lock()

        def lister(project, timeout=None):
            start = time.monotonic(); time.sleep(delay); end = time.monotonic()
            with lock:
                spans[project] = (start, end)
            return [], rec.OUTCOME_OK
        self._run({"projects": ["a", "b"]}, lister)
        self.assertEqual(sorted(spans), ["a", "b", self.MGMT])
        self.assertLessEqual(spans[self.MGMT][1], min(spans["a"][0], spans["b"][0]))

    def test_a_listing_still_running_at_the_budget_reads_unreachable_and_the_run_goes_on(self):
        import time
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])

        import threading
        cuts: dict[str, float] = {}

        def lister(project, timeout=None):
            cuts[project] = timeout
            if project == "slow":
                # Honours the timeout the way gcloud's would: sleeps no longer than it.
                time.sleep(min(1.5, timeout if timeout is not None else 1.5))
            return [], rec.OUTCOME_OK
        baseline = threading.active_count()
        start = time.monotonic()
        with mock.patch.object(rec, "LIST_BUDGET_SECONDS", 0.3), mock.patch.object(rec, "LIST_GRACE_SECONDS", 0.05):
            report, _, deleted = self._run({"projects": ["slow", "quick"]}, lister,
                                           profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertLess(time.monotonic() - start, 1.2)
        self.assertEqual(report["projects"], {self.MGMT: rec.OUTCOME_OK, "quick": rec.OUTCOME_OK, "slow": rec.OUTCOME_UNREACHABLE})
        # Unreachable switches the scope prune off; the snapshot is still written.
        self.assertEqual((deleted, report["retiring"]), ([], ["gone"]))
        self.assertEqual({p["id"]: p["outcome"] for p in self._snapshot()["projects"]}["slow"], rec.OUTCOME_UNREACHABLE)
        # The worker's own timeout was cut to the budget left, so no thread outlives the run
        # by more than the grace: the interpreter joins the pool's threads at exit.
        self.assertLessEqual(cuts["slow"], 1.0)
        time.sleep(1.2)
        self.assertEqual(threading.active_count(), baseline)

    # ---- phase 2: folders and organisations through Cloud Asset Inventory ----

    FOLDER = "folders/123456789012"

    def _asset(self, project, cluster, location, zonal=False):
        kind = "zones" if zonal else "locations"
        return {"name": f"//container.googleapis.com/projects/{project}/{kind}/{location}/clusters/{cluster}",
                "location": location, "project": "projects/757207957170"}

    def test_asset_names_parse_in_both_shapes_and_key_on_the_project_id(self):
        regional = self._asset("team-a", "prod", "us-central1")
        zonal = self._asset("team-a", "dev", "us-central1-a", zonal=True)
        self.assertEqual(rec._parse_asset(regional), ("team-a", "prod", "us-central1"))
        self.assertEqual(rec._parse_asset(zonal), ("team-a", "dev", "us-central1-a"))
        self.assertIsNone(rec._parse_asset({"name": "//compute.googleapis.com/projects/x/zones/z/instances/i"}))
        self.assertIsNone(rec._parse_asset("not a dict"))

    def test_a_folder_resolves_its_members_with_their_clusters_and_via(self):
        members = {"team-a": [("team-a", "prod", "us-central1")], "team-b": [("team-b", "dev", "us-central1-a")]}
        report, created, _ = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                       searches={self.FOLDER: (members, rec.OUTCOME_OK)})
        self.assertEqual(sorted(created), [("team-a", "prod", "us-central1"), ("team-b", "dev", "us-central1-a")])
        snap = self._snapshot()
        self.assertEqual(snap["resolver"], rec.RESOLVER_ASSET_INVENTORY)
        self.assertEqual(snap["containers"], [{"id": self.FOLDER, "outcome": rec.OUTCOME_OK, "projects": 2}])
        rows = {p["id"]: p for p in snap["projects"]}
        self.assertEqual((rows["team-a"]["via"], rows["team-a"]["outcome"], rows["team-a"]["clusters"]), ([self.FOLDER], rec.OUTCOME_OK, 1))
        self.assertEqual(report["projects"], {self.MGMT: rec.OUTCOME_OK, "team-a": rec.OUTCOME_OK, "team-b": rec.OUTCOME_OK})

    def test_a_member_that_is_also_explicit_or_the_management_project_keeps_both_vias(self):
        members = {self.MGMT: [(self.MGMT, "m1", "us-central1")], "team-a": [("team-a", "prod", "us-central1")]}
        report, created, _ = self._run({"projects": ["team-a"], "folders": ["123456789012"]},
                                       {self.MGMT: [(self.MGMT, "m1", "us-central1")], "team-a": [("team-a", "prod", "us-central1")]},
                                       searches={self.FOLDER: (members, rec.OUTCOME_OK)})
        rows = {p["id"]: p["via"] for p in self._snapshot()["projects"]}
        self.assertEqual(rows[self.MGMT], [self.FOLDER, rec.VIA_MANAGEMENT])
        self.assertEqual(rows["team-a"], [rec.VIA_EXPLICIT, self.FOLDER])
        self.assertEqual(sorted(created), [(self.MGMT, "m1", "us-central1"), ("team-a", "prod", "us-central1")])

    def test_an_excluded_member_is_dropped_after_the_folder_resolved(self):
        members = {"team-scratch": [("team-scratch", "x", "us-central1")], "team-a": [("team-a", "prod", "us-central1")]}
        report, created, _ = self._run({"folders": ["123456789012"], "exclude": {"projects": ["*-scratch"]}}, {self.MGMT: []},
                                       searches={self.FOLDER: (members, rec.OUTCOME_OK)})
        self.assertEqual(created, [("team-a", "prod", "us-central1")])
        self.assertNotIn("team-scratch", {p["id"] for p in self._snapshot()["projects"]})

    def test_an_unreadable_tick_does_not_make_the_folder_newly_declared_on_the_next(self):
        # Tick N-1 could not read the declaration: it resolved no container (`containers: []`)
        # and carried `declared` forward. Tick N reads a declaration that has dropped the
        # explicit project p2. The folder is not new, so p2 is the declaration's to retire,
        # not held for a day under the index's reason.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE},
                              {"id": "p2", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}],
                             containers=[],
                             declared={"projects": ["p2"], "folders": ["123456789012"], "organizations": [],
                                       "exclude": {"projects": [], "clusters": []}})
        ids = {"cluster-a": _identity("team-a", "prod"), "cluster-p2": _identity("p2", "x")}
        report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                       profiles=["cluster-a", "cluster-p2"], identities=ids,
                                       searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual(deleted, [])
        self.assertEqual(report["retiring"], ["p2"])
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertNotIn(rec.ABSENT_SINCE_KEY, rows["p2"])

    def test_a_folder_that_cannot_be_read_freezes_its_previous_members_and_the_prune(self):
        # Last run: the folder resolved team-a; an explicit project p2 was in scope too. The
        # folder is in the previous snapshot, so p2's hold below is the freeze's and not the
        # one-edit migration rule's (a container declared this run for the first time).
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE},
                              {"id": "p2", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}],
                             containers=[{"id": self.FOLDER, "outcome": rec.OUTCOME_OK, "projects": 1}])
        ids = {"cluster-a": _identity("team-a", "prod"), "cluster-p2": _identity("p2", "x")}
        # This run: the folder is denied and p2 was dropped from the declaration.
        report, created, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                             profiles=["cluster-a", "cluster-p2"], identities=ids,
                                             searches={self.FOLDER: (None, rec.OUTCOME_DENIED)})
        self.assertEqual((created, deleted, report["retiring"]), ([], [], []))
        snap = self._snapshot()
        self.assertEqual(snap["containers"], [{"id": self.FOLDER, "outcome": rec.OUTCOME_DENIED, "projects": 1}])
        rows = {p["id"]: p for p in snap["projects"]}
        self.assertEqual((rows["team-a"]["outcome"], rows["team-a"]["state"], rows["team-a"]["via"]), (rec.OUTCOME_DENIED, rec.STATE_IN_SCOPE, [self.FOLDER]))
        # p2 is carried, not retired: the frozen container holds back the prune.
        self.assertEqual(rows["p2"]["state"], rec.STATE_IN_SCOPE)
        self.assertNotIn(rec.ABSENT_SINCE_KEY, rows["p2"])
        self.assertIn("cluster-p2", report["unmanaged"])
        # The same edit with the folder readable retires p2, so the hold above was the freeze's.
        report, _, _ = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                 profiles=["cluster-a", "cluster-p2"], identities=ids,
                                 searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual(report["retiring"], ["p2"])

    def test_an_over_cap_folder_carries_its_members_without_holding_back_the_prune(self):
        # The folder was declared before this run (the previous snapshot carries it), so the
        # dropped explicit project is the declaration's to retire, not a same-edit migration.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE},
                              {"id": "p2", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}],
                             containers=[{"id": self.FOLDER, "outcome": rec.OUTCOME_OK, "projects": 1}])
        ids = {"cluster-a": _identity("team-a", "prod"), "cluster-p2": _identity("p2", "x")}
        big = {f"proj-{i:03d}": [(f"proj-{i:03d}", "c", "us-central1")] for i in range(rec.RESOLVED_SET_CAP)}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                                 profiles=["cluster-a", "cluster-p2"], identities=ids,
                                                 searches={self.FOLDER: (big, rec.OUTCOME_OK)})
        self.assertEqual(created, [])
        snap = self._snapshot()
        self.assertEqual(snap["containers"][0], {"id": self.FOLDER, "outcome": rec.OUTCOME_OVER_CAP, "projects": len(big)})
        rows = {p["id"]: p for p in snap["projects"]}
        # The members the run resolved are carried reading over-cap, with no CREATE.
        self.assertEqual({rows[p]["outcome"] for p in big}, {rec.OUTCOME_OVER_CAP})
        self.assertEqual(rows["proj-000"]["via"], [self.FOLDER])
        # over-cap is a decided outcome: the dropped explicit project starts retiring; team-a, a
        # member the index no longer places under the still-declared folder, is kept instead.
        self.assertEqual(report["retiring"], ["p2"])
        self.assertEqual(rows["team-a"]["state"], rec.STATE_IN_SCOPE)

    def test_a_project_that_moved_into_an_over_cap_folder_is_kept_not_retired(self):
        # x was reached through F1; it moves to F2, whose membership crosses the cap. The run
        # knows x is under F2, so x is carried over-cap rather than judged absent.
        f1, f2 = "folders/111111111111", "folders/222222222222"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "x", "via": [f1], "state": rec.STATE_IN_SCOPE}])
        ids = {"cluster-x": _identity("x", "c")}
        big = {f"q-{i}": [(f"q-{i}", "c", "us-central1")] for i in range(4)} | {"x": [("x", "c", "us-central1")]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            for _ in range(2):
                report, _, deleted = self._run({"folders": [f1[8:], f2[8:]]}, {self.MGMT: []}, profiles=["cluster-x"], identities=ids,
                                               searches={f1: ({}, rec.OUTCOME_OK), f2: (big, rec.OUTCOME_OK)})
                self.assertEqual((deleted, report["retiring"]), ([], []))
                rows = {p["id"]: p for p in self._snapshot()["projects"]}
                self.assertEqual((rows["x"]["outcome"], rows["x"]["via"]), (rec.OUTCOME_OVER_CAP, [f2]))

    def test_a_project_under_two_containers_takes_the_live_listing_whichever_sorted_first(self):
        # F1 (sorts first) is denied and carries x frozen; F2 lists x live: the listing wins.
        f1, f2 = "folders/111111111111", "folders/222222222222"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "x", "via": [f1, f2], "state": rec.STATE_IN_SCOPE}])
        report, created, _ = self._run({"folders": [f1[8:], f2[8:]]}, {self.MGMT: []},
                                       searches={f1: (None, rec.OUTCOME_DENIED), f2: ({"x": [("x", "c", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual(created, [("x", "c", "us-central1")])
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((rows["x"]["outcome"], rows["x"]["via"], rows["x"]["clusters"]), (rec.OUTCOME_OK, [f1, f2], 1))
        # The denied container still holds the prune.
        self.assertEqual(report["retiring"], [])

    def test_a_first_run_with_a_frozen_or_over_cap_container_contributes_what_it_knows(self):
        big = {f"q-{i}": [(f"q-{i}", "c", "us-central1")] for i in range(5)}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run({"folders": ["111111111111", "222222222222"]}, {self.MGMT: []},
                                           searches={"folders/111111111111": (None, rec.OUTCOME_UNREACHABLE), "folders/222222222222": (big, rec.OUTCOME_OK)})
        self.assertEqual(created, [])
        containers = {c["id"]: c for c in self._snapshot()["containers"]}
        self.assertEqual(containers["folders/111111111111"], {"id": "folders/111111111111", "outcome": rec.OUTCOME_UNREACHABLE, "projects": 0})
        self.assertEqual(containers["folders/222222222222"]["projects"], 5)
        self.assertEqual(len([p for p in self._snapshot()["projects"] if p["outcome"] == rec.OUTCOME_OVER_CAP]), 5)

    def test_a_member_project_the_account_cannot_read_is_probed_once_and_not_scaffolded(self):
        # The index names the member's clusters without a permission check; one describe
        # before the first create answers 403, and nothing is scaffolded under it this run.
        members = {"team-a": [("team-a", "prod", "us-central1"), ("team-a", "dev", "us-central1")]}
        probes: list[tuple] = []

        def describe(project, cluster, location):
            probes.append((project, cluster))
            rec._denied_this_run.add(project)
            return None
        report, created, _ = self._run({"projects": ["p2"], "folders": ["123456789012"]},
                                       {self.MGMT: [], "p2": [("p2", "x", "us-central1")]},
                                       searches={self.FOLDER: (members, rec.OUTCOME_OK)}, exists=describe)
        self.assertEqual(probes, [("team-a", "dev")])  # the first cluster answers 403; the second is not probed
        self.assertEqual(created, [("p2", "x", "us-central1")])  # the explicit project is not probed
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_DENIED)
        self.assertEqual(report["create_failed"], [])

    def test_a_member_cluster_the_index_still_names_but_describe_says_is_gone_is_skipped(self):
        members = {"team-a": [("team-a", "gone", "us-central1"), ("team-a", "live", "us-central1")]}
        answers = {"gone": False, "live": True}
        report, created, _ = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                       searches={self.FOLDER: (members, rec.OUTCOME_OK)},
                                       exists=lambda project, cluster, location: answers[cluster])
        self.assertEqual(created, [("team-a", "live", "us-central1")])
        self.assertEqual(report["create_failed"], [])
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_OK)

    def test_a_local_permission_error_on_create_is_not_an_iam_denial_and_skips_nothing_else(self):
        # An EACCES writing the profile home, or the shim's own "Permission denied", must not read
        # as a 403: the next cluster in the same project is still attempted, in a member project
        # and in the management project alike.
        members = {"team-a": [("team-a", "a1", "us-central1"), ("team-a", "a2", "us-central1")]}
        attempts: list[str] = []

        def failing_create(pr, c, l):
            attempts.append(c)
            raise OSError(13, "Permission denied", f"/opt/data/profiles/cluster-{c}")
        with mock.patch.object(rec, "create_profile", side_effect=failing_create):
            report, created, _ = self._run({"folders": ["123456789012"]},
                                           {self.MGMT: [(self.MGMT, "m1", "us-central1"), (self.MGMT, "m2", "us-central1")]},
                                           searches={self.FOLDER: (members, rec.OUTCOME_OK)},
                                           create_raises=None)
        self.assertEqual(sorted(attempts), ["a1", "a2", "m1", "m2"])
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_OK)
        self.assertEqual(len(report["create_failed"]), 4)

    def test_a_carried_row_keeps_the_via_it_had(self):
        # An unjudged run carries a member with its container via, so a freeze on a later run
        # still finds it among the container's previous members.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE}])
        ids = {"cluster-a": _identity("team-a", "prod")}
        # Unclean tick: an explicit project unreachable; the folder itself is not declared this run.
        report, _, deleted = self._run({"projects": ["flaky"]}, {self.MGMT: [], "flaky": (None, rec.OUTCOME_UNREACHABLE)},
                                       profiles=["cluster-a"], identities=ids)
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((rows["team-a"]["state"], rows["team-a"]["via"]), (rec.STATE_IN_SCOPE, [self.FOLDER]))
        self.assertEqual(rec._previous_container_members(self._snapshot(), self.FOLDER), ["team-a"])

    def test_a_403_on_create_marks_a_member_denied_and_never_an_explicit_project(self):
        members = {"team-a": [("team-a", "prod", "us-central1")]}
        report, _, _ = self._run({"projects": ["p2"], "folders": ["123456789012"]},
                                 {self.MGMT: [], "p2": [("p2", "x", "us-central1")]},
                                 searches={self.FOLDER: (members, rec.OUTCOME_OK)},
                                 create_raises=SystemExit("ERROR: code=403 PERMISSION_DENIED"))
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_DENIED)
        # The explicit project's own listing decides its outcome; a per-cluster 403 there is not a revision.
        self.assertEqual(report["projects"]["p2"], rec.OUTCOME_OK)
        self.assertEqual(sorted(report["create_failed"]), ["prod/us-central1", "x/us-central1"])

    def test_a_403_on_create_in_an_explicit_project_skips_none_of_its_other_clusters(self):
        attempts: list[str] = []

        def failing_create(pr, c, l):
            attempts.append(c)
            raise SystemExit("ERROR: failed to fetch credentials for 'x': ResponseError: code=403, message=Required permission")
        with mock.patch.object(rec, "create_profile", side_effect=failing_create):
            report, _, _ = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "a", "us-central1"), (self.MGMT, "b", "us-central1"), (self.MGMT, "c", "us-central1")]})
        self.assertEqual(attempts, ["a", "b", "c"])
        self.assertEqual(report["projects"][self.MGMT], rec.OUTCOME_OK)

    def test_container_searches_share_the_listing_budget_and_the_management_listing_comes_first(self):
        import time
        calls: dict[str, float] = {}
        order: list[str] = []

        def search(container, timeout=None):
            order.append(container)
            calls[container] = timeout
            time.sleep(min(1.5, timeout if timeout is not None else 1.5))
            return None, rec.OUTCOME_UNREACHABLE

        def lister(project, timeout=None):
            order.append(project)
            calls[project] = timeout
            return [], rec.OUTCOME_OK
        start = time.monotonic()
        with mock.patch.object(rec, "LIST_BUDGET_SECONDS", 0.3), mock.patch.object(rec, "LIST_GRACE_SECONDS", 0.05):
            report, _, _ = self._run({"projects": ["p2"], "folders": ["111111111111"]}, lister, searches=search)
        self.assertLess(time.monotonic() - start, 1.5)
        # Management first, with its full timeout; the container and the explicit project are
        # cut to the budget left, which is the floor once the container has spent it.
        self.assertEqual(order[0], self.MGMT)
        self.assertIsNone(calls[self.MGMT])
        self.assertLessEqual(calls["folders/111111111111"], 1.0)
        self.assertLessEqual(calls["p2"], 1.0)
        self.assertTrue(report["create_pass_ran"])
        self.assertEqual(self._snapshot()["containers"][0]["outcome"], rec.OUTCOME_UNREACHABLE)

    def test_a_member_the_index_no_longer_places_under_its_folder_is_kept_until_the_declaration_speaks(self):
        # The asset index lags a move by minutes to hours; the declaration did not change, so the
        # member is kept and listed, run after run, and returns to scope when the index does.
        prev = [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE}]
        self._write_previous(prev)
        ids = {"cluster-a": _identity("team-a", "prod")}
        stamps = set()
        for _ in range(3):
            report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                           searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
            self.assertEqual((deleted, report["retiring"], report["unmanaged"]), ([], [], ["cluster-a"]))
            rows = {p["id"]: p for p in self._snapshot()["projects"]}
            self.assertEqual((rows["team-a"]["state"], rows["team-a"]["via"]), (rec.STATE_IN_SCOPE, [self.FOLDER]))
            self.assertIn("asset index", self._snapshot()["unmanaged"][0]["reason"])
            stamps.add(rows["team-a"][rec.ABSENT_SINCE_KEY])
        self.assertEqual(len(stamps), 1)  # the first absent run's time, carried, not restarted
        # The member's own folder freezing in between carries the stamp too: the frozen
        # container pulls the member back in as an entry, and the stamp rides along.
        report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                       searches={self.FOLDER: (None, rec.OUTCOME_DENIED)})
        self.assertEqual(deleted, [])
        self.assertEqual({p["id"]: p.get(rec.ABSENT_SINCE_KEY) for p in self._snapshot()["projects"]}["team-a"], next(iter(stamps)))
        # An unclean run in between (an explicit project unreachable) carries the stamp too.
        report, _, deleted = self._run({"projects": ["flaky"], "folders": ["123456789012"]},
                                       {self.MGMT: [], "flaky": (None, rec.OUTCOME_UNREACHABLE)},
                                       profiles=["cluster-a"], identities=ids, searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertEqual(deleted, [])
        self.assertEqual({p["id"]: p.get(rec.ABSENT_SINCE_KEY) for p in self._snapshot()["projects"]}["team-a"], next(iter(stamps)))
        # The index places it again: back in scope, nothing lost, the clock gone.
        report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                       searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["unmanaged"], report["kept"]), ([], [], ["cluster-a"]))
        self.assertNotIn(rec.ABSENT_SINCE_KEY, {p["id"]: p for p in self._snapshot()["projects"]}["team-a"])
        # A project deleted, or moved under a parent the CR does not declare: absent past the
        # grace, the ordinary two-run retire applies, so the leak has a ceiling.
        old = "2020-01-01T00:00:00Z"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE, rec.ABSENT_SINCE_KEY: old}])
        report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                       searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["retiring"]), ([], ["team-a"]))
        report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                       searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertEqual(deleted, ["cluster-a"])
        # The declaration speaks, two ways: the folder leaves the CR, or a glob names the project.
        # On an unstamped row, and on a row already stamped (the operator reacting to
        # `unmanaged` by dropping the folder): both retire at once.
        stamped = [{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                   {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE,
                    rec.ABSENT_SINCE_KEY: datetime.now(timezone.utc).strftime(rec.SNAPSHOT_TIME_FORMAT)}]
        for scope, previous in (({"projects": []}, prev), ({"projects": []}, stamped),
                                ({"folders": ["123456789012"], "exclude": {"projects": ["team-*"]}}, prev),
                                ({"folders": ["123456789012"], "exclude": {"projects": ["team-*"]}}, stamped)):
            self._write_previous(previous, containers=[{"id": self.FOLDER, "outcome": rec.OUTCOME_OK, "projects": 1}],
                                 declared={"projects": [], "folders": ["123456789012"], "organizations": [],
                                           "exclude": {"projects": [], "clusters": []}})
            report, _, deleted = self._run(scope, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                           searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
            self.assertEqual((deleted, report["retiring"]), ([], ["team-a"]), scope)
            report, _, deleted = self._run(scope, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                           searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
            self.assertEqual(deleted, ["cluster-a"], scope)

    def test_dropping_the_explicit_entry_of_a_project_still_under_a_declared_folder_keeps_it(self):
        # The ordinary migration: the project moves into the folder and leaves `projects` in the
        # same edit, while the index is still behind on the move. The container route is still
        # declared, so the keep-for-a-day rule applies.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": ["explicit", self.FOLDER], "state": rec.STATE_IN_SCOPE}])
        ids = {"cluster-a": _identity("team-a", "prod")}
        for _ in range(2):
            report, _, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                           profiles=["cluster-a"], identities=ids, searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
            self.assertEqual((deleted, report["retiring"], report["unmanaged"]), ([], [], ["cluster-a"]))
        # The index catches up: listed through the folder, nothing lost.
        report, created, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                             profiles=["cluster-a"], identities=ids,
                                             searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["kept"]), ([], ["cluster-a"]))

    def test_a_project_moved_into_a_folder_declared_in_the_same_edit_is_kept_through_the_lag(self):
        # The other ordering of the migration: the folder is declared and the explicit entry
        # dropped in one edit, within the index's lag after the move. The previous row's via
        # names no container, so the newly declared container is what keeps it, for a day.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}])
        ids = {"cluster-a": _identity("team-a", "prod")}
        stamps = set()
        for _ in range(3):
            report, _, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                           profiles=["cluster-a"], identities=ids, searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
            self.assertEqual((deleted, report["retiring"], report["unmanaged"]), ([], [], ["cluster-a"]))
            rows = {p["id"]: p for p in self._snapshot()["projects"]}
            self.assertEqual(rows["team-a"]["state"], rec.STATE_IN_SCOPE)
            stamps.add(rows["team-a"][rec.ABSENT_SINCE_KEY])
        self.assertEqual(len(stamps), 1)  # the container is no longer new on the later runs; the stamp carries
        # The index catches up: listed through the folder, nothing lost, the clock gone.
        report, _, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                       profiles=["cluster-a"], identities=ids,
                                       searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["kept"]), ([], ["cluster-a"]))
        self.assertNotIn(rec.ABSENT_SINCE_KEY, {p["id"]: p for p in self._snapshot()["projects"]}["team-a"])
        # Past the grace, the ordinary two-run retire: the leak has the same ceiling.
        old = "2020-01-01T00:00:00Z"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": ["explicit"], "state": rec.STATE_IN_SCOPE, rec.ABSENT_SINCE_KEY: old}])
        report, _, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                       profiles=["cluster-a"], identities=ids, searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["retiring"]), ([], ["team-a"]))
        # Dropped from `projects` with no new container declared: the declaration decides, as before.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}])
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), ([], ["team-a"]))

    def test_a_retiring_project_is_not_rescued_by_a_newly_declared_folder_on_an_unclean_run(self):
        # X was dropped from `projects` and is retiring. The operator declares a folder whose
        # first search is denied (the grant not yet there), so the run is unclean and the
        # folder is new: X stays retiring, and is neither stamped nor carried back in scope.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "x", "via": [], "state": rec.STATE_RETIRING}])
        ids = {"cluster-x": _identity("x", "c")}
        report, _, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                       profiles=["cluster-x"], identities=ids,
                                       searches={self.FOLDER: (None, rec.OUTCOME_DENIED)})
        self.assertEqual(deleted, [])
        self.assertEqual(report["retiring"], ["x"])
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual(rows["x"]["state"], rec.STATE_RETIRING)
        self.assertNotIn(rec.ABSENT_SINCE_KEY, rows["x"])
        # The next clean run prunes it, as the declaration asked.
        report, _, deleted = self._run({"projects": [], "folders": ["123456789012"]}, {self.MGMT: []},
                                       profiles=["cluster-x"], identities=ids,
                                       searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertEqual(deleted, ["cluster-x"])

    def test_a_member_whose_get_credentials_is_refused_is_rolled_back_not_left_half_built(self):
        # describe succeeds (the account holds clusters.get in the member project) but
        # get-credentials answers 403: the profile was registered and its layout pushed
        # before the failure, so the reconcile deletes it again rather than leave a home the
        # next run reads as incomplete and scaffolds once more.
        def refuse_credentials(pr, c, l):
            if pr == "team-a":
                raise SystemExit("ERROR: failed to fetch credentials for team-a/prod: code=403 PERMISSION_DENIED")
        report, created, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                             searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)},
                                             create_raises=refuse_credentials)
        self.assertEqual(created, [])
        self.assertEqual(report["create_failed"], ["prod/us-central1"])
        self.assertEqual(deleted, [rec.profile_name("team-a", "prod", "us-central1")])
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_DENIED)

    def test_an_over_cap_folder_does_not_close_the_cap_on_the_folders_after_it(self):
        # F1 sorts first and crosses the cap; F2 is small. F1's members are carried over-cap
        # without counting, so F2 still fits and its cluster is created (design §3: a
        # container that does not fit is skipped and the next one is still tried).
        f1, f2 = "folders/111111111111", "folders/222222222222"
        big = {f"q-{i}": [(f"q-{i}", "c", "us-central1")] for i in range(5)}
        small = {"small": [("small", "s", "us-central1")]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run({"folders": [f1[8:], f2[8:]]}, {self.MGMT: []},
                                           searches={f1: (big, rec.OUTCOME_OK), f2: (small, rec.OUTCOME_OK)})
        self.assertEqual(created, [("small", "s", "us-central1")])
        containers = {c["id"]: c for c in self._snapshot()["containers"]}
        self.assertEqual((containers[f1]["outcome"], containers[f2]["outcome"]), (rec.OUTCOME_OVER_CAP, rec.OUTCOME_OK))
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((rows["q-0"]["outcome"], rows["small"]["outcome"]), (rec.OUTCOME_OVER_CAP, rec.OUTCOME_OK))

    def test_a_later_container_cannot_lift_an_over_cap_members_past_the_cap(self):
        # Cap 3, management plus explicit p and q already listed. F1 = {a} crosses the cap and
        # carries a over-cap; F2 = {a} would list a live, so a counts against the cap for F2
        # and F2 reads over-cap too: nothing is created, three projects listed.
        f1, f2 = "folders/111111111111", "folders/222222222222"
        a = {"a": [("a", "c", "us-central1")]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run({"projects": ["p", "q"], "folders": [f1[8:], f2[8:]]},
                                           {self.MGMT: [], "p": [], "q": []},
                                           searches={f1: (a, rec.OUTCOME_OK), f2: (a, rec.OUTCOME_OK)})
        self.assertEqual(created, [])
        containers = {c["id"]: c for c in self._snapshot()["containers"]}
        self.assertEqual((containers[f1]["outcome"], containers[f2]["outcome"]), (rec.OUTCOME_OVER_CAP, rec.OUTCOME_OVER_CAP))
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((rows["a"]["outcome"], rows["a"]["via"]), (rec.OUTCOME_OVER_CAP, [f1, f2]))
        self.assertEqual(len([p for p in rows.values() if p["outcome"] != rec.OUTCOME_OVER_CAP]), 3)

    def test_a_sub_folder_that_fits_lifts_its_members_out_of_the_parents_over_cap(self):
        # The CRD page's remedy: the parent crosses the cap, the declared sub-folder that holds
        # the clusters fits, and its members take the live listing; the rest stay over-cap.
        parent, sub = "folders/111111111111", "folders/222222222222"
        big = {f"q-{i}": [(f"q-{i}", "c", "us-central1")] for i in range(5)}
        small = {"q-0": big["q-0"], "q-1": big["q-1"]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run({"folders": [parent[8:], sub[8:]]}, {self.MGMT: []},
                                           searches={parent: (big, rec.OUTCOME_OK), sub: (small, rec.OUTCOME_OK)})
        self.assertEqual(created, [("q-0", "c", "us-central1"), ("q-1", "c", "us-central1")])
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((rows["q-0"]["outcome"], rows["q-2"]["outcome"]), (rec.OUTCOME_OK, rec.OUTCOME_OVER_CAP))
        containers = {c["id"]: c for c in self._snapshot()["containers"]}
        self.assertEqual((containers[parent]["outcome"], containers[sub]["outcome"]), (rec.OUTCOME_OVER_CAP, rec.OUTCOME_OK))

    def test_a_frozen_folder_that_was_over_cap_does_not_shut_its_siblings_out(self):
        # A read over-cap last run (its five members written over-cap); this run its search
        # fails and B, small, is ok. A's members are carried frozen under `unreachable` but
        # were not listed last run either, so they do not count against the cap: B lists
        # and its cluster is created.
        a, b = "folders/111111111111", "folders/222222222222"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE}]
                             + [{"id": f"q-{i}", "via": [a], "state": rec.STATE_IN_SCOPE, "outcome": rec.OUTCOME_OVER_CAP}
                                for i in range(5)],
                             containers=[{"id": a, "outcome": rec.OUTCOME_OVER_CAP, "projects": 5},
                                         {"id": b, "outcome": rec.OUTCOME_OK, "projects": 1}])
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run({"folders": [a[8:], b[8:]]}, {self.MGMT: []},
                                           searches={a: (None, rec.OUTCOME_UNREACHABLE),
                                                     b: ({"small": [("small", "s", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual(created, [("small", "s", "us-central1")])
        containers = {c["id"]: c for c in self._snapshot()["containers"]}
        self.assertEqual((containers[a]["outcome"], containers[a]["projects"], containers[b]["outcome"]),
                         (rec.OUTCOME_UNREACHABLE, 5, rec.OUTCOME_OK))
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((rows["q-0"]["outcome"], rows["small"]["outcome"]), (rec.OUTCOME_UNREACHABLE, rec.OUTCOME_OK))
        # A frozen member that WAS listed last run still counts, so the cap does not flap
        # when the folder recovers: the same run with A's members recorded ok reads B over-cap.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE}]
                             + [{"id": f"q-{i}", "via": [a], "state": rec.STATE_IN_SCOPE, "outcome": rec.OUTCOME_OK}
                                for i in range(2)],
                             containers=[{"id": a, "outcome": rec.OUTCOME_OK, "projects": 2}])
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run({"folders": [a[8:], b[8:]]}, {self.MGMT: []},
                                           searches={a: (None, rec.OUTCOME_UNREACHABLE),
                                                     b: ({"small": [("small", "s", "us-central1")]}, rec.OUTCOME_OK)})
        self.assertEqual(created, [])
        self.assertEqual({c["id"]: c["outcome"] for c in self._snapshot()["containers"]}[b], rec.OUTCOME_OVER_CAP)

    def test_a_get_credentials_403_on_a_home_that_predates_the_run_leaves_it_in_place(self):
        # The home exists but its cluster_identity cannot be read, so the create path
        # reaches it (the triple is not among the existing keys); get-credentials answers
        # 403. PRUNE keeps an unverifiable profile on purpose, so the rollback must not
        # remove it: the project reads denied, the home stays.
        home = rec.profile_name("team-a", "prod", "us-central1")

        def refuse_credentials(pr, c, l):
            if pr == "team-a":
                raise SystemExit("ERROR: failed to fetch credentials for team-a/prod: code=403 PERMISSION_DENIED")
        report, created, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                             profiles=[home], identities={home: None},
                                             searches={self.FOLDER: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)},
                                             create_raises=refuse_credentials)
        self.assertEqual(created, [])
        self.assertEqual(deleted, [])
        self.assertEqual(report["create_failed"], ["prod/us-central1"])
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_DENIED)
        self.assertIn(home, report["skipped_no_identity"])

    def test_a_member_whose_gke_api_is_disabled_reads_api_disabled_not_denied_on_the_create_path(self):
        # gcloud's API-disabled answer is itself a 403; the create path classifies it first,
        # the way the listing classifier does, so the snapshot sends the operator to the API
        # and not to IAM, and the second cluster in the project is not attempted.
        def api_off(pr, c, l):
            if pr == "team-a":
                raise SystemExit("ERROR: failed to fetch credentials for team-a/prod: ResponseError: code=403, "
                                 "message=Kubernetes Engine API has not been used in project team-a before or it is disabled.")
        members = {"team-a": [("team-a", "prod", "us-central1"), ("team-a", "dev", "us-central1")]}
        report, created, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []},
                                             searches={self.FOLDER: (members, rec.OUTCOME_OK)},
                                             create_raises=api_off)
        self.assertEqual(created, [])
        self.assertEqual(report["create_failed"], ["dev/us-central1"])
        self.assertEqual(deleted, [rec.profile_name("team-a", "dev", "us-central1")])
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_API_DISABLED)

    def test_a_folder_to_folder_move_in_one_edit_is_held_through_the_index_lag(self):
        # team-a sat under folder 111; the operator moves it into folder 222 and swaps the
        # declaration from 111 to 222 in one edit. The index lags, so 222 resolves without
        # it: team-a is held the same day the explicit case is (its previous route names no
        # declared container, and the new container marks the edit), and lists once placed.
        old, new = "folders/111111111111", "folders/222222222222"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [old], "state": rec.STATE_IN_SCOPE}],
                             containers=[{"id": old, "outcome": rec.OUTCOME_OK, "projects": 1}])
        ids = {"cluster-a": _identity("team-a", "prod")}
        report, _, deleted = self._run({"folders": [new[8:]]}, {self.MGMT: []},
                                       profiles=["cluster-a"], identities=ids,
                                       searches={new: ({}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["retiring"]), ([], []))
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual(rows["team-a"]["state"], rec.STATE_IN_SCOPE)
        self.assertIn(rec.ABSENT_SINCE_KEY, rows["team-a"])
        report, _, deleted = self._run({"folders": [new[8:]]}, {self.MGMT: []},
                                       profiles=["cluster-a"], identities=ids,
                                       searches={new: ({"team-a": [("team-a", "prod", "us-central1")]}, rec.OUTCOME_OK)})
        rows = {p["id"]: p for p in self._snapshot()["projects"]}
        self.assertEqual((deleted, rows["team-a"]["outcome"], rows["team-a"]["via"]), ([], rec.OUTCOME_OK, [new]))
        self.assertNotIn(rec.ABSENT_SINCE_KEY, rows["team-a"])
        # Removing 111 while 222 was already declared is the declaration speaking: retiring.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [old], "state": rec.STATE_IN_SCOPE}],
                             containers=[{"id": old, "outcome": rec.OUTCOME_OK, "projects": 1},
                                         {"id": new, "outcome": rec.OUTCOME_OK, "projects": 0}])
        report, _, _ = self._run({"folders": [new[8:]]}, {self.MGMT: []},
                                 profiles=["cluster-a"], identities=ids,
                                 searches={new: ({}, rec.OUTCOME_OK)})
        self.assertEqual(report["retiring"], ["team-a"])

    def test_a_dry_run_previews_the_creates_and_outcomes_the_real_run_makes(self):
        # The per-cluster probe runs on a dry run too: a member the account cannot read
        # previews as denied with no WOULD-create, and a cluster the index still names but
        # describe says is gone is left out, the way the real run leaves them.
        members = {"team-a": [("team-a", "prod", "us-central1")], "team-b": [("team-b", "old", "us-central1")],
                   "team-c": [("team-c", "live", "us-central1")]}

        def probe(project, cluster, location):
            if project == "team-a":
                rec._denied_this_run.add(project)
                return None
            return project != "team-b"
        report, created, _ = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, exists=probe, dry_run=True,
                                       searches={self.FOLDER: (members, rec.OUTCOME_OK)})
        self.assertEqual(created, [])
        self.assertEqual(report["created"], ["live/us-central1"])
        self.assertEqual((report["projects"]["team-a"], report["projects"]["team-b"], report["projects"]["team-c"]),
                         (rec.OUTCOME_DENIED, rec.OUTCOME_OK, rec.OUTCOME_OK))

    def test_an_over_cap_members_stamp_clears_because_the_index_placed_it(self):
        old = "2020-01-01T00:00:00Z"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE, rec.ABSENT_SINCE_KEY: old}])
        ids = {"cluster-a": _identity("team-a", "prod")}
        big = {f"q-{i}": [(f"q-{i}", "c", "us-central1")] for i in range(4)} | {"team-a": [("team-a", "prod", "us-central1")]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 2):
            report, _, _ = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                     searches={self.FOLDER: (big, rec.OUTCOME_OK)})
        row = {p["id"]: p for p in self._snapshot()["projects"]}["team-a"]
        self.assertEqual(row["outcome"], rec.OUTCOME_OVER_CAP)
        self.assertNotIn(rec.ABSENT_SINCE_KEY, row)
        # A malformed stamp restarts the clock instead of keeping for ever or retiring at once.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE, rec.ABSENT_SINCE_KEY: "yesterday"}])
        report, _, deleted = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                       searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertEqual((deleted, report["retiring"]), ([], []))
        stamp = {p["id"]: p for p in self._snapshot()["projects"]}["team-a"][rec.ABSENT_SINCE_KEY]
        self.assertNotEqual(stamp, "yesterday")

    def test_a_stamp_on_a_project_listed_as_explicit_or_placed_by_a_later_container_clears(self):
        old = "2020-01-01T00:00:00Z"
        ids = {"cluster-a": _identity("team-a", "prod")}
        # Explicit and under the folder, with a stamp from an earlier lag: listed live, the
        # stamp clears, so removing the explicit entry weeks later starts a fresh day.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": ["explicit", self.FOLDER], "state": rec.STATE_IN_SCOPE, rec.ABSENT_SINCE_KEY: old}])
        self._run({"projects": ["team-a"], "folders": ["123456789012"]}, {self.MGMT: [], "team-a": [("team-a", "prod", "us-central1")]},
                  profiles=["cluster-a"], identities=ids, searches={self.FOLDER: ({}, rec.OUTCOME_OK)})
        self.assertNotIn(rec.ABSENT_SINCE_KEY, {p["id"]: p for p in self._snapshot()["projects"]}["team-a"])
        # A container sorted first freezes and carries it; a later over-cap container's
        # successful lookup places it: the index saw it, the stamp clears.
        f1, f2 = "folders/111111111111", "folders/222222222222"
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [f1], "state": rec.STATE_IN_SCOPE, rec.ABSENT_SINCE_KEY: old}])
        big = {f"q-{i}": [(f"q-{i}", "c", "us-central1")] for i in range(4)} | {"team-a": [("team-a", "prod", "us-central1")]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 2):
            self._run({"folders": [f1[8:], f2[8:]]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                      searches={f1: (None, rec.OUTCOME_DENIED), f2: (big, rec.OUTCOME_OK)})
        self.assertNotIn(rec.ABSENT_SINCE_KEY, {p["id"]: p for p in self._snapshot()["projects"]}["team-a"])

    def test_a_render_that_predates_containers_keeps_their_members(self):
        # Rollback to the previous release: its render has present and projects but no folders
        # or organizations key, and the CRD pruned the lists from the stored CR. It declares
        # nothing about containers, so a member reached through one is kept, not retired.
        self._write_previous([{"id": self.MGMT, "via": ["management"], "state": rec.STATE_IN_SCOPE},
                              {"id": "team-a", "via": [self.FOLDER], "state": rec.STATE_IN_SCOPE}])
        ids = {"cluster-a": _identity("team-a", "prod")}
        old_render = {rec.SCOPE_PRESENT_KEY: True, "projects": [], "exclude": {"projects": [], "clusters": []}}
        path = Path(self._tmp.name) / "scope.json"
        path.write_text(json.dumps(old_render), encoding="utf-8")
        os.environ[rec.SCOPE_FILE_ENV] = str(path)
        for _ in range(2):
            report, _, deleted = self._run(None, {self.MGMT: []}, profiles=["cluster-a"], identities=ids)
            self.assertEqual((deleted, report["retiring"], report["unmanaged"]), ([], [], ["cluster-a"]))
            self.assertIn("does not know", self._snapshot()["unmanaged"][0]["reason"])

    def test_a_member_whose_describe_answers_403_reads_denied(self):
        members = {"team-a": [("team-a", "prod", "us-central1")]}
        ids = {"cluster-a": _identity("team-a", "prod")}

        def describe(project, cluster, location):
            rec._denied_this_run.add(project)
            return None
        report, _, _ = self._run({"folders": ["123456789012"]}, {self.MGMT: []}, profiles=["cluster-a"], identities=ids,
                                 searches={self.FOLDER: (members, rec.OUTCOME_OK)}, exists=describe)
        self.assertEqual(report["projects"]["team-a"], rec.OUTCOME_DENIED)
        self.assertEqual({p["id"]: p["outcome"] for p in self._snapshot()["projects"]}["team-a"], rec.OUTCOME_DENIED)

    def test_describe_classifies_a_403_as_denied_for_the_project(self):
        err = subprocess.CalledProcessError(1, ["gcloud"], stderr='ERROR: (gcloud.container.clusters.describe) ResponseError: code=403, message=Required "container.clusters.get" permission')
        with mock.patch.object(rec.sandbox_exec, "run", side_effect=err):
            rec._denied_this_run.clear()
            self.assertIsNone(rec._cluster_exists("team-a", "prod", "us-central1"))
            self.assertEqual(rec._denied_this_run, {"team-a"})

    def test_search_container_parses_results_and_classifies_failures(self):
        out = json.dumps([self._asset("team-a", "prod", "us-central1"), self._asset("team-a", "dev", "us-central1-a", zonal=True)])
        with mock.patch.object(rec.sandbox_exec, "run", return_value=mock.Mock(stdout=out)) as run:
            members, outcome = rec._search_container(self.FOLDER)
        self.assertEqual(outcome, rec.OUTCOME_OK)
        self.assertEqual(members, {"team-a": [("team-a", "prod", "us-central1"), ("team-a", "dev", "us-central1-a")]})
        # A row this parser cannot read is a shape this run does not know, not a foreign asset
        # to skip: the search is filtered to clusters, so the container freezes rather than
        # resolving empty and retiring every member a day later.
        odd = json.dumps([self._asset("team-a", "prod", "us-central1"), {"name": "//container.googleapis.com/projects/x/regions/r/clusters/c"}])
        with mock.patch.object(rec.sandbox_exec, "run", return_value=mock.Mock(stdout=odd)), mock.patch.object(rec, "log") as logged:
            self.assertEqual(rec._search_container(self.FOLDER), (None, rec.OUTCOME_UNREACHABLE))
        self.assertIn("shape this run cannot read", " ".join(str(c) for c in logged.call_args_list))
        self.assertIn(f"--scope={self.FOLDER}", run.call_args[0][0])
        self.assertIn(f"--asset-types={rec.ASSET_TYPE_CLUSTER}", run.call_args[0][0])
        self.assertIn(f"--format={rec.ASSET_SEARCH_FORMAT}", run.call_args[0][0])
        # Output the proxy cut at its cap is not a transient: the log names the cause, the
        # outcome stays unreachable, and the container freezes rather than resolving empty.
        cut = mock.Mock(stdout='[{"name": "//container.googleapis.com/projects/a/locations/l/clu', stderr="credential proxy output truncated at 8388608 bytes")
        with mock.patch.object(rec.sandbox_exec, "run", return_value=cut), mock.patch.object(rec, "log") as logged:
            self.assertEqual(rec._search_container(self.FOLDER), (None, rec.OUTCOME_UNREACHABLE))
        self.assertIn("cut the search output at its cap", " ".join(str(c) for c in logged.call_args_list))
        for stderr, want in (("PERMISSION_DENIED", rec.OUTCOME_DENIED),
                             ("Cloud Asset API has not been used in project 1 before or it is disabled", rec.OUTCOME_API_DISABLED),
                             ("connection reset", rec.OUTCOME_UNREACHABLE)):
            err = subprocess.CalledProcessError(1, ["gcloud"], stderr=stderr)
            with mock.patch.object(rec.sandbox_exec, "run", side_effect=err):
                self.assertEqual(rec._search_container(self.FOLDER), (None, want), stderr)

    def test_the_notification_counts_unlisted_projects_past_a_limit_and_names_containers(self):
        report = {"created": ["cluster-x"], "pruned": [], "create_failed": [], "skipped_error": [],
                  "projects": {f"proj-{i:04d}": rec.OUTCOME_OVER_CAP for i in range(30)} | {self.MGMT: rec.OUTCOME_OK},
                  "containers": [{"id": "organizations/111", "outcome": rec.OUTCOME_OVER_CAP, "projects": 30}]}
        text = rec._format_notification(report)
        # An over-cap container resolved; it is not reported as one that could not.
        self.assertNotIn("could not be resolved", text)
        self.assertIn(f"1 folder(s)/organisation(s) resolved past the listing cap of {rec.RESOLVED_SET_CAP}", text)
        self.assertIn("`organizations/111` (30 project(s))", text)
        self.assertIn("exclude.projects", text)
        report["containers"].append({"id": "folders/222", "outcome": rec.OUTCOME_DENIED, "projects": 2})
        text = rec._format_notification(report)
        self.assertIn("1 folder(s)/organisation(s) could not be resolved (members carried, profiles kept): `folders/222` (denied, 2 project(s)).", text)
        self.assertIn("30 project(s) in scope could not be listed", text)
        self.assertIn(f"and {30 - rec.NOTIFY_UNLISTED_LIMIT} more (see fleet_scope.json)", text)
        self.assertEqual(text.count("`proj-"), rec.NOTIFY_UNLISTED_LIMIT)
        self.assertLess(len(text), 1500)

    def test_an_unrelated_profile_of_unknown_identity_does_not_keep_a_project_retiring(self):
        # gone's last profile goes this tick; a hand-made directory with no identity, never
        # attributed to gone, must not pin gone in the snapshot, or a cluster onboarded there
        # by hand later would be pruned instead of kept as never in scope.
        (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(json.dumps(
            {"projects": [{"id": "gone", "state": rec.STATE_RETIRING}], "profiles": {"cluster-g": "gone"}}),
            encoding="utf-8")
        ids = {"cluster-g": _identity("gone", "g"), "hand-made": None}
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-g", "hand-made"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), (["cluster-g"], []))
        self.assertEqual([p["id"] for p in self._snapshot()["projects"]], [self.MGMT])
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-g2", "hand-made"],
                                       identities={"cluster-g2": _identity("gone", "g2"), "hand-made": None})
        self.assertEqual((deleted, report["unmanaged"]), ([], ["cluster-g2"]))
        self.assertEqual(self._snapshot()["unmanaged"][0]["reason"], "never in scope")

    def test_the_snapshot_attributes_every_readable_profile_to_its_project(self):
        report, _, _ = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                                 profiles=["cluster-m1", "cluster-h"],
                                 identities={"cluster-m1": _identity(self.MGMT, "m1"), "cluster-h": _identity("hand", "h")})
        self.assertEqual(self._snapshot()["profiles"], {"cluster-h": "hand", "cluster-m1": self.MGMT})

    def test_a_retiring_project_stays_in_the_snapshot_until_its_profiles_are_gone(self):
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        # The delete fails this tick: the home survives, so the project stays retiring and the
        # next run tries again rather than reading the survivor as never in scope.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")},
                                       delete_removes=False)
        self.assertEqual(deleted, ["cluster-g"])
        self.assertIn("gone", report["retiring"])
        self.assertEqual([(p["id"], p["clusters"]) for p in self._snapshot()["projects"] if p["state"] == rec.STATE_RETIRING], [("gone", 1)])

    def test_explicit_projects_past_the_cap_read_over_cap_and_are_not_listed(self):
        scope = {"projects": ["b", "c", "a"]}
        with mock.patch.object(rec, "RESOLVED_SET_CAP", 3):
            report, created, _ = self._run(scope, {
                self.MGMT: [], "a": [("a", "x", "us-central1")], "b": [("b", "y", "us-central1")],
                "c": [("c", "z", "us-central1")],
            })
        self.assertEqual(created, [("a", "x", "us-central1"), ("b", "y", "us-central1")])
        self.assertEqual(report["projects"], {self.MGMT: "ok", "a": "ok", "b": "ok", "c": rec.OUTCOME_OVER_CAP})

    def test_reconcile_exclude_still_works_beside_a_scope(self):
        scope = {"projects": ["other"]}
        report, created, _ = self._run(scope, {self.MGMT: [], "other": [("other", "skipme", "us-central1"),
                                                                        ("other", "keep", "us-central1")]},
                                       extra_exclude={"skipme"})
        self.assertEqual(created, [("other", "keep", "us-central1")])

    def test_an_unreadable_scope_file_falls_back_to_the_management_project(self):
        path = Path(self._tmp.name) / "scope.json"
        path.write_text("{not json", encoding="utf-8")
        os.environ[rec.SCOPE_FILE_ENV] = str(path)
        report, created, _ = self._run(None, {self.MGMT: [(self.MGMT, "m", "us-central1")],
                                              "other": [("other", "o", "us-central1")]})
        self.assertEqual(created, [(self.MGMT, "m", "us-central1")])

    def test_an_unresolved_management_project_switches_the_scope_prune_off(self):
        # The previous snapshot always names the management project, so a tick that
        # cannot resolve it would otherwise read every management profile as dropped.
        self._write_previous([{"id": self.MGMT, "state": rec.STATE_IN_SCOPE}])
        report, _, deleted = self._run({"projects": []}, {}, management=None,
                                       profiles=["cluster-m1", "cluster-m2"],
                                       identities={"cluster-m1": _identity(self.MGMT, "m1"),
                                                   "cluster-m2": _identity(self.MGMT, "m2")})
        self.assertEqual(deleted, [])
        self.assertEqual(sorted(report["unmanaged"]), ["cluster-m1", "cluster-m2"])
        self.assertEqual(report["kept"], ["cluster-m1", "cluster-m2"])

    def test_a_corrupt_scope_file_switches_the_scope_prune_off(self):
        self._write_previous([{"id": "other", "state": rec.STATE_IN_SCOPE}])
        path = Path(self._tmp.name) / "scope.json"
        path.write_text("[1, 2]", encoding="utf-8")   # valid JSON, not a declaration
        os.environ[rec.SCOPE_FILE_ENV] = str(path)
        report, created, deleted = self._run(None, {self.MGMT: [(self.MGMT, "m", "us-central1")]},
                                             profiles=["cluster-o"], identities={"cluster-o": _identity("other", "o")})
        self.assertEqual(deleted, [])
        self.assertEqual(created, [(self.MGMT, "m", "us-central1")])  # the management project still reconciles
        self.assertEqual(report["unmanaged"], ["cluster-o"])

    def test_an_empty_declaration_from_the_operator_is_a_declaration(self):
        # The operator renders {"present": true, "projects": [], "exclude": {...}} for a CR
        # whose scope block is present but empty; that is a declaration, and a project
        # dropped from it retires (an absent block, present=false, is the case that does not).
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        report, _, deleted = self._run({"projects": [], "exclude": {"projects": [], "clusters": []}},
                                       {self.MGMT: []}, profiles=["cluster-g"],
                                       identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual(deleted, ["cluster-g"])

    def test_a_missing_scope_file_is_not_a_declaration(self):
        # Variable set, file absent: the render did not reach the pod (a rollback, a
        # ConfigMap mid-resync). Create still runs; the scope prune does not.
        self._write_previous([{"id": "gone", "state": rec.STATE_IN_SCOPE}])
        os.environ[rec.SCOPE_FILE_ENV] = str(Path(self._tmp.name) / "absent.json")
        report, created, deleted = self._run(None, {self.MGMT: [(self.MGMT, "m", "us-central1")]},
                                             profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual(deleted, [])
        self.assertEqual(created, [(self.MGMT, "m", "us-central1")])
        self.assertTrue(report["create_pass_ran"])
        # Nothing dropped it, so it is carried forward in scope, not written as retiring.
        carried = next(p for p in self._snapshot()["projects"] if p["id"] == "gone")
        self.assertEqual((carried["state"], carried["outcome"]), (rec.STATE_IN_SCOPE, rec.OUTCOME_UNREACHABLE))
        self.assertEqual(report["retiring"], [])

    def test_a_project_dropped_on_an_unclean_tick_is_carried_and_takes_two_clean_runs(self):
        self._write_previous([{"id": "gone", "state": rec.STATE_IN_SCOPE}])
        gone = {"cluster-g": _identity("gone", "g")}
        # Tick 1: a flaky lookup; the drop is not judged, the project is carried forward.
        report, _, deleted = self._run({"projects": ["flaky"]},
                                       {self.MGMT: [], "flaky": (None, rec.OUTCOME_UNREACHABLE)},
                                       profiles=["cluster-g"], identities=gone)
        self.assertEqual((deleted, report["retiring"]), ([], []))
        rows = {p["id"]: p["state"] for p in self._snapshot()["projects"]}
        self.assertEqual(rows["gone"], rec.STATE_IN_SCOPE)
        # Tick 2: the first clean run marks it retiring.
        report, _, deleted = self._run({"projects": ["flaky"]}, {self.MGMT: [], "flaky": []},
                                       profiles=["cluster-g"], identities=gone)
        self.assertEqual((deleted, report["retiring"]), ([], ["gone"]))
        # Tick 3: unclean again; still retiring, not pruned, count not restarted.
        report, _, deleted = self._run({"projects": ["flaky"]},
                                       {self.MGMT: [], "flaky": (None, rec.OUTCOME_UNREACHABLE)},
                                       profiles=["cluster-g"], identities=gone)
        self.assertEqual((deleted, report["retiring"]), ([], ["gone"]))
        # Tick 4: the next clean run prunes.
        report, _, deleted = self._run({"projects": ["flaky"]}, {self.MGMT: [], "flaky": []},
                                       profiles=["cluster-g"], identities=gone)
        self.assertEqual(deleted, ["cluster-g"])

    def test_a_malformed_previous_snapshot_does_not_abort_the_run(self):
        for body in ("[1, 2]", '{"projects": null}', '{"projects": [1, "x", {"id": "z"}]}'):
            (Path(self._tmp.name) / rec.SNAPSHOT_FILE).write_text(body, encoding="utf-8")
            report, created, deleted = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m", "us-central1")]},
                                                 profiles=["cluster-h"], identities={"cluster-h": _identity("hand", "h")})
            self.assertEqual(created, [(self.MGMT, "m", "us-central1")], body)
            self.assertEqual(deleted, [], body)
            self.assertTrue(report["create_pass_ran"], body)

    def test_an_unresolved_management_project_leaves_the_roster_unreconciled_even_with_a_scope(self):
        report, created, _ = self._run({"projects": ["other"]}, {"other": [("other", "o", "us-central1")]},
                                       management=None)
        self.assertEqual(created, [("other", "o", "us-central1")])
        self.assertFalse(report["create_pass_ran"])

    def test_only_the_management_projects_own_listing_reconciles_the_roster(self):
        report, created, _ = self._run({"projects": ["other"]},
                                       {self.MGMT: (None, rec.OUTCOME_UNREACHABLE), "other": [("other", "o", "us-central1")]})
        self.assertEqual(created, [("other", "o", "us-central1")])
        self.assertFalse(report["create_pass_ran"])

    def test_a_changed_management_project_keeps_the_old_ones_profiles(self):
        # RECONCILE_PROJECT removed or re-pointed, or the metadata server naming another project
        # (a fallback answer that disagrees is the unresolved case, tested below): the old
        # management project reads as dropped, and nothing in the declaration changed.
        self._write_previous([{"id": "old-mgmt", "via": ["management"], "state": rec.STATE_IN_SCOPE}])
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-x"], identities={"cluster-x": _identity("old-mgmt", "x")})
        self.assertEqual(deleted, [])
        self.assertEqual(report["unmanaged"], ["cluster-x"])
        self.assertEqual(self._snapshot()["unmanaged"][0]["reason"],
                         "was the management project until this run; retiring, pruned on the next clean run")
        self.assertEqual([p["id"] for p in self._snapshot()["projects"] if p["state"] == rec.STATE_RETIRING], ["old-mgmt"])
        # The next clean run prunes it like any dropped project, unless it is named in the scope.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-x"], identities={"cluster-x": _identity("old-mgmt", "x")})
        self.assertEqual(deleted, ["cluster-x"])
        self.assertEqual(report["retiring"], [])

    def test_a_fallback_answer_that_disagrees_with_the_previous_run_is_treated_as_unresolved(self):
        # Metadata timed out and the broker's gcloud config named another project: two such
        # ticks in a row must not retire and then prune the real management project's profiles.
        mgmt_profiles = {"cluster-m1": _identity(self.MGMT, "m1")}
        self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                  profiles=["cluster-m1"], identities=mgmt_profiles)
        for _ in range(2):
            report, created, deleted = self._run({"projects": []}, {"other-proj": []}, management="other-proj",
                                                 profiles=["cluster-m1"], identities=mgmt_profiles, authoritative=False)
            self.assertEqual((deleted, created), ([], []))
            self.assertEqual(report["retiring"], [])
            self.assertFalse(report["create_pass_ran"])
            rows = {p["id"]: (p["via"], p["outcome"]) for p in self._snapshot()["projects"]}
            self.assertEqual(rows, {self.MGMT: ([rec.VIA_MANAGEMENT], rec.OUTCOME_UNREACHABLE)})
        # Metadata is back: the management project lists again and nothing was lost.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                                       profiles=["cluster-m1"], identities=mgmt_profiles)
        self.assertEqual(deleted, [])
        self.assertEqual(report["kept"], ["cluster-m1"])

    def test_a_fallback_answer_on_the_first_run_is_the_management_project(self):
        # No previous snapshot: nothing to disagree with, so the fallback answer stands.
        report, created, _ = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                                       authoritative=False)
        self.assertTrue(report["create_pass_ran"])
        self.assertEqual(created, [(self.MGMT, "m1", "us-central1")])

    def test_a_retiring_project_re_declared_before_the_second_run_returns_to_scope(self):
        self._write_previous([{"id": "p", "via": ["explicit"], "state": rec.STATE_IN_SCOPE}])
        ids = {"cluster-p": _identity("p", "p1")}
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-p"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), ([], ["p"]))
        report, _, deleted = self._run({"projects": ["p"]}, {self.MGMT: [], "p": [("p", "p1", "us-central1")]},
                                       profiles=["cluster-p"], identities=ids)
        self.assertEqual((deleted, report["retiring"], report["unmanaged"]), ([], [], []))
        rows = {q["id"]: (q["state"], q["via"]) for q in self._snapshot()["projects"]}
        self.assertEqual(rows["p"], (rec.STATE_IN_SCOPE, [rec.VIA_EXPLICIT]))
        # Dropped again: the count starts over, retiring rather than pruned.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []}, profiles=["cluster-p"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), ([], ["p"]))

    def test_a_glob_newly_matching_an_explicit_project_retires_it_over_two_runs(self):
        ids = {"cluster-s": _identity("team-scratch", "s1")}
        self._run({"projects": ["team-scratch"]}, {self.MGMT: [], "team-scratch": [("team-scratch", "s1", "us-central1")]},
                  profiles=["cluster-s"], identities=ids)
        scope = {"projects": ["team-scratch"], "exclude": {"projects": ["*-scratch"]}}
        report, _, deleted = self._run(scope, {self.MGMT: []}, profiles=["cluster-s"], identities=ids)
        self.assertEqual((deleted, report["retiring"], report["unmanaged"]), ([], ["team-scratch"], ["cluster-s"]))
        report, _, deleted = self._run(scope, {self.MGMT: []}, profiles=["cluster-s"], identities=ids)
        self.assertEqual((deleted, report["retiring"]), (["cluster-s"], []))

    def test_a_changed_management_project_named_in_the_scope_stays_managed(self):
        self._write_previous([{"id": "old-mgmt", "via": ["management"], "state": rec.STATE_IN_SCOPE}])
        report, _, deleted = self._run({"projects": ["old-mgmt"]}, {self.MGMT: [], "old-mgmt": []},
                                       profiles=["cluster-x"], identities={"cluster-x": _identity("old-mgmt", "x")})
        self.assertEqual(deleted, [])
        self.assertEqual(report["unmanaged"], [])
        self.assertEqual(report["retiring"], [])

    def test_a_retiring_project_stays_retiring_on_a_tick_that_could_not_read_the_declaration(self):
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        self._write_scope("{not json")
        report, _, deleted = self._run(None, {self.MGMT: []},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual(deleted, [])
        self.assertEqual(report["retiring"], ["gone"])
        rows = [(p["id"], p["state"]) for p in self._snapshot()["projects"] if p["id"] == "gone"]
        self.assertEqual(rows, [("gone", rec.STATE_RETIRING)])
        # The next clean run prunes: the unreadable tick did not restart the count.
        report, _, deleted = self._run({"projects": []}, {self.MGMT: []},
                                       profiles=["cluster-g"], identities={"cluster-g": _identity("gone", "g")})
        self.assertEqual(deleted, ["cluster-g"])

    def test_dry_run_deletes_nothing_and_writes_no_snapshot(self):
        self._write_previous([{"id": "gone", "state": rec.STATE_RETIRING}])
        before = (self.homes / rec.SNAPSHOT_FILE).read_bytes()
        report, created, deleted = self._run(
            {"projects": [], "exclude": {"clusters": [{"projectId": self.MGMT, "location": "us-central1", "clusterName": "x"}]}},
            {self.MGMT: [(self.MGMT, "new", "us-central1"), (self.MGMT, "x", "us-central1")]},
            profiles=["cluster-g", "cluster-x"],
            identities={"cluster-g": _identity("gone", "g"), "cluster-x": _identity(self.MGMT, "x")},
            dry_run=True)
        self.assertEqual((deleted, created), ([], []))
        self.assertEqual(sorted(report["pruned"]), ["cluster-g", "cluster-x"])
        self.assertEqual(report["created"], ["new/us-central1"])
        self.assertEqual(report["retiring"], ["gone"])
        self.assertEqual((self.homes / rec.SNAPSHOT_FILE).read_bytes(), before)

    def test_an_unresolved_tick_keeps_naming_the_management_project_so_the_next_tick_cannot_prune_it(self):
        mgmt_profiles = {"cluster-m1": _identity(self.MGMT, "m1")}
        # Tick 1: normal.
        self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                  profiles=["cluster-m1"], identities=mgmt_profiles)
        # Tick 2: the management project cannot be resolved.
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        report, _, deleted = self._run({"projects": []}, {}, management=None,
                                       profiles=["cluster-m1"], identities=mgmt_profiles)
        self.assertEqual(deleted, [])
        self.assertEqual(report["retiring"], [])
        carried = next(p for p in self._snapshot()["projects"] if p["id"] == self.MGMT)
        self.assertEqual((carried["via"], carried["outcome"], carried["state"]),
                         (["management"], rec.OUTCOME_UNREACHABLE, rec.STATE_IN_SCOPE))
        # Tick 3: resolved again; nothing was dropped, nothing is pruned.
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        report, _, deleted = self._run({"projects": []}, {self.MGMT: [(self.MGMT, "m1", "us-central1")]},
                                       profiles=["cluster-m1"], identities=mgmt_profiles)
        self.assertEqual(deleted, [])
        self.assertEqual(report["kept"], ["cluster-m1"])
        # Tick 3': resolved to a different project instead; the old one is kept, not pruned.
        os.environ.pop(rec.SCOPE_FILE_ENV, None)
        report, _, deleted = self._run({"projects": []}, {"other-mgmt": []}, management="other-mgmt",
                                       profiles=["cluster-m1"], identities=mgmt_profiles)
        self.assertEqual(deleted, [])
        self.assertEqual(report["unmanaged"], ["cluster-m1"])

    def test_a_carried_forward_management_project_that_is_also_explicit_is_listed_once(self):
        self._write_previous([{"id": "p1", "via": ["management"], "state": rec.STATE_IN_SCOPE}])
        report, created, _ = self._run({"projects": ["p1"]}, {"p1": [("p1", "c", "us-central1")]}, management=None)
        rows = [p for p in self._snapshot()["projects"] if p["id"] == "p1"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["via"], ["explicit", "management"])
        self.assertEqual(rows[0]["outcome"], rec.OUTCOME_OK)
        self.assertEqual(created, [("p1", "c", "us-central1")])
        self.assertEqual(report["projects"], {"p1": rec.OUTCOME_OK})


if __name__ == "__main__":
    unittest.main()
