"""The broker store (#5619): report_status's HTTP client over /v1/audit-reports,
the switch that selects it, the seed from a local file store, and the file
store's write semantics through it (audit_report.write_report and friends).

FakeBroker is a local http.server that answers as the broker relays the store
listener: a stream list, latest (a byte copy of a ring entry or 409), the ring
newest first, a prune to REPORT_HISTORY, 404 for what is not there. It records
every request so the order of writes can be asserted.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report  # noqa: E402
import report_status  # noqa: E402

AUDIT = "compliance-audit"
REPO = "acme/fleet"
KEY = "acme%2Ffleet"
NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)
BROKER_TOKEN = "broker-token-never-logged"
PREFIX = "/v1/audit-reports"


def _jwt(claims: dict) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


REPORT_CREDENTIAL = _jwt({"nats": {"profile": "fleet-audit"}})


def stamp_name(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(audit_report.REPORT_STAMP_FORMAT) + ".json"


class FakeBroker:
    """The broker's /v1/audit-reports, in memory."""

    def __init__(self, history: int = audit_report.REPORT_HISTORY):
        self.history = history
        self.streams: dict[tuple[str, str], dict] = {}
        self.requests: list[dict] = []
        # (method, key) -> status the broker answers instead, for every request.
        self.refuse: dict[tuple[str, str], int] = {}
        # A status every request is answered with, when set.
        self.down: int | None = None
        # Answer every request 302 to this base URL, when set.
        self.redirect_to: str | None = None
        # False: a broker from before run-start records (#6812), which
        # answers their route 404 `not_found` as for any route it lacks.
        self.run_starts = True
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                broker.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "task": self.headers.get(report_status.TASK_CREDENTIAL_HEADER),
                    "body": body,
                })
                if broker.redirect_to:
                    self.send_response(302)
                    # A fixed target: nothing from the request goes into a header.
                    self.send_header("Location", broker.redirect_to + PREFIX)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, payload = broker.answer(self.command, self.path, body)
                self.send_response(status)
                data = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PUT = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def stream(self, audit: str, key: str) -> dict:
        return self.streams.setdefault((audit, key), {"latest": None, "runs": {}})

    def answer(self, method: str, path: str, body: bytes):
        if self.down is not None:
            return self.down, {"code": "AUDIT_REPORT_STORE_UNAVAILABLE"}
        if path == PREFIX:
            rows = [
                {"audit_id": audit, "key": key,
                 "newest_run": max(s["runs"]) if s["runs"] else None, "latest_finished_at": None,
                 **({"last_run_start": report_status.newest_run_start(
                     [json.loads(b) for b in s["starts"].values()])} if s.get("starts") else {})}
                for (audit, key), s in sorted(self.streams.items()) if s["runs"] or s["latest"]
            ]
            return 200, rows
        parts = path[len(PREFIX) + 1:].split("/")
        if len(parts) < 3:
            return 404, {"status": "not_found"}
        audit, key, *rest = parts
        refused = self.refuse.get((method, key))
        if refused:
            return refused, {"status": "blocked", "rule": "audit-report.repository-key"}
        store = self.streams.get((audit, key))
        if rest == ["latest"]:
            if method == "GET":
                return (200, store["latest"]) if store and store["latest"] is not None else (404, {"error": "not found"})
            if method == "PUT":
                if not store or body not in store["runs"].values():
                    return 409, {"error": "latest must be a copy of a run in the ring"}
                store["latest"] = body
                return 204, None
            if method == "DELETE":
                if store:
                    store["latest"] = None
                return 204, None
        if rest == ["runs"] and method == "GET":
            return 200, sorted((store or {"runs": {}})["runs"], reverse=True)
        if len(rest) == 2 and rest[0] == "runs" and rest[1].startswith(report_status.RUN_START_PREFIX):
            if not self.run_starts:
                return 404, {"status": "not_found"}
            run_id = rest[1][len(report_status.RUN_START_PREFIX):]
            if method == "GET":
                data = ((store or {}).get("starts") or {}).get(run_id)
                return (200, data) if data is not None else (404, {"error": "not found"})
            if method == "PUT":
                # As the listener: a record never creates a store.
                if store is None:
                    return 404, {"error": "not found"}
                store.setdefault("starts", {})[run_id] = body
                return 204, None
        if len(rest) == 2 and rest[0] == "runs":
            if method == "GET":
                data = (store or {"runs": {}})["runs"].get(rest[1])
                return (200, data) if data is not None else (404, {"error": "not found"})
            if method == "PUT":
                try:
                    if not isinstance(json.loads(body), dict):
                        raise ValueError
                except ValueError:
                    return 400, {"error": "the body is not a JSON object"}
                store = self.stream(audit, key)
                store["runs"][rest[1]] = body
                for stale in sorted(store["runs"])[:-self.history]:
                    del store["runs"][stale]
                return 204, None
        return 404, {"status": "not_found"}

    def calls(self, method: str | None = None) -> list[tuple[str, str]]:
        return [(r["method"], r["path"]) for r in self.requests if method in (None, r["method"])]


