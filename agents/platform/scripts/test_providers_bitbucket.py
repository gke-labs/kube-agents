#!/usr/bin/env python3
"""The Bitbucket Cloud forge (`bitbucket`), tested against a mock of its API.

    cd agents/platform/scripts && python3 -m unittest test_providers_bitbucket

The mock is a local HTTP server that answers the routes of
https://api.bitbucket.org/2.0 from a table that each test fills. The forge has
one fixed public host and no caFile, so the system CAs must vouch for it, and
a private CA cannot. Thus the test does not send the host's connections to
the mock, as the GitHub Enterprise Server test does. It gives the broker an
HTTP opener instead (`VcsBroker(http_opener=...)`, the hook the broker hands
to `HttpTransport`). The opener records the URL that the transport built,
refuses any URL outside the API root, and sends the same request, with the
same method, headers and body, to the mock. It follows no redirect, as the
transport's own opener does. So everything above the socket is real: the
broker, the registry, the forge config file, the transport, the error
mapping and the forge package.

What is pinned: how a repository URL is read and narrowed by workspace; the
forge config rules; the token goes only in the `Authorization: Bearer` header,
and git gets it through the helper with the username `x-token-auth`; the
account `uuid` as the identity, and an unknown one as FORGE_IDENTITY_UNKNOWN;
the opaque `next` links, and a link outside the API is refused; the
pull-request verbs; the pull-request diff route (which answers 302) is never
used; the 12-character hash is expanded; the Cloud error texts; label fields
are refused before a call; the read-only token; and git's refusal text.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import providers
import vcs_broker
from providers import transport as transport_module
from providers.base import IdentityUnknown, LabelsUnsupported
from providers.errors import GUIDANCE, UNAVAILABLE
from providers.registry import FORGES_CONFIG_ENV
from workspace_paths import WorkspaceError

# From the package surface: no test reaches into a forge package (test_providers_boundary).
BitbucketCloudForge = next(cls for cls in providers.AVAILABLE if cls.name == "bitbucket")
#: The Cloud texts, as the forge declares them.
TOKEN_REFUSED_TEXT = getattr(BitbucketCloudForge.error_overrides.get(401), "text", None)
GIT_REFUSED_TEXT = BitbucketCloudForge.git_refused

API = "https://api.bitbucket.org/2.0"
REPO_URL = "https://bitbucket.org/acme/infra"
TOKEN = "mock-bitbucket-write-token-0001"
READ_TOKEN = "mock-bitbucket-read-token-0002"
ACCOUNT = "{2f7c1d0e-5b8a-4c3e-9f61-0a4b7d2e9c11}"
SHORT = "4e7a1c9b2d30"
FULL = SHORT + "5f6e8a7b9c0d1e2f3a4b5c6d7e8f"
DEST = "9a8b7c6d5e4f"
DIFF = "diff --git a/x b/x\n+one\n"


# -- the mock ---------------------------------------------------------------


class Status:
    """An answer with a status other than 200, and a JSON body."""

    def __init__(self, status: int, body=None) -> None:
        self.status = status
        self.body = body if body is not None else {"type": "error", "error": {"message": f"mock {status}"}}


class Text:
    """A 200 answer with a text/plain body."""

    def __init__(self, text: str) -> None:
        self.text = text


class Redirect:
    """A 302 answer to `location`."""

    def __init__(self, location: str) -> None:
        self.location = location


class MockBitbucketCloud:
    """A mock of https://api.bitbucket.org/2.0, on a loopback port.

    `routes` maps (method, path) to an answer. The path is relative to the API
    root and has no query. An answer is a JSON value (200), a `Status`, a
    `Text`, a `Redirect`, or a callable that gets the request record and
    gives one of these. A request with no route gets a 500, and the test
    fails on it (`unrouted`).
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[dict] = []
        self.unrouted: list[str] = []
        mock_server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                split = urllib.parse.urlsplit(self.path)
                path = split.path
                if path.startswith("/2.0/"):
                    path = path[len("/2.0/"):]
                record = {
                    "method": self.command,
                    "path": path,
                    "query": urllib.parse.parse_qs(split.query),
                    "raw_path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "accept": self.headers.get("Accept"),
                    "raw_body": raw,
                    "body": json.loads(raw) if raw else None,
                }
                mock_server.requests.append(record)
                answer = mock_server.routes.get((self.command, path))
                if answer is None:
                    mock_server.unrouted.append(f"{self.command} {self.path}")
                    return self._send(500, "application/json", b'{"error": {"message": "no route"}}')
                if callable(answer):
                    answer = answer(record)
                if isinstance(answer, Redirect):
                    self.send_response(302)
                    self.send_header("Location", answer.location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
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
        self.port = self.server.server_address[1]
        # A short poll, so that `close` does not wait half a second per test.
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def calls(self, method: str | None = None) -> list[tuple[str, str]]:
        return [(r["method"], r["path"]) for r in self.requests if method in (None, r["method"])]


def paged(rows: list, path: str, default_size: int = 10):
    """Bitbucket's pages: `values`, and an opaque absolute `next` link.

    The page size comes from `pagelen` and the page from `page`, as
    Bitbucket reads them. `next` carries every other parameter of the request.
    """

    def answer(request: dict):
        query = {key: values[-1] if len(values) == 1 else values for key, values in request["query"].items()}
        size = int(query.get("pagelen") or default_size)
        number = int(query.get("page") or 1)
        chunk = rows[(number - 1) * size: number * size]
        page = {"pagelen": size, "page": number, "size": len(rows), "values": chunk}
        if number * size < len(rows):
            following = dict(query, page=str(number + 1))
            page["next"] = f"{API}/{path}?{urllib.parse.urlencode(following, doseq=True)}"
        return page

    return answer


def pull(number: int, state: str = "OPEN", *, source: str = "platform-agent/x", holder: str = "acme/infra",
         source_hash: str = FULL, dest_hash: str = "0" * 40, target: str = "main", **extra) -> dict:
    """A pull request as the API answers it, cut to what the forge reads."""
    node = {
        "type": "pullrequest", "id": number, "title": f"pr {number}", "description": "body",
        "state": state, "draft": False,
        "author": {"type": "user", "uuid": ACCOUNT},
        "source": {"branch": {"name": source}, "commit": {"hash": source_hash},
                   "repository": {"full_name": holder}},
        "destination": {"branch": {"name": target}, "commit": {"hash": dest_hash},
                        "repository": {"full_name": "acme/infra"}},
        "created_on": "2026-10-20T14:03:11+00:00", "updated_on": "2026-10-20T15:10:42+00:00",
        "links": {"html": {"href": f"https://bitbucket.org/acme/infra/pull-requests/{number}"}},
    }
    node.update(extra)
    return node


def comment_row(ident: int, text: str = "hello", *, deleted: bool = False, inline: dict | None = None) -> dict:
    row = {"id": ident, "content": {"raw": "" if deleted else text}, "deleted": deleted,
           "user": {"type": "user", "uuid": "{person}"}, "created_on": f"2026-10-20T10:{ident % 60:02d}:00+00:00",
           "links": {"html": {"href": f"https://bitbucket.org/acme/infra/pull-requests/7/_/diff#comment-{ident}"}}}
    if inline is not None:
        row["inline"] = inline
    return row


# -- the test case -----------------------------------------------------------


class CloudCase(unittest.TestCase):
    """A broker with one Bitbucket Cloud forge, configured by a forge config file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token = self.dir / "token"
        self.token.write_text(TOKEN + "\n")
        self.read = self.dir / "read-token"
        self.mock = MockBitbucketCloud()
        self.addCleanup(self.mock.close)
        self.addCleanup(lambda: self.assertEqual([], self.mock.unrouted, "the forge called a route the test did not set"))
        self.urls: list[str] = []
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

    def opener(self, request: urllib.request.Request, timeout: float):
        """Send the transport's request to the mock, with nothing changed but the address."""
        url = request.full_url
        self.urls.append(url)
        if not url.startswith(API + "/"):
            raise AssertionError(f"the transport built a URL outside the API: {url}")
        local = f"http://127.0.0.1:{self.mock.port}/2.0/{url[len(API) + 1:]}"
        copy = urllib.request.Request(local, data=request.data, method=request.get_method())
        for name, value in request.header_items():
            copy.add_header(name, value)
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), transport_module._RefuseRedirect)
        return direct.open(copy, timeout=timeout)

    def broker(self, *, git_runner=None, **entry) -> vcs_broker.VcsBroker:
        config = {"provider": "bitbucket", "host": "bitbucket.org", "tokenPath": str(self.token), "allowedPaths": []}
        config.update(entry)
        path = self.dir / "forges.json"
        path.write_text(json.dumps({"forges": [config]}))
        os.environ[FORGES_CONFIG_ENV] = str(path)
        return vcs_broker.VcsBroker(
            self.dir / "scratch",
            git_runner=git_runner or (lambda *a, **k: None),
            http_opener=self.opener,
            http_timeout=10.0,
        )

    def verb(self, verb_name: str, /, **payload):
        broker = self.broker()
        return getattr(broker, verb_name.replace("-", "_"))({"repository": REPO_URL, **payload})

    def refused(self, call) -> WorkspaceError:
        with self.assertRaises(WorkspaceError) as caught:
            call()
        return caught.exception


