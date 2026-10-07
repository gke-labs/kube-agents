"""Tests for report_status.py, the read side of the fleet-audit report store."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report  # noqa: E402
import report_status  # noqa: E402

AUDIT = "compliance-audit"
REPO = "acme/fleet"
NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)
SCRATCH_ENV = "FLEET_AUDIT_SCRATCH_DIR"
REPORTS_ENV = "FLEET_AUDIT_REPORTS_DIR"


def fresh_module(module):
    """A second copy of `module`, loaded now, so its import-time reads see the
    environment as it is at the call."""
    spec = importlib.util.spec_from_file_location(f"fresh_{module.__name__}", module.__file__)
    copy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copy)
    return copy


class ReportStatusTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "reports"
        self.scratch = Path(tmp.name) / "scratch"
        self.scratch.mkdir()

    def write_latest(self, audit=AUDIT, repo=REPO, **overrides):
        envelope = {
            "audit_id": audit,
            "repo": repo,
            "finished_at": NOW.isoformat(),
            "status": "UPDATED",
            "issue_number": 42,
            "new_ids": ["a"],
            "resolved_ids": [],
            "current_ids": ["a", "b"],
            "ledger_body": "the rendered body",
            "document": {
                "findings": [{"severity": "critical"}, {"severity": "major"}],
                "scope": {"clusters": [{}, {}, {}], "skipped": [{}]},
            },
        }
        envelope.update(overrides)
        directory = self.root / audit / repo / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(envelope)
        (directory / "20260801T093000.000000Z.json").write_text(text)
        (self.root / audit / repo / "latest.json").write_text(text)

    def write_note(self, audit=AUDIT, age_s=60.0, text=None):
        path = self.scratch / f"inflight_{audit}.json"
        started = NOW.timestamp() - age_s
        path.write_text(text if text is not None else json.dumps({"audit": audit, "started_at": started}))
        return path

    def project(self):
        return report_status.project(str(self.root), NOW, scratch=str(self.scratch))


class TestProjection(ReportStatusTestCase):
    def test_a_completed_stream_projects_counts_and_drops_the_heavy_keys(self):
        self.write_latest(chat="kept")
        stream = self.project()["streams"][AUDIT]
        self.assertEqual(stream["liveness"], "completed")
        latest = stream["repos"][REPO]["latest"]
        self.assertEqual((latest["new"], latest["resolved"], latest["current"]), (1, 0, 2))
        self.assertEqual((latest["findings"], latest["critical"]), (2, 1))
        self.assertEqual((latest["clusters"], latest["skipped"]), (3, 1))
        self.assertEqual(latest["repo"], "acme/fleet")
        self.assertEqual(latest["chat"], "kept")
        for key in ("document", "ledger_body", "new_ids", "current_ids"):
            self.assertNotIn(key, latest)
        self.assertIsNone(latest["prs_opened"])
        self.assertEqual(stream["repos"][REPO]["runs"], ["20260801T093000.000000Z.json"])

    def test_a_withheld_delta_is_carried_and_an_old_envelope_reads_null(self):
        self.write_latest(delta_known=False)
        latest = self.project()["streams"][AUDIT]["repos"][REPO]["latest"]
        self.assertIs(latest["delta_known"], False)
        self.write_latest()
        latest = self.project()["streams"][AUDIT]["repos"][REPO]["latest"]
        self.assertIn("delta_known", latest)
        self.assertIsNone(latest["delta_known"])

    def test_a_malformed_envelope_counts_unknown_not_zero(self):
        self.write_latest(new_ids="x", document=[])
        latest = self.project()["streams"][AUDIT]["repos"][REPO]["latest"]
        self.assertIsNone(latest["new"])
        self.assertIsNone(latest["findings"])
        self.assertIsNone(latest["critical"])

    def test_a_corrupt_stream_costs_only_itself(self):
        self.write_latest()
        (self.root / "drift-audit" / REPO).mkdir(parents=True)
        (self.root / "drift-audit" / REPO / "latest.json").write_text("[]")
        streams = self.project()["streams"]
        self.assertEqual(streams["drift-audit"]["liveness"], "error")
        self.assertIn(f"{REPO}: latest.json: not a JSON object", streams["drift-audit"]["error"])
        self.assertEqual(streams[AUDIT]["liveness"], "completed")

    def test_an_unreadable_repository_does_not_hide_a_dead_run(self):
        self.write_latest()
        (self.root / AUDIT / "acme" / "other").mkdir(parents=True)
        (self.root / AUDIT / "acme" / "other" / "latest.json").write_text("[]")
        self.write_note(age_s=report_status.INFLIGHT_TTL_S + 60)
        stream = self.project()["streams"][AUDIT]
        self.assertEqual(stream["liveness"], "died")
        self.assertIn("acme/other: latest.json: not a JSON object", stream["error"])

    def test_a_mixed_case_directory_is_named_rather_than_listed_as_never_run(self):
        # The writer lower-cases every path and store_path opens nothing else,
        # so a directory spelled otherwise would list as a repository with no
        # runs and no error.
        self.write_latest()
        self.write_latest(repo="Zeta/Fleet")
        stream = self.project()["streams"][AUDIT]
        self.assertEqual(sorted(stream["repos"]), [REPO])
        self.assertIn("Zeta/Fleet: not lower-case", stream["error"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-000 directory")
    def test_one_unreadable_owner_keeps_its_readable_siblings(self):
        # Owner directories created by different uids on one volume: the
        # unreadable owner is named, and the other owner's store still shows.
        self.write_latest()
        locked = self.root / AUDIT / "zeta"
        (locked / "fleet").mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        stream = self.project()["streams"][AUDIT]
        self.assertEqual(sorted(stream["repos"]), [REPO])
        self.assertEqual(stream["repos"][REPO]["latest"]["findings"], 2)
        self.assertEqual(stream["liveness"], "error")
        self.assertIn("zeta/: ", stream["error"])
        self.assertEqual(report_status.scan_repo_dirs(str(self.root), AUDIT)[0], [REPO])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-000 directory")
    def test_an_unreadable_scratch_is_an_error_not_nothing_in_flight(self):
        self.write_latest()
        self.write_note(age_s=60)
        self.scratch.chmod(0)
        self.addCleanup(self.scratch.chmod, 0o700)
        stream = self.project()["streams"][AUDIT]
        self.assertIsNone(stream["started"])
        self.assertEqual(stream["liveness"], "error")
        self.assertIn(str(self.scratch), stream["error"])

    def test_no_store_is_said_rather_than_read_as_an_empty_fleet(self):
        document = self.project()
        self.assertFalse(document["root_exists"])
        self.assertEqual(document["streams"], {})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-000 directory")
    def test_an_unlistable_store_keeps_its_reason(self):
        # Present but unlistable is not absent: the key the exit code hangs on
        # stays false, and the reason travels so a reader can say which.
        self.write_latest()
        self.root.chmod(0)
        self.addCleanup(self.root.chmod, 0o700)
        document = self.project()
        self.assertFalse(document["root_exists"])
        self.assertIn("Permission denied", document["root_error"])

    def test_a_file_where_the_store_should_be_is_unreadable_and_named_once(self):
        # The reader prints `root` beside `root_error`, so the reason must not
        # carry the path a second time.
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_text("not a directory")
        document = self.project()
        self.assertFalse(document["root_exists"])
        self.assertEqual(document["root_error"], "Not a directory")

    def test_an_absent_store_has_no_root_error(self):
        self.assertIsNone(self.project()["root_error"])

    def test_the_temp_file_of_a_write_in_progress_is_not_a_run(self):
        self.write_latest()
        (self.root / AUDIT / REPO / "runs" / "tmpabc.tmp").write_text("{")
        self.assertEqual(len(self.project()["streams"][AUDIT]["repos"][REPO]["runs"]), 1)

    def test_each_repository_is_its_own_entry(self):
        self.write_latest()
        self.write_latest(repo="acme/other", status="CLEAN", new_ids=[])
        repos = self.project()["streams"][AUDIT]["repos"]
        self.assertEqual(sorted(repos), [REPO, "acme/other"])
        self.assertEqual(repos["acme/other"]["latest"]["status"], "CLEAN")
        self.assertEqual(repos[REPO]["latest"]["status"], "UPDATED")

    def test_a_directory_that_cannot_be_a_repository_is_not_one(self):
        self.write_latest()
        (self.root / AUDIT / "stray").mkdir()
        (self.root / AUDIT / "acme" / "bad name").mkdir()
        self.assertEqual(list(self.project()["streams"][AUDIT]["repos"]), [REPO])

    def test_a_repository_argument_cannot_leave_the_store(self):
        for repo in ("../x", "acme/..", "a/b/c", ""):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                report_status.store_path(str(self.root), AUDIT, repo)


class TestLiveness(ReportStatusTestCase):
    def test_the_ttl_is_the_leases(self):
        self.assertEqual(report_status.INFLIGHT_TTL_S, audit_report.INFLIGHT_TTL_SECONDS)

    def test_the_note_path_is_the_one_start_writes(self):
        # `audit_report` reads the environment at import and `scratch_root` at
        # call time; with it unset both fall back to their module defaults.
        with patch.dict(os.environ):
            os.environ.pop(SCRATCH_ENV, None)
            self.assertEqual(
                os.path.join(
                    report_status.scratch_root(),
                    f"{report_status.INFLIGHT_PREFIX}{AUDIT}{report_status.INFLIGHT_SUFFIX}",
                ),
                audit_report.inflight_path_for(AUDIT),
            )

    def test_the_default_roots_are_the_writers(self):
        # The copies are deliberate, so the defaults are compared as each
        # module computes them with the environment empty, not as imported.
        with patch.dict(os.environ):
            for name in (SCRATCH_ENV, REPORTS_ENV):
                os.environ.pop(name, None)
            reader, writer = fresh_module(report_status), fresh_module(audit_report)
        self.assertEqual(reader.SCRATCH_DIR, writer.SCRATCH_DIR)
        self.assertEqual(reader.REPORTS_DIR, writer.REPORTS_DIR)

    def test_the_five_states(self):
        ttl = report_status.INFLIGHT_TTL_S
        cases = [
            ("never", None, False),
            ("completed", None, True),
            ("running", 60.0, True),
            ("running", ttl - 1, False),
            ("died", ttl, True),
            ("died", ttl * 3, False),
        ]
        for expected, age, finished in cases:
            with self.subTest(expected=expected, age=age, finished=finished):
                self.setUp()
                if finished:
                    self.write_latest()
                if age is not None:
                    self.write_note(age_s=age)
                if not finished and age is None:
                    (self.root / AUDIT).mkdir(parents=True)
                stream = self.project()["streams"][AUDIT]
                self.assertEqual(stream["liveness"], expected)

    def test_a_ring_without_latest_is_the_last_run_not_never(self):
        """A `finish` that failed before storing itself leaves the ring and
        no `latest.json`; the stream ran, and its newest entry is flagged."""
        self.write_latest()
        (self.root / AUDIT / REPO / "latest.json").unlink()
        stream = self.project()["streams"][AUDIT]
        self.assertEqual(stream["liveness"], "completed")
        entry = stream["repos"][REPO]
        self.assertTrue(entry["latest_missing"])
        self.assertEqual(entry["latest"]["finished_at"], NOW.isoformat())

    def test_a_ring_entry_newer_than_latest_is_the_last_run(self):
        """A held-open run whose `latest.json` write failed after its ring
        entry landed: the ring holds the newer run, and it is flagged."""
        self.write_latest()
        later = NOW + timedelta(hours=1)
        newer = {"audit_id": AUDIT, "repo": REPO, "finished_at": later.isoformat(), "status": "CLEAN"}
        stamp = later.astimezone(timezone.utc).strftime(audit_report.REPORT_STAMP_FORMAT)
        (self.root / AUDIT / REPO / "runs" / f"{stamp}.json").write_text(json.dumps(newer))
        entry = self.project()["streams"][AUDIT]["repos"][REPO]
        self.assertTrue(entry["latest_missing"])
        self.assertEqual(entry["latest"]["finished_at"], later.isoformat())
        self.assertEqual(entry["latest"]["status"], "CLEAN")

    def test_a_latest_that_is_the_newest_run_is_not_flagged(self):
        self.write_latest()
        entry = self.project()["streams"][AUDIT]["repos"][REPO]
        self.assertFalse(entry["latest_missing"])
        self.assertEqual(entry["latest"]["status"], "UPDATED")

    def test_the_run_stamp_is_the_one_finish_writes(self):
        self.assertEqual(report_status.RUN_STAMP_FORMAT, audit_report.REPORT_STAMP_FORMAT)

    def test_a_corrupt_ring_entry_behind_a_missing_latest_is_named(self):
        self.write_latest()
        (self.root / AUDIT / REPO / "latest.json").unlink()
        (entry_file,) = (self.root / AUDIT / REPO / "runs").iterdir()
        entry_file.write_text("[]")
        entry = self.project()["streams"][AUDIT]["repos"][REPO]
        self.assertTrue(entry["error"].startswith(f"runs/{entry_file.name}: "), entry["error"])
        self.assertTrue(entry["latest_missing"])

    def test_a_first_run_in_flight_is_running_before_the_store_exists(self):
        self.write_note(audit="cost-audit", age_s=30.0)
        stream = self.project()["streams"]["cost-audit"]
        self.assertEqual(stream["liveness"], "running")
        self.assertEqual(stream["started"]["age_s"], 30.0)
        self.assertEqual(stream["repos"], {})

    def test_a_note_whose_name_is_not_a_stream_id_is_no_stream(self):
        """The scratch directory is shared with the worker, and the id is joined
        onto the store root: `inflight_...json` would name `..`, and the root's
        parent would be listed as that stream's repositories."""
        outside = self.root.parent / "acme" / "fleet"
        outside.mkdir(parents=True, exist_ok=True)
        for audit in ("..", ".", "a b"):
            self.write_note(audit=audit, age_s=30.0)
        self.write_note(audit="cost-audit", age_s=30.0)
        self.assertEqual(report_status.in_flight_ids(str(self.scratch)), ["cost-audit"])
        self.assertEqual(sorted(self.project()["streams"]), ["cost-audit"])

    def test_an_unparseable_note_counts_from_its_mtime(self):
        # A `start` that created the note and has not written it yet: a claim.
        path = self.write_note(text="")
        os.utime(path, (NOW.timestamp() - 10, NOW.timestamp() - 10))
        self.assertEqual(self.project()["streams"][AUDIT]["liveness"], "running")

    def test_the_lease_and_this_reader_agree_on_a_non_numeric_started_at(self):
        # `true` is an int to Python; were it 1.0 on one side and the mtime on
        # the other, `start` would take over a stream the readers call running.
        for value in (True, False, "soon", None):
            with self.subTest(started_at=value):
                path = self.write_note(text=json.dumps({"started_at": value}))
                self.assertEqual(
                    report_status.in_flight_since(str(self.scratch), AUDIT),
                    audit_report._in_flight_since(Path(path)),
                )

    def test_an_out_of_range_started_at_counts_from_the_mtime(self):
        # A millisecond epoch, JSON's inf and NaN are numbers `datetime`
        # refuses; the note is a claim read from its mtime, and the projection
        # of every other stream survives it.
        mtime = NOW.timestamp() - 10
        for value in (NOW.timestamp() * 1000, float("inf"), float("nan")):
            with self.subTest(started_at=value):
                path = self.write_note(text=json.dumps({"started_at": value}))
                os.utime(path, (mtime, mtime))
                self.assertEqual(report_status.in_flight_since(str(self.scratch), AUDIT), mtime)
                self.assertEqual(
                    report_status.in_flight_since(str(self.scratch), AUDIT),
                    audit_report._in_flight_since(Path(path)),
                )
                self.assertEqual(self.project()["streams"][AUDIT]["liveness"], "running")

    def test_the_lock_file_is_not_a_stream(self):
        (self.scratch / f"inflight_{AUDIT}.json.lock").write_text("")
        (self.scratch / "inflight_.json").write_text("{}")
        self.assertEqual(self.project()["streams"], {})


class TestCli(ReportStatusTestCase):
    def test_main_prints_one_json_document(self):
        self.write_latest()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = report_status.main(["--root", str(self.root), "--scratch", str(self.scratch)])
        self.assertEqual(rc, 0)
        self.assertIn(AUDIT, json.loads(out.getvalue())["streams"])

    def test_it_runs_from_stdin_with_no_sibling_modules(self):
        """The view streams this file into a pod on stdin, where there is no
        `__file__` and no sibling to import."""
        self.write_latest()
        source = Path(report_status.__file__).read_text()
        result = subprocess.run(
            [sys.executable, "-", "--root", str(self.root), "--scratch", str(self.scratch)],
            input=source,
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["streams"][AUDIT]["liveness"], "completed")


if __name__ == "__main__":
    unittest.main()
