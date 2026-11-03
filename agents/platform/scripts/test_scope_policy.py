#!/usr/bin/env python3
"""Tests for scope_policy: what the broker runs for the scope publisher."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scope_policy  # noqa: E402

CLUSTERS_LIST = ["gcloud", "container", "clusters", "list", "--project", "p-one", "--format=value(name,location)"]
ASSET_SEARCH = [
    "gcloud", "asset", "search-all-resources", "--scope=folders/111",
    "--asset-types=container.googleapis.com/Cluster", "--format=json(name,location)",
]
SHARED_VPC = ["gcloud", "compute", "shared-vpc", "list-associated-resources", "host-proj", "--format=json(id,type)"]
METRICS_SCOPE = [
    "gcloud", "beta", "monitoring", "metrics-scopes", "describe",
    "locations/global/metricsScopes/scoping-proj", "--format=json(name,monitoredProjects)",
]
PROJECT_DESCRIBE = ["gcloud", "projects", "describe", "123456", "--format=value(projectId)"]

DECLARATION = {
    "present": True,
    "projects": ["explicit-proj"],
    "folders": ["111"],
    "organizations": ["222"],
    "sharedVpcHosts": ["host-proj"],
    "metricsScopes": ["scoping-proj"],
    "maxProjects": 100,
    "exclude": {"projects": [], "clusters": []},
}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScopeFileTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.scope_file = str(Path(self.temp_dir.name) / "scope.json")
        self.write_declaration(DECLARATION)
        self.clock = FakeClock()
        self.window = scope_policy.MemberWindow(clock=self.clock, cap=3)

    def write_declaration(self, document):
        Path(self.scope_file).write_text(json.dumps(document), encoding="utf-8")

    def declaration(self):
        return scope_policy.load_declaration(self.scope_file)

    def check(self, argv, extras=False):
        return scope_policy.check(argv, extras=extras, scope_file=self.scope_file, window=self.window)


class ShapeTest(unittest.TestCase):
    """The argv is one of five shapes, token for token."""

    def test_each_shape_matches_with_its_argument(self):
        for argv, want in (
            (CLUSTERS_LIST, (scope_policy.SHAPE_CLUSTERS_LIST, "p-one")),
            (ASSET_SEARCH, (scope_policy.SHAPE_ASSET_SEARCH, "folders/111")),
            (SHARED_VPC, (scope_policy.SHAPE_SHARED_VPC, "host-proj")),
            (METRICS_SCOPE, (scope_policy.SHAPE_METRICS_SCOPE, "scoping-proj")),
            (PROJECT_DESCRIBE, (scope_policy.SHAPE_PROJECT_NUMBER, "123456")),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(want, scope_policy.match_shape(argv))
                self.assertEqual((None, want), scope_policy.argv_refusal(argv))

    def test_each_named_flag_is_refused_by_name_in_both_spellings(self):
        for flag in scope_policy.REFUSED_FLAGS:
            for extra in ([flag], [f"{flag}=x"], [flag, "x"]):
                argv = [*ASSET_SEARCH, *extra]
                with self.subTest(argv=argv):
                    refusal, matched = scope_policy.argv_refusal(argv)
                    self.assertIsNone(matched)
                    self.assertEqual(scope_policy.RULE_SCOPE_ARGV, refusal.rule)
                    self.assertIn(flag, refusal.message)

    def test_the_identity_configuration_and_logging_flags_are_all_named(self):
        for flag in (
            "--impersonate-service-account", "--flags-file", "--access-token-file",
            "--configuration", "--log-http", "--verbosity", "--account", "--billing-project", "--quiet",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, scope_policy.REFUSED_FLAGS)

    def test_a_named_flag_is_refused_even_on_a_shape_that_otherwise_matches(self):
        refusal, _ = scope_policy.argv_refusal([*CLUSTERS_LIST[:-1], "--verbosity=debug", CLUSTERS_LIST[-1]])
        self.assertIn("--verbosity", refusal.message)

    def test_project_outside_clusters_list_is_refused_by_name(self):
        for argv in (
            [*SHARED_VPC, "--project", "other"],
            [*PROJECT_DESCRIBE, "--project=other"],
            ["gcloud", "projects", "list", "--project", "x"],
        ):
            with self.subTest(argv=argv):
                refusal, _ = scope_policy.argv_refusal(argv)
                self.assertEqual(scope_policy.RULE_SCOPE_ARGV, refusal.rule)
                self.assertIn("--project", refusal.message)

    def test_near_misses_are_refused(self):
        for argv in (
            # A changed --format, a dropped --format, an extra token.
            [*CLUSTERS_LIST[:-1], "--format=json"],
            CLUSTERS_LIST[:-1],
            [*CLUSTERS_LIST, "--zone=us-central1-a"],
            [*ASSET_SEARCH, "--page-size=10"],
            # The `=` form where the shape has a space, and the reverse.
            ["gcloud", "container", "clusters", "list", "--project=p-one", "--format=value(name,location)"],
            ["gcloud", "asset", "search-all-resources", "--scope", "folders/111",
             "--asset-types=container.googleapis.com/Cluster", "--format=json(name,location)"],
            # Reordered tokens.
            ["gcloud", "container", "clusters", "list", "--format=value(name,location)", "--project", "p-one"],
            # A repeated flag.
            [*ASSET_SEARCH[:4], ASSET_SEARCH[4], *ASSET_SEARCH[4:]],
            # Another asset type, another container kind, a wider metrics scope path.
            [*ASSET_SEARCH[:4], "--asset-types=compute.googleapis.com/Instance", ASSET_SEARCH[5]],
            [*ASSET_SEARCH[:3], "--scope=projects/p-one", *ASSET_SEARCH[4:]],
            [*METRICS_SCOPE[:5], "locations/global/metricsScopes/a/b", METRICS_SCOPE[6]],
            # Arguments that are empty, or read as a flag, or break the pattern.
            ["gcloud", "container", "clusters", "list", "--project", "", "--format=value(name,location)"],
            ["gcloud", "container", "clusters", "list", "--project", "-x", "--format=value(name,location)"],
            ["gcloud", "container", "clusters", "list", "--project", "P-One", "--format=value(name,location)"],
            ["gcloud", "projects", "describe", "my-proj", "--format=value(projectId)"],
            [*ASSET_SEARCH[:3], "--scope=folders/abc", *ASSET_SEARCH[4:]],
            # Other gcloud commands and other executables.
            ["gcloud", "container", "clusters", "get-credentials", "c", "--project", "p-one"],
            ["gcloud", "auth", "print-access-token"],
            ["gcloud", "config", "set", "project", "x"],
            ["kubectl", "get", "pods"],
            ["gcloud"],
        ):
            with self.subTest(argv=argv):
                refusal, matched = scope_policy.argv_refusal(argv)
                self.assertIsNone(matched)
                self.assertEqual(scope_policy.RULE_SCOPE_ARGV, refusal.rule)

    def test_no_refusal_message_carries_a_stderr_marker_the_publisher_classifies(self):
        # The publisher keeps the reconcile's stderr classification; a refusal
        # must not read as an IAM deny or a disabled API. Every message the
        # check can answer with, against every marker the reconcile reads.
        import cluster_agent_reconcile

        markers = (
            "PERMISSION_DENIED", "403", "does not have permission", "Permission denied",
            "SERVICE_DISABLED", "accessNotConfigured", "API has not been used", "is not enabled", "has not been enabled",
        )
        # A marker the reconcile adds later must be added here too.
        self.assertLessEqual(
            {*cluster_agent_reconcile._DENIED_MARKERS, *cluster_agent_reconcile._API_DISABLED_MARKERS}, set(markers)
        )
        window = scope_policy.MemberWindow()
        with tempfile.TemporaryDirectory() as directory:
            scope_file = str(Path(directory) / "scope.json")
            missing = str(Path(directory) / "missing.json")
            Path(scope_file).write_text(json.dumps(DECLARATION), encoding="utf-8")
            refusals = [
                scope_policy.argv_refusal([*ASSET_SEARCH, "--quiet"])[0],
                scope_policy.argv_refusal(["gcloud", "x"])[0],
                scope_policy.argv_refusal([*SHARED_VPC, "--project", "x"])[0],
                scope_policy.check(ASSET_SEARCH, extras=True, scope_file=scope_file, window=window)[0],
                scope_policy.check(ASSET_SEARCH, extras=False, scope_file=missing, window=window)[0],
                scope_policy.check(CLUSTERS_LIST, extras=False, scope_file=scope_file, window=window)[0],
            ]
        self.assertEqual(
            {scope_policy.RULE_SCOPE_ARGV, scope_policy.RULE_SCOPE_REQUEST, scope_policy.RULE_SCOPE_ARGUMENT},
            {refusal.rule for refusal in refusals},
        )
        for refusal in refusals:
            for marker in markers:
                with self.subTest(message=refusal.message, marker=marker):
                    self.assertNotIn(marker, refusal.message)


class ArgumentTest(ScopeFileTestCase):
    """Every argument is declared, or returned by an earlier lookup (condition 2b)."""

    def test_declared_selectors_are_admitted(self):
        for argv in (
            ASSET_SEARCH,
            [*ASSET_SEARCH[:3], "--scope=organizations/222", *ASSET_SEARCH[4:]],
            SHARED_VPC,
            METRICS_SCOPE,
        ):
            with self.subTest(argv=argv):
                refusal, admitted = self.check(argv)
                self.assertIsNone(refusal)
                self.assertEqual(scope_policy.match_shape(argv)[0], admitted.shape)

    def test_undeclared_selectors_are_refused(self):
        for argv in (
            [*ASSET_SEARCH[:3], "--scope=folders/999", *ASSET_SEARCH[4:]],
            # A declared organization's ID named as a folder, and the reverse.
            [*ASSET_SEARCH[:3], "--scope=folders/222", *ASSET_SEARCH[4:]],
            [*ASSET_SEARCH[:3], "--scope=organizations/111", *ASSET_SEARCH[4:]],
            [*SHARED_VPC[:4], "other-host", SHARED_VPC[5]],
            [*METRICS_SCOPE[:5], "locations/global/metricsScopes/other-scope", METRICS_SCOPE[6]],
        ):
            with self.subTest(argv=argv):
                refusal, admitted = self.check(argv)
                self.assertIsNone(admitted)
                self.assertEqual(scope_policy.RULE_SCOPE_ARGUMENT, refusal.rule)

    def test_an_explicit_project_is_not_probed(self):
        # Contract §5: the publisher never probes scope.json `projects`; the
        # gateway admits them from the file.
        refusal, _ = self.check([*CLUSTERS_LIST[:5], "explicit-proj", CLUSTERS_LIST[6]])
        self.assertEqual(scope_policy.RULE_SCOPE_ARGUMENT, refusal.rule)

    def test_the_probe_and_the_naming_call_wait_for_a_lookup(self):
        for argv in (CLUSTERS_LIST, PROJECT_DESCRIBE):
            with self.subTest(argv=argv):
                refusal, _ = self.check(argv)
                self.assertEqual(scope_policy.RULE_SCOPE_ARGUMENT, refusal.rule)

    def test_request_fields_beyond_argv_are_refused(self):
        refusal, admitted = self.check(ASSET_SEARCH, extras=True)
        self.assertIsNone(admitted)
        self.assertEqual(scope_policy.RULE_SCOPE_REQUEST, refusal.rule)

    def test_a_scope_file_that_cannot_be_read_refuses_everything(self):
        for content in (None, "", "not json", "[]",
                        json.dumps({**DECLARATION, "folders": "111"}),
                        json.dumps({**DECLARATION, "folders": [111]})):
            with self.subTest(content=content):
                path = Path(self.scope_file)
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content, encoding="utf-8")
                refusal, admitted = self.check(ASSET_SEARCH)
                self.assertIsNone(admitted)
                self.assertEqual(scope_policy.RULE_SCOPE_ARGUMENT, refusal.rule)

    def test_an_unset_scope_file_refuses_everything(self):
        refusal, _ = scope_policy.check(ASSET_SEARCH, extras=False, scope_file="", window=self.window)
        self.assertEqual(scope_policy.RULE_SCOPE_ARGUMENT, refusal.rule)

    def test_a_declaration_edit_is_read_on_the_next_call(self):
        self.write_declaration({**DECLARATION, "folders": []})
        refusal, _ = self.check(ASSET_SEARCH)
        self.assertEqual(scope_policy.RULE_SCOPE_ARGUMENT, refusal.rule)


ASSET_OUTPUT = json.dumps([
    {"name": "//container.googleapis.com/projects/p-one/locations/us-central1/clusters/c1", "location": "us-central1"},
    {"name": "//container.googleapis.com/projects/p-two/zones/us-central1-a/clusters/c2", "location": "us-central1-a"},
    {"name": "//container.googleapis.com/projects/p-one/locations/europe-west1/clusters/c3"},
    {"name": "not an asset name"},
    {"location": "no-name"},
    "a string row",
])
XPN_OUTPUT = json.dumps([
    {"id": "svc-one", "type": "PROJECT"},
    {"id": "123", "type": "RESOURCE_TYPE_UNSPECIFIED"},
    {"id": 7, "type": "PROJECT"},
])
METRICS_OUTPUT = json.dumps({
    "name": "locations/global/metricsScopes/scoping-proj",
    "monitoredProjects": [
        {"name": "locations/global/metricsScopes/scoping-proj/projects/123456"},
        {"name": "locations/global/metricsScopes/scoping-proj/projects/named-proj"},
        {"name": "something else"},
    ],
})


class ReconcileCommandsTest(unittest.TestCase):
    """The five shapes are the commands cluster_agent_reconcile.py sends.

    The publisher calls the reconcile's own lookups through the broker, so a
    format or flag edited on one side alone would refuse every publisher call.
    """

    def captured_argv(self, call):
        import cluster_agent_reconcile

        captured = []

        def run(cmd, **kwargs):
            captured.append(cmd)
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="stub")

        with mock.patch.object(cluster_agent_reconcile.sandbox_exec, "run", side_effect=run), \
                mock.patch.object(cluster_agent_reconcile, "log"):
            call(cluster_agent_reconcile)
        self.assertEqual(1, len(captured), captured)
        return captured[0]

    def test_each_reconcile_lookup_is_a_shape(self):
        for call, want in (
            (lambda r: r._list_project("p-one"), (scope_policy.SHAPE_CLUSTERS_LIST, "p-one")),
            (lambda r: r._search_container("folders/111"), (scope_policy.SHAPE_ASSET_SEARCH, "folders/111")),
            (lambda r: r._resolve_selector("sharedVpcHosts/host-proj"), (scope_policy.SHAPE_SHARED_VPC, "host-proj")),
            (lambda r: r._resolve_selector("metricsScopes/scoping-proj"), (scope_policy.SHAPE_METRICS_SCOPE, "scoping-proj")),
            (lambda r: r._project_id_of("123456"), (scope_policy.SHAPE_PROJECT_NUMBER, "123456")),
        ):
            with self.subTest(want=want):
                self.assertEqual(want, scope_policy.match_shape(self.captured_argv(call)))


class MembersNamedTest(unittest.TestCase):
    """What each lookup's stdout adds to the member window."""

    def test_the_table(self):
        for shape, stdout, want in (
            (scope_policy.SHAPE_ASSET_SEARCH, ASSET_OUTPUT, (scope_policy.KIND_PROJECTS, {"p-one", "p-two"})),
            (scope_policy.SHAPE_SHARED_VPC, XPN_OUTPUT, (scope_policy.KIND_PROJECTS, {"svc-one"})),
            # Numbers only: a Metrics Scope member opens the naming call,
            # never the probe.
            (scope_policy.SHAPE_METRICS_SCOPE, METRICS_OUTPUT, (scope_policy.KIND_NUMBERS, {"123456"})),
            (scope_policy.SHAPE_PROJECT_NUMBER, "named-proj\n", (scope_policy.KIND_PROJECTS, {"named-proj"})),
            # A legacy domain-scoped ID cannot be probed.
            (scope_policy.SHAPE_PROJECT_NUMBER, "example.com:legacy\n", (scope_policy.KIND_PROJECTS, set())),
            (scope_policy.SHAPE_ASSET_SEARCH, "not json", (scope_policy.KIND_PROJECTS, set())),
            (scope_policy.SHAPE_ASSET_SEARCH, "{}", (scope_policy.KIND_PROJECTS, set())),
            (scope_policy.SHAPE_METRICS_SCOPE, "[]", (scope_policy.KIND_NUMBERS, set())),
            # A monitoredProjects that is not a list is skipped, not iterated.
            (scope_policy.SHAPE_METRICS_SCOPE, '{"monitoredProjects": 7}', (scope_policy.KIND_NUMBERS, set())),
            (scope_policy.SHAPE_METRICS_SCOPE, '{"monitoredProjects": true}', (scope_policy.KIND_NUMBERS, set())),
            (scope_policy.SHAPE_METRICS_SCOPE, '{"monitoredProjects": "x"}', (scope_policy.KIND_NUMBERS, set())),
            (scope_policy.SHAPE_CLUSTERS_LIST, "c1 us-central1\n", (scope_policy.KIND_PROJECTS, set())),
        ):
            with self.subTest(shape=shape, stdout=stdout[:40]):
                self.assertEqual(want, scope_policy.members_named(shape, stdout))


