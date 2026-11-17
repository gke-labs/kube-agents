#!/usr/bin/env python3
"""The broker's audit-report routes (#5619): who may read and write, and the relay.

A fake store listener records what reaches it, so every refusal can show that
nothing did. Task credentials are the golden fixtures, verified by the real
ProfileTaskGate; a stand-in gate covers claims the verifier never admits.
"""

from __future__ import annotations

import ast
import dataclasses
import http.client
import importlib.util
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import credential_proxy
import task_credential
from credential_proxy import CredentialProxyHandler, Policy
from test_credential_proxy_profile import CASES, DOC, POD, POD_UID, PROFILE_SA, VALID, _RecordingExecutor

AUDIT_REPORT_PATH = Path(__file__).resolve().parents[1] / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
LISTENER_PATH = Path(__file__).resolve().parent / "audit_report_store.py"

REPORT = CASES["a report credential: unattended, a builtin schedule trigger, no target"]
REPORT_COLLECTOR = CASES["a report credential: unattended, a collector trigger, no target"]
TARGETED_SCHEDULE = CASES["a builtin schedule trigger"]
TARGETED_COLLECTOR = CASES["a collector trigger with no stream"]
TARGETED_EMPTY_STREAM = CASES["a schedule trigger with an explicit empty stream"]

AUDIT = "compliance-audit"
OTHER_AUDIT = "obtainability-audit"
GITOPS_KEY = "acme%2Fgitops"
STREAM = f"/v1/audit-reports/{AUDIT}/{GITOPS_KEY}"
NO_FORGE_STREAM = f"/v1/audit-reports/{AUDIT}/no_forge%2Flocal"
RUN = "20261113T120000.123456Z"
STORE_TOKEN = "store-token-for-the-routes-test"
BODY = json.dumps({"envelope": "a-body-marker-never-logged"}).encode("utf-8")
SHELL_SA = "system:serviceaccount:kubeagents-system:platform-agent"

GITOPS = {"type": "github", "url": "https://github.com/acme/gitops"}
MANAGED = [GITOPS, {"type": "github", "url": "https://github.com/acme/terraform"}]

READS = (
    ("GET", "/v1/audit-reports"),
    ("GET", f"{STREAM}/latest"),
    ("GET", f"{STREAM}/runs"),
    ("GET", f"{STREAM}/runs/{RUN}"),
)
WRITES = (
    ("PUT", f"{STREAM}/runs/{RUN}"),
    ("PUT", f"{STREAM}/latest"),
    ("DELETE", f"{STREAM}/latest"),
)


class FakeStore:
    """A local listener that records every request and answers as told."""

    def __init__(self):
        self.seen: list[dict] = []
        self.answer: tuple[int, bytes] = (200, b'{"ok":true}')
        store = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                store.seen.append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": self.rfile.read(length) if length else None,
                })
                status, payload = store.answer
                self.send_response(status)
                if status != 204:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if status != 204:
                    self.wfile.write(payload)

            do_GET = do_PUT = do_DELETE = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _MutableAuthenticator:
    authenticates = True

    def __init__(self, principal):
        self.principal = principal

    def authenticate(self, headers):
        return self.principal


class _StandInGate:
    """Admits any token as a task with the claims given: for claims the verifier refuses."""

    def __init__(self, profile, trigger):
        self.task = task_credential.TaskClaims(
            task="task-1", profile=profile, tier="unattended", pod=POD, pod_uid=POD_UID,
            expires=0, issued_at=0, jti="jti-stand-in", issuer="i", target=None, trigger=trigger,
        )

    def admit(self, principal, token):
        return dataclasses.replace(principal, caller="task stand-in", task=self.task, task_target=None)