def forge(allowed=(), **kwargs) -> BitbucketCloudForge:
    return BitbucketCloudForge("/var/run/forge/token", allowed, **kwargs)


# -- parse and configuration -------------------------------------------------


class ParseTest(unittest.TestCase):
    def test_a_repository_is_workspace_and_repository_in_every_spelling(self):
        built = forge()
        for url in (
            "acme/infra",
            "https://bitbucket.org/acme/infra",
            "https://bitbucket.org/acme/infra.git",
            "https://www.bitbucket.org/acme/infra",
            "https://www.bitbucket.org/acme/infra.git",
            "bitbucket.org/acme/infra",
        ):
            with self.subTest(url=url):
                self.assertEqual("acme/infra", built.parse(url))

    def test_one_or_three_segments_and_another_host_are_refused(self):
        built = forge()
        for url in ("acme", "https://bitbucket.org/acme", "acme/infra/extra",
                    "https://bitbucket.org/acme/infra/extra", "https://github.com/acme/infra",
                    "https://bitbucket.example.com/acme/infra"):
            with self.subTest(url=url), self.assertRaises(WorkspaceError) as caught:
                built.parse(url)
            self.assertEqual(400, caught.exception.status)
            self.assertIn("expected workspace/repository", str(caught.exception))

    def test_allowed_paths_narrow_by_workspace(self):
        built = forge(["acme", "/Other/"])
        self.assertEqual(("acme", "other"), built.allowed_paths)
        self.assertEqual("acme/infra", built.parse("https://bitbucket.org/acme/infra"))
        self.assertEqual("OTHER/x", built.parse("OTHER/x"))
        with self.assertRaises(WorkspaceError) as caught:
            built.parse("https://bitbucket.org/stranger/infra")
        self.assertEqual(403, caught.exception.status)
        self.assertEqual("REPOSITORY_NOT_ALLOWED", caught.exception.fields["code"])
        # A prefix of an allowed workspace is not that workspace.
        with self.assertRaises(WorkspaceError):
            built.parse("acmex/infra")

    def test_an_allowed_paths_entry_is_one_workspace(self):
        for bad, words in (
            ("", "names no workspace"),
            ("/", "names no workspace"),
            ("ac me", "whitespace"),
            (" acme", "whitespace"),
            ("acme/infra", "not one workspace"),
            ("bitbucket.org", "names the host"),
            ("www.bitbucket.org", "names the host"),
        ):
            with self.subTest(entry=bad), self.assertRaises(ValueError) as caught:
                forge([bad])
            self.assertIn(words, str(caught.exception))


