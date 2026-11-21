"""Tests for audit_report_store.py, the broker-only audit-report store listener (#5619)."""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import audit_report_store as store  # noqa: E402

AUDIT_REPORT_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
)

ISSUER = "https://container.googleapis.com/v1/projects/p/locations/l/clusters/c"
BROKER = "system:serviceaccount:agents:platform-agent"
POD_PREFIX = "platform-agent-credential-proxy-"
BROKER_POD = POD_PREFIX + "7d9f8c6b5-x2k4q"
STREAM = "/v1/audit-reports/compliance-audit/acme%2Fgitops"
STAMP_1 = "20261101T010203.000001Z"
STAMP_2 = "20261102T010203.000001Z"
# Why a test skips when the search of `nested_past_the_parser` finds no document.
NO_DOCUMENT_REFUSED = "the JSON parser of this Python parses %d levels, the deepest that this test tries"


def nested_past_the_parser(levels, most):
    """A JSON array nested `levels` deep or deeper that `json.loads` refuses for its
    depth on this run, or None when one of `most` levels still parses.

    The parser's depth limit is the interpreter's: about 10,000 levels on Python
    3.12 and 3.13, and on 3.14 as many as the stack holds (more than 100,000
    with a 16 MB stack). So no fixed depth is past it on every run.
    """
    depth = levels
    while True:
        text = "[" * depth + "]" * depth
        try:
            json.loads(text)
        except RecursionError:
            return text
        if depth >= most:
            return None
        depth = min(2 * depth, most)


def nested_past_the_parser_on_a_thread(levels, most):
    """`nested_past_the_parser`, searched on a new thread: the server parses on
    one, and on Python 3.14 the limit is that thread's stack."""
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(nested_past_the_parser, levels, most).result()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


_RSA_KEYS: dict[int, rsa.RSAPrivateKey] = {}


class RSAKey:
    """An RSA signing key. Generated once per slot, because 2048-bit keys are slow."""

    def __init__(self, kid: str, slot: int = 0) -> None:
        self.kid = kid
        if slot not in _RSA_KEYS:
            _RSA_KEYS[slot] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private = _RSA_KEYS[slot]

    def jwk(self) -> dict:
        numbers = self.private.public_key().public_numbers()
        return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": self.kid, "n": _int_b64(numbers.n), "e": _int_b64(numbers.e)}

    def sign(self, data: bytes) -> bytes:
        return self.private.sign(data, padding.PKCS1v15(), hashes.SHA256())


class ECKey:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = ec.generate_private_key(ec.SECP256R1())

    def jwk(self) -> dict:
        numbers = self.private.public_key().public_numbers()
        return {
            "kty": "EC", "alg": "ES256", "use": "sig", "crv": "P-256", "kid": self.kid,
            "x": _b64(numbers.x.to_bytes(32, "big")), "y": _b64(numbers.y.to_bytes(32, "big")),
        }

    def sign(self, data: bytes) -> bytes:
        r, s = decode_dss_signature(self.private.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def mint(key, alg: str = "RS256", kid: str | None = None, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": [store.AUDIENCE],
        "sub": BROKER,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "kubernetes.io": {
            "namespace": "agents",
            "pod": {"name": BROKER_POD, "uid": "pod-uid"},
            "serviceaccount": {"name": "platform-agent", "uid": "sa-uid"},
        },
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    header = {"alg": alg, "kid": kid or key.kid, "typ": "JWT"}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing_input}.{_b64(key.sign(signing_input.encode()))}"


class FakeAPIServer:
    """The API server's discovery document and JWKS, served on loopback."""

    def __init__(self) -> None:
        self.keys: list[dict] = []
        self.fetches = 0
        self.authorizations: list[str | None] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.authorizations.append(self.headers.get("Authorization"))
                if self.path == store.DISCOVERY_PATH:
                    body = {"issuer": ISSUER, "jwks_uri": "https://x/openid/v1/jwks"}
                elif self.path == store.JWKS_PATH:
                    outer.fetches += 1
                    body = {"keys": list(outer.keys)}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.server.server_close()


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Resolved: the store refuses a symlink anywhere on its root's path, and
        # macOS puts the temp dir under /var, a symlink to /private/var.
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.root = self.tmp / "reports"
        self.sa_dir = self.tmp / "sa"
        self.sa_dir.mkdir()
        (self.sa_dir / "token").write_text("pod-own-token\n")
        self.api = FakeAPIServer()
        self.key = RSAKey("k1")
        self.api.keys = [self.key.jwk()]
        env = {store.ALLOWED_SUBJECT_ENV: BROKER, store.ALLOWED_POD_PREFIX_ENV: POD_PREFIX}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.api.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = self.make_client()
        self.token = mint(self.key)

    def fetch(self):
        return store.fetch_cluster_keys(self.api.url, str(self.sa_dir))

    def make_client(self, root: Path | None = None, **verifier_args) -> TestClient:
        verifier = store.TokenVerifier(fetch=self.fetch, **verifier_args)
        return TestClient(store.create_app(verifier, root=str(root or self.root)))

    def call(
        self, method: str, path: str, body: bytes | None = None, token: str | None = "default",
        writer: str | None = "default",
    ):
        headers = {}
        if token == "default":
            token = self.token
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        # Every PUT carries the broker's writer stamp unless a test says otherwise.
        if writer == "default":
            writer = store.REPORT_WRITER_SHELL if method == "PUT" else None
        if writer is not None:
            headers[store.REPORT_WRITER_HEADER] = writer
        return self.client.request(method, path, content=body, headers=headers)

    def put_run(self, stamp: str, doc: dict | None = None, stream: str = STREAM):
        return self.call("PUT", f"{stream}/runs/{stamp}", json.dumps(doc or {"finished_at": stamp}).encode())


class RouteTests(StoreTestCase):
    def test_every_route_happy_path(self) -> None:
        self.assertEqual(200, self.call("GET", "/v1/audit-reports").status_code)
        self.assertEqual([], self.call("GET", "/v1/audit-reports").json())

        body = json.dumps({"finished_at": "2026-11-01T01:02:03+00:00", "n": 1}).encode()
        self.assertEqual(204, self.call("PUT", f"{STREAM}/runs/{STAMP_1}", body).status_code)
        written = self.root / "compliance-audit" / "acme" / "gitops" / "runs" / f"{STAMP_1}.json"
        self.assertEqual(body, written.read_bytes())
        self.assertEqual(0o644, written.stat().st_mode & 0o777)
        self.assertEqual(0o755, written.parent.stat().st_mode & 0o777)
        self.assertEqual(0o755, written.parent.parent.stat().st_mode & 0o777)
        self.assertEqual([], [p.name for p in written.parent.iterdir() if p.suffix == ".tmp"])

        self.assertEqual(204, self.put_run(STAMP_2).status_code)
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", body).status_code)
        latest = self.call("GET", f"{STREAM}/latest")
        self.assertEqual(200, latest.status_code)
        self.assertEqual(body, latest.content)
        self.assertEqual([STAMP_2, STAMP_1], self.call("GET", f"{STREAM}/runs").json())
        self.assertEqual(body, self.call("GET", f"{STREAM}/runs/{STAMP_1}").content)
        self.assertEqual(
            [{"audit_id": "compliance-audit", "key": "acme%2Fgitops", "newest_run": STAMP_2,
              "latest_finished_at": "2026-11-01T01:02:03+00:00"}],
            self.call("GET", "/v1/audit-reports").json(),
        )

        self.assertEqual(204, self.call("DELETE", f"{STREAM}/latest").status_code)
        self.assertEqual(404, self.call("GET", f"{STREAM}/latest").status_code)
        # Idempotent.
        self.assertEqual(204, self.call("DELETE", f"{STREAM}/latest").status_code)

    def test_missing_entries_are_404(self) -> None:
        self.assertEqual(404, self.call("GET", f"{STREAM}/latest").status_code)
        self.assertEqual(404, self.call("GET", f"{STREAM}/runs/{STAMP_1}").status_code)
        self.assertEqual([], self.call("GET", f"{STREAM}/runs").json())

    def test_reads_what_the_file_store_wrote(self) -> None:
        # The tree audit_report.py writes, read back as written.
        directory = self.root / "compliance-audit" / "acme" / "gitops"
        (directory / "runs").mkdir(parents=True)
        (directory / "runs" / f"{STAMP_1}.json").write_text('{"a": 1}\n')
        (directory / "latest.json").write_text('{"a": 1}\n')
        self.assertEqual(b'{"a": 1}\n', self.call("GET", f"{STREAM}/latest").content)
        # Mixed case is the same store, as reports_dir_for lowercases.
        self.assertEqual(b'{"a": 1}\n', self.call("GET", "/v1/audit-reports/compliance-audit/Acme%2FGitOps/latest").content)

    def test_ring_prune_keeps_the_newest_fourteen(self) -> None:
        stamps = [f"202611{day:02d}T000000.000000Z" for day in range(1, 16)]
        for stamp in stamps:
            self.assertEqual(204, self.put_run(stamp).status_code)
        self.assertEqual(list(reversed(stamps[1:])), self.call("GET", f"{STREAM}/runs").json())
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        self.assertEqual(14, len(list(runs.glob("*.json"))))
        # Each envelope goes with its writer sidecar.
        self.assertEqual(sorted(f"{stamp}.writer" for stamp in stamps[1:]), sorted(p.name for p in runs.glob("*.writer")))

    def test_a_failed_prune_does_not_fail_the_write(self) -> None:
        for day in range(1, 15):
            self.put_run(f"202611{day:02d}T000000.000000Z")
        with mock.patch.object(store.os, "unlink", side_effect=PermissionError("read-only")):
            self.assertEqual(204, self.put_run("20261120T000000.000000Z").status_code)

    def test_latest_must_equal_a_ring_entry(self) -> None:
        self.put_run(STAMP_1, {"run": 1})
        self.assertEqual(409, self.call("PUT", f"{STREAM}/latest", json.dumps({"run": 2}).encode()).status_code)
        # Byte-identical, not JSON-equal.
        self.assertEqual(409, self.call("PUT", f"{STREAM}/latest", b'{"run":1}').status_code)
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", json.dumps({"run": 1}).encode()).status_code)
        # No ring at all.
        other = "/v1/audit-reports/compliance-audit/acme%2Fother"
        self.assertEqual(409, self.call("PUT", f"{other}/latest", b"{}").status_code)

    def test_a_non_github_repository_key(self) -> None:
        stream = "/v1/audit-reports/compliance-audit/gitlab.com%2FGroup%252Fsub%252Frepo"
        self.assertEqual(204, self.put_run(STAMP_1, stream=stream).status_code)
        directory = self.root / "compliance-audit" / "gitlab.com" / "group%2Fsub%2Frepo"
        self.assertTrue((directory / "runs" / f"{STAMP_1}.json").is_file())
        listed = self.call("GET", "/v1/audit-reports").json()
        self.assertEqual("gitlab.com%2Fgroup%252Fsub%252Frepo", listed[0]["key"])
        self.assertEqual(200, self.call("GET", f"/v1/audit-reports/compliance-audit/{listed[0]['key']}/runs/{STAMP_1}").status_code)

    def test_github_with_its_host_keeps_the_bare_slug_directory(self) -> None:
        self.put_run(STAMP_1, stream="/v1/audit-reports/compliance-audit/github.com%2Facme%252Fgitops")
        self.assertTrue((self.root / "compliance-audit" / "acme" / "gitops" / "runs" / f"{STAMP_1}.json").is_file())

    def test_store_key_mapping_matches_the_file_store(self) -> None:
        # audit_report.reports_dir_for is the writer's spelling; the listener
        # must land on the same directory for each name it accepts.
        import importlib.util

        spec = importlib.util.spec_from_file_location("audit_report_for_store_test", AUDIT_REPORT_PATH)
        audit_report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit_report)
        for name in ("REPORT_HISTORY", "REPORT_STAMP_FORMAT", "REPORT_RUNS_DIR", "REPORT_LATEST_NAME",
                     "REPORT_DIR_MODE", "REPORT_FILE_MODE", "STORE_PATH_SEPARATOR"):
            self.assertEqual(getattr(audit_report, name), getattr(store, name), name)
        self.assertEqual(audit_report.REPORT_REPO_SEGMENT_RE.pattern, store.REPORT_REPO_SEGMENT_RE.pattern)
        for repo in ("acme/gitops", "Acme/GitOps", "github.com/acme/gitops", "gitlab.com/group/sub/repo",
                     "git.example.org/a/b"):
            expected = audit_report.reports_dir_for("x", repo)
            owner, name = expected.parts[-2], expected.parts[-1]
            key = store.store_key_for_dirs(owner, name)
            self.assertEqual((owner, name), store.store_dirs_for_key(key), repo)