class RoutesTestCase(unittest.TestCase):
    ATTRIBUTES = (
        "policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator", "profile_gate",
        "report_store", "report_profiles", "broker_role",
    )

    def setUp(self):
        for attribute in self.ATTRIBUTES:
            self.addCleanup(
                self._restore, attribute, attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        self.addCleanup(CredentialProxyHandler.draining.clear)
        CredentialProxyHandler.executor = _RecordingExecutor()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.broker_role = credential_proxy.ROLE_COMBINED

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.token_file = self.tmp / "token"
        self.token_file.write_text(STORE_TOKEN + "\n", encoding="utf-8")
        self.state_dir = self.tmp / "gitops"
        self.state_dir.mkdir()
        self.addCleanup(setattr, credential_proxy, "_managed_repository_cache", None)
        self.set_gitops(MANAGED, GITOPS)
        patcher = mock.patch.dict(os.environ, {"GITOPS_STATE_PATH": str(self.state_dir / "managed_repos")})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.store = FakeStore()
        self.addCleanup(self.store.close)
        CredentialProxyHandler.report_store = credential_proxy.ReportStoreRelay.from_url(self.store.url, str(self.token_file))
        CredentialProxyHandler.report_profiles = frozenset({"auditor"})

        self.profile = credential_proxy.Principal(
            workload=PROFILE_SA, uid="sa-uid", role=credential_proxy.CALLER_ROLE_PROFILE, pod=POD, pod_uid=POD_UID,
        )
        self.shell = credential_proxy.Principal(workload=SHELL_SA, uid="shell-uid", role=credential_proxy.CALLER_ROLE_SHELL)
        self.authenticator = _MutableAuthenticator(self.profile)
        CredentialProxyHandler.authenticator = self.authenticator
        self.set_gate("unattended")

        self.records: list[str] = []
        test = self

        class Capture(logging.Handler):
            def emit(self, record):
                test.records.append(record.getMessage() + " " + json.dumps(getattr(record, "audit", {}), default=str))

        level = credential_proxy.LOGGER.level
        previous, propagate = list(credential_proxy.LOGGER.handlers), credential_proxy.LOGGER.propagate
        credential_proxy.LOGGER.setLevel(logging.DEBUG)
        credential_proxy.LOGGER.handlers = [Capture()]
        credential_proxy.LOGGER.propagate = False
        self.addCleanup(credential_proxy.LOGGER.setLevel, level)
        self.addCleanup(setattr, credential_proxy.LOGGER, "propagate", propagate)
        self.addCleanup(setattr, credential_proxy.LOGGER, "handlers", previous)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.assert_nothing_secret_logged)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def set_gate(self, tier):
        CredentialProxyHandler.profile_gate = credential_proxy.ProfileTaskGate(
            trusted=tuple(DOC["trusted"]),
            identities={PROFILE_SA: task_credential.ProfileIdentity("auditor", tier)},
            max_lifetime_raw=f"{DOC['max_lifetime_seconds']}s",
            clock=lambda: VALID["now"],
        )

    def set_gitops(self, managed, gitops):
        # managed_repositories() caches for 30 s; a state change here is read at once.
        credential_proxy._managed_repository_cache = None
        (self.state_dir / "managed_repos").write_text(json.dumps(managed), encoding="utf-8")
        path = self.state_dir / "gitops_repo"
        if gitops is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(gitops if isinstance(gitops, str) else json.dumps(gitops), encoding="utf-8")

    def request(self, method, path, token=None, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        sent = dict(headers or {})
        if token is not None:
            sent[credential_proxy.TASK_CREDENTIAL_HEADER] = token
        if body is None and method == "PUT":
            body = BODY
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        return response.status, parsed

    def assert_relayed(self, method, path, status_body, body=None):
        status, answer = status_body
        self.assertEqual(200, status, answer)
        self.assertEqual({"ok": True}, answer)
        seen = self.store.seen[-1]
        self.assertEqual(method, seen["method"])
        self.assertEqual(path, seen["path"])
        # The broker's own token, read from the file: compared, never printed.
        self.assertTrue(seen["authorization"] == f"Bearer {STORE_TOKEN}", "the store saw another Authorization")
        self.assertEqual(body if method == "PUT" else None, seen["body"])

    def assert_refused(self, status_body, rule=None, status=403, code=None):
        got, answer = status_body
        self.assertEqual(status, got, answer)
        if rule is not None:
            self.assertEqual(rule, answer.get("rule"), answer)
        if code is not None:
            self.assertEqual(code, answer.get("code"), answer)
        self.assertEqual([], self.store.seen, "a refused request reached the store")

    def assert_nothing_secret_logged(self):
        text = "\n".join(self.records)
        self.assertNotIn(STORE_TOKEN, text)
        self.assertNotIn("a-body-marker-never-logged", text)
        for case in (REPORT, VALID, TARGETED_SCHEDULE):
            self.assertNotIn(case["token"], text)


class PermittedTest(RoutesTestCase):
    def test_the_matching_schedule_writes_its_stream(self):
        for method, path in WRITES:
            with self.subTest(method=method, path=path):
                self.assert_relayed(method, path, self.request(method, path, token=REPORT["token"]), BODY)

    def test_an_allowlisted_profile_reads_every_route(self):
        for method, path in READS:
            with self.subTest(path=path):
                self.assert_relayed(method, path, self.request(method, path, token=REPORT["token"]))

    def test_a_targeted_credential_follows_the_same_rules(self):
        # Its target is irrelevant here: a builtin schedule trigger writes.
        self.set_gate("read")
        path = f"{STREAM}/runs/{RUN}"
        self.assert_relayed("PUT", path, self.request("PUT", path, token=TARGETED_SCHEDULE["token"]), BODY)
        # And one with no trigger reads.
        self.assert_relayed("GET", f"{STREAM}/latest", self.request("GET", f"{STREAM}/latest", token=VALID["token"]))

    def test_no_forge_is_the_key_when_no_gitops_repo_is_named(self):
        self.set_gitops([], None)
        path = f"{NO_FORGE_STREAM}/latest"
        self.assert_relayed("PUT", path, self.request("PUT", path, token=REPORT["token"]), BODY)

    def test_a_repository_change_takes_effect_without_a_restart(self):
        other = {"type": "github", "url": "https://github.com/acme/other"}
        self.set_gitops(MANAGED + [other], other)
        path = f"/v1/audit-reports/{AUDIT}/acme%2Fother/latest"
        self.assert_relayed("DELETE", path, self.request("DELETE", path, token=REPORT["token"]))
        self.store.seen.clear()
        status, answer = self.request("DELETE", f"{STREAM}/latest", token=REPORT["token"])
        self.assertEqual(403, status, answer)
        self.assertEqual(credential_proxy.RULE_REPORT_REPOSITORY_KEY, answer.get("rule"), answer)
        self.assertEqual([], self.store.seen)

    def test_the_shell_reads_every_stream(self):
        self.authenticator.principal = self.shell
        for method, path in READS + (("GET", f"/v1/audit-reports/{OTHER_AUDIT}/other%2Frepo/latest"),):
            with self.subTest(path=path):
                self.assert_relayed(method, path, self.request(method, path))

    def test_the_shell_writes_any_audit_to_the_install_key(self):
        self.authenticator.principal = self.shell
        for audit in (AUDIT, OTHER_AUDIT, "an-audit-no-schedule-names"):
            path = f"/v1/audit-reports/{audit}/{GITOPS_KEY}/runs/{RUN}"
            with self.subTest(audit=audit):
                self.assert_relayed("PUT", path, self.request("PUT", path), BODY)

    def test_the_listener_status_and_body_come_back(self):
        for status, payload in ((404, b'{"error":"no such report"}'), (409, b'{"error":"latest is no ring copy"}'),
                                (413, b'{"error":"too large"}'), (500, b'{"error":"the store could not be read"}')):
            with self.subTest(status=status):
                self.store.answer = (status, payload)
                got, answer = self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
                self.assertEqual(status, got)
                self.assertEqual(json.loads(payload), answer)

    def test_a_204_comes_back_with_no_body(self):
        self.store.answer = (204, b"")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("DELETE", f"{STREAM}/latest", headers={credential_proxy.TASK_CREDENTIAL_HEADER: REPORT["token"]})
        response = connection.getresponse()
        self.assertEqual(204, response.status)
        self.assertEqual(b"", response.read())
        self.assertIsNone(response.getheader("Content-Length"))
        # The connection is still good for the next request.
        connection.request("GET", f"{STREAM}/latest", headers={credential_proxy.TASK_CREDENTIAL_HEADER: REPORT["token"]})
        self.assertEqual(204, connection.getresponse().status)

    def test_the_token_is_read_fresh_on_every_call(self):
        self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
        self.token_file.write_text("a-rotated-store-token", encoding="utf-8")
        self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
        self.assertTrue(self.store.seen[0]["authorization"] == f"Bearer {STORE_TOKEN}")
        self.assertTrue(self.store.seen[1]["authorization"] == "Bearer a-rotated-store-token")


class RefusalTest(RoutesTestCase):
    def test_a_wrong_stream(self):
        path = f"/v1/audit-reports/{OTHER_AUDIT}/{GITOPS_KEY}/latest"
        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                self.assert_refused(self.request(method, path, token=REPORT["token"]), credential_proxy.RULE_REPORT_TRIGGER_STREAM)

    def test_the_wrong_repository_key(self):
        for key in ("acme%2Fterraform", "other%2Frepo", "Acme%2Fgitops", "acme%2fgitops", "github.com%2Facme%252Fgitops",
                    "no_forge%2Flocal"):
            with self.subTest(key=key):
                path = f"/v1/audit-reports/{AUDIT}/{key}/runs/{RUN}"
                self.assert_refused(self.request("PUT", path, token=REPORT["token"]), credential_proxy.RULE_REPORT_REPOSITORY_KEY)

    def test_a_missing_trigger_on_a_write(self):
        self.set_gate("read")
        for method, path in WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path, token=VALID["token"]), credential_proxy.RULE_REPORT_NO_TRIGGER)

    def test_a_trigger_that_binds_no_stream(self):
        self.assert_refused(self.request("PUT", f"{STREAM}/latest", token=REPORT_COLLECTOR["token"]),
                            credential_proxy.RULE_REPORT_TRIGGER_STREAM)
        self.set_gate("read")
        for case in (TARGETED_COLLECTOR, TARGETED_EMPTY_STREAM):
            with self.subTest(case=case["name"]):
                self.assert_refused(self.request("PUT", f"{STREAM}/latest", token=case["token"]),
                                    credential_proxy.RULE_REPORT_TRIGGER_STREAM)

    def test_another_schedules_trigger_on_a_builtin_stream(self):
        trigger = task_credential.TaskTrigger(name=f"schedule/{OTHER_AUDIT}", origin="builtin", stream=OTHER_AUDIT)
        CredentialProxyHandler.profile_gate = _StandInGate("auditor", trigger)
        self.assert_refused(self.request("PUT", f"{STREAM}/runs/{RUN}", token="t"), credential_proxy.RULE_REPORT_TRIGGER_STREAM)

    def test_a_user_or_unknown_origin(self):
        # The verifier admits only builtin today (task_credential.TRIGGER_ORIGINS);
        # this is the route's own check, should it ever admit another.
        for origin in ("user", "", "Builtin"):
            with self.subTest(origin=origin):
                trigger = task_credential.TaskTrigger(name=f"schedule/{AUDIT}", origin=origin, stream=AUDIT)
                CredentialProxyHandler.profile_gate = _StandInGate("auditor", trigger)
                self.assert_refused(self.request("PUT", f"{STREAM}/latest", token="t"),
                                    credential_proxy.RULE_REPORT_TRIGGER_ORIGIN)

    def test_a_stream_with_a_name_that_is_not_its_schedule(self):
        trigger = task_credential.TaskTrigger(name="stall-watch", origin="builtin", stream=AUDIT)
        CredentialProxyHandler.profile_gate = _StandInGate("auditor", trigger)
        self.assert_refused(self.request("PUT", f"{STREAM}/latest", token="t"), credential_proxy.RULE_REPORT_TRIGGER_STREAM)

    def test_a_profile_outside_the_allowlist(self):
        for allowlist in (frozenset(), frozenset({"fleet-audit", "platform"})):
            CredentialProxyHandler.report_profiles = allowlist
            for method, path in READS + WRITES:
                with self.subTest(allowlist=sorted(allowlist), method=method, path=path):
                    self.assert_refused(self.request(method, path, token=REPORT["token"]), credential_proxy.RULE_REPORT_PROFILE)

    def test_a_missing_task_credential(self):
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path), credential_proxy.RULE_REPORT_NO_TASK)

    def test_a_broker_that_verifies_no_credentials_refuses_a_profile_caller(self):
        CredentialProxyHandler.profile_gate = None
        self.assert_refused(self.request("GET", f"{STREAM}/latest"), credential_proxy.RULE_REPORT_NO_TASK)

    def test_the_shell_writing_to_an_unregistered_key(self):
        self.authenticator.principal = self.shell
        for key in ("other%2Frepo", "acme%2Fremoved", "no_forge%2Flocal"):
            for method in ("PUT", "DELETE"):
                with self.subTest(key=key, method=method):
                    path = f"/v1/audit-reports/{AUDIT}/{key}/latest"
                    self.assert_refused(self.request(method, path), credential_proxy.RULE_REPORT_REPOSITORY_KEY)

    def test_a_spelling_that_normalises_to_a_registered_repo_is_refused(self):
        # Canonical spelling only: the listener would map each of these to a
        # registered repository's directory, and the broker still refuses.
        self.authenticator.principal = self.shell
        for key in ("ACME%2Fgitops", "acme%2fgitops", "github.com%2Facme%252Fgitops", "Acme%2FTerraform",
                    "acme%252Fterraform", "github.com%2Facme%252fterraform"):
            with self.subTest(key=key):
                path = f"/v1/audit-reports/{AUDIT}/{key}/runs/{RUN}"
                self.assert_refused(self.request("PUT", path), credential_proxy.RULE_REPORT_REPOSITORY_KEY)

    def test_the_shell_writes_a_second_registered_repo_and_a_profile_does_not(self):
        path = f"/v1/audit-reports/{AUDIT}/acme%2Fterraform/runs/{RUN}"
        self.assert_refused(self.request("PUT", path, token=REPORT["token"]), credential_proxy.RULE_REPORT_REPOSITORY_KEY)
        self.authenticator.principal = self.shell
        self.assert_relayed("PUT", path, self.request("PUT", path), BODY)

    def test_the_shell_writes_only_no_forge_with_no_gitops_repo(self):
        self.authenticator.principal = self.shell
        self.set_gitops([], None)
        self.assert_refused(self.request("PUT", f"{STREAM}/latest"), credential_proxy.RULE_REPORT_REPOSITORY_KEY)
        self.assert_relayed("PUT", f"{NO_FORGE_STREAM}/latest", self.request("PUT", f"{NO_FORGE_STREAM}/latest"), BODY)

    def test_other_roles(self):
        for role in (
            credential_proxy.CALLER_ROLE_CHAT, credential_proxy.CALLER_ROLE_A2A_CHAT, credential_proxy.CALLER_ROLE_SESSION,
            credential_proxy.CALLER_ROLE_WATCHER, credential_proxy.CALLER_ROLE_MCP, credential_proxy.CALLER_ROLE_SCOPE,
        ):
            self.authenticator.principal = credential_proxy.Principal(workload="w", uid="u", role=role)
            for method, path in READS + WRITES:
                with self.subTest(role=role, method=method, path=path):
                    self.assert_refused(self.request(method, path), code="CALLER_ROLE_FORBIDDEN")

    def test_a_principal_with_no_role_is_refused(self):
        # The role table admits it (a Unix-socket broker); these routes don't.
        self.authenticator.principal = credential_proxy.Principal(workload="unauthenticated")
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path), credential_proxy.RULE_REPORT_ROLE)

    def test_an_oversize_body(self):
        # Announced and not sent: the broker answers before reading a byte.
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=10) as conn:
            conn.sendall(
                f"PUT {STREAM}/runs/{RUN} HTTP/1.1\r\nHost: x\r\n"
                f"{credential_proxy.TASK_CREDENTIAL_HEADER}: {REPORT['token']}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {credential_proxy.AUDIT_REPORT_MAX_BYTES + 1}\r\n\r\n".encode()
            )
            response = http.client.HTTPResponse(conn)
            response.begin()
            self.assertEqual(413, response.status)
            self.assertEqual("close", response.getheader("Connection"))
        self.assertEqual([], self.store.seen)
        # At the cap exactly, it is relayed.
        body = b'"' + b"x" * (credential_proxy.AUDIT_REPORT_MAX_BYTES - 2) + b'"'
        self.store.answer = (204, b"")
        status, _ = self.request("PUT", f"{STREAM}/runs/{RUN}", token=REPORT["token"], body=body)
        self.assertEqual(204, status)
        self.assertEqual(len(body), len(self.store.seen[0]["body"]))

    def test_a_chunked_or_unframed_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("PUT", f"{STREAM}/latest", body=iter([BODY]), encode_chunked=True,
                           headers={credential_proxy.TASK_CREDENTIAL_HEADER: REPORT["token"]})
        response = connection.getresponse()
        self.assertEqual(411, response.status)
        self.assertEqual([], self.store.seen)

    def test_the_store_not_configured(self):
        CredentialProxyHandler.report_store = None
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path, token=REPORT["token"]), status=503,
                                    code="AUDIT_REPORT_STORE_UNCONFIGURED")

    def test_an_unreadable_store_token(self):
        self.token_file.unlink()
        self.assert_refused(self.request("GET", f"{STREAM}/latest", token=REPORT["token"]), status=503,
                            code="AUDIT_REPORT_STORE_UNAVAILABLE")
        self.token_file.write_text("  \n", encoding="utf-8")
        self.assert_refused(self.request("PUT", f"{STREAM}/latest", token=REPORT["token"]), status=503,
                            code="AUDIT_REPORT_STORE_UNAVAILABLE")

    def test_a_stale_gitops_key_leaves_only_no_forge_to_a_profile(self):
        # Two managed repositories and a gitops_repo naming neither: finish
        # writes its no-repository-chosen envelope under no_forge.
        self.set_gitops(MANAGED, {"type": "github", "url": "https://github.com/acme/removed"})
        self.assert_refused(self.request("PUT", f"{STREAM}/latest", token=REPORT["token"]),
                            credential_proxy.RULE_REPORT_REPOSITORY_KEY)
        self.assert_relayed("PUT", f"{NO_FORGE_STREAM}/latest",
                            self.request("PUT", f"{NO_FORGE_STREAM}/latest", token=REPORT["token"]), BODY)

    def test_unreadable_state_refuses_writes_and_not_reads(self):
        for target in ("gitops_workspace.get_managed_repo_keys", "gitops_workspace.gitops_repo_key_text"):
            with self.subTest(target=target), mock.patch(target, side_effect=RuntimeError("unreadable")):
                credential_proxy._managed_repository_cache = None
                for principal, token in ((self.profile, REPORT["token"]), (self.shell, None)):
                    self.authenticator.principal = principal
                    for path in (f"{STREAM}/latest", f"{NO_FORGE_STREAM}/latest"):
                        self.assert_refused(self.request("PUT", path, token=token), status=503,
                                            code="GITOPS_REPO_UNAVAILABLE")
                self.authenticator.principal = self.profile
                self.assert_relayed("GET", f"{STREAM}/latest", self.request("GET", f"{STREAM}/latest", token=REPORT["token"]))
                self.store.seen.clear()

    def test_a_path_the_routes_do_not_match(self):
        self.authenticator.principal = self.shell
        for method, path in (
            ("GET", "/v1/audit-reports/"),
            ("GET", f"{STREAM}"),
            ("GET", f"{STREAM}/latest?x=1"),
            ("PUT", f"{STREAM}/runs"),
            ("DELETE", f"{STREAM}/runs/{RUN}"),
            ("PUT", "/v1/audit-reports"),
            ("GET", f"/v1/audit-reports/{AUDIT}/%2e%2e/latest"),
            ("PUT", f"/v1/audit-reports/{AUDIT}/acme%2F..%2Fother/latest"),
        ):
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path), status=404)