class ConfigTest(unittest.TestCase):
    def entry(self, **extra) -> dict:
        entry = {"provider": "bitbucket", "host": "bitbucket.org", "token_path": "/var/run/forge/token",
                 "allowed_paths": ()}
        entry.update(extra)
        return entry

    def test_the_host_is_bitbucket_org_or_its_www_spelling(self):
        for host in ("bitbucket.org", "www.bitbucket.org"):
            with self.subTest(host=host):
                [built] = BitbucketCloudForge.for_config({"forges": [self.entry(host=host)]})
                self.assertEqual(("bitbucket.org", "www.bitbucket.org"), built.hosts)
        for host in ("bitbucket.acme.example", "api.bitbucket.org", "", "github.com"):
            with self.subTest(host=host), self.assertRaises(ValueError) as caught:
                BitbucketCloudForge.for_config({"forges": [self.entry(host=host)]})
            self.assertIn("bitbucket-datacenter", str(caught.exception))

    def test_a_ca_file_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            BitbucketCloudForge.for_config({"forges": [self.entry(ca_file="/etc/ca/ca.crt")]})
        self.assertIn("caFile", str(caught.exception))

    def test_a_token_path_is_required(self):
        with self.assertRaises(ValueError) as caught:
            BitbucketCloudForge.for_config({"forges": [self.entry(token_path="")]})
        self.assertIn("tokenPath", str(caught.exception))

    def test_allowed_paths_is_required_and_empty_is_every_workspace(self):
        with self.assertRaises(ValueError) as caught:
            BitbucketCloudForge.for_config({"forges": [self.entry(allowed_paths=None)]})
        self.assertIn("allowedPaths", str(caught.exception))
        [built] = BitbucketCloudForge.for_config({"forges": [self.entry(allowed_paths=())]})
        self.assertEqual((), built.allowed_paths)
        self.assertEqual("anyone/repo", built.parse("anyone/repo"))

    def test_other_providers_are_not_built_and_the_fields_reach_the_forge(self):
        self.assertEqual((), tuple(BitbucketCloudForge.for_config({"forges": [{"provider": "gitlab"}]})))
        [built] = BitbucketCloudForge.for_config({"forges": [self.entry(
            allowed_paths=("acme",), read_token_path="/var/run/forge/read-token", identity=" {9D3E7A10-1111-4222-8333-444455556666} ")]})
        self.assertEqual(("acme",), built.allowed_paths)
        self.assertEqual("/var/run/forge/read-token", built.read_token_path)
        self.assertEqual("{9d3e7a10-1111-4222-8333-444455556666}", built.identity)

    def test_the_broker_refuses_a_config_file_with_a_ca_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forges.json"
            path.write_text(json.dumps({"forges": [{"provider": "bitbucket", "host": "bitbucket.org",
                                                    "tokenPath": "/t/token", "allowedPaths": [],
                                                    "caFile": "/etc/ca/ca.crt"}]}))
            with mock.patch.dict(os.environ, {FORGES_CONFIG_ENV: str(path)}), self.assertRaises(ValueError):
                vcs_broker.VcsBroker(Path(tmp) / "scratch", git_runner=lambda *a, **k: None)


class UrlsAndCapabilitiesTest(unittest.TestCase):
    def test_clone_and_file_urls_are_on_bitbucket_org(self):
        built = forge()
        self.assertEqual("https://bitbucket.org/acme/infra.git", built.clone_url("acme/infra"))
        self.assertEqual("https://bitbucket.org/acme/infra/src/{ref}/{path}", built.file_url("acme/infra"))
        self.assertEqual(API, built.api_url)
        self.assertEqual("http", built.transport)

    def test_capabilities_have_no_issue_label_or_acknowledge_verb(self):
        answer = forge().capabilities("acme/infra")
        self.assertEqual("bitbucket", answer["forge"])
        self.assertEqual("https://bitbucket.org/acme/infra/src/{ref}/{path}", answer["fileUrl"])
        self.assertIs(False, answer["labels"])
        self.assertIs(False, answer["acknowledge"])
        verbs = set(answer["verbs"])
        self.assertFalse({v for v in verbs if v.startswith("issue-")})
        self.assertNotIn("label-ensure", verbs)
        self.assertNotIn("proposal-acknowledge", verbs)
        for verb in ("proposal-create", "proposal-list", "proposal-view", "proposal-comment",
                     "proposal-update", "proposal-close", "proposal-commits", "branch-delete", "identity"):
            self.assertIn(verb, verbs)


# -- the token ----------------------------------------------------------------


class TokenTest(CloudCase):
    def test_the_token_is_a_bearer_header_and_never_in_a_url_or_body(self):
        self.mock.routes[("GET", "repositories/acme/infra/pullrequests")] = paged([pull(1)], "repositories/acme/infra/pullrequests")
        self.mock.routes[("POST", "repositories/acme/infra/pullrequests/1/comments")] = comment_row(5, "hi")
        self.verb("proposal-list")
        self.verb("proposal-comment", number=1, body="hi")
        self.assertEqual(2, len(self.mock.requests))
        for request in self.mock.requests:
            self.assertEqual(f"Bearer {TOKEN}", request["authorization"])
            self.assertNotIn(TOKEN, request["raw_path"])
            self.assertNotIn(TOKEN, request["raw_body"])
        for url in self.urls:
            self.assertNotIn(TOKEN, url)

    def test_a_rotated_token_file_is_the_next_calls_token(self):
        self.mock.routes[("GET", "repositories/acme/infra/pullrequests")] = paged([], "repositories/acme/infra/pullrequests")
        broker = self.broker()
        broker.proposal_list({"repository": REPO_URL})
        self.token.write_text("mock-rotated-token\n")
        broker.proposal_list({"repository": REPO_URL})
        self.assertEqual([f"Bearer {TOKEN}", "Bearer mock-rotated-token"],
                         [r["authorization"] for r in self.mock.requests])

    def test_git_gets_the_token_through_the_helper_with_x_token_auth(self):
        built = BitbucketCloudForge(str(self.token), ())
        config = dict(built.credential.git_config("acme/infra"))
        self.assertEqual("", config["credential.helper"])
        helper = config["credential.https://bitbucket.org.helper"].split()
        self.assertEqual([str(self.token), "x-token-auth"], helper[1:])
        # The token is in no git config value: the helper reads the file.
        self.assertFalse([value for value in config.values() if TOKEN in value])
        self.assertEqual({"Authorization": f"Bearer {TOKEN}"}, built.credential.headers("acme/infra"))


# -- identity -------------------------------------------------------------------


