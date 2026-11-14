"""fleet_scope_targets: the declared scope's resolved projects for an audit.

Run: python3 -m unittest discover -s agents/platform/scripts -p 'test_fleet_scope_targets.py' -v
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fleet_scope_targets as fst  # noqa: E402

DECLARED = {"projects": ["payments-prod"], "folders": [], "organizations": [], "sharedVpcHosts": [], "metricsScopes": [], "exclude": {}}


def _snapshot(projects, declared=DECLARED, resolved_at="2026-10-09T12:00:00Z", present=None, boundary=None):
    snapshot = {"resolvedAt": resolved_at, "declared": declared, "maxProjects": 100, "projects": projects}
    if present is not None:
        snapshot["present"] = present
    # The reconcile writes `boundary` beside `present`, and a block read this
    # run is a boundary; a carried or absent block says which it is itself.
    if boundary is None and present is True:
        boundary = True
    if boundary is not None:
        snapshot["boundary"] = boundary
    return snapshot


class DeclaredScopeTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        # The reader consults the operator's render and the project env when no
        # snapshot answers; neither may leak in from the shell running the tests.
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in (fst.SCOPE_FILE_ENV, *fst.MANAGEMENT_PROJECT_ENVS):
            os.environ.pop(name, None)

    def _write(self, snapshot) -> None:
        (self.home / fst.SNAPSHOT_FILE).write_text(json.dumps(snapshot), encoding="utf-8")

    def test_no_snapshot_is_none_so_the_caller_lists_as_before(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fst.SCOPE_FILE_ENV, None)
            self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_no_snapshot_but_a_render_with_a_block_is_the_host_alone_until_the_first_tick(self):
        # A fresh install has the operator's render from the first second and
        # no snapshot until the reconcile's first tick; the audits must not
        # list every visible project in that window.
        render = self.home / "scope.json"
        render.write_text(json.dumps({"present": True, "projects": ["payments-prod"], "exclude": {"projects": [], "clusters": []}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            targets = fst.declared_scope_targets(self.home)
            # The render's declared projects ride as unread, so the host-only
            # sweep publishes as partial rather than resolving their findings.
            self.assertEqual((targets.projects, targets.unread, targets.resolved_at), (("ops-mgmt",), (("declared-scope", "unresolved"), ("payments-prod", "unresolved")), None))
            self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt --scope-unread declared-scope=unresolved,payments-prod=unresolved")
        # A render that names a folder and no project is partial too: the fixed
        # row rides whatever the render names, so a complete host-only sweep
        # never publishes from a render answer.
        render.write_text(json.dumps({"present": True, "projects": [], "folders": ["folders/1"]}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            targets = fst.declared_scope_targets(self.home)
            self.assertEqual(targets.unread, (("declared-scope", "unresolved"),))
            self.assertIn("until the next reconcile", targets.note)
        # A named render that cannot be read is a fault, not "no scope".
        render.write_text("<not json>", encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            with self.assertRaises(fst.ScopeRenderUnreadable):
                fst.declared_scope_targets(self.home)
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(self.home / "absent.json"), "GCP_PROJECT_ID": "ops-mgmt"}):
            with self.assertRaises(fst.ScopeRenderUnreadable):
                fst.declared_scope_targets(self.home)
        # A snapshot that says boundary: false (a readable render with no block
        # last tick) yields to a render that has gained a block since.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": []}}
        snap = _snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}], declared=empty, present=False, boundary=False)
        self._write(snap)
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            render.write_text(json.dumps({"present": True, "projects": ["payments-prod"]}), encoding="utf-8")
            self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))
        render.write_text(json.dumps({"present": False}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_the_boundary_key_decides_when_the_reconcile_wrote_it(self):
        # A never-declared install on an unreadable tick: present false, empty
        # lists, boundary false. Nothing is carried; no scope.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": []}}
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}], declared=empty, present=False, boundary=False))
        self.assertIsNone(fst.declared_scope_targets(self.home))
        # The stock install on the same tick: the reconcile carried its boundary.
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}], declared=empty, present=False, boundary=True))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_file_that_is_not_a_snapshot_is_none(self):
        (self.home / fst.SNAPSHOT_FILE).write_text("<not json>", encoding="utf-8")
        self.assertIsNone(fst.declared_scope_targets(self.home))
        (self.home / fst.SNAPSHOT_FILE).write_text(json.dumps({"projects": "no"}), encoding="utf-8")
        self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_an_install_that_declares_no_scope_is_none(self):
        # No scope block at all: no boundary was drawn, so the audit keeps the
        # listing it had. The reconcile records that as present: false; a
        # snapshot that predates the flag is read by its empty lists.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": ["*-sandbox"]}}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty, present=False))
        self.assertIsNone(fst.declared_scope_targets(self.home))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fst.SCOPE_FILE_ENV, None)
            self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty))
            self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_a_present_block_with_empty_lists_is_the_host_only_boundary(self):
        # spec.scope: {projects: [], exclude: {projects: ["*-sandbox"]}} bounds
        # discovery to the management project; the audits follow it rather
        # than listing every project the identity can see.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": ["*-sandbox"]}}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty, present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.projects, ("ops-mgmt",))
        self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt")

    def test_a_declared_project_with_no_row_rides_as_unread(self):
        # The carried tick writes rows for the management project and the
        # projects holding a profile; a declared project with no cluster has
        # none, and must not vanish from a sweep that then reads as complete.
        declared = dict(DECLARED, projects=["payments-prod", "payments-net"])
        snap = _snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
                          {"id": "payments-prod", "outcome": "ok", "state": "in-scope"}], declared=declared, present=False)
        snap["boundary"] = True
        self._write(snap)
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.projects, ("ops-mgmt", "payments-prod"))
        self.assertEqual(targets.unread, (("payments-net", "unresolved"),))

    def test_the_render_answer_honours_the_renders_own_exclusions(self):
        render = self.home / "scope.json"
        render.write_text(
            json.dumps({"present": True, "projects": ["payments-prod", "team-sandbox"], "folders": ["123456789012"], "metricsScopes": ["ops-mon"],
                        "exclude": {"projects": ["*-sandbox"], "clusters": []}}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            targets = fst.declared_scope_targets(self.home)
            self.assertEqual(targets.unread, (("declared-scope", "unresolved"), ("payments-prod", "unresolved")))
            self.assertEqual(targets.excluded, ("*-sandbox",))
            self.assertEqual(targets.declared_containers, ("folders/123456789012", "metricsScopes/ops-mon"))

    def test_a_numeric_host_is_not_handed_on_by_the_render_answer(self):
        render = self.home / "scope.json"
        render.write_text(json.dumps({"present": True, "projects": []}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "123456789012"}):
            targets = fst.declared_scope_targets(self.home)
            self.assertEqual(targets.projects, ())
            self.assertEqual(targets.collector_args(), "--scope-unread declared-scope=unresolved")
            self.assertIn("known here by number only", targets.note, "the note must not say the host is swept when nothing is")

    def test_the_management_project_is_swept_whatever_the_reconciles_listing_said(self):
        # One failed `clusters list` at the tick must not stop every audit for
        # an hour: the collectors read the host themselves and record failures.
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "unreachable", "state": "in-scope"}], declared=dict(DECLARED, projects=[]), present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual((targets.projects, targets.unread), (("ops-mgmt",), ()))

    def test_an_excluded_explicit_project_is_not_unread(self):
        # The reconcile writes no row for an explicit project an exclude
        # pattern matches; the operator left it out, so it is not a coverage gap.
        declared = dict(DECLARED, projects=["payments-prod", "team-sandbox", "legacy"], exclude={"projects": ["*-sandbox", "legacy"], "clusters": []})
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
                               {"id": "payments-prod", "outcome": "ok", "state": "in-scope"}], declared=declared, present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual((targets.projects, targets.unread), (("ops-mgmt", "payments-prod"), ()))
        # The entries ride on the targets, so a reader that meets such a
        # project under another name (a profile still on the volume) can tell
        # it from one the declaration never named; the match is the reader's.
        self.assertEqual(targets.excluded, ("*-sandbox", "legacy"))
        self.assertTrue(targets.excludes("team-sandbox"))
        self.assertTrue(targets.excludes("legacy"))
        self.assertFalse(targets.excludes("payments-prod"))
        self.assertFalse(targets.excludes("legacy-2"))
        # Excluded by number: only the reconcile can match that, and it writes
        # the dropped ids for the reader.
        declared = dict(DECLARED, projects=["payments-prod", "team-prod"], exclude={"projects": ["123456789012"], "clusters": []})
        snap = _snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
                          {"id": "payments-prod", "outcome": "ok", "state": "in-scope"}], declared=declared, present=True)
        snap["excludedProjects"] = ["team-prod"]
        self._write(snap)
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.unread, ())
        self.assertEqual(targets.excluded, ("123456789012", "team-prod"))
        self.assertTrue(targets.excludes("team-prod"))

    def test_a_carried_tick_on_a_folder_scoped_install_is_partial(self):
        # A carried tick resolves no folder, organisation or selector; a
        # declaration that names one cannot publish a complete sweep from it.
        declared = dict(DECLARED, projects=[], folders=["123456789012"])
        snap = _snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=declared, present=False)
        snap["boundary"] = True
        self._write(snap)
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.projects, ("ops-mgmt",))
        self.assertEqual(targets.unread, (("declared-scope", "unresolved"),))
        # On a readable tick the folder's members have rows, and nothing rides.
        snap = _snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
                          {"id": "member-a", "via": ["folders/123456789012"], "outcome": "ok", "state": "in-scope"}], declared=declared, present=True)
        self._write(snap)
        self.assertEqual(fst.declared_scope_targets(self.home).unread, ())

    def test_a_declared_scope_yields_the_ok_projects_in_snapshot_order(self):
        self._write(_snapshot([
            {"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "via": ["explicit"], "outcome": "ok", "state": "in-scope"},
            {"id": "payments-staging", "via": ["explicit"], "outcome": "denied", "state": "in-scope"},
            {"id": "legacy-api", "via": ["folders/1"], "outcome": "api-disabled", "state": "in-scope"},
            {"id": "payments-legacy", "via": [], "outcome": "ok", "state": "retiring"},
        ]))
        targets = fst.declared_scope_targets(self.home)
        self.assertIsNotNone(targets)
        # api-disabled is swept (counted empty by the GKE collectors, read by the
        # Compute and networking ones); denied is a declared project unread.
        self.assertEqual(targets.projects, ("ops-mgmt", "payments-prod", "legacy-api"))
        self.assertEqual(targets.unread, (("payments-staging", "denied"),))
        # The retiring row is a project the declaration no longer names, kept
        # until its profiles are pruned: not swept, not unread, and carried by
        # id for a reader that meets the project under another name.
        self.assertEqual(targets.retiring, ("payments-legacy",))
        self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt,payments-prod,legacy-api --scope-unread payments-staging=denied")
        self.assertEqual(targets.resolved_at, "2026-10-09T12:00:00Z")
        self.assertEqual(targets.path, str(self.home / fst.SNAPSHOT_FILE))

    def test_a_folder_alone_is_a_declared_scope(self):
        declared = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"folders": ["123456789012"]}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=declared))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_failed_lookup_with_no_carried_members_is_partial_on_a_readable_tick(self):
        # The first tick after a folder is declared, with its search refused:
        # the reconcile carries no members (it never had any), records the
        # lookup under its outcome, and the sweep must not publish complete
        # over a fleet nobody looked at.
        declared = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"folders": ["123456789012"]}
        host = {"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}
        snap = _snapshot([host], declared=declared, present=True)
        snap["containers"] = [{"id": "folders/123456789012", "outcome": "denied", "projects": 0}]
        self._write(snap)
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual((targets.projects, targets.unread), (("ops-mgmt",), (("declared-scope", "unresolved"),)))
        self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt --scope-unread declared-scope=unresolved")
        self.assertEqual(targets.declared_containers, ("folders/123456789012",))
        # A lookup that succeeded and found nothing is a resolved, empty folder.
        snap["containers"] = [{"id": "folders/123456789012", "outcome": "ok", "projects": 0}]
        self._write(snap)
        self.assertEqual(fst.declared_scope_targets(self.home).unread, ())
        # A failed lookup that carried members: they ride under its outcome,
        # and the fixed row says the rest of the folder is unknown too.
        snap["containers"] = [{"id": "folders/123456789012", "outcome": "unreachable", "projects": 1}]
        snap["projects"] = [host, {"id": "payments-prod", "via": ["folders/123456789012"], "outcome": "unreachable", "state": "in-scope", "frozen": True}]
        self._write(snap)
        self.assertEqual(fst.declared_scope_targets(self.home).unread, (("payments-prod", "unreachable"), ("declared-scope", "unresolved")))
        # An over-cap lookup resolved: the members all have rows at over-cap,
        # which is the whole gap by name, so the fixed row does not ride.
        snap["containers"] = [{"id": "folders/123456789012", "outcome": "over-cap", "projects": 2}]
        snap["projects"] = [host] + [{"id": p, "via": ["folders/123456789012"], "outcome": "over-cap", "state": "in-scope"} for p in ("m1", "m2")]
        self._write(snap)
        self.assertEqual(fst.declared_scope_targets(self.home).unread, (("m1", "over-cap"), ("m2", "over-cap")))

    def test_the_declared_containers_ride_as_the_reconcile_keys_their_lookups(self):
        # The folders, organisations and selectors the declaration names, as
        # the snapshot's `containers[].id` spells them, sorted, so a reader
        # that holds a project's cause across a failed lookup can tell a
        # container the declaration had from one it has gained; never in the
        # sweep or the collector flags, and none on a declaration of projects.
        declared = dict(DECLARED, projects=[], folders=["222222222222", "111111111111"], organizations=["333333333333"], sharedVpcHosts=["net-host"], metricsScopes=["ops-mon"])
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=declared, present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(
            targets.declared_containers,
            ("folders/111111111111", "folders/222222222222", "metricsScopes/ops-mon", "organizations/333333333333", "sharedVpcHosts/net-host"),
        )
        self.assertEqual(targets.collector_argv(), ("--scope-projects", "ops-mgmt"))
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=dict(DECLARED, folders="123"), present=True))
        self.assertEqual(fst.declared_scope_targets(self.home).declared_containers, ())

    def test_collector_args_without_an_unread_project_carries_the_sweep_alone(self):
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}], declared=dict(DECLARED, projects=[]), present=True))
        self.assertEqual(fst.declared_scope_targets(self.home).collector_args(), "--scope-projects ops-mgmt")

    def test_a_carried_declaration_under_an_unreadable_render_is_still_a_boundary(self):
        # A tick that cannot read the render keeps the last declaration and
        # writes present: false, boundary: true; the audits keep the boundary
        # too rather than listing every visible project.
        self._write(_snapshot([
            {"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "outcome": "ok", "state": "in-scope"},
        ], present=False, boundary=True))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt", "payments-prod"))
        # A snapshot from before the boundary key is read by its lists.
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}]))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_carried_host_only_boundary_under_an_unreadable_render_stays_host_only(self):
        # The stock install's unreadable tick: present false, empty lists,
        # boundary carried from the tick that read the block.
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": []}}
        self._write(_snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}], declared=empty, present=False, boundary=True))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_a_block_removed_from_a_readable_cr_is_no_boundary_whatever_is_carried(self):
        # The operator removed spec.scope: the render is readable and carries
        # no block. The reconcile keeps re-persisting the last declaration
        # (removing the block retires nothing), so the lists stay non-empty for
        # good; the audits must not read that as a boundary, or they would
        # shrink to the host and pin every run partial indefinitely.
        self._write(_snapshot([
            {"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "outcome": "unreachable", "state": "in-scope"},
        ], present=False, boundary=False))
        self.assertIsNone(fst.declared_scope_targets(self.home))

    def test_nothing_readable_still_hands_the_collector_the_unread_flag(self):
        # The unread flag alone tells the collector a scope was declared, so a
        # verbatim paste of collector_args cannot widen the run to the listing.
        self._write(_snapshot([{"id": "payments-prod", "outcome": "denied", "state": "in-scope"}]))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.projects, ())
        self.assertEqual(targets.collector_args(), "--scope-unread payments-prod=denied")
        # Every declared project has a row here, so nothing else rides as unread.
        # A declared scope with no row at all still hands the collector something
        # that says "declared": an empty string would have the guard tell the
        # agent to call the tool it has just called.
        self._write(_snapshot([], declared=dict(DECLARED, projects=[]), present=True))
        self.assertEqual(fst.declared_scope_targets(self.home).collector_args(), "--scope-unread declared-scope=unresolved")

    def test_rows_without_an_id_or_without_a_state_are_handled(self):
        self._write(_snapshot([{"outcome": "ok"}, {"id": "ops-mgmt", "outcome": "ok"}, "junk"]))
        self.assertEqual(fst.declared_scope_targets(self.home).projects, ("ops-mgmt",))

    def test_the_data_root_is_read_from_platform_agent_home_not_hermes_home(self):
        # A platform worker runs with HERMES_HOME at the profile home beneath the
        # data root; the snapshot sits at the root, which PLATFORM_AGENT_HOME names.
        self._write(_snapshot([{"id": "ops-mgmt", "outcome": "ok", "state": "in-scope"}]))
        profile_home = self.home / "profiles" / "platform"
        profile_home.mkdir(parents=True)
        with mock.patch.dict(os.environ, {fst.AGENT_HOME_ENV: str(self.home), "HERMES_HOME": str(profile_home)}):
            self.assertEqual(fst.declared_scope_targets().projects, ("ops-mgmt",))
        empty_default = self.home / "empty-default"
        empty_default.mkdir()
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False), mock.patch.object(fst, "DEFAULT_AGENT_HOME", str(empty_default)):
            os.environ.pop(fst.AGENT_HOME_ENV, None)
            self.assertIsNone(fst.declared_scope_targets(), "HERMES_HOME must not be the key: on the pod it names the profile home")
        self.assertEqual(fst.snapshot_path("/elsewhere"), pathlib.Path("/elsewhere") / fst.SNAPSHOT_FILE)

    # spec.scope.exclude.clusters rides to the collectors (#3081).
    EXCLUDE_CLUSTERS = {"projects": [], "clusters": [
        {"projectId": "payments-prod", "location": "us-central1", "clusterName": "lane-b"},
        {"projectId": "ops-mgmt", "location": "us-east1-b", "clusterName": "kyber-test"},
    ]}
    EXCLUDE_ROWS = [{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
            {"id": "payments-prod", "via": ["explicit"], "outcome": "ok", "state": "in-scope"}]

    def test_the_snapshot_declaration_hands_the_excluded_clusters_on(self):
        self._write(_snapshot(self.EXCLUDE_ROWS, declared=dict(DECLARED, exclude=self.EXCLUDE_CLUSTERS), present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(targets.excluded_clusters, (("ops-mgmt", "us-east1-b", "kyber-test"), ("payments-prod", "us-central1", "lane-b")))
        self.assertEqual(
            targets.collector_args(),
            "--scope-projects ops-mgmt,payments-prod --scope-exclude-clusters ops-mgmt/us-east1-b/kyber-test,payments-prod/us-central1/lane-b",
        )

    def test_the_render_answer_hands_the_excluded_clusters_on(self):
        render = self.home / "scope.json"
        render.write_text(json.dumps({"present": True, "projects": [], "exclude": self.EXCLUDE_CLUSTERS}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            targets = fst.declared_scope_targets(self.home)
        self.assertEqual(len(targets.excluded_clusters), 2)
        self.assertIn("--scope-exclude-clusters ops-mgmt/us-east1-b/kyber-test,payments-prod/us-central1/lane-b", targets.collector_args())

    def test_an_entry_admission_would_refuse_is_skipped_so_the_args_still_parse(self):
        exclude = {"clusters": [*self.EXCLUDE_CLUSTERS["clusters"], {"projectId": "p", "location": "x/y", "clusterName": "c"},
                                {"projectId": "p", "location": "l"}, "not-a-dict"]}
        self._write(_snapshot(self.EXCLUDE_ROWS, declared=dict(DECLARED, exclude=exclude), present=True))
        targets = fst.declared_scope_targets(self.home)
        self.assertEqual(len(targets.excluded_clusters), 2)
        fst.fleet_scope_args.parse_scope_exclude_clusters(targets.collector_args().split("--scope-exclude-clusters ")[1])

    def test_no_exclusion_adds_no_flag(self):
        self._write(_snapshot(self.EXCLUDE_ROWS, present=True))
        self.assertNotIn("--scope-exclude-clusters", fst.declared_scope_targets(self.home).collector_args())

    def test_management_project_id_reads_the_management_row_regardless_of_boundary(self):
        self.assertIsNone(fst.management_project_id(self.home))
        (self.home / fst.SNAPSHOT_FILE).write_text("<not json>", encoding="utf-8")
        self.assertIsNone(fst.management_project_id(self.home))
        empty = {key: [] for key in fst.DECLARED_SCOPE_KEYS} | {"exclude": {"projects": []}}
        self._write(_snapshot(
            [{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"}],
            declared=empty, present=False, boundary=False,
        ))
        self.assertEqual(fst.management_project_id(self.home), "ops-mgmt")
        self._write(_snapshot(
            [{"id": "old-mgmt", "via": ["management"], "outcome": "ok", "state": "retiring"},
             {"id": "123456789012", "via": ["management"], "outcome": "ok", "state": "in-scope"}],
            present=True,
        ))
        self.assertIsNone(fst.management_project_id(self.home))


class CollectorArgvTest(unittest.TestCase):
    def test_the_argv_form_is_the_pasted_line_tokenised(self):
        targets = fst.ScopeTargets(projects=("ops-mgmt", "payments-prod"), unread=(("billing", "denied"),), resolved_at=None, path="x")
        self.assertEqual(targets.collector_argv(), ("--scope-projects", "ops-mgmt,payments-prod", "--scope-unread", "billing=denied"))
        self.assertEqual(targets.collector_args(), "--scope-projects ops-mgmt,payments-prod --scope-unread billing=denied")
        nothing = fst.ScopeTargets(projects=(), unread=(), resolved_at=None, path="x")
        self.assertEqual(nothing.collector_argv(), ("--scope-unread", "declared-scope=unresolved"))
        self.assertEqual(nothing.collector_args(), "--scope-unread declared-scope=unresolved")
        # The exclude entries never reach the collector flags: the sweep and the unread rows are the whole argv.
        excluding = fst.ScopeTargets(projects=("ops-mgmt",), unread=(), resolved_at=None, path="x", excluded=("*-sandbox",), declared_containers=("folders/1",), retiring=("legacy",))
        self.assertEqual(excluding.collector_argv(), ("--scope-projects", "ops-mgmt"))



def _identity_config(project, cluster, location):
    return f"cluster_identity:\n  project: {project}\n  cluster: {cluster}\n  location: {location}\n"


def _resolved(rows, clusters=None, state="ok", excluded=(), version=1):
    document = {"v": version, "resolvedAt": "2026-11-10T12:00:00Z", "state": state, "memberHash": "sha256:x",
                "excludedProjects": list(excluded),
                "projects": [{"id": p, "via": ["folders/1"], "outcome": o, "confirmedAt": "2026-11-10T12:00:00Z"} for p, o in rows]}
    if clusters is not None:
        document["clusters"] = clusters
    return document


class ListScopeClustersTest(unittest.TestCase):
    """list_scope_clusters (#5621): the resolved scope under spec.mode: next,
    the profiles walk otherwise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in (fst.SCOPE_FILE_ENV, fst.RESOLVED_SCOPE_FILE_ENV, *fst.MANAGEMENT_PROJECT_ENVS):
            os.environ.pop(name, None)
        # A profile that would give a different answer if anything walked it
        # in next mode.
        self._profile("cluster-ghost", "ghost-proj", "ghost", "us-east1")

    def _profile(self, name, project, cluster, location):
        home = self.home / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(_identity_config(project, cluster, location), encoding="utf-8")

    def _render(self, render):
        path = self.home / "scope.json"
        path.write_text(json.dumps(render), encoding="utf-8")
        os.environ[fst.SCOPE_FILE_ENV] = str(path)

    def _resolved_file(self, document):
        path = self.home / "resolved-scope.json"
        path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        os.environ[fst.RESOLVED_SCOPE_FILE_ENV] = str(path)
        return path

    def _next(self, document, render=None, management="mgmt"):
        render = render if render is not None else {"present": True, "projects": ["explicit-a"], "folders": ["1"]}
        self._render(render)
        if isinstance(document, dict) and document.get("memberHash") == "sha256:x":
            # Resolved for this declaration, as the publisher stamps it.
            import scope_publisher
            document = {**document, "memberHash": scope_publisher.member_hash(render)}
        self._resolved_file(document)
        return fst.list_scope_clusters(self.home, management_project=management)

    def test_members_come_from_the_clusters_map_and_live_projects_are_marked(self):
        got = self._next(_resolved([("member-a", "ok"), ("member-b", "ok")], clusters={
            "member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "web"], ["europe-west1", "api"]]},
            "member-b": {"at": "2026-11-09T08:00:00Z", "clusters": []},
        }))
        self.assertEqual(got.source, fst.SOURCE_RESOLVED_SCOPE)
        self.assertTrue(got.next_mode)
        by = got.by_project()
        self.assertEqual(list(by), ["mgmt", "explicit-a", "member-a", "member-b"])
        self.assertEqual(by["mgmt"].status, fst.STATUS_LIST_LIVE)
        self.assertEqual(by["explicit-a"].status, fst.STATUS_LIST_LIVE)
        # "List live" is never an empty list a caller could read as no clusters.
        self.assertIsNone(by["mgmt"].clusters)
        self.assertIsNone(by["explicit-a"].clusters)
        self.assertEqual(by["member-a"].status, fst.STATUS_LISTED)
        self.assertEqual(by["member-a"].clusters, (("member-a", "europe-west1", "api"), ("member-a", "us-central1", "web")))
        self.assertEqual(by["member-a"].at, "2026-11-10T12:00:00Z")
        # A member the probe listed empty is a real empty list, with its age.
        self.assertEqual(by["member-b"].clusters, ())
        self.assertEqual(by["member-b"].at, "2026-11-09T08:00:00Z")
        self.assertNotIn("ghost-proj", by)

    def test_management_falls_back_to_the_project_env(self):
        os.environ[fst.MANAGEMENT_PROJECT_ENVS[0]] = "env-mgmt"
        got = self._next(_resolved([]), management=None)
        self.assertEqual(got.by_project()["env-mgmt"].status, fst.STATUS_LIST_LIVE)

    def test_a_member_with_no_entry_is_unread_never_empty(self):
        got = self._next(_resolved([("member-a", "ok"), ("member-c", "denied")], clusters={
            "member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "web"]]},
        }))
        entry = got.by_project()["member-c"]
        self.assertEqual(entry.status, fst.STATUS_UNREAD)
        self.assertIsNone(entry.clusters)
        self.assertIn("denied", entry.reason)
        self.assertIsNone(got.note)

    def test_a_document_resolved_for_another_declaration_leaves_members_unread(self):
        # The gateway refuses a stale document; so does the reader, or a scope
        # edit would be read through the old membership until the next publish.
        document = _resolved([("member-a", "ok")], clusters={"member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "web"]]}})
        document["memberHash"] = "sha256:" + "0" * 64
        got = self._next(document)
        self.assertEqual(got.by_project()["member-a"].status, fst.STATUS_UNREAD)
        self.assertIn("another declaration", got.note)
        self.assertEqual(got.by_project()["explicit-a"].status, fst.STATUS_LIST_LIVE)

    def test_an_api_disabled_member_is_listed_empty(self):
        # A project whose clusters API is off has no GKE clusters: listed empty,
        # not unread, so every run doesn't read as partial.
        got = self._next(_resolved([("member-a", "api-disabled")]))
        entry = got.by_project()["member-a"]
        self.assertEqual(entry.status, fst.STATUS_LISTED)
        self.assertEqual(entry.clusters, ())

    def test_an_api_disabled_member_is_listed_empty_over_a_carried_entry(self):
        # The publisher keeps a member's previous entry whenever this run's
        # outcome is not ok, api-disabled included: the API being off now
        # outranks the clusters an older run listed.
        got = self._next(_resolved([("member-a", "api-disabled")], clusters={"member-a": {"at": "2026-11-01T12:00:00Z", "clusters": [["l", "gone"]]}}))
        entry = got.by_project()["member-a"]
        self.assertEqual(entry.status, fst.STATUS_LISTED)
        self.assertEqual(entry.clusters, ())
        self.assertNotEqual(entry.at, "2026-11-01T12:00:00Z")

    def test_a_malformed_entry_is_unread(self):
        got = self._next(_resolved([("member-a", "ok")], clusters={"member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["only-one"]]}}))
        self.assertEqual(got.by_project()["member-a"].status, fst.STATUS_UNREAD)

    def test_an_unusable_document_leaves_every_member_unread_and_says_why(self):
        cases = {
            "too-large": _resolved([], state="too-large"),
            "error": _resolved([("member-a", "ok")], clusters={"member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["l", "c"]]}}, state="error"),
            "version": _resolved([("member-a", "ok")], version=2),
            "unparseable": "{not json",
            "not an object": "[]",
        }
        for label, document in cases.items():
            with self.subTest(label):
                got = self._next(document)
                by = got.by_project()
                self.assertIsNotNone(got.note)
                self.assertEqual(by["mgmt"].status, fst.STATUS_LIST_LIVE)
                self.assertEqual(by["explicit-a"].status, fst.STATUS_LIST_LIVE)
                # The members no row names are one named gap, not "no members".
                self.assertEqual(by[fst.UNRESOLVED_SCOPE_ROW].status, fst.STATUS_UNREAD)
                self.assertFalse(any(e.status == fst.STATUS_LISTED for e in got.projects))
                if label == "error":
                    # The rows an error document carries are named unread, not listed.
                    self.assertEqual(by["member-a"].status, fst.STATUS_UNREAD)

    def test_a_missing_document_leaves_members_unread(self):
        self._render({"present": True, "projects": [], "folders": ["1"]})
        os.environ[fst.RESOLVED_SCOPE_FILE_ENV] = str(self.home / "absent.json")
        got = fst.list_scope_clusters(self.home, management_project="mgmt")
        self.assertEqual(got.source, fst.SOURCE_RESOLVED_SCOPE)
        self.assertIn("cannot be read", got.note)
        self.assertEqual(got.by_project()[fst.UNRESOLVED_SCOPE_ROW].status, fst.STATUS_UNREAD)

    def test_an_unusable_document_on_explicit_projects_alone_names_no_member_gap(self):
        # A declaration with no folder, organisation or selector has no members.
        got = self._next("{not json", render={"present": True, "projects": ["explicit-a"]})
        self.assertNotIn(fst.UNRESOLVED_SCOPE_ROW, got.by_project())

    def test_exclusions_are_applied(self):
        render = {"present": True, "projects": ["explicit-a", "explicit-skip"], "folders": ["1"],
                  "exclude": {"projects": ["*-skip"], "clusters": [{"projectId": "member-a", "location": "us-central1", "clusterName": "web"},
                                                                    {"projectId": "explicit-a", "location": "us-east1", "clusterName": "old"}]}}
        got = self._next(_resolved([("member-a", "ok"), ("member-skip", "ok"), ("member-dropped", "ok")], clusters={
            "member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "web"], ["us-central1", "api"]]},
            "member-skip": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "x"]]},
            "member-dropped": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "y"]]},
        }, excluded=["member-dropped"]), render=render)
        by = got.by_project()
        self.assertEqual(list(by), ["mgmt", "explicit-a", "member-a"])
        self.assertEqual(by["member-a"].clusters, (("member-a", "us-central1", "api"),))
        self.assertIn(("explicit-a", "us-east1", "old"), got.excluded_clusters)
        self.assertFalse(got.admits("explicit-a", "us-east1", "old"))
        self.assertTrue(got.admits("explicit-a", "us-east1", "new"))
        self.assertTrue(got.admits("member-a", "us-central1", "api"))
        self.assertFalse(got.admits("member-a", "us-central1", "web"))
        self.assertFalse(got.admits("ghost-proj", "us-east1", "ghost"))
        self.assertFalse(got.admits(fst.UNRESOLVED_SCOPE_ROW, "l", "c"))

    def test_an_unreadable_render_raises_in_next_mode(self):
        os.environ[fst.SCOPE_FILE_ENV] = str(self.home / "absent-render.json")
        self._resolved_file(_resolved([]))
        with self.assertRaises(fst.ScopeRenderUnreadable):
            fst.list_scope_clusters(self.home, management_project="mgmt")

    def test_an_empty_env_is_today_mode_the_profiles_walk(self):
        self._resolved_file(_resolved([("member-a", "ok")], clusters={"member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["l", "c"]]}}))
        os.environ[fst.RESOLVED_SCOPE_FILE_ENV] = ""
        got = fst.list_scope_clusters(self.home)
        self.assertEqual(got.source, fst.SOURCE_PROFILES_WALK)
        self.assertFalse(got.next_mode)
        self.assertEqual([e.project for e in got.projects], ["ghost-proj"])

    def test_the_profiles_walk_reads_identities_as_the_scripts_did(self):
        self._profile("cluster-a1", "proj-a", "one", "us-central1")
        self._profile("cluster-a2", "proj-a", "two", "us-east1")
        # Reserved and hidden profiles, files and unreadable identities.
        self._profile("platform", "reserved-proj", "x", "y")
        self._profile(".hidden", "hidden-proj", "x", "y")
        (self.home / "profiles" / "a-file").write_text("", encoding="utf-8")
        (self.home / "profiles" / "cluster-broken").mkdir()
        (self.home / "profiles" / "cluster-broken" / "config.yaml").write_text("cluster_identity: [", encoding="utf-8")
        got = fst.list_scope_clusters(self.home)
        by = got.by_project()
        self.assertEqual(list(by), ["ghost-proj", "proj-a"])
        self.assertEqual(by["proj-a"].status, fst.STATUS_LISTED)
        self.assertEqual(by["proj-a"].clusters, (("proj-a", "us-central1", "one"), ("proj-a", "us-east1", "two")))
        self.assertIsNone(by["proj-a"].at)
        self.assertEqual(got.unidentified_profiles, ("cluster-broken",))
        self.assertEqual(sorted(name for name, _ in fst.profile_identities(self.home)), ["cluster-a1", "cluster-a2", "cluster-broken", "cluster-ghost"])

    def test_no_profiles_directory_is_an_empty_answer(self):
        empty = pathlib.Path(self.tmp.name) / "elsewhere"
        empty.mkdir()
        got = fst.list_scope_clusters(empty)
        self.assertEqual((got.source, got.projects, got.unidentified_profiles), (fst.SOURCE_PROFILES_WALK, (), ()))

    def test_next_mode_never_reads_the_profiles_directory(self):
        with mock.patch.object(fst, "profile_identities", side_effect=AssertionError("walked the profiles")):
            got = self._next(_resolved([]))
        self.assertEqual(got.source, fst.SOURCE_RESOLVED_SCOPE)


class NoCallerWalksProfilesTest(unittest.TestCase):
    """#5621: fleet_scope_targets is the one reader of the profiles directory
    for a cluster list; the watch scripts call it rather than list the
    directory themselves."""

    SCRIPTS = ("stall_watch.py", "upgrade_readiness_watch.py", "sa_role_watch.py", "leeway_watch.py")
    WALKS = {"iterdir", "listdir", "scandir", "glob", "rglob", "walk"}

    @classmethod
    def walking_functions(cls, source: str) -> list[str]:
        """The functions that both name the profiles directory (PROFILES_DIR
        or a "profiles" literal) and list a directory."""
        found = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names_profiles = any(
                (isinstance(n, ast.Name) and n.id == "PROFILES_DIR")
                or (isinstance(n, ast.Attribute) and n.attr == "PROFILES_DIR")
                or (isinstance(n, ast.Constant) and n.value == "profiles")
                for n in ast.walk(node)
            )
            walks = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in cls.WALKS
                for n in ast.walk(node)
            )
            if names_profiles and walks:
                found.append(f"{node.name}:{node.lineno}")
        return found

    def test_no_watch_script_lists_the_profiles_directory(self):
        here = pathlib.Path(__file__).resolve().parent
        for name in self.SCRIPTS:
            with self.subTest(name):
                self.assertEqual(self.walking_functions((here / name).read_text(encoding="utf-8")), [],
                                 f"{name} walks the profiles directory; call fleet_scope_targets.list_scope_clusters")

    def test_the_check_catches_the_walk_it_replaced(self):
        # The shape every script had before #5621, and the reader's own walk.
        before = (
            "def roster_projects():\n"
            "    base = Path(gitops_workspace.agent_home()) / PROFILES_DIR\n"
            "    for home in base.iterdir():\n"
            "        pass\n"
        )
        self.assertEqual(self.walking_functions(before), ["roster_projects:1"])
        reader = (pathlib.Path(__file__).resolve().parent / "fleet_scope_targets.py").read_text(encoding="utf-8")
        self.assertEqual([f.split(":")[0] for f in self.walking_functions(reader)], ["profile_identities"])

    def test_the_board_reads_no_cluster_profile_under_next(self):
        # #3658: the Home tab's board still reads the profiles outside next (its
        # today path is unchanged, so the static check above does not hold it);
        # under next it neither lists the profiles directory nor reads a
        # Cluster Agent profile.
        import sqlite3
        from datetime import datetime, timezone

        import board_snapshot
        import findings_queue
        import scope_publisher

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        profiles = home / "profiles"
        ghost = profiles / "cluster-ghost-proj-ghost-us-east1"
        ghost.mkdir(parents=True)
        (ghost / "config.yaml").write_text(_identity_config("ghost-proj", "ghost", "us-east1"), encoding="utf-8")
        render = {"present": True, "projects": [], "folders": ["1"]}
        (home / "scope.json").write_text(json.dumps(render), encoding="utf-8")
        document = _resolved([("member-a", "ok")], clusters={"member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "web"]]}})
        document["memberHash"] = scope_publisher.member_hash(render)
        (home / "resolved-scope.json").write_text(json.dumps(document), encoding="utf-8")
        env = mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(home / "scope.json"),
                                           fst.RESOLVED_SCOPE_FILE_ENV: str(home / "resolved-scope.json"),
                                           "GCP_PROJECT_ID": "mgmt"})
        env.start()
        self.addCleanup(env.stop)

        real_iterdir = pathlib.Path.iterdir

        def no_profiles_walk(path):
            if path.name == "profiles":
                raise AssertionError(f"listed {path}")
            return real_iterdir(path)

        session = sqlite3.connect(":memory:")
        findings_queue.init_findings_schema(session)
        with mock.patch.object(pathlib.Path, "iterdir", autospec=True, side_effect=no_profiles_walk), \
                mock.patch.object(fst, "profile_identities", side_effect=AssertionError("read a Cluster Agent profile")):
            board = board_snapshot.snapshot(session, None, datetime.now(timezone.utc), profiles, lambda: None)
        self.assertIn("web", [row["name"] for row in board["clusters"]])
        self.assertNotIn("ghost", json.dumps(board))


if __name__ == "__main__":
    unittest.main()