class ValidationTests(StoreTestCase):
    def assertRefused(self, method: str, path: str, status: int = 400, body: bytes | None = None) -> None:
        response = self.call(method, path, body if body is not None else (b"{}" if method == "PUT" else None))
        self.assertEqual(status, response.status_code, (method, path, response.text))

    def test_bad_audit_ids(self) -> None:
        for audit in ("Compliance", "-a", "a-", "a_b", "a" * 64, "%2e%2e"):
            with self.subTest(audit=audit):
                self.assertRefused("GET", f"/v1/audit-reports/{audit}/acme%2Fgitops/latest")

    def test_bad_store_keys(self) -> None:
        for key in (
            "%2e%2e",                  # `..`
            "%2e%2e%2Fetc",            # `../etc`
            "acme%2F%2e%2e",           # `acme/..`
            "acme%2F.",
            "%252e%252e%2Fx",          # `..` escaped twice
            "acme",                    # one part
            "acme%2Fa%2Fb",            # three parts, no host
            "acme%252Fgitops",         # an escaped `/` where a part belongs
            "acme%2Fgit%00ops",        # NUL
            "acme%2Fgit%0aops",        # a newline
            "%2Fetc%2Fpasswd",         # absolute
            "%2Fetc",
            "acme%2F",                 # empty part
            "gitlab.com%2Fa%252F%252e%252e",  # `..` below a host
            "gitlab.com%2Fa%252F",     # empty part below a host
            "acme%2Fgit%20ops",        # not the GitHub charset
            "acme%2Fgit%ffops",        # not UTF-8
        ):
            with self.subTest(key=key):
                self.assertRefused("GET", f"/v1/audit-reports/a/{key}/latest")
                self.assertRefused("PUT", f"/v1/audit-reports/a/{key}/runs/{STAMP_1}")
        self.assertFalse(self.root.exists() and any(self.root.rglob("*.json")))

    def test_an_unescaped_slash_is_another_route(self) -> None:
        self.assertRefused("GET", "/v1/audit-reports/a/acme/gitops/latest", 404)
        self.assertRefused("GET", "/v1/audit-reports/a/acme%2Fgitops/latest/", 404)
        self.assertRefused("GET", "/v1/audit-reports/a/acme%2Fgitops/other", 404)
        self.assertRefused("GET", "/v1/other", 404)
        self.assertRefused("GET", "/docs", 404)
        self.assertRefused("GET", "/openapi.json", 404)

    def test_bad_stamps(self) -> None:
        for stamp in ("latest", "20261301T000000.000000Z", "20261101T000000Z", "20261101T000000.000000Z.json",
                      "%2e%2e", "20261101T000000.00000Z"):
            with self.subTest(stamp=stamp):
                self.assertRefused("PUT", f"{STREAM}/runs/{stamp}")

    def assertNothingOutside(self, outside: Path) -> None:
        self.assertEqual([], sorted(p.name for p in outside.iterdir() if p.name != "secret.json"))

    def test_a_symlinked_path_component_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "compliance-audit").mkdir(parents=True)
        os.symlink(outside, self.root / "compliance-audit" / "acme")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 409)
        self.assertRefused("GET", f"{STREAM}/runs", 409)
        self.assertRefused("GET", f"{STREAM}/latest", 409)
        self.assertRefused("DELETE", f"{STREAM}/latest", 409)
        self.assertNothingOutside(outside)
        # And the listing does not walk into it.
        self.assertEqual([], self.call("GET", "/v1/audit-reports").json())

    def test_a_symlinked_audit_directory_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        self.root.mkdir()
        os.symlink(outside, self.root / "compliance-audit")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 409)
        self.assertNothingOutside(outside)

    def test_a_symlinked_latest_json_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        secret = outside / "secret.json"
        secret.write_text('{"secret": 1}')
        good = self.root / "compliance-audit" / "acme" / "gitops"
        (good / "runs").mkdir(parents=True)
        os.symlink(secret, good / "latest.json")
        self.assertRefused("GET", f"{STREAM}/latest", 409)
        # A write replaces the link itself and never writes through it.
        self.put_run(STAMP_1, {"run": 1})
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", json.dumps({"run": 1}).encode()).status_code)
        self.assertEqual('{"secret": 1}', secret.read_text())
        self.assertFalse((good / "latest.json").is_symlink())
        # A DELETE of a link removes the link, not its target.
        os.unlink(good / "latest.json")
        os.symlink(secret, good / "latest.json")
        self.assertEqual(204, self.call("DELETE", f"{STREAM}/latest").status_code)
        self.assertTrue(secret.exists())

    def test_a_symlinked_runs_directory_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / f"{STAMP_1}.json").write_text("{}")
        good = self.root / "compliance-audit" / "acme" / "gitops"
        good.mkdir(parents=True)
        os.symlink(outside, good / "runs")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_2}", 409)
        self.assertRefused("GET", f"{STREAM}/runs/{STAMP_1}", 409)
        self.assertRefused("GET", f"{STREAM}/runs", 409)
        self.assertRefused("PUT", f"{STREAM}/latest", 409, b"{}")
        self.assertEqual([f"{STAMP_1}.json"], [p.name for p in outside.iterdir()])

    def test_a_symlinked_ring_entry_is_not_a_ring_entry(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "x.json").write_text("{}")
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        runs.mkdir(parents=True)
        os.symlink(outside / "x.json", runs / f"{STAMP_1}.json")
        self.assertEqual([], self.call("GET", f"{STREAM}/runs").json())
        self.assertRefused("GET", f"{STREAM}/runs/{STAMP_1}", 409)
        # latest may not copy a linked entry either.
        self.assertRefused("PUT", f"{STREAM}/latest", 409, b"{}")

    def test_a_symlink_on_the_roots_own_path_is_refused(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        os.symlink(real, link)
        self.client = self.make_client(root=link / "reports")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 409)
        self.assertEqual([], list(real.iterdir()))

    def test_bodies(self) -> None:
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 400, b"not json")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 400, b"[1, 2]")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 400, b"\xff\xfe")
        oversize = json.dumps({"x": "a" * store.REPORT_MAX_BYTES}).encode()
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}", 413, oversize)
        exactly = b'{"x": "' + b"a" * (store.REPORT_MAX_BYTES - 10) + b'"}'
        self.assertEqual(store.REPORT_MAX_BYTES - 1, len(exactly))
        self.assertEqual(204, self.call("PUT", f"{STREAM}/runs/{STAMP_1}", exactly).status_code)

    def test_methods(self) -> None:
        self.assertRefused("POST", f"{STREAM}/runs/{STAMP_1}", 405)
        self.assertRefused("PUT", f"{STREAM}/runs", 405)
        self.assertRefused("DELETE", f"{STREAM}/runs/{STAMP_1}", 405)
        self.assertRefused("PUT", "/v1/audit-reports", 405)


