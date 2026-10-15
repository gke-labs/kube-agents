"""Unit tests for report_query — the bounded read side of the report store.

Run:
  python3 -m unittest discover -s agents/platform/skills/fleet-audit-reports/scripts \
      -p 'test_report_query.py' -v

Stdlib only, matching the other agent-script tests. No pod and no cluster: the
store is a directory of JSON files, so every case here builds one in a temp
directory and drives the CLI through `main`.

The property most of these tests defend is boundedness. `finding` is the
only subcommand allowed to return prose, and PROSE is planted in every
evidence, impact and recommendation field so that a subcommand which starts
leaking the document fails here rather than in a context window.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_query  # noqa: E402

AUDIT = "compliance-audit"
OTHER = "obtainability-audit"
PROSE = "PROSE-MARKER"
REPO = "acme/fleet"


def without_helpers():
    """A fresh copy of the module, loaded where `import report_status` fails.

    The import happens once, at module load, so the guard is exercised by
    loading the file again with the sibling masked in `sys.modules` — the
    message then comes from the module's own `except`, not from this test.
    """
    spec = importlib.util.spec_from_file_location(
        "report_query_without_helpers", report_query.__file__
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"report_status": None}):
        spec.loader.exec_module(module)
    return module


def finding(fid, severity="critical", cluster="prod-us-east", check="netpol-missing"):
    """A finding shaped exactly as the validated document carries one."""
    return {
        "id": fid,
        "severity": severity,
        "title": f"{fid} needs attention",
        "cluster": cluster,
        "namespace": "payments",
        "object": "Namespace/payments",
        "evidence": {
            "command": "kubectl --context prod-us-east get networkpolicy -n payments",
            "excerpt": f"{PROSE} excerpt",
        },
        "impact": f"{PROSE} impact",
        "recommendation": {
            "action": f"{PROSE} action",
            "rationale": f"{PROSE} rationale",
            "risk": f"{PROSE} risk",
        },
        "check": check,
    }


def scope_document(audit_id, findings, clusters):
    """A document whose scope carries the check commands the ledger publishes."""
    return {
        "audit": audit_id,
        "scope": {"clusters": clusters, "skipped": []},
        "findings": findings,
    }


def finished_at_for(stamp):
    """The `finished_at` a writer would pair with a ring stamp.

    A parseable value matters: the reader compares it with the ring to find an
    entry newer than `latest.json`, and an unparseable one skips that path.
    """
    try:
        at = datetime.strptime(stamp, report_query.report_status.RUN_STAMP_FORMAT)
    except ValueError:
        return f"{stamp}+00:00"
    return at.replace(tzinfo=timezone.utc).isoformat()


def envelope(audit_id, finished_at, findings, **overrides):
    """One run's envelope, the keys `audit_report.report_envelope` writes."""
    body = {
        "audit_id": audit_id,
        "finished_at": finished_at,
        "status": "UPDATED",
        "issue_number": 128,
        "issue_url": "https://github.com/acme/fleet/issues/128",
        "partial": False,
        "coverage_gaps": [],
        "repo": "acme/fleet",
        "ledger_body": f"{PROSE} ledger body",
        "prs_opened": [],
        "prs_closed": [],
        "silent_ok": False,
        "new_ids": [],
        "resolved_ids": [],
        "current_ids": sorted(f["id"] for f in findings),
        "id_scheme": 2,
        "document": {
            "audit": audit_id,
            "scope": {
                "clusters": [{"name": "prod-us-east"}, {"name": "prod-autopilot"}],
                "skipped": [{"cluster": "dr-west", "reason": "control plane unreachable"}],
            },
            "findings": findings,
        },
    }
    body.update(overrides)
    return body


class StoreTestCase(unittest.TestCase):
    """A store on disk, and the CLI driven over it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="report-store-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # Liveness reads the in-flight notes `start` leaves in the scratch
        # directory, which the CLI takes from the environment.
        self.scratch = tempfile.mkdtemp(prefix="report-scratch-")
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        env = patch.dict(os.environ, {"FLEET_AUDIT_SCRATCH_DIR": self.scratch})
        env.start()
        self.addCleanup(env.stop)

    def stream_dir(self, audit_id, repo=REPO):
        """The store one stream keeps for one repository."""
        path = Path(self.root) / audit_id / repo
        (path / "runs").mkdir(parents=True, exist_ok=True)
        return path

    def write_run(self, audit_id, stamp, findings, *, latest=True, repo=REPO, **overrides):
        """One ring entry, and (by default) the `latest.json` copy of it."""
        directory = self.stream_dir(audit_id, repo)
        text = json.dumps(
            envelope(audit_id, finished_at_for(stamp), findings, repo=repo, **overrides),
            indent=2,
            sort_keys=True,
        )
        (directory / "runs" / f"{stamp}.json").write_text(text, encoding="utf-8")
        if latest:
            (directory / "latest.json").write_text(text, encoding="utf-8")
        return f"{stamp}.json"

    def write_claim(self, audit_id, age_s):
        """The in-flight note `start` leaves while a run holds the stream."""
        self.stream_dir(audit_id)
        (Path(self.scratch) / f"inflight_{audit_id}.json").write_text(
            json.dumps({"audit": audit_id, "started_at": time.time() - age_s}),
            encoding="utf-8",
        )

    def query(self, *argv):
        """The CLI, as its caller sees it: an exit code and one JSON object."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report_query.main(["--root", self.root, *argv])
        return code, json.loads(out.getvalue())

    def ok(self, *argv):
        code, payload = self.query(*argv)
        self.assertEqual(code, 0, payload.get("error"))
        self.assertIsNone(payload["error"])
        return payload

    def refused(self, *argv):
        code, payload = self.query(*argv)
        self.assertEqual(code, 2)
        self.assertTrue(payload["error"])
        return payload