class MemberWindowTest(ScopeFileTestCase):
    """The member window: recorded by lookups, read by the probe and the naming call."""

    def admits(self, argv):
        return self.check(argv)[0] is None

    def test_a_member_a_lookup_returned_is_admitted_until_the_window_closes(self):
        _, admitted = self.check(ASSET_SEARCH)
        scope_policy.record(admitted, ASSET_OUTPUT, scope_file=self.scope_file, window=self.window)
        self.assertTrue(self.admits(CLUSTERS_LIST))
        self.assertTrue(self.admits([*CLUSTERS_LIST[:5], "p-two", CLUSTERS_LIST[6]]))
        self.assertFalse(self.admits([*CLUSTERS_LIST[:5], "p-three", CLUSTERS_LIST[6]]))
        self.clock.now += scope_policy.MEMBER_WINDOW_SECONDS - 1
        self.assertTrue(self.admits(CLUSTERS_LIST))
        self.clock.now += 1
        self.assertFalse(self.admits(CLUSTERS_LIST))

    def test_a_metrics_scope_number_opens_the_naming_call_and_its_answer_opens_the_probe(self):
        _, admitted = self.check(METRICS_SCOPE)
        scope_policy.record(admitted, METRICS_OUTPUT, scope_file=self.scope_file, window=self.window)
        # The number is not a project ID, and the named ID is not yet known.
        self.assertFalse(self.admits([*CLUSTERS_LIST[:5], "123456", CLUSTERS_LIST[6]]))
        self.assertFalse(self.admits([*CLUSTERS_LIST[:5], "named-proj", CLUSTERS_LIST[6]]))
        refusal, admitted = self.check(PROJECT_DESCRIBE)
        self.assertIsNone(refusal)
        scope_policy.record(admitted, "named-proj\n", scope_file=self.scope_file, window=self.window)
        self.assertTrue(self.admits([*CLUSTERS_LIST[:5], "named-proj", CLUSTERS_LIST[6]]))

    def test_a_shared_vpc_member_opens_the_probe(self):
        _, admitted = self.check(SHARED_VPC)
        scope_policy.record(admitted, XPN_OUTPUT, scope_file=self.scope_file, window=self.window)
        self.assertTrue(self.admits([*CLUSTERS_LIST[:5], "svc-one", CLUSTERS_LIST[6]]))

    def test_a_declaration_change_empties_the_window(self):
        _, admitted = self.check(ASSET_SEARCH)
        scope_policy.record(admitted, ASSET_OUTPUT, scope_file=self.scope_file, window=self.window)
        self.write_declaration({**DECLARATION, "maxProjects": 101})
        self.assertFalse(self.admits(CLUSTERS_LIST))
        # And changing it back does not bring the old members back.
        self.write_declaration(DECLARATION)
        self.assertFalse(self.admits(CLUSTERS_LIST))

    def test_a_declaration_change_while_the_lookup_ran_records_nothing(self):
        _, admitted = self.check(ASSET_SEARCH)
        self.write_declaration({**DECLARATION, "maxProjects": 101})
        scope_policy.record(admitted, ASSET_OUTPUT, scope_file=self.scope_file, window=self.window)
        self.assertFalse(self.admits(CLUSTERS_LIST))

    def test_the_probe_records_nothing(self):
        _, admitted = self.check(ASSET_SEARCH)
        scope_policy.record(admitted, ASSET_OUTPUT, scope_file=self.scope_file, window=self.window)
        _, probe = self.check(CLUSTERS_LIST)
        scope_policy.record(probe, "c1 us-central1\n", scope_file=self.scope_file, window=self.window)
        self.assertFalse(self.admits([*CLUSTERS_LIST[:5], "c1", CLUSTERS_LIST[6]]))

    def test_the_cap_records_nothing_new_and_evicts_nothing(self):
        declaration = self.declaration()  # the window's cap is 3
        self.window.record(scope_policy.KIND_PROJECTS, {"a-1", "a-2"}, declaration)
        self.clock.now += 10
        self.window.record(scope_policy.KIND_PROJECTS, {"b-1", "b-2"}, declaration)
        held = {p for p in ("a-1", "a-2", "b-1", "b-2") if self.window.holds(scope_policy.KIND_PROJECTS, p, declaration.digest)}
        self.assertEqual({"a-1", "a-2", "b-1"}, held)
        # Full: a new member is not recorded, the earliest are kept.
        self.window.record(scope_policy.KIND_PROJECTS, {"c-1"}, declaration)
        self.assertFalse(self.window.holds(scope_policy.KIND_PROJECTS, "c-1", declaration.digest))
        self.assertTrue(self.window.holds(scope_policy.KIND_PROJECTS, "a-1", declaration.digest))

    def test_a_held_member_is_refreshed_when_the_window_is_full(self):
        declaration = self.declaration()
        self.window.record(scope_policy.KIND_PROJECTS, {"a-1", "a-2", "a-3"}, declaration)
        self.clock.now += scope_policy.MEMBER_WINDOW_SECONDS - 1
        self.window.record(scope_policy.KIND_PROJECTS, {"a-1"}, declaration)
        self.clock.now += 2
        self.assertTrue(self.window.holds(scope_policy.KIND_PROJECTS, "a-1", declaration.digest))
        self.assertFalse(self.window.holds(scope_policy.KIND_PROJECTS, "a-2", declaration.digest))

    def test_expired_members_free_their_places(self):
        declaration = self.declaration()
        self.window.record(scope_policy.KIND_PROJECTS, {"a-1", "a-2", "a-3"}, declaration)
        self.clock.now += scope_policy.MEMBER_WINDOW_SECONDS
        self.window.record(scope_policy.KIND_PROJECTS, {"b-1"}, declaration)
        self.assertTrue(self.window.holds(scope_policy.KIND_PROJECTS, "b-1", declaration.digest))

    def test_the_window_outlasts_the_longest_publisher_run(self):
        self.assertEqual(5 * 60 * 60, scope_policy.MEMBER_WINDOW_SECONDS)

    def test_the_cap_is_a_fixed_memory_bound_not_max_projects(self):
        self.assertEqual(5000, scope_policy.MEMBER_WINDOW_CAP)
        self.assertEqual(scope_policy.MEMBER_WINDOW_CAP, scope_policy.MemberWindow().cap)
        # More members than maxProjects are recorded: excluded and over-cap
        # members fill the window without counting toward the reconcile's cap.
        self.write_declaration({**DECLARATION, "maxProjects": 1})
        window = scope_policy.MemberWindow(clock=self.clock)
        window.record(scope_policy.KIND_PROJECTS, {"a-1", "a-2"}, self.declaration())
        self.assertTrue(window.holds(scope_policy.KIND_PROJECTS, "a-2", self.declaration().digest))

    def test_numbers_have_their_own_count(self):
        declaration = self.declaration()
        self.window.record(scope_policy.KIND_PROJECTS, {"a-1", "a-2", "a-3"}, declaration)
        self.window.record(scope_policy.KIND_NUMBERS, {"1", "2", "3"}, declaration)
        for number in ("1", "2", "3"):
            self.assertTrue(self.window.holds(scope_policy.KIND_NUMBERS, number, declaration.digest))
        # A number is never a project, and a project never a number.
        self.assertFalse(self.window.holds(scope_policy.KIND_PROJECTS, "1", declaration.digest))
        self.assertFalse(self.window.holds(scope_policy.KIND_NUMBERS, "a-1", declaration.digest))


if __name__ == "__main__":
    unittest.main()
