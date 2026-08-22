"""Unit tests for cluster_agent_reconcile — the orphaned-profile pruner.

Run: python3 -m unittest agents.platform.scripts.test_cluster_agent_reconcile

The safety-critical invariant under test: a profile is deleted ONLY on a definitive
GKE NotFound. Missing identity, transient errors, and reserved profiles are never
deleted.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_agent_profile as cap  # noqa: E402
import cluster_agent_reconcile as rec  # noqa: E402


def _identity(project="p", cluster="c", location="us-central1"):
    return {"project": project, "cluster": cluster, "location": location}


def _home_factory(root: Path, incomplete=frozenset()):
    """A ``profile_home`` stub over real directories.

    reconcile() reads the filesystem to tell a finished scaffold from one killed
    after the identity stamp, so these have to exist. Names in ``incomplete`` get
    the directory without the artifacts ``create_profile`` writes last.
    """
    def factory(name: str) -> Path:
        home = root / name
        home.mkdir(parents=True, exist_ok=True)
        if name not in incomplete:
            for artifact in rec.SCAFFOLD_ARTIFACTS:
                (home / artifact).touch()
        return home

    return factory


class HomesMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.homes = Path(self._tmp.name)


class ReconcileTest(HomesMixin):
    def _run(self, profiles, identities, existence, dry_run=False):
        """Drive reconcile() with stubbed enumeration/identity/liveness/delete.

        identities: name -> identity dict or None
        existence:  name -> True/False/None (cluster exists / gone / unknown)
        Returns (report, list_of_deleted_names).
        """
        deleted: list[str] = []
        # _project() -> None disables the CREATE direction, isolating the prune behavior
        # under test (and avoiding any real metadata/gcloud calls).
        with mock.patch.object(rec, "_project", return_value=None), \
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
        with mock.patch.object(rec, "_project", return_value="p"), \
             mock.patch.object(rec, "_all_clusters", return_value=[
                 ("p", "alpha", "us-central1"),   # no profile -> CREATE
                 ("p", "beta", "us-central1"),    # already has a profile -> skip
                 ("p", "mgmt", "us-central1"),    # the management cluster -> CREATE too
             ]), \
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
        with mock.patch.object(rec, "_project", return_value="p"), \
             mock.patch.object(rec, "EXTRA_EXCLUDE", {"skipme"}), \
             mock.patch.object(rec, "_all_clusters", return_value=[("p", "skipme", "us-central1")]), \
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
        with mock.patch.object(rec, "_project", return_value="p"), \
             mock.patch.object(rec, "EXTRA_EXCLUDE", {"skipme"}), \
             mock.patch.object(rec, "_all_clusters", return_value=[
                 ("p", "keep", "us-central1"), ("p", "skipme", "us-central1")]), \
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
        with mock.patch.object(rec, "_project", return_value="p"), \
             mock.patch.object(rec, "_all_clusters", return_value=[("p", "beta", "us-central1")]), \
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
        self.assertEqual(rec._scaffold_gaps(home), list(rec.SCAFFOLD_ARTIFACTS))
        (home / "kubeconfig.yaml").touch()
        self.assertEqual(rec._scaffold_gaps(home), ["USER.md"])
        (home / "USER.md").touch()
        self.assertEqual(rec._scaffold_gaps(home), [])


class AllClustersTest(unittest.TestCase):
    """A failed `gcloud list` must be loud and must not look like an empty project."""

    def test_parses_name_and_location(self):
        done = subprocess.CompletedProcess([], 0, stdout="a us-central1\nb europe-west1\n")
        with mock.patch.object(rec.subprocess, "run", return_value=done):
            self.assertEqual(
                rec._all_clusters("p"),
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
            self.assertIsNone(rec._all_clusters("p"))
        self.assertIn("Reauthentication required", " ".join(str(c) for c in logged.call_args_list))

    def test_uses_check_true_so_nonzero_exit_cannot_pass_silently(self):
        seen = {}
        with mock.patch.object(rec.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="")
            rec._all_clusters("p")
            seen = run.call_args.kwargs
        self.assertTrue(seen.get("check"), "gcloud list must run with check=True")

    def test_timeout_degrades_to_empty_and_logs(self):
        with mock.patch.object(rec.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["gcloud"], 120)), \
             mock.patch.object(rec, "log") as logged:
            self.assertIsNone(rec._all_clusters("p"))
        self.assertTrue(logged.called)


class CreatePassSignalTest(unittest.TestCase):
    """`create_pass_ran` is the only honest answer to "is the roster reconciled?".

    Everything in this script is caught and logged so the cron path always exits 0,
    which leaves the exit code saying nothing. The bootstrap scan gate has to know
    the difference: filing its sweep against a roster the CREATE pass never touched
    produces a solo audit that `.bootstrap_scan_filed` then makes permanent.
    """

    def _reconcile(self, clusters):
        with mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "_project", return_value="p"), \
             mock.patch.object(rec, "_all_clusters", return_value=clusters), \
             mock.patch.object(rec, "log"):
            return rec.reconcile(dry_run=True)

    def test_an_empty_project_still_counts_as_reconciled(self):
        self.assertTrue(self._reconcile([])["create_pass_ran"])

    def test_a_failed_cluster_list_does_not(self):
        self.assertFalse(self._reconcile(None)["create_pass_ran"])

    def test_an_unresolvable_project_does_not(self):
        with mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "_project", return_value=None), \
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
        with mock.patch.object(rec, "list_profiles", return_value=[]), \
             mock.patch.object(rec, "_project", return_value="p"), \
             mock.patch.object(rec, "_all_clusters", return_value=[("p", "alpha", "us-central1")]), \
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


if __name__ == "__main__":
    unittest.main()
