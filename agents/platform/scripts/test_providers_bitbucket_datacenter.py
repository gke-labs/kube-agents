#!/usr/bin/env python3
"""The Bitbucket Data Center forge (`bitbucket-datacenter`), tested against a mock instance.

    cd agents/platform/scripts && python3 -m unittest test_providers_bitbucket_datacenter

The mock is a local HTTPS server. Its private CA, key and certificate are made
with the `openssl` binary when the module starts (`make_pki`, from the GitHub
Enterprise Server test), so no key is kept in the repository. It answers the
REST API under `rest/api/latest` and the `whoami` servlet at
`plugins/servlet/applinks/whoami`, from a table that each test fills. The
broker reaches it at its own hostname, `bitbucket.mock.test`, with the CA in
the forge config (`caFile`). The test sends that name's connections to the
loopback port of the mock, so TLS checks the certificate against the real
hostname. Everything above the socket is real: the broker, the registry, the
forge config file, the transport, the CA trust, the error mapping and the
forge package.

What is pinned: how a repository URL is read (`KEY/slug`, the clone URL, the
web URL), and that a personal repository is refused by name; the forge config
rules; the CA trust, and the token only as a `Bearer` header; the identity
from the configured value or the servlet; write access from the user search;
the `start`/`limit` pages, and that a broken page cannot loop; the
pull-request verbs, with the `version` that a write must carry and one retry
on a 409; times from epoch milliseconds; the Data Center error texts; label
fields refused before a call; the read-only token; and git's refusal text.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import providers
import vcs_broker
from providers import transport as transport_module
from providers.base import IdentityUnknown, LabelsUnsupported
from providers.errors import GUIDANCE, UNAVAILABLE
from providers.registry import FORGES_CONFIG_ENV
from test_providers_github_enterprise import make_pki
from workspace_paths import WorkspaceError

# From the package surface: no test reaches into a forge package (test_providers_boundary).
BitbucketDataCenterForge = next(cls for cls in providers.AVAILABLE if cls.name == "bitbucket-datacenter")
#: The Data Center texts, as the forge declares them.
TOKEN_REFUSED_TEXT = getattr(BitbucketDataCenterForge.error_overrides.get(401), "text", None)
GIT_REFUSED_TEXT = BitbucketDataCenterForge.git_refused

OPENSSL = shutil.which("openssl")
HOST = "bitbucket.mock.test"
REPO_URL = f"https://{HOST}/projects/ACME/repos/infra"
REST = "rest/api/latest"
REPO = f"{REST}/projects/ACME/repos/infra"
PRS = f"{REPO}/pull-requests"
WHOAMI = "plugins/servlet/applinks/whoami"
TOKEN = "mock-datacenter-write-token-0001"
READ_TOKEN = "mock-datacenter-read-token-0002"
DIFF = "diff --git a/x b/x\n+one\n"


# -- the PKI, made once for the module ----------------------------------------

_PKI: dict[str, Path] = {}
_PKI_DIR: tempfile.TemporaryDirectory | None = None


def setUpModule():
    global _PKI_DIR
    if not OPENSSL:
        return
    _PKI_DIR = tempfile.TemporaryDirectory()
    root = Path(_PKI_DIR.name)
    _PKI["ca"], _PKI["cert"], _PKI["key"] = make_pki(root / "pki", (HOST,))
    _PKI["other_ca"], _, _ = make_pki(root / "other-pki", (HOST,))


def tearDownModule():
    if _PKI_DIR is not None:
        _PKI_DIR.cleanup()


# -- the mock -------------------------------------------------------------------


class Status:
    """An answer with a status other than 200, and a JSON body."""

    def __init__(self, status: int, body=None) -> None:
        self.status = status
        self.body = body if body is not None else {"errors": [{"message": f"mock {status}"}]}


class Text:
    """A 200 answer with a text/plain body."""

    def __init__(self, text: str) -> None:
        self.text = text


class MockDataCenter:
    """A mock Bitbucket Data Center instance over HTTPS.

    `routes` maps (method, path) to an answer. The path has no leading slash
    and no query. An answer is a JSON value (200), a `Status`, a `Text`, or a
    callable that gets the request record and gives one of these. A list of
    answers is used one per call, in order. A request with no route gets a
    500, and the test fails on it (`unrouted`). `redirect_to` makes every
    request answer 302 to that URL.
    """

    def __init__(self, cert: Path, key: Path) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[dict] = []
        self.unrouted: list[str] = []
        self.redirect_to = ""
        mock_server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                split = urllib.parse.urlsplit(self.path)
                record = {
                    "method": self.command,
                    "path": split.path.lstrip("/"),
                    "query": urllib.parse.parse_qs(split.query, keep_blank_values=True),
                    "raw_path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "accept": self.headers.get("Accept"),
                    "host": self.headers.get("Host"),
                    "raw_body": raw,
                    "body": json.loads(raw) if raw else None,
                }
                mock_server.requests.append(record)
                if mock_server.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", mock_server.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                answer = mock_server.routes.get((self.command, record["path"]))
                if isinstance(answer, list):
                    answer = answer.pop(0) if answer else None
                if answer is None:
                    mock_server.unrouted.append(f"{self.command} {self.path}")
                    return self._send(500, "application/json", b'{"errors": [{"message": "no route"}]}')
                if callable(answer):
                    answer = answer(record)
                if isinstance(answer, Text):
                    return self._send(200, "text/plain", answer.text.encode())
                status, body = (answer.status, answer.body) if isinstance(answer, Status) else (200, answer)
                self._send(status, "application/json", json.dumps(body).encode())

            def _send(self, status: int, kind: str, data: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _answer

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        # A short poll, so that `close` does not wait half a second per test.
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def calls(self) -> list[tuple[str, str]]:
        return [(r["method"], r["path"]) for r in self.requests]


def paged(rows: list, default_limit: int = 25):
    """Data Center's pages: `start`, `limit`, `isLastPage` and `nextPageStart`."""

    def answer(request: dict):
        start = int((request["query"].get("start") or ["0"])[0])
        limit = int((request["query"].get("limit") or [str(default_limit)])[0])
        chunk = rows[start:start + limit]
        last = start + limit >= len(rows)
        page = {"size": len(chunk), "limit": limit, "start": start, "isLastPage": last, "values": chunk}
        if not last:
            page["nextPageStart"] = start + limit
        return page

    return answer