class AuthTests(StoreTestCase):
    def assertStatus(self, status: int, token: str | None) -> None:
        response = self.call("GET", f"{STREAM}/runs", token=token)
        self.assertEqual(status, response.status_code, response.text)

    def test_a_valid_token_passes_and_jwks_is_read_with_the_pods_token(self) -> None:
        self.assertStatus(200, self.token)
        self.assertIn("Bearer pod-own-token", self.api.authorizations)

    def test_es256(self) -> None:
        ec_key = ECKey("e1")
        self.api.keys = [self.key.jwk(), ec_key.jwk()]
        self.assertStatus(200, mint(ec_key, alg="ES256"))

    def test_no_token(self) -> None:
        self.assertStatus(401, None)
        response = self.client.get(f"{STREAM}/runs", headers={"Authorization": "Basic abc"})
        self.assertEqual(401, response.status_code)

    def test_refusals_come_before_validation(self) -> None:
        # An unauthenticated caller learns nothing about the path rules.
        self.assertEqual(401, self.call("GET", "/v1/audit-reports/a/%2e%2e/latest", token=None).status_code)
        self.assertEqual(401, self.call("GET", "/v1/other", token=None).status_code)

    def test_bad_signature(self) -> None:
        other = RSAKey("k1", slot=1)  # same kid, another key
        self.assertStatus(401, mint(other))
        head, payload, sig = self.token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        claims["sub"] = "system:serviceaccount:agents:someone"
        forged = f"{head}.{_b64(json.dumps(claims).encode())}.{sig}"
        self.assertStatus(401, forged)
        self.assertStatus(401, "a.b.c")
        self.assertStatus(401, "not-a-jwt")

    def test_alg_none_and_hs256(self) -> None:
        for alg in ("none", "HS256"):
            header = _b64(json.dumps({"alg": alg, "kid": "k1"}).encode())
            payload = _b64(json.dumps({"iss": ISSUER, "aud": store.AUDIENCE, "sub": BROKER, "exp": time.time() + 60}).encode())
            self.assertStatus(401, f"{header}.{payload}.")

    def test_wrong_audience(self) -> None:
        self.assertStatus(401, mint(self.key, aud=["https://kubernetes.default.svc"]))
        self.assertStatus(401, mint(self.key, aud="kubeagents-credential-proxy"))
        self.assertStatus(200, mint(self.key, aud=store.AUDIENCE))

    def test_wrong_issuer(self) -> None:
        self.assertStatus(401, mint(self.key, iss="https://evil.example"))
        self.assertStatus(401, mint(self.key, iss=None))

    def test_expired_and_not_yet_valid(self) -> None:
        now = int(time.time())
        self.assertStatus(401, mint(self.key, exp=now - 31))
        self.assertStatus(200, mint(self.key, exp=now - 20))  # inside the skew
        self.assertStatus(401, mint(self.key, exp=None))
        self.assertStatus(401, mint(self.key, nbf=now + 60))
        self.assertStatus(401, mint(self.key, exp=True))

    def test_wrong_subject_is_403(self) -> None:
        self.assertStatus(403, mint(self.key, sub="system:serviceaccount:agents:platform-agent-gateway-agent"))
        self.assertStatus(403, mint(self.key, sub=None))
        with mock.patch.dict(os.environ, {store.ALLOWED_SUBJECT_ENV: ""}):
            self.assertStatus(403, self.token)

    def test_unknown_kid_refetches_once(self) -> None:
        self.assertStatus(200, self.token)
        self.assertEqual(1, self.api.fetches)
        rotated = RSAKey("k2", slot=2)
        self.api.keys = [self.key.jwk(), rotated.jwk()]
        self.assertStatus(200, mint(rotated))
        self.assertEqual(2, self.api.fetches)
        # An unknown kid inside the refetch interval does not refetch again.
        self.assertStatus(401, mint(RSAKey("k3")))
        self.assertEqual(2, self.api.fetches)
        # A known one is served from the cache.
        self.assertStatus(200, self.token)
        self.assertEqual(2, self.api.fetches)

    def test_jwks_down_is_503(self) -> None:
        self.api.stop()
        self.assertStatus(503, self.token)

    def test_jwks_down_after_the_ttl_is_503_not_the_stale_keys(self) -> None:
        clock = [1000.0]
        client = self.make_client(monotonic=lambda: clock[0])
        headers = {"Authorization": f"Bearer {self.token}"}
        self.assertEqual(200, client.get(f"{STREAM}/runs", headers=headers).status_code)
        self.api.stop()
        clock[0] += store.JWKS_TTL_SECONDS + 1
        self.assertEqual(503, client.get(f"{STREAM}/runs", headers=headers).status_code)

    def test_the_token_is_never_logged(self) -> None:
        with self.assertLogs("audit_report_store", level="INFO") as logs:
            self.assertStatus(200, self.token)
            self.assertStatus(403, mint(self.key, sub="system:serviceaccount:x:y"))
            self.assertStatus(401, mint(RSAKey("k1", slot=1)))
        text = "\n".join(logs.output)
        self.assertNotIn(self.token.split(".")[2], text)
        self.assertNotIn(self.token.split(".")[1], text)
        self.assertIn(f"subject={BROKER}", text)
        self.assertIn("-> 200", text)


