"""fleet_scope_targets: the declared scope's resolved projects for an audit.

Run: python3 -m unittest discover -s agents/platform/scripts -p 'test_fleet_scope_targets.py' -v
"""

from __future__ import annotations

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
        render.write_text(json.dumps({"present": True, "projects": ["payments-prod", "team-sandbox"], "exclude": {"projects": ["*-sandbox"], "clusters": []}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {fst.SCOPE_FILE_ENV: str(render), "GCP_PROJECT_ID": "ops-mgmt"}):
            targets = fst.declared_scope_targets(self.home)
            self.assertEqual(targets.unread, (("declared-scope", "unresolved"), ("payments-prod", "unresolved")))

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
        # Excluded by number: only the reconcile can match that, and it writes
        # the dropped ids for the reader.
        declared = dict(DECLARED, projects=["payments-prod", "team-prod"], exclude={"projects": ["123456789012"], "clusters": []})
        snap = _snapshot([{"id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope"},
                          {"id": "payments-prod", "outcome": "ok", "state": "in-scope"}], declared=declared, present=True)
        snap["excludedProjects"] = ["team-prod"]
        self._write(snap)
        self.assertEqual(fst.declared_scope_targets(self.home).unread, ())

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


if __name__ == "__main__":
    unittest.main()