class ListenerFaultTest(RoutesTestCase):
    def test_a_listener_that_is_down(self):
        self.store.close()
        CredentialProxyHandler.report_store = dataclasses.replace(CredentialProxyHandler.report_store, timeout=2.0)
        status, answer = self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
        self.assertEqual(502, status, answer)
        self.assertEqual("AUDIT_REPORT_STORE_FAILED", answer["code"])

    def test_a_listener_that_refuses_the_broker_is_not_the_callers_401(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.store.answer = (status, b'{"error":"the token is not the broker"}')
                self.records.clear()
                got, answer = self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
                self.assertEqual(502, got, answer)
                self.assertNotIn("token is not the broker", json.dumps(answer))
                # The log names the broker's credential, so nobody chases the caller.
                self.assertTrue(any("refused the broker's own store credential" in r for r in self.records), self.records)

    def test_an_answer_over_the_cap(self):
        self.store.answer = (200, b"x" * (credential_proxy.AUDIT_REPORT_MAX_BYTES + 1))
        status, answer = self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
        self.assertEqual(502, status)
        self.assertNotIn("raw", answer)

    def test_a_listener_that_stalls(self):
        stalled = socket.socket()
        stalled.bind(("127.0.0.1", 0))
        stalled.listen(1)
        self.addCleanup(stalled.close)
        CredentialProxyHandler.report_store = credential_proxy.ReportStoreRelay(
            "http", "127.0.0.1", stalled.getsockname()[1], str(self.token_file), timeout=0.5,
        )
        status, answer = self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
        self.assertEqual(504, status, answer)
        self.assertEqual("AUDIT_REPORT_STORE_TIMEOUT", answer["code"])

    def test_a_listener_that_drips_hits_the_whole_relays_deadline(self):
        # Each byte inside the per-read timeout, the whole well past the deadline.
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        self.addCleanup(listening.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        def drip():
            try:
                conn, _ = listening.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                try:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n")
                    while not stop.is_set():
                        conn.sendall(b" ")
                        stop.wait(0.1)
                except OSError:
                    return

        threading.Thread(target=drip, daemon=True).start()
        CredentialProxyHandler.report_store = credential_proxy.ReportStoreRelay(
            "http", "127.0.0.1", listening.getsockname()[1], str(self.token_file), timeout=5.0, deadline=1.0,
        )
        started = time.monotonic()
        status, answer = self.request("GET", f"{STREAM}/latest", token=REPORT["token"])
        self.assertEqual(504, status, answer)
        self.assertEqual("AUDIT_REPORT_STORE_TIMEOUT", answer["code"])
        self.assertLess(time.monotonic() - started, 4.0)

    def test_the_default_deadline(self):
        self.assertEqual(15.0, credential_proxy.ReportStoreRelay("http", "h", 1, "t").deadline)


class ShellIdentityTest(RoutesTestCase):
    """The planner's condition: `shell` is the sandbox's identity, never a profile worker's."""

    def test_a_profile_credential_is_never_the_shell(self):
        # With a trigger: the shell could write this audit; the credential can't.
        path = f"/v1/audit-reports/{OTHER_AUDIT}/{GITOPS_KEY}/latest"
        self.assert_refused(self.request("PUT", path, token=REPORT["token"]), credential_proxy.RULE_REPORT_TRIGGER_STREAM)
        # Without one: the shell writes; a profile caller with a triggerless credential doesn't.
        self.set_gate("read")
        self.assert_refused(self.request("PUT", f"{STREAM}/latest", token=VALID["token"]), credential_proxy.RULE_REPORT_NO_TRIGGER)
        # Outside the allowlist the shell still reads; a profile caller doesn't.
        CredentialProxyHandler.report_profiles = frozenset()
        self.assert_refused(self.request("GET", f"{STREAM}/latest", token=VALID["token"]), credential_proxy.RULE_REPORT_PROFILE)

    def test_a_profile_caller_with_no_credential_is_not_defaulted_to_the_shell(self):
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path), credential_proxy.RULE_REPORT_NO_TASK)

    def test_no_authentication_at_all_is_401(self):
        CredentialProxyHandler.authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"kubeagents-credential-proxy": credential_proxy.CALLER_ROLE_SHELL},
            allowed_callers=frozenset({SHELL_SA}),
            api_host="127.0.0.1", api_port="1", ca_file="/nonexistent", token_file="/nonexistent",
        )
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path, token=REPORT["token"]), status=401)

    def test_the_authenticator_never_confers_shell_on_a_profile_worker(self):
        # build_authenticator's audience map: a profile worker's ServiceAccount
        # may present only the profile audience, so the shell audience fails.
        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"shell-aud": credential_proxy.CALLER_ROLE_SHELL, "profile-aud": credential_proxy.CALLER_ROLE_PROFILE},
            allowed_callers=frozenset({SHELL_SA, PROFILE_SA}),
            api_host="127.0.0.1", api_port="1", ca_file="", token_file="/nonexistent", cache_seconds=0.0,
            profile_identities={PROFILE_SA: task_credential.ProfileIdentity("auditor", "unattended")},
        )

        def review(username, audience):
            return {"status": {"authenticated": True, "audiences": [audience],
                               "user": {"username": username, "uid": "u", "groups": []}}}

        with self.assertRaisesRegex(credential_proxy.AuthenticationError, "only the profile audience"):
            authenticator._principal_from(review(PROFILE_SA, "shell-aud"))
        self.assertEqual(credential_proxy.CALLER_ROLE_PROFILE,
                         authenticator._principal_from(review(PROFILE_SA, "profile-aud")).role)
        self.assertEqual(credential_proxy.CALLER_ROLE_SHELL, authenticator._principal_from(review(SHELL_SA, "shell-aud")).role)