def ref(branch: str, key: str = "ACME", slug: str = "infra", commit: str = "a" * 40) -> dict:
    return {"id": f"refs/heads/{branch}", "displayId": branch, "latestCommit": commit,
            "repository": {"slug": slug, "project": {"key": key}}}


def pull(number: int, state: str = "OPEN", *, source: str = "platform-agent/x", source_repo=("ACME", "infra"),
         target: str = "main", target_repo=("ACME", "infra"), version: int = 0, reviewers=(), **extra) -> dict:
    """A pull request as the instance answers it, cut to what the forge reads."""
    node = {
        "id": number, "version": version, "title": f"pr {number}", "description": "body", "state": state,
        "draft": False, "createdDate": 1792505000000, "updatedDate": 1792506000000,
        "fromRef": ref(source, *source_repo), "toRef": ref(target, *target_repo),
        "author": {"user": {"name": "svc-kube-agents", "type": "SERVICE"}, "role": "AUTHOR"},
        "reviewers": [{"user": {"name": name, "type": "NORMAL"}, "role": "REVIEWER"} for name in reviewers],
        "links": {"self": [{"href": f"https://{HOST}/projects/ACME/repos/infra/pull-requests/{number}"}]},
    }
    node.update(extra)
    return node


def comment(ident: int, text: str, created: int, *, author: str = "jdoe", replies=(), anchor=None) -> dict:
    node = {"id": ident, "text": text, "createdDate": created, "author": {"name": author, "type": "NORMAL"},
            "comments": list(replies)}
    if anchor is not None:
        node["anchor"] = anchor
    return node


def commented(ident: int, node: dict) -> dict:
    return {"id": ident, "action": "COMMENTED", "commentAction": "ADDED", "comment": node,
            "createdDate": node["createdDate"]}


# -- the test case ---------------------------------------------------------------