class HardeningTests(StoreTestCase):
    """Review follow-ups: no environment trust anchors, FIFOs, bounds, locks, the port."""

    def test_the_module_reads_no_root_api_server_or_sa_dir_from_the_environment(self) -> None:
        import ast

        tree = ast.parse(Path(store.__file__).read_text())
        read = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("get", "getenv"):
                target = ast.unparse(node.func.value) if hasattr(node.func, "value") else ""
                if target in ("os.environ", "os") and node.args:
                    read.add(ast.unparse(node.args[0]))
            if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                read.add(ast.unparse(node.slice))
        self.assertEqual({"ALLOWED_SUBJECT_ENV", "ALLOWED_POD_PREFIX_ENV"}, read)

    def test_setting_the_old_overrides_changes_nothing(self) -> None:
        # A stream under the overridden root: a listener that honoured
        # FLEET_AUDIT_REPORTS_DIR would list it.
        elsewhere = self.tmp / "elsewhere"
        planted = elsewhere / "compliance-audit" / "acme" / "gitops" / "runs"
        planted.mkdir(parents=True)
        (planted / f"{STAMP_1}.json").write_text("{}")
        before = sorted(str(p) for p in elsewhere.rglob("*"))
        hostile = {
            "FLEET_AUDIT_REPORTS_DIR": str(elsewhere),
            "AUDIT_REPORT_STORE_API_SERVER": "http://127.0.0.1:1",
            "AUDIT_REPORT_STORE_SA_DIR": str(elsewhere),
        }
        with mock.patch.dict(os.environ, hostile):
            client = TestClient(store.create_app(store.TokenVerifier(fetch=self.fetch)))
            self.assertEqual(store.DEFAULT_REPORTS_DIR, client.app.state.root)
            # Read-only, so the default root is never created on this machine.
            listed = client.request("GET", "/v1/audit-reports", headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(200, listed.status_code)
            self.assertNotIn(
                {"audit_id": "compliance-audit", "key": "acme%2Fgitops", "newest_run": STAMP_1,
                 "latest_finished_at": None},
                listed.json(),
            )
            # The default fetch goes to the fixed API server with the fixed SA directory.
            seen = []

            def recording(url, token, ca_file):
                seen.append((url, ca_file))
                raise store.JWKSUnavailable("not dialled in a test")

            with mock.patch.object(store, "_http_get_json", side_effect=recording):
                with self.assertRaises(store.JWKSUnavailable):
                    store.fetch_cluster_keys()
            self.assertEqual(
                [(store.DEFAULT_API_SERVER + store.DISCOVERY_PATH, str(Path(store.DEFAULT_SA_DIR) / "ca.crt"))], seen
            )
        self.assertEqual(before, sorted(str(p) for p in elsewhere.rglob("*")))

    def test_fifos_are_refused_without_blocking(self) -> None:
        directory = self.root / "compliance-audit" / "acme" / "gitops"
        runs = directory / "runs"
        runs.mkdir(parents=True)
        os.mkfifo(directory / "latest.json")
        os.mkfifo(runs / f"{STAMP_1}.json")
        self.assertEqual(409, self.call("GET", f"{STREAM}/latest").status_code)
        self.assertEqual(409, self.call("GET", f"{STREAM}/runs/{STAMP_1}").status_code)
        # The ring scan in PUT latest neither blocks on it nor counts it.
        self.assertEqual(409, self.call("PUT", f"{STREAM}/latest", b"{}").status_code)
        self.assertEqual([], self.call("GET", f"{STREAM}/runs").json())
        # And a direct read of a FIFO name returns at once.
        fd = os.open(runs, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(store.Refused):
                store._read_at(fd, f"{STAMP_1}.json")
        finally:
            os.close(fd)

    def test_an_oversized_stored_entry_is_refused(self) -> None:
        directory = self.root / "compliance-audit" / "acme" / "gitops"
        directory.mkdir(parents=True)
        (directory / "latest.json").write_bytes(b" " * (store.REPORT_MAX_BYTES + 1))
        self.assertEqual(409, self.call("GET", f"{STREAM}/latest").status_code)

    def test_an_oversized_older_ring_entry_does_not_refuse_put_latest(self) -> None:
        # The file store's writer has no size cap, so the ring can hold one.
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        runs.mkdir(parents=True)
        (runs / f"{STAMP_1}.json").write_bytes(b" " * (store.REPORT_MAX_BYTES + 1))
        (runs / f"{STAMP_2}.json").write_text('{"run": 2}')
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", b'{"run": 2}').status_code)
        # And a body matching nothing is still a 409, not an error from the big entry.
        self.assertEqual(409, self.call("PUT", f"{STREAM}/latest", b'{"run": 3}').status_code)

    def test_a_directory_or_fifo_in_the_ring_is_a_non_match(self) -> None:
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        runs.mkdir(parents=True)
        (runs / f"{STAMP_1}.json").write_text('{"run": 1}')
        real = store._ring_names
        # Turn the newest entry into a directory after the ring is listed, as a
        # racing writer could.
        def racing(runs_fd):
            names = real(runs_fd)
            (runs / f"{STAMP_2}.json").mkdir()
            return names + [f"{STAMP_2}.json"]

        with mock.patch.object(store, "_ring_names", side_effect=racing):
            self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", b'{"run": 1}').status_code)

    def test_put_latest_reads_the_ring_newest_first_and_stops_at_the_match(self) -> None:
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        runs.mkdir(parents=True)
        stamps = [f"202611{day:02d}T000000.000000Z" for day in range(1, store.REPORT_HISTORY + 1)]
        for i, stamp in enumerate(stamps):
            (runs / f"{stamp}.json").write_text(json.dumps({"run": i}))
        reads = []
        real = store._read_at

        def counting(dir_fd, name):
            # The ring entries read; the matched entry's writer sidecar is read after.
            if name.endswith(".json"):
                reads.append(name)
            return real(dir_fd, name)

        with mock.patch.object(store, "_read_at", side_effect=counting):
            newest = json.dumps({"run": len(stamps) - 1}).encode()
            self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", newest).status_code)
            self.assertEqual([f"{stamps[-1]}.json"], reads)
            reads.clear()
            third = json.dumps({"run": len(stamps) - 3}).encode()
            self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", third).status_code)
            self.assertEqual([f"{stamp}.json" for stamp in reversed(stamps[-3:])], reads)

    def test_a_directory_at_latest_is_refused_without_leaking_an_fd(self) -> None:
        directory = self.root / "compliance-audit" / "acme" / "gitops"
        (directory / "runs").mkdir(parents=True)
        (directory / "runs" / f"{STAMP_1}.json").write_text("{}")
        (directory / "latest.json").mkdir()
        # Warm up: the first request opens sockets and threads of its own.
        self.call("GET", f"{STREAM}/latest")
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            self.assertEqual(409, self.call("GET", f"{STREAM}/latest").status_code)
            listed = self.call("GET", "/v1/audit-reports")
            self.assertEqual(200, listed.status_code)
            self.assertEqual(STAMP_1, listed.json()[0]["newest_run"])
        self.assertEqual(before, len(os.listdir("/dev/fd")))
        # And directly.
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            before = len(os.listdir("/dev/fd"))
            with self.assertRaises(store.Refused) as caught:
                store._read_at(fd, "latest.json")
            self.assertEqual(409, caught.exception.status)
            self.assertEqual(before, len(os.listdir("/dev/fd")))
        finally:
            os.close(fd)

    def test_latest_compares_only_the_newest_ring_entries(self) -> None:
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        runs.mkdir(parents=True)
        stamps = [f"202611{day:02d}T000000.000000Z" for day in range(1, store.REPORT_HISTORY + 3)]
        for i, stamp in enumerate(stamps):
            (runs / f"{stamp}.json").write_text(json.dumps({"run": i}))
        self.assertEqual(409, self.call("PUT", f"{STREAM}/latest", json.dumps({"run": 0}).encode()).status_code)
        newest = json.dumps({"run": len(stamps) - 1}).encode()
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", newest).status_code)
        self.assertEqual(store.REPORT_HISTORY, len(self.call("GET", f"{STREAM}/runs").json()))

    def test_a_directory_with_too_many_entries_is_refused(self) -> None:
        runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        runs.mkdir(parents=True)
        for i in range(store.MAX_DIR_ENTRIES + 1):
            (runs / f"junk{i}").touch()
        self.assertEqual(409, self.call("GET", f"{STREAM}/runs").status_code)

    def test_writes_hold_the_store_directory_lock(self) -> None:
        calls = []
        real = store.fcntl.flock

        def recording(fd, op):
            calls.append(op)
            return real(fd, op)

        with mock.patch.object(store.fcntl, "flock", side_effect=recording):
            self.put_run(STAMP_1, {"run": 1})
            self.call("PUT", f"{STREAM}/latest", json.dumps({"run": 1}).encode())
        self.assertEqual([store.fcntl.LOCK_EX, store.fcntl.LOCK_UN] * 2, calls)

    def test_a_taken_port_is_a_loud_error(self) -> None:
        import socket

        holder = socket.socket()
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        with self.assertLogs("audit_report_store", level="ERROR") as logs, self.assertRaises(SystemExit):
            store.bind_listener(port)
        self.assertIn(f"port {port}", "\n".join(logs.output))
        self.assertIn("another process", "\n".join(logs.output))


class JWKSFailureHoldTests(unittest.TestCase):
    def test_a_failed_fetch_is_held_then_retried(self) -> None:
        clock = [100.0]
        attempts = []

        def failing():
            attempts.append(clock[0])
            raise store.JWKSUnavailable("down")

        verifier = store.TokenVerifier(fetch=failing, monotonic=lambda: clock[0])
        token = mint(RSAKey("k1"))
        for _ in range(3):
            with self.assertRaises(store.AuthRefused) as caught:
                verifier.verify(f"Bearer {token}")
            self.assertEqual(503, caught.exception.status)
        self.assertEqual(1, len(attempts))
        clock[0] += store.JWKS_FAILURE_HOLD_SECONDS + 1
        with self.assertRaises(store.AuthRefused):
            verifier.verify(f"Bearer {token}")
        self.assertEqual(2, len(attempts))

    def test_the_lock_is_not_held_across_the_fetch(self) -> None:
        verifier = store.TokenVerifier(fetch=lambda: (verifier._lock.locked() and self.fail("fetch under the lock")) or (ISSUER, []))
        with self.assertRaises(store.AuthRefused):
            verifier.verify(f"Bearer {mint(RSAKey('k1'))}")


class TokenCheckAloneTests(StoreTestCase):
    """With no NetworkPolicy (spec.networkPolicy.enabled false), the token check
    alone has to refuse every caller but the broker's own pod."""

    def test_only_the_brokers_pod_token_passes(self) -> None:
        gateway_sa = "system:serviceaccount:agents:platform-agent-gateway-agent"
        cases = {
            "the pod's own API token": (401, mint(self.key, aud=["https://kubernetes.default.svc"], sub=gateway_sa)),
            "the broker's token for another audience": (401, mint(self.key, aud=["kubeagents-credential-proxy"])),
            "the right audience, another ServiceAccount": (403, mint(self.key, sub=gateway_sa)),
            "the right audience and subject, an exec-tier pod": (403, mint(self.key, **{"kubernetes.io": {
                "pod": {"name": POD_PREFIX + "exec-7d9f8c6b5-x2k4q"}}})),
            "the right audience and subject, another pod": (403, mint(self.key, **{"kubernetes.io": {
                "pod": {"name": "platform-agent-kube-proxy-7d9f8c6b5-x2k4q"}}})),
            "the right audience and subject, no pod claim": (403, mint(self.key, **{"kubernetes.io": None})),
            "no token": (401, None),
        }
        for name, (status, token) in cases.items():
            with self.subTest(name):
                self.assertEqual(status, self.call("GET", f"{STREAM}/runs", token=token).status_code)
        self.assertEqual(200, self.call("GET", f"{STREAM}/runs").status_code)

    def test_no_pod_prefix_configured_refuses_everyone(self) -> None:
        with mock.patch.dict(os.environ, {store.ALLOWED_POD_PREFIX_ENV: ""}):
            self.assertEqual(403, self.call("GET", f"{STREAM}/runs").status_code)


# The exec tier exists only for agent names up to 63 minus this suffix
# (credentialProxyExecTierMaxAgentName in k8s-operator).
EXEC_TIER_SUFFIX = "-credential-proxy-exec-tier"
EXEC_TIER_MAX_AGENT_NAME = 63 - len(EXEC_TIER_SUFFIX)


def generated_pod_name(deployment: str, pod_hash: str, random_part: str = "x2k4q") -> str:
    """What the ReplicaSet controller and SimpleNameGenerator name a Deployment's pod."""
    return (f"{deployment}-{pod_hash}-")[:58] + random_part


class BrokerPodNameTests(unittest.TestCase):
    """The pod-name check against Kubernetes' own naming, at every agent-name length that changes it."""

    LENGTHS = (5, 29, 30, 31, 35, 36, 40, 41, 50)
    HASHES = ("7b9f8c6d5", "68b5fc9d47")

    @staticmethod
    def agent(length: int) -> str:
        return ("agent" + "z" * length)[:length]

    def test_constants(self) -> None:
        self.assertEqual(58, store.POD_GENERATE_NAME_MAX_BASE)
        self.assertEqual(5, store.POD_GENERATE_NAME_RANDOM_LENGTH)
        self.assertEqual((1, 10), (store.POD_TEMPLATE_HASH_MIN_LENGTH, store.POD_TEMPLATE_HASH_MAX_LENGTH))
        self.assertEqual(36, EXEC_TIER_MAX_AGENT_NAME)

    def test_table(self) -> None:
        for length in self.LENGTHS:
            agent = self.agent(length)
            prefix = f"{agent}-credential-proxy-"
            for pod_hash in self.HASHES:
                broker = generated_pod_name(f"{agent}-credential-proxy", pod_hash)
                with self.subTest(length=length, hash=pod_hash, case="broker"):
                    self.assertTrue(store.is_broker_pod_name(prefix, broker), broker)
                if length <= EXEC_TIER_MAX_AGENT_NAME:
                    for deployment in (agent + EXEC_TIER_SUFFIX, f"{agent}-credential-proxy-exec"):
                        exec_pod = generated_pod_name(deployment, pod_hash)
                        with self.subTest(length=length, hash=pod_hash, case="exec", name=exec_pod):
                            self.assertFalse(store.is_broker_pod_name(prefix, exec_pod), exec_pod)
                refused = {
                    "another agent's broker": generated_pod_name(f"{'y' * length}-credential-proxy", pod_hash),
                    "the kube-proxy": generated_pod_name(f"{agent}-kube-proxy", pod_hash),
                    "an extra segment": f"{prefix}{pod_hash}-extra-x2k4q",
                    "uppercase throughout": broker.upper(),
                    "an uppercase random part": broker[:-1] + "Q",
                    "an uppercase hash": f"{prefix}{pod_hash.upper()}-x2k4q",
                    "a random part one short": broker[:-1],
                    "a random part one long": broker + "q",
                    "no hash": f"{prefix}-x2k4q",
                    "no random part": f"{prefix}{pod_hash}-",
                    "the prefix alone": prefix,
                    "a dot in the random part": broker[:-1] + ".",
                }
                if len(prefix) + 12 <= 58:
                    # Only an untruncated name shows an 11-character hash.
                    refused["an 11-character hash"] = generated_pod_name(f"{agent}-credential-proxy", "a" * 11)
                for case, name in refused.items():
                    with self.subTest(length=length, hash=pod_hash, case=case, name=name):
                        self.assertFalse(store.is_broker_pod_name(prefix, name), name)

    def test_the_cut_points(self) -> None:
        # The "-" after a 10-character hash is the 58th character at 29, so
        # from 30 it is cut, and from 41 the prefix itself is cut.
        for length, expect in ((29, "-"), (30, "7"), (41, None)):
            agent = self.agent(length)
            name = generated_pod_name(f"{agent}-credential-proxy", "68b5fc9d47")
            with self.subTest(length=length):
                self.assertEqual(63, len(name))
                if expect is None:
                    self.assertFalse(name.startswith(f"{agent}-credential-proxy-"))
                else:
                    self.assertEqual(expect, name[57])
                self.assertTrue(store.is_broker_pod_name(f"{agent}-credential-proxy-", name))

    def test_any_valid_hash_passes(self) -> None:
        # No hash is pinned: a pod of another ReplicaSet of the same broker
        # Deployment (a stale or newer pod-template hash) passes as any does.
        for length in self.LENGTHS:
            agent = self.agent(length)
            prefix = f"{agent}-credential-proxy-"
            for pod_hash in ("7b9f8c6d5", "5d4c8f9b7", "b", "zzzzzzzzzz"):
                name = generated_pod_name(f"{agent}-credential-proxy", pod_hash, "q7w9z")
                with self.subTest(length=length, hash=pod_hash):
                    self.assertTrue(store.is_broker_pod_name(prefix, name), name)

    def test_the_exec_tier_boundary(self) -> None:
        # 36, the longest agent name with an exec tier: its pod is refused,
        # though truncation leaves the remainder a bare "exec".
        agent = self.agent(EXEC_TIER_MAX_AGENT_NAME)
        prefix = f"{agent}-credential-proxy-"
        exec_pod = generated_pod_name(agent + EXEC_TIER_SUFFIX, "68b5fc9d47")
        self.assertEqual("exec", exec_pod[len(prefix):-5])
        self.assertFalse(store.is_broker_pod_name(prefix, exec_pod))
        self.assertTrue(store.is_broker_pod_name(prefix, generated_pod_name(f"{agent}-credential-proxy", "68b5fc9d47")))
        # 37: the operator renders no exec tier (credentialProxyExecTierMaxAgentName),
        # and the broker's pod passes. An exec-shaped name is cut to "exe",
        # indistinguishable from a broker pod whose hash begins "exe", and
        # passes; recorded here so a change to that is deliberate.
        agent = self.agent(EXEC_TIER_MAX_AGENT_NAME + 1)
        prefix = f"{agent}-credential-proxy-"
        self.assertTrue(store.is_broker_pod_name(prefix, generated_pod_name(f"{agent}-credential-proxy", "68b5fc9d47")))
        exec_shaped = generated_pod_name(agent + EXEC_TIER_SUFFIX, "68b5fc9d47")
        self.assertEqual("exe", exec_shaped[len(prefix):-5])
        self.assertTrue(store.is_broker_pod_name(prefix, exec_shaped))

    def test_no_prefix_refuses(self) -> None:
        self.assertFalse(store.is_broker_pod_name("", BROKER_POD))


class JWKSRequestTests(unittest.TestCase):
    """The key fetch asks for the media type the API server serves the JWKS as."""

    def test_the_fetch_accepts_a_jwk_set(self):
        seen = []

        class Answer:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit=-1):
                return self.body

        def fake_urlopen(request, timeout=None, context=None):
            seen.append((request.full_url, request.get_header("Accept")))
            if request.full_url.endswith("/openid/v1/jwks"):
                if "application/jwk-set+json" not in (request.get_header("Accept") or ""):
                    raise urllib.error.HTTPError(request.full_url, 406, "Not Acceptable", {}, None)
                return Answer(b'{"keys": []}')
            return Answer(b'{"issuer": "https://issuer.example"}')

        with mock.patch.object(store.urllib.request, "urlopen", fake_urlopen):
            issuer, keys = store.fetch_cluster_keys("https://api.example", "/nonexistent")
        self.assertEqual(("https://issuer.example", []), (issuer, keys))
        self.assertIn("application/jwk-set+json", dict(seen)["https://api.example/openid/v1/jwks"])


# The suite's stamps are in November 2026; pin the store's clock past them so the
# future-stamp refusal judges them as written, not against the wall clock.
FIXED_NOW = 1798761600.0  # 2027-01-01T00:00:00Z


def setUpModule():
    setUpModule.original = store._now
    store._now = lambda: FIXED_NOW


def tearDownModule():
    store._now = setUpModule.original


class RoundThreeTests(StoreTestCase):
    """A rotated kid during a failed fetch, overlong store keys, deeply nested JSON."""

    def test_a_rotated_kid_during_a_failed_fetch_is_503_until_a_fetch_succeeds(self) -> None:
        clock = [1000.0]
        rotated = RSAKey("k2", slot=2)
        state = {"keys": [self.key.jwk()], "fail": False, "fetches": 0}

        def fetch():
            state["fetches"] += 1
            if state["fail"]:
                raise store.JWKSUnavailable("down")
            return ISSUER, list(state["keys"])

        verifier = store.TokenVerifier(fetch=fetch, monotonic=lambda: clock[0])
        self.assertEqual(BROKER, verifier.verify(f"Bearer {self.token}"))
        state["fail"] = True
        for step in range(4):
            with self.subTest(step=step), self.assertRaises(store.AuthRefused) as caught:
                verifier.verify(f"Bearer {mint(rotated)}")
            self.assertEqual(503, caught.exception.status)
            # A cached kid still verifies inside the hold.
            self.assertEqual(BROKER, verifier.verify(f"Bearer {self.token}"))
            clock[0] += 2
        # The forced-refresh rate limit holds: one failed fetch in the window.
        self.assertEqual(2, state["fetches"])
        clock[0] += max(store.JWKS_FAILURE_HOLD_SECONDS, store.JWKS_FORCED_REFRESH_SECONDS)
        state["fail"] = False
        state["keys"] = [self.key.jwk(), rotated.jwk()]
        self.assertEqual(BROKER, verifier.verify(f"Bearer {mint(rotated)}"))
        self.assertEqual(3, state["fetches"])

    def test_overlong_store_keys_are_400_and_create_nothing(self) -> None:
        name_parts = "%252F".join(["abcdefghij"] * 30)
        streams = {
            "owner": f"/v1/audit-reports/compliance-audit/{'a' * 256}%2Fgitops",
            "name": f"/v1/audit-reports/compliance-audit/gitlab.com%2F{name_parts}",
            "github name": f"/v1/audit-reports/compliance-audit/acme%2F{'g' * 256}",
        }
        for case, stream in streams.items():
            with self.subTest(case):
                self.assertEqual(400, self.put_run(STAMP_1, stream=stream).status_code)
                self.assertEqual(400, self.call("GET", f"{stream}/latest").status_code)
        self.assertFalse(self.root.exists() and any(self.root.iterdir()))
        # 255 bytes still passes.
        ok = f"/v1/audit-reports/compliance-audit/{'a' * 255}%2Fgitops"
        self.assertEqual(204, self.put_run(STAMP_1, stream=ok).status_code)

    def test_enametoolong_on_the_path_is_400(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for create in (False, True):
                with self.subTest(create=create), self.assertRaises(store.Refused) as caught:
                    store._open_child_dir(fd, "x" * 300, create)
                self.assertEqual(400, caught.exception.status)
        finally:
            os.close(fd)

    def test_deeply_nested_json(self) -> None:
        # The store refuses a body over REPORT_MAX_BYTES before it parses it.
        deepest = store.REPORT_MAX_BYTES // 2
        with self.subTest("a nested report"):
            found = nested_past_the_parser_on_a_thread(100000, deepest)
            if found is None:
                self.skipTest(NO_DOCUMENT_REFUSED % deepest)
            nested = found.encode()
            # The parser's refusal, not the answer for a document that is no object.
            for path in (f"{STREAM}/runs/{STAMP_1}", f"{STREAM}/latest"):
                refused = self.call("PUT", path, nested)
                self.assertEqual((400, {"error": "the body is not JSON"}), (refused.status_code, refused.json()))
            # A nested latest.json is skipped by the listing (the stream is listed
            # without a finished_at). GET latest hands stored bytes back unparsed.
            directory = self.root / "compliance-audit" / "acme" / "gitops"
            (directory / "runs").mkdir(parents=True)
            (directory / "runs" / f"{STAMP_1}.json").write_text("{}")
            (directory / "latest.json").write_bytes(nested)
            listed = self.call("GET", "/v1/audit-reports")
            self.assertEqual(200, listed.status_code)
            self.assertEqual(
                [{"audit_id": "compliance-audit", "key": "acme%2Fgitops", "newest_run": STAMP_1,
                  "latest_finished_at": None}],
                listed.json(),
            )
        # A nested JWT payload is a malformed token.
        header = _b64(json.dumps({"alg": "RS256", "kid": self.key.kid}).encode())
        payload = _b64(b"[" * 5000 + b"]" * 5000)
        self.assertEqual(401, self.call("GET", f"{STREAM}/runs", token=f"{header}.{payload}.c2ln").status_code)
        # And a nested JWKS answer is the JWKS unavailable.
        with mock.patch.object(store.json, "loads", side_effect=RecursionError("deep")):
            with self.assertRaises(store.JWKSUnavailable):
                store.fetch_cluster_keys(self.api.url, str(self.sa_dir))


class FutureStampTests(StoreTestCase):
    """A run stamped far ahead of the store's clock would never leave the ring."""

    def test_a_far_future_stamp_is_refused(self):
        self.assertEqual(400, self.put_run("99991231T235959.999999Z").status_code)
        self.assertFalse(any(self.root.rglob("*.json")))

    def test_a_stamp_inside_the_skew_is_written(self):
        inside = time.strftime("%Y%m%dT%H%M%S.000000Z", time.gmtime(FIXED_NOW + store.RUN_STAMP_MAX_FUTURE_SECONDS - 5))
        self.assertEqual(204, self.put_run(inside).status_code)

    def test_a_stamp_past_the_skew_is_refused(self):
        past = time.strftime("%Y%m%dT%H%M%S.000000Z", time.gmtime(FIXED_NOW + store.RUN_STAMP_MAX_FUTURE_SECONDS + 5))
        self.assertEqual(400, self.put_run(past).status_code)


class WriterTests(StoreTestCase):
    """The broker's X-Report-Writer stamp, kept beside each envelope (fleet-audit-report-store.md §3)."""

    BUILTIN = "builtin-schedule:compliance-audit:task-7f3a"

    def setUp(self) -> None:
        super().setUp()
        self.runs = self.root / "compliance-audit" / "acme" / "gitops" / "runs"
        self.store_dir = self.runs.parent

    @staticmethod
    def sidecar(writer: str, body: bytes) -> dict:
        return {"writer": writer, "sha256": hashlib.sha256(body).hexdigest()}

    def put(self, stamp: str, writer: str | None, doc: dict | None = None):
        body = json.dumps(doc or {"finished_at": stamp}).encode()
        return self.call("PUT", f"{STREAM}/runs/{stamp}", body, writer=writer), body

    def test_each_valid_writer_is_stored_beside_the_envelope(self) -> None:
        for stamp, writer in ((STAMP_1, "shell"), (STAMP_2, self.BUILTIN)):
            with self.subTest(writer=writer):
                response, body = self.put(stamp, writer)
                self.assertEqual(204, response.status_code)
                sidecar = self.runs / f"{stamp}.writer"
                self.assertEqual(self.sidecar(writer, body), json.loads(sidecar.read_bytes()))
                self.assertEqual(0o644, sidecar.stat().st_mode & 0o777)
                # The envelope is the caller's bytes, unchanged.
                self.assertEqual(body, (self.runs / f"{stamp}.json").read_bytes())
                got = self.call("GET", f"{STREAM}/runs/{stamp}")
                self.assertEqual(body, got.content)
                self.assertEqual(writer, got.headers.get(store.REPORT_WRITER_HEADER))
        self.assertEqual([], [p.name for p in self.runs.iterdir() if p.suffix == ".tmp"])
        # The run index and the stream list name envelopes alone, and carry no writer.
        index = self.call("GET", f"{STREAM}/runs")
        self.assertEqual([STAMP_2, STAMP_1], index.json())
        self.assertNotIn(store.REPORT_WRITER_HEADER, index.headers)
        listed = self.call("GET", "/v1/audit-reports")
        self.assertEqual(STAMP_2, listed.json()[0]["newest_run"])
        self.assertNotIn(store.REPORT_WRITER_HEADER, listed.headers)

    def test_a_missing_or_malformed_writer_is_400_and_writes_nothing(self) -> None:
        for writer in ("", "Shell", "shellx", "profile", "builtin-schedule", "builtin-schedule:compliance-audit",
                       "builtin-schedule:compliance-audit:", "builtin-schedule::task-1",
                       "builtin-schedule:compliance-audit:Task-1", "builtin-schedule:compliance-audit:task_1",
                       "builtin-schedule:compliance-audit:a:b", "builtin-schedule:compliance-audit:" + "a" * 64,
                       "builtin-schedule:compliance-audit:-task", "collector:compliance-audit:task-1"):
            with self.subTest(writer=writer):
                response, _ = self.put(STAMP_1, writer)
                self.assertEqual(400, response.status_code, response.text)
        self.assertFalse(any(self.root.rglob("*.json")))
        self.assertFalse(any(self.root.rglob("*.writer")))

    def test_an_unstamped_put_is_written_with_no_sidecar(self) -> None:
        # A broker from before the stamp sends none; the two roll independently.
        self.put(STAMP_1, "shell")
        response, body = self.put(STAMP_1, None, {"unstamped": 1})
        self.assertEqual(204, response.status_code)
        self.assertEqual(body, (self.runs / f"{STAMP_1}.json").read_bytes())
        self.assertFalse((self.runs / f"{STAMP_1}.writer").exists())
        got = self.call("GET", f"{STREAM}/runs/{STAMP_1}")
        self.assertEqual(body, got.content)
        self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", body).status_code)
        self.assertFalse((self.store_dir / "latest.writer").exists())
        self.assertNotIn(store.REPORT_WRITER_HEADER, self.call("GET", f"{STREAM}/latest").headers)

    def test_two_writer_headers_are_400(self) -> None:
        response = self.client.request(
            "PUT", f"{STREAM}/runs/{STAMP_1}", content=b"{}",
            headers=[("Authorization", f"Bearer {self.token}"), (store.REPORT_WRITER_HEADER, "shell"),
                     (store.REPORT_WRITER_HEADER, self.BUILTIN)],
        )
        self.assertEqual(400, response.status_code)
        self.assertFalse(any(self.root.rglob("*.json")))

    def test_a_builtin_writer_for_another_audit_is_400(self) -> None:
        response, _ = self.put(STAMP_1, "builtin-schedule:other-audit:task-1")
        self.assertEqual(400, response.status_code)
        self.assertFalse(any(self.root.rglob("*.json")))

    def test_a_rewritten_run_takes_the_new_writer(self) -> None:
        self.put(STAMP_1, self.BUILTIN)
        response, body = self.put(STAMP_1, "shell", {"again": 1})
        self.assertEqual(204, response.status_code)
        self.assertEqual(self.sidecar("shell", body), json.loads((self.runs / f"{STAMP_1}.writer").read_bytes()))

    def test_no_sidecar_answers_no_writer(self) -> None:
        # An envelope from before the sidecar, or the file store's own write.
        self.runs.mkdir(parents=True)
        (self.runs / f"{STAMP_1}.json").write_text("{}")
        (self.store_dir / "latest.json").write_text("{}")
        for path in (f"{STREAM}/runs/{STAMP_1}", f"{STREAM}/latest"):
            with self.subTest(path=path):
                got = self.call("GET", path)
                self.assertEqual(200, got.status_code)
                self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)

    def test_a_malformed_sidecar_answers_no_writer(self) -> None:
        self.runs.mkdir(parents=True)
        body = b"{}"
        (self.runs / f"{STAMP_1}.json").write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        for raw in (b"shell", b"shell\r\nX-Other: 1", b"", b"[]", b"{", b'{"writer": "shell"}',
                    json.dumps({"sha256": digest}).encode(), json.dumps({"writer": "Shell", "sha256": digest}).encode(),
                    json.dumps({"writer": "shell\r\nX-Other: 1", "sha256": digest}).encode(),
                    json.dumps({"writer": "shell", "sha256": 7}).encode(), "\u00e9".encode(),
                    # ASCII on disk, a non-ASCII str once decoded: never compared.
                    json.dumps({"writer": "shell", "sha256": "\u00e9" * 64}).encode(),
                    json.dumps({"writer": "shell", "sha256": digest.upper()}).encode()):
            with self.subTest(raw=raw):
                (self.runs / f"{STAMP_1}.writer").write_bytes(raw)
                got = self.call("GET", f"{STREAM}/runs/{STAMP_1}")
                self.assertEqual(200, got.status_code)
                self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)
        # PUT latest copies the matched run's sidecar: a planted digest is no
        # writer there either, and the copy still goes ahead.
        (self.runs / f"{STAMP_1}.writer").write_bytes(json.dumps({"writer": "shell", "sha256": "\u00e9" * 64}).encode())
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", body).status_code)
        self.assertNotIn(store.REPORT_WRITER_HEADER, self.call("GET", f"{STREAM}/latest").headers)
        # The same sidecar, well formed, is passed on.
        (self.runs / f"{STAMP_1}.writer").write_text(json.dumps(self.sidecar("shell", body)))
        self.assertEqual("shell", self.call("GET", f"{STREAM}/runs/{STAMP_1}").headers.get(store.REPORT_WRITER_HEADER))

    def test_a_sidecar_for_other_bytes_answers_no_writer(self) -> None:
        _, body = self.put(STAMP_1, self.BUILTIN, {"run": 1})
        self.call("PUT", f"{STREAM}/latest", body)
        # The envelopes rewritten under their sidecars, as a listener that
        # keeps none would after a rollback: the hashes no longer bind.
        (self.runs / f"{STAMP_1}.json").write_text('{"run": "forged"}')
        (self.store_dir / "latest.json").write_text('{"run": "forged"}')
        for path in (f"{STREAM}/runs/{STAMP_1}", f"{STREAM}/latest"):
            with self.subTest(path=path):
                got = self.call("GET", path)
                self.assertEqual(b'{"run": "forged"}', got.content)
                self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)

    def test_a_write_between_the_two_reads_never_pairs_mismatched(self) -> None:
        # A concurrent writer (one that ignores the lock: the platform-agent
        # container, or a listener without it) swaps latest and its sidecar
        # between the envelope read and the sidecar read, either way round.
        _, first = self.put(STAMP_1, "shell", {"run": 1})
        _, second = self.put(STAMP_2, self.BUILTIN, {"run": 2})
        writers = {first: "shell", second: self.BUILTIN}
        real = store._read_at
        for before, after in ((first, second), (second, first)):
            with self.subTest(before=before):
                self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", before).status_code)

                def racing(dir_fd, name, after=after):
                    data = real(dir_fd, name)
                    if name == store.REPORT_LATEST_NAME:
                        (self.store_dir / "latest.json").write_bytes(after)
                        (self.store_dir / "latest.writer").write_text(json.dumps(self.sidecar(writers[after], after)))
                    return data

                with mock.patch.object(store, "_read_at", side_effect=racing):
                    got = self.call("GET", f"{STREAM}/latest")
                self.assertEqual(before, got.content)
                self.assertIn(got.headers.get(store.REPORT_WRITER_HEADER), (None, writers[before]))
                self.assertIsNone(got.headers.get(store.REPORT_WRITER_HEADER))

    def test_reads_hold_the_store_lock(self) -> None:
        _, body = self.put(STAMP_1, "shell")
        self.call("PUT", f"{STREAM}/latest", body)
        held = []
        real = store._locked

        def recording(dir_fd):
            held.append(dir_fd)
            return real(dir_fd)

        with mock.patch.object(store, "_locked", side_effect=recording):
            for path in (f"{STREAM}/latest", f"{STREAM}/runs/{STAMP_1}"):
                with self.subTest(path=path):
                    held.clear()
                    self.assertEqual("shell", self.call("GET", path).headers.get(store.REPORT_WRITER_HEADER))
                    self.assertEqual(1, len(held))

    def test_put_latest_copies_the_matched_runs_writer(self) -> None:
        _, shell_body = self.put(STAMP_1, "shell", {"run": 1})
        _, builtin_body = self.put(STAMP_2, self.BUILTIN, {"run": 2})
        # The caller's own header on PUT latest is not what is kept.
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", builtin_body, writer="shell").status_code)
        self.assertEqual(self.sidecar(self.BUILTIN, builtin_body), json.loads((self.store_dir / "latest.writer").read_bytes()))
        self.assertEqual(0o644, (self.store_dir / "latest.writer").stat().st_mode & 0o777)
        got = self.call("GET", f"{STREAM}/latest")
        self.assertEqual(builtin_body, got.content)
        self.assertEqual(self.BUILTIN, got.headers.get(store.REPORT_WRITER_HEADER))
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", shell_body, writer=self.BUILTIN).status_code)
        self.assertEqual("shell", self.call("GET", f"{STREAM}/latest").headers.get(store.REPORT_WRITER_HEADER))
        # A matched entry with no sidecar leaves latest with none, never the last one's.
        (self.runs / f"{STAMP_1}.writer").unlink()
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", shell_body).status_code)
        self.assertFalse((self.store_dir / "latest.writer").exists())
        self.assertNotIn(store.REPORT_WRITER_HEADER, self.call("GET", f"{STREAM}/latest").headers)

    def test_delete_latest_drops_its_writer(self) -> None:
        _, body = self.put(STAMP_1, "shell")
        self.call("PUT", f"{STREAM}/latest", body)
        self.assertTrue((self.store_dir / "latest.writer").exists())
        self.assertEqual(204, self.call("DELETE", f"{STREAM}/latest").status_code)
        self.assertFalse((self.store_dir / "latest.writer").exists())

    def test_prune_removes_each_sidecar_with_its_envelope(self) -> None:
        stamps = [f"202611{day:02d}T000000.000000Z" for day in range(1, store.REPORT_HISTORY + 3)]
        self.runs.mkdir(parents=True)
        # An orphan sidecar older than the ring, as a sidecar-less listener's prune leaves.
        (self.runs / "20261001T000000.000000Z.writer").write_text(json.dumps(self.sidecar("shell", b"{}")))
        for stamp in stamps:
            self.assertEqual(204, self.put(stamp, self.BUILTIN)[0].status_code)
        kept = stamps[-store.REPORT_HISTORY:]
        self.assertEqual(sorted(f"{s}.json" for s in kept), sorted(p.name for p in self.runs.glob("*.json")))
        self.assertEqual(sorted(f"{s}.writer" for s in kept), sorted(p.name for p in self.runs.glob("*.writer")))

    def test_a_symlinked_sidecar_is_no_writer(self) -> None:
        _, body = self.put(STAMP_1, "shell")
        outside = self.tmp / "outside.writer"
        outside.write_text(json.dumps(self.sidecar(self.BUILTIN, body)))
        sidecar = self.runs / f"{STAMP_1}.writer"
        sidecar.unlink()
        sidecar.symlink_to(outside)
        # Never followed: the envelope reads, unstamped.
        got = self.call("GET", f"{STREAM}/runs/{STAMP_1}")
        self.assertEqual((200, body), (got.status_code, got.content))
        self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)
        # PUT latest copies no writer from it.
        self.assertEqual(204, self.call("PUT", f"{STREAM}/latest", body).status_code)
        self.assertFalse((self.store_dir / "latest.writer").exists())
        # latest.writer as a symlink: no writer on read, never followed.
        (self.store_dir / "latest.writer").symlink_to(outside)
        got = self.call("GET", f"{STREAM}/latest")
        self.assertEqual((200, body), (got.status_code, got.content))
        self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)
        # A directory at the sidecar's name is no writer either.
        sidecar.unlink()
        sidecar.mkdir()
        got = self.call("GET", f"{STREAM}/runs/{STAMP_1}")
        self.assertEqual(200, got.status_code)
        self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)
        sidecar.rmdir()
        # A rewrite replaces a linked sidecar itself and leaves its target alone.
        (self.runs / f"{STAMP_1}.writer").symlink_to(outside)
        self.assertEqual(204, self.put(STAMP_1, "shell")[0].status_code)
        self.assertFalse(sidecar.is_symlink())
        self.assertEqual(self.sidecar(self.BUILTIN, body), json.loads(outside.read_text()))

    def test_delete_latest_holds_the_store_lock(self) -> None:
        _, body = self.put(STAMP_1, "shell")
        self.call("PUT", f"{STREAM}/latest", body)
        held = []
        real = store._locked

        def recording(dir_fd):
            held.append(dir_fd)
            return real(dir_fd)

        with mock.patch.object(store, "_locked", side_effect=recording):
            self.assertEqual(204, self.call("DELETE", f"{STREAM}/latest").status_code)
        self.assertEqual(1, len(held))


