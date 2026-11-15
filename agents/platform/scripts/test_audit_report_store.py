"""Tests for audit_report_store.py, the broker-only audit-report store listener (#5619)."""

from __future__ import annotations

import base64
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
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
STREAM = "/v1/audit-reports/compliance-audit/acme%2Fgitops"
STAMP_1 = "20261101T010203.000001Z"
STAMP_2 = "20261102T010203.000001Z"


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
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "reports"
        self.sa_dir = self.tmp / "sa"
        self.sa_dir.mkdir()
        (self.sa_dir / "token").write_text("pod-own-token\n")
        self.api = FakeAPIServer()
        self.key = RSAKey("k1")
        self.api.keys = [self.key.jwk()]
        env = {
            store.REPORTS_DIR_ENV: str(self.root),
            store.ALLOWED_SUBJECT_ENV: BROKER,
            store.API_SERVER_ENV: self.api.url,
            store.SA_DIR_ENV: str(self.sa_dir),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.api.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = TestClient(store.create_app(store.TokenVerifier()))
        self.token = mint(self.key)

    def call(self, method: str, path: str, body: bytes | None = None, token: str | None = "default"):
        headers = {}
        if token == "default":
            token = self.token
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
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
        self.assertEqual(14, len(list((self.root / "compliance-audit" / "acme" / "gitops" / "runs").iterdir())))

    def test_a_failed_prune_does_not_fail_the_write(self) -> None:
        for day in range(1, 15):
            self.put_run(f"202611{day:02d}T000000.000000Z")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("read-only")):
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

    def test_a_symlink_escaping_the_root_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "compliance-audit").mkdir(parents=True)
        os.symlink(outside, self.root / "compliance-audit" / "acme")
        self.assertRefused("PUT", f"{STREAM}/runs/{STAMP_1}")
        self.assertRefused("GET", f"{STREAM}/runs")
        self.assertEqual([], list(outside.iterdir()))
        # A planted latest.json symlink too.
        good = self.root / "compliance-audit" / "corp" / "gitops"
        good.mkdir(parents=True)
        secret = outside / "secret.json"
        secret.write_text("{}")
        os.symlink(secret, good / "latest.json")
        self.assertRefused("GET", "/v1/audit-reports/compliance-audit/corp%2Fgitops/latest")

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
        verifier = store.TokenVerifier(monotonic=lambda: clock[0])
        client = TestClient(store.create_app(verifier))
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


if __name__ == "__main__":
    unittest.main()
