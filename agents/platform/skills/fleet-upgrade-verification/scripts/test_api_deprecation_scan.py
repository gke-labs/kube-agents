#!/usr/bin/env python3
"""Unit tests for api_deprecation_scan.py, with the broker and git replaced by fakes."""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import api_deprecation_scan as scan  # noqa: E402

PDB_V1BETA1 = b"apiVersion: policy/v1beta1\nkind: PodDisruptionBudget\nmetadata:\n  name: web-pdb\n"
CRONJOB_V1BETA1 = b"apiVersion: batch/v1beta1\nkind: CronJob\nmetadata:\n  name: nightly\n"
CRONJOB_V1 = b"apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: hourly\n"
HPA_V2BETA2_LIST_JSON = json.dumps(
    {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {"apiVersion": "autoscaling/v2beta2", "kind": "HorizontalPodAutoscaler", "metadata": {"name": "web-hpa"}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "settings"}},
        ],
    }
).encode()
HELM_TEMPLATE = b"apiVersion: {{ .Values.api }}\nkind: Deployment\nmetadata:\n  labels: {{- toYaml .Values.labels | nindent 4 }}\n"
FLEET_JSON = {
    "target_version": "1.27.0-gke.100",
    "members": [
        {"project": "p", "cluster": "old", "control_plane_version": "1.24.9-gke.1", "status": "lagging"},
        {"project": "p", "cluster": "mid", "control_plane_version": "1.26.2-gke.1", "status": "lagging"},
        {"project": "p", "cluster": "odd", "control_plane_version": None, "status": "unknown"},
    ],
}


def write_tree(root, files):
    for relative, data in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class FakeListing(list):
    def __init__(self, entries, truncated=False):
        super().__init__(entries)
        self.total = len(entries)
        self.truncated = truncated


class FakeWorkspace:
    """The subset of credential_proxy_client.Workspace the scan uses, over an in-memory tree."""

    page_size = 2

    def __init__(self, files, sha="abc1234", unreadable=()):
        self.files = files
        self.base_sha = sha
        self.unreadable = set(unreadable)
        self.closed = False
        self.read_batches = []

    def list(self, prefix=None, after=None):
        paths = sorted(self.files)
        if after:
            paths = [p for p in paths if p > after]
        page = paths[: self.page_size]
        return FakeListing([{"path": p, "size": len(self.files[p])} for p in page], truncated=len(paths) > self.page_size)

    def read_many(self, paths):
        self.read_batches.append(list(paths))
        files = {p: self.files[p] for p in paths if p not in self.unreadable}
        skipped = [{"path": p, "reason": "tooLarge"} for p in paths if p in self.unreadable]
        return files, skipped

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TableTest(unittest.TestCase):
    def setUp(self):
        self.table = scan.load_table()

    def test_every_entry_parses_and_is_unique(self):
        self.assertGreater(len(self.table.removals), 0)
        self.assertEqual(len(self.table.by_key()), len(self.table.removals))
        for removal in self.table.removals:
            self.assertTrue(removal.replacement, removal)
            self.assertLessEqual(removal.removed_in, self.table.as_of, removal)

    def test_metadata(self):
        self.assertEqual(self.table.as_of, (1, 32))
        self.assertTrue(self.table.source_url.startswith("https://"))

    def test_known_removals_present(self):
        keys = self.table.by_key()
        self.assertEqual(keys[("policy/v1beta1", "PodDisruptionBudget")].removed_in, (1, 25))
        self.assertEqual(keys[("policy/v1beta1", "PodDisruptionBudget")].replacement, "policy/v1")
        self.assertEqual(keys[("autoscaling/v2beta2", "HorizontalPodAutoscaler")].removed_in, (1, 26))
        self.assertEqual(keys[("extensions/v1beta1", "Ingress")].removed_in, (1, 22))

    def test_malformed_table_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"as_of": "1.32", "removed": [{"api_version": "a/v1", "kind": "K", "removed_in": "1.25"}]}))
            with self.assertRaises(ValueError):
                scan.load_table(path)
            path.write_text(
                json.dumps(
                    {
                        "as_of": "1.32",
                        "removed": [
                            {"api_version": "a/v1", "kind": "K", "removed_in": "1.25", "replacement": "a/v2"},
                            {"api_version": "a/v1", "kind": "K", "removed_in": "1.26", "replacement": "a/v2"},
                        ],
                    }
                )
            )
            with self.assertRaises(ValueError):
                scan.load_table(path)


class ParseVersionTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(scan.parse_version("1.30.5-gke.1355000"), (1, 30))
        self.assertEqual(scan.parse_version("1.27.0"), (1, 27))
        self.assertEqual(scan.parse_version("1.27"), (1, 27))
        self.assertEqual(scan.parse_version("v1.27"), (1, 27))

    def test_garbage_is_none(self):
        for text in (None, "", "latest", "1", "1.27.0-gke", "1.27.x", 42):
            self.assertIsNone(scan.parse_version(text), text)


class RangeTest(unittest.TestCase):
    def setUp(self):
        self.table = scan.load_table()

    def keys(self, floor, target):
        return {(r.api_version, r.kind) for r in scan.removals_in_range(self.table, floor, target)}

    def test_floor_excluded_target_included(self):
        keys = self.keys((1, 24), (1, 27))
        self.assertIn(("policy/v1beta1", "PodDisruptionBudget"), keys)  # 1.25
        self.assertIn(("autoscaling/v2beta2", "HorizontalPodAutoscaler"), keys)  # 1.26
        self.assertIn(("storage.k8s.io/v1beta1", "CSIStorageCapacity"), keys)  # 1.27, the target itself
        self.assertNotIn(("extensions/v1beta1", "Ingress"), keys)  # 1.22, before the floor
        self.assertNotIn(("flowcontrol.apiserver.k8s.io/v1beta2", "FlowSchema"), keys)  # 1.29, past the target

    def test_removal_at_the_floor_is_already_gone(self):
        self.assertNotIn(("policy/v1beta1", "PodDisruptionBudget"), self.keys((1, 25), (1, 27)))

    def test_target_below_floor_is_empty(self):
        self.assertEqual(self.keys((1, 27), (1, 24)), set())

    def test_across_a_major(self):
        self.assertEqual(self.keys((1, 32), (2, 0)), set())
        self.assertIn(("flowcontrol.apiserver.k8s.io/v1beta3", "FlowSchema"), self.keys((0, 9), (2, 0)))