class RunStartTests(StoreTestCase):
    """The run-start record (#6812): `runs/started-<run>` on the run routes alone, kept beside the ring."""

    def setUp(self) -> None:
        super().setUp()
        self.store_dir = self.root / "compliance-audit" / "acme" / "gitops"
        self.starts = self.store_dir / store.RUN_STARTS_DIR
        # This listener's clock moves a millisecond a reading, so each record
        # is received after the one before it, whatever its `started_at` says.
        self.ticks = [FIXED_NOW]
        fixed = store._now

        def clock() -> float:
            self.ticks[0] += 0.001
            return self.ticks[0]

        store._now = clock
        self.addCleanup(setattr, store, "_now", fixed)
        # A run published before: the store exists.
        self.assertEqual(204, self.put_run(STAMP_1).status_code)

    @staticmethod
    def record(run_id: str, started: str = "2026-12-01T00:00:00+00:00", **extra) -> bytes:
        return json.dumps({"run_id": run_id, "started_at": started, "scheduled": True, **extra}).encode()

    def put_start(self, run_id: str, body: bytes | None = None, writer: str | None = "default"):
        return self.call("PUT", f"{STREAM}/runs/started-{run_id}", body or self.record(run_id), writer=writer)

    def test_a_record_round_trips_beside_the_ring(self) -> None:
        body = self.record("task-7f3a")
        self.assertEqual(204, self.put_start("task-7f3a", body).status_code)
        written = self.starts / "task-7f3a.json"
        kept = json.loads(written.read_bytes())
        received = kept.pop(store.RUN_START_RECEIVED_KEY)
        self.assertEqual(json.loads(body), kept)
        self.assertEqual(0o644, written.stat().st_mode & 0o777)
        got = self.call("GET", f"{STREAM}/runs/started-task-7f3a")
        self.assertEqual(200, got.status_code)
        self.assertEqual(written.read_bytes(), got.content)
        # Unstamped: a broker header is checked, not kept.
        self.assertNotIn(store.REPORT_WRITER_HEADER, got.headers)
        self.assertFalse(any(p.suffix == ".writer" for p in self.starts.iterdir()))
        self.assertEqual(404, self.call("GET", f"{STREAM}/runs/started-task-0").status_code)
        # Never a ring entry: not in the index, not the newest run, not in runs/.
        self.assertEqual([STAMP_1], self.call("GET", f"{STREAM}/runs").json())
        (row,) = self.call("GET", "/v1/audit-reports").json()
        self.assertEqual(STAMP_1, row["newest_run"])
        self.assertEqual(
            {"run_id": "task-7f3a", "started_at": "2026-12-01T00:00:00+00:00", "received_at": received, "scheduled": True},
            row["last_run_start"],
        )
        self.assertEqual([f"{STAMP_1}.json", f"{STAMP_1}.writer"], sorted(p.name for p in (self.store_dir / "runs").iterdir()))

    def test_a_record_never_creates_a_store(self) -> None:
        other = "/v1/audit-reports/compliance-audit/acme%2Fother"
        response = self.call("PUT", f"{other}/runs/started-task-1", self.record("task-1"))
        self.assertEqual(404, response.status_code)
        self.assertEqual({"error": "not found"}, response.json())
        self.assertFalse((self.root / "compliance-audit" / "acme" / "other").exists())

    def test_a_record_must_be_its_runs_and_must_have_started(self) -> None:
        future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(FIXED_NOW + store.RUN_STAMP_MAX_FUTURE_SECONDS + 60))
        for name, body, status in (
            ("another run's", self.record("task-2"), 400),
            ("a done mark with no start", json.dumps({"run_id": "task-1", "done_at": "2026-12-01T00:00:00+00:00"}).encode(), 400),
            ("a start with no zone", self.record("task-1", "2026-12-01T00:00:00"), 400),
            ("a start far ahead", self.record("task-1", future), 400),
            ("scheduled that is no boolean", self.record("task-1", scheduled="yes"), 400),
            ("a done_at that is no time", self.record("task-1", done_at="soon"), 400),
            ("not an object", b"[]", 400),
            ("too large", self.record("task-1", pad="x" * store.RUN_START_MAX_BYTES), 413),
        ):
            with self.subTest(name):
                self.assertEqual(status, self.put_start("task-1", body).status_code)
        self.assertFalse(self.starts.exists() and any(self.starts.iterdir()))
        # The broker's writer header is held to a run write's rule.
        self.assertEqual(400, self.put_start("task-1", writer="builtin-schedule:other-audit:task-1").status_code)
        self.assertEqual(204, self.put_start("task-1", writer="builtin-schedule:compliance-audit:task-1").status_code)
        self.assertEqual(204, self.put_start("task-1", writer=None).status_code)

    def test_the_reserved_name_is_refused_off_its_two_routes(self) -> None:
        body = self.record("task-1")
        self.assertEqual(204, self.put_start("task-1", body).status_code)
        # Not latest: a record is never a ring entry, so it is no copy of one.
        self.assertEqual(409, self.call("PUT", f"{STREAM}/latest", body).status_code)
        self.assertEqual(405, self.call("DELETE", f"{STREAM}/runs/started-task-1").status_code)
        for path in (f"{STREAM}/runs/started-task-1/latest", f"{STREAM}/started-task-1", f"{STREAM}/latest/started-task-1"):
            with self.subTest(path=path):
                self.assertEqual(404, self.call("GET", path).status_code)
        for bad in ("started-", "started-Task-1", "started-" + "a" * 64, "started--x", "started-task-1.json", "started-a.b"):
            with self.subTest(name=bad):
                self.assertEqual(400, self.call("PUT", f"{STREAM}/runs/{bad}", body).status_code)
                self.assertEqual(400, self.call("GET", f"{STREAM}/runs/{bad}").status_code)
        self.assertEqual(["task-1.json"], sorted(p.name for p in self.starts.iterdir()))

    def test_records_never_count_in_the_ring(self) -> None:
        for n in range(store.RUN_START_HISTORY):
            self.assertEqual(204, self.put_start(f"task-{n}", self.record(f"task-{n}", f"2026-12-0{n + 1}T00:00:00+00:00")).status_code)
        stamps = [f"202611{day:02d}T000000.000000Z" for day in range(2, 2 + store.REPORT_HISTORY)]
        for stamp in stamps:
            self.assertEqual(204, self.put_run(stamp).status_code)
        # The ring pruned to its fourteen, every one a stamp; the records all stand.
        self.assertEqual(list(reversed(stamps)), self.call("GET", f"{STREAM}/runs").json())
        self.assertEqual(store.RUN_START_HISTORY, len(list(self.starts.glob("*.json"))))

    def test_the_newest_records_are_kept_by_their_receipt(self) -> None:
        for n in range(store.RUN_START_HISTORY + 2):
            self.put_start(f"task-{n}", self.record(f"task-{n}", f"2026-11-{n + 10:02d}T00:00:00+00:00"))
        # A record received last stays, whatever start its writer claims; the
        # first received goes.
        self.assertEqual(204, self.put_start("task-old", self.record("task-old", "2026-10-01T00:00:00+00:00")).status_code)
        # A file named for a run that is no record of it goes too.
        (self.starts / "task-junk.json").write_text("{}")
        # A done mark keeps its record's place: task-9 is still older than task-old.
        self.put_start("task-9", self.record("task-9", "2026-11-19T00:00:00+00:00", done_at="2026-11-19T01:00:00+00:00"))
        self.assertEqual(
            sorted([f"task-{n}.json" for n in range(3, store.RUN_START_HISTORY + 2)] + ["task-old.json"]),
            sorted(p.name for p in self.starts.iterdir()),
        )

    def test_a_start_further_ahead_than_the_skew_is_refused(self) -> None:
        def ahead(seconds: float) -> str:
            return datetime.fromtimestamp(FIXED_NOW + seconds, timezone.utc).isoformat()

        self.assertEqual(60, store.RUN_START_MAX_FUTURE_SECONDS)
        # Planted to stand as the newest over a real run that starts later.
        self.assertEqual(400, self.put_start("task-1", self.record("task-1", ahead(store.RUN_START_MAX_FUTURE_SECONDS + 30))).status_code)
        self.assertFalse(self.starts.exists() and any(self.starts.iterdir()))
        self.assertEqual(204, self.put_start("task-1", self.record("task-1", ahead(30))).status_code)

    def test_the_newest_is_by_receipt_not_the_writers_clock(self) -> None:
        # A record whose writer claims the later start, received first, does
        # not stand over a real run received after it.
        self.assertEqual(204, self.put_start("task-planted", self.record("task-planted", "2027-01-01T00:00:50+00:00")).status_code)
        self.assertEqual(204, self.put_start("task-real", self.record("task-real", "2026-12-31T23:00:00+00:00")).status_code)
        (row,) = self.call("GET", "/v1/audit-reports").json()
        self.assertEqual("task-real", row["last_run_start"]["run_id"])

    def test_a_done_mark_keeps_its_received_at(self) -> None:
        self.put_start("task-old", self.record("task-old"))
        first = json.loads((self.starts / "task-old.json").read_bytes())[store.RUN_START_RECEIVED_KEY]
        self.put_start("task-new", self.record("task-new", "2026-11-01T00:00:00+00:00"))
        # The older run's done mark, sent with a received_at of its own.
        self.assertEqual(
            204,
            self.put_start(
                "task-old",
                self.record("task-old", done_at="2026-12-01T01:00:00+00:00", outcome="UPDATED", received_at="2099-01-01T00:00:00+00:00"),
            ).status_code,
        )
        marked = json.loads((self.starts / "task-old.json").read_bytes())
        self.assertEqual(first, marked[store.RUN_START_RECEIVED_KEY])
        self.assertEqual("2026-12-01T01:00:00+00:00", marked["done_at"])
        (row,) = self.call("GET", "/v1/audit-reports").json()
        self.assertEqual("task-new", row["last_run_start"]["run_id"])

    def test_a_received_at_the_writer_sends_is_never_kept(self) -> None:
        self.put_start("task-1", self.record("task-1", received_at="2099-01-01T00:00:00+00:00"))
        kept = json.loads((self.starts / "task-1.json").read_bytes())[store.RUN_START_RECEIVED_KEY]
        self.assertNotEqual("2099-01-01T00:00:00+00:00", kept)
        self.assertTrue(FIXED_NOW < datetime.fromisoformat(kept).timestamp() <= self.ticks[0] + 1e-6, kept)

    def test_a_record_from_before_received_at_sorts_below_every_one_with_it(self) -> None:
        self.starts.mkdir()
        (self.starts / "task-legacy.json").write_bytes(self.record("task-legacy", "2027-01-01T00:00:30+00:00"))
        self.put_start("task-1", self.record("task-1", "2026-12-01T00:00:00+00:00"))
        (row,) = self.call("GET", "/v1/audit-reports").json()
        self.assertEqual("task-1", row["last_run_start"]["run_id"])

    def test_the_listing_names_the_newest_start_not_the_newest_write(self) -> None:
        self.put_start("task-old", self.record("task-old", "2026-12-01T00:00:00+00:00"))
        self.put_start("task-new", self.record("task-new", "2026-12-02T00:00:00+00:00"))
        # The older run's done mark lands after the newer run started.
        self.put_start(
            "task-old", self.record("task-old", "2026-12-01T00:00:00+00:00", done_at="2026-12-02T01:00:00+00:00", outcome="UPDATED")
        )
        (row,) = self.call("GET", "/v1/audit-reports").json()
        self.assertEqual("task-new", row["last_run_start"]["run_id"])
        self.assertNotIn("done_at", row["last_run_start"])

    def test_a_symlinked_record_directory_is_refused(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.starts.symlink_to(elsewhere)
        self.assertEqual(409, self.put_start("task-1").status_code)
        self.assertEqual([], list(elsewhere.iterdir()))
        self.assertEqual(409, self.call("GET", f"{STREAM}/runs/started-task-1").status_code)

    def test_the_client_copies_match(self) -> None:
        import importlib.util

        path = AUDIT_REPORT_PATH.parent / "report_status.py"
        spec = importlib.util.spec_from_file_location("report_status_for_store_test", path)
        status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(status)
        for name in ("RUN_START_PREFIX", "RUN_STARTS_DIR", "RUN_START_HISTORY", "RUN_START_MAX_BYTES"):
            self.assertEqual(getattr(store, name), getattr(status, name), name)
        for run_id in ("task-1", "local-0a1b2c3d4e5f", "a" * 63, "a" * 64, "Task-1", "-a", "a-", "a.b", ""):
            self.assertEqual(bool(store.RUN_ID_RE.match(run_id)), bool(status.RUN_ID_RE.match(run_id)), run_id)


if __name__ == "__main__":
    unittest.main()