class A2AReportsTest(RoutesTestCase):
    """The A2A gateway's report reader (#5618): one finished report, on the publish key, and nothing else."""

    def setUp(self):
        super().setUp()
        self.a2a = credential_proxy.Principal(
            workload="system:serviceaccount:kubeagents-system:platformagent-gateway", uid="gw-uid",
            role=credential_proxy.CALLER_ROLE_A2A_REPORTS,
        )
        self.authenticator.principal = self.a2a

    def test_the_publish_word_is_relayed_as_the_publish_key(self):
        # The listener sees the resolved key, never the word the gateway sent.
        for audit in (AUDIT, OTHER_AUDIT, "any-audit-at-all"):
            for suffix in ("latest", f"runs/{RUN}"):
                path = f"/v1/audit-reports/{audit}/publish/{suffix}"
                with self.subTest(path=path):
                    self.assert_relayed("GET", f"/v1/audit-reports/{audit}/{GITOPS_KEY}/{suffix}", self.request("GET", path))

    def test_no_forge_is_the_key_when_the_install_publishes_to_none(self):
        self.set_gitops([], None)
        self.assert_relayed("GET", f"{NO_FORGE_STREAM}/latest", self.request("GET", f"/v1/audit-reports/{AUDIT}/publish/latest"))
        self.assert_relayed("GET", f"{NO_FORGE_STREAM}/runs/{RUN}",
                            self.request("GET", f"/v1/audit-reports/{AUDIT}/publish/runs/{RUN}"))

    def test_a_real_key_is_refused_the_publish_key_included(self):
        for key in (GITOPS_KEY, "acme%2Fterraform", "no_forge%2Flocal"):
            for suffix in ("latest", f"runs/{RUN}"):
                path = f"/v1/audit-reports/{AUDIT}/{key}/{suffix}"
                with self.subTest(path=path):
                    self.assert_refused(self.request("GET", path), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY)
        self.set_gitops([], None)
        self.assert_refused(self.request("GET", f"{NO_FORGE_STREAM}/latest"), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY)

    def test_an_unregistered_key_or_another_spelling(self):
        for key in ("other%2Frepo", "acme%2Fremoved", "ACME%2Fgitops", "acme%2fgitops", "github.com%2Facme%252Fgitops",
                    "PUBLISH", "Publish", "publish%2Fx", "%70ublish", "publish.", "publis"):
            for suffix in ("latest", f"runs/{RUN}"):
                path = f"/v1/audit-reports/{AUDIT}/{key}/{suffix}"
                with self.subTest(path=path):
                    self.assert_refused(self.request("GET", path), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY)

    def test_the_list_and_the_run_index(self):
        for path in ("/v1/audit-reports", f"{STREAM}/runs", f"{NO_FORGE_STREAM}/runs", f"/v1/audit-reports/{AUDIT}/publish/runs"):
            with self.subTest(path=path):
                self.assert_refused(self.request("GET", path), credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE)

    def test_writes_and_deletes(self):
        self.set_gitops([], None)
        for stream in (STREAM, NO_FORGE_STREAM, f"/v1/audit-reports/{AUDIT}/publish"):
            for method, path in (("PUT", f"{stream}/runs/{RUN}"), ("PUT", f"{stream}/latest"), ("DELETE", f"{stream}/latest")):
                with self.subTest(method=method, path=path):
                    self.assert_refused(self.request(method, path), credential_proxy.RULE_REPORT_A2A_REPORTS_WRITE)

    def test_unreadable_state_refuses_the_relays_reads(self):
        for target in ("gitops_workspace.get_managed_repo_keys", "gitops_workspace.gitops_repo_key_text"):
            with self.subTest(target=target), mock.patch(target, side_effect=RuntimeError("unreadable")):
                credential_proxy._managed_repository_cache = None
                self.assert_refused(self.request("GET", f"/v1/audit-reports/{AUDIT}/publish/latest"),
                                    status=503, code="GITOPS_REPO_UNAVAILABLE")

    def test_a_refused_read_reads_no_state(self):
        with mock.patch.object(credential_proxy, "report_publish_store_key", side_effect=AssertionError("read")) as publish:
            self.assert_refused(self.request("GET", f"{STREAM}/latest"), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY)
            self.assert_refused(self.request("GET", f"/v1/audit-reports/{AUDIT}/publish/runs"),
                                credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE)
        publish.assert_not_called()

    def test_the_shell_and_a_profile_sending_publish_are_not_resolved(self):
        # The word is the a2a-reports reader's alone: another role's goes upstream
        # as sent, where it names no stream, and the publish key is never read for it.
        CredentialProxyHandler.report_profiles = frozenset({"auditor"})
        for principal, token in ((self.shell, None), (self.profile, REPORT["token"])):
            self.authenticator.principal = principal
            for suffix in ("latest", f"runs/{RUN}"):
                path = f"/v1/audit-reports/{AUDIT}/publish/{suffix}"
                with self.subTest(role=principal.role, path=path), mock.patch.object(
                    credential_proxy, "report_publish_store_key", side_effect=AssertionError("resolved")
                ):
                    self.assert_relayed("GET", path, self.request("GET", path, token=token))
        # A profile write to it is the repository-key refusal, as any unregistered key.
        self.authenticator.principal = self.profile
        self.store.seen.clear()
        self.assert_refused(self.request("PUT", f"/v1/audit-reports/{AUDIT}/publish/latest", token=REPORT["token"]),
                            credential_proxy.RULE_REPORT_REPOSITORY_KEY)

    def test_a_task_credential_changes_nothing(self):
        # The relay's role is its audience's; a task credential confers no profile reads.
        self.assert_refused(self.request("GET", f"{STREAM}/runs", token=REPORT["token"]), credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE)
        self.assert_refused(self.request("PUT", f"{STREAM}/latest", token=REPORT["token"]), credential_proxy.RULE_REPORT_A2A_REPORTS_WRITE)
        self.assert_refused(self.request("GET", f"/v1/audit-reports/{AUDIT}/acme%2Fterraform/latest", token=REPORT["token"]),
                            credential_proxy.RULE_REPORT_A2A_REPORTS_KEY)

    def test_the_a2a_chat_role_reaches_no_store_route(self):
        self.authenticator.principal = credential_proxy.Principal(
            workload=self.a2a.workload, uid="gw-uid", role=credential_proxy.CALLER_ROLE_A2A_CHAT)
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path), code="CALLER_ROLE_FORBIDDEN")

    def test_every_other_route_refuses_the_role(self):
        # ROUTE_ROLES names the role on the store's entry alone.
        reached = [prefix for prefix, roles in credential_proxy.ROUTE_ROLES
                   if credential_proxy.CALLER_ROLE_A2A_REPORTS in roles]
        self.assertEqual([credential_proxy.AUDIT_REPORTS_PREFIX], reached)
        for method, path in (("POST", "/v1/exec"), ("POST", "/v1/chat/api"), ("GET", "/v1/chat/a2a/events"),
                             ("GET", credential_proxy.API_RELAY_PREFIX + "compute.googleapis.com/x"),
                             ("POST", "/v1/vcs/issue-view"), ("POST", "/v1/forge/refresh"),
                             ("POST", credential_proxy.mcp_policy.MCP_RELAY_PREFIX + "developer_knowledge")):
            with self.subTest(method=method, path=path):
                status, answer = self.request(method, path, body=b"{}" if method == "POST" else None)
                self.assertEqual(403, status, answer)
                self.assertEqual("CALLER_ROLE_FORBIDDEN", answer.get("code"), answer)
        self.assertEqual([], self.store.seen)

    def env(self, **extra):
        env = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": f"{SHELL_SA},{self.a2a.workload},{PROFILE_SA}",
            "CREDENTIAL_PROXY_AUDIENCE": "shell-aud",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "chat-aud",
            "CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE": "a2a-chat-aud",
            credential_proxy.A2A_REPORTS_AUDIENCE_ENV: "reports-aud",
            credential_proxy.A2A_REPORTS_CALLERS_ENV: self.a2a.workload,
            credential_proxy.PROFILE_AUDIENCE_ENV: "profile-aud",
            credential_proxy.PROFILE_IDENTITIES_ENV: json.dumps({PROFILE_SA: {"profile": "auditor", "tier": "unattended"}}),
            "KUBERNETES_SERVICE_HOST": "127.0.0.1",
        }
        env.update(extra)
        return env

    @staticmethod
    def review(username, *audiences):
        return {"status": {"authenticated": True, "audiences": list(audiences),
                           "user": {"username": username, "uid": "u", "groups": []}}}

    def test_only_its_audience_and_the_gateways_account_yield_the_role(self):
        with mock.patch.dict(os.environ, self.env()):
            authenticator = credential_proxy.build_authenticator()
        self.assertEqual(["reports-aud"], [a for a, r in authenticator.audience_roles.items()
                                           if r == credential_proxy.CALLER_ROLE_A2A_REPORTS])
        self.assertEqual(frozenset({self.a2a.workload}), authenticator.a2a_reports_callers)
        principal_from = authenticator._principal_from
        self.assertEqual(credential_proxy.CALLER_ROLE_A2A_REPORTS, principal_from(self.review(self.a2a.workload, "reports-aud")).role)
        # The gateway keeps its Chat token's role, and no other audience.
        self.assertEqual(credential_proxy.CALLER_ROLE_A2A_CHAT, principal_from(self.review(self.a2a.workload, "a2a-chat-aud")).role)
        for audience in ("shell-aud", "chat-aud", "profile-aud"):
            with self.subTest(gateway_audience=audience), self.assertRaises(credential_proxy.AuthenticationError):
                principal_from(self.review(self.a2a.workload, audience))
        # Any other account presenting the reports audience is refused.
        for username in (SHELL_SA, PROFILE_SA):
            with self.subTest(other=username), self.assertRaises(credential_proxy.AuthenticationError):
                principal_from(self.review(username, "reports-aud"))
        # And no other audience yields the role for anyone.
        for username, audience in ((SHELL_SA, "shell-aud"), (SHELL_SA, "chat-aud"), (PROFILE_SA, "profile-aud")):
            with self.subTest(username=username, audience=audience):
                self.assertNotEqual(credential_proxy.CALLER_ROLE_A2A_REPORTS, principal_from(self.review(username, audience)).role)
        with self.assertRaises(credential_proxy.AuthenticationError):
            principal_from(self.review(self.a2a.workload, "reports-aud", "a2a-chat-aud"))

    def test_no_callers_named_confers_the_role_on_nobody(self):
        with mock.patch.dict(os.environ, self.env(**{credential_proxy.A2A_REPORTS_CALLERS_ENV: ""})):
            authenticator = credential_proxy.build_authenticator()
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator._principal_from(self.review(self.a2a.workload, "reports-aud"))

    def test_an_audience_that_repeats_another_confers_nothing(self):
        for repeated in ("shell-aud", "chat-aud", "a2a-chat-aud"):
            with self.subTest(repeated=repeated), mock.patch.dict(
                os.environ, self.env(**{credential_proxy.A2A_REPORTS_AUDIENCE_ENV: repeated})
            ):
                roles = credential_proxy.build_authenticator().audience_roles
                self.assertNotIn(credential_proxy.CALLER_ROLE_A2A_REPORTS, roles.values())

    def test_a_broker_without_the_audience_still_binds_the_gateway(self):
        # The exec tier: callers rendered, audience dropped. The gateway's
        # account still reaches no shell or chat role there.
        env = self.env()
        del env[credential_proxy.A2A_REPORTS_AUDIENCE_ENV]
        with mock.patch.dict(os.environ, env, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertNotIn(credential_proxy.CALLER_ROLE_A2A_REPORTS, authenticator.audience_roles.values())
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator._principal_from(self.review(self.a2a.workload, "shell-aud"))
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator._principal_from(self.review(self.a2a.workload, "reports-aud"))


class MethodsTest(RoutesTestCase):
    def test_put_and_delete_elsewhere_keep_their_old_answer(self):
        # Before authentication, as before these methods existed: an
        # authenticator that refuses everyone changes nothing here.
        CredentialProxyHandler.authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"a": credential_proxy.CALLER_ROLE_SHELL}, allowed_callers=frozenset({SHELL_SA}),
            api_host="127.0.0.1", api_port="1", ca_file="/nonexistent", token_file="/nonexistent",
        )
        for method in ("PUT", "DELETE"):
            for path in ("/v1/exec", "/v1/chat/api", "/healthz", "/v1/audit-reportsx", "/", "/v1/vcs/issue-view"):
                with self.subTest(method=method, path=path):
                    connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
                    self.addCleanup(connection.close)
                    connection.request(method, path, body=b"{}" if method == "PUT" else None)
                    response = connection.getresponse()
                    self.assertEqual(501, response.status)
                    self.assertIn(f"Unsupported method ('{method}')", response.read().decode())
        self.assertEqual([], self.store.seen)

    def test_a_path_that_only_starts_like_the_store_keeps_its_old_answer(self):
        # Not one of the store's routes: no role demanded, and the unknown
        # route's 404, as before this change, for any role.
        self.assertEqual((), credential_proxy.required_roles("/v1/audit-reportsx"))
        self.assertEqual(credential_proxy.LABEL_OTHER, credential_proxy._endpoint_label("/v1/audit-reportsx"))
        for role in (credential_proxy.CALLER_ROLE_CHAT, credential_proxy.CALLER_ROLE_SHELL):
            self.authenticator.principal = credential_proxy.Principal(workload="w", uid="u", role=role)
            for path in ("/v1/audit-reportsx", "/v1/audit-reports-latest", "/v1/audit-reports?x=1"):
                with self.subTest(role=role, path=path):
                    status, answer = self.request("GET", path)
                    self.assertEqual(404, status, answer)
                    self.assertEqual({"status": "not_found"}, answer)
        self.assertEqual("/v1/audit-reports", credential_proxy._endpoint_label(f"{STREAM}/latest"))
        self.assertEqual([], self.store.seen)

    def test_patch_on_a_store_path_is_still_501(self):
        status, _ = self.request("PATCH", f"{STREAM}/latest", token=REPORT["token"], body=BODY)
        self.assertEqual(501, status)
        self.assertEqual([], self.store.seen)

    def test_post_on_a_store_path_is_not_relayed(self):
        status, answer = self.request("POST", f"{STREAM}/latest", token=REPORT["token"], body=BODY)
        self.assertEqual(credential_proxy.RULE_REPORT_CREDENTIAL_ROUTE, answer.get("rule"), answer)
        self.authenticator.principal = self.shell
        status, _ = self.request("POST", f"{STREAM}/latest", body=BODY)
        self.assertEqual(404, status)
        self.assertEqual([], self.store.seen)

    def test_an_exec_pod_does_not_serve_the_store(self):
        CredentialProxyHandler.broker_role = credential_proxy.EXEC_ROLE
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assertFalse(credential_proxy.exec_pod_serves(method, path))
                self.assert_refused(self.request(method, path, token=REPORT["token"]), status=404)

    def test_a_draining_broker_relays_nothing(self):
        CredentialProxyHandler.draining.set()
        for method, path in READS + WRITES:
            with self.subTest(method=method, path=path):
                self.assert_refused(self.request(method, path, token=REPORT["token"]), status=503)


