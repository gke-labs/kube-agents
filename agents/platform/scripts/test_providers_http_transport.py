#!/usr/bin/env python3
"""The in-process transport a forge with a REST API is reached through.

    python3 -m pytest -q agents/platform/scripts/test_providers_http_transport.py

No network: every test hands the transport an opener that records the request
and answers from a fixture, which is also how the broker's tests reach it. What
is pinned is what the transport owns and no forge may -- the URL it composes,
the bounds it enforces, how a refusal becomes a caller-facing answer, and that
the credential never follows a redirect.
"""

from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import providers
import vcs_broker
from providers.transport import HttpTransport, _RefuseRedirect
from workspace_paths import WorkspaceError

BASE = "https://forge.example.test/api/v4"


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Opener:
    """Records each request and answers with the next fixture."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, _Response):
            return answer
        return _Response(json.dumps(answer).encode())


def refusal(status: int, body: object) -> urllib.error.HTTPError:
    text = body if isinstance(body, str) else json.dumps(body)
    return urllib.error.HTTPError(
        f"{BASE}/x", status, "refused", {}, io.BytesIO(text.encode())
    )


def transport(opener, headers=None, **kwargs) -> HttpTransport:
    kwargs.setdefault("timeout", 7.0)
    kwargs.setdefault("max_bytes", 1 << 16)
    return HttpTransport(BASE, headers or (lambda: {"PRIVATE-TOKEN": "t"}), opener=opener, **kwargs)


class RequestTest(unittest.TestCase):
    def test_a_get_is_composed_from_the_base_the_path_and_the_params(self):
        opener = Opener([{"iid": 1}])
        answer = transport(opener).api(
            "GET", "projects/acme%2Finfra/merge_requests", params={"state": "opened", "page": None}
        )
        self.assertEqual([{"iid": 1}], answer)
        request = opener.requests[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual(f"{BASE}/projects/acme%2Finfra/merge_requests?state=opened", request.full_url)
        self.assertEqual("application/json", request.get_header("Accept"))
        self.assertEqual([7.0], opener.timeouts)

    def test_the_credential_headers_are_read_per_call(self):
        # A rotated token file is the next call's token, with no restart.
        tokens = iter(["old", "new"])
        opener = Opener({}, {})
        sender = transport(opener, headers=lambda: {"PRIVATE-TOKEN": next(tokens)})
        sender.api("GET", "user")
        sender.api("GET", "user")
        self.assertEqual(
            ["old", "new"], [r.get_header("Private-token") for r in opener.requests]
        )

    def test_a_body_is_sent_as_json(self):
        opener = Opener({"iid": 3})
        transport(opener).api("POST", "projects/1/issues", body={"title": "t"})
        request = opener.requests[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"title": "t"}, json.loads(request.data))
        self.assertEqual("application/json", request.get_header("Content-type"))

    def test_a_raw_answer_is_returned_as_text_under_the_media_type_asked(self):
        opener = Opener(_Response(b"diff --git a/x b/x\n"))
        text = transport(opener).api("GET", "projects/1/merge_requests/2/raw_diffs", raw="text/plain")
        self.assertEqual("diff --git a/x b/x\n", text)
        self.assertEqual("text/plain", opener.requests[0].get_header("Accept"))

    def test_a_path_that_names_a_host_or_climbs_out_is_not_sent(self):
        # The credential header goes wherever the URL does.
        for path in ("https://elsewhere.test/api", "projects/../../admin", "../user"):
            with self.subTest(path=path):
                opener = Opener()
                with self.assertRaises(WorkspaceError) as caught:
                    transport(opener).api("GET", path)
                self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])
                self.assertEqual([], opener.requests)

    def test_only_https_is_accepted(self):
        with self.assertRaises(ValueError):
            HttpTransport("http://forge.example.test/api/v4", dict, timeout=1, max_bytes=1)


class BoundsTest(unittest.TestCase):
    def test_an_answer_over_the_ceiling_is_refused_not_truncated(self):
        opener = Opener(_Response(b"x" * 65))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener, max_bytes=64).api("GET", "projects")
        self.assertEqual("FORGE_RESPONSE_TOO_LARGE", caught.exception.fields["code"])

    def test_a_call_that_got_no_answer_is_a_call_failure(self):
        opener = Opener(urllib.error.URLError("connection refused"))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener).api("GET", "projects")
        self.assertEqual(502, caught.exception.status)
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_a_timeout_is_a_call_failure(self):
        opener = Opener(TimeoutError("timed out"))
        with self.assertRaises(WorkspaceError) as caught:
            transport(opener).api("GET", "projects")
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_a_redirect_is_never_followed(self):
        # The credential rides in a header; a hop would present it to wherever
        # the redirect pointed.
        handler = _RefuseRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/")
        )
        opener = transport(None)._open.__self__
        self.assertTrue(any(isinstance(h, _RefuseRedirect) for h in opener.handlers))

    def test_a_redirect_says_the_host_is_misconfigured_not_retry(self):
        # Review finding: the declined hop arrives as an HTTPError, and a 3xx
        # has no entry in the shared table, so it read "one retry is
        # reasonable" -- for a host that will redirect every time.
        for status in (301, 302, 307):
            with self.subTest(status=status):
                with self.assertRaises(WorkspaceError) as caught:
                    transport(Opener(refusal(status, "moved"))).api("GET", "projects")
                self.assertEqual("FORGE_REDIRECTED", caught.exception.fields["code"])
                self.assertIn("misconfigured", str(caught.exception))

    def test_a_broken_response_is_a_call_failure_not_an_opaque_error(self):
        # Review finding: `urllib` wraps only the send in URLError; a bad
        # status line or a chunked body cut short raises http.client's own.
        for raised in (http.client.BadStatusLine("x"), http.client.IncompleteRead(b"{", 9)):
            with self.subTest(raised=type(raised).__name__):
                with self.assertRaises(WorkspaceError) as caught:
                    transport(Opener(raised)).api("GET", "projects")
                self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_a_peer_that_trickles_is_cut_off_at_the_calls_deadline(self):
        # Review finding: the opener's timeout bounds each receive, so a byte
        # inside every window held the call -- and a request slot -- forever.
        class Trickle(_Response):
            def read1(self, n=-1):
                return b" "

        clock = iter(range(0, 10_000, 5))
        opener = Opener(Trickle(b""))
        with mock.patch("providers.transport.time.monotonic", lambda: next(clock)):
            with self.assertRaises(WorkspaceError) as caught:
                transport(opener, timeout=30.0).api("GET", "projects")
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])
        self.assertIn("longer than 30s", caught.exception.fields["detail"])

    def test_a_body_cut_short_of_its_length_is_a_call_failure_not_the_answer(self):
        # Review round 2: `read1` answers b"" at EOF without raising, so a
        # diff the peer cut short came back as the whole diff.
        class CutShort(_Response):
            length = 40  # what http.client still expected when the peer closed

        for raw in (None, "text/plain"):
            with self.subTest(raw=raw):
                with self.assertRaises(WorkspaceError) as caught:
                    transport(Opener(CutShort(b"diff --git a/x"))).api("GET", "diff", raw=raw)
                self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])
                self.assertIn("before its answer was complete", caught.exception.fields["detail"])

    def test_each_receive_is_bounded_by_what_is_left_of_the_deadline(self):
        # Review round 2: the opener's full timeout bounded every receive, so
        # one byte just inside the deadline bought another whole window.
        armed = []

        class Sock:
            def settimeout(self, seconds):
                armed.append(seconds)

        response = _Response(b"{}")
        response.fp = mock.Mock(raw=mock.Mock(_sock=Sock()))
        clock = iter([0.0, 4.0, 6.0])
        with mock.patch("providers.transport.time.monotonic", lambda: next(clock)):
            transport(Opener(response), timeout=7.0).api("GET", "projects")
        self.assertEqual([3.0, 1.0], armed)

    def test_the_ceiling_refuses_before_the_whole_body_arrives(self):
        reads = []

        class Endless(_Response):
            def read1(self, n=-1):
                reads.append(n)
                return b"x" * 1024

        with self.assertRaises(WorkspaceError) as caught:
            transport(Opener(Endless(b"")), max_bytes=4096).api("GET", "projects")
        self.assertEqual("FORGE_RESPONSE_TOO_LARGE", caught.exception.fields["code"])
        self.assertLessEqual(len(reads), 5)


class RefusalTest(unittest.TestCase):
    def refuse(self, status, body, **kwargs):
        with self.assertRaises(WorkspaceError) as caught:
            transport(Opener(refusal(status, body)), **kwargs).api("GET", "projects/1")
        return caught.exception

    def test_a_status_takes_the_shared_guidance_and_the_forges_reason(self):
        err = self.refuse(404, {"message": "404 Project Not Found"})
        self.assertEqual(404, err.status)
        self.assertEqual("FORGE_NOT_FOUND", err.fields["code"])
        self.assertIn("404 Project Not Found", err.fields["detail"])

    def test_an_error_key_is_read_as_well_as_message(self):
        self.assertIn("404 Not Found", self.refuse(404, {"error": "404 Not Found"}).fields["detail"])

    def test_a_message_that_is_a_list_is_joined(self):
        err = self.refuse(409, {"message": ["Another open merge request already exists"]})
        self.assertEqual("FORGE_CONFLICT", err.fields["code"])
        self.assertIn("Another open merge request already exists", err.fields["detail"])

    def test_per_field_reasons_name_their_field(self):
        err = self.refuse(422, {"message": {"title": ["is too long"]}})
        self.assertEqual("FORGE_REJECTED", err.fields["code"])
        self.assertIn("title: is too long", err.fields["detail"])

    def test_a_plain_text_body_gives_its_first_line(self):
        self.assertIn("upstream gone", self.refuse(500, "upstream gone\nmore").fields["detail"])

    def test_a_body_nested_past_the_recursion_limit_keeps_the_status(self):
        # Review finding: RecursionError escaped and the 404 was lost.
        deep = "[" * 100_000 + "]" * 100_000
        err = self.refuse(404, deep)
        self.assertEqual("FORGE_NOT_FOUND", err.fields["code"])
        with self.assertRaises(WorkspaceError) as caught:
            transport(Opener(_Response(deep.encode())), max_bytes=1 << 20).api("GET", "projects")
        self.assertEqual("FORGE_CALL_FAILED", caught.exception.fields["code"])

    def test_a_body_the_parser_accepts_but_nests_past_the_frame_limit_keeps_the_status(self):
        # Review round 2: the C scanner's nesting budget is larger than
        # Python's frame limit, so this parses -- and the walk overflowed.
        deep = "[" * 1_500 + '"why"' + "]" * 1_500
        self.assertEqual("FORGE_NOT_FOUND", self.refuse(404, deep).fields["code"])

    def test_a_refusal_whose_body_breaks_off_or_stalls_keeps_its_status(self):
        # Review round 2: the error body was read outside the deadline and
        # outside the failure handling, so a reset or a stall became a bare
        # exception and the forge's status was lost.
        class Broken(io.BytesIO):
            def read1(self, n=-1):
                raise ConnectionResetError("reset")

        class Trickle(io.BytesIO):
            def read1(self, n=-1):
                return b" "

        for body in (Broken(b""), Trickle(b"")):
            with self.subTest(body=type(body).__name__):
                exc = urllib.error.HTTPError(f"{BASE}/x", 404, "refused", {}, body)
                clock = iter(range(0, 10_000, 5))
                with mock.patch("providers.transport.time.monotonic", lambda: next(clock)):
                    with self.assertRaises(WorkspaceError) as caught:
                        transport(Opener(exc), timeout=30.0).api("GET", "projects/1")
                self.assertEqual("FORGE_NOT_FOUND", caught.exception.fields["code"])

    def test_a_forge_override_is_applied(self):
        named = providers.Guidance(401, "FORGE_TOKEN_EXPIRED", "the token in Secret x expired")
        err = self.refuse(401, {"message": "401 Unauthorized"}, overrides={401: named})
        self.assertEqual("FORGE_TOKEN_EXPIRED", err.fields["code"])


class WhoamiTest(unittest.TestCase):
    def test_the_declared_route_names_the_login(self):
        opener = Opener({"username": "group_42_bot_abc", "id": 9})
        login = transport(opener, whoami_route=("user", "username")).whoami()
        self.assertEqual("group_42_bot_abc", login)
        self.assertTrue(opener.requests[0].full_url.endswith("/user"))

    def test_no_route_says_so_rather_than_guessing(self):
        self.assertEqual("", transport(Opener()).whoami())

    def test_a_refused_lookup_raises_rather_than_answering_empty(self):
        opener = Opener(refusal(401, {"message": "401 Unauthorized"}))
        with self.assertRaises(WorkspaceError):
            transport(opener, whoami_route=("user", "username")).whoami()


class _HttpForge(providers.Forge):
    name = "httpforge"
    hosts = ("forge.example.test",)
    transport = "http"
    verbs = ("issue-view",)

    def __init__(self, api_url=BASE):
        super().__init__()
        self.api_url = api_url


class BrokerBuildsItTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def broker(self, **kwargs):
        return vcs_broker.VcsBroker(self.root, git_runner=lambda *a, **k: None, **kwargs)

    def test_an_http_forge_gets_the_in_process_transport_with_the_brokers_bounds(self):
        opener = Opener({})
        broker = self.broker(http_timeout=3.0, http_max_bytes=99, http_opener=opener)
        built = broker._transport(_HttpForge(), "acme/infra")
        self.assertIsInstance(built, HttpTransport)
        built.api("GET", "user")
        self.assertEqual([3.0], opener.timeouts)
        self.assertEqual(99, built._max_bytes)

    def test_an_http_forge_with_no_api_root_is_refused_by_name(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.broker()._transport(_HttpForge(api_url=""), "acme/infra")
        self.assertEqual("FORGE_UNSUPPORTED", caught.exception.fields["code"])


if __name__ == "__main__":
    unittest.main()
