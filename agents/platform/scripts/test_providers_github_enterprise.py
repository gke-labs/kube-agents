#!/usr/bin/env python3
"""A GitHub forge at a GitHub Enterprise Server host, tested against a mock instance (#2752).

    python3 -m pytest -q agents/platform/scripts/test_providers_github_enterprise.py

The mock is a local HTTPS server. Its CA, key and certificate are made with
the `openssl` binary at test time, so no key is kept in the repository. It
answers at the `/api/v3` prefix a GitHub Enterprise Server instance serves the
REST API under, and it replays the github.com recordings in
`testdata/providers/github/`: the instance serves the same API, so the forge
must make the same calls and give the same answers as on github.com. The
broker reaches it at its own hostname, `ghe.mock.test`; the test sends that
name's connections to the loopback port the mock listens on, so TLS checks
the certificate against the real hostname.

What is pinned, beside the verbs: the write token reaches the mock as an
`Authorization` header and nowhere else; the CA trust is the forge's own, so a
wrong or missing CA is FORGE_TLS_UNTRUSTED; a redirect is refused and the
credential is not sent on; the managed-repository keys are
`github:<host>/owner/name`; and a github.com install is what it was.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import credential_proxy
import github_token_refresh
import gitops_workspace
import providers
import vcs_broker
from providers import transport as transport_module
from providers.credentials import MintedWriteCredential
from providers.registry import FORGES_CONFIG_ENV
from workspace_paths import WorkspaceError

OPENSSL = shutil.which("openssl")
HOST = "ghe.mock.test"
OTHER_HOST = "elsewhere.mock.test"
REPO_URL = f"https://{HOST}/acme/infra"
TOKEN = "mock-installation-token-0001"
FIXTURES = Path(__file__).resolve().parent / "testdata" / "providers" / "github"
DIFF = "diff --git a/x b/x\n"


def setUpModule():
    # gitops_workspace ignores GITOPS_STATE_PATH where the operator's
    # /etc/gitops mount exists (in_operator_pod), and this module isolates its
    # tests with that variable. Point the mount check away from any real one.
    global _NO_MOUNT
    _NO_MOUNT = mock.patch.object(gitops_workspace, "GITOPS_STATE_DIR", "/nonexistent/kube-agents-test/etc-gitops")
    _NO_MOUNT.start()


def tearDownModule():
    _NO_MOUNT.stop()


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True)


def make_pki(root: Path, hosts: tuple[str, ...]) -> tuple[Path, Path, Path]:
    """A private CA, and a server certificate for `hosts` it signed."""
    root.mkdir(parents=True, exist_ok=True)
    _openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=mock GitHub Enterprise Server CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash",
        cwd=root,
    )
    (root / "leaf.ext").write_text(
        "subjectAltName=" + ",".join(f"DNS:{h}" for h in hosts) + "\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid\n"
        "subjectKeyIdentifier=hash\n"
    )
    _openssl(
        "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
        "-out", "leaf.csr", "-subj", f"/CN={hosts[0]}", cwd=root,
    )
    _openssl(
        "x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-out", "leaf.crt", "-extfile", "leaf.ext",
        cwd=root,
    )
    return root / "ca.crt", root / "leaf.crt", root / "leaf.key"


class MockGitHubEnterprise:
    """A mock GitHub Enterprise Server: the REST API at /api/v3, answered from a queue.

    Each request takes the next queued answer: a JSON value, a recorded
    refusal (`{"__status__": 404, "__detail__": ...}`), or, for a request that
    asks for the diff media type, the diff. A path outside /api/v3 is a 404
    the test fails on. `redirect_to` makes every request answer 302 to that
    URL instead.
    """

    def __init__(self, cert: Path, key: Path) -> None:
        self.answers: list = []
        self.requests: list[dict] = []
        self.redirect_to = ""
        mock_server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                mock_server.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "accept": self.headers.get("Accept"),
                    "host": self.headers.get("Host"),
                    "body": json.loads(body) if body else None,
                })
                if mock_server.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", mock_server.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self.path.startswith("/api/v3/"):
                    return self._json(404, {"message": "Not Found"})
                if (self.headers.get("Accept") or "").endswith(".diff"):
                    data = DIFF.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if not mock_server.answers:
                    return self._json(500, {"message": f"the mock has no answer for {self.command} {self.path}"})
                answer = mock_server.answers.pop(0)
                if isinstance(answer, dict) and "__status__" in answer:
                    return self._json(int(answer["__status__"]), {"message": answer.get("__detail__") or ""})
                self._json(200, answer)

            def _json(self, status: int, value) -> None:
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Recorded:
    """The transport's `api` answering from a fixture: how the forge behaves on github.com."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, path, *, params=None, body=None, raw=None):
        self.calls.append((method, path, params, body, raw))
        if raw:
            return DIFF
        answer = self.responses.pop(0)
        if isinstance(answer, dict) and "__status__" in answer:
            raise WorkspaceError(answer.get("__detail__") or "recorded refusal",
                                 status=int(answer["__status__"]), code="FORGE_CALL_FAILED")
        return answer


