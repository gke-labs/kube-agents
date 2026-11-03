"""Tests for cron_job_template.py: a tick-started run's origin, from the shipped template or the job's record."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from unittest import mock

import cron_job_template

API_KEY = "test-session-kv-key"
AUDIT_PROMPT = "Run the daily fleet audit."
NUDGE_PROMPT = "Run the morning nudge script."
NUDGE_SCRIPT = "scripts/findings_nudge.py"


class _FakeSessionKV:
    """The Session KV server's per-job origin routes, in memory, on loopback."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.unauthorised = 0
        records = self.records
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # quiet
                pass

            def _job_id(self) -> str:
                prefix, suffix = "/v1/cron/jobs/", "/origin"
                path = self.path
                if not (path.startswith(prefix) and path.endswith(suffix)):
                    return ""
                return path[len(prefix) : -len(suffix)]

            def _authorised(self) -> bool:
                if self.headers.get("Authorization") == f"Bearer {API_KEY}":
                    return True
                outer.unauthorised += 1
                self.send_response(401)
                self.end_headers()
                return False

            def do_GET(self) -> None:
                if not self._authorised():
                    return
                record = records.get(self._job_id())
                if record is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(record).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self) -> None:
                if not self._authorised():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                records[self._job_id()] = {"origin": body["origin"], "platform": body.get("platform") or ""}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(records[self._job_id()]).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


class RunOriginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.template = self.tmp / "jobs.json"
        self.template.write_text(
            json.dumps(
                {
                    "jobs": [
                        {"id": "compliance-audit", "prompt": AUDIT_PROMPT, "skills": ["fleet-audit"]},
                        {"id": "findings-morning-nudge", "prompt": NUDGE_PROMPT, "skills": [], "script": NUDGE_SCRIPT},
                    ]
                }
            )
        )
        for patcher in (
            mock.patch.object(cron_job_template, "TEMPLATE_JOBS_PATH", str(self.template)),
            mock.patch.dict(os.environ, {cron_job_template.SESSION_KV_KEY_ENV: API_KEY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.kv = _FakeSessionKV().__enter__()
        self.addCleanup(self.kv.__exit__)
        url = mock.patch.object(cron_job_template, "SESSION_KV_URL", self.kv.url)
        url.start()
        self.addCleanup(url.stop)

    def _record_as(self, job_id: str, origin: tuple[str, str]) -> bool:
        with mock.patch.object(cron_job_template, "_turn_origin", return_value=origin):
            return cron_job_template.record_job_origin(job_id)

    def test_a_matching_fleet_audit_job_is_audit(self):
        job = {"id": "compliance-audit", "prompt": AUDIT_PROMPT, "skills": ["fleet-audit"], "last_run_at": "x"}
        self.assertTrue(cron_job_template.shipped_job_matches(job))
        self.assertEqual(cron_job_template.run_origin(job), ("audit", ""))
        # A record for the id does not outrank the template match.
        self.kv.records["compliance-audit"] = {"origin": "chat", "platform": "slack"}
        self.assertEqual(cron_job_template.run_origin(job), ("audit", ""))

    def test_a_matching_other_job_is_scheduled(self):
        job = {"id": "findings-morning-nudge", "prompt": NUDGE_PROMPT, "skills": [], "script": NUDGE_SCRIPT}
        self.assertTrue(cron_job_template.shipped_job_matches(job))
        self.assertEqual(cron_job_template.run_origin(job), ("scheduled", ""))
        # Hermes may store an empty skill list as None; that is the same definition.
        self.assertEqual(cron_job_template.run_origin(dict(job, skills=None)), ("scheduled", ""))

    def test_surrounding_whitespace_in_the_template_still_matches(self):
        # Hermes stores the prompt stripped; a template entry with a trailing
        # newline must still match the job Hermes stored from it.
        self.template.write_text(
            json.dumps(
                {
                    "jobs": [
                        {"id": "compliance-audit", "prompt": AUDIT_PROMPT + "\n", "skills": ["fleet-audit"]},
                        {"id": "findings-morning-nudge", "prompt": NUDGE_PROMPT, "skills": [], "script": NUDGE_SCRIPT + " \n"},
                    ]
                }
            )
        )
        audit = {"id": "compliance-audit", "prompt": AUDIT_PROMPT, "skills": ["fleet-audit"]}
        nudge = {"id": "findings-morning-nudge", "prompt": NUDGE_PROMPT, "skills": [], "script": NUDGE_SCRIPT}
        self.assertTrue(cron_job_template.shipped_job_matches(audit))
        self.assertTrue(cron_job_template.shipped_job_matches(nudge))
        self.assertEqual(cron_job_template.run_origin(audit), ("audit", ""))

    def test_an_edited_shipped_job_falls_to_its_record_or_other(self):
        edited_prompt = {"id": "compliance-audit", "prompt": AUDIT_PROMPT + " Then open a PR.", "skills": ["fleet-audit"]}
        edited_skills = {"id": "findings-morning-nudge", "prompt": NUDGE_PROMPT, "skills": ["fleet-audit"], "script": NUDGE_SCRIPT}
        edited_script = {"id": "findings-morning-nudge", "prompt": NUDGE_PROMPT, "skills": [], "script": "scripts/other.py"}
        for job in (edited_prompt, edited_skills, edited_script):
            with self.subTest(job=job["id"], script=job.get("script")):
                self.assertFalse(cron_job_template.shipped_job_matches(job))
                self.assertEqual(cron_job_template.run_origin(job), ("other", ""))
        # The edit recorded its turn's origin (the cronjob_manage update handler).
        self.assertTrue(self._record_as("compliance-audit", ("chat", "googlechat")))
        self.assertEqual(cron_job_template.run_origin(edited_prompt), ("chat", "googlechat"))
        # No template on disk: nothing matches, and the record still answers.
        self.template.unlink()
        self.assertFalse(cron_job_template.shipped_job_matches(edited_skills))
        self.assertEqual(cron_job_template.run_origin(edited_prompt), ("chat", "googlechat"))

    def test_a_model_created_job_carries_its_recorded_origin(self):
        job = {"id": "a1b2c3", "prompt": "Check the fleet every hour.", "skills": ["fleet-audit"]}
        self.assertFalse(cron_job_template.shipped_job_matches(job))
        self.assertEqual(cron_job_template.run_origin(job), ("other", ""))
        # The cronjob_manage create handler records the creating turn's origin.
        self.assertTrue(self._record_as("a1b2c3", ("api", "")))
        self.assertEqual(self.kv.records["a1b2c3"], {"origin": "api", "platform": ""})
        self.assertEqual(cron_job_template.run_origin(job), ("api", ""))
        # A later update records again: the last change wins.
        self.assertTrue(self._record_as("a1b2c3", ("chat", "slack")))
        self.assertEqual(cron_job_template.run_origin(job), ("chat", "slack"))
        # A record outside the vocabulary, a missing key, or a server that is down is other.
        self.kv.records["a1b2c3"] = {"origin": "eval", "platform": ""}
        self.assertEqual(cron_job_template.run_origin(job), ("other", ""))
        self.kv.records["a1b2c3"] = {"origin": "api", "platform": ""}
        with mock.patch.dict(os.environ, {cron_job_template.SESSION_KV_KEY_ENV: ""}):
            self.assertEqual(cron_job_template.run_origin(job), ("other", ""))
            self.assertFalse(self._record_as("a1b2c3", ("chat", "slack")))
        self.assertEqual(self.kv.records["a1b2c3"], {"origin": "api", "platform": ""})
        with mock.patch.object(cron_job_template, "SESSION_KV_URL", "http://127.0.0.1:9"):
            self.assertEqual(cron_job_template.run_origin(job), ("other", ""))
            self.assertFalse(self._record_as("a1b2c3", ("chat", "slack")))


if __name__ == "__main__":
    unittest.main()