class IdentityTest(CloudCase):
    def identity(self, broker=None, **payload):
        broker = broker or self.broker()
        return broker.identity({"repository": REPO_URL, **payload})["identity"]

    def test_a_configured_identity_answers_with_no_call(self):
        answer = self.identity(self.broker(identity="{9d3e7a10-1111-4222-8333-444455556666}"))
        self.assertEqual("{9d3e7a10-1111-4222-8333-444455556666}", answer["login"])
        self.assertEqual([], self.mock.requests)

    def test_without_one_the_user_route_names_the_account_uuid(self):
        self.mock.routes[("GET", "user")] = {"type": "user", "uuid": ACCOUNT, "nickname": "kube", "display_name": "k"}
        answer = self.identity()
        self.assertEqual(ACCOUNT, answer["login"])
        self.assertEqual([("GET", "user")], self.mock.calls())
        self.assertEqual(f"Bearer {TOKEN}", self.mock.requests[0]["authorization"])

    def test_a_refused_user_route_is_identity_unknown(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.mock.routes[("GET", "user")] = Status(status)
                with self.assertRaises(IdentityUnknown) as caught:
                    self.identity()
                self.assertEqual(502, caught.exception.status)
                self.assertEqual("FORGE_IDENTITY_UNKNOWN", caught.exception.fields["code"])

    def test_an_answer_with_no_uuid_is_identity_unknown(self):
        self.mock.routes[("GET", "user")] = {"type": "user", "nickname": "kube"}
        with self.assertRaises(IdentityUnknown):
            self.identity()

    def test_a_server_failure_is_the_shared_error_and_not_an_unknown_identity(self):
        self.mock.routes[("GET", "user")] = Status(500)
        error = self.refused(self.identity)
        self.assertNotIsInstance(error, IdentityUnknown)
        self.assertEqual((UNAVAILABLE.status, UNAVAILABLE.code), (error.status, error.fields["code"]))

    def test_a_uuid_without_braces_is_the_braced_login_and_matches_an_author(self):
        # Bitbucket writes every author's uuid in braces. A UUID copied without
        # them must still match this install's own proposals.
        bare = ACCOUNT.strip("{}").upper()
        answer = self.identity(self.broker(identity=bare))
        self.assertEqual(ACCOUNT, answer["login"])

    def test_an_identity_that_is_not_a_uuid_stops_the_build(self):
        for bad in ("jdoe", "{jdoe}", "{" + ACCOUNT.strip("{}"), ACCOUNT.strip("{}") + "}"):
            with self.subTest(identity=bad), self.assertRaises(ValueError) as caught:
                BitbucketCloudForge(str(self.token), (), identity=bad)
            self.assertIn("account UUID", str(caught.exception))

    def test_write_access_of_another_account_is_unknown_and_an_automation_is_no(self):
        broker = self.broker(identity=ACCOUNT)
        self.assertIsNone(self.identity(broker, login="{someone}")["canWrite"])
        self.assertIs(False, self.identity(broker, login="{someone}", bot=True)["canWrite"])
        self.assertEqual([], self.mock.requests)


# -- pages ---------------------------------------------------------------------


class PagesTest(CloudCase):
    def test_reach_follows_the_next_links_to_the_end(self):
        rows = [{"full_name": f"acme/repo-{n}"} for n in range(150)] + [{"full_name": ""}]
        self.mock.routes[("GET", "repositories")] = paged(rows, "repositories")
        broker = self.broker()
        paths, more = broker.credential_reach(broker.registry.forges[0])
        self.assertEqual([f"acme/repo-{n}" for n in range(150)], paths)
        self.assertFalse(more)
        self.assertEqual(2, len(self.mock.requests))
        first, second = self.mock.requests
        self.assertEqual({"role": ["member"], "pagelen": ["100"]}, first["query"])
        self.assertEqual(["2"], second["query"]["page"])
        self.assertEqual(["member"], second["query"]["role"])

    def test_reach_stops_at_its_ceiling_and_says_so(self):
        rows = [{"full_name": f"acme/repo-{n}"} for n in range(BitbucketCloudForge.REACH_PAGES * 100 + 5)]
        self.mock.routes[("GET", "repositories")] = paged(rows, "repositories")
        broker = self.broker()
        paths, more = broker.credential_reach(broker.registry.forges[0])
        self.assertEqual(BitbucketCloudForge.REACH_PAGES * 100, len(paths))
        self.assertTrue(more)
        self.assertEqual(BitbucketCloudForge.REACH_PAGES, len(self.mock.requests))

    def test_a_next_link_outside_the_api_is_refused_and_not_followed(self):
        for link in ("https://evil.example/2.0/repositories?page=2",
                     "https://api.bitbucket.org/1.0/repositories?page=2",
                     "https://api.bitbucket.org/2.0evil/repositories?page=2",
                     "http://api.bitbucket.org/2.0/repositories?page=2",
                     "/2.0/repositories?page=2"):
            with self.subTest(link=link):
                self.mock.requests.clear()
                self.urls.clear()
                self.mock.routes[("GET", "repositories")] = {"values": [{"full_name": "acme/a"}], "next": link}
                broker = self.broker()
                error = self.refused(lambda: broker.credential_reach(broker.registry.forges[0]))
                self.assertEqual(502, error.status)
                self.assertEqual("FORGE_CALL_FAILED", error.fields["code"])
                self.assertEqual(1, len(self.mock.requests))
                self.assertEqual(1, len(self.urls))


# -- proposal-list ------------------------------------------------------------


class ProposalListTest(CloudCase):
    PATH = "repositories/acme/infra/pullrequests"

    def test_the_neutral_state_is_the_bitbucket_states(self):
        rows = [pull(1, "MERGED"), pull(2, "DECLINED"), pull(3, "SUPERSEDED")]
        self.mock.routes[("GET", self.PATH)] = paged(rows, self.PATH)
        answer = self.verb("proposal-list", state="closed")
        self.assertEqual(["merged", "closed", "closed"], [p["state"] for p in answer["proposals"]])
        self.assertEqual(["MERGED", "DECLINED", "SUPERSEDED"], self.mock.requests[0]["query"]["state"])
        self.assertEqual(["-created_on"], self.mock.requests[0]["query"]["sort"])
        for state, sent in (("open", ["OPEN"]), ("all", ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"])):
            with self.subTest(state=state):
                self.mock.requests.clear()
                self.verb("proposal-list", state=state)
                self.assertEqual(sent, self.mock.requests[0]["query"]["state"])

    def test_source_and_target_are_a_bbql_query(self):
        self.mock.routes[("GET", self.PATH)] = paged([pull(1, source="fix/a")], self.PATH)
        self.verb("proposal-list", source="fix/a", target="main")
        self.assertEqual(['source.branch.name="fix/a" AND destination.branch.name="main"'],
                         self.mock.requests[0]["query"]["q"])
        self.mock.requests.clear()
        self.verb("proposal-list", target="release/1.2")
        self.assertEqual(['destination.branch.name="release/1.2"'], self.mock.requests[0]["query"]["q"])
        self.mock.requests.clear()
        self.verb("proposal-list")
        self.assertNotIn("q", self.mock.requests[0]["query"])

    def test_a_forks_branch_of_the_same_name_is_not_this_repositorys(self):
        rows = [pull(1, source="fix/a", holder="stranger/infra"), pull(2, source="fix/a"),
                pull(3, source="fix/a", holder="ACME/Infra"), pull(4, source="fix/a", holder="")]
        self.mock.routes[("GET", self.PATH)] = paged(rows, self.PATH)
        answer = self.verb("proposal-list", source="fix/a")
        self.assertEqual([2, 3], [p["number"] for p in answer["proposals"]])
        self.assertEqual(["acme/infra", "acme/infra"], [p["sourceRepo"] for p in answer["proposals"]])
        self.assertEqual(["50"], self.mock.requests[0]["query"]["pagelen"])

    def test_forks_cannot_crowd_this_repositorys_pull_request_off_the_page(self):
        rows = [pull(n, source="fix/a", holder=f"fork-{n}/infra") for n in range(1, 61)] + [pull(99, source="fix/a")]
        self.mock.routes[("GET", self.PATH)] = paged(rows, self.PATH)
        answer = self.verb("proposal-list", source="fix/a", limit=1)
        self.assertEqual([99], [p["number"] for p in answer["proposals"]])
        self.assertFalse(answer["truncated"])
        self.assertEqual(2, len(self.mock.requests))

    def test_a_window_that_spans_bitbucket_pages_is_correct(self):
        rows = [pull(n) for n in range(130)]
        self.mock.routes[("GET", self.PATH)] = paged(rows, self.PATH)
        answer = self.verb("proposal-list", limit=60, page=2)
        self.assertEqual(list(range(60, 120)), [p["number"] for p in answer["proposals"]])
        self.assertTrue(answer["truncated"])
        self.assertEqual(["50"], self.mock.requests[0]["query"]["pagelen"])
        self.assertEqual(["2"], self.mock.requests[0]["query"]["page"])
        self.assertEqual(["3"], self.mock.requests[1]["query"]["page"])
        self.mock.requests.clear()
        answer = self.verb("proposal-list", limit=60, page=3)
        self.assertEqual(list(range(120, 130)), [p["number"] for p in answer["proposals"]])
        self.assertFalse(answer["truncated"])
        self.mock.requests.clear()
        answer = self.verb("proposal-list", limit=10)
        self.assertEqual(list(range(10)), [p["number"] for p in answer["proposals"]])
        self.assertTrue(answer["truncated"])
        self.assertEqual(["10"], self.mock.requests[0]["query"]["pagelen"])
        self.assertNotIn("page", self.mock.requests[0]["query"])

    def test_page_two_of_a_source_filtered_listing_continues_page_one(self):
        rows = [pull(n, source="fix/a") for n in range(1, 6)]
        self.mock.routes[("GET", self.PATH)] = paged(rows, self.PATH)
        one = self.verb("proposal-list", source="fix/a", limit=2)
        two = self.verb("proposal-list", source="fix/a", limit=2, page=2)
        three = self.verb("proposal-list", source="fix/a", limit=2, page=3)
        self.assertEqual([[1, 2], [3, 4], [5]], [[p["number"] for p in a["proposals"]] for a in (one, two, three)])
        self.assertEqual([True, True, False], [a["truncated"] for a in (one, two, three)])

    def test_a_short_source_hash_in_a_filtered_listing_is_expanded(self):
        self.mock.routes[("GET", self.PATH)] = paged([pull(1, source="fix/a", source_hash=SHORT)], self.PATH)
        self.mock.routes[("GET", f"repositories/acme/infra/commit/{SHORT}")] = {"hash": FULL}
        answer = self.verb("proposal-list", source="fix/a")
        self.assertEqual(FULL, answer["proposals"][0]["sourceRevision"])


# -- proposal-view ------------------------------------------------------------


class ProposalViewTest(CloudCase):
    BASE = "repositories/acme/infra/pullrequests/7"

    def setUp(self):
        super().setUp()
        self.node = pull(7, source="platform-agent/replicas", source_hash=SHORT, dest_hash=DEST)
        self.mock.routes[("GET", self.BASE)] = self.node
        self.mock.routes[("GET", f"repositories/acme/infra/commit/{SHORT}")] = {"hash": FULL}
        # The pull-request diff route answers 302, as Bitbucket's does. The
        # transport refuses a redirect, so the forge must never ask it.
        self.mock.routes[("GET", f"{self.BASE}/diff")] = Redirect(
            f"{API}/repositories/acme/infra/diff/acme/infra:{SHORT}%0D{DEST}?from_pullrequest_id=7&topic=true")
        self.mock.routes[("GET", f"repositories/acme/infra/diff/{FULL}..{DEST}")] = Text(DIFF)

    def test_the_short_source_hash_is_expanded_to_the_full_id(self):
        answer = self.verb("proposal-view", number=7)
        self.assertEqual(FULL, answer["proposal"]["sourceRevision"])
        self.assertEqual([("GET", self.BASE), ("GET", f"repositories/acme/infra/commit/{SHORT}")], self.mock.calls())

    def test_a_short_hash_is_kept_when_the_commit_cannot_be_read(self):
        self.mock.routes[("GET", f"repositories/acme/infra/commit/{SHORT}")] = Status(404)
        answer = self.verb("proposal-view", number=7)
        self.assertEqual(SHORT, answer["proposal"]["sourceRevision"])

    def test_a_commit_answer_that_is_not_the_same_commit_is_not_taken(self):
        self.mock.routes[("GET", f"repositories/acme/infra/commit/{SHORT}")] = {"hash": "f" * 40}
        self.assertEqual(SHORT, self.verb("proposal-view", number=7)["proposal"]["sourceRevision"])

    def test_a_forks_short_hash_is_kept_and_no_call_goes_to_the_fork(self):
        # The fork's workspace is outside allowed_paths, so the token is not
        # spent there; the fork's revision decides nothing here.
        self.node["source"]["repository"]["full_name"] = "stranger/infra"
        self.assertEqual(SHORT, self.verb("proposal-view", number=7)["proposal"]["sourceRevision"])
        self.assertEqual([("GET", self.BASE)], self.mock.calls())

    def test_the_diff_is_the_repository_diff_of_the_two_commits_and_never_the_pull_request_route(self):
        answer = self.verb("proposal-view", number=7, diff=True)
        self.assertEqual(DIFF, answer["diff"])
        diff = [r for r in self.mock.requests if "/diff/" in r["path"]]
        self.assertEqual(1, len(diff))
        # The expanded source commit, not the 12-character one.
        self.assertEqual(f"repositories/acme/infra/diff/{FULL}..{DEST}", diff[0]["path"])
        self.assertEqual({"topic": ["true"]}, diff[0]["query"])
        self.assertEqual("text/plain", diff[0]["accept"])
        self.assertNotIn(("GET", f"{self.BASE}/diff"), self.mock.calls())

    def test_a_source_on_a_fork_is_named_with_its_repository(self):
        # The fork's commit is not in this repository, so the diff names the
        # repository the commit is in, as Bitbucket's own redirect does.
        self.node["source"]["repository"] = {"full_name": "jdoe/infra"}
        self.mock.routes[("GET", f"repositories/acme/infra/diff/jdoe/infra:{SHORT}..{DEST}")] = Text(DIFF)
        answer = self.verb("proposal-view", number=7, diff=True)
        self.assertEqual(DIFF, answer["diff"])
        diff = [r for r in self.mock.requests if "/diff/" in r["path"]]
        self.assertEqual(f"repositories/acme/infra/diff/jdoe/infra:{SHORT}..{DEST}", diff[0]["path"])

    def test_the_pull_request_diff_route_is_refused_by_the_transport(self):
        # Why the forge avoids it: the mock answers it as Bitbucket does.
        binding = self.broker()._bind({"repository": REPO_URL})
        error = self.refused(lambda: binding.api("GET", f"{self.BASE}/diff", raw="text/plain"))
        self.assertEqual("FORGE_REDIRECTED", error.fields["code"])
        self.assertEqual(1, len(self.mock.requests))

    def test_no_diff_when_bitbucket_does_not_name_both_commits(self):
        self.node["destination"]["commit"] = {}
        answer = self.verb("proposal-view", number=7, diff=True)
        self.assertIn("did not name both commits", answer["diff"])
        self.assertFalse([r for r in self.mock.requests if "diff" in r["path"]])

    def comments(self, rows):
        path = f"{self.BASE}/comments"
        self.mock.routes[("GET", path)] = paged(rows, path)

    def test_comments_come_oldest_first_with_deleted_rows_skipped_and_inline_as_review_comments(self):
        self.comments([
            comment_row(1, "first"),
            comment_row(2, deleted=True),
            comment_row(3, "on a line", inline={"path": "apps/x.yaml", "to": 12, "from": None}),
            comment_row(4, "on an old line", inline={"path": "apps/y.yaml", "to": None, "from": 4}),
        ])
        answer = self.verb("proposal-view", number=7, comments=True)
        self.assertEqual([1, 3, 4], [c["id"] for c in answer["comments"]])
        self.assertEqual(["issue", "review_comment", "review_comment"], [c["kind"] for c in answer["comments"]])
        self.assertEqual(["", "apps/x.yaml", "apps/y.yaml"], [c["path"] for c in answer["comments"]])
        self.assertEqual([None, 12, 4], [c["line"] for c in answer["comments"]])
        self.assertEqual(["issue-1", "review_comment-3", "review_comment-4"], [c["ref"] for c in answer["comments"]])
        self.assertEqual(3, answer["commentCount"])
        self.assertFalse(answer["commentsTruncated"])
        request = next(r for r in self.mock.requests if r["path"].endswith("/comments"))
        self.assertEqual({"pagelen": ["100"], "sort": ["created_on"]}, request["query"])

    def test_comments_follow_pages_and_stop_at_the_limit(self):
        rows = [comment_row(n) for n in range(1, 151)]
        self.comments(rows)
        answer = self.verb("proposal-view", number=7, comments=True, limit=120)
        self.assertEqual(list(range(1, 121)), [c["id"] for c in answer["comments"]])
        self.assertTrue(answer["commentsTruncated"])
        self.comments([comment_row(1), comment_row(2, deleted=True), comment_row(3)])
        answer = self.verb("proposal-view", number=7, comments=True, limit=2)
        self.assertEqual([1, 3], [c["id"] for c in answer["comments"]])
        self.assertFalse(answer["commentsTruncated"], "a deleted row does not count toward the limit")
        self.comments([comment_row(1), comment_row(2), comment_row(3)])
        answer = self.verb("proposal-view", number=7, comments=True, limit=2)
        self.assertEqual([1, 2], [c["id"] for c in answer["comments"]])
        self.assertTrue(answer["commentsTruncated"])

    def test_an_automation_comment_is_marked_as_a_bot(self):
        row = comment_row(1)
        row["user"] = {"type": "app_user", "uuid": ACCOUNT}
        self.comments([row])
        answer = self.verb("proposal-view", number=7, comments=True)
        self.assertEqual([(ACCOUNT, True)], [(c["author"], c["bot"]) for c in answer["comments"]])


# -- writes -------------------------------------------------------------------


class WriteTest(CloudCase):
    PATH = "repositories/acme/infra/pullrequests"

    def test_create_sends_the_fields_and_a_draft_only_when_asked(self):
        self.mock.routes[("POST", self.PATH)] = pull(8, source="fix/a")
        answer = self.verb("proposal-create", title=" Fix it ", body="why", source="fix/a", target="main")
        self.assertEqual(8, answer["proposal"]["number"])
        self.assertEqual(
            {"title": "Fix it", "description": "why", "source": {"branch": {"name": "fix/a"}},
             "destination": {"branch": {"name": "main"}}},
            self.mock.requests[0]["body"],
        )
        self.mock.requests.clear()
        self.verb("proposal-create", title="t", source="fix/a", target="main", draft=False)
        self.assertNotIn("draft", self.mock.requests[0]["body"])
        self.mock.requests.clear()
        self.verb("proposal-create", title="t", source="fix/a", target="main", draft=True)
        self.assertIs(True, self.mock.requests[0]["body"]["draft"])

    def test_update_puts_the_text_and_reads_when_nothing_changes(self):
        self.mock.routes[("PUT", f"{self.PATH}/7")] = pull(7)
        self.mock.routes[("GET", f"{self.PATH}/7")] = pull(7)
        self.verb("proposal-update", number=7, title=" New ", body="b")
        self.assertEqual([("GET", f"{self.PATH}/7"), ("PUT", f"{self.PATH}/7")], self.mock.calls())
        self.assertEqual({"title": "New", "description": "b", "reviewers": []}, self.mock.requests[1]["body"])
        self.mock.requests.clear()
        self.verb("proposal-update", number=7, body="only the body")
        self.assertEqual({"description": "only the body", "reviewers": []}, self.mock.requests[1]["body"])
        self.mock.requests.clear()
        answer = self.verb("proposal-update", number=7)
        self.assertEqual([("GET", f"{self.PATH}/7")], self.mock.calls())
        self.assertEqual(7, answer["proposal"]["number"])

    def test_update_sends_back_the_reviewers_it_read(self):
        # An update that leaves `reviewers` out can remove them, so the ones
        # that were read go back as they were, by uuid.
        reviewed = pull(7, reviewers=[{"type": "user", "uuid": "{r1}"}, {"type": "user", "uuid": "{r2}"}, {}])
        self.mock.routes[("GET", f"{self.PATH}/7")] = reviewed
        self.mock.routes[("PUT", f"{self.PATH}/7")] = reviewed
        self.verb("proposal-update", number=7, title="t")
        put = next(r for r in self.mock.requests if r["method"] == "PUT")
        self.assertEqual([{"uuid": "{r1}"}, {"uuid": "{r2}"}], put["body"]["reviewers"])

    def test_close_declines(self):
        self.mock.routes[("POST", f"{self.PATH}/7/decline")] = pull(7, "DECLINED")
        answer = self.verb("proposal-close", number=7)
        self.assertEqual("closed", answer["proposal"]["state"])
        self.assertEqual([("POST", f"{self.PATH}/7/decline")], self.mock.calls())
        self.assertEqual({}, self.mock.requests[0]["body"])

    def test_comment_sends_content_raw(self):
        self.mock.routes[("POST", f"{self.PATH}/7/comments")] = comment_row(42, "thanks")
        answer = self.verb("proposal-comment", number=7, body="thanks")
        self.assertEqual({"content": {"raw": "thanks"}}, self.mock.requests[0]["body"])
        self.assertEqual(("issue-42", "thanks"), (answer["comment"]["ref"], answer["comment"]["body"]))

    def test_commits_come_back_oldest_first(self):
        newest_first = [{"hash": f"{n:040x}", "message": f"c{n}", "date": f"2026-10-0{n}T00:00:00+00:00",
                         "author": {"raw": f"Person {n} <p{n}@example.com>"},
                         "links": {"html": {"href": f"https://bitbucket.org/acme/infra/commits/{n:040x}"}}}
                        for n in range(9, 0, -1)]
        path = f"{self.PATH}/7/commits"
        self.mock.routes[("GET", path)] = paged(newest_first, path, default_size=4)
        answer = self.verb("proposal-commits", number=7)
        self.assertEqual([f"c{n}" for n in range(1, 10)], [c["message"] for c in answer["commits"]])
        self.assertEqual("Person 1", answer["commits"][0]["author"])
        self.assertEqual(f"{1:040x}", answer["commits"][0]["sha"])
        page_two = self.verb("proposal-commits", number=7, limit=4, page=2)
        self.assertEqual(["c5", "c6", "c7", "c8"], [c["message"] for c in page_two["commits"]])


# -- errors and labels -------------------------------------------------------


class ErrorTest(CloudCase):
    PATH = "repositories/acme/infra/pullrequests/7"

    def test_a_401_is_the_cloud_token_text(self):
        self.mock.routes[("GET", self.PATH)] = Status(401)
        error = self.refused(lambda: self.verb("proposal-view", number=7))
        self.assertEqual(401, error.status)
        self.assertEqual("FORGE_UNAUTHENTICATED", error.fields["code"])
        self.assertEqual(TOKEN_REFUSED_TEXT, str(error))
        self.assertIn("Bitbucket Cloud", str(error))

    def test_a_400_is_a_rejected_request_and_not_a_retry(self):
        self.mock.routes[("POST", "repositories/acme/infra/pullrequests")] = Status(
            400, {"type": "error", "error": {"message": "source: branch not found"}})
        error = self.refused(lambda: self.verb("proposal-create", title="t", source="fix/a", target="main"))
        self.assertEqual((422, "FORGE_REJECTED"), (error.status, error.fields["code"]))
        self.assertEqual(GUIDANCE[422].text, str(error))
        self.assertIn("branch not found", error.fields["detail"])

    def test_a_404_is_the_shared_404(self):
        self.mock.routes[("GET", self.PATH)] = Status(404)
        error = self.refused(lambda: self.verb("proposal-view", number=7))
        self.assertEqual((404, "FORGE_NOT_FOUND", GUIDANCE[404].text), (error.status, error.fields["code"], str(error)))


class LabelTest(CloudCase):
    def test_a_label_field_is_refused_before_any_call(self):
        for verb, payload in (
            ("proposal-list", {"labels": ["x"]}),
            ("proposal-list", {"excludeLabels": ["x"]}),
            ("proposal-create", {"title": "t", "source": "fix/a", "target": "main", "labels": ["x"]}),
            ("proposal-update", {"number": 7, "labelsAdd": ["x"]}),
            ("proposal-update", {"number": 7, "labelsRemove": ["x"]}),
            ("label-ensure", {"name": "x"}),
        ):
            with self.subTest(verb=verb, payload=payload):
                with self.assertRaises(LabelsUnsupported) as caught:
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
                getattr(forge(), name)(api, "acme/infra", payload)


# -- the read-only token --------------------------------------------------------


class ReadCredentialTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.token = self.dir / "token"
        self.token.write_text(TOKEN + "\n")
        self.read = self.dir / "read-token"

    def built(self, read_token_path=None) -> BitbucketCloudForge:
        path = str(self.read) if read_token_path is None else read_token_path
        return BitbucketCloudForge(str(self.token), (), read_token_path=path)

    def test_no_read_token_path_is_no_credential(self):
        self.assertIsInstance(BitbucketCloudForge(str(self.token), ()).read_credential("acme/docs"),
                              providers.NoCredential)

    def test_a_missing_empty_or_malformed_file_is_no_credential(self):
        built = self.built()
        self.assertIsInstance(built.read_credential("acme/docs"), providers.NoCredential)
        for bad in ("", "\n", "two words\n", "a\nb\n"):
            with self.subTest(bad=bad):
                self.read.write_text(bad)
                self.assertIsInstance(built.read_credential("acme/docs"), providers.NoCredential)

    def test_a_stored_read_token_is_presented_and_the_write_token_never_is(self):
        self.read.write_text(READ_TOKEN + "\n")
        built = self.built()
        credential = built.read_credential("acme/docs")
        self.assertIsInstance(credential, providers.StaticFileCredential)
        self.assertEqual({"Authorization": f"Bearer {READ_TOKEN}"}, credential.headers("acme/docs"))
        helper = dict(credential.git_config("acme/docs"))["credential.https://bitbucket.org.helper"].split()
        self.assertEqual([str(self.read), "x-token-auth"], helper[1:])
        self.assertNotIn(str(self.token), " ".join(v for _, v in credential.git_config("acme/docs")))
        self.assertEqual({"Authorization": f"Bearer {TOKEN}"}, built.credential.headers("acme/infra"))

    def test_a_key_added_later_is_read_with_no_restart(self):
        built = self.built()
        self.assertIsInstance(built.read_credential("acme/docs"), providers.NoCredential)
        self.read.write_text(READ_TOKEN)
        self.assertIsInstance(built.read_credential("acme/docs"), providers.StaticFileCredential)
        self.read.unlink()
        self.assertIsInstance(built.read_credential("acme/docs"), providers.NoCredential)

    def test_a_read_token_path_git_may_not_be_handed_stops_the_build(self):
        for bad in ("relative/read-token", "/t/../read-token", "/t/read token"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.built(bad)


# -- git's refusal -------------------------------------------------------------


class GitRefusedTest(CloudCase):
    def refusing(self, stderr: str):
        def git_runner(argv, cwd, check=True, config=()):
            raise subprocess.CalledProcessError(128, argv, "", stderr)

        return git_runner

    def test_a_refused_credential_is_the_forges_own_text_with_no_url(self):
        for stderr in (
            "fatal: Authentication failed for 'https://bitbucket.org/acme/infra.git/'\n",
            "fatal: could not read Username for 'https://bitbucket.org': terminal prompts disabled\n",
            "error: RPC failed; HTTP 401 curl 22 The requested URL returned error: 401\n",
        ):
            with self.subTest(stderr=stderr):
                broker = self.broker(git_runner=self.refusing(stderr))
                with self.assertRaises(WorkspaceError) as caught:
                    broker.clone({"repository": REPO_URL})
                error = caught.exception
                self.assertEqual(401, error.status)
                self.assertEqual("FORGE_UNAUTHENTICATED", error.fields["code"])
                self.assertEqual(GIT_REFUSED_TEXT, str(error))
                self.assertNotIn("https://", str(error))
                self.assertNotIn("acme/infra", str(error))
                self.assertEqual({"code": "FORGE_UNAUTHENTICATED"}, error.fields)

    def test_no_token_to_present_is_named_as_that_and_not_as_a_refused_token(self):
        # git asks for a username when the helper has no token as well: the
        # Secret is not there yet, or the file is empty. That is
        # FORGE_CREDENTIAL_UNAVAILABLE, not "the token expired".
        stderr = "fatal: could not read Username for 'https://bitbucket.org': terminal prompts disabled\n"
        for state in ("missing", "empty"):
            with self.subTest(state=state):
                broker = self.broker(git_runner=self.refusing(stderr))
                if state == "missing":
                    self.token.unlink()
                else:
                    self.token.write_text("")
                with self.assertRaises(WorkspaceError) as caught:
                    broker.clone({"repository": REPO_URL})
                self.assertEqual("FORGE_CREDENTIAL_UNAVAILABLE", caught.exception.fields["code"])
                self.assertNotEqual(GIT_REFUSED_TEXT, str(caught.exception))
                self.token.write_text(TOKEN + "\n")

    def answering(self, stderr: str):
        # A runner that does what `check=True` callers need and answers every
        # `ls-remote` and `push` as git does when the server refuses the token.
        def git_runner(argv, cwd, check=True, config=()):
            if any(verb in argv for verb in ("ls-remote", "push")):
                return subprocess.CompletedProcess(argv, 128, "", stderr)
            return subprocess.CompletedProcess(argv, 0, "", "")

        return git_runner

    def test_the_remote_branch_reads_answer_a_refused_token_the_same_way(self):
        # branch-view and branch-delete read the remote with check=False, so
        # git's failure never raises; the refusal is still the forge's text,
        # with no URL, not a retryable FORGE_CALL_FAILED with git's stderr.
        stderr = "fatal: Authentication failed for 'https://bitbucket.org/acme/infra.git/'\n"
        calls = (
            lambda broker: broker.branch_view({"repository": REPO_URL, "branch": "platform-agent/x"}),
            lambda broker: broker.branch_delete(
                {"repository": REPO_URL, "branch": "platform-agent/x", "revision": "a" * 40}),
        )
        # branch-delete lists the branch's proposals first: none.
        self.mock.routes[("GET", "repositories/acme/infra/pullrequests")] = {"values": []}
        for call in calls:
            broker = self.broker(git_runner=self.answering(stderr))
            with self.subTest(call=call), self.assertRaises(WorkspaceError) as caught:
                call(broker)
            self.assertEqual("FORGE_UNAUTHENTICATED", caught.exception.fields["code"])
            self.assertEqual(GIT_REFUSED_TEXT, str(caught.exception))

    def test_another_git_failure_keeps_the_generic_failure(self):
        broker = self.broker(git_runner=self.refusing("fatal: unable to access: Could not resolve host\n"))
        with self.assertRaises(subprocess.CalledProcessError):
            broker.clone({"repository": REPO_URL})

    def test_a_forge_with_no_git_refused_text_keeps_the_generic_failure(self):
        path = self.dir / "github.json"
        path.write_text(json.dumps({"forges": [{"provider": "github", "host": "github.com"}]}))
        os.environ[FORGES_CONFIG_ENV] = str(path)
        stderr = "fatal: Authentication failed for 'https://github.com/acme/infra.git/'\n"
        broker = vcs_broker.VcsBroker(self.dir / "scratch-github", git_runner=self.refusing(stderr),
                                      refresh=lambda *a: None)
        self.assertEqual("", broker.registry.forges[0].git_refused)
        with self.assertRaises(subprocess.CalledProcessError):
            broker.clone({"repository": "https://github.com/acme/infra"})


if __name__ == "__main__":
    unittest.main()