@unittest.skipUnless(OPENSSL, "openssl is needed to make the private CA of the mock")
class DataCenterCase(unittest.TestCase):
    """A broker with one Data Center forge at the mock's host, configured by a forge config file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token = self.dir / "token"
        self.token.write_text(TOKEN + "\n")
        transport_module._CA_CONTEXTS.clear()
        self.mock = MockDataCenter(_PKI["cert"], _PKI["key"])
        self.addCleanup(self.mock.close)
        self.addCleanup(lambda: self.assertEqual([], self.mock.unrouted, "the forge called a route the test did not set"))
        # The mock's hostname reaches the mock's loopback port; nothing else is touched.
        real = socket.create_connection
        port = self.mock.port

        def connect(address, *args, **kwargs):
            if address[0] == HOST and address[1] == 443:
                address = ("127.0.0.1", port)
            return real(address, *args, **kwargs)

        patcher = mock.patch("socket.create_connection", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def broker(self, *, git_runner=None, ca_file: str | None = None, **entry) -> vcs_broker.VcsBroker:
        config = {"provider": "bitbucket-datacenter", "host": HOST, "tokenPath": str(self.token),
                  "allowedPaths": []}
        if ca_file is None:
            ca_file = str(_PKI["ca"])
        if ca_file:
            config["caFile"] = ca_file
        config.update(entry)
        path = self.dir / "forges.json"
        path.write_text(json.dumps({"forges": [config]}))
        os.environ[FORGES_CONFIG_ENV] = str(path)
        return vcs_broker.VcsBroker(self.dir / "scratch", git_runner=git_runner or (lambda *a, **k: None),
                                    http_timeout=10.0)

    def verb(self, verb_name: str, /, **payload):
        return getattr(self.broker(), verb_name.replace("-", "_"))({"repository": REPO_URL, **payload})

    def refused(self, call) -> WorkspaceError:
        with self.assertRaises(WorkspaceError) as caught:
            call()
        return caught.exception


def forge(allowed=(), host: str = HOST, **kwargs) -> BitbucketDataCenterForge:
    return BitbucketDataCenterForge(host, "/var/run/forge/token", allowed, **kwargs)


# -- parse and configuration -------------------------------------------------------


class ParseTest(unittest.TestCase):
    def test_key_and_slug_from_every_spelling(self):
        built = forge()
        for url in (
            "ACME/infra",
            f"{HOST}/ACME/infra",
            f"https://{HOST}/scm/ACME/infra.git",
            f"https://{HOST}/scm/acme/infra",
            f"https://{HOST}/projects/ACME/repos/infra",
            f"https://{HOST}/projects/ACME/repos/infra/browse",
            f"https://{HOST}/projects/ACME/repos/infra/browse/apps/x.yaml",
            f"https://{HOST}/projects/ACME/repos/infra/pull-requests/7/overview",
        ):
            with self.subTest(url=url):
                self.assertEqual("infra", built.parse(url).split("/")[1])
                self.assertEqual("acme", built.parse(url).split("/")[0].lower())
        self.assertEqual("ACME/infra", built.parse(f"https://{HOST}/scm/ACME/infra.git"))

    def test_a_personal_repository_is_refused_by_name(self):
        built = forge()
        for url in (f"https://{HOST}/scm/~jdoe/infra.git", f"https://{HOST}/projects/~JDOE/repos/infra",
                    f"{HOST}/~jdoe/infra", "~jdoe/infra", f"https://{HOST}/users/jdoe/repos/infra"):
            with self.subTest(url=url), self.assertRaises(WorkspaceError) as caught:
                built.parse(url)
            self.assertIn("personal repository", str(caught.exception))

    def test_a_bad_project_key_another_host_or_another_shape_is_refused(self):
        built = forge()
        for url in ("1ACME/infra", "AC-ME/infra", "ACME", "ACME/infra/extra", "https://other.mock.test/ACME/infra",
                    f"https://{HOST}/projects/ACME/infra", f"https://{HOST}/scm/ACME", "projects/ACME"):
            with self.subTest(url=url), self.assertRaises(WorkspaceError) as caught:
                built.parse(url)
            self.assertEqual(400, caught.exception.status)
            self.assertIn("KEY/repository", str(caught.exception))

    def test_allowed_paths_are_project_keys_and_narrow_the_forge(self):
        built = forge(["ACME", "/ops/"])
        self.assertEqual(("acme", "ops"), built.allowed_paths)
        self.assertEqual("acme/infra", built.parse("acme/infra"))
        self.assertEqual("OPS/x", built.parse(f"https://{HOST}/scm/OPS/x.git"))
        with self.assertRaises(WorkspaceError) as caught:
            built.parse(f"https://{HOST}/projects/OTHER/repos/infra")
        self.assertEqual((403, "REPOSITORY_NOT_ALLOWED"), (caught.exception.status, caught.exception.fields["code"]))
        with self.assertRaises(WorkspaceError):
            built.parse("ACMEX/infra")

    def test_an_allowed_paths_entry_is_one_project_key(self):
        for bad, words in (("", "names no project"), ("/", "names no project"), ("AC ME", "whitespace"),
                           ("ACME/infra", "not a project key"), ("1ACME", "not a project key"),
                           ("~jdoe", "not a project key")):
            with self.subTest(entry=bad), self.assertRaises(ValueError) as caught:
                forge([bad])
            self.assertIn(words, str(caught.exception))


class ConfigTest(unittest.TestCase):
    def entry(self, **extra) -> dict:
        entry = {"provider": "bitbucket-datacenter", "host": HOST, "token_path": "/var/run/forge/token",
                 "allowed_paths": ()}
        entry.update(extra)
        return entry

    def test_a_bitbucket_cloud_host_is_refused(self):
        for host in ("bitbucket.org", "www.bitbucket.org", "api.bitbucket.org", "bitbucket.io", "x.bitbucket.io"):
            with self.subTest(host=host), self.assertRaises(ValueError) as caught:
                forge(host=host)
            self.assertIn("Bitbucket Cloud", str(caught.exception))
        # A name that only ends with the same letters is not Cloud.
        self.assertEqual(("mybitbucket.org",), forge(host="mybitbucket.org").hosts)

    def test_token_path_and_allowed_paths_are_required(self):
        with self.assertRaises(ValueError) as caught:
            BitbucketDataCenterForge.for_config({"forges": [self.entry(token_path="")]})
        self.assertIn("tokenPath", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            BitbucketDataCenterForge.for_config({"forges": [self.entry(allowed_paths=None)]})
        self.assertIn("allowedPaths", str(caught.exception))
        [built] = BitbucketDataCenterForge.for_config({"forges": [self.entry()]})
        self.assertEqual((), built.allowed_paths)

    def test_one_instance_per_host_with_its_own_fields(self):
        built = BitbucketDataCenterForge.for_config({"forges": [
            self.entry(ca_file="/etc/ca/a.crt", ca_source="the Secret a", identity=" svc ",
                       read_token_path="/var/run/forge/read-token", allowed_paths=("ACME",)),
            self.entry(host="bitbucket2.mock.test"),
            {"provider": "gitlab", "host": "gitlab.com"},
        ]})
        self.assertEqual([(HOST,), ("bitbucket2.mock.test",)], [f.hosts for f in built])
        first = built[0]
        self.assertEqual(("/etc/ca/a.crt", "the Secret a", "svc", "/var/run/forge/read-token", ("acme",)),
                         (first.ca_file, first.ca_source, first.identity, first.read_token_path, first.allowed_paths))
        self.assertEqual(f"https://{HOST}", first.api_url)


class UrlsAndCapabilitiesTest(unittest.TestCase):
    def test_clone_and_file_urls(self):
        built = forge()
        self.assertEqual(f"https://{HOST}/scm/ACME/infra.git", built.clone_url("ACME/infra"))
        self.assertEqual(f"https://{HOST}/projects/ACME/repos/infra/browse/{{path}}?at={{ref}}",
                         built.file_url("ACME/infra"))

    def test_capabilities_have_no_issue_label_or_acknowledge_verb(self):
        answer = forge().capabilities("ACME/infra")
        self.assertEqual("bitbucket-datacenter", answer["forge"])
        self.assertEqual(forge().file_url("ACME/infra"), answer["fileUrl"])
        self.assertIs(False, answer["labels"])
        self.assertIs(False, answer["acknowledge"])
        verbs = set(answer["verbs"])
        self.assertFalse({v for v in verbs if v.startswith("issue-")})
        self.assertNotIn("label-ensure", verbs)
        self.assertNotIn("proposal-acknowledge", verbs)
        for verb in ("proposal-create", "proposal-list", "proposal-view", "proposal-comment",
                     "proposal-update", "proposal-close", "proposal-commits", "branch-delete"):
            self.assertIn(verb, verbs)


# -- TLS and the token ---------------------------------------------------------------


class TrustTest(DataCenterCase):
    def call(self, broker):
        self.mock.routes[("GET", PRS)] = paged([])
        return broker.proposal_list({"repository": REPO_URL})

    def test_the_forges_own_ca_is_trusted_and_the_token_is_only_a_bearer_header(self):
        self.assertEqual([], self.call(self.broker())["proposals"])
        [request] = self.mock.requests
        self.assertEqual(f"Bearer {TOKEN}", request["authorization"])
        self.assertEqual(HOST, request["host"])
        self.assertNotIn(TOKEN, request["raw_path"])
        self.assertEqual("", request["raw_body"])

    def test_a_wrong_ca_is_untrusted_and_sends_nothing(self):
        error = self.refused(lambda: self.call(self.broker(ca_file=str(_PKI["other_ca"]))))
        self.assertEqual("FORGE_TLS_UNTRUSTED", error.fields["code"])
        self.assertIn(HOST, error.fields["detail"])
        self.assertEqual([], self.mock.requests)

    def test_a_missing_ca_file_or_none_is_untrusted(self):
        error = self.refused(lambda: self.call(self.broker(ca_file=str(self.dir / "absent.crt"), caSecret="bb-ca")))
        self.assertEqual("FORGE_TLS_UNTRUSTED", error.fields["code"])
        self.assertIn("bb-ca", error.fields["detail"])
        error = self.refused(lambda: self.call(self.broker(ca_file="")))
        self.assertEqual("FORGE_TLS_UNTRUSTED", error.fields["code"])
        self.assertEqual([], self.mock.requests)

    def test_a_redirect_is_refused_and_not_followed(self):
        self.mock.redirect_to = "https://elsewhere.mock.test/rest/api/latest/x"
        error = self.refused(lambda: self.call(self.broker()))
        self.assertEqual("FORGE_REDIRECTED", error.fields["code"])
        self.assertEqual(1, len(self.mock.requests))

    def test_git_gets_the_token_through_the_helper_with_x_token_auth(self):
        built = BitbucketDataCenterForge(HOST, str(self.token), ())
        config = dict(built.credential.git_config("ACME/infra"))
        self.assertEqual("", config["credential.helper"])
        self.assertEqual([str(self.token), "x-token-auth"], config[f"credential.https://{HOST}.helper"].split()[1:])
        self.assertFalse([value for value in config.values() if TOKEN in value])


# -- identity and write access ------------------------------------------------------


class IdentityTest(DataCenterCase):
    def identity(self, broker=None, **payload):
        return (broker or self.broker()).identity({"repository": REPO_URL, **payload})["identity"]

    def test_a_configured_identity_answers_with_no_call(self):
        self.assertEqual("svc-configured", self.identity(self.broker(identity="svc-configured"))["login"])
        self.assertEqual([], self.mock.requests)

    def test_the_servlet_names_the_username_as_plain_text(self):
        self.mock.routes[("GET", WHOAMI)] = Text("svc-kube-agents\n")
        self.assertEqual("svc-kube-agents", self.identity()["login"])
        [request] = self.mock.requests
        self.assertEqual("text/plain", request["accept"])
        self.assertEqual(f"Bearer {TOKEN}", request["authorization"])

    def test_a_refusal_or_an_answer_that_is_not_one_username_is_identity_unknown(self):
        for answer in (Status(401), Status(403), Status(404), Text(""), Text("two words"),
                       Text("<html><body>Log in</body></html>"), Text("a\nb")):
            with self.subTest(answer=getattr(answer, "status", None) or getattr(answer, "text", None)):
                self.mock.routes[("GET", WHOAMI)] = answer
                with self.assertRaises(IdentityUnknown) as caught:
                    self.identity()
                self.assertEqual((502, "FORGE_IDENTITY_UNKNOWN"),
                                 (caught.exception.status, caught.exception.fields["code"]))

    def test_a_server_failure_is_the_shared_error(self):
        self.mock.routes[("GET", WHOAMI)] = Status(500)
        error = self.refused(self.identity)
        self.assertNotIsInstance(error, IdentityUnknown)
        self.assertEqual(UNAVAILABLE.code, error.fields["code"])

    WRITERS_CAP = next(cls for cls in providers.AVAILABLE if cls.name == "bitbucket-datacenter").WRITERS_CAP

    def users(self, answer):
        self.mock.routes[("GET", f"{REST}/users")] = answer
        return self.identity(self.broker(identity="svc"), login="JDoe")["canWrite"]

    def test_write_access_is_the_user_search_filtered_on_repo_write(self):
        self.assertIs(True, self.users({"values": [{"name": "other"}, {"name": "jdoe", "type": "NORMAL"}]}))
        [request] = self.mock.requests
        self.assertEqual(f"{REST}/users", request["path"])
        # The numbered filter form that Atlassian documents.
        self.assertEqual({"filter": ["JDoe"], "permission.1": ["REPO_WRITE"], "permission.1.projectKey": ["ACME"],
                          "permission.1.repositorySlug": ["infra"], "start": ["0"], "limit": ["100"]},
                         request["query"])

    def test_the_search_is_read_past_its_first_page(self):
        # `filter` matches a part of a name, so 150 other accounts can come
        # before the user: the user on the second page is still found.
        others = [{"name": f"jdoe{n}", "type": "NORMAL"} for n in range(150)]
        self.assertIs(True, self.users(paged(others + [{"name": "jdoe", "type": "NORMAL"}])))
        self.assertEqual(["0", "100"], [r["query"]["start"][0] for r in self.mock.requests])

    def test_a_user_not_found_in_the_rows_read_is_unknown_not_a_no(self):
        # More rows than the cap, none of them the user: a "no" would be a
        # refusal the conversation keeps, so the answer is "not known".
        others = [{"name": f"jdoe{n}", "type": "NORMAL"} for n in range(self.WRITERS_CAP + 50)]
        self.assertIsNone(self.users(paged(others)))
        self.assertIs(False, self.users(paged(others[:150])))

    def test_a_service_user_is_no_and_so_is_one_not_listed(self):
        self.assertIs(False, self.users({"values": [{"name": "jdoe", "type": "SERVICE"}]}))
        self.assertIs(False, self.users({"values": [{"name": "jdoe2", "type": "NORMAL"}]}))
        self.assertIs(False, self.users({"values": []}))

    def test_a_failed_search_is_unknown(self):
        self.assertIsNone(self.users(Status(500)))
        self.assertIsNone(self.users(Status(403)))

    def test_an_automation_named_by_the_caller_is_no_with_no_call(self):
        self.assertIs(False, self.identity(self.broker(identity="svc"), login="jdoe", bot=True)["canWrite"])
        self.assertEqual([], self.mock.requests)


# -- pages ------------------------------------------------------------------------------


class PagesTest(DataCenterCase):
    def reach(self):
        broker = self.broker()
        return broker.credential_reach(broker.registry.forges[0])

    def test_reach_reads_every_page(self):
        rows = [{"slug": f"r{n}", "project": {"key": "ACME"}} for n in range(150)] + [{"slug": "", "project": {}}]
        self.mock.routes[("GET", f"{REST}/repos")] = paged(rows)
        paths, more = self.reach()
        self.assertEqual([f"ACME/r{n}" for n in range(150)], paths)
        self.assertFalse(more)
        self.assertEqual([("0", "100"), ("100", "100")],
                         [(r["query"]["start"][0], r["query"]["limit"][0]) for r in self.mock.requests])
        self.assertEqual(["REPO_WRITE"], self.mock.requests[0]["query"]["permission"])

    def test_a_page_not_last_with_no_next_start_stops(self):
        self.mock.routes[("GET", f"{REST}/repos")] = {"values": [{"slug": "a", "project": {"key": "ACME"}}],
                                                      "isLastPage": False}
        self.assertEqual((["ACME/a"], False), self.reach())
        self.assertEqual(1, len(self.mock.requests))

    def test_a_next_start_that_does_not_advance_stops(self):
        def answer(request):
            start = int(request["query"]["start"][0])
            return {"values": [{"slug": f"r{start}", "project": {"key": "ACME"}}], "isLastPage": False,
                    "start": start, "nextPageStart": 0 if start else 5}

        self.mock.routes[("GET", f"{REST}/repos")] = answer
        self.assertEqual((["ACME/r0", "ACME/r5"], False), self.reach())
        self.assertEqual(["0", "5"], [r["query"]["start"][0] for r in self.mock.requests])


# -- proposal-list ---------------------------------------------------------------------


class ProposalListTest(DataCenterCase):
    def listed(self, rows, **payload):
        self.mock.requests.clear()
        self.mock.routes[("GET", PRS)] = paged(rows)
        return self.verb("proposal-list", **payload)

    def test_open_is_asked_as_open_and_all_as_all(self):
        self.listed([pull(1)], state="open")
        self.assertEqual(["OPEN"], self.mock.requests[0]["query"]["state"])
        self.assertEqual(["NEWEST"], self.mock.requests[0]["query"]["order"])
        self.listed([pull(1)], state="all")
        self.assertEqual(["ALL"], self.mock.requests[0]["query"]["state"])

    def test_closed_is_all_filtered_here(self):
        answer = self.listed([pull(1), pull(2, "MERGED"), pull(3, "DECLINED", closedDate=1792507200000)],
                             state="closed")
        self.assertEqual(["ALL"], self.mock.requests[0]["query"]["state"])
        self.assertEqual([(2, "merged"), (3, "closed")], [(p["number"], p["state"]) for p in answer["proposals"]])
        self.assertEqual("2026-10-20T14:40:00Z", answer["proposals"][1]["closed"])
        self.assertEqual("2026-10-20T14:20:00Z", answer["proposals"][0]["closed"])

    def test_a_source_is_outgoing_and_excludes_forks_and_other_targets(self):
        rows = [
            pull(1, source="fix/a"),
            pull(2, source="fix/a", source_repo=("ME", "infra-fork")),
            pull(3, source="fix/a", target_repo=("OTHER", "upstream")),
            pull(4, source="fix/a", source_repo=("acme", "infra")),
        ]
        answer = self.listed(rows, source="fix/a")
        query = self.mock.requests[0]["query"]
        self.assertEqual((["refs/heads/fix/a"], ["OUTGOING"]), (query["at"], query["direction"]))
        self.assertEqual([1, 4], [p["number"] for p in answer["proposals"]])
        self.assertEqual(["ACME/infra", "ACME/infra"], [p["sourceRepo"] for p in answer["proposals"]])

    def test_a_target_alone_is_incoming(self):
        answer = self.listed([pull(1, target="release"), pull(2, source_repo=("ME", "fork"), target="release")],
                             target="release")
        query = self.mock.requests[0]["query"]
        self.assertEqual((["refs/heads/release"], ["INCOMING"]), (query["at"], query["direction"]))
        self.assertEqual([1, 2], [p["number"] for p in answer["proposals"]])

    def test_a_source_and_a_target_filter_the_target_here(self):
        answer = self.listed([pull(1, source="fix/a", target="main"), pull(2, source="fix/a", target="release")],
                             source="fix/a", target="release")
        self.assertEqual(["OUTGOING"], self.mock.requests[0]["query"]["direction"])
        self.assertEqual([2], [p["number"] for p in answer["proposals"]])

    def test_pages_are_asked_by_start_and_limit(self):
        rows = [pull(n) for n in range(25)]
        answer = self.listed(rows, limit=10, page=2)
        self.assertEqual(list(range(10, 20)), [p["number"] for p in answer["proposals"]])
        self.assertTrue(answer["truncated"])
        self.assertEqual((["10"], ["10"]), (self.mock.requests[0]["query"]["start"], self.mock.requests[0]["query"]["limit"]))
        answer = self.listed(rows, limit=10, page=3)
        self.assertEqual(list(range(20, 25)), [p["number"] for p in answer["proposals"]])
        self.assertFalse(answer["truncated"])

    def test_a_filtered_listing_is_paged_here(self):
        rows = [pull(n, source="fix/a", source_repo=("ME", "fork") if n % 2 else ("ACME", "infra"))
                for n in range(150)]
        one = self.listed(rows, source="fix/a", limit=30)
        three = self.listed(rows, source="fix/a", limit=30, page=3)
        self.assertEqual(list(range(0, 60, 2)), [p["number"] for p in one["proposals"]])
        self.assertTrue(one["truncated"])
        self.assertEqual(list(range(120, 150, 2)), [p["number"] for p in three["proposals"]])
        self.assertFalse(three["truncated"])
        self.assertEqual(["0", "100"], [r["query"]["start"][0] for r in self.mock.requests])

    def test_times_are_utc_iso_8601_from_milliseconds(self):
        answer = self.listed([pull(1)])
        proposal = answer["proposals"][0]
        self.assertEqual(("2026-10-20T14:03:20Z", "2026-10-20T14:20:00Z", ""),
                         (proposal["created"], proposal["updated"], proposal["closed"]))


# -- proposal-view ---------------------------------------------------------------------


class ProposalViewTest(DataCenterCase):
    BASE = f"{PRS}/7"

    def setUp(self):
        super().setUp()
        self.mock.routes[("GET", self.BASE)] = pull(7, version=3)

    def activities(self, rows):
        self.mock.routes[("GET", f"{self.BASE}/activities")] = paged(rows)

    def test_comments_come_from_commented_activities_oldest_first_with_replies_flattened(self):
        anchor = {"line": 12, "lineType": "ADDED", "path": "apps/x.yaml"}
        self.activities([
            # The activity stream is newest first.
            commented(905, comment(204, "on a line", 1792507200000, anchor=anchor,
                                   replies=[comment(206, "reply on the line", 1792507300000)])),
            {"id": 904, "action": "APPROVED", "createdDate": 1792506900000},
            # Only a COMMENTED row is a comment, even when another row carries one.
            {"id": 903, "action": "RESCOPED", "createdDate": 1792506800000,
             "comment": comment(299, "not a comment row", 1792506800000)},
            commented(902, comment(202, "why one?", 1792506000000, replies=[
                comment(203, "cost", 1792506100000, author="svc", replies=[comment(207, "deep", 1792506200000)]),
            ])),
            {"id": 901, "action": "COMMENTED", "createdDate": 1792505900000},
        ])
        answer = self.verb("proposal-view", number=7, comments=True)
        comments = answer["comments"]
        self.assertEqual([202, 203, 207, 204, 206], [c["id"] for c in comments])
        self.assertEqual(["issue", "issue", "issue", "review_comment", "review_comment"], [c["kind"] for c in comments])
        self.assertEqual(["", "", "", "apps/x.yaml", "apps/x.yaml"], [c["path"] for c in comments])
        self.assertEqual([None, None, None, 12, 12], [c["line"] for c in comments])
        self.assertEqual("2026-10-20T14:20:00Z", comments[0]["created"])
        self.assertEqual(5, answer["commentCount"])
        self.assertFalse(answer["commentsTruncated"])

    def test_an_anchor_on_the_activity_row_is_the_comments_anchor(self):
        row = commented(905, comment(204, "on a line", 1792507200000))
        row["commentAnchor"] = {"line": 3, "path": {"components": ["apps", "y.yaml"], "toString": "apps/y.yaml"}}
        self.activities([row])
        [only] = self.verb("proposal-view", number=7, comments=True)["comments"]
        self.assertEqual(("review_comment", "apps/y.yaml", 3), (only["kind"], only["path"], only["line"]))

    def test_comments_are_truncated_at_the_limit_after_ordering(self):
        rows = [commented(1000 + n, comment(n, f"c{n}", 1792500000000 + n * 1000)) for n in range(130, 0, -1)]
        self.activities(rows)
        answer = self.verb("proposal-view", number=7, comments=True, limit=3)
        self.assertEqual([1, 2, 3], [c["id"] for c in answer["comments"]])
        self.assertTrue(answer["commentsTruncated"])
        activity_requests = [r for r in self.mock.requests if r["path"].endswith("/activities")]
        self.assertEqual(2, len(activity_requests))

    def test_the_diff_is_the_pull_request_diff_as_text(self):
        self.mock.routes[("GET", f"{self.BASE}.diff")] = Text(DIFF)
        answer = self.verb("proposal-view", number=7, diff=True)
        self.assertEqual(DIFF, answer["diff"])
        request = self.mock.requests[-1]
        self.assertEqual((f"{self.BASE}.diff", "text/plain"), (request["path"], request["accept"]))

    def test_the_proposal_fields(self):
        proposal = self.verb("proposal-view", number=7)["proposal"]
        self.assertEqual(
            {"number": 7, "state": "open", "author": "svc-kube-agents", "source": "platform-agent/x",
             "target": "main", "sourceRepo": "ACME/infra", "sourceRevision": "a" * 40, "labels": [],
             "url": f"https://{HOST}/projects/ACME/repos/infra/pull-requests/7"},
            {key: proposal[key] for key in ("number", "state", "author", "source", "target", "sourceRepo",
                                            "sourceRevision", "labels", "url")},
        )


# -- writes ------------------------------------------------------------------------------


class WriteTest(DataCenterCase):
    BASE = f"{PRS}/7"

    def test_create_sends_from_and_to_refs_and_a_draft_only_when_asked(self):
        self.mock.routes[("POST", PRS)] = pull(8)
        answer = self.verb("proposal-create", title=" Fix ", body="why", source="fix/a", target="main")
        self.assertEqual(8, answer["proposal"]["number"])
        repository = {"slug": "infra", "project": {"key": "ACME"}}
        self.assertEqual({"title": "Fix", "description": "why",
                          "fromRef": {"id": "refs/heads/fix/a", "repository": repository},
                          "toRef": {"id": "refs/heads/main", "repository": repository}},
                         self.mock.requests[0]["body"])
        self.mock.requests.clear()
        self.verb("proposal-create", title="t", source="fix/a", target="main", draft=True)
        self.assertIs(True, self.mock.requests[0]["body"]["draft"])

    def test_update_sends_the_version_and_the_reviewers_it_read(self):
        self.mock.routes[("GET", self.BASE)] = pull(7, version=5, reviewers=("jdoe", "asmith"))
        self.mock.routes[("PUT", self.BASE)] = pull(7, version=6)
        self.verb("proposal-update", number=7, title="New")
        self.assertEqual([("GET", self.BASE), ("PUT", self.BASE)], self.mock.calls())
        self.assertEqual({"version": 5, "title": "New",
                          "reviewers": [{"user": {"name": "jdoe"}}, {"user": {"name": "asmith"}}]},
                         self.mock.requests[1]["body"])

    def test_an_update_with_nothing_to_change_only_reads(self):
        self.mock.routes[("GET", self.BASE)] = pull(7)
        self.verb("proposal-update", number=7)
        self.assertEqual([("GET", self.BASE)], self.mock.calls())

    def test_a_409_is_retried_once_on_a_fresh_read(self):
        self.mock.routes[("GET", self.BASE)] = [pull(7, version=5), pull(7, version=6)]
        self.mock.routes[("PUT", self.BASE)] = [Status(409), pull(7, version=7)]
        self.verb("proposal-update", number=7, body="b")
        self.assertEqual([("GET", self.BASE), ("PUT", self.BASE)] * 2, self.mock.calls())
        self.assertEqual([5, 6], [r["body"]["version"] for r in self.mock.requests if r["method"] == "PUT"])

    def test_a_second_409_is_raised(self):
        self.mock.routes[("GET", self.BASE)] = [pull(7, version=5), pull(7, version=6)]
        self.mock.routes[("PUT", self.BASE)] = [Status(409), Status(409)]
        error = self.refused(lambda: self.verb("proposal-update", number=7, body="b"))
        self.assertEqual((409, "FORGE_CONFLICT"), (error.status, error.fields["code"]))
        self.assertEqual(4, len(self.mock.requests))

    def test_another_failure_is_not_retried(self):
        self.mock.routes[("GET", self.BASE)] = [pull(7, version=5)]
        self.mock.routes[("PUT", self.BASE)] = [Status(400)]
        self.refused(lambda: self.verb("proposal-update", number=7, body="b"))
        self.assertEqual(2, len(self.mock.requests))

    def test_close_declines_with_the_version(self):
        self.mock.routes[("GET", self.BASE)] = [pull(7, version=3), pull(7, version=4)]
        self.mock.routes[("POST", f"{self.BASE}/decline")] = [Status(409), pull(7, "DECLINED", version=5)]
        answer = self.verb("proposal-close", number=7)
        self.assertEqual("closed", answer["proposal"]["state"])
        declines = [r for r in self.mock.requests if r["method"] == "POST"]
        self.assertEqual([["3"], ["4"]], [r["query"]["version"] for r in declines])

    def test_comment_sends_text(self):
        self.mock.routes[("POST", f"{self.BASE}/comments")] = comment(205, "thanks", 1792508400000, author="svc")
        answer = self.verb("proposal-comment", number=7, body="thanks")
        self.assertEqual({"text": "thanks"}, self.mock.requests[0]["body"])
        self.assertEqual(("issue-205", "thanks", "2026-10-20T15:00:00Z"),
                         (answer["comment"]["ref"], answer["comment"]["body"], answer["comment"]["created"]))

    def test_commits_come_back_oldest_first_with_a_commit_url(self):
        newest_first = [{"id": f"{n:040x}", "message": f"c{n}", "author": {"name": "jdoe", "displayName": "Jane"},
                         "committerTimestamp": 1700000000000 + n} for n in range(9, 0, -1)]
        self.mock.routes[("GET", f"{self.BASE}/commits")] = paged(newest_first, default_limit=4)
        answer = self.verb("proposal-commits", number=7)
        self.assertEqual([f"c{n}" for n in range(1, 10)], [c["message"] for c in answer["commits"]])
        first = answer["commits"][0]
        self.assertEqual((f"{1:040x}", "Jane", "2023-11-14T22:13:20Z",
                          f"https://{HOST}/projects/ACME/repos/infra/commits/{1:040x}"),
                         (first["sha"], first["author"], first["committed"], first["url"]))


# -- errors and labels ------------------------------------------------------------------


class ErrorTest(DataCenterCase):
    BASE = f"{PRS}/7"

    def test_a_401_is_the_data_center_token_text(self):
        self.mock.routes[("GET", self.BASE)] = Status(401)
        error = self.refused(lambda: self.verb("proposal-view", number=7))
        self.assertEqual((401, "FORGE_UNAUTHENTICATED"), (error.status, error.fields["code"]))
        self.assertEqual(TOKEN_REFUSED_TEXT, str(error))
        self.assertIn("Bitbucket Data Center", str(error))

    def test_a_400_is_a_rejected_request(self):
        self.mock.routes[("POST", PRS)] = Status(400, {"errors": [{"message": "fromRef: no such branch"}]})
        error = self.refused(lambda: self.verb("proposal-create", title="t", source="fix/a", target="main"))
        self.assertEqual((422, "FORGE_REJECTED", GUIDANCE[422].text), (error.status, error.fields["code"], str(error)))
        self.assertIn("no such branch", error.fields["detail"])

    def test_a_404_is_the_shared_404(self):
        self.mock.routes[("GET", self.BASE)] = Status(404)
        error = self.refused(lambda: self.verb("proposal-view", number=7))
        self.assertEqual((404, "FORGE_NOT_FOUND", GUIDANCE[404].text), (error.status, error.fields["code"], str(error)))


class LabelTest(DataCenterCase):
    def test_a_label_field_is_refused_before_any_call(self):
        for verb, payload in (
            ("proposal-list", {"labels": ["x"]}),
            ("proposal-create", {"title": "t", "source": "fix/a", "target": "main", "labels": ["x"]}),
            ("proposal-update", {"number": 7, "labelsAdd": ["x"]}),
            ("label-ensure", {"name": "x"}),
        ):
            with self.subTest(verb=verb), self.assertRaises(LabelsUnsupported) as caught:
                self.verb(verb, **payload)
            self.assertEqual((501, "LABELS_UNSUPPORTED"), (caught.exception.status, caught.exception.fields["code"]))
        self.assertEqual([], self.mock.requests)

    def test_the_package_refuses_a_label_by_itself_too(self):
        # Called with no broker in front, the forge still makes no call.
        def api(*args, **kwargs):
            raise AssertionError("the forge called the API")

        for name, payload in (("proposal_list", {"labels": ["x"]}),
                              ("proposal_create", {"title": "t", "source": "a", "target": "main", "labels": ["x"]}),
                              ("proposal_update", {"number": 1, "labelsAdd": ["x"]})):
            with self.subTest(name=name), self.assertRaises(LabelsUnsupported):
                getattr(forge(), name)(api, "ACME/infra", payload)


# -- the read-only token --------------------------------------------------------------


class ReadCredentialTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token = self.dir / "token"
        self.token.write_text(TOKEN + "\n")
        self.read = self.dir / "read-token"

    def built(self, read_token_path=None) -> BitbucketDataCenterForge:
        path = str(self.read) if read_token_path is None else read_token_path
        return BitbucketDataCenterForge(HOST, str(self.token), (), read_token_path=path)

    def test_no_read_token_path_is_no_credential(self):
        self.assertIsInstance(BitbucketDataCenterForge(HOST, str(self.token)).read_credential("ACME/docs"),
                              providers.NoCredential)

    def test_a_missing_empty_or_malformed_file_is_no_credential(self):
        built = self.built()
        self.assertIsInstance(built.read_credential("ACME/docs"), providers.NoCredential)
        for bad in ("", "\n", "two words\n", "a\nb\n"):
            with self.subTest(bad=bad):
                self.read.write_text(bad)
                self.assertIsInstance(built.read_credential("ACME/docs"), providers.NoCredential)

    def test_a_stored_read_token_is_presented_and_the_write_token_never_is(self):
        self.read.write_text(READ_TOKEN + "\n")
        built = self.built()
        credential = built.read_credential("ACME/docs")
        self.assertIsInstance(credential, providers.StaticFileCredential)
        self.assertEqual({"Authorization": f"Bearer {READ_TOKEN}"}, credential.headers("ACME/docs"))
        helper = dict(credential.git_config("ACME/docs"))[f"credential.https://{HOST}.helper"].split()
        self.assertEqual([str(self.read), "x-token-auth"], helper[1:])
        self.assertEqual({"Authorization": f"Bearer {TOKEN}"}, built.credential.headers("ACME/infra"))

    def test_a_key_added_later_is_read_with_no_restart(self):
        built = self.built()
        self.assertIsInstance(built.read_credential("ACME/docs"), providers.NoCredential)
        self.read.write_text(READ_TOKEN)
        self.assertIsInstance(built.read_credential("ACME/docs"), providers.StaticFileCredential)
        self.read.unlink()
        self.assertIsInstance(built.read_credential("ACME/docs"), providers.NoCredential)

    def test_a_read_token_path_git_may_not_be_handed_stops_the_build(self):
        for bad in ("relative/read-token", "/t/../read-token", "/t/read token"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.built(bad)


# -- git's refusal -------------------------------------------------------------------


class GitRefusedTest(DataCenterCase):
    def refusing(self, stderr: str):
        def git_runner(argv, cwd, check=True, config=()):
            raise subprocess.CalledProcessError(128, argv, "", stderr)

        return git_runner

    def test_a_refused_credential_is_the_forges_own_text_with_no_url(self):
        for stderr in (f"fatal: Authentication failed for 'https://{HOST}/scm/ACME/infra.git/'\n",
                       f"fatal: could not read Username for 'https://{HOST}': terminal prompts disabled\n"):
            with self.subTest(stderr=stderr):
                broker = self.broker(git_runner=self.refusing(stderr))
                with self.assertRaises(WorkspaceError) as caught:
                    broker.clone({"repository": REPO_URL})
                self.assertEqual((401, "FORGE_UNAUTHENTICATED"),
                                 (caught.exception.status, caught.exception.fields["code"]))
                self.assertEqual(GIT_REFUSED_TEXT, str(caught.exception))
                self.assertNotIn(HOST, str(caught.exception))

    def test_another_git_failure_keeps_the_generic_failure(self):
        broker = self.broker(git_runner=self.refusing("fatal: unable to access: SSL certificate problem\n"))
        with self.assertRaises(subprocess.CalledProcessError):
            broker.clone({"repository": REPO_URL})


if __name__ == "__main__":
    unittest.main()