@unittest.skipUnless(OPENSSL, "openssl is needed to make the mock's private CA")
class MockGitHubEnterpriseCase(unittest.TestCase):
    """A broker configured with one GitHub forge at the mock's host."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        transport_module._CA_CONTEXTS.clear()
        self.ca, cert, key = make_pki(self.dir / "pki", (HOST, OTHER_HOST))
        self.other_ca, _, _ = make_pki(self.dir / "other-pki", (HOST,))
        self.mock = MockGitHubEnterprise(cert, key)
        self.addCleanup(self.mock.close)
        # The mock's hostname, and the second one a redirect names, reach
        # the mock's loopback port; nothing else is touched.
        real = socket.create_connection
        port = self.mock.port

        def connect(address, *args, **kwargs):
            if address[0] in (HOST, OTHER_HOST) and address[1] == 443:
                address = ("127.0.0.1", port)
            return real(address, *args, **kwargs)

        patcher = mock.patch("socket.create_connection", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.minted: list[tuple[str, str]] = []

    def mint_write(self, provider: str, repo: str) -> str:
        self.minted.append((provider, repo))
        return TOKEN

    def broker(self, ca_file: str | None = None, *, ca_source: str = "") -> vcs_broker.VcsBroker:
        entry = {"provider": "github", "host": HOST}
        if ca_file is None:
            ca_file = str(self.ca)
        if ca_file:
            entry["caFile"] = ca_file
        if ca_source:
            entry["caSecret"] = ca_source
        config = self.dir / "forges.json"
        config.write_text(json.dumps({"forges": [entry]}))
        os.environ[FORGES_CONFIG_ENV] = str(config)
        return vcs_broker.VcsBroker(
            self.dir / "scratch",
            git_runner=lambda *a, **k: None,
            mint_write=self.mint_write,
            http_timeout=10.0,
        )


class VerbsAgainstTheMockTest(MockGitHubEnterpriseCase):
    def test_every_verb_makes_github_coms_calls_at_the_api_v3_prefix_with_the_minted_token(self):
        dotcom = providers.Registry({"forges": [{"provider": "github", "host": "github.com"}]}).forges[0]
        broker = self.broker()
        forge = broker.registry.forges[0]
        for fixture_file in sorted(FIXTURES.glob("*.json")):
            verb = fixture_file.stem
            fixture = json.loads(fixture_file.read_text())
            with self.subTest(verb=verb):
                recorded = Recorded(fixture["responses"])
                expected = getattr(dotcom, verb.replace("-", "_"))(recorded, "acme/infra", dict(fixture["payload"]))
                self.mock.requests.clear()
                self.mock.answers = [r for r in fixture["responses"]]
                binding = broker._bind({"repository": REPO_URL})
                self.assertIs(forge, binding.forge)
                answer = getattr(forge, verb.replace("-", "_"))(binding.api, binding.repo, dict(fixture["payload"]))
                self.assertEqual(expected, answer)
                self.assertEqual(len(recorded.calls), len(self.mock.requests))
                for (method, path, params, body, raw), seen in zip(recorded.calls, self.mock.requests):
                    self.assertEqual(method, seen["method"])
                    self.assertTrue(seen["path"].startswith(f"/api/v3/{path.lstrip('/')}"), seen["path"])
                    self.assertEqual(f"Bearer {TOKEN}", seen["authorization"])
                    self.assertEqual(HOST, seen["host"])
                    self.assertEqual(body, seen["body"])
                    if raw:
                        self.assertEqual(raw, seen["accept"])
                self.assertEqual([], self.mock.answers, "the mock holds answers the forge never asked for")
        self.assertTrue(self.minted)
        self.assertEqual({("github", "acme/infra")}, set(self.minted))

    def test_the_forge_is_the_http_transport_with_no_cli(self):
        broker = self.broker()
        forge = broker.registry.forges[0]
        self.assertEqual(("http", f"https://{HOST}/api/v3", (HOST,)), (forge.transport, forge.api_url, forge.hosts))
        self.assertEqual((), broker.registry.executables)
        self.assertIsInstance(forge.credential, MintedWriteCredential)
        self.assertEqual(f"https://{HOST}/acme/infra.git", forge.clone_url("acme/infra"))
        self.assertEqual(f"https://{HOST}/acme/infra/blob/{{ref}}/{{path}}", forge.file_url("acme/infra"))


class RefusedTokenTest(MockGitHubEnterpriseCase):
    """A 401 from the instance drops the cached write token, so the next call mints anew."""

    def test_an_api_401_drops_the_token(self):
        dropped = []
        config = self.dir / "forges.json"
        config.write_text(json.dumps({"forges": [{"provider": "github", "host": HOST, "caFile": str(self.ca)}]}))
        os.environ[FORGES_CONFIG_ENV] = str(config)
        broker = vcs_broker.VcsBroker(self.dir / "scratch", git_runner=lambda *a, **k: None,
                                      mint_write=self.mint_write, drop_write=dropped.append, http_timeout=10.0)
        self.mock.answers = [{"__status__": 401, "__detail__": "Bad credentials"}]
        binding = broker._bind({"repository": REPO_URL})
        with self.assertRaises(WorkspaceError) as caught:
            binding.api("GET", "repos/acme/infra/labels")
        self.assertEqual(401, caught.exception.status)
        self.assertEqual(["github"], dropped)

    def test_a_401_in_one_request_leaves_a_concurrent_requests_token(self):
        # Two requests on one repository: B meets a 401, A is past its
        # `ensure` and still presents its token. Only the minter's cache is
        # dropped, so A's next call carries its token rather than none.
        dropped = []
        config = self.dir / "forges.json"
        config.write_text(json.dumps({"forges": [{"provider": "github", "host": HOST, "caFile": str(self.ca)}]}))
        os.environ[FORGES_CONFIG_ENV] = str(config)
        broker = vcs_broker.VcsBroker(self.dir / "scratch", git_runner=lambda *a, **k: None,
                                      mint_write=self.mint_write, drop_write=dropped.append, http_timeout=10.0)
        a = broker._bind({"repository": REPO_URL})
        b = broker._bind({"repository": REPO_URL})
        a.ensure()
        self.mock.answers = [{"__status__": 401, "__detail__": "Bad credentials"}]
        with self.assertRaises(WorkspaceError):
            b.api("GET", "repos/acme/infra/labels")
        self.assertEqual(["github"], dropped)
        self.assertEqual({"Authorization": f"Bearer {TOKEN}"}, a.forge.credential.headers("acme/infra"))
        self.assertIn(EXTRAHEADER, dict(a.forge.credential.git_config("acme/infra")))

    def test_a_401_on_the_identity_query_drops_the_token(self):
        dropped = []
        config = self.dir / "forges.json"
        config.write_text(json.dumps({"forges": [{"provider": "github", "host": HOST, "caFile": str(self.ca)}]}))
        os.environ[FORGES_CONFIG_ENV] = str(config)
        broker = vcs_broker.VcsBroker(self.dir / "scratch", git_runner=lambda *a, **k: None,
                                      mint_write=self.mint_write, drop_write=dropped.append, http_timeout=10.0)
        handler_class = self.mock.server.RequestHandlerClass

        def refuse(handler):
            handler._json(401, {"message": "Bad credentials"})

        handler_class.do_POST = refuse
        with self.assertRaises(WorkspaceError) as caught:
            broker.identity({"repository": REPO_URL})
        self.assertEqual(401, caught.exception.status)
        self.assertEqual(["github"], dropped)


class TrustTest(MockGitHubEnterpriseCase):
    def call(self, broker) -> object:
        self.mock.answers = [[]]
        binding = broker._bind({"repository": REPO_URL})
        return binding.api("GET", "repos/acme/infra/labels")

    def test_the_forges_own_ca_is_trusted(self):
        self.assertEqual([], self.call(self.broker()))

    def test_a_wrong_ca_is_untrusted_and_sends_nothing(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.call(self.broker(str(self.other_ca)))
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.fields["code"])
        self.assertIn(HOST, caught.exception.fields["detail"])
        self.assertEqual([], self.mock.requests)

    def test_no_ca_is_untrusted(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.call(self.broker(""))
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.fields["code"])

    def test_a_ca_file_that_is_not_mounted_names_its_secret(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.call(self.broker(str(self.dir / "absent.crt"), ca_source="ghe-ca"))
        self.assertEqual("FORGE_TLS_UNTRUSTED", caught.exception.fields["code"])
        self.assertIn("ghe-ca", caught.exception.fields["detail"])

    def test_a_redirect_is_refused_and_the_token_is_not_sent_on(self):
        broker = self.broker()
        self.mock.redirect_to = f"https://{OTHER_HOST}/api/v3/repos/acme/infra/labels"
        with self.assertRaises(WorkspaceError) as caught:
            self.call(broker)
        self.assertEqual("FORGE_REDIRECTED", caught.exception.fields["code"])
        self.assertEqual([HOST], [r["host"] for r in self.mock.requests])


class WriteCredentialTest(unittest.TestCase):
    def test_git_gets_the_token_as_an_extraheader_on_the_host_only(self):
        credential = MintedWriteCredential("github", lambda provider, repo: TOKEN, HOST)
        self.assertEqual((("credential.helper", ""),), credential.git_config("acme/infra"))
        credential.ensure("acme/infra")
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        self.assertEqual(
            ((f"http.https://{HOST}/.extraheader", f"AUTHORIZATION: basic {basic}"),
             (f"http.https://{HOST}/.followRedirects", "false"), ("credential.helper", "")),
            credential.git_config("acme/infra"),
        )
        self.assertEqual({"Authorization": f"Bearer {TOKEN}"}, credential.headers("acme/infra"))
        # Another repository has no token until its own mint.
        with self.assertRaises(WorkspaceError):
            credential.headers("acme/other")

    def test_a_refused_mint_propagates_and_a_failed_one_is_named(self):
        def refuse(provider, repo):
            raise PermissionError("not managed")

        with self.assertRaises(PermissionError):
            MintedWriteCredential("github", refuse, HOST).ensure("acme/infra")
        for mint in (lambda p, r: (_ for _ in ()).throw(RuntimeError("minter down")), lambda p, r: "", None):
            with self.subTest(mint=mint), self.assertRaises(WorkspaceError) as caught:
                MintedWriteCredential("github", mint, HOST).ensure("acme/infra")
            self.assertEqual("FORGE_CREDENTIAL_UNAVAILABLE", caught.exception.fields["code"])


class ManagedKeysTest(unittest.TestCase):
    def state(self, github_host: str | None) -> None:
        """A mounted gitops-state directory, with the operator's `github_host` key or without it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "managed_repos").write_text("[]")
        if github_host is not None:
            (root / gitops_workspace.GITHUB_HOST_KEY).write_text(github_host + "\n")
        patcher = mock.patch.dict(os.environ, {"GITOPS_STATE_PATH": str(root / "managed_repos")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_enterprise_entry_is_keyed_and_named_with_its_host(self):
        self.state(HOST)
        entries = [
            {"type": "github", "url": f"https://{HOST}/acme/infra"},
            {"type": "github", "url": "https://github.com/acme/web"},
            {"type": "github", "url": "acme/bare"},
        ]
        # The github.com and bare entries are skipped on this install (see
        # GitHubDotComEntriesOnAnEnterpriseInstallTest).
        with self.assertLogs(gitops_workspace.LOGGER, level="WARNING"):
            self.assertEqual([f"github:{HOST}/acme/infra"],
                             gitops_workspace._repository_keys(entries, gitops_workspace.MANAGED_REPOS_KEY))
        with self.assertLogs(gitops_workspace.LOGGER, level="WARNING"):
            self.assertEqual([f"{HOST}/acme/infra"],
                             gitops_workspace._forge_repo_names(entries, gitops_workspace.MANAGED_REPOS_KEY))
        self.assertEqual(HOST, gitops_workspace.github_host())
        self.assertTrue(gitops_workspace.is_valid_repo_slug(f"{HOST}/acme/infra"))

    def test_the_host_comes_from_the_operators_key_and_never_from_an_entry(self):
        # A hand-added GitHub entry on another host must not move the host of
        # a github.com install: it is skipped with the warning it had before.
        self.state(None)
        entries = [{"type": "github", "url": "https://gitlab.acme.example/acme/infra"}]
        self.assertEqual("github.com", gitops_workspace.github_host())
        with self.assertLogs(gitops_workspace.LOGGER, level="WARNING") as logs:
            self.assertEqual([], gitops_workspace._repository_keys(entries, gitops_workspace.MANAGED_REPOS_KEY))
        self.assertIn("typed github but is not a github.com owner/name repository", "\n".join(logs.output))
        # On a GitHub Enterprise Server install, an entry on a third host is skipped too.
        self.state(HOST)
        with self.assertLogs(gitops_workspace.LOGGER, level="WARNING"):
            self.assertEqual([], gitops_workspace._repository_keys(entries, gitops_workspace.MANAGED_REPOS_KEY))
        # A key that is not a hostname, or names github.com, reads as github.com.
        for value in ("not a host", "github.com", "WWW.GitHub.com", ""):
            with self.subTest(value=value):
                self.state(value)
                self.assertEqual("github.com", gitops_workspace.github_host())

    def test_the_broker_admits_an_enterprise_repository_by_its_host_key(self):
        forge = providers.Registry({"forges": [{"provider": "github", "host": HOST}]}).forges[0]
        dotcom = providers.Registry({"forges": [{"provider": "github", "host": "github.com"}]}).forges[0]
        keys = frozenset({f"github:{HOST}/acme/infra"})
        with mock.patch.object(credential_proxy, "managed_repositories", return_value=keys):
            self.assertTrue(credential_proxy.repository_is_managed("acme/infra", forge))
            self.assertFalse(credential_proxy.repository_is_managed("acme/infra", dotcom))


class WriteMintTest(unittest.TestCase):
    """The executor's in-memory write mint: managed only, cached per host."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.executor = credential_proxy.CommandExecutor(
            timeout_seconds=5, max_output_bytes=1 << 16, state_dir=tmp.name, scoped_pool=None
        )
        self.forge = providers.Registry({"forges": [{"provider": "github", "host": HOST}]}).forges[0]
        self.runs: list[list[str]] = []
        helper = Path(tmp.name) / "github_token_refresh.py"
        helper.write_text("")
        patches = [
            mock.patch.object(credential_proxy, "_provider_forge", return_value=self.forge),
            mock.patch.object(credential_proxy, "managed_repositories",
                              return_value=frozenset({f"github:{HOST}/acme/infra", f"github:{HOST}/acme/web"})),
            mock.patch.object(credential_proxy.CommandExecutor, "_forge_helper", return_value=helper),
            mock.patch.object(credential_proxy.CommandExecutor, "_run_forge_helper", side_effect=self.run_helper),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_helper(self, provider, helper, arguments, action, log_success=False):
        self.runs.append(arguments)
        stdout = json.dumps({"token": TOKEN, "repositories": ["acme/infra", "acme/web"]})
        return credential_proxy.ExecutionResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1, truncated=False, timed_out=False)

    def test_a_managed_repository_is_minted_once_for_the_repositories_the_token_covers(self):
        self.assertEqual(TOKEN, self.executor.mint_write_credential("github", "acme/infra"))
        self.assertEqual(TOKEN, self.executor.mint_write_credential("github", "acme/web"))
        self.assertEqual([[credential_proxy.FORGE_WRITE_MINT_FLAG, credential_proxy.FORGE_HOST_FLAG, HOST, "acme/infra"]],
                         self.runs)
        self.assertEqual({("github", HOST, "acme")}, set(self.executor._write_tokens))

    def test_an_unmanaged_repository_is_refused_before_anything_runs(self):
        with self.assertRaises(PermissionError):
            self.executor.mint_write_credential("github", "acme/elsewhere")
        self.assertEqual([], self.runs)

    def test_a_repository_unregistered_since_the_mint_is_refused_from_the_cache(self):
        self.executor.mint_write_credential("github", "acme/infra")
        with mock.patch.object(credential_proxy, "managed_repositories", return_value=frozenset()):
            with self.assertRaises(PermissionError):
                self.executor.mint_write_credential("github", "acme/infra")

    def test_a_refused_git_call_drops_the_token_and_the_next_use_mints_anew(self):
        self.executor.mint_write_credential("github", "acme/infra")
        config = ((f"http.https://{HOST}/.extraheader", "AUTHORIZATION: basic x"), ("credential.helper", ""))
        # A failure that is not a refused credential keeps the token.
        self.executor._drop_write_tokens_refused_by(128, "fatal: repository not found", config)
        self.assertIn(("github", HOST, "acme"), self.executor._write_tokens)
        self.executor._drop_write_tokens_refused_by(
            128, "fatal: could not read Username for 'https://ghe.other.example': terminal prompts disabled", config)
        self.assertIn(("github", HOST, "acme"), self.executor._write_tokens)
        self.executor._drop_write_tokens_refused_by(
            128, f"fatal: could not read Username for 'https://{HOST}': terminal prompts disabled", config)
        self.assertNotIn(("github", HOST, "acme"), self.executor._write_tokens)
        self.executor.mint_write_credential("github", "acme/infra")
        self.assertEqual(2, len(self.runs))

    def test_two_organisations_each_keep_their_token(self):
        # A mint covers one organisation, so each keeps its own slot and
        # alternate requests do not mint again.
        with mock.patch.object(credential_proxy, "managed_repositories",
                               return_value=frozenset({f"github:{HOST}/acme/infra", f"github:{HOST}/other/x"})):
            for repo in ("acme/infra", "other/x", "acme/infra", "other/x"):
                self.executor.mint_write_credential("github", repo)
        self.assertEqual([["acme/infra"], ["other/x"]], [run[-1:] for run in self.runs])
        self.assertEqual({("github", HOST, "acme"), ("github", HOST, "other")}, set(self.executor._write_tokens))
        self.executor.drop_write_credential("github")
        self.assertEqual({}, self.executor._write_tokens)

    def test_drop_write_credential_forgets_the_forges_token(self):
        self.executor.mint_write_credential("github", "acme/infra")
        self.executor.drop_write_credential("github")
        self.assertEqual({}, self.executor._write_tokens)

    def test_the_flags_match_the_helpers(self):
        self.assertEqual(github_token_refresh.PRINT_WRITE_FLAG, credential_proxy.FORGE_WRITE_MINT_FLAG)
        self.assertEqual(github_token_refresh.HOST_FLAG, credential_proxy.FORGE_HOST_FLAG)


@unittest.skipUnless(OPENSSL and shutil.which("git"), "openssl and git are needed")
class RefusedGitCallTest(unittest.TestCase):
    """Real git, through the executor, against a local HTTPS server that answers 401.

    With the extraheader and the helper cleared, git answers a 401 by asking
    for a username. The executor turns prompts off on these calls, so git's
    refusal is stable, and the cached write token for the host is dropped.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        ca, cert, key = make_pki(self.dir / "pki", ("localhost",))
        self.ca = ca

        self.seen: list[str | None] = []
        seen = self.seen

        class Refuse(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the stdlib's name
                seen.append(self.headers.get("Authorization"))
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="mock"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.host = f"localhost:{server.server_address[1]}"
        self.url = f"https://{self.host}/acme/infra.git"
        self.executor = credential_proxy.CommandExecutor(
            timeout_seconds=20, max_output_bytes=1 << 16, state_dir=str(self.dir / "state"), scoped_pool=None
        )

    def test_a_401_from_git_drops_the_refusing_hosts_token_only(self):
        now = time.monotonic()
        self.executor._write_tokens[("github", self.host, "acme")] = (now, frozenset({"acme/infra"}), TOKEN)
        self.executor._write_tokens[("github", "ghe.other.example", "acme")] = (now, frozenset({"acme/infra"}), TOKEN)
        header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        config = (
            (f"http.https://{self.host}/.extraheader", header),
            ("http.https://ghe.other.example/.extraheader", header),
            ("credential.helper", ""),
            ("http.sslCAInfo", str(self.ca)),
        )
        self.executor.vcs_root.mkdir(parents=True, exist_ok=True)
        result = self.executor.execute_vcs_git(
            ["git", "ls-remote", self.url], self.executor.vcs_root, check=False, config=config
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(f"could not read Username for 'https://{self.host}'", result.stderr)
        self.assertIn("terminal prompts disabled", result.stderr)
        # The token was presented, and the server refused it.
        self.assertIn(header.split(": ", 1)[1].replace("basic", "Basic").lower(), [h.lower() for h in self.seen if h])
        self.assertNotIn(("github", self.host, "acme"), self.executor._write_tokens)
        self.assertIn(("github", "ghe.other.example", "acme"), self.executor._write_tokens)


class HelperPrintModeTest(unittest.TestCase):
    def test_it_mints_for_the_host_and_installs_nothing(self):
        keys = [f"github:{HOST}/acme/infra", f"github:{HOST}/acme/web", f"github:{HOST}/other/x",
                "github:github.com/acme/dotcom"]
        with mock.patch.object(github_token_refresh, "broker_oidc_token", return_value="oidc"), \
                mock.patch.object(github_token_refresh, "request_minty_token", return_value=TOKEN) as minty, \
                mock.patch.object(gitops_workspace, "get_managed_repo_keys", return_value=keys), \
                mock.patch.object(github_token_refresh.subprocess, "run") as run:
            answer = github_token_refresh.mint_write_token("acme/infra", HOST)
        self.assertEqual({"token": TOKEN, "repositories": ["acme/infra", "acme/web"]}, answer)
        minty.assert_called_once_with("oidc", "acme", ["infra", "web"], github_token_refresh.MINTY_WRITE_SCOPE)
        run.assert_not_called()

    def test_it_refuses_github_com_and_a_bad_repository(self):
        for repo, host in (("acme/infra", "github.com"), ("acme/infra", ""), ("not-a-slug", HOST)):
            with self.subTest(repo=repo, host=host), self.assertRaises(RuntimeError):
                github_token_refresh.mint_write_token(repo, host)


class ConsumerLinksTest(unittest.TestCase):
    """The links and names built from a repository follow the install's GitHub host."""

    def test_a_ledger_on_the_enterprise_host_is_found_and_named_with_it(self):
        import slack_audit_report

        report = f"Fleet audit: 2 new findings — https://{HOST}/acme/infra/issues/12"
        with mock.patch.object(slack_audit_report, "_github_host", return_value=HOST):
            ref = slack_audit_report.ledger_ref(report)
            dotcom = slack_audit_report.ledger_ref("Fleet audit: 2 new — https://github.com/acme/infra/issues/7")
            other = slack_audit_report.ledger_ref("Fleet audit: 2 new — https://ghe.other.example/acme/infra/issues/7")
        self.assertEqual((f"https://{HOST}/acme/infra/issues/12", f"{HOST}/acme/infra", 12), tuple(ref))
        self.assertEqual(("https://github.com/acme/infra/issues/7", "acme/infra", 7), tuple(dotcom))
        self.assertIsNone(other)
        # A github.com install does not read another host's issue as its ledger.
        with mock.patch.object(slack_audit_report, "_github_host", return_value="github.com"):
            self.assertIsNone(slack_audit_report.ledger_ref(report))

    def test_an_opened_pull_request_on_the_enterprise_host_is_a_moment(self):
        import slack_moments

        text = f"Opened PR https://{HOST}/acme/infra/pull/41"
        self.assertEqual(f"https://{HOST}/acme/infra/pull/41", slack_moments.opened_pr(text, github_host=HOST)[0])
        self.assertIsNone(slack_moments.opened_pr(text, github_host="github.com"))
        self.assertIsNone(slack_moments.opened_pr("Opened PR https://ghe.other.example/a/b/pull/1", github_host=HOST))
        self.assertEqual("https://github.com/acme/infra/pull/9",
                         slack_moments.opened_pr("Opened PR https://github.com/acme/infra/pull/9", github_host=HOST)[0])

    def test_github_org_binds_an_enterprise_repository(self):
        with mock.patch.dict(os.environ, {"GITHUB_ORG": "acme"}), \
                mock.patch.object(gitops_workspace, "github_host", return_value=HOST):
            os.environ.pop("GITOPS_ORG", None)
            self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.validate_repo_org(f"{HOST}/acme/infra"))
            with self.assertRaises(ValueError):
                gitops_workspace.validate_repo_org(f"{HOST}/other/infra")

    def test_other_github_com_spellings_keep_mains_org_answer(self):
        # On main, `www.github.com/...` and `ssh.github.com/...` are not held
        # to GITHUB_ORG; this change applies the org only to the instance.
        for host in (HOST, "github.com"):
            with self.subTest(install_host=host), mock.patch.dict(os.environ, {"GITHUB_ORG": "acme"}), \
                    mock.patch.object(gitops_workspace, "github_host", return_value=host):
                os.environ.pop("GITOPS_ORG", None)
                for name in ("www.github.com/other/x", "ssh.github.com/other/x"):
                    self.assertEqual(name, gitops_workspace.validate_repo_org(name))

    def test_the_sandbox_refresh_names_the_repository_on_the_install_host(self):
        seen = []

        class Answer:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def urlopen(request, timeout=None):
            seen.append(json.loads(request.data))
            return Answer()

        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://broker"}), \
                mock.patch.object(gitops_workspace, "github_host", return_value=HOST), \
                mock.patch.object(github_token_refresh.urllib.request, "urlopen", side_effect=urlopen):
            github_token_refresh.refresh_git_credentials("acme/infra")
        self.assertEqual([{"provider": "github", "repository": f"https://{HOST}/acme/infra"}], seen)


class ContentWorkspaceTest(unittest.TestCase):
    def test_a_bare_name_is_placed_on_the_install_github_host(self):
        from content_workspace import ContentWorkspaceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "agent").mkdir()
        store = ContentWorkspaceStore(root / "trees", root / "agent", lambda *a, **k: None, github_host=lambda: HOST)
        self.assertEqual((f"{HOST}/acme/infra", f"https://{HOST}/acme/infra.git"), store._where("acme/infra"))
        dotcom = ContentWorkspaceStore(root / "trees2", root / "agent", lambda *a, **k: None)
        self.assertEqual(("acme/infra", "https://github.com/acme/infra.git"), dotcom._where("acme/infra"))

    def test_the_broker_locates_an_enterprise_repository_with_its_host(self):
        registry = providers.Registry({"forges": [{"provider": "github", "host": HOST}]})
        with mock.patch.object(credential_proxy, "forge_registry", return_value=registry):
            self.assertEqual((f"{HOST}/acme/infra", f"https://{HOST}/acme/infra.git"),
                             credential_proxy._workspace_locate(f"{HOST}/acme/infra"))
            self.assertEqual(HOST, credential_proxy._workspace_github_host())
            self.assertIs(registry.forges[0], credential_proxy._provider_forge("github"))
        dotcom = providers.Registry({"forges": [{"provider": "github", "host": "github.com"}]})
        with mock.patch.object(credential_proxy, "forge_registry", return_value=dotcom):
            self.assertEqual(("acme/infra", "https://github.com/acme/infra.git"),
                             credential_proxy._workspace_locate("github.com/acme/infra"))
            self.assertEqual("github.com", credential_proxy._workspace_github_host())


WRITE_TOKEN = "mock-write-token-0002"
READ_TOKEN = "mock-read-token-0003"
EXTRAHEADER = f"http.https://{HOST}/.extraheader"


def _basic(token: str) -> str:
    return "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                               "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@example.com"})


class _EnterpriseGitCase(unittest.TestCase):
    """A GitHub Enterprise Server install whose repositories are local bare repositories.

    Every git the broker runs is recorded with the configuration its
    credential asked for, then run for real with the instance's clone URL
    pointed at a local bare repository. So the assertion is on what git was
    actually handed, on the invocations that reach the remote.
    """

    REMOTE_SUBCOMMANDS = ("clone", "fetch", "push", "ls-remote")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.origins = {}
        for repo in ("acme/infra", "acme/docs"):
            origin = self.dir / "origins" / repo
            origin.mkdir(parents=True)
            _git(origin, "init", "--quiet", "--bare", "--initial-branch=main")
            seed = self.dir / "seed" / repo
            seed.mkdir(parents=True)
            _git(seed, "init", "--quiet", "--initial-branch=main")
            (seed / "README.md").write_text("origin\n")
            _git(seed, "add", "-A")
            _git(seed, "commit", "--quiet", "-m", "first")
            _git(seed, "push", "--quiet", str(origin), "main")
            self.origins[f"https://{HOST}/{repo}.git"] = str(origin)
        config = self.dir / "forges.json"
        config.write_text(json.dumps({"forges": [{"provider": "github", "host": HOST}]}))
        env = mock.patch.dict(os.environ, {FORGES_CONFIG_ENV: str(config)})
        env.start()
        self.addCleanup(env.stop)
        # The module-level registry the routes share is built from the same file.
        previous = credential_proxy._forge_registry
        credential_proxy._forge_registry = None
        self.addCleanup(setattr, credential_proxy, "_forge_registry", previous)
        self.calls: list[tuple[str, dict]] = []

    def run_git(self, argv, cwd, config=()):
        """Record the call, then run it with the instance's URLs on local paths."""
        subcommand = next((a for a in argv[1:] if not a.startswith("-")), "")
        self.calls.append((subcommand, dict(config)))
        argv = [self.origins.get(a, a) for a in argv]
        flags = [f for key, value in config for f in ("-c", f"{key}={value}")]
        return subprocess.run([argv[0], *flags, *argv[1:]], cwd=str(cwd), capture_output=True, text=True,
                              env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                                   "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                                   "GIT_COMMITTER_EMAIL": "t@example.com"})

    def remote_calls(self) -> list[tuple[str, dict]]:
        return [(sub, config) for sub, config in self.calls if sub in self.REMOTE_SUBCOMMANDS]


class VcsBrokerGitTest(_EnterpriseGitCase):
    """`clone` and `publish` through the broker hand git the minted token on the host."""

    def broker(self):
        minted = []

        def mint_write(provider, repo):
            minted.append(repo)
            return WRITE_TOKEN

        def git_runner(argv, cwd, check=True, config=()):
            done = self.run_git(argv, cwd, config)
            if check and done.returncode != 0:
                raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
            return done

        broker = vcs_broker.VcsBroker(self.dir / "scratch", git_runner=git_runner, mint_write=mint_write)
        forge = broker.registry.forges[0]
        # The forge composes the instance's URL; the runner maps it to a local repository.
        self.assertEqual(f"https://{HOST}/acme/infra.git", forge.clone_url("acme/infra"))
        return broker, minted

    def test_clone_and_publish_present_the_write_token_to_git_on_the_host(self):
        broker, minted = self.broker()
        answer = broker.clone({"repository": f"https://{HOST}/acme/infra"})
        work = self.dir / "work"
        bundle = self.dir / "in.bundle"
        bundle.write_bytes(base64.b64decode(answer["bundleBase64"]))
        _git(self.dir, "clone", "--quiet", "--branch", answer["branch"], str(bundle), str(work))
        _git(work, "checkout", "--quiet", "-b", "fix/replicas")
        (work / "README.md").write_text("changed\n")
        _git(work, "commit", "--quiet", "-am", "change it")
        out = self.dir / "out.bundle"
        _git(work, "bundle", "create", str(out), "fix/replicas", f"^{answer['revision']}")
        broker.publish({
            "repository": f"https://{HOST}/acme/infra",
            "branch": "fix/replicas",
            "target": "main",
            "baseRevision": answer["revision"],
            "bundleBase64": base64.b64encode(out.read_bytes()).decode(),
        })
        remote = self.remote_calls()
        self.assertIn("clone", [sub for sub, _ in remote])
        self.assertIn("push", [sub for sub, _ in remote])
        for sub, config in remote:
            with self.subTest(subcommand=sub):
                self.assertEqual(_basic(WRITE_TOKEN), config.get(EXTRAHEADER))
                self.assertEqual("", config.get("credential.helper"))
                self.assertEqual({EXTRAHEADER, f"http.https://{HOST}/.followRedirects", "credential.helper"}, set(config))
                self.assertEqual("false", config[f"http.https://{HOST}/.followRedirects"])
        self.assertEqual({"acme/infra"}, set(minted))
        tip = _git(self.origins[f"https://{HOST}/acme/infra.git"], "rev-parse", "refs/heads/fix/replicas").stdout
        self.assertTrue(tip.strip())


class ContentWorkspaceGitTest(_EnterpriseGitCase):
    """`build_workspace_store` opens a managed and a context repository with the right token."""

    def setUp(self):
        super().setUp()
        executor = credential_proxy.CommandExecutor(
            timeout_seconds=5, max_output_bytes=1 << 16, state_dir=str(self.dir / "state"), scoped_pool=None
        )
        self.helper_runs: list[list[str]] = []

        def run_helper(provider, helper, arguments, action, log_success=False):
            self.helper_runs.append(arguments)
            if arguments[0] == credential_proxy.FORGE_READ_ONLY_FLAG:
                stdout = READ_TOKEN
            else:
                stdout = json.dumps({"token": WRITE_TOKEN, "repositories": ["acme/infra"]})
            return credential_proxy.ExecutionResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1,
                                                    truncated=False, timed_out=False)

        def workspace_git(argv, cwd, config=()):
            done = self.run_git(argv, cwd, config)
            return credential_proxy.ExecutionResult(exit_code=done.returncode, stdout=done.stdout,
                                                    stderr=done.stderr, duration_ms=1, truncated=False,
                                                    timed_out=False)

        helper = self.dir / "github_token_refresh.py"
        helper.write_text("")
        for patcher in (
            mock.patch.object(credential_proxy, "managed_repositories",
                              return_value=frozenset({f"github:{HOST}/acme/infra"})),
            mock.patch.object(credential_proxy, "context_repositories",
                              return_value=frozenset({f"github:{HOST}/acme/docs"})),
            mock.patch.object(credential_proxy.CommandExecutor, "_forge_helper", return_value=helper),
            mock.patch.object(credential_proxy.CommandExecutor, "_run_forge_helper", side_effect=run_helper),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = credential_proxy.build_workspace_store(types.SimpleNamespace(
            content_workspace_root=self.dir / "trees",
            workspace_dir=self.dir / "agent",
            execute_workspace_git=workspace_git,
            mint_read_credential=executor.mint_read_credential,
            mint_write_credential=executor.mint_write_credential,
            drop_write_credential=executor.drop_write_credential,
            tls_refusal=executor.tls_refusal,
        ))
        (self.dir / "agent").mkdir()

    def test_a_managed_repository_opens_and_pushes_on_the_write_token(self):
        from content_workspace import Change, repo_relative

        workspace = self.store.open("acme/infra")
        self.assertEqual(f"{HOST}/acme/infra", workspace.repo)
        self.store.commit(workspace.handle, "platform-agent/change", "feat: a change",
                          [Change(repo_relative("notes.md"), b"hello\n")])
        credential_proxy.require_managed_workspace(self.store, workspace.handle)
        self.store.push(workspace.handle, "platform-agent/change")
        remote = self.remote_calls()
        self.assertIn("clone", [sub for sub, _ in remote])
        self.assertIn("push", [sub for sub, _ in remote])
        for sub, config in remote:
            with self.subTest(subcommand=sub):
                self.assertEqual(_basic(WRITE_TOKEN), config.get(EXTRAHEADER))
        # The mint was asked about the forge's path, never the store's host-qualified name.
        self.assertTrue(self.helper_runs)
        for arguments in self.helper_runs:
            self.assertEqual("acme/infra", arguments[-1])

    def test_a_context_repository_opens_on_a_read_token_and_never_the_write_one(self):
        workspace = self.store.open(f"{HOST}/acme/docs")
        self.assertEqual(f"{HOST}/acme/docs", workspace.repo)
        [(sub, config)] = [c for c in self.remote_calls() if c[0] == "clone"]
        self.assertEqual(_basic(READ_TOKEN), config.get(EXTRAHEADER))
        self.assertEqual([[credential_proxy.FORGE_READ_ONLY_FLAG, "acme/docs"]], self.helper_runs)


BOT = "kube-agents[bot]"


def _pull(number: int, branch: str, author: str, state: str, sha: str) -> dict:
    """A pull request as the instance answers it, trimmed to what the translation reads."""
    return {
        "number": number, "state": "closed" if state == "merged" else state, "title": "t",
        "user": {"login": author, "type": "Bot" if author.endswith("[bot]") else "User"},
        "body": "", "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
        "closed_at": None if state == "open" else "2026-10-02T00:00:00Z",
        "merged_at": "2026-10-02T00:00:00Z" if state == "merged" else None, "draft": False,
        "html_url": f"https://{HOST}/acme/infra/pull/{number}",
        "head": {"ref": branch, "sha": sha, "repo": {"full_name": "acme/infra"}},
        "base": {"ref": "main", "sha": "0" * 40},
    }


class OwnershipAgainstTheMockTest(MockGitHubEnterpriseCase, _EnterpriseGitCase):
    """The ownership checks hold on a GitHub Enterprise Server.

    The mock answers GraphQL `viewer { login }` at /api/graphql with the App's
    bot login, as GitHub answers it for an installation token, and the pull
    requests by the query each call sends. git reaches a local bare repository.
    """

    def setUp(self):
        MockGitHubEnterpriseCase.setUp(self)
        _EnterpriseGitCase.setUp(self)
        self.pulls_open: list[dict] = []
        self.pulls_all: list[dict] = []
        self.viewer: object = {"data": {"viewer": {"login": BOT}}}
        mock_server = self.mock
        test = self
        original = mock_server.server.RequestHandlerClass._answer

        def routed(handler):
            if handler.path == "/api/graphql" and handler.command == "POST":
                length = int(handler.headers.get("Content-Length") or 0)
                body = json.loads(handler.rfile.read(length) or b"{}")
                mock_server.requests.append({"method": "POST", "path": handler.path, "body": body,
                                             "authorization": handler.headers.get("Authorization"),
                                             "host": handler.headers.get("Host"), "accept": None})
                return handler._json(200, test.viewer)
            if handler.command == "GET" and handler.path.startswith("/api/v3/repos/acme/infra/pulls?"):
                mock_server.requests.append({"method": "GET", "path": handler.path, "body": None,
                                             "authorization": handler.headers.get("Authorization"),
                                             "host": handler.headers.get("Host"), "accept": None})
                query = handler.path.split("?", 1)[1]
                if "base=" in query:
                    return handler._json(200, [])
                return handler._json(200, test.pulls_open if "state=open" in query else test.pulls_all)
            return original(handler)

        handler_class = mock_server.server.RequestHandlerClass
        handler_class.do_GET = handler_class.do_POST = routed

    def broker(self):
        config = self.dir / "forges.json"
        config.write_text(json.dumps({"forges": [{"provider": "github", "host": HOST, "caFile": str(self.ca)}]}))
        os.environ[FORGES_CONFIG_ENV] = str(config)

        def git_runner(argv, cwd, check=True, config=()):
            done = self.run_git(argv, cwd, config)
            if check and done.returncode != 0:
                raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
            return done

        return vcs_broker.VcsBroker(self.dir / "scratch", git_runner=git_runner,
                                    mint_write=lambda provider, repo: WRITE_TOKEN, http_timeout=10.0)

    def push_branch(self, branch: str) -> str:
        """Put `branch` on the instance's repository, as a person's push would."""
        origin = self.origins[f"https://{HOST}/acme/infra.git"]
        work = self.dir / "person"
        if not work.exists():
            _git(self.dir, "clone", "--quiet", origin, str(work))
        _git(work, "checkout", "--quiet", "-B", branch, "origin/main")
        (work / "theirs.txt").write_text(branch + "\n")
        _git(work, "add", "-A")
        _git(work, "commit", "--quiet", "-m", "theirs")
        _git(work, "push", "--quiet", "--force", "origin", branch)
        return _git(work, "rev-parse", "HEAD").stdout.strip()

    def test_identity_names_the_installation_login(self):
        answer = self.broker().identity({"repository": REPO_URL})
        self.assertEqual(BOT, answer["identity"]["login"])
        [graphql] = [r for r in self.mock.requests if r["path"] == "/api/graphql"]
        self.assertEqual(f"Bearer {WRITE_TOKEN}", graphql["authorization"])
        self.assertIn("viewer", graphql["body"]["query"])

    def test_a_missing_login_is_refused_not_read_as_no_login(self):
        self.viewer = {"data": None, "errors": [{"message": "Resource not accessible by integration"}]}
        with self.assertRaises(WorkspaceError) as caught:
            self.broker().identity({"repository": REPO_URL})
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_advance_onto_a_strangers_open_proposal_is_refused(self):
        broker = self.broker()
        branch = "release-1.2"
        self.push_branch(branch)
        answer = broker.clone({"repository": REPO_URL, "branch": branch})
        work = self.dir / "agent-work"
        bundle = self.dir / "agent.bundle"
        bundle.write_bytes(base64.b64decode(answer["bundleBase64"]))
        _git(self.dir, "clone", "--quiet", "--branch", branch, str(bundle), str(work))
        (work / "mine.txt").write_text("mine\n")
        _git(work, "add", "-A")
        _git(work, "commit", "--quiet", "-m", "mine")
        out = self.dir / "agent-out.bundle"
        _git(work, "bundle", "create", str(out), branch, f"^{answer['revision']}")
        self.pulls_open = [_pull(7, branch, "a-person", "open", answer["revision"])]
        with self.assertRaises(WorkspaceError) as caught:
            broker.publish({
                "repository": REPO_URL, "branch": branch, "target": "main", "advance": True,
                "baseRevision": answer["revision"],
                "bundleBase64": base64.b64encode(out.read_bytes()).decode(),
            })
        self.assertEqual("CLONED_BRANCH", caught.exception.fields["code"])
        self.assertIn("a-person", str(caught.exception))
        tip = _git(self.origins[f"https://{HOST}/acme/infra.git"], "rev-parse", f"refs/heads/{branch}").stdout.strip()
        self.assertEqual(answer["revision"], tip)

    def test_branch_delete_refuses_a_branch_a_stranger_proposed_from(self):
        branch = "platform-agent/someone-elses"
        tip = self.push_branch(branch)
        self.pulls_all = [_pull(9, branch, "a-person", "merged", tip)]
        with self.assertRaises(WorkspaceError) as caught:
            self.broker().branch_delete({"repository": REPO_URL, "branch": branch, "revision": tip})
        self.assertEqual("BRANCH_NOT_OURS", caught.exception.fields["code"])
        self.assertIn("a-person", str(caught.exception))
        still = _git(self.origins[f"https://{HOST}/acme/infra.git"], "rev-parse", f"refs/heads/{branch}").stdout.strip()
        self.assertEqual(tip, still)

    def test_branch_delete_removes_a_spent_branch_the_installation_opened(self):
        branch = "platform-agent/mine"
        tip = self.push_branch(branch)
        self.pulls_all = [_pull(10, branch, BOT, "merged", tip)]
        answer = self.broker().branch_delete({"repository": REPO_URL, "branch": branch, "revision": tip})
        self.assertTrue(answer["branch"]["deleted"])


class RedirectPinTest(unittest.TestCase):
    """Every git call a minted token rides follows no redirect from the forge's host."""

    def test_the_write_and_read_credentials_pin_follow_redirects_off(self):
        for credential in (
            MintedWriteCredential("github", lambda provider, repo: TOKEN, HOST),
            providers.Registry({"forges": [{"provider": "github", "host": HOST}],
                                "mint": lambda provider, repo: TOKEN}).forges[0].read_credential("acme/infra"),
        ):
            with self.subTest(credential=type(credential).__name__):
                credential.ensure("acme/infra")
                config = dict(credential.git_config("acme/infra"))
                self.assertEqual("false", config[f"http.https://{HOST}/.followRedirects"])
                self.assertIn(EXTRAHEADER, config)

    def test_github_com_keeps_its_redirect_behaviour(self):
        # A repository renamed on github.com still clones on the read token.
        credential = providers.Registry({"forges": [{"provider": "github", "host": "github.com"}],
                                         "mint": lambda provider, repo: TOKEN}).forges[0].read_credential("acme/infra")
        credential.ensure("acme/infra")
        config = credential.git_config("acme/infra")
        self.assertEqual(
            ("http.https://github.com/.extraheader", "credential.helper"),
            tuple(key for key, _ in config),
        )


class HostNormalisationTest(unittest.TestCase):
    def test_a_host_is_lowered_and_stripped_before_it_is_judged(self):
        [forge] = providers.Registry({"forges": [{"provider": "github", "host": " GHE.Example.COM "}]}).forges
        self.assertEqual((HOST.replace("mock.test", "example.com"),), forge.hosts)
        for host in (" GitHub.com ", "SSH.GITHUB.COM"):
            with self.subTest(host=host):
                forge = providers.GitHubForge(host=host) if hasattr(providers, "GitHubForge") else \
                    next(c for c in providers.AVAILABLE if c.name == "github")(host=host)
                self.assertEqual("cli", forge.transport)
        with self.assertRaises(ValueError):
            next(c for c in providers.AVAILABLE if c.name == "github")(host=" API.GitHub.com ")


class EnterpriseRepositoryReadersTest(unittest.TestCase):
    """The readers that pick repositories to act on name the instance's repositories."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "managed_repos").write_text(json.dumps([{"type": "github", "url": f"https://{HOST}/acme/infra"}]))
        (root / "context_repos").write_text(json.dumps([
            {"type": "github", "url": f"https://{HOST}/acme/docs", "ref": "release"},
        ]))
        (root / gitops_workspace.GITHUB_HOST_KEY).write_text(HOST)
        patcher = mock.patch.dict(os.environ, {"GITOPS_STATE_PATH": str(root / "managed_repos")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_context_entries_carry_the_instances_repository_and_its_ref(self):
        entries = gitops_workspace.get_context_github_repo_entries()
        self.assertEqual([(f"{HOST}/acme/docs", "release")], [(e["repo"], e["ref"]) for e in entries])

    def test_the_picker_names_the_instances_repository_and_the_bare_reader_stays_empty(self):
        self.assertEqual([f"{HOST}/acme/infra"], gitops_workspace.get_managed_github_repo_names())
        self.assertEqual([], gitops_workspace.get_managed_github_repos())

    def test_the_delivery_ledger_is_the_instances_repository(self):
        import chat_delivery_watch

        with mock.patch.dict(os.environ):
            os.environ.pop(chat_delivery_watch.LEDGER_REPO_ENV, None)
            self.assertEqual(f"{HOST}/acme/infra", chat_delivery_watch.ledger_repo())


class GitHubDotComEntriesOnAnEnterpriseInstallTest(unittest.TestCase):
    """On a GitHub Enterprise Server install, a github.com or bare GitHub entry is skipped."""

    def test_they_are_neither_named_nor_keyed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "managed_repos").write_text("[]")
        (root / gitops_workspace.GITHUB_HOST_KEY).write_text(HOST)
        entries = [
            {"type": "github", "url": "https://github.com/other/repo"},
            {"type": "github", "url": "acme/bare"},
            {"type": "github", "url": f"https://{HOST}/acme/infra"},
        ]
        with mock.patch.dict(os.environ, {"GITOPS_STATE_PATH": str(root / "managed_repos")}), \
                self.assertLogs(gitops_workspace.LOGGER, level="WARNING") as logs:
            names = gitops_workspace._forge_repo_names(entries, gitops_workspace.MANAGED_REPOS_KEY)
            keys = gitops_workspace._repository_keys(entries, gitops_workspace.MANAGED_REPOS_KEY)
            slugs = gitops_workspace._github_slugs(entries, gitops_workspace.MANAGED_REPOS_KEY, fold_case=False)
        self.assertEqual([f"{HOST}/acme/infra"], names)
        self.assertEqual([f"github:{HOST}/acme/infra"], keys)
        self.assertEqual([], slugs)
        self.assertIn("this install's GitHub host is " + HOST, "\n".join(logs.output))


class BrokerHostCheckTest(unittest.TestCase):
    def test_github_owned_hosts_never_build_an_enterprise_forge(self):
        for host in ("api.github.com", "gist.github.com", "raw.githubusercontent.com", "acme.ghe.com",
                     "acme.github.io", "githubassets.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                providers.Registry({"forges": [{"provider": "github", "host": host}]})
        # Every spelling of github.com is the github.com forge, ssh.github.com included.
        [forge] = providers.Registry({"forges": [{"provider": "github", "host": "ssh.github.com"}]}).forges
        self.assertEqual(("cli", ("github.com", "www.github.com")), (forge.transport, forge.hosts))


    def test_github_com_with_a_port_never_builds_an_enterprise_forge(self):
        # A port does not make github.com, or a host GitHub runs, a GitHub Enterprise
        # Server host: no minted write token and no private CA for it.
        for host in ("github.com:443", "GITHUB.COM:8443", "www.github.com:443", "api.github.com:443",
                     "acme.ghe.com:8443"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                providers.Registry({"forges": [{"provider": "github", "host": host}]})
        # A real instance on a port is still an instance.
        [forge] = providers.Registry({"forges": [{"provider": "github", "host": "ghe.example.com:8443"}]}).forges
        self.assertEqual(("http", ("ghe.example.com:8443",)), (forge.transport, forge.hosts))


def _load_skill_script(skill: str, module: str):
    """A skill script, loaded by path as its own tests load it."""
    import importlib.util
    import sys

    scripts = Path(__file__).resolve().parent.parent / "skills" / skill / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(module, scripts / f"{module}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class BareNameOnTheEnterpriseHostTest(unittest.TestCase):
    """A bare `owner/name` resolves to the install's GitHub host (live finding F4).

    The managed list names each repository on the instance `<host>/owner/name`.
    A `--repo owner/name` from a cron or an operator was refused against it,
    because `qualify` lifted a bare name to github.com only.
    """

    def state(self, entries: list[dict[str, str]], github_host: str | None = HOST) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "managed_repos").write_text(json.dumps(entries))
        if github_host is not None:
            (root / gitops_workspace.GITHUB_HOST_KEY).write_text(github_host + "\n")
        patcher = mock.patch.dict(os.environ, {"GITOPS_STATE_PATH": str(root / "managed_repos")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GITOPS_ORG", "GITHUB_ORG"):
            os.environ.pop(name, None)
        return root

    def test_a_bare_name_qualifies_to_the_install_host(self):
        self.state([{"type": "github", "url": f"https://{HOST}/acme/infra"}])
        self.assertEqual([f"{HOST}/acme/infra"], gitops_workspace.get_managed_repos())
        self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.qualify("acme/infra"))
        # GitHub names fold case; the answer is the list's own spelling, which the gates compare.
        self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.qualify("Acme/Infra"))
        # A name on the host already, and a repository the list does not hold, are unchanged.
        self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.qualify(f"{HOST}/acme/infra"))
        self.assertEqual("acme/other", gitops_workspace.qualify("acme/other"))

    def test_a_github_com_name_is_not_moved_to_the_install_host(self):
        self.state([{"type": "github", "url": f"https://{HOST}/acme/infra"}])
        self.assertEqual("acme/infra", gitops_workspace.qualify("github.com/acme/infra"))
        submit_suggestion = _load_skill_script("submit-suggestion", "submit_suggestion")
        with self.assertRaises(ValueError) as caught:
            submit_suggestion.validate_repo("github.com/acme/infra")
        self.assertIn("not in the managed repositories list", str(caught.exception))

    def test_submit_suggestion_and_the_audit_take_a_bare_repo(self):
        self.state([{"type": "github", "url": f"https://{HOST}/acme/infra"}])
        os.environ["GITHUB_ORG"] = "acme"
        submit_suggestion = _load_skill_script("submit-suggestion", "submit_suggestion")
        self.assertEqual(f"{HOST}/acme/infra", submit_suggestion.validate_repo("acme/infra"))
        audit_report = _load_skill_script("fleet-audit", "audit_report")
        self.assertEqual(f"{HOST}/acme/infra", audit_report.resolve_repo(repo="acme/infra"))

    def test_a_workspace_and_a_lease_record_read_back_on_the_install_host(self):
        root = self.state([{"type": "github", "url": f"https://{HOST}/acme/infra"}])
        holder = gitops_workspace.lease_dir(root / "ws", "l")
        holder.mkdir(parents=True)
        gitops_workspace.write_lease(holder, "l", "acme/infra")
        tree = holder / "acme__infra"
        tree.mkdir()
        self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.resolve_repo(workspace=tree))
        self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.resolve_repo(workspace=holder))

    def test_a_bare_name_two_forges_hold_stays_bare_and_is_refused(self):
        self.state([
            {"type": "github", "url": f"https://{HOST}/acme/infra"},
            {"type": "gitlab", "url": "https://gitlab.example.com/acme/infra"},
        ])
        self.assertEqual("acme/infra", gitops_workspace.qualify("acme/infra"))
        submit_suggestion = _load_skill_script("submit-suggestion", "submit_suggestion")
        with self.assertRaises(ValueError) as caught:
            submit_suggestion.validate_repo("acme/infra")
        self.assertIn("not in the managed repositories list", str(caught.exception))
        # Each forge's repository is still taken by its host.
        self.assertEqual(f"{HOST}/acme/infra", gitops_workspace.qualify(f"{HOST}/acme/infra"))
        self.assertEqual("gitlab.example.com/acme/infra", gitops_workspace.qualify("gitlab.example.com/acme/infra"))

    def test_a_github_com_install_qualifies_as_before(self):
        self.state([{"type": "github", "url": "https://github.com/acme/infra"}], github_host=None)
        self.assertEqual("acme/infra", gitops_workspace.qualify("acme/infra"))
        self.assertEqual("acme/infra", gitops_workspace.qualify("github.com/acme/infra"))
        # The list is passed in, so the key is the only thing that differs.
        collided = ["github.com/acme/infra", "gitlab.com/acme/infra"]
        self.assertEqual("github.com/acme/infra", gitops_workspace.qualify("acme/infra", collided))
        self.assertEqual("acme/infra", gitops_workspace.qualify("acme/infra", [f"{HOST}/acme/infra"]))


class GitHubDotComUnaffectedTest(unittest.TestCase):
    def test_a_github_com_install_builds_the_cli_forge_as_before(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(FORGES_CONFIG_ENV, None)
            registry = providers.Registry({"refresh": lambda p, r: None, "mint_write": lambda p, r: TOKEN})
        [forge] = registry.forges
        self.assertEqual(("cli", "gh", ("github.com", "www.github.com")), (forge.transport, forge.cli, forge.hosts))
        self.assertIsInstance(forge.credential, providers.BrokeredCredential)
        self.assertEqual(("gh",), registry.executables)
        self.assertEqual("https://github.com/acme/infra.git", forge.clone_url("acme/infra"))
        with mock.patch.dict(os.environ, {"GITOPS_STATE_PATH": "/nonexistent/managed_repos"}):
            self.assertEqual("github.com", gitops_workspace.github_host())


if __name__ == "__main__":
    unittest.main()