class WriteDecisionTest(unittest.TestCase):
    """report_write_permitted and report_read_permitted, without HTTP or crypto."""

    ALLOW = frozenset({"fleet-audit", "platform"})

    def trigger(self, stream=AUDIT, origin="builtin", name=None):
        return task_credential.TaskTrigger(name=name or f"schedule/{stream}", origin=origin, stream=stream)

    def write(self, role=credential_proxy.CALLER_ROLE_PROFILE, profile="fleet-audit", trigger="default",
              audit_id=AUDIT, key=GITOPS_KEY, gitops_key=GITOPS_KEY, allowlist=None):
        return credential_proxy.report_write_permitted(
            role, profile, self.trigger() if trigger == "default" else trigger, audit_id, key, gitops_key,
            self.ALLOW if allowlist is None else allowlist,
        )

    def test_the_matching_schedule_is_permitted(self):
        self.assertIsNone(self.write())
        self.assertIsNone(self.write(profile="platform"))
        self.assertIsNone(self.write(key="no_forge%2Flocal", gitops_key=None))

    def test_each_refusal(self):
        cases = {
            "another schedule": (self.write(trigger=self.trigger(OTHER_AUDIT)), credential_proxy.RULE_REPORT_TRIGGER_STREAM),
            "no trigger": (self.write(trigger=None), credential_proxy.RULE_REPORT_NO_TRIGGER),
            "user origin": (self.write(trigger=self.trigger(origin="user")), credential_proxy.RULE_REPORT_TRIGGER_ORIGIN),
            "no stream": (self.write(trigger=task_credential.TaskTrigger("schedule/x", "builtin", "")),
                          credential_proxy.RULE_REPORT_TRIGGER_STREAM),
            "name not the schedule": (self.write(trigger=self.trigger(name="stall-watch")), credential_proxy.RULE_REPORT_TRIGGER_STREAM),
            "profile outside": (self.write(profile="auditor"), credential_proxy.RULE_REPORT_PROFILE),
            "empty allowlist": (self.write(allowlist=frozenset()), credential_proxy.RULE_REPORT_PROFILE),
            "no task": (self.write(profile=None), credential_proxy.RULE_REPORT_NO_TASK),
            "profile not a str": (self.write(profile=["fleet-audit"]), credential_proxy.RULE_REPORT_PROFILE),
            "wrong key": (self.write(key="acme%2Fterraform"), credential_proxy.RULE_REPORT_REPOSITORY_KEY),
            "no_forge beside a gitops repo": (self.write(key="no_forge%2Flocal"), credential_proxy.RULE_REPORT_REPOSITORY_KEY),
            "gitops key with none named": (self.write(gitops_key=None), credential_proxy.RULE_REPORT_REPOSITORY_KEY),
            "empty gitops key": (self.write(key="", gitops_key=""), credential_proxy.RULE_REPORT_REPOSITORY_KEY),
            "other role": (self.write(role=credential_proxy.CALLER_ROLE_SESSION), credential_proxy.RULE_REPORT_ROLE),
            "no role": (self.write(role=""), credential_proxy.RULE_REPORT_ROLE),
            "role not a str": (self.write(role=None), credential_proxy.RULE_REPORT_ROLE),
        }
        for name, (got, rule) in cases.items():
            with self.subTest(name):
                self.assertEqual(rule, got)

    def test_the_shell(self):
        shell = credential_proxy.CALLER_ROLE_SHELL
        registered = frozenset({GITOPS_KEY, "acme%2Fterraform"})
        self.assertIsNone(self.write(role=shell, profile=None, trigger=None, audit_id="any-audit"))
        self.assertEqual(credential_proxy.RULE_REPORT_REPOSITORY_KEY,
                         self.write(role=shell, profile=None, trigger=None, key="acme%2Fterraform"))
        write = credential_proxy.report_write_permitted
        self.assertIsNone(write(shell, None, None, AUDIT, "acme%2Fterraform", GITOPS_KEY, self.ALLOW, registered))
        for key in ("other%2Frepo", "ACME%2Fterraform", "no_forge%2Flocal"):
            with self.subTest(key=key):
                self.assertEqual(credential_proxy.RULE_REPORT_REPOSITORY_KEY,
                                 write(shell, None, None, AUDIT, key, GITOPS_KEY, self.ALLOW, registered | {"no_forge%2Flocal"}))
        # A profile task gets the publish target alone, whatever is registered.
        self.assertEqual(credential_proxy.RULE_REPORT_REPOSITORY_KEY, write(
            credential_proxy.CALLER_ROLE_PROFILE, "fleet-audit", self.trigger(), AUDIT, "acme%2Fterraform", GITOPS_KEY,
            self.ALLOW, registered))
        self.assertIsNone(credential_proxy.report_read_permitted(shell, None, frozenset()))

    def test_reads(self):
        read = credential_proxy.report_read_permitted
        profile = credential_proxy.CALLER_ROLE_PROFILE
        self.assertIsNone(read(profile, "fleet-audit", self.ALLOW))
        self.assertEqual(credential_proxy.RULE_REPORT_PROFILE, read(profile, "auditor", self.ALLOW))
        self.assertEqual(credential_proxy.RULE_REPORT_NO_TASK, read(profile, None, self.ALLOW))
        for role in ("", None, credential_proxy.CALLER_ROLE_CHAT, credential_proxy.CALLER_ROLE_MCP):
            with self.subTest(role=role):
                self.assertEqual(credential_proxy.RULE_REPORT_ROLE, read(role, "fleet-audit", self.ALLOW))
                self.assertEqual(credential_proxy.RULE_REPORT_ROLE,
                                 read(role, None, self.ALLOW, credential_proxy.REPORT_ROUTE_LATEST, "publish"))

    def test_the_a2a_reports_role(self):
        a2a = credential_proxy.CALLER_ROLE_A2A_REPORTS
        read = credential_proxy.report_read_permitted
        latest, run = credential_proxy.REPORT_ROUTE_LATEST, credential_proxy.REPORT_ROUTE_RUN
        word = credential_proxy.REPORT_A2A_PUBLISH_KEY
        self.assertEqual("publish", word)
        self.assertIsNone(read(a2a, None, frozenset(), latest, word))
        self.assertIsNone(read(a2a, None, frozenset(), run, word))
        cases = {
            "the list": (read(a2a, None, frozenset(), credential_proxy.REPORT_ROUTE_LIST, None),
                         credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE),
            "the run index": (read(a2a, None, frozenset(), credential_proxy.REPORT_ROUTE_RUNS, word),
                              credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE),
            "no route": (read(a2a, None, frozenset(), None, word), credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE),
            "the publish key itself": (read(a2a, None, frozenset(), latest, GITOPS_KEY), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
            "no_forge itself": (read(a2a, None, frozenset(), run, "no_forge%2Flocal"), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
            "another key": (read(a2a, None, frozenset(), latest, "acme%2Fterraform"), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
            "no key": (read(a2a, None, frozenset(), latest), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
            "empty key": (read(a2a, None, frozenset(), latest, ""), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
            "key not a str": (read(a2a, None, frozenset(), latest, [word]), credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
            "an allowlisted profile is no help": (read(a2a, "fleet-audit", self.ALLOW, latest, "acme%2Fterraform"),
                                                  credential_proxy.RULE_REPORT_A2A_REPORTS_KEY),
        }
        for name, (got, rule) in cases.items():
            with self.subTest(name):
                self.assertEqual(rule, got)
        self.assertNotIn(credential_proxy.RULE_REPORT_A2A_REPORTS_KEY,
                         (credential_proxy.RULE_REPORT_REPOSITORY_KEY, credential_proxy.RULE_REPORT_A2A_REPORTS_ROUTE,
                          credential_proxy.RULE_REPORT_A2A_REPORTS_WRITE))
        write = credential_proxy.report_write_permitted
        for key, publish in ((GITOPS_KEY, GITOPS_KEY), ("no_forge%2Flocal", None)):
            with self.subTest(key=key):
                self.assertEqual(credential_proxy.RULE_REPORT_A2A_REPORTS_WRITE,
                                 write(a2a, "fleet-audit", self.trigger(), AUDIT, key, publish, self.ALLOW, frozenset({key})))
        # The a2a-chat role reads and writes nothing here.
        self.assertEqual(credential_proxy.RULE_REPORT_ROLE,
                         read(credential_proxy.CALLER_ROLE_A2A_CHAT, None, frozenset(), latest, word))
        self.assertEqual(credential_proxy.RULE_REPORT_ROLE, write(
            credential_proxy.CALLER_ROLE_A2A_CHAT, None, None, AUDIT, GITOPS_KEY, GITOPS_KEY, self.ALLOW))

    def test_the_route_kinds(self):
        kind = credential_proxy.report_route_kind
        self.assertEqual(credential_proxy.REPORT_ROUTE_LIST, kind("/v1/audit-reports"))
        self.assertEqual(credential_proxy.REPORT_ROUTE_LATEST, kind(f"{STREAM}/latest"))
        self.assertEqual(credential_proxy.REPORT_ROUTE_RUNS, kind(f"{STREAM}/runs"))
        self.assertEqual(credential_proxy.REPORT_ROUTE_RUN, kind(f"{STREAM}/runs/{RUN}"))
        for path in (STREAM, f"{STREAM}/latest/x", f"{STREAM}/other", f"{STREAM}/latest/{RUN}", "/v1/audit-reports/x"):
            with self.subTest(path=path):
                self.assertIsNone(kind(path))


def _load_audit_report():
    spec = importlib.util.spec_from_file_location("audit_report_for_routes_test", AUDIT_REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoreKeyTest(unittest.TestCase):
    """report_store_key against audit_report.reports_dir_for, the file store's writer."""

    @classmethod
    def setUpClass(cls):
        cls.audit_report = _load_audit_report()

    def test_the_mirrored_constants_are_equal(self):
        ar = self.audit_report
        self.assertEqual(ar.STORE_PATH_SEPARATOR, credential_proxy.REPORT_STORE_PATH_SEPARATOR)
        self.assertEqual(ar.NO_FORGE_REPO, credential_proxy.REPORT_NO_FORGE_REPO)
        self.assertEqual(ar.REPORT_REPO_SEGMENT_RE.pattern, credential_proxy.REPORT_REPO_SEGMENT_RE.pattern)
        self.assertEqual(task_credential.TRIGGER_ORIGINS, frozenset({credential_proxy.REPORT_TRIGGER_ORIGIN}))
        self.assertEqual(task_credential.TRIGGER_SCHEDULE_SOURCE, credential_proxy.REPORT_TRIGGER_SCHEDULE_SOURCE)
        self.assertEqual("no_forge%2Flocal", credential_proxy.REPORT_NO_FORGE_STORE_KEY)

    def test_the_cap_is_the_listeners(self):
        tree = ast.parse(LISTENER_PATH.read_text(encoding="utf-8"))
        (value,) = [
            node.value for node in tree.body
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "REPORT_MAX_BYTES" for t in node.targets)
        ]
        # The listener spells it `1 << 20`: read the shift, nothing evaluated.
        self.assertIsInstance(value, ast.BinOp)
        self.assertIsInstance(value.op, ast.LShift)
        cap = ast.literal_eval(value.left) << ast.literal_eval(value.right)
        self.assertEqual(cap, credential_proxy.AUDIT_REPORT_MAX_BYTES)

    def test_the_key_names_the_file_stores_directory(self):
        root = Path("/reports-root")
        with mock.patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": str(root)}):
            for repo in ("acme/gitops", "Acme/GitOps", "github.com/acme/gitops", "GitHub.com/Acme/GitOps",
                         "gitlab.com/group/sub/repo", "ghe.example.com/Acme/GitOps", "git.example.org/a/b/c/d",
                         "no_forge/local"):
                with self.subTest(repo=repo):
                    relative = self.audit_report.reports_dir_for("x", repo).relative_to(root / "x")
                    expected = "/".join(relative.parts)
                    self.assertEqual(urllib.parse.quote(expected, safe=""), credential_proxy.report_store_key(repo))

    def test_spellings(self):
        key = credential_proxy.report_store_key
        self.assertEqual("acme%2Fgitops", key("Acme/GitOps"))
        self.assertEqual("acme%2Fgitops", key("github.com/acme/gitops"))
        self.assertEqual("ghe.example.com%2Facme%252Fgitops", key("ghe.example.com/acme/gitops"))
        self.assertEqual("gitlab.com%2Fgroup%252Fsub%252Frepo", key("gitlab.com/Group/Sub/Repo"))

    def test_names_the_file_store_refuses(self):
        for repo in ("acme", "acme/../x", "a/b/c", "acme/", "/acme", "acme/gi tops", "", None, 3):
            with self.subTest(repo=repo):
                with self.assertRaises((ValueError, AttributeError, TypeError)):
                    self.audit_report.reports_dir_for("x", repo) if isinstance(repo, str) else repo.lower()
                self.assertIsNone(credential_proxy.report_store_key(repo))

    def test_every_key_holds_an_escaped_slash_so_none_is_the_publish_word(self):
        # The a2a-reports reader's REPORT_A2A_PUBLISH_KEY holds no %2F, so it
        # can never be a real stream's key: every key report_store_key yields does.
        import random
        rng = random.Random(5619)
        alphabet = "abcXYZ019_.-/ %:@"
        repos = ["publish", "publish/", "/publish", "github.com/publish", "a/publish", "publish/a", "no_forge/local",
                 "acme/gitops", "gitlab.com/a/b/c", "x.y/a/b", "..", "./a", "a/."]
        repos += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(20000)]
        yielded = 0
        for repo in repos:
            got = credential_proxy.report_store_key(repo)
            if got is None:
                continue
            yielded += 1
            with self.subTest(repo=repo):
                self.assertIn("%2F", got)
                self.assertNotEqual(credential_proxy.REPORT_A2A_PUBLISH_KEY, got)
        self.assertGreater(yielded, 100)
        self.assertNotIn("%2F", credential_proxy.REPORT_A2A_PUBLISH_KEY.upper())


class PublishKeyTest(unittest.TestCase):
    """report_publish_store_key: audit_report's publish target for the install, as a store key."""

    TERRAFORM = {"type": "github", "url": "https://github.com/acme/terraform"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {
            "GITOPS_STATE_PATH": str(self.tmp / "managed_repos"),
            "GITOPS_WORKSPACE": str(self.tmp / "workspaces"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, credential_proxy, "_managed_repository_cache", None)

    def state(self, managed, gitops=None, github_host=None):
        credential_proxy._managed_repository_cache = None
        (self.tmp / "managed_repos").write_text(json.dumps(managed), encoding="utf-8")
        for name, value in (("gitops_repo", gitops), ("github_host", github_host)):
            (self.tmp / name).unlink(missing_ok=True)
            if value is not None:
                (self.tmp / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    CASES = {
        "two, gitops set": ([GITOPS, TERRAFORM], GITOPS, None, GITOPS_KEY),
        "two, no gitops": ([GITOPS, TERRAFORM], None, None, None),
        "two, a stale gitops key": ([GITOPS, TERRAFORM], {"type": "github", "url": "https://github.com/acme/removed"}, None, None),
        "one, no gitops role": ([TERRAFORM], None, None, "acme%2Fterraform"),
        "one, gitops naming another": ([TERRAFORM], GITOPS, None, "acme%2Fterraform"),
        "none": ([], None, None, None),
        "GHES, mixed case": ([{"type": "github", "url": "https://ghe.example.com/Acme/GitOps"}], None, "ghe.example.com",
                             "ghe.example.com%2Facme%252Fgitops"),
        "GitLab, two, gitops set": ([{"type": "gitlab", "url": "https://gitlab.example.com/platform/infra/gitops"}, TERRAFORM],
                                    {"type": "gitlab", "url": "https://gitlab.example.com/platform/infra/gitops"}, None,
                                    "gitlab.example.com%2Fplatform%252Finfra%252Fgitops"),
    }

    def test_each_case(self):
        for name, (managed, gitops, host, want) in self.CASES.items():
            with self.subTest(name):
                self.state(managed, gitops, host)
                self.assertEqual(want, credential_proxy.report_publish_store_key())

    def test_each_case_matches_audit_reports_resolution(self):
        # audit_report's own answer for the same state: resolve_repo_or_no_forge,
        # with NoGitOpsRepo (several repositories, no usable key) read as the
        # no-repository-chosen envelope under no_forge, as finish writes it.
        audit_report = _load_audit_report()
        root = self.tmp / "reports"
        for name, (managed, gitops, host, _) in self.CASES.items():
            with self.subTest(name), mock.patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": str(root)}), \
                    mock.patch("subprocess.run", side_effect=AssertionError("no kubectl")):
                self.state(managed, gitops, host)
                try:
                    repo = audit_report.resolve_repo_or_no_forge(AUDIT)
                except audit_report.NoGitOpsRepo:
                    repo = audit_report.NO_FORGE_REPO
                relative = audit_report.reports_dir_for(AUDIT, repo).relative_to(root / AUDIT)
                expected = urllib.parse.quote("/".join(relative.parts), safe="")
                got = credential_proxy.report_publish_store_key() or credential_proxy.REPORT_NO_FORGE_STORE_KEY
                self.assertEqual(expected, got)

    def test_the_registered_keys_are_every_managed_repository(self):
        self.state([GITOPS, self.TERRAFORM])
        self.assertEqual(
            frozenset({GITOPS_KEY, "acme%2Fterraform"}),
            credential_proxy.report_install_store_keys()[1],
        )

    def test_unreadable_state_raises(self):
        self.state([GITOPS, self.TERRAFORM], GITOPS)
        for target in ("gitops_workspace.get_managed_repo_keys", "gitops_workspace.gitops_repo_key_text"):
            with self.subTest(target=target), mock.patch(target, side_effect=RuntimeError("unreadable")):
                credential_proxy._managed_repository_cache = None
                with self.assertRaises(credential_proxy.ReportStoreUnavailable):
                    credential_proxy.report_publish_store_key()

    def test_the_pure_rule(self):
        rule = credential_proxy.report_publish_store_key_for
        self.assertIsNone(rule([], "acme/gitops"))
        self.assertEqual("acme%2Fterraform", rule(["github:github.com/acme/terraform"], None))
        self.assertIsNone(rule(["github:github.com/acme/gitops", "github:github.com/acme/terraform"], None))
        self.assertEqual(GITOPS_KEY, rule(["github:github.com/acme/gitops", "github:github.com/acme/terraform"], "acme/gitops"))


class ConfigurationTest(unittest.TestCase):
    def test_the_store_from_env(self):
        url, token = credential_proxy.AUDIT_REPORT_STORE_URL_ENV, credential_proxy.AUDIT_REPORT_STORE_TOKEN_FILE_ENV
        good = {url: "http://platformagent-report-store.kubeagents-system.svc:8700", token: "/var/run/t/token"}
        with mock.patch.dict(os.environ, good):
            relay = credential_proxy.report_store_from_env(credential_proxy.ROLE_COMBINED)
            self.assertEqual(("http", "platformagent-report-store.kubeagents-system.svc", 8700), (relay.scheme, relay.host, relay.port))
            self.assertIsNone(credential_proxy.report_store_from_env(credential_proxy.EXEC_ROLE))
        for env in ({}, {url: good[url]}, {token: good[token]}, {**good, url: "http://h:8700/prefix"},
                    {**good, url: "ftp://h"}, {**good, url: "http://u:p@h:1"}, {**good, url: "http://h:99999"}):
            with self.subTest(env=env):
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertIsNone(credential_proxy.report_store_from_env(credential_proxy.ROLE_COMBINED))

    def test_the_profiles_from_env(self):
        name = credential_proxy.REPORT_PROFILES_ENV
        for raw, want in (("fleet-audit,platform", {"fleet-audit", "platform"}), ("", set()), (" fleet-audit , ", {"fleet-audit"}),
                          ("Fleet-Audit,platform", {"platform"}), ("*", set()), ("a b", set())):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {name: raw}):
                    self.assertEqual(frozenset(want), credential_proxy.report_profiles_from_env())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(frozenset(), credential_proxy.report_profiles_from_env())

    def test_the_route_table_admits_the_shell_profile_and_a2a_reports_alone(self):
        want = (credential_proxy.CALLER_ROLE_SHELL, credential_proxy.CALLER_ROLE_PROFILE, credential_proxy.CALLER_ROLE_A2A_REPORTS)
        self.assertEqual(want, credential_proxy.required_roles("/v1/audit-reports"))
        self.assertEqual(want, credential_proxy.required_roles(f"{STREAM}/latest"))


if __name__ == "__main__":
    unittest.main()