class TestSharedHelpers(StoreTestCase):
    """The two readers share one parser of the envelope."""

    def test_the_helpers_come_from_the_sibling_writer_skill(self):
        self.assertIsNotNone(
            report_query.report_status,
            f"report_status was not importable from {report_query.HELPERS_DIR}",
        )
        self.assertEqual(report_query.HELPERS_DIR.name, "scripts")
        self.assertEqual(report_query.HELPERS_DIR.parent.name, "fleet-audit")
        self.assertTrue((report_query.HELPERS_DIR / "report_status.py").is_file())
        self.assertEqual(
            Path(report_query.report_status.__file__).resolve(),
            report_query.HELPERS_DIR / "report_status.py",
        )

    def test_a_missing_sibling_is_reported_and_never_fallen_back_from(self):
        """The guard names the path it looked in and answers nothing else."""
        self.assertIsNone(report_query.IMPORT_ERROR)
        module = without_helpers()
        self.assertIsNone(module.report_status)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = module.main(["--root", self.root, "streams"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertIn("cannot import report_status", payload["error"])
        self.assertIn(str(report_query.HELPERS_DIR), payload["error"])
        self.assertIn("fleet-audit skill must be installed", payload["error"])
        self.assertEqual(payload["looked_in"], str(report_query.HELPERS_DIR))
        self.assertNotIn("streams", payload)


class TestArgumentsStayInsideTheStore(StoreTestCase):
    """The stream id and `--run` are joined onto the store root, so both have
    to be names rather than paths. This subcommand set is the constrained way
    to read the store; a `../` that escapes it defeats the whole point."""

    def setUp(self):
        super().setUp()
        self.write_run(AUDIT, "20260826T063100.000000Z", [])
        self.outside = Path(self.root).parent / f"{Path(self.root).name}-outside.json"
        self.outside.write_text(json.dumps(envelope(AUDIT, "2026-08-26T06:31:00+00:00", [])), encoding="utf-8")
        self.addCleanup(self.outside.unlink, missing_ok=True)

    def test_a_run_that_walks_out_of_the_ring_is_refused(self):
        escape = os.path.relpath(self.outside, Path(self.root) / AUDIT / REPO / "runs")[: -len(".json")]
        payload = self.refused("show", AUDIT, "--run", escape)
        self.assertIn("not a name inside the report store", payload["error"])

    def test_a_stream_that_walks_out_of_the_store_is_refused(self):
        payload = self.refused("show", f"../{Path(self.root).name}/{AUDIT}")
        self.assertIn("not a name inside the report store", payload["error"])

    def test_the_ordinary_stamp_and_stream_still_resolve(self):
        self.assertEqual(self.ok("show", AUDIT)["run"], "latest.json")
        self.assertEqual(
            self.ok("show", AUDIT, "--run", "20260826T063100.000000Z")["run"],
            "20260826T063100.000000Z.json",
        )


class TestStreams(StoreTestCase):
    def test_a_completed_stream_reports_its_counts(self):
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [finding("a"), finding("b", severity="minor")],
            new_ids=["a"],
            resolved_ids=["x", "y"],
        )
        row = self.ok("streams")["streams"][0]
        self.assertEqual(row["audit_id"], AUDIT)
        self.assertEqual(row["liveness"], "completed")
        self.assertEqual(row["findings"], 2)
        self.assertEqual(row["critical"], 1)
        self.assertEqual(row["new"], 1)
        self.assertEqual(row["resolved"], 2)
        self.assertEqual(row["current"], 2)
        self.assertEqual(row["clusters"], 2)
        self.assertEqual(row["skipped"], 1)
        self.assertEqual(row["runs"], 1)
        self.assertEqual(row["issue_number"], 128)

    def test_gaps_are_a_count_not_eight_streams_of_prose(self):
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [],
            partial=True,
            coverage_gaps=["dr-west: control plane unreachable", "prod-autopilot: 7/11 checks"],
        )
        row = self.ok("streams")["streams"][0]
        self.assertTrue(row["partial"])
        self.assertEqual(row["gaps"], 2)

    def test_a_stray_directory_reaches_the_rows_that_read_fine(self):
        # The stray belongs to no repository, so without the stream's error on
        # the row the answer would say "error" liveness with a null error.
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")], repo="Zeta/Fleet")
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        (row,) = payload["streams"]
        self.assertEqual(row["repo"], REPO)
        self.assertIn("Zeta/Fleet: not lower-case", row["error"])
        self.assertIn(AUDIT, payload["error"])

    def test_running_and_never_are_told_apart(self):
        self.write_claim(AUDIT, age_s=60)
        self.stream_dir(OTHER)
        rows = {row["audit_id"]: row for row in self.ok("streams")["streams"]}
        self.assertEqual(rows[AUDIT]["liveness"], "running")
        self.assertLess(rows[AUDIT]["age_s"], 7200)
        self.assertEqual(rows[OTHER]["liveness"], "never")
        self.assertIsNone(rows[OTHER]["finished_at"])

    def test_a_first_run_in_flight_is_running_before_it_has_a_report(self):
        (Path(self.scratch) / f"inflight_{OTHER}.json").write_text(
            json.dumps({"audit": OTHER, "started_at": time.time() - 30}), encoding="utf-8"
        )
        self.write_run(AUDIT, "20260826T063100.000000Z", [])
        rows = {row["audit_id"]: row for row in self.ok("streams")["streams"]}
        self.assertEqual(rows[OTHER]["liveness"], "running")
        payload = self.refused("show", OTHER)
        self.assertEqual(payload["liveness"], "running")

    def test_a_first_run_in_flight_is_running_before_the_store_exists(self):
        """A fresh volume: no `finish` has made the root yet, and the lease in
        scratch says the first run is in flight."""
        shutil.rmtree(self.root)
        (Path(self.scratch) / f"inflight_{OTHER}.json").write_text(
            json.dumps({"audit": OTHER, "started_at": time.time() - 30}), encoding="utf-8"
        )
        rows = {row["audit_id"]: row for row in self.query("streams")[1]["streams"]}
        self.assertEqual(rows[OTHER]["liveness"], "running")
        code, payload = self.query("show", OTHER)
        self.assertEqual(code, 2)
        self.assertIn("report store not found", payload["error"])
        self.assertEqual(payload["liveness"], "running")

    def test_a_run_past_the_ceiling_is_dead(self):
        self.write_claim(AUDIT, age_s=report_query.report_status.INFLIGHT_TTL_S + 60)
        self.assertEqual(self.ok("streams")["streams"][0]["liveness"], "died")

    def test_an_absent_root_is_an_error_not_an_empty_fleet(self):
        shutil.rmtree(self.root)
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertFalse(payload["root_exists"])
        self.assertIsNone(payload["root_error"])
        self.assertIn(f"report store not found at {self.root}", payload["error"])
        self.assertIn("unknown, not clean", payload["error"])
        self.assertNotIn("not readable", payload["error"])
        self.assertEqual(payload["streams"], [])
        # The per-stream subcommands give the same answer.
        self.assertEqual(self.query("show", AUDIT)[1]["error"], payload["error"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root searches a mode-0644 directory")
    def test_a_listable_but_unsearchable_root_says_the_stream_is_unreadable(self):
        """Mode 0644 lists the stream but refuses the stat of its directory:
        the per-stream answer is the unreadable one `streams` gives, never
        "no reports for stream"."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [])
        os.chmod(self.root, 0o644)
        self.addCleanup(os.chmod, self.root, 0o700)
        streams = self.query("streams")[1]
        self.assertIn("streams that could not be read", streams["error"])
        self.assertIn(AUDIT, streams["error"])
        code, payload = self.query("show", AUDIT)
        self.assertEqual(code, 2)
        self.assertIn(f"streams that could not be read: {AUDIT}", payload["error"])
        self.assertIn("unknown, not clean", payload["error"])
        self.assertNotIn("no reports for stream", payload["error"])
        self.assertEqual(payload["liveness"], "error")
        row = next(row for row in streams["streams"] if row["audit_id"] == AUDIT)
        self.assertEqual(payload["stream_error"], row["error"])

    def test_an_unsearchable_directory_root_is_unreadable_for_a_stream(self):
        """A mode-denied root passes `isdir`; the per-stream answer must be the
        unreadable-store one `streams` gives, not an absent stream."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [])
        denied = PermissionError(13, "Permission denied", self.root)
        with patch.object(report_query.report_status, "stream_ids", side_effect=denied):
            streams = self.query("streams")[1]
            code, payload = self.query("show", AUDIT)
        self.assertEqual(code, 2)
        self.assertFalse(payload["root_exists"])
        self.assertEqual(payload["root_error"], "Permission denied")
        self.assertEqual(payload["error"], streams["error"])
        self.assertIn("unknown, not clean", payload["error"])

    def test_an_unlistable_lease_directory_over_an_empty_store_is_an_error(self):
        # No stream directory means no row to carry the lease error, and a
        # first run in flight is what the unlisted directory would have shown.
        denied = PermissionError(13, "Permission denied")
        with patch.object(report_query.report_status, "in_flight_ids", side_effect=denied):
            code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertEqual(payload["streams"], [])
        self.assertIn("in-flight leases not readable", payload["error"])

    def test_an_unlistable_store_says_why(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [])
        denied = PermissionError(13, "Permission denied")
        with patch.object(report_query.report_status, "stream_ids", side_effect=denied):
            code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertFalse(payload["root_exists"])
        self.assertIn("Permission denied", payload["root_error"])
        self.assertIn("not readable", payload["error"])
        self.assertIn("Permission denied", payload["error"])

    def test_a_file_root_is_unreadable_for_every_subcommand_and_named_once(self):
        """`streams` and a per-stream subcommand give the same answer, and the
        reason does not repeat the path the message already names."""
        shutil.rmtree(self.root)
        Path(self.root).write_text("not a directory", encoding="utf-8")
        self.addCleanup(Path(self.root).unlink, missing_ok=True)
        for argv in (("streams",), ("findings", AUDIT), ("show", AUDIT)):
            code, payload = self.query(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(f"not readable at {self.root} (Not a directory)", payload["error"])
            self.assertEqual(payload["error"].count(self.root), 1, argv)
            self.assertEqual(payload["root_error"], "Not a directory", argv)

    def test_an_unlistable_lease_directory_blames_the_leases_not_the_stores(self):
        # `project` stamps the lease failure on every stream, so an answer that
        # listed those streams would call readable stores unreadable.
        self.write_run(AUDIT, "2026-07-27T08:00:00", [])
        denied = PermissionError(13, "Permission denied")
        with patch.object(report_query.report_status, "in_flight_ids", side_effect=denied):
            code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertIn("in-flight leases not readable", payload["error"])
        self.assertNotIn("streams that could not be read", payload["error"])
        self.assertIn("Permission denied", payload["lease_error"])

    def test_an_unparseable_envelope_is_an_error_row_and_a_nonzero_exit(self):
        self.stream_dir(AUDIT)
        (Path(self.root) / AUDIT / REPO / "latest.json").write_text("{not json", encoding="utf-8")
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertIn(AUDIT, payload["error"])
        row = payload["streams"][0]
        self.assertEqual(row["liveness"], "error")
        self.assertIn("latest.json", row["error"])

    def test_one_broken_stream_does_not_hide_the_others(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [finding("q")])
        self.stream_dir(AUDIT)
        (Path(self.root) / AUDIT / REPO / "latest.json").write_text("[]", encoding="utf-8")
        rows = {row["audit_id"]: row for row in self.query("streams")[1]["streams"]}
        self.assertEqual(rows[OTHER]["findings"], 1)
        self.assertEqual(rows[AUDIT]["liveness"], "error")


class TestShow(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.older = self.write_run(
            AUDIT, "20260825T063100.000000Z", [finding("a")], latest=False
        )
        self.newest = self.write_run(
            AUDIT, "20260826T063100.000000Z", [finding("a"), finding("b", severity="major")]
        )

    def test_the_document_never_crosses_this_boundary(self):
        payload = self.ok("show", AUDIT)
        self.assertEqual(payload["run"], "latest.json")
        self.assertNotIn("document", payload["envelope"])
        self.assertNotIn(PROSE, json.dumps(payload))

    def test_latest_missing_is_always_a_boolean(self):
        # As on `findings`, `finding` and `checks`: false is said, not implied.
        self.assertIs(self.ok("show", AUDIT)["latest_missing"], False)
        self.assertIs(self.ok("show", AUDIT, "--run", self.older)["latest_missing"], False)

    def test_it_carries_the_outcome_and_the_delta(self):
        row = self.ok("show", AUDIT)["envelope"]
        self.assertEqual(row["status"], "UPDATED")
        self.assertEqual(row["issue_url"], "https://github.com/acme/fleet/issues/128")
        self.assertEqual(row["findings"], 2)
        self.assertEqual(row["critical"], 1)
        self.assertEqual(row["repo"], "acme/fleet")
        self.assertNotIn("ledger_body", row)
        self.assertEqual(row["coverage_gaps"], [])

    def test_a_stamp_reads_that_run_with_or_without_the_extension(self):
        for spelling in (self.older, self.older[: -len(".json")]):
            with self.subTest(run=spelling):
                payload = self.ok("show", AUDIT, "--run", spelling)
                self.assertEqual(payload["run"], self.older)
                self.assertEqual(payload["envelope"]["findings"], 1)

    def test_root_may_follow_the_subcommand(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report_query.main(["show", AUDIT, "--root", self.root])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["root"], self.root)

    def test_a_missing_latest_answers_from_the_newest_ring_entry(self):
        """A failed `finish` deletes `latest.json` and leaves the ring: the
        store still holds a record, flagged as possibly behind the ledger."""
        (Path(self.root) / AUDIT / REPO / "latest.json").unlink()
        payload = self.ok("show", AUDIT)
        self.assertEqual(payload["run"], self.newest)
        self.assertIs(payload["latest_missing"], True)
        self.assertNotIn("latest_missing", payload["envelope"])
        self.assertTrue(self.ok("findings", AUDIT)["latest_missing"])
        self.assertTrue(self.ok("checks", AUDIT)["latest_missing"])
        self.assertFalse(self.ok("findings", AUDIT, "--run", self.older)["latest_missing"])

    def test_a_corrupt_ring_entry_behind_a_missing_latest_is_named(self):
        (Path(self.root) / AUDIT / REPO / "latest.json").unlink()
        (Path(self.root) / AUDIT / REPO / "runs" / self.newest).write_text("{", encoding="utf-8")
        payload = self.refused("show", AUDIT)
        self.assertIn(f"runs/{self.newest}", payload["error"])
        self.assertIn("latest.json in acme/fleet is gone", payload["error"])

    def test_github_named_with_its_host_answers_from_the_same_store(self):
        # Review round 4: on a mixed install every surface hands out
        # `github.com/owner/name`, and `--repo` with it was refused as "no
        # reports" for the store it had just found.
        payload = self.ok("show", AUDIT, "--repo", f"github.com/{REPO}")
        self.assertEqual(payload["repo"], REPO)

    def test_repository_casing_is_not_a_second_store(self):
        payload = self.ok("show", AUDIT, "--repo", REPO.upper())
        self.assertEqual(payload["repo"], REPO)

    def test_an_absent_stamp_names_the_ring(self):
        payload = self.refused("show", AUDIT, "--run", "20990101T000000.000000Z")
        self.assertIn("20990101T000000.000000Z.json", payload["error"])
        self.assertEqual(payload["runs"], [self.older, self.newest])

    def test_an_absent_stream_lists_the_streams_that_exist(self):
        payload = self.refused("show", "ai-security-audit")
        self.assertIn("ai-security-audit", payload["error"])
        self.assertEqual(payload["streams"], [AUDIT])

    def test_an_absent_store_says_so_rather_than_answering(self):
        shutil.rmtree(self.root)
        payload = self.refused("show", AUDIT)
        self.assertFalse(payload["root_exists"])
        self.assertIn("report store not found", payload["error"])


class TestUnknownIsNotClean(StoreTestCase):
    """A stream with no stored run has no record, which is not a clean run."""

    def test_a_missing_latest_is_unknown(self):
        self.stream_dir(AUDIT)
        payload = self.refused("show", AUDIT)
        self.assertIn("unknown, not clean", payload["error"])
        self.assertEqual(payload["liveness"], "never")
        self.assertEqual(payload["runs"], [])

    def test_a_failed_write_leaves_the_ring_and_answers_flagged(self):
        """`write_report` and `finish` unlink `latest.json` when a run fails,
        so the ring can hold entries while the newest record is gone. The
        newest entry is still a run the stream completed: answered from, and
        flagged, never reported as a stream that never ran."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        os.unlink(Path(self.root) / AUDIT / REPO / "latest.json")
        payload = self.ok("findings", AUDIT)
        self.assertEqual(payload["run"], "20260826T063100.000000Z.json")
        self.assertTrue(payload["latest_missing"])
        row = self.ok("streams")["streams"][0]
        self.assertEqual(row["liveness"], "completed")
        self.assertTrue(row["latest_missing"])

    def test_a_ring_entry_newer_than_latest_answers_flagged(self):
        """A held-open run whose `latest.json` write failed leaves the ring a
        run ahead of the file; the CLI answers from the ring and says so."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        newer = self.write_run(AUDIT, "20260902T063100.000000Z", [finding("b")], latest=False)
        payload = self.ok("findings", AUDIT)
        self.assertEqual(payload["run"], newer)
        self.assertTrue(payload["latest_missing"])
        self.assertEqual([f["id"] for f in payload["findings"]], ["b"])

    def test_the_run_named_is_the_run_read_when_a_finish_lands_between(self):
        """The ring entry the fallback answers from is named from the same
        listing that picked it: a `finish` writing a newer entry mid-query
        must not pair the new stamp with the older entry's content."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        os.unlink(Path(self.root) / AUDIT / REPO / "latest.json")
        real = report_query.report_status.list_runs

        def then_a_finish_lands(*args):
            names = real(*args)
            self.write_run(AUDIT, "20260902T063100.000000Z", [], latest=False)
            return names

        with patch.object(report_query.report_status, "list_runs", then_a_finish_lands):
            payload = self.ok("findings", AUDIT)
        self.assertEqual(payload["run"], "20260826T063100.000000Z.json")
        self.assertTrue(payload["latest_missing"])


class TestFindings(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [
                finding("minor-one", severity="minor", cluster="prod-autopilot", check="hostpath"),
                finding("crit-one"),
                finding("major-one", severity="major", cluster="prod-autopilot"),
                finding("crit-two", cluster="prod-autopilot"),
            ],
        )

    def test_identity_columns_only(self):
        payload = self.ok("findings", AUDIT)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["returned"], 4)
        self.assertFalse(payload["truncated"])
        for row in payload["findings"]:
            self.assertEqual(
                sorted(row), ["check", "cluster", "id", "severity", "title"]
            )
        self.assertNotIn(PROSE, json.dumps(payload))

    def test_severity_first_so_truncation_eats_the_least_severe_end(self):
        payload = self.ok("findings", AUDIT, "--limit", "2")
        self.assertEqual([row["id"] for row in payload["findings"]], ["crit-one", "crit-two"])
        self.assertEqual(payload["matched"], 4)
        self.assertEqual(payload["returned"], 2)
        self.assertTrue(payload["truncated"])

    def test_the_filters_are_exact_and_case_insensitive(self):
        cases = (
            (("--severity", "critical"), ["crit-one", "crit-two"]),
            (("--severity", "CRITICAL"), ["crit-one", "crit-two"]),
            (("--cluster", "prod-us-east"), ["crit-one"]),
            (("--check", "hostpath"), ["minor-one"]),
            (("--severity", "critical", "--cluster", "prod-autopilot"), ["crit-two"]),
            (("--cluster", "dr-west"), []),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                payload = self.ok("findings", AUDIT, *flags)
                self.assertEqual([row["id"] for row in payload["findings"]], expected)
                self.assertEqual(payload["matched"], len(expected))
                self.assertEqual(payload["total"], 4)

    def test_a_clean_run_is_zero_findings_and_not_an_error(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [], status="CLEAN")
        payload = self.ok("findings", OTHER)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["status"], "CLEAN")

    def test_a_document_that_is_not_a_document_is_refused(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [], document=None)
        self.assertIn("no findings document", self.refused("findings", OTHER)["error"])


class TestFinding(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [finding("netpol-missing-payments"), finding("other", severity="minor")],
        )

    def test_the_one_path_that_returns_prose(self):
        payload = self.ok("finding", AUDIT, "netpol-missing-payments")
        body = payload["finding"]
        self.assertEqual(body["id"], "netpol-missing-payments")
        self.assertEqual(body["impact"], f"{PROSE} impact")
        self.assertEqual(body["recommendation"]["risk"], f"{PROSE} risk")
        self.assertEqual(body["evidence"]["excerpt"], f"{PROSE} excerpt")

    def test_an_unknown_id_offers_candidates_rather_than_the_roster(self):
        payload = self.refused("finding", AUDIT, "netpol-missing-payment")
        self.assertIn("netpol-missing-payment", payload["error"])
        self.assertEqual(payload["available"], 2)
        self.assertEqual(payload["ids"], ["netpol-missing-payments", "other"])

    def test_the_hint_list_is_capped(self):
        many = [finding(f"f{index:03d}", severity="minor") for index in range(60)]
        self.write_run(OTHER, "20260826T070500.000000Z", many)
        payload = self.refused("finding", OTHER, "nope")
        self.assertEqual(payload["available"], 60)
        self.assertEqual(len(payload["ids"]), report_query.MAX_ID_HINTS)


class TestDiff(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.write_run(
            AUDIT, "20260824T063100.000000Z", [finding("a"), finding("b")], latest=False
        )
        self.second = self.write_run(
            AUDIT, "20260825T063100.000000Z", [finding("b"), finding("c")], latest=False
        )
        self.third = self.write_run(
            AUDIT, "20260826T063100.000000Z", [finding("b"), finding("d", severity="minor")]
        )

    def test_it_defaults_to_the_newest_two(self):
        payload = self.ok("diff", AUDIT)
        self.assertEqual((payload["from"], payload["to"]), (self.second, self.third))
        self.assertEqual([row["id"] for row in payload["added"]], ["d"])
        self.assertEqual([row["id"] for row in payload["resolved"]], ["c"])
        self.assertEqual(payload["unchanged"], 1)
        self.assertNotIn(PROSE, json.dumps(payload))

    def test_added_and_resolved_carry_titles(self):
        self.assertEqual(
            self.ok("diff", AUDIT)["added"][0]["title"], "d needs attention"
        )

    def test_explicit_stamps_span_the_whole_ring(self):
        payload = self.ok("diff", AUDIT, "--from", self.first, "--to", self.third)
        self.assertEqual([row["id"] for row in payload["added"]], ["d"])
        self.assertEqual([row["id"] for row in payload["resolved"]], ["a"])
        self.assertEqual(payload["added_total"], 1)
        self.assertEqual(payload["resolved_total"], 1)

    def test_reversed_stamps_are_refused_rather_than_swapped(self):
        payload = self.refused("diff", AUDIT, "--from", self.third, "--to", self.first)
        self.assertIn("is not older than", payload["error"])

    def test_to_alone_diffs_against_the_run_before_it(self):
        payload = self.ok("diff", AUDIT, "--to", self.second)
        self.assertEqual(payload["from"], self.first)
        self.assertEqual([row["id"] for row in payload["resolved"]], ["a"])

    def test_a_partial_run_is_flagged_so_unseen_does_not_read_as_fixed(self):
        payload = self.ok("diff", AUDIT)
        self.assertFalse(payload["from_partial"])
        self.assertFalse(payload["to_partial"])
        later = self.write_run(AUDIT, "20260904T060000.000000Z", [], partial=True)
        payload = self.ok("diff", AUDIT, "--to", later)
        self.assertTrue(payload["to_partial"])

    def test_a_run_that_held_the_ledger_open_is_flagged(self):
        payload = self.ok("diff", AUDIT)
        self.assertFalse(payload["to_held_open"])
        later = self.write_run(AUDIT, "20260904T060000.000000Z", [], ledger_held_open=True)
        payload = self.ok("diff", AUDIT, "--to", later)
        self.assertTrue(payload["to_held_open"])
        self.assertTrue(self.ok("show", AUDIT)["envelope"]["ledger_held_open"])
        self.assertTrue(self.ok("findings", AUDIT)["ledger_held_open"])
        rows = [row for row in self.ok("streams")["streams"] if row["audit_id"] == AUDIT]
        self.assertTrue(rows[0]["ledger_held_open"])

    def test_a_withheld_delta_is_carried_by_streams_and_show(self):
        self.write_run(AUDIT, "20260904T060000.000000Z", [], delta_known=False)
        self.assertIs(self.ok("show", AUDIT)["envelope"]["delta_known"], False)
        rows = [row for row in self.ok("streams")["streams"] if row["audit_id"] == AUDIT]
        self.assertIs(rows[0]["delta_known"], False)

    def test_the_oldest_entry_has_nothing_behind_it(self):
        payload = self.refused("diff", AUDIT, "--to", self.first)
        self.assertIn("oldest entry", payload["error"])
        self.assertEqual(len(payload["runs"]), 3)

    def test_an_empty_ring_is_refused(self):
        self.stream_dir(OTHER)
        self.assertIn("ring is empty", self.refused("diff", OTHER)["error"])

    def test_a_single_entry_ring_is_refused(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [finding("z")])
        self.assertIn("oldest entry", self.refused("diff", OTHER)["error"])

    def test_an_unknown_stamp_names_the_ring(self):
        payload = self.refused("diff", AUDIT, "--to", "20990101T000000.000000Z")
        self.assertEqual(payload["runs"], [self.first, self.second, self.third])

    def test_the_lists_are_bounded(self):
        self.write_run(
            OTHER,
            "20260825T070500.000000Z",
            [finding(f"old{index}", severity="minor") for index in range(5)],
            latest=False,
        )
        self.write_run(
            OTHER,
            "20260826T070500.000000Z",
            [finding(f"new{index}", severity="minor") for index in range(5)],
        )
        payload = self.ok("diff", OTHER, "--limit", "2")
        self.assertEqual(len(payload["added"]), 2)
        self.assertEqual(payload["added_total"], 5)
        self.assertEqual(payload["resolved_total"], 5)
        self.assertTrue(payload["truncated"])


class TestRepositories(StoreTestCase):
    """A stream an SOP finishes once per managed repository keeps one store
    per repository, and no answer mixes two."""

    def setUp(self):
        super().setUp()
        self.write_run(AUDIT, "20260825T063100.000000Z", [finding("a")])
        self.write_run(
            AUDIT, "20260825T064100.000000Z", [finding("z")], repo="acme/other", issue_number=7
        )

    def test_streams_carries_a_row_per_repository(self):
        rows = self.ok("streams")["streams"]
        self.assertEqual(
            [(row["audit_id"], row["repo"], row["issue_number"]) for row in rows],
            [(AUDIT, REPO, 128), (AUDIT, "acme/other", 7)],
        )

    def test_a_per_run_question_names_one_or_is_refused(self):
        payload = self.refused("findings", AUDIT)
        self.assertIn("name one with --repo", payload["error"])
        self.assertEqual(payload["repos"], [REPO, "acme/other"])
        chosen = self.ok("findings", AUDIT, "--repo", "acme/other")
        self.assertEqual(chosen["repo"], "acme/other")
        self.assertEqual([row["id"] for row in chosen["findings"]], ["z"])

    def test_a_corrupt_sibling_names_itself_on_the_healthy_row(self):
        # Liveness is the stream's, so the healthy row reads `error` too, and
        # must say why rather than carry a null error beside counts.
        (Path(self.root) / AUDIT / "acme/other" / "latest.json").write_text("{", encoding="utf-8")
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        rows = {row["repo"]: row for row in payload["streams"]}
        self.assertEqual(rows[REPO]["liveness"], "error")
        self.assertIn("acme/other", rows[REPO]["error"])

    def test_a_corrupt_sibling_names_itself_while_the_stream_holds_a_lease(self):
        # A lease makes liveness `running`, which must not clear the sibling's
        # error from the healthy row.
        (Path(self.root) / AUDIT / "acme/other" / "latest.json").write_text("{", encoding="utf-8")
        self.write_claim(AUDIT, age_s=60)
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        rows = {row["repo"]: row for row in payload["streams"]}
        self.assertEqual(rows[REPO]["liveness"], "running")
        self.assertIn("acme/other", rows[REPO]["error"])

    def test_diff_reads_one_repositorys_ring(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("b")])
        payload = self.ok("diff", AUDIT, "--repo", REPO)
        self.assertEqual([row["id"] for row in payload["added"]], ["b"])
        self.assertEqual([row["id"] for row in payload["resolved"]], ["a"])

    def test_an_unknown_or_malformed_repository_is_refused(self):
        self.assertEqual(
            self.refused("show", AUDIT, "--repo", "acme/none")["repos"], [REPO, "acme/other"]
        )
        self.assertIn(
            "is not owner/name", self.refused("show", AUDIT, "--repo", "../acme")["error"]
        )


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root lists a mode-000 directory")
class TestAnUnlistableOwner(StoreTestCase):
    """An owner directory that cannot be listed hides its repositories. The
    per-stream answer is the unreadable one `streams` gives, never "no
    record": a store that could not be read is not one never written."""

    def deny(self, owner):
        path = Path(self.root) / AUDIT / owner
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o700)

    def assert_unreadable(self, payload):
        streams = self.query("streams")[1]
        self.assertIn(f"streams that could not be read: {AUDIT}", streams["error"])
        self.assertIn(f"streams that could not be read: {AUDIT}", payload["error"])
        self.assertIn("unknown, not clean", payload["error"])
        self.assertNotIn("no record", payload["error"])
        self.assertNotIn("no reports", payload["error"])
        self.assertEqual(payload["liveness"], "error")
        row = next(row for row in streams["streams"] if row["audit_id"] == AUDIT)
        self.assertEqual(payload["stream_error"], row["error"])

    def test_the_only_owner_unlisted_is_unreadable(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.deny("acme")
        for command in ("show", "findings", "runs"):
            self.assert_unreadable(self.refused(command, AUDIT))

    def test_a_repository_named_under_it_is_unreadable(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.deny("acme")
        self.assert_unreadable(self.refused("show", AUDIT, "--repo", REPO))

    def test_a_readable_sibling_is_not_the_default_while_one_is_unlisted(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.write_run(AUDIT, "20260826T064100.000000Z", [finding("z")], repo="other/repo")
        self.deny("acme")
        payload = self.refused("findings", AUDIT)
        self.assert_unreadable(payload)
        self.assertEqual(payload["repos"], ["other/repo"])
        # Named, the readable one still answers.
        chosen = self.ok("findings", AUDIT, "--repo", "other/repo")
        self.assertEqual([row["id"] for row in chosen["findings"]], ["z"])


class TestRuns(StoreTestCase):
    def test_it_lists_stamps_without_parsing_the_ring(self):
        first = self.write_run(AUDIT, "20260825T063100.000000Z", [finding("a")], latest=False)
        second = self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        # A ring entry nothing can parse. `runs` still answers, because a stamp
        # listing that reads fourteen documents is the cost this command exists
        # to avoid. Liveness reads `latest.json`, as `streams` does, and opens
        # the newest ring entry only to check it is not a run the file missed;
        # an entry that does not parse is passed over, so the file answers.
        broken = Path(self.root) / AUDIT / REPO / "runs" / "20260827T063100.000000Z.json"
        broken.write_text("{not json", encoding="utf-8")
        payload = self.ok("runs", AUDIT)
        self.assertEqual(payload["runs"], [first, second, broken.name])
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["newest"], broken.name)
        self.assertEqual(payload["liveness"], "completed")

    def test_a_dead_lease_is_not_hidden_by_an_unreadable_envelope(self):
        # `streams` reads the lease before a store error; `runs` must answer
        # the same, or the two subcommands disagree about one stream.
        self.stream_dir(AUDIT)
        (Path(self.root) / AUDIT / REPO / "latest.json").write_text("[]", encoding="utf-8")
        self.write_claim(AUDIT, age_s=report_query.report_status.INFLIGHT_TTL_S + 60)
        row = self.query("streams")[1]["streams"][0]
        self.assertEqual(row["liveness"], "died")
        # The store's error rides along beside the lease's verdict, as it does
        # on the `streams` row, and exits 2 the same way.
        payload = self.refused("runs", AUDIT)
        self.assertEqual(payload["liveness"], "died")
        self.assertIn(row["error"], payload["error"])

    def test_a_temp_file_mid_write_is_not_a_run(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        (Path(self.root) / AUDIT / REPO / "runs" / "tmpabc123.tmp").write_text("{", encoding="utf-8")
        self.assertEqual(self.ok("runs", AUDIT)["count"], 1)

    def test_an_absent_stream_is_refused(self):
        self.assertEqual(self.refused("runs", AUDIT)["streams"], [])

    def test_a_corrupt_sibling_names_itself_as_streams_does(self):
        # `liveness: error` with a null error is a verdict nobody can pass on;
        # `runs` carries the stream's reason exactly as the `streams` row does.
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        (Path(self.root) / AUDIT / "acme/other").mkdir(parents=True)
        (Path(self.root) / AUDIT / "acme/other" / "latest.json").write_text("{", encoding="utf-8")
        payload = self.refused("runs", AUDIT, "--repo", REPO)
        self.assertEqual(payload["liveness"], "error")
        self.assertIn("acme/other", payload["error"])
        self.assertEqual(payload["count"], 1)
        row = self.query("streams")[1]["streams"][0]
        self.assertEqual(row["error"], payload["error"])


class TestRefusalLiveness(StoreTestCase):
    """A per-stream refusal carries the stream's liveness, and its reason
    whenever that liveness is `error`."""

    def test_a_refusal_with_an_error_liveness_carries_the_reason(self):
        # A stream directory holding only a stray mixed-case repository has
        # no repository the reader can answer from; the refusal says that,
        # and its `error` liveness arrives with the stream's reason.
        (Path(self.root) / AUDIT / "Acme" / "Fleet").mkdir(parents=True)
        payload = self.refused("show", AUDIT)
        self.assertIn("no repository directory", payload["error"])
        self.assertEqual(payload["liveness"], "error")
        self.assertTrue(payload["stream_error"])
        row = self.query("streams")[1]["streams"][0]
        self.assertEqual(row["error"], payload["stream_error"])

    def test_a_refusal_without_a_stream_error_carries_none(self):
        self.stream_dir(AUDIT)
        payload = self.refused("show", AUDIT)
        self.assertEqual(payload["liveness"], "never")
        self.assertNotIn("stream_error", payload)


class TestUnusableLeaseTimestamps(StoreTestCase):
    """A lease note's `started_at` is whatever the writer left. One out of
    `datetime`'s range -- milliseconds instead of seconds, `inf`, NaN -- must
    not crash the projection; the note is a claim, so its mtime dates it."""

    BAD = {
        "milliseconds": 1759000000000,
        "infinity": float("inf"),
        "nan": float("nan"),
    }

    def write_bad_claim(self, started_at):
        self.stream_dir(AUDIT)
        # `json.dumps` writes inf and NaN as the bare tokens `json.loads` reads back.
        (Path(self.scratch) / f"inflight_{AUDIT}.json").write_text(
            json.dumps({"audit": AUDIT, "started_at": started_at}), encoding="utf-8"
        )

    def test_every_reader_answers_and_reads_the_note_as_a_fresh_claim(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        for label, started_at in self.BAD.items():
            with self.subTest(label):
                self.write_bad_claim(started_at)
                row = self.ok("streams")["streams"][0]
                self.assertEqual(row["liveness"], "running")
                self.assertEqual(self.ok("runs", AUDIT)["liveness"], "running")
                self.assertEqual(self.ok("show", AUDIT)["run"], "latest.json")

    def test_an_old_note_with_a_bad_timestamp_dies_on_its_mtime(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.write_bad_claim(float("inf"))
        old = time.time() - report_query.report_status.INFLIGHT_TTL_S - 60
        os.utime(Path(self.scratch) / f"inflight_{AUDIT}.json", (old, old))
        self.assertEqual(self.ok("streams")["streams"][0]["liveness"], "died")


class TestChecks(StoreTestCase):
    """The ledger drops its evidence table when the body runs out of room and
    tells the reader to ask the agent for the stored report instead. This is the
    path that sentence promises."""

    CLUSTERS = [
        {
            "name": "prod-us-east",
            "location": "us-east4",
            "project": "acme-prod",
            "checks_run": [
                {
                    "check": "netpol-missing",
                    "command": "kubectl --context prod-us-east get netpol -A -o json",
                },
                {
                    "check": "wildcard-rbac",
                    "command": "kubectl --context prod-us-east get clusterrole -o json",
                },
            ],
        },
        {
            "name": "prod-autopilot",
            "location": "us-central1",
            "project": "acme-prod",
            "checks_run": [
                {
                    "check": "netpol-missing",
                    "command": "kubectl --context prod-autopilot get netpol -A -o json",
                },
            ],
            "checks_not_applicable": [
                {
                    "check": "secure-boot",
                    "reason": "Autopilot owns the node pool, so there is none to read",
                },
            ],
        },
    ]

    def setUp(self):
        super().setUp()
        self.findings = [finding("a"), finding("b", severity="minor")]
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            self.findings,
            document=scope_document(AUDIT, self.findings, self.CLUSTERS),
        )

    def test_every_command_comes_back_in_the_order_the_table_published_them(self):
        payload = self.ok("checks", AUDIT)
        self.assertEqual(payload["scope_entries"], 2)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["matched"], 3)
        self.assertFalse(payload["truncated"])
        self.assertEqual(
            [(row["cluster"], row["check"]) for row in payload["checks"]],
            [
                ("prod-us-east", "netpol-missing"),
                ("prod-us-east", "wildcard-rbac"),
                ("prod-autopilot", "netpol-missing"),
            ],
        )
        self.assertEqual(
            payload["checks"][0]["command"],
            "kubectl --context prod-us-east get netpol -A -o json",
        )

    def test_the_filters_are_exact_and_case_insensitive(self):
        self.assertEqual(self.ok("checks", AUDIT, "--cluster", "PROD-US-EAST")["matched"], 2)
        self.assertEqual(self.ok("checks", AUDIT, "--check", "Netpol-Missing")["matched"], 2)
        both = self.ok("checks", AUDIT, "--cluster", "prod-autopilot", "--check", "netpol-missing")
        self.assertEqual(both["matched"], 1)
        self.assertEqual(both["filters"], {"cluster": "prod-autopilot", "check": "netpol-missing"})
        # A near miss is zero rows and not a substring match.
        self.assertEqual(self.ok("checks", AUDIT, "--cluster", "prod")["matched"], 0)

    def test_the_list_is_bounded_and_says_when_it_bit(self):
        payload = self.ok("checks", AUDIT, "--limit", "2")
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(payload["matched"], 3)
        self.assertEqual(payload["total"], 3)
        self.assertTrue(payload["truncated"])

    def test_the_exclusions_come_back_too(self):
        """The notice counts them, and an excluded check is the one claim that
        can make a partial run read as complete."""
        payload = self.ok("checks", AUDIT)
        self.assertEqual(payload["not_applicable_total"], 1)
        self.assertEqual(payload["not_applicable_returned"], 1)
        self.assertFalse(payload["not_applicable_truncated"])
        self.assertEqual(
            payload["not_applicable"],
            [
                {
                    "cluster": "prod-autopilot",
                    "check": "secure-boot",
                    "reason": "Autopilot owns the node pool, so there is none to read",
                }
            ],
        )
        # The filters narrow the exclusions with the commands, not around them.
        self.assertEqual(
            self.ok("checks", AUDIT, "--cluster", "prod-us-east")["not_applicable_matched"], 0
        )

    def test_a_named_stamp_reads_that_run(self):
        stamp = self.write_run(
            AUDIT,
            "20260825T063100.000000Z",
            self.findings,
            latest=False,
            document=scope_document(AUDIT, self.findings, self.CLUSTERS[:1]),
        )
        payload = self.ok("checks", AUDIT, "--run", stamp)
        self.assertEqual(payload["run"], stamp)
        self.assertEqual(payload["total"], 2)

    def test_a_scope_that_carries_no_commands_is_zero_rows_and_not_an_error(self):
        """`envelope`'s default scope names its clusters and nothing else. A run
        shaped that way has no evidence table to reproduce, which is an answer."""
        self.write_run(OTHER, "20260826T063100.000000Z", [finding("a")])
        payload = self.ok("checks", OTHER)
        self.assertEqual(payload["scope_entries"], 2)
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["checks"], [])
        self.assertEqual(payload["not_applicable_total"], 0)

    def test_a_scope_that_is_not_a_scope_is_refused(self):
        self.write_run(
            OTHER,
            "20260826T063100.000000Z",
            [finding("a")],
            document={"audit": OTHER, "scope": [], "findings": [finding("a")]},
        )
        self.assertIn("document.scope is not an object", self.refused("checks", OTHER)["error"])

    def test_a_clusters_list_that_is_not_a_list_is_refused(self):
        self.write_run(
            OTHER,
            "20260826T063100.000000Z",
            [finding("a")],
            document={"audit": OTHER, "scope": {"clusters": {}}, "findings": []},
        )
        self.assertIn(
            "document.scope.clusters is not a list", self.refused("checks", OTHER)["error"]
        )


class TestBoundedOutput(StoreTestCase):
    """The rule the subcommands exist for: only `finding` returns prose."""

    def test_no_other_subcommand_emits_the_document(self):
        self.write_run(AUDIT, "20260825T063100.000000Z", [finding("a")], latest=False)
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a"), finding("b")])
        for argv in (
            ("streams",),
            ("show", AUDIT),
            ("findings", AUDIT),
            ("checks", AUDIT),
            ("diff", AUDIT),
            ("runs", AUDIT),
        ):
            with self.subTest(command=argv[0]):
                self.assertNotIn(PROSE, json.dumps(self.ok(*argv)))

    def test_every_answer_carries_an_error_key(self):
        """Null on success, a sentence on failure — so a caller never has to
        tell an absent key from a null one."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.assertIn("error", self.ok("show", AUDIT))
        self.assertIn("error", self.refused("show", OTHER))



class TestTheWriterIsWhatIsRead(StoreTestCase):
    """The reader against envelopes the real writer made, not hand-built ones.

    Every other test here builds its envelope by hand, so a key the writer
    renames, or a severity it adds, would break the reader with every suite
    still green.
    """

    def setUp(self):
        super().setUp()
        import audit_report  # noqa: PLC0415 — on the path report_query put there

        self.audit_report = audit_report
        env = patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": self.root})
        env.start()
        self.addCleanup(env.stop)

    def test_the_severity_order_is_the_writers(self):
        self.assertEqual(
            sorted(report_query.SEVERITY_ORDER, key=report_query.SEVERITY_ORDER.get),
            list(self.audit_report.SEVERITIES),
        )

    def test_a_written_run_answers_every_subcommand(self):
        from datetime import datetime, timezone  # noqa: PLC0415

        findings = [finding("a1", severity="minor"), finding("b2", severity="critical")]
        document = scope_document(
            AUDIT,
            findings,
            [{"name": "prod-us-east", "checks_run": [
                {"check": "netpol-missing", "command": "kubectl get networkpolicy -A"}
            ]}],
        )
        written = self.audit_report.report_envelope(
            AUDIT,
            {"status": "OPENED", "partial": False, "coverage_gaps": []},
            document,
            datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc),
            repo=REPO,
            issue_number=7,
            ledger_body="body",
            new_ids=["a1", "b2"],
            resolved_ids=[],
            rendered_ids=["a1", "b2"],
        )
        self.audit_report.write_report(
            AUDIT, written, datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)
        )
        shown = self.ok("show", AUDIT)["envelope"]
        self.assertEqual(shown["status"], "OPENED")
        self.assertEqual(shown["issue_number"], 7)
        self.assertEqual(shown["findings"], 2)
        self.assertEqual(shown["critical"], 1)
        self.assertEqual(
            [row["id"] for row in self.ok("findings", AUDIT)["findings"]], ["b2", "a1"]
        )
        self.assertEqual(self.ok("finding", AUDIT, "b2")["finding"]["impact"], f"{PROSE} impact")
        self.assertEqual(self.ok("checks", AUDIT)["matched"], 1)
        row = self.ok("streams")["streams"][0]
        self.assertEqual(row["status"], "OPENED")
        self.assertEqual(row["liveness"], "completed")
        # A second run that fixed a1 gives `runs` and `diff` a ring to read.
        later = datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc)
        second = self.audit_report.report_envelope(
            AUDIT,
            {"status": "UPDATED", "partial": False, "coverage_gaps": []},
            scope_document(AUDIT, findings[1:], []),
            later,
            repo=REPO,
            issue_number=7,
            ledger_body="body",
            new_ids=[],
            resolved_ids=["a1"],
            rendered_ids=["b2"],
        )
        self.audit_report.write_report(AUDIT, second, later)
        self.assertEqual(self.ok("runs", AUDIT)["count"], 2)
        diff = self.ok("diff", AUDIT)
        self.assertEqual([row["id"] for row in diff["resolved"]], ["a1"])
        self.assertEqual(diff["added_total"], 0)


if __name__ == "__main__":
    unittest.main()