class ParseManifestsTest(unittest.TestCase):
    def test_multi_document_yaml(self):
        docs, reason = scan.parse_manifests("a.yaml", PDB_V1BETA1 + b"---\n" + CRONJOB_V1BETA1 + b"---\n" + CRONJOB_V1)
        self.assertIsNone(reason)
        self.assertEqual([(d.kind, d.name, d.api_version) for d in docs], [
            ("PodDisruptionBudget", "web-pdb", "policy/v1beta1"),
            ("CronJob", "nightly", "batch/v1beta1"),
            ("CronJob", "hourly", "batch/v1"),
        ])

    def test_json_list_is_expanded(self):
        docs, reason = scan.parse_manifests("l.json", HPA_V2BETA2_LIST_JSON)
        self.assertIsNone(reason)
        self.assertEqual([d.kind for d in docs], ["HorizontalPodAutoscaler", "ConfigMap"])

    def test_yaml_list_and_bare_array(self):
        docs, _ = scan.parse_manifests("l.yaml", b"apiVersion: v1\nkind: List\nitems:\n- apiVersion: batch/v1beta1\n  kind: CronJob\n  metadata: {name: x}\n")
        self.assertEqual([d.api_version for d in docs], ["batch/v1beta1"])
        docs, _ = scan.parse_manifests("a.json", b'[{"apiVersion": "v1", "kind": "Pod", "metadata": {"generateName": "p-"}}]')
        self.assertEqual([(d.kind, d.name) for d in docs], [("Pod", "")])

    def test_non_manifests_are_ignored(self):
        docs, reason = scan.parse_manifests("values.yaml", b"replicas: 3\nimage:\n  tag: v1\n")
        self.assertIsNone(reason)
        self.assertEqual(docs, [])
        docs, reason = scan.parse_manifests("empty.yaml", b"")
        self.assertIsNone(reason)
        self.assertEqual(docs, [])

    def test_helm_template_is_skipped_with_reason(self):
        docs, reason = scan.parse_manifests("t.yaml", HELM_TEMPLATE)
        self.assertEqual(docs, [])
        self.assertEqual(reason, scan.REASON_TEMPLATE)

    def test_loader_errors_outside_yamlerror_are_skipped_not_fatal(self):
        # PyYAML's timestamp constructor raises a bare ValueError for a date that
        # does not exist; that used to escape and mark the whole repository unread.
        docs, reason = scan.parse_manifests("d.yaml", b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  annotations:\n    reviewed: 2001-02-30\n")
        self.assertEqual(docs, [])
        self.assertTrue(reason.startswith("YAML did not parse: ValueError"), reason)
        nested = b"[" * 20000 + b"]" * 20000
        docs, reason = scan.parse_manifests("deep.json", nested)
        self.assertEqual(docs, [])
        self.assertTrue(reason.startswith("JSON did not parse"), reason)

    def test_a_bad_scalar_in_one_file_does_not_hide_a_hit_in_another(self):
        snapshot = scan.Snapshot(source="local directory t", sha=None, files={
            "a.yaml": b"apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, annotations: {reviewed: 2001-02-30}}\n",
            "b.yaml": PDB_V1BETA1,
        })
        report = scan.build_report(scan.load_table(), (1, 24), (1, 27), [], [("t", lambda: snapshot)])
        section = report["sources"]["t"]
        self.assertIsNone(section["error"])
        self.assertEqual([h["kind"] for h in section["hits"]], ["PodDisruptionBudget"])
        self.assertEqual([k["path"] for k in section["skipped"]], ["a.yaml"])

    def test_bad_yaml_json_and_bytes_are_skipped(self):
        _, reason = scan.parse_manifests("b.yaml", b"a: [unclosed\n")
        self.assertTrue(reason.startswith("YAML did not parse"), reason)
        _, reason = scan.parse_manifests("b.json", b"{not json")
        self.assertTrue(reason.startswith("JSON did not parse"), reason)
        _, reason = scan.parse_manifests("b.yaml", b"\xff\xfe\x00")
        self.assertEqual(reason, scan.REASON_ENCODING)


class FleetJsonTest(unittest.TestCase):
    def test_members_and_floor(self):
        members = scan.members_from_fleet_json(FLEET_JSON)
        self.assertEqual([m.label for m in members], ["p/old", "p/mid", "p/odd"])
        self.assertEqual(scan.fleet_floor(members), (1, 24))
        self.assertIsNone(members[2].version)
        self.assertIn("unparsable", members[2].note)

    def test_affected_members(self):
        members = scan.members_from_fleet_json(FLEET_JSON)
        self.assertEqual([m.label for m in scan.affected_members(members, (1, 25))], ["p/old"])
        self.assertEqual([m.label for m in scan.affected_members(members, (1, 27))], ["p/old", "p/mid"])

    def test_no_parsable_member_has_no_floor(self):
        self.assertIsNone(scan.fleet_floor(scan.members_from_fleet_json({"members": [{"cluster": "x", "control_plane_version": "?"}]})))


class ScanAndRenderTest(unittest.TestCase):
    def setUp(self):
        self.table = scan.load_table()
        self.members = scan.members_from_fleet_json(FLEET_JSON)

    def report(self, files, sha="abc1234", floor=(1, 24), target=(1, 27), name="acme/infra"):
        snapshot = scan.Snapshot(source=scan.SOURCE_REPO.format(sha=sha), sha=sha, files=files)
        return scan.build_report(self.table, floor, target, self.members, [(name, lambda: snapshot)])

    def test_hits_carry_resource_api_replacement_and_members(self):
        report = self.report({"apps/pdb.yaml": PDB_V1BETA1 + b"---\n" + CRONJOB_V1, "apps/hpa.json": HPA_V2BETA2_LIST_JSON})
        section = report["sources"]["acme/infra"]
        self.assertEqual(section["source"], "repo manifests as of abc1234")
        self.assertEqual(
            [(h["path"], h["kind"], h["name"], h["api_version"], h["removed_in"], h["replacement"], h["members_affected"]) for h in section["hits"]],
            [
                ("apps/hpa.json", "HorizontalPodAutoscaler", "web-hpa", "autoscaling/v2beta2", "1.26", "autoscaling/v2", ["p/old"]),
                ("apps/pdb.yaml", "PodDisruptionBudget", "web-pdb", "policy/v1beta1", "1.25", "policy/v1", ["p/old"]),
            ],
        )
        self.assertEqual(section["documents"], 4)
        text = scan.render_report(report)
        self.assertIn("## acme/infra — repo manifests as of abc1234", text)
        self.assertIn("| apps/pdb.yaml | PodDisruptionBudget | web-pdb | policy/v1beta1 | 1.25 | policy/v1 | p/old |", text)
        self.assertIn("Not measured: p/odd", text)
        self.assertIn("Members: p/old (1.24), p/mid (1.26)", text)
        self.assertIn(scan.DEPRECATION_INSIGHTS_COMMAND, text)
        self.assertIn(scan.DEPRECATION_INSIGHTS_URL, text)
        self.assertEqual(report["summary"], {"sources": 1, "hits": 2, "skipped": 0, "errors": 0})

    def test_clean_section_states_source_and_counts(self):
        report = self.report({"apps/job.yaml": CRONJOB_V1, "chart/templates/d.yaml": HELM_TEMPLATE})
        section = report["sources"]["acme/infra"]
        self.assertEqual(section["hits"], [])
        self.assertEqual(section["skipped"], [{"path": "chart/templates/d.yaml", "reason": scan.REASON_TEMPLATE}])
        text = scan.render_report(report)
        self.assertIn("repo manifests as of abc1234", text)
        self.assertIn("Clean: no manifest declares an apiVersion removed after 1.24 up to 1.27; 1 file(s), 1 document(s) read.", text)
        self.assertIn("- skipped chart/templates/d.yaml: " + scan.REASON_TEMPLATE, text)

    def test_removal_before_floor_is_not_a_hit(self):
        report = self.report({"pdb.yaml": PDB_V1BETA1}, floor=(1, 25), target=(1, 27))
        self.assertEqual(report["sources"]["acme/infra"]["hits"], [])
        self.assertIn("no removal lies in range", scan.render_report(self.report({}, floor=(1, 27), target=(1, 27))))

    def test_target_past_the_table_is_noted(self):
        report = self.report({}, target=(1, 40))
        self.assertTrue(any("newer than removed_apis.json" in n for n in report["notes"]), report["notes"])

    def test_reader_failure_is_an_error_section(self):
        def boom():
            raise RuntimeError("clone refused")

        report = scan.build_report(self.table, (1, 24), (1, 27), [], [("acme/broken", boom)])
        section = report["sources"]["acme/broken"]
        self.assertEqual(section["error"], "RuntimeError: clone refused")
        self.assertEqual(report["summary"]["errors"], 1)
        text = scan.render_report(report)
        self.assertIn("## acme/broken — not scanned", text)
        self.assertIn("- read failed: RuntimeError: clone refused", text)

    def test_no_sources_says_so(self):
        text = scan.render_report(scan.build_report(self.table, (1, 24), (1, 27), [], []))
        self.assertIn(scan.NO_REPOS_HEADING, text)


class ContentModeTest(unittest.TestCase):
    def test_a_client_without_workspace_routes_is_not_armed(self):
        # Observed live: an agent image built before content-passing has a
        # credential_proxy_client with no workspaces_available at all.
        with patch.object(scan, "credential_proxy_client", types.SimpleNamespace()):
            self.assertFalse(scan.content_mode_available("http://broker"))
        self.assertFalse(scan.content_mode_available(""))

    def test_pages_filters_batches_and_reports_broker_skips(self):
        files = {
            "README.md": b"# nope",
            "apps/a.yaml": PDB_V1BETA1,
            "apps/b.yml": CRONJOB_V1BETA1,
            "apps/c.json": HPA_V2BETA2_LIST_JSON,
            "apps/huge.yaml": b"x" * 10,
            "img/logo.png": b"\x89PNG",
        }
        fake = FakeWorkspace(files, sha="feedbeef", unreadable={"apps/huge.yaml"})
        opened = []

        def open_workspace(endpoint, repo, depth=None, **kwargs):
            opened.append((endpoint, repo, depth))
            return fake

        snapshot = scan.read_repo_content("acme/infra", "http://broker", open_workspace=open_workspace)
        self.assertEqual(opened, [("http://broker", "acme/infra", 1)])
        self.assertEqual(sorted(snapshot.files), ["apps/a.yaml", "apps/b.yml", "apps/c.json"])
        self.assertEqual(snapshot.skipped, [{"path": "apps/huge.yaml", "reason": "tooLarge"}])
        self.assertEqual(snapshot.sha, "feedbeef")
        self.assertEqual(snapshot.source, "repo manifests as of feedbeef")
        self.assertIsNone(snapshot.stopped)
        self.assertTrue(fake.closed)
        self.assertEqual(sum(len(b) for b in fake.read_batches), 4)

    def test_request_budget_deferrals_are_asked_for_again(self):
        files = {"a.yaml": PDB_V1BETA1, "b.yaml": CRONJOB_V1BETA1, "c.yaml": CRONJOB_V1}

        class BudgetedWorkspace(FakeWorkspace):
            page_size = 10

            def read_many(self, paths):
                self.read_batches.append(list(paths))
                if len(self.read_batches) == 1:
                    # The broker served the first path and deferred the rest.
                    return {paths[0]: self.files[paths[0]]}, [{"path": p, "reason": "requestBudget"} for p in paths[1:]]
                return {p: self.files[p] for p in paths}, []

        fake = BudgetedWorkspace(files)
        snapshot = scan.read_repo_content("acme/infra", "http://broker", open_workspace=lambda *a, **k: fake)
        self.assertEqual(sorted(snapshot.files), ["a.yaml", "b.yaml", "c.yaml"])
        self.assertEqual(snapshot.skipped, [])
        self.assertEqual(fake.read_batches, [["a.yaml", "b.yaml", "c.yaml"], ["b.yaml", "c.yaml"]])

    def test_a_deferral_that_never_clears_is_recorded_once_and_ends(self):
        class StuckWorkspace(FakeWorkspace):
            page_size = 10

            def read_many(self, paths):
                self.read_batches.append(list(paths))
                return {}, [{"path": p, "reason": "requestBudget"} for p in paths]

        fake = StuckWorkspace({"a.yaml": PDB_V1BETA1, "b.yaml": CRONJOB_V1})
        snapshot = scan.read_repo_content("acme/infra", "http://broker", open_workspace=lambda *a, **k: fake)
        self.assertEqual(snapshot.files, {})
        self.assertEqual([k["path"] for k in snapshot.skipped], ["a.yaml", "b.yaml"])
        self.assertEqual(len(fake.read_batches), 1)

    def test_file_cap_is_reported(self):
        fake = FakeWorkspace({f"m{i}.yaml": CRONJOB_V1 for i in range(5)})
        snapshot = scan.read_repo_content("acme/infra", "http://broker", max_files=3, open_workspace=lambda *a, **k: fake)
        self.assertEqual(len(snapshot.files), 3)
        self.assertEqual(snapshot.stopped, scan.STOPPED_MAX_FILES)

    def test_byte_cap_is_reported(self):
        fake = FakeWorkspace({"a.yaml": b"x" * 40, "b.yaml": b"y" * 40})
        snapshot = scan.read_repo_content("acme/infra", "http://broker", max_bytes=50, open_workspace=lambda *a, **k: fake)
        self.assertEqual(list(snapshot.files), ["a.yaml"])
        self.assertEqual(snapshot.stopped, scan.STOPPED_MAX_BYTES)


class DirectoryModeTest(unittest.TestCase):
    def test_leased_checkout_and_head_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"apps/pdb.yaml": PDB_V1BETA1, ".git/HEAD": b"ref: refs/heads/main", ".git/x.yaml": CRONJOB_V1BETA1})
            calls = []

            def runner(cmd, *, cwd=None, check=True):
                calls.append((tuple(cmd), cwd))

                class Result:
                    returncode = 0
                    stdout = "cafebabe\n"

                return Result()

            with patch.object(scan.gitops_workspace, "ensure_workspace", return_value=Path(tmp)) as ensure:
                snapshot = scan.read_repo_directory("acme/infra", "lease-1", runner)
            ensure.assert_called_once()
            self.assertEqual(ensure.call_args.kwargs["lease"], "lease-1")
            self.assertEqual(ensure.call_args.kwargs["owner"], scan.OWNER)
            self.assertEqual(calls, [(scan.GIT_HEAD_CMD, tmp)])
            self.assertEqual(snapshot.sha, "cafebabe")
            self.assertEqual(snapshot.source, "repo manifests as of cafebabe")
            self.assertEqual(list(snapshot.files), ["apps/pdb.yaml"])

    def test_clone_failure_surfaces_as_error(self):
        with patch.object(scan.gitops_workspace, "ensure_workspace", side_effect=RuntimeError("no remote branch")):
            report = scan.build_report(scan.load_table(), (1, 24), (1, 27), [], [("acme/infra", lambda: scan.read_repo_directory("acme/infra", "l", lambda *a, **k: None))])
        self.assertEqual(report["sources"]["acme/infra"]["error"], "RuntimeError: no remote branch")


class MainTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = scan.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_local_tree_with_fleet_json_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"tree/apps/pdb.yaml": PDB_V1BETA1 + b"---\n" + CRONJOB_V1BETA1 + b"---\n" + CRONJOB_V1, "tree/apps/t.yaml": HELM_TEMPLATE})
            versions = Path(tmp) / "fleet.json"
            versions.write_text(json.dumps(FLEET_JSON))
            output = Path(tmp) / "out" / "scan.json"
            rc, out, err = self.run_main(["--versions", str(versions), "--manifests-dir", str(Path(tmp) / "tree"), "--output", str(output)])
            self.assertEqual(rc, scan.EXIT_OK, err)
            self.assertIn("# API deprecation scan: 1.24 -> 1.27", out)
            self.assertIn("| apps/pdb.yaml | PodDisruptionBudget | web-pdb | policy/v1beta1 | 1.25 | policy/v1 | p/old |", out)
            self.assertIn("| apps/pdb.yaml | CronJob | nightly | batch/v1beta1 | 1.25 | batch/v1 | p/old |", out)
            self.assertNotIn("| hourly |", out)
            self.assertIn("local directory", out)
            written = json.loads(output.read_text())
            self.assertEqual(written["summary"]["hits"], 2)
            self.assertEqual(written["target_version"], [1, 27])

    def test_explicit_target_overrides_fleet_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"tree/pdb.yaml": PDB_V1BETA1})
            versions = Path(tmp) / "fleet.json"
            versions.write_text(json.dumps(FLEET_JSON))
            rc, out, _ = self.run_main(["--versions", str(versions), "--target-version", "1.24.9", "--manifests-dir", str(Path(tmp) / "tree")])
            self.assertEqual(rc, scan.EXIT_OK)
            self.assertIn("Clean:", out)

    def test_current_version_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"pdb.yaml": PDB_V1BETA1})
            rc, out, _ = self.run_main(["--current-version", "1.24.0", "--target-version", "1.27.0", "--manifests-dir", tmp])
            self.assertEqual(rc, scan.EXIT_OK)
            self.assertIn("| pdb.yaml | PodDisruptionBudget | web-pdb | policy/v1beta1 | 1.25 | policy/v1 | - |", out)

    def test_usage_errors(self):
        rc, _, err = self.run_main(["--target-version", "1.27.0", "--manifests-dir", "."])
        self.assertEqual(rc, scan.EXIT_USAGE)
        self.assertIn("--versions", err)
        rc, _, err = self.run_main(["--current-version", "old", "--target-version", "1.27.0", "--manifests-dir", "."])
        self.assertEqual(rc, scan.EXIT_USAGE)
        rc, _, err = self.run_main(["--current-version", "1.24", "--target-version", "next", "--manifests-dir", "."])
        self.assertEqual(rc, scan.EXIT_USAGE)
        rc, _, err = self.run_main(["--current-version", "1.24", "--manifests-dir", "."])
        self.assertEqual(rc, scan.EXIT_USAGE)
        self.assertIn("--target-version", err)
        with tempfile.TemporaryDirectory() as tmp:
            versions = Path(tmp) / "fleet.json"
            versions.write_text(json.dumps({"members": [{"cluster": "x", "control_plane_version": None}]}))
            rc, _, err = self.run_main(["--versions", str(versions), "--target-version", "1.27", "--manifests-dir", tmp])
            self.assertEqual(rc, scan.EXIT_USAGE)
            self.assertIn("--current-version", err)

    def test_missing_directory_is_an_error_not_a_crash(self):
        rc, out, _ = self.run_main(["--current-version", "1.24", "--target-version", "1.27", "--manifests-dir", "/nonexistent/tree"])
        self.assertEqual(rc, scan.EXIT_PARTIAL)
        self.assertIn("not scanned", out)

    def test_no_managed_repos_is_a_clean_empty_report(self):
        with patch.object(scan.gitops_workspace, "get_managed_github_repos", return_value=[]), patch.object(scan, "content_mode_available", return_value=False):
            rc, out, _ = self.run_main(["--current-version", "1.24", "--target-version", "1.27"])
        self.assertEqual(rc, scan.EXIT_OK)
        self.assertIn(scan.NO_REPOS_HEADING, out)

    def test_managed_repos_go_through_the_broker_when_armed(self):
        fake = FakeWorkspace({"apps/pdb.yaml": PDB_V1BETA1}, sha="0123abc")
        with patch.object(scan.gitops_workspace, "get_managed_github_repos", return_value=["acme/infra"]), patch.object(
            scan, "content_mode_available", return_value=True
        ), patch.object(scan.credential_proxy_client.Workspace, "open", return_value=fake), patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://broker"}):
            rc, out, _ = self.run_main(["--current-version", "1.24", "--target-version", "1.27"])
        self.assertEqual(rc, scan.EXIT_OK)
        self.assertIn("## acme/infra — repo manifests as of 0123abc", out)
        self.assertIn("| apps/pdb.yaml | PodDisruptionBudget |", out)

    def test_named_repo_falls_back_to_a_leased_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, {"pdb.yaml": PDB_V1BETA1})
            runner_calls = []

            def runner(cmd, *, cwd=None, check=True):
                runner_calls.append(tuple(cmd))

                class Result:
                    returncode = 0
                    stdout = "d00d\n"

                return Result()

            with patch.object(scan, "content_mode_available", return_value=False), patch.object(scan, "_runner", runner), patch.object(
                scan.gitops_workspace, "ensure_workspace", return_value=Path(tmp)
            ) as ensure, patch.dict(os.environ, {"HERMES_SESSION_ID": "session-7", "HERMES_KANBAN_TASK": ""}):
                rc, out, _ = self.run_main(["--repo", "acme/infra", "--current-version", "1.24", "--target-version", "1.27"])
        self.assertEqual(rc, scan.EXIT_OK)
        self.assertIn("## acme/infra — repo manifests as of d00d", out)
        self.assertIn(scan.GIT_HEAD_CMD, runner_calls)
        # Not the session lease: that clone is the tree submit-suggestion's
        # prepare handed the agent, and ensure_workspace(reset=True) scrubs it.
        self.assertEqual(ensure.call_args.kwargs["lease"], "api-deprecation-scan-session-7")


class ScanLeaseTest(unittest.TestCase):
    def test_explicit_lease_is_used_as_given(self):
        self.assertEqual(scan.scan_lease("my lease"), "my-lease")

    def test_default_lease_is_the_sessions_with_the_scan_prefix(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "card-42"}):
            self.assertEqual(scan.scan_lease(None), "api-deprecation-scan-card-42")
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_SESSION_ID": ""}):
            self.assertTrue(scan.scan_lease(None).startswith("api-deprecation-scan-adhoc-"))

    def test_a_long_session_lease_still_differs_from_the_scan_lease(self):
        long_lease = "x" * 80
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": long_lease}):
            self.assertNotEqual(scan.scan_lease(None), scan.gitops_workspace.lease_id(None))


if __name__ == "__main__":
    unittest.main()