class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.local = self.tmp / "reports"
        self.token_file = self.tmp / "broker-token"
        self.token_file.write_text(BROKER_TOKEN)
        self.broker = FakeBroker()
        self.addCleanup(self.broker.close)
        self.env = {
            report_status.REPORT_STORE_ENV: report_status.REPORT_STORE_BROKER,
            report_status.BROKER_URL_ENV: self.broker.url,
            report_status.BROKER_TOKEN_FILE_ENV: str(self.token_file),
            report_status.TASK_TOKEN_FILE_ENV: "",
            "FLEET_AUDIT_REPORTS_DIR": str(self.local),
            "CREDENTIAL_PROXY_URL": self.broker.url,
        }
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        audit_report._BROKER_CLIENTS.clear()
        audit_report._REPORT_STORE_LOGGED.clear()
        self.addCleanup(audit_report._BROKER_CLIENTS.clear)
        self.addCleanup(audit_report._REPORT_STORE_LOGGED.clear)
        self.logged: list[str] = []

    def store(self, local_root: str | None = None) -> report_status.BrokerReportStore:
        return report_status.BrokerReportStore(self.broker.url, local_root, log=self.logged.append)

    def envelope(self, n: int = 0, **extra) -> bytes:
        body = {"audit_id": AUDIT, "repo": REPO, "finished_at": (NOW + timedelta(minutes=n)).isoformat(),
                "issue_number": 42, "status": "UPDATED", "ledger_body": "body", "current_ids": [], **extra}
        return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()

    def put_remote(self, n: int, latest: bool = True) -> bytes:
        data = self.envelope(n)
        stream = self.broker.stream(AUDIT, KEY)
        stream["runs"][stamp_name(NOW + timedelta(minutes=n))[: -len(".json")]] = data
        if latest:
            stream["latest"] = data
        return data


class TestTheHttpStore(BrokerTestCase):
    def test_every_operation(self):
        store = self.store()
        name = stamp_name(NOW)
        data = self.envelope()
        self.assertIsNone(store.read_latest(AUDIT, REPO))
        self.assertEqual([], store.list_runs(AUDIT, REPO))
        store.write_run(AUDIT, REPO, name, data)
        store.write_latest(AUDIT, REPO, data)
        self.assertEqual(data, store.read_latest(AUDIT, REPO))
        self.assertEqual([name], store.list_runs(AUDIT, REPO))
        self.assertEqual(data, store.read_run(AUDIT, REPO, name))
        self.assertIsNone(store.read_run(AUDIT, REPO, stamp_name(NOW + timedelta(days=1))))
        self.assertEqual([AUDIT], store.stream_ids())
        self.assertEqual([REPO], store.repo_dirs(AUDIT))
        store.delete_latest(AUDIT, REPO)
        store.delete_latest(AUDIT, REPO)
        self.assertIsNone(store.read_latest(AUDIT, REPO))
        self.assertEqual(
            [
                ("PUT", f"{PREFIX}/{AUDIT}/{KEY}/runs/{name[:-5]}"),
                ("PUT", f"{PREFIX}/{AUDIT}/{KEY}/latest"),
            ],
            self.broker.calls("PUT"),
        )

    def test_the_ring_is_oldest_first_and_pruned_by_the_server(self):
        store = self.store()
        names = [stamp_name(NOW + timedelta(minutes=n)) for n in range(audit_report.REPORT_HISTORY + 2)]
        for name in reversed(names):
            store.write_run(AUDIT, REPO, name, self.envelope())
        self.assertEqual(names[2:], store.list_runs(AUDIT, REPO))

    def test_a_listing_spells_repositories_as_the_scanner_does(self):
        store = self.store()
        for repo in ("gitlab.com/acme/platform/infra", "my.org/repo", "GHE.example.com/Acme/Fleet"):
            store.write_run(AUDIT, repo, stamp_name(NOW), self.envelope())
        self.assertEqual(
            ["ghe.example.com/acme/fleet", "gitlab.com/acme/platform/infra", "my.org/repo"],
            store.repo_dirs(AUDIT),
        )

    def test_the_bearer_is_read_fresh_on_every_call(self):
        store = self.store()
        store.read_latest(AUDIT, REPO)
        self.token_file.write_text("rotated-token")
        store.read_latest(AUDIT, REPO)
        self.assertEqual(
            [f"Bearer {BROKER_TOKEN}", "Bearer rotated-token"],
            [r["authorization"] for r in self.broker.requests],
        )

    def test_the_task_credential_goes_only_when_present(self):
        store = self.store()
        store.read_latest(AUDIT, REPO)
        task = self.tmp / "task-token"
        with patch.dict(os.environ, {report_status.TASK_TOKEN_FILE_ENV: str(task)}):
            store.read_latest(AUDIT, REPO)  # the file is missing: no task
            task.write_text(REPORT_CREDENTIAL)
            store.read_latest(AUDIT, REPO)
        self.assertEqual([None, None, REPORT_CREDENTIAL], [r["task"] for r in self.broker.requests])

    def test_a_report_credential_never_leaves_for_another_route(self):
        task = self.tmp / "task-token"
        task.write_text(REPORT_CREDENTIAL)
        targeted = _jwt({"nats": {"target": {"cluster": "c"}}})
        with patch.dict(os.environ, {report_status.TASK_TOKEN_FILE_ENV: str(task)}):
            self.assertEqual({}, report_status.task_credential_headers("/v1/exec"))
            self.assertEqual({}, report_status.task_credential_headers("/v1/audit-reportsx"))
            self.assertIn(report_status.TASK_CREDENTIAL_HEADER, report_status.task_credential_headers(f"{PREFIX}/a/b/latest"))
            task.write_text(targeted)
            self.assertIn(report_status.TASK_CREDENTIAL_HEADER, report_status.task_credential_headers("/v1/exec"))
            task.write_text("not a jwt!")
            with self.assertRaises(report_status.ReportStoreUnavailable):
                report_status.task_credential_headers(f"{PREFIX}/a/b/latest")

    def test_the_error_mapping(self):
        store = self.store()
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.broker.down = status
                with self.assertRaises(report_status.ReportStoreUnavailable) as caught:
                    store.read_latest(AUDIT, REPO)
                self.assertEqual(status, caught.exception.status)
                self.assertIsInstance(caught.exception, OSError)
        self.broker.down = None
        for status in (400, 403, 409, 413, 500):
            with self.subTest(status=status):
                self.broker.refuse[("PUT", KEY)] = status
                with self.assertRaises(report_status.ReportStoreError) as caught:
                    store.write_run(AUDIT, REPO, stamp_name(NOW), self.envelope())
                self.assertNotIsInstance(caught.exception, report_status.ReportStoreUnavailable)
                self.assertEqual(status, caught.exception.status)
        self.broker.refuse[("PUT", KEY)] = 403
        with self.assertRaises(report_status.ReportStoreError) as caught:
            store.write_latest(AUDIT, REPO, b'{"not": "in the ring"}')
        self.assertEqual(403, caught.exception.status)
        self.assertIn("audit-report.repository-key", str(caught.exception))

    def test_a_latest_that_is_no_copy_is_refused(self):
        store = self.store()
        store.write_run(AUDIT, REPO, stamp_name(NOW), self.envelope())
        with self.assertRaises(report_status.ReportStoreError) as caught:
            store.write_latest(AUDIT, REPO, self.envelope(1))
        self.assertEqual(409, caught.exception.status)

    def test_no_answer_and_no_url_are_unavailable(self):
        self.broker.close()
        with self.assertRaises(report_status.ReportStoreUnavailable):
            self.store().read_latest(AUDIT, REPO)
        with self.assertRaises(report_status.ReportStoreUnavailable) as caught:
            report_status.BrokerReportStore("").read_latest(AUDIT, REPO)
        self.assertIn(report_status.BROKER_URL_ENV, str(caught.exception))

    def test_a_redirect_is_never_followed(self):
        """A 3xx is a store that can't be used now, and the host it names
        sees nothing: urllib would re-send the bearer across a redirect."""
        elsewhere = FakeBroker()
        self.addCleanup(elsewhere.close)
        self.broker.redirect_to = elsewhere.url
        store = self.store()
        for call in (lambda: store.read_latest(AUDIT, REPO),
                     lambda: store.write_run(AUDIT, REPO, stamp_name(NOW), self.envelope())):
            with self.assertRaises(report_status.ReportStoreUnavailable) as caught:
                call()
            self.assertEqual(302, caught.exception.status)
        self.assertEqual(2, len(self.broker.requests))
        self.assertEqual(f"Bearer {BROKER_TOKEN}", self.broker.requests[0]["authorization"])
        self.assertEqual([], elsewhere.requests)

    def test_no_proxy_from_the_environment_sees_a_request(self):
        proxy = FakeBroker()
        self.addCleanup(proxy.close)
        proxied = {name: proxy.url for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY")}
        # urlopen's module-wide opener reads the proxy environment once, when
        # it is first built; dropping it makes a regression to urlopen show.
        with patch.dict(os.environ, {**proxied, "NO_PROXY": "", "no_proxy": ""}), \
                patch.object(urllib.request, "_opener", None):
            store = self.store()
            store.write_run(AUDIT, REPO, stamp_name(NOW), self.envelope())
            self.assertEqual([stamp_name(NOW)], store.list_runs(AUDIT, REPO))
        self.assertEqual([], proxy.requests)
        self.assertEqual(2, len(self.broker.requests))

    def test_only_an_http_url_is_used(self):
        for url in ("file:///etc/passwd", "ftp://127.0.0.1/x"):
            with self.subTest(url=url), self.assertRaises(report_status.ReportStoreUnavailable):
                report_status.BrokerReportStore(url).read_latest(AUDIT, REPO)

    def test_only_the_listeners_not_found_is_a_missing_file(self):
        """The listener's `{"error": "not found"}`, relayed whole, is a missing
        file. The broker's own codeless `not_found` (a route it does not serve,
        as a broker from before the routes answers every one) and the
        listener's `no such route` are a store that can't be used now."""
        store = self.store()
        self.assertIsNone(store.read_latest(AUDIT, REPO))
        with self.assertRaises(FileNotFoundError):
            store._request("GET", f"{PREFIX}/{AUDIT}/{KEY}/latest")
        for body in ({"status": "not_found"}, {"error": "no such route"}, None, b"not json"):
            with self.subTest(body=body):
                self.broker.answer = lambda method, path, _body, body=body: (404, body if body is not None else b"")
                with self.assertRaises(report_status.ReportStoreUnavailable) as caught:
                    store.read_latest(AUDIT, REPO)
                self.assertEqual(404, caught.exception.status)
                with self.assertRaises(report_status.ReportStoreUnavailable):
                    store.read_run(AUDIT, REPO, stamp_name(NOW))
                with self.assertRaises(report_status.ReportStoreUnavailable):
                    store.delete_latest(AUDIT, REPO)

    def test_a_broker_without_the_routes_is_no_first_run(self):
        """During a rollout, a broker that predates the routes answers 404
        `not_found` to all of them: that reads as a lost memory, never as a
        store no run has written."""
        self.broker.answer = lambda method, path, body: (404, {"status": "not_found"})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(audit_report.store_never_published(audit_report.report_store_dir(AUDIT, REPO)))
            self.assertIsNone(audit_report.newest_stored_record(AUDIT, REPO))

    def test_a_name_outside_the_store_is_refused_before_any_request(self):
        store = self.store()
        for audit, repo, name in (("..", REPO, None), (AUDIT, "acme/../x", None), (AUDIT, REPO, "../../etc.json")):
            with self.subTest(audit=audit, repo=repo, name=name):
                with self.assertRaises(ValueError):
                    if name:
                        store.read_run(audit, repo, name)
                    else:
                        store.read_latest(audit, repo)
        self.assertEqual([], self.broker.requests)


class TestRunStartRecords(BrokerTestCase):
    """The run-start record (#6812) through the broker: the run route, a reserved name."""

    def setUp(self):
        super().setUp()
        audit_report._RUN_START_LOGGED.clear()
        self.addCleanup(audit_report._RUN_START_LOGGED.clear)

    def record(self, run_id="task-1", minutes=0, **done):
        return report_status.run_start_record(run_id, (NOW + timedelta(minutes=minutes)).isoformat(), True, **done)

    def test_a_record_rides_the_run_route_and_stays_out_of_the_ring(self):
        self.put_remote(0)
        store = self.store()
        report_status.write_run_start(store, AUDIT, REPO, self.record())
        self.assertEqual([("PUT", f"{PREFIX}/{AUDIT}/{KEY}/runs/started-task-1")], self.broker.calls("PUT"))
        self.assertEqual([stamp_name(NOW)], store.list_runs(AUDIT, REPO))
        self.assertEqual(self.record(), report_status.read_run_start(store, AUDIT, REPO, "task-1"))
        self.assertIsNone(report_status.read_run_start(store, AUDIT, REPO, "task-2"))
        self.assertEqual("task-1", report_status.unfinished_run_of(self.store(), AUDIT, REPO)["run_id"])
        report_status.write_run_start(
            store, AUDIT, REPO, self.record(done_at=(NOW + timedelta(minutes=5)).isoformat(), outcome="UPDATED")
        )
        self.assertIsNone(report_status.unfinished_run_of(self.store(), AUDIT, REPO))

    def test_a_run_id_that_is_no_label_is_refused_before_any_request(self):
        self.put_remote(0)
        self.broker.requests.clear()
        for bad in ("Task-1", "../latest", "", "a" * 64):
            with self.subTest(run_id=bad), self.assertRaises(ValueError):
                report_status.read_run_start(self.store(), AUDIT, REPO, bad)
        self.assertEqual([], self.broker.requests)

    def test_an_older_broker_refusing_the_name_never_stops_start(self):
        self.put_remote(0)
        self.broker.run_starts = False
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            audit_report.record_run_start(AUDIT, REPO, "task-1", True, NOW)
            audit_report.record_run_start(AUDIT, REPO, "task-2", True, NOW)
            marked = audit_report.mark_run_done(AUDIT, "task-1", [REPO], "UPDATED", NOW)
        self.assertFalse(marked)
        self.assertEqual(1, err.getvalue().count("does not take run-start records yet"), err.getvalue())
        self.assertNotIn("not marked done", err.getvalue())
        # Older readers: the stream list carries no record, and nothing is shown.
        self.assertIsNone(report_status.unfinished_run_of(self.store(), AUDIT, REPO))

    def test_a_first_run_through_the_broker_is_untracked_and_quiet(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            audit_report.record_run_start(AUDIT, REPO, "task-1", True, NOW)
        self.assertNotIn("run-start", err.getvalue())
        self.assertEqual({}, self.broker.streams.get((AUDIT, KEY), {}).get("starts") or {})

    def test_finish_marks_its_own_run_by_id_through_the_broker(self):
        self.put_remote(0)
        with contextlib.redirect_stderr(io.StringIO()):
            audit_report.record_run_start(AUDIT, REPO, "task-old", True, NOW)
            audit_report.record_run_start(AUDIT, REPO, "task-new", True, NOW + timedelta(minutes=30))
            self.assertTrue(audit_report.mark_run_done(AUDIT, "task-old", [REPO], "UPDATED", NOW))
            # A run with no record of its own writes no done mark.
            self.assertFalse(audit_report.mark_run_done(AUDIT, "task-none", [REPO], "UPDATED", NOW))
        starts = self.broker.stream(AUDIT, KEY)["starts"]
        self.assertEqual({"task-old", "task-new"}, set(starts))
        self.assertIn("done_at", json.loads(starts["task-old"]))
        self.assertNotIn("done_at", json.loads(starts["task-new"]))
        self.assertEqual("task-new", report_status.unfinished_run_of(self.store(), AUDIT, REPO)["run_id"])


class TestSelection(BrokerTestCase):
    def test_broker_only_with_the_switch(self):
        store, line = report_status.select_report_store()
        self.assertIsInstance(store, report_status.BrokerReportStore)
        self.assertEqual(f"report store: broker {self.broker.url} (FLEET_AUDIT_REPORT_STORE=broker)", line)
        for value, why in (("", "FLEET_AUDIT_REPORT_STORE unset"), ("file", "FLEET_AUDIT_REPORT_STORE is 'file', not broker"),
                           ("Broker", "FLEET_AUDIT_REPORT_STORE is 'Broker', not broker")):
            with self.subTest(value=value), patch.dict(os.environ, {report_status.REPORT_STORE_ENV: value}):
                store, line = report_status.select_report_store()
                self.assertNotIsInstance(store, report_status.BrokerReportStore)
                self.assertEqual(str(self.local), store)
                self.assertEqual(f"report store: file {self.local} ({why})", line)

    def test_an_explicit_root_is_the_file_store(self):
        store, line = report_status.select_report_store("/some/root")
        self.assertEqual("/some/root", store)
        self.assertEqual("report store: file /some/root (--root)", line)

    def test_the_line_never_carries_a_token(self):
        task = self.tmp / "task-token"
        task.write_text(REPORT_CREDENTIAL)
        with patch.dict(os.environ, {report_status.TASK_TOKEN_FILE_ENV: str(task)}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                report_status.main([])
        self.assertEqual(f"report store: broker {self.broker.url} (FLEET_AUDIT_REPORT_STORE=broker)\n", err.getvalue())
        self.assertNotIn(BROKER_TOKEN, err.getvalue())
        self.assertNotIn(REPORT_CREDENTIAL, err.getvalue())

    def test_report_status_logs_one_line_for_the_file_store(self):
        with patch.dict(os.environ, {report_status.REPORT_STORE_ENV: ""}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                report_status.main([])
        self.assertEqual(f"report store: file {self.local} (FLEET_AUDIT_REPORT_STORE unset)\n", err.getvalue())
        self.assertEqual([], self.broker.requests)

    def test_audit_report_logs_its_choice_once(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            audit_report.read_report_memory(AUDIT, 42, REPO)
            audit_report.read_report_memory(AUDIT, 42, REPO)
        self.assertEqual(1, err.getvalue().count("report store: broker"))
        audit_report._REPORT_STORE_LOGGED.clear()
        with patch.dict(os.environ, {report_status.REPORT_STORE_ENV: "file"}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                audit_report.write_report(AUDIT, json.loads(self.envelope()), NOW)
        self.assertIn(f"report store: file {self.local} (FLEET_AUDIT_REPORT_STORE is 'file', not broker)", err.getvalue())
        self.assertTrue((self.local / AUDIT / "acme" / "fleet" / "latest.json").is_file())
        self.assertEqual([], self.broker.calls("PUT"))
        # Unset, finish's stderr is what it was before the switch existed.
        audit_report._REPORT_STORE_LOGGED.clear()
        with patch.dict(os.environ, {report_status.REPORT_STORE_ENV: ""}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                audit_report.write_report(AUDIT, json.loads(self.envelope()), NOW + timedelta(minutes=1))
        self.assertNotIn("report store:", err.getvalue())


class TestTheKey(unittest.TestCase):
    def test_it_names_the_file_stores_directory(self):
        root = Path("/reports-root")
        with patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": str(root)}):
            for repo in ("acme/gitops", "Acme/GitOps", "github.com/acme/gitops", "ghe.example.com/Acme/GitOps",
                         "gitlab.com/group/sub/repo", "no_forge/local"):
                with self.subTest(repo=repo):
                    relative = audit_report.reports_dir_for("x", repo).relative_to(root / "x")
                    self.assertEqual(urllib.parse.quote("/".join(relative.parts), safe=""),
                                     report_status.broker_store_key(repo))

    def test_names_the_file_store_refuses(self):
        for repo in ("acme", "acme/../x", "a/b/c", "acme/", "/acme", "acme/gi tops", ""):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                report_status.broker_store_key(repo)


class TestWriteSemantics(BrokerTestCase):
    """audit_report's writes, through the broker, in the file store's order."""

    def remote(self) -> dict:
        return self.broker.stream(AUDIT, KEY)

    def write(self, n: int = 0, **kwargs) -> bool:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = audit_report.write_report(AUDIT, json.loads(self.envelope(n)), NOW + timedelta(minutes=n), **kwargs)
        self.err = err.getvalue()
        return result

    def test_the_ring_entry_then_latest_as_its_byte_copy(self):
        self.assertTrue(self.write())
        puts = [r for r in self.broker.requests if r["method"] == "PUT"]
        self.assertEqual([f"{PREFIX}/{AUDIT}/{KEY}/runs/{stamp_name(NOW)[:-5]}", f"{PREFIX}/{AUDIT}/{KEY}/latest"],
                         [r["path"] for r in puts])
        self.assertEqual(puts[0]["body"], puts[1]["body"])
        self.assertEqual(self.envelope(), self.remote()["latest"])
        self.assertIsNotNone(audit_report.read_report_memory(AUDIT, 42, REPO))
        self.assertFalse(self.local.exists(), "nothing is written to the file store")

    def test_a_failed_write_drops_latest(self):
        self.write()
        self.broker.refuse[("PUT", KEY)] = 503
        self.assertFalse(self.write(1))
        self.assertIn("report store write for compliance-audit failed", self.err)
        self.assertIn("unavailable: HTTP 503", self.err)
        self.assertEqual(("DELETE", f"{PREFIX}/{AUDIT}/{KEY}/latest"), self.broker.calls()[-1])
        self.assertIsNone(self.remote()["latest"])

    def test_a_failed_latest_write_drops_latest_and_keeps_the_ring(self):
        self.write()
        self.broker.refuse[("PUT", KEY)] = None
        real = self.broker.answer

        def refuse_latest(method, path, body):
            if method == "PUT" and path.endswith("/latest"):
                return 500, {"error": "boom"}
            return real(method, path, body)

        self.broker.answer = refuse_latest
        self.assertFalse(self.write(1))
        self.assertIsNone(self.remote()["latest"])
        self.assertEqual(2, len(self.remote()["runs"]))
        # The readers answer from the ring, flagged, as over the file store.
        store = report_status.BrokerReportStore(self.broker.url)
        envelope, missing = report_status.load_last(store, AUDIT, REPO)
        self.assertTrue(missing)
        self.assertEqual(json.loads(self.envelope(1)), envelope)

    def test_a_failed_write_over_an_unchanged_ledger_keeps_latest(self):
        self.write()
        self.broker.refuse[("PUT", KEY)] = 503
        self.assertFalse(self.write(1, ledger_unchanged=True))
        self.assertEqual([], self.broker.calls("DELETE"))
        self.assertEqual(self.envelope(), self.remote()["latest"])

    def test_an_unavailable_store_never_fails_the_write_call(self):
        self.broker.down = 503
        self.assertFalse(self.write())
        self.assertIn("could not drop the stale latest.json", self.err)

    def test_invalidation_deletes_latest_before_a_rewrite(self):
        self.write()
        with contextlib.redirect_stderr(io.StringIO()):
            audit_report.invalidate_report_memory(AUDIT, REPO)
        self.assertEqual(("DELETE", f"{PREFIX}/{AUDIT}/{KEY}/latest"), self.broker.calls()[-1])
        self.assertIsNone(self.remote()["latest"])
        self.assertEqual(1, len(self.remote()["runs"]))
        self.broker.down = 503
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            audit_report.invalidate_report_memory(AUDIT, REPO)
        self.assertIn("could not set the stored report for compliance-audit aside", err.getvalue())

    def test_the_memory_readers(self):
        self.write()
        diverted = json.loads(self.envelope(1))
        diverted.update({"issue_number": None, "status": audit_report.NO_FORGE_STATUS,
                         audit_report.UNREACHABLE_REPO_KEY: {"repo": REPO, "reason": "r"}})
        with contextlib.redirect_stderr(io.StringIO()):
            audit_report.write_report(AUDIT, diverted, NOW + timedelta(minutes=1))
            # A marked latest defers to the newest published ring entry.
            memory = audit_report.read_report_memory(AUDIT, 42, REPO)
            self.assertEqual("body", memory["ledger_body"])
            self.assertFalse(audit_report.store_never_published(audit_report.report_store_dir(AUDIT, REPO)))
            self.assertTrue(audit_report.store_never_published(audit_report.report_store_dir(AUDIT, "acme/other")))
            self.assertEqual([42], audit_report._stored_ledger_numbers(AUDIT, REPO))
            self.assertEqual(json.loads(self.envelope()), audit_report.newest_stored_record(AUDIT, REPO))

    def test_an_unavailable_store_is_a_lost_memory_not_a_first_run(self):
        self.write()
        self.broker.down = 503
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(audit_report.store_never_published(audit_report.report_store_dir(AUDIT, REPO)))
            self.assertIsNone(audit_report.read_report_memory(AUDIT, 42, REPO))
            self.assertIsNone(audit_report.newest_stored_record(AUDIT, REPO))
        self.assertIn("unreadable", err.getvalue())


class TestAnOutageIsNoFirstRun(BrokerTestCase):
    """A store the broker cannot answer for is there and unreadable, never
    absent: not when the ring's listing fails, and not when every entry's read
    does, with `latest.json` missing or marked."""

    def write(self, envelope: dict, n: int) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(audit_report.write_report(AUDIT, envelope, NOW + timedelta(minutes=n)))

    def diverted(self, n: int = 1) -> dict:
        envelope = json.loads(self.envelope(n))
        envelope.update({"issue_number": None, "status": audit_report.NO_FORGE_STATUS,
                         audit_report.UNREACHABLE_REPO_KEY: {"repo": REPO, "reason": "r"}})
        return envelope

    def break_ring(self, listing: bool = False, entries: bool = False) -> None:
        real = self.broker.answer
        runs = f"{PREFIX}/{AUDIT}/{KEY}/runs"

        def answer(method, path, body):
            if method == "GET" and ((listing and path == runs) or (entries and path.startswith(runs + "/"))):
                return 503, {"code": "AUDIT_REPORT_STORE_UNAVAILABLE"}
            return real(method, path, body)

        self.broker.answer = answer

    def test_a_failed_listing_with_no_latest_is_unknowable(self):
        self.write(json.loads(self.envelope()), 0)
        self.broker.stream(AUDIT, KEY)["latest"] = None
        self.break_ring(listing=True)
        directory = audit_report.report_store_dir(AUDIT, REPO)
        self.assertIsNone(audit_report.first_stored_record(directory, lambda envelope: True))
        self.assertIsNone(audit_report.newest_stored_record(AUDIT, REPO))

    def test_a_failed_listing_still_answers_from_a_trusted_latest(self):
        self.write(json.loads(self.envelope()), 0)
        self.break_ring(listing=True)
        directory = audit_report.report_store_dir(AUDIT, REPO)
        self.assertEqual(json.loads(self.envelope()), audit_report.first_stored_record(directory, lambda e: True))

    def test_an_unread_ring_behind_a_marked_latest_was_published(self):
        self.write(json.loads(self.envelope()), 0)
        self.write(self.diverted(), 1)
        for failure in ({"listing": True}, {"entries": True}):
            with self.subTest(**failure):
                self.broker.answer = FakeBroker.answer.__get__(self.broker)
                self.break_ring(**failure)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(
                        audit_report.store_never_published(audit_report.report_store_dir(AUDIT, REPO))
                    )
                    self.assertEqual(
                        (None, False), audit_report._store_ledger_envelope(AUDIT, REPO, open_fix=False)
                    )

    def test_a_ring_read_whole_with_no_published_entry_was_never_published(self):
        self.write(self.diverted(0), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(audit_report.store_never_published(audit_report.report_store_dir(AUDIT, REPO)))
            self.assertEqual((None, True), audit_report._store_ledger_envelope(AUDIT, REPO, open_fix=False))


class TestReaders(BrokerTestCase):
    def test_project_reads_through_the_broker(self):
        self.put_remote(0)
        self.put_remote(1, latest=False)
        projection = report_status.project(self.store(), scratch=str(self.tmp / "scratch"))
        self.assertTrue(projection["root_exists"])
        self.assertEqual(f"broker {self.broker.url}", projection["root"])
        repo = projection["streams"][AUDIT]["repos"][REPO]
        self.assertTrue(repo["latest_missing"], "a ring entry newer than latest reads as today")
        self.assertEqual(2, len(repo["runs"]))
        self.assertEqual("completed", projection["streams"][AUDIT]["liveness"])
        json.dumps(projection)

    def test_an_unavailable_broker_is_an_unreadable_store(self):
        self.broker.down = 503
        projection = report_status.project(self.store(), scratch=str(self.tmp / "scratch"))
        self.assertFalse(projection["root_exists"])
        self.assertIn("HTTP 503", projection["root_error"])


class TestSeed(BrokerTestCase):
    def plant_local(self, count: int = 3, latest: str = "newest", repo_dir=("acme", "fleet")) -> Path:
        directory = self.local.joinpath(AUDIT, *repo_dir)
        (directory / "runs").mkdir(parents=True)
        for n in range(count):
            (directory / "runs" / stamp_name(NOW + timedelta(minutes=n))).write_bytes(self.envelope(n))
        (directory / "runs" / "x.json.tmp").write_text("partial")
        if latest == "newest":
            (directory / "latest.json").write_bytes(self.envelope(count - 1))
        elif latest == "older":
            (directory / "latest.json").write_bytes(self.envelope(0))
        return directory

    def tree_digest(self) -> dict:
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.local.rglob("*")) if p.is_file() and p.name != report_status.SEED_MARKER_NAME}

    def test_a_missing_stream_is_seeded_once_oldest_first_then_latest(self):
        directory = self.plant_local()
        before = self.tree_digest()
        store = self.store(str(self.local))
        self.assertEqual(self.envelope(2), store.read_latest(AUDIT, REPO))
        puts = self.broker.calls("PUT")
        self.assertEqual(
            [("PUT", f"{PREFIX}/{AUDIT}/{KEY}/runs/{stamp_name(NOW + timedelta(minutes=n))[:-5]}") for n in range(3)]
            + [("PUT", f"{PREFIX}/{AUDIT}/{KEY}/latest")],
            puts,
        )
        self.assertTrue((directory / report_status.SEED_MARKER_NAME).is_file())
        self.assertEqual(1, sum("seeded compliance-audit in acme/fleet" in line for line in self.logged), self.logged)
        self.assertEqual(before, self.tree_digest(), "the seed changes no local envelope")
        # Again, in a new process: the marker holds it.
        self.broker.requests.clear()
        again = self.store(str(self.local))
        again.read_latest(AUDIT, REPO)
        self.assertEqual([], self.broker.calls("PUT"))
        self.assertEqual(1, len(self.broker.requests))

    def test_latest_is_copied_only_as_a_copy_of_the_newest_run(self):
        self.plant_local(latest="older")
        store = self.store(str(self.local))
        self.assertIsNone(store.read_latest(AUDIT, REPO))
        self.assertEqual(3, len(store.list_runs(AUDIT, REPO)))
        self.assertNotIn(("PUT", f"{PREFIX}/{AUDIT}/{KEY}/latest"), self.broker.calls("PUT"))
        self.assertIn("latest not copied", self.logged[-1])

    def test_a_stream_the_broker_holds_is_not_seeded(self):
        directory = self.plant_local()
        self.put_remote(9)
        self.store(str(self.local)).read_latest(AUDIT, REPO)
        self.assertEqual([], self.broker.calls("PUT"))
        self.assertFalse((directory / report_status.SEED_MARKER_NAME).exists())

    def test_a_refused_stream_is_skipped_and_logged(self):
        directory = self.plant_local()
        self.broker.refuse[("PUT", KEY)] = 403
        store = self.store(str(self.local))
        self.assertIsNone(store.read_latest(AUDIT, REPO))
        self.assertEqual(1, len(self.broker.calls("PUT")))
        self.assertFalse((directory / report_status.SEED_MARKER_NAME).exists())
        self.assertIn("HTTP 403", self.logged[-1])
        self.assertIn("not seeding compliance-audit in acme/fleet", self.logged[-1])

    def test_a_listing_seeds_every_local_stream(self):
        self.plant_local()
        self.plant_local(count=1, repo_dir=("acme", "other"))
        self.broker.refuse[("PUT", "acme%2Fother")] = 403
        store = self.store(str(self.local))
        self.assertEqual([REPO], store.repo_dirs(AUDIT))
        self.assertEqual(2, sum(line.startswith("report store: ") for line in self.logged), self.logged)

    def test_an_unavailable_broker_leaves_the_seed_for_the_next_run(self):
        directory = self.plant_local()
        self.broker.down = 503
        with self.assertRaises(report_status.ReportStoreUnavailable):
            self.store(str(self.local)).read_latest(AUDIT, REPO)
        self.assertFalse((directory / report_status.SEED_MARKER_NAME).exists())
        self.broker.down = None
        self.store(str(self.local)).read_latest(AUDIT, REPO)
        self.assertTrue((directory / report_status.SEED_MARKER_NAME).exists())

    def test_repeating_a_seed_is_harmless(self):
        directory = self.plant_local()
        self.store(str(self.local)).read_latest(AUDIT, REPO)
        (directory / report_status.SEED_MARKER_NAME).unlink()
        self.broker.requests.clear()
        self.store(str(self.local)).read_latest(AUDIT, REPO)
        self.assertEqual([], self.broker.calls("PUT"), "the broker already holds it")
        self.assertEqual(3, len(self.remote_runs()))
        self.assertTrue((directory / report_status.SEED_MARKER_NAME).exists())

    def test_a_seed_that_stopped_part_way_is_resumed(self):
        directory = self.plant_local(count=5)
        before = self.tree_digest()
        real = self.broker.answer
        puts = []

        def fail_the_fourth(method, path, body):
            if method == "PUT":
                puts.append(path)
                if len(puts) == 4:
                    return 503, {"code": "AUDIT_REPORT_STORE_UNAVAILABLE"}
            return real(method, path, body)

        self.broker.answer = fail_the_fourth
        with self.assertRaises(report_status.ReportStoreUnavailable):
            self.store(str(self.local)).read_latest(AUDIT, REPO)
        self.assertEqual(3, len(self.remote_runs()))
        self.assertIsNone(self.broker.stream(AUDIT, KEY)["latest"])
        self.assertFalse((directory / report_status.SEED_MARKER_NAME).exists())
        self.assertIn("stopped after 3 run(s), to be resumed", self.logged[-1])
        # The next run copies only what is missing, oldest first, then latest.
        self.broker.answer = real
        self.broker.requests.clear()
        self.assertEqual(self.envelope(4), self.store(str(self.local)).read_latest(AUDIT, REPO))
        self.assertEqual(
            [("PUT", f"{PREFIX}/{AUDIT}/{KEY}/runs/{stamp_name(NOW + timedelta(minutes=n))[:-5]}") for n in (3, 4)]
            + [("PUT", f"{PREFIX}/{AUDIT}/{KEY}/latest")],
            self.broker.calls("PUT"),
        )
        self.assertEqual(5, len(self.remote_runs()))
        self.assertTrue((directory / report_status.SEED_MARKER_NAME).exists())
        self.assertEqual(before, self.tree_digest())

    def test_an_outage_mid_seed_fails_the_touch_and_strands_nothing(self):
        """A broker that stops answering part way through a seed fails the
        read or write that touched the stream, as an outage, and the next
        touch in the same process seeds again. A run's write never lands on a
        stream part copied, which would leave the broker with a stamp the
        local ring lacks and the rest of the local history never copied."""
        directory = self.plant_local(count=5)
        real = self.broker.answer
        puts = []

        def fail_the_fourth_once(method, path, body):
            if method == "PUT":
                puts.append(path)
                if len(puts) == 4:
                    return 503, {"code": "AUDIT_REPORT_STORE_UNAVAILABLE"}
            return real(method, path, body)

        self.broker.answer = fail_the_fourth_once
        store = self.store(str(self.local))
        with self.assertRaises(report_status.ReportStoreUnavailable):
            store.read_latest(AUDIT, REPO)
        self.assertEqual(3, len(self.remote_runs()))
        # The same process's next touch, the run's own write: the seed first.
        store.write_run(AUDIT, REPO, stamp_name(NOW + timedelta(minutes=9)), self.envelope(9))
        self.assertEqual(
            sorted(stamp_name(NOW + timedelta(minutes=n))[:-5] for n in (0, 1, 2, 3, 4, 9)),
            sorted(self.remote_runs()),
        )
        self.assertTrue((directory / report_status.SEED_MARKER_NAME).exists())

    def test_an_outage_during_a_listing_seed_fails_the_listing(self):
        self.plant_local()
        self.broker.refuse[("PUT", KEY)] = 503
        with self.assertRaises(report_status.ReportStoreUnavailable):
            self.store(str(self.local)).stream_ids()

    def test_a_run_that_lands_mid_seed_keeps_its_latest(self):
        """A run writing through the broker while the seed copies (a profile
        worker, which takes no local lock) is never rolled back to the seed's
        older latest."""
        self.plant_local()
        real = self.broker.answer
        newer = self.envelope(9)
        newer_stamp = stamp_name(NOW + timedelta(minutes=9))[:-5]

        def a_run_lands_after_the_last_copy(method, path, body):
            answer = real(method, path, body)
            if method == "PUT" and path.endswith(stamp_name(NOW + timedelta(minutes=2))[:-5]):
                stream = self.broker.stream(AUDIT, KEY)
                stream["runs"][newer_stamp] = newer
                stream["latest"] = newer
            return answer

        self.broker.answer = a_run_lands_after_the_last_copy
        self.assertEqual(newer, self.store(str(self.local)).read_latest(AUDIT, REPO))
        self.assertNotIn(("PUT", f"{PREFIX}/{AUDIT}/{KEY}/latest"), self.broker.calls("PUT"))
        self.assertEqual(newer, self.broker.stream(AUDIT, KEY)["latest"])
        self.assertIn("latest not copied", self.logged[-1])

    def test_a_newer_ring_entry_alone_also_holds_latest_back(self):
        """A run whose ring entry landed and whose latest has not (or failed)."""
        self.plant_local()
        real = self.broker.answer

        def a_ring_entry_lands(method, path, body):
            answer = real(method, path, body)
            if method == "PUT" and path.endswith(stamp_name(NOW + timedelta(minutes=2))[:-5]):
                self.broker.stream(AUDIT, KEY)["runs"][stamp_name(NOW + timedelta(minutes=9))[:-5]] = self.envelope(9)
            return answer

        self.broker.answer = a_ring_entry_lands
        self.store(str(self.local)).read_latest(AUDIT, REPO)
        self.assertIsNone(self.broker.stream(AUDIT, KEY)["latest"])

    def test_seeds_on_one_pod_take_turns(self):
        """The seed holds an flock on the local stream directory and looks for
        the marker again once it has it, so a second seed waits and then finds
        the first one's work done."""
        import fcntl  # noqa: PLC0415

        directory = self.plant_local()
        fd = os.open(directory, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        seeder = threading.Thread(target=lambda: self.store(str(self.local))._maybe_seed(AUDIT, REPO))
        seeder.start()
        seeder.join(0.5)
        self.assertTrue(seeder.is_alive(), "the seed waits for the lock")
        self.assertEqual([], self.broker.requests)
        (directory / report_status.SEED_MARKER_NAME).write_text("{}\n")
        fcntl.flock(fd, fcntl.LOCK_UN)
        seeder.join(5)
        self.assertFalse(seeder.is_alive())
        self.assertEqual([], self.broker.requests)

    def remote_runs(self) -> dict:
        return self.broker.stream(AUDIT, KEY)["runs"]

    def test_no_local_store_no_seed(self):
        store = self.store(str(self.tmp / "absent"))
        self.assertIsNone(store.read_latest(AUDIT, REPO))
        self.assertEqual([("GET", f"{PREFIX}/{AUDIT}/{KEY}/latest")], self.broker.calls())

    def test_finish_writes_seed_first(self):
        """A write is a touch too: a run that reads no memory first (no open
        ledger) still copies the history in before its own run lands."""
        self.plant_local()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(audit_report.write_report(AUDIT, json.loads(self.envelope(5)), NOW + timedelta(minutes=5)))
        self.assertEqual(4, len(self.remote_runs()))
        self.assertEqual(self.envelope(5), self.broker.stream(AUDIT, KEY)["latest"])
        self.assertIn("seeded compliance-audit in acme/fleet into the broker", err.getvalue())


if __name__ == "__main__":
    unittest.main()
